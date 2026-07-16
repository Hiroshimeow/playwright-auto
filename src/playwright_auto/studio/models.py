from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Sequence


class WorkerStatus(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"
    DISCONNECTED = "disconnected"
    AUTH_REQUIRED = "auth_required"


class RunStatus(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ERROR = "error"


_PENDING_STATUSES = {
    WorkerStatus.IDLE,
    WorkerStatus.QUEUED,
    WorkerStatus.PAUSED,
    WorkerStatus.STOPPED,
    WorkerStatus.ERROR,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerModel:
    page_id: str
    title: str
    url: str
    role: str | None = None
    order: int = 0
    status: WorkerStatus = WorkerStatus.IDLE
    prompt_template: str = "Work on the global goal from the perspective of this role. Return concrete results for the next worker."
    last_prompt: str = ""
    responses: list[str] = field(default_factory=list)
    error: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    connected: bool = True
    pending_retry: bool = False
    enabled: bool = True
    browser_state: str = "unknown"
    requires_login: bool = False

    @property
    def latest_response(self) -> str:
        return self.responses[-1] if self.responses else ""

    @property
    def display_role(self) -> str:
        return self.role or "UNASSIGNED"

    @property
    def is_pending(self) -> bool:
        return self.status in _PENDING_STATUSES

    def reset_for_run(self) -> None:
        self.status = WorkerStatus.QUEUED
        self.error = ""
        self.started_at = None
        self.finished_at = None
        self.pending_retry = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "title": self.title,
            "url": self.url,
            "role": self.role,
            "order": self.order,
            "status": self.status.value,
            "prompt_template": self.prompt_template,
            "last_prompt": self.last_prompt,
            "responses": list(self.responses),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "connected": self.connected,
            "pending_retry": self.pending_retry,
            "enabled": self.enabled,
            "browser_state": self.browser_state,
            "requires_login": self.requires_login,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkerModel":
        return cls(
            page_id=str(value["page_id"]),
            title=str(value.get("title") or "ChatGPT"),
            url=str(value.get("url") or ""),
            role=(str(value["role"]) if value.get("role") else None),
            order=int(value.get("order") or 0),
            status=WorkerStatus(str(value.get("status") or WorkerStatus.IDLE.value)),
            prompt_template=str(value.get("prompt_template") or ""),
            last_prompt=str(value.get("last_prompt") or ""),
            responses=[str(item) for item in value.get("responses") or []],
            error=str(value.get("error") or ""),
            started_at=(str(value["started_at"]) if value.get("started_at") else None),
            finished_at=(str(value["finished_at"]) if value.get("finished_at") else None),
            connected=bool(value.get("connected", True)),
            pending_retry=bool(value.get("pending_retry", False)),
            enabled=bool(value.get("enabled", True)),
            browser_state=str(value.get("browser_state") or "unknown"),
            requires_login=bool(value.get("requires_login", False)),
        )


@dataclass
class StudioSettings:
    global_goal: str = ""
    task_id: str = ""
    rounds: int = 1
    response_timeout_ms: int = 600_000
    context_char_limit: int = 12_000
    cdp_url: str = "http://127.0.0.1:9222"

    def validate(self) -> None:
        if self.rounds < 1 or self.rounds > 20:
            raise ValueError("rounds must be between 1 and 20")
        if self.response_timeout_ms < 10_000 or self.response_timeout_ms > 3_600_000:
            raise ValueError("response_timeout_ms must be between 10000 and 3600000")
        if self.context_char_limit < 500 or self.context_char_limit > 100_000:
            raise ValueError("context_char_limit must be between 500 and 100000")

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_goal": self.global_goal,
            "task_id": self.task_id,
            "rounds": self.rounds,
            "response_timeout_ms": self.response_timeout_ms,
            "context_char_limit": self.context_char_limit,
            "cdp_url": self.cdp_url,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StudioSettings":
        result = cls(
            global_goal=str(value.get("global_goal") or ""),
            task_id=str(value.get("task_id") or ""),
            rounds=int(value.get("rounds") or 1),
            response_timeout_ms=int(value.get("response_timeout_ms") or 600_000),
            context_char_limit=int(value.get("context_char_limit") or 12_000),
            cdp_url=str(value.get("cdp_url") or "http://127.0.0.1:9222"),
        )
        result.validate()
        return result


@dataclass
class StudioState:
    settings: StudioSettings = field(default_factory=StudioSettings)
    workers: list[WorkerModel] = field(default_factory=list)
    run_status: RunStatus = RunStatus.IDLE
    active_page_id: str | None = None
    current_round: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.normalize_order()

    def normalize_order(self) -> None:
        self.workers.sort(key=lambda item: item.order)
        for index, item in enumerate(self.workers):
            item.order = index

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "settings": self.settings.to_dict(),
            "workers": [item.to_dict() for item in self.workers],
            "run_status": self.run_status.value,
            "active_page_id": self.active_page_id,
            "current_round": self.current_round,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StudioState":
        if int(value.get("version") or 1) != 1:
            raise ValueError("unsupported studio state version")
        state = cls(
            settings=StudioSettings.from_dict(dict(value.get("settings") or {})),
            workers=[WorkerModel.from_dict(dict(item)) for item in value.get("workers") or []],
            run_status=RunStatus(str(value.get("run_status") or RunStatus.IDLE.value)),
            active_page_id=(
                str(value["active_page_id"]) if value.get("active_page_id") else None
            ),
            current_round=int(value.get("current_round") or 0),
            created_at=str(value.get("created_at") or utc_now_iso()),
            updated_at=str(value.get("updated_at") or utc_now_iso()),
        )
        state.normalize_order()
        return state


@dataclass(frozen=True)
class StudioEvent:
    kind: str
    message: str
    page_id: str | None = None
    role: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "page_id": self.page_id,
            "role": self.role,
            "payload": dict(self.payload),
            "at": self.at,
        }


def duplicate_roles(workers: Iterable[WorkerModel]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for item in workers:
        if item.role:
            counts[item.role] = counts.get(item.role, 0) + 1
    return tuple(sorted(role for role, count in counts.items() if count > 1))


def reorder_pending_workers(
    workers: Sequence[WorkerModel], source_page_id: str, target_index: int
) -> list[WorkerModel]:
    result = list(workers)
    source_index = next(
        (index for index, item in enumerate(result) if item.page_id == source_page_id),
        None,
    )
    if source_index is None:
        raise KeyError(source_page_id)
    source = result[source_index]
    if not source.is_pending:
        raise ValueError(f"worker {source_page_id!r} is not pending and cannot be reordered")

    pending_indices = [
        index for index, item in enumerate(result) if item.status in _PENDING_STATUSES
    ]
    if not pending_indices:
        return result
    target_index = max(min(int(target_index), len(result) - 1), 0)
    candidate_indices = [index for index in pending_indices if index != source_index]
    insertion_slot = sum(1 for index in candidate_indices if index < target_index)
    pending_items = [result[index] for index in pending_indices if index != source_index]
    pending_items.insert(insertion_slot, source)

    for index, item in zip(pending_indices, pending_items, strict=True):
        result[index] = item
    for index, item in enumerate(result):
        item.order = index
    return result


def _render_prior_context(
    prior_outputs: Sequence[tuple[str, str]], context_char_limit: int
) -> str:
    if not prior_outputs:
        return "No prior worker output is available."
    chunks = [f"[{role}]\n{text.strip()}" for role, text in prior_outputs if text.strip()]
    joined = "\n\n".join(chunks)
    if len(joined) <= context_char_limit:
        return joined
    marker = "[earlier context truncated]\n\n"
    keep = max(context_char_limit - len(marker), 0)
    return marker + joined[-keep:]


def render_worker_prompt(
    *,
    global_goal: str,
    worker: WorkerModel,
    ordinal: int,
    total_workers: int,
    round_index: int,
    total_rounds: int,
    prior_outputs: Sequence[tuple[str, str]],
    context_char_limit: int,
) -> str:
    role = worker.role or "UNASSIGNED"
    goal = global_goal.strip()
    if not goal:
        raise ValueError("global goal must not be empty")
    if not worker.role:
        raise ValueError(f"worker {worker.page_id!r} is unassigned")
    context = _render_prior_context(prior_outputs, context_char_limit)
    template = worker.prompt_template.strip() or (
        "Work on the global goal from this role and return concrete results for the next worker."
    )
    return (
        f"GLOBAL GOAL:\n{goal}\n\n"
        f"Worker: {role} ({ordinal}/{total_workers})\n"
        f"Round: {round_index + 1}/{total_rounds}\n\n"
        f"ROLE-SPECIFIC INSTRUCTION:\n{template}\n\n"
        f"PRIOR WORKER OUTPUTS:\n{context}\n\n"
        "Produce a concrete, evidence-based result for the next worker. "
        "Do not merely summarize the prior output; extend, verify, or correct it according to your role."
    )


def slug_task_id(goal: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", goal.strip().lower()).strip("-")
    normalized = normalized[:64] or "studio-task"
    digest = hashlib.sha256(goal.strip().encode("utf-8")).hexdigest()[:10]
    return f"{normalized}-{digest}"
