from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

from .workflow import BlockStatus, Workflow, WorkflowExecutionError, serialize

TClient = TypeVar("TClient")
StopPredicate = Callable[["LoopContext[Any]"], bool]


class LoopStatus(str, Enum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class LoopOptions:
    max_iterations: int | None = 1
    interval_seconds: float = 0.0
    continue_on_error: bool = False
    stop_file: Path | None = None
    checkpoint_path: Path | None = None

    def __post_init__(self) -> None:
        if self.max_iterations is not None and self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1 or None")
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds must not be negative")
        if self.max_iterations is None and self.stop_file is None:
            raise ValueError("an infinite loop requires stop_file")

    @classmethod
    def from_mapping(cls, value: MutableMapping[str, Any]) -> "LoopOptions":
        stop_file = value.get("stop_file")
        checkpoint_path = value.get("checkpoint_path")
        return cls(
            max_iterations=value.get("max_iterations", 1),
            interval_seconds=float(value.get("interval_seconds", 0.0)),
            continue_on_error=bool(value.get("continue_on_error", False)),
            stop_file=Path(stop_file) if stop_file else None,
            checkpoint_path=Path(checkpoint_path) if checkpoint_path else None,
        )


@dataclass
class LoopIteration:
    iteration: int
    status: BlockStatus
    started_at: float
    finished_at: float
    workflow_run: dict[str, Any] | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return round((self.finished_at - self.started_at) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "workflow_run": self.workflow_run,
            "error": self.error,
        }


@dataclass
class LoopContext(Generic[TClient]):
    client: TClient
    variables: MutableMapping[str, Any]
    iterations: list[LoopIteration] = field(default_factory=list)

    @property
    def iteration(self) -> int:
        return len(self.iterations)

    @property
    def last(self) -> LoopIteration | None:
        return self.iterations[-1] if self.iterations else None


@dataclass
class LoopRun(Generic[TClient]):
    workflow_name: str
    status: LoopStatus
    context: LoopContext[TClient]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "status": self.status.value,
            "reason": self.reason,
            "variables": serialize(self.context.variables),
            "iterations": [item.to_dict() for item in self.context.iterations],
        }


class WorkflowLoop(Generic[TClient]):
    def __init__(
        self,
        workflow: Workflow[TClient],
        options: LoopOptions | None = None,
        *,
        stop_when: StopPredicate | None = None,
    ) -> None:
        self.workflow = workflow
        self.options = options or LoopOptions()
        self.stop_when = stop_when

    def _stop_file_exists(self) -> bool:
        return bool(self.options.stop_file and self.options.stop_file.exists())

    def _write_checkpoint(
        self,
        status: LoopStatus,
        context: LoopContext[TClient],
        reason: str,
    ) -> None:
        path = self.options.checkpoint_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = LoopRun(
            workflow_name=self.workflow.name,
            status=status,
            context=context,
            reason=reason,
        ).to_dict()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    async def run(
        self,
        client: TClient,
        variables: MutableMapping[str, Any] | None = None,
    ) -> LoopRun[TClient]:
        shared = variables if variables is not None else {}
        context = LoopContext(client=client, variables=shared)
        iteration = 0

        try:
            while True:
                if self._stop_file_exists():
                    result = LoopRun(
                        self.workflow.name,
                        LoopStatus.STOPPED,
                        context,
                        "stop_file_exists",
                    )
                    self._write_checkpoint(result.status, context, result.reason)
                    return result

                if (
                    self.options.max_iterations is not None
                    and iteration >= self.options.max_iterations
                ):
                    result = LoopRun(
                        self.workflow.name,
                        LoopStatus.COMPLETED,
                        context,
                        "max_iterations_reached",
                    )
                    self._write_checkpoint(result.status, context, result.reason)
                    return result

                iteration += 1
                shared["loop_iteration"] = iteration
                shared["loop_started_at"] = time.time()
                started_at = time.monotonic()

                try:
                    workflow_run = await self.workflow.run(client, shared)
                    item = LoopIteration(
                        iteration=iteration,
                        status=workflow_run.status,
                        started_at=started_at,
                        finished_at=time.monotonic(),
                        workflow_run=workflow_run.to_dict(),
                    )
                except WorkflowExecutionError as exc:
                    item = LoopIteration(
                        iteration=iteration,
                        status=BlockStatus.FAILED,
                        started_at=started_at,
                        finished_at=time.monotonic(),
                        workflow_run={
                            "name": self.workflow.name,
                            "status": BlockStatus.FAILED.value,
                            "variables": serialize(exc.context.variables),
                            "results": serialize(exc.context.results),
                            "trace": serialize(
                                [entry.to_dict() for entry in exc.context.trace]
                            ),
                        },
                        error=str(exc),
                    )
                    context.iterations.append(item)
                    self._write_checkpoint(
                        LoopStatus.FAILED,
                        context,
                        "workflow_error",
                    )
                    if not self.options.continue_on_error:
                        return LoopRun(
                            self.workflow.name,
                            LoopStatus.FAILED,
                            context,
                            "workflow_error",
                        )
                else:
                    context.iterations.append(item)
                    if item.status is BlockStatus.FAILED:
                        self._write_checkpoint(
                            LoopStatus.FAILED,
                            context,
                            "workflow_returned_failed",
                        )
                        if not self.options.continue_on_error:
                            return LoopRun(
                                self.workflow.name,
                                LoopStatus.FAILED,
                                context,
                                "workflow_returned_failed",
                            )
                    else:
                        self._write_checkpoint(
                            LoopStatus.COMPLETED,
                            context,
                            "iteration_completed",
                        )

                if self.stop_when and self.stop_when(context):
                    result = LoopRun(
                        self.workflow.name,
                        LoopStatus.STOPPED,
                        context,
                        "stop_predicate_matched",
                    )
                    self._write_checkpoint(result.status, context, result.reason)
                    return result

                if self._stop_file_exists():
                    result = LoopRun(
                        self.workflow.name,
                        LoopStatus.STOPPED,
                        context,
                        "stop_file_exists",
                    )
                    self._write_checkpoint(result.status, context, result.reason)
                    return result

                if (
                    self.options.max_iterations is not None
                    and iteration >= self.options.max_iterations
                ):
                    result = LoopRun(
                        self.workflow.name,
                        LoopStatus.COMPLETED,
                        context,
                        "max_iterations_reached",
                    )
                    self._write_checkpoint(result.status, context, result.reason)
                    return result

                if self.options.interval_seconds:
                    remaining = self.options.interval_seconds
                    while remaining > 0:
                        if self._stop_file_exists():
                            result = LoopRun(
                                self.workflow.name,
                                LoopStatus.STOPPED,
                                context,
                                "stop_file_exists",
                            )
                            self._write_checkpoint(
                                result.status, context, result.reason
                            )
                            return result
                        chunk = min(0.25, remaining)
                        await asyncio.sleep(chunk)
                        remaining -= chunk
        except asyncio.CancelledError:
            result = LoopRun(
                self.workflow.name,
                LoopStatus.CANCELLED,
                context,
                "cancelled",
            )
            self._write_checkpoint(result.status, context, result.reason)
            raise
