from __future__ import annotations

import inspect
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

TClient = TypeVar("TClient")
MaybeAwaitable = Any | Awaitable[Any]
Predicate = Callable[["WorkflowContext[Any]"], MaybeAwaitable]


class BlockStatus(str, Enum):
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class BlockExecution:
    block_id: str
    block_type: str
    status: BlockStatus
    started_at: float
    finished_at: float
    output: Any = None
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return round((self.finished_at - self.started_at) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class WorkflowContext(Generic[TClient]):
    client: TClient
    variables: MutableMapping[str, Any] = field(default_factory=dict)
    results: MutableMapping[str, Any] = field(default_factory=dict)
    trace: list[BlockExecution] = field(default_factory=list)

    def require(self, key: str) -> Any:
        if key not in self.variables:
            raise KeyError(f"workflow variable {key!r} is required")
        return self.variables[key]

    def result(self, block_id: str) -> Any:
        if block_id not in self.results:
            raise KeyError(f"workflow block {block_id!r} has no result")
        return self.results[block_id]


class WorkflowBlock(ABC, Generic[TClient]):
    retry_safe = True

    def __init__(self, block_id: str) -> None:
        block_id = block_id.strip()
        if not block_id:
            raise ValueError("block_id must not be empty")
        self.block_id = block_id

    @abstractmethod
    async def run(self, context: WorkflowContext[TClient]) -> Any:
        raise NotImplementedError


async def resolve(value: MaybeAwaitable) -> Any:
    return await value if inspect.isawaitable(value) else value


def serialize(value: Any) -> Any:
    """Convert workflow outputs to JSON-compatible structures when possible."""
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return serialize(value.to_dict())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, MutableMapping):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


class ActionBlock(WorkflowBlock[TClient]):
    """A small adapter for one-off actions without defining a new class."""

    def __init__(
        self,
        block_id: str,
        action: Callable[[WorkflowContext[TClient]], MaybeAwaitable],
        *,
        retry_safe: bool = False,
    ) -> None:
        super().__init__(block_id)
        self.action = action
        self.retry_safe = retry_safe

    async def run(self, context: WorkflowContext[TClient]) -> Any:
        return await resolve(self.action(context))


class SequenceBlock(WorkflowBlock[TClient]):
    def __init__(self, block_id: str, blocks: Iterable[WorkflowBlock[TClient]]) -> None:
        super().__init__(block_id)
        self.blocks = list(blocks)
        self.retry_safe = all(
            getattr(block, "retry_safe", False) for block in self.blocks
        )

    async def run(self, context: WorkflowContext[TClient]) -> list[Any]:
        outputs = []
        for block in self.blocks:
            outputs.append(await block.run(context))
        return outputs


class WhenBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        predicate: Predicate,
        then_blocks: Iterable[WorkflowBlock[TClient]],
        else_blocks: Iterable[WorkflowBlock[TClient]] = (),
    ) -> None:
        super().__init__(block_id)
        self.predicate = predicate
        self.then_blocks = list(then_blocks)
        self.else_blocks = list(else_blocks)
        self.retry_safe = all(
            getattr(block, "retry_safe", False)
            for block in [*self.then_blocks, *self.else_blocks]
        )

    async def run(self, context: WorkflowContext[TClient]) -> dict[str, Any]:
        matched = bool(await resolve(self.predicate(context)))
        selected = self.then_blocks if matched else self.else_blocks
        outputs = [await block.run(context) for block in selected]
        return {"matched": matched, "outputs": outputs}


class RetryBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        block: WorkflowBlock[TClient],
        attempts: int = 3,
        delay_seconds: float = 0.0,
        retry_on: tuple[type[BaseException], ...] = (Exception,),
    ) -> None:
        super().__init__(block_id)
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        if not getattr(block, "retry_safe", True):
            raise ValueError(
                f"block {block.block_id!r} has ambiguous side effects and cannot "
                "be wrapped in generic RetryBlock"
            )
        self.block = block
        self.attempts = attempts
        self.delay_seconds = delay_seconds
        self.retry_on = retry_on

    async def run(self, context: WorkflowContext[TClient]) -> dict[str, Any]:
        import asyncio

        last_error: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                output = await self.block.run(context)
                return {"attempt": attempt, "output": output}
            except self.retry_on as exc:
                last_error = exc
                if attempt < self.attempts and self.delay_seconds:
                    await asyncio.sleep(self.delay_seconds)
        assert last_error is not None
        raise last_error


class DelayBlock(WorkflowBlock[TClient]):
    def __init__(self, seconds: float, block_id: str = "delay") -> None:
        super().__init__(block_id)
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        self.seconds = seconds

    async def run(self, context: WorkflowContext[TClient]) -> float:
        import asyncio

        await asyncio.sleep(self.seconds)
        return self.seconds


class SetVariableBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        key: str,
        value: Callable[[WorkflowContext[TClient]], MaybeAwaitable] | Any,
        block_id: str | None = None,
    ) -> None:
        super().__init__(block_id or f"set_{key}")
        if not key:
            raise ValueError("variable key must not be empty")
        self.key = key
        self.value = value

    async def run(self, context: WorkflowContext[TClient]) -> Any:
        raw = self.value(context) if callable(self.value) else self.value
        value = await resolve(raw)
        context.variables[self.key] = value
        return value


class WaitUntilBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        predicate: Predicate,
        *,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.1,
    ) -> None:
        super().__init__(block_id)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.predicate = predicate
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    async def run(self, context: WorkflowContext[TClient]) -> bool:
        import asyncio

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if bool(await resolve(self.predicate(context))):
                return True
            await asyncio.sleep(self.poll_seconds)
        raise TimeoutError(
            f"condition {self.block_id!r} did not match within "
            f"{self.timeout_seconds} seconds"
        )


class TimeoutBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        block: WorkflowBlock[TClient],
        timeout_seconds: float,
    ) -> None:
        super().__init__(block_id)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.block = block
        self.timeout_seconds = timeout_seconds
        self.retry_safe = getattr(block, "retry_safe", False)

    async def run(self, context: WorkflowContext[TClient]) -> Any:
        import asyncio

        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self.block.run(context)
        except TimeoutError as exc:
            raise TimeoutError(
                f"block {self.block.block_id!r} exceeded "
                f"{self.timeout_seconds} seconds"
            ) from exc


class RepeatBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        blocks: Iterable[WorkflowBlock[TClient]],
        *,
        times: int | None = None,
        while_predicate: Predicate | None = None,
        max_iterations: int = 100,
    ) -> None:
        super().__init__(block_id)
        if times is None and while_predicate is None:
            raise ValueError("repeat requires times or while_predicate")
        if times is not None and times < 0:
            raise ValueError("times must not be negative")
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.blocks = list(blocks)
        self.times = times
        self.while_predicate = while_predicate
        self.max_iterations = max_iterations
        self.retry_safe = all(
            getattr(block, "retry_safe", False) for block in self.blocks
        )

    async def run(self, context: WorkflowContext[TClient]) -> list[list[Any]]:
        outputs: list[list[Any]] = []
        iteration = 0
        while iteration < self.max_iterations:
            if self.times is not None and iteration >= self.times:
                break
            if self.while_predicate is not None and not bool(
                await resolve(self.while_predicate(context))
            ):
                break
            iteration += 1
            context.variables[f"{self.block_id}_iteration"] = iteration
            outputs.append([await block.run(context) for block in self.blocks])
        else:
            if self.while_predicate is not None:
                raise RuntimeError(
                    f"repeat block {self.block_id!r} reached max_iterations="
                    f"{self.max_iterations}"
                )
        return outputs


class TryBlock(WorkflowBlock[TClient]):
    def __init__(
        self,
        block_id: str,
        try_blocks: Iterable[WorkflowBlock[TClient]],
        *,
        except_blocks: Iterable[WorkflowBlock[TClient]] = (),
        finally_blocks: Iterable[WorkflowBlock[TClient]] = (),
        reraise: bool = False,
    ) -> None:
        super().__init__(block_id)
        self.try_blocks = list(try_blocks)
        self.except_blocks = list(except_blocks)
        self.finally_blocks = list(finally_blocks)
        self.reraise = reraise
        self.retry_safe = all(
            getattr(block, "retry_safe", False)
            for block in [*self.try_blocks, *self.except_blocks, *self.finally_blocks]
        )

    async def run(self, context: WorkflowContext[TClient]) -> dict[str, Any]:
        output: list[Any] = []
        handled: list[Any] = []
        final: list[Any] = []
        error: BaseException | None = None
        try:
            output = [await block.run(context) for block in self.try_blocks]
        except Exception as exc:
            error = exc
            context.variables[f"{self.block_id}_error"] = exc
            handled = [await block.run(context) for block in self.except_blocks]
        finally:
            final = [await block.run(context) for block in self.finally_blocks]

        if error is not None and self.reraise:
            raise error
        return {
            "output": output,
            "handled": handled,
            "finally": final,
            "error": f"{type(error).__name__}: {error}" if error else None,
        }


@dataclass
class WorkflowRun(Generic[TClient]):
    name: str
    context: WorkflowContext[TClient]
    status: BlockStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "variables": serialize(self.context.variables),
            "results": serialize(self.context.results),
            "trace": serialize([item.to_dict() for item in self.context.trace]),
        }


class Workflow(Generic[TClient]):
    def __init__(
        self,
        name: str,
        blocks: Iterable[WorkflowBlock[TClient]] = (),
        *,
        fail_fast: bool = True,
    ) -> None:
        self.name = name.strip()
        if not self.name:
            raise ValueError("workflow name must not be empty")
        self.blocks = list(blocks)
        self.fail_fast = fail_fast
        self._validate_unique_ids()

    @staticmethod
    def _ensure_unique_ids(blocks: Iterable[WorkflowBlock[TClient]]) -> None:
        ids = [block.block_id for block in blocks]
        duplicates = sorted({block_id for block_id in ids if ids.count(block_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate workflow block ids: {', '.join(duplicates)}")

    def _validate_unique_ids(self) -> None:
        self._ensure_unique_ids(self.blocks)

    def _replace_blocks(
        self, blocks: list[WorkflowBlock[TClient]]
    ) -> "Workflow[TClient]":
        self._ensure_unique_ids(blocks)
        self.blocks = blocks
        return self

    def _index(self, block_id: str) -> int:
        for index, block in enumerate(self.blocks):
            if block.block_id == block_id:
                return index
        raise KeyError(f"workflow block {block_id!r} does not exist")

    def then(self, block: WorkflowBlock[TClient]) -> "Workflow[TClient]":
        return self._replace_blocks([*self.blocks, block])

    def insert_before(
        self, target_id: str, block: WorkflowBlock[TClient]
    ) -> "Workflow[TClient]":
        index = self._index(target_id)
        blocks = [*self.blocks]
        blocks.insert(index, block)
        return self._replace_blocks(blocks)

    def insert_after(
        self, target_id: str, block: WorkflowBlock[TClient]
    ) -> "Workflow[TClient]":
        index = self._index(target_id) + 1
        blocks = [*self.blocks]
        blocks.insert(index, block)
        return self._replace_blocks(blocks)

    def replace(
        self, target_id: str, block: WorkflowBlock[TClient]
    ) -> "Workflow[TClient]":
        index = self._index(target_id)
        blocks = [*self.blocks]
        blocks[index] = block
        return self._replace_blocks(blocks)

    def remove(self, block_id: str) -> "Workflow[TClient]":
        del self.blocks[self._index(block_id)]
        return self

    def clone(self, name: str | None = None) -> "Workflow[TClient]":
        return Workflow(name or self.name, list(self.blocks), fail_fast=self.fail_fast)

    async def _execute(
        self,
        context: WorkflowContext[TClient],
    ) -> WorkflowRun[TClient]:
        overall = BlockStatus.PASSED

        for block in self.blocks:
            started_at = time.monotonic()
            try:
                output = await block.run(context)
            except Exception as exc:
                finished_at = time.monotonic()
                context.trace.append(
                    BlockExecution(
                        block_id=block.block_id,
                        block_type=type(block).__name__,
                        status=BlockStatus.FAILED,
                        started_at=started_at,
                        finished_at=finished_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                overall = BlockStatus.FAILED
                if self.fail_fast:
                    raise WorkflowExecutionError(
                        self.name, block.block_id, exc, context
                    ) from exc
                continue

            finished_at = time.monotonic()
            context.results[block.block_id] = output
            context.trace.append(
                BlockExecution(
                    block_id=block.block_id,
                    block_type=type(block).__name__,
                    status=BlockStatus.PASSED,
                    started_at=started_at,
                    finished_at=finished_at,
                    output=output,
                )
            )

        return WorkflowRun(name=self.name, context=context, status=overall)

    async def run(
        self,
        client: TClient,
        variables: MutableMapping[str, Any] | None = None,
    ) -> WorkflowRun[TClient]:
        context = WorkflowContext(
            client=client,
            variables=variables if variables is not None else {},
        )
        guard_factory = getattr(client, "workflow_guard", None)
        if guard_factory is None:
            return await self._execute(context)
        async with guard_factory():
            return await self._execute(context)



class WorkflowExecutionError(RuntimeError):
    def __init__(
        self,
        workflow_name: str,
        block_id: str,
        cause: BaseException,
        context: WorkflowContext[Any],
    ) -> None:
        super().__init__(
            f"workflow {workflow_name!r} failed at block {block_id!r}: "
            f"{type(cause).__name__}: {cause}"
        )
        self.workflow_name = workflow_name
        self.block_id = block_id
        self.cause = cause
        self.context = context
