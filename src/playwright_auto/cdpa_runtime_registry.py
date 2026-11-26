from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_dependencies import terminal_event_from_state
from .cdpa_independent import is_independent_task
from .cdpa_team import dispatch_order_key, is_active_team_owner

TERMINAL = frozenset({"DONE", "STOPPED"})
ACTIVE_HOP_STATES = frozenset({"pre_send", "sending", "sent", "waiting", "responded", "routed"})
MINIMUM_DEADLINE_SECONDS = 0.5
ACTIVE_REPORT_CHECK_SECONDS = 15
_HELD_REPORT_CHECK_SECONDS = (180, 600, 1740)
REPORT_RECHECK_PENDING_MESSAGE = (
    "The accepted turn did not return a complete result. CDPA will recheck the exact report "
    "and continue if it verifies a route."
)
MISSING_TERMINAL_RESULT_BLOCK_CODES = frozenset({
    "empty_terminal_response",
    "response_terminal_missing",
    "response_without_terminal_result",
    "accepted_report_draft_present",
    "accepted_report_generation_active",
    "accepted_report_latest_turn_unverified",
    "accepted_report_page_not_clean",
    "accepted_report_retry_present",
    "accepted_report_repair_unavailable",
})
REPORT_RECOVERABLE_BLOCK_CODES = MISSING_TERMINAL_RESULT_BLOCK_CODES | {
    "accepted_conversation_identity_unresolved",
    "route_repair_invalid_response",
}


def _timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _active_hop_state(state: Mapping[str, Any]) -> str:
    active = state.get("active_hop_id")
    for hop in state.get("hops") or []:
        if isinstance(hop, Mapping) and hop.get("hop_id") == active:
            return str(hop.get("state") or "")
    return ""


def _latest_applied_pause_id(state: Mapping[str, Any]) -> int:
    if state.get("status") != "PAUSED":
        return 0
    return max((
        item["control_id"] for item in state.get("controls") or []
        if isinstance(item, Mapping) and isinstance(item.get("control_id"), int)
        and item.get("action") == "pause" and item.get("status") == "applied"
    ), default=0)


def pause_superseding_recovering_resume(state: Mapping[str, Any]) -> int | None:
    pause_id = _latest_applied_pause_id(state)
    if pause_id and any(
        isinstance(item, Mapping) and isinstance(item.get("control_id"), int)
        and item["control_id"] < pause_id
        and item.get("action") == "resume" and item.get("status") == "recovering"
        for item in state.get("controls") or []
    ):
        return pause_id
    return None


def next_recovering_resume_control(state: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(state.get("status") or "").upper() in TERMINAL:
        return None
    pause_id = _latest_applied_pause_id(state)
    return next(
        (
            item for item in reversed(state.get("controls") or [])
            if isinstance(item, dict)
            and item.get("action") == "resume"
            and item.get("status") == "recovering"
            and (not pause_id or (isinstance(item.get("control_id"), int) and item["control_id"] > pause_id))
        ),
        None,
    )


def held_report_check(state: Mapping[str, Any], now: float) -> tuple[int, float] | None:
    status = state.get("status")
    active_id = state.get("active_hop_id")
    if status == "STOPPED":
        active_id = (state.get("cleanup") or {}).get("active_hop_id")
        held_at = _timestamp(state.get("stopped_at"))
    elif status == "BLOCKED":
        held_at = _timestamp(state.get("blocked_at"))
    elif status == "PAUSED":
        control = next(
            (item for item in reversed(state.get("controls") or [])
             if item.get("action") == "pause" and item.get("status") == "applied"),
            {},
        )
        held_at = _timestamp(control.get("applied_at") or control.get("requested_at"))
    else:
        return None
    if held_at is None or now > held_at + 1800:
        return None
    for hop in state.get("hops") or []:
        if hop.get("hop_id") != active_id:
            continue
        observation = hop.get("report_result")
        if not isinstance(observation, Mapping) or not hop.get("receipt") or hop.get("report_path"):
            return None
        checked_at = _timestamp(observation.get("checked_at"))
        remaining = [
            held_at + seconds for seconds in _HELD_REPORT_CHECK_SECONDS
            if checked_at is None or checked_at < held_at + seconds
        ]
        if remaining:
            # If the worker was offline, coalesce overdue checks instead of bursting.
            return int(active_id), max((due for due in remaining if due <= now), default=remaining[0])
    return None


def active_report_check(state: Mapping[str, Any]) -> tuple[int, float] | None:
    if state.get("status") != "RUNNING" and (
        state.get("status") != "WAITING" or (state.get("waiting") or {}).get("reason") != "browser_pool"
    ):
        return None
    active_id = state.get("active_hop_id")
    for hop in state.get("hops") or []:
        if hop.get("hop_id") != active_id:
            continue
        observation = hop.get("report_result")
        if (
            hop.get("state") == "waiting" and hop.get("receipt") and not hop.get("response")
            and not hop.get("report_path") and isinstance(observation, Mapping)
        ):
            checked_at = _timestamp(observation.get("checked_at"))
            return int(active_id), 0.0 if checked_at is None else checked_at + ACTIVE_REPORT_CHECK_SECONDS
    return None


@dataclass
class CDPARuntimeRegistry:
    tasks_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    paths_by_id: dict[str, Path] = field(default_factory=dict)
    dependency_children: dict[str, set[str]] = field(default_factory=dict)
    team_members: dict[str, set[str]] = field(default_factory=dict)
    immutable_task_ids: set[str] = field(default_factory=set)
    cleanup_idle_seconds: float = 3600.0
    _due_at: dict[str, float] = field(default_factory=dict)

    @classmethod
    def hydrate(
        cls,
        tasks: Sequence[Mapping[str, Any]],
        *,
        now: float | None = None,
        cleanup_idle_seconds: float = 3600.0,
    ) -> "CDPARuntimeRegistry":
        registry = cls(cleanup_idle_seconds=cleanup_idle_seconds)
        for raw in tasks:
            task_id = str(raw.get("task_id") or "")
            if not task_id:
                raise ValueError("runtime registry task is missing task_id")
            if task_id in registry.tasks_by_id:
                raise ValueError(f"duplicate runtime registry task_id: {task_id}")
            state = dict(raw)
            registry.tasks_by_id[task_id] = state
            registry.paths_by_id[task_id] = Path(str(state["manifest_path"])).expanduser().resolve()
            team = str(state.get("team") or "")
            registry.team_members.setdefault(team, set()).add(task_id)
            for parent_id in state.get("depends_on_task_ids") or []:
                registry.dependency_children.setdefault(str(parent_id), set()).add(task_id)
        registry.immutable_task_ids = {
            str(state.get("replaces_task_id"))
            for state in registry.tasks_by_id.values()
            if state.get("replaces_task_id")
            and str(state.get("replaces_task_id")) in registry.tasks_by_id
        }
        registry._reschedule_all(0.0 if now is None else now)
        return registry

    def _dependencies_ready(self, state: Mapping[str, Any]) -> bool:
        parent_ids = [str(item) for item in state.get("depends_on_task_ids") or []]
        return all(
            parent_id in self.tasks_by_id
            and str(self.tasks_by_id[parent_id].get("status") or "").upper() == "DONE"
            for parent_id in parent_ids
        )

    def _teamtask_continuation_ready(self, state: Mapping[str, Any]) -> bool:
        if str(state.get("waiting_code") or "") != "async_callback":
            return False
        waiting = state.get("waiting") if isinstance(state.get("waiting"), Mapping) else {}
        continuation_id = str(waiting.get("continuation_id") or "")
        registry = state.get("async_continuations")
        record = registry.get(continuation_id) if isinstance(registry, Mapping) else None
        if not isinstance(record, Mapping) or record.get("source_kind") != "teamtask":
            return False
        source_ids = tuple(str(item) for item in record.get("source_refs") or ())
        if not source_ids:
            return False
        events = [
            terminal_event_from_state(self.tasks_by_id[source_id])
            if source_id in self.tasks_by_id
            else None
            for source_id in source_ids
        ]
        return all(event is not None for event in events)

    def _team_ready(self, task_id: str, state: Mapping[str, Any]) -> bool:
        team = str(state.get("team") or "")
        for sibling_id in self.team_members.get(team, ()):
            if sibling_id == task_id:
                continue
            sibling_status = str(
                self.tasks_by_id[sibling_id].get("status") or ""
            ).upper()
            if is_active_team_owner(self.tasks_by_id[sibling_id]) or (sibling_status not in TERMINAL and sibling_status != "WAITING"):
                return False
        return True

    def _deadline(self, task_id: str, state: Mapping[str, Any], now: float) -> float | None:
        if task_id in self.immutable_task_ids:
            return None
        controls = state.get("controls") if isinstance(state.get("controls"), list) else []
        if (
            next_recovering_resume_control(state) is not None
            or pause_superseding_recovering_resume(state) is not None
            or any(isinstance(item, Mapping) and item.get("status") == "requested" for item in controls)
        ):
            return now + MINIMUM_DEADLINE_SECONDS
        cleanup = state.get("cleanup") if isinstance(state.get("cleanup"), Mapping) else {}
        cleanup_state = str(cleanup.get("state") or "ACTIVE").upper()
        report_check = held_report_check(state, now)
        report_due = max(report_check[1], now + MINIMUM_DEADLINE_SECONDS) if report_check else None
        if bool(cleanup.get("retry_suppressed")):
            return report_due
        cleanup_retry_at = _timestamp(cleanup.get("next_retry_at"))
        if cleanup.get("next_retry_at") is not None and cleanup_retry_at is not None:
            cleanup_due = max(cleanup_retry_at, now + MINIMUM_DEADLINE_SECONDS)
            return min(cleanup_due, report_due) if report_due is not None else cleanup_due
        if cleanup_state == "CLEARING":
            return now + MINIMUM_DEADLINE_SECONDS
        status = str(state.get("status") or "INBOX").upper()
        if status in TERMINAL:
            if cleanup_state in {"CLEARED", "STOP_RELEASED"}:
                return report_due
            base = _timestamp(
                state.get("completed_at")
                or state.get("stopped_at")
                or state.get("updated_at")
            )
            deadline = (base if base is not None else now) + self.cleanup_idle_seconds
            cleanup_due = max(deadline, now + MINIMUM_DEADLINE_SECONDS)
            return min(cleanup_due, report_due) if report_due is not None else cleanup_due
        if report_due is not None:
            return report_due
        if status == "WAITING" and (state.get("waiting") or {}).get("reason") == "browser_pool":
            return now + MINIMUM_DEADLINE_SECONDS
        if is_independent_task(state):
            if status != "RUNNING":
                return None
            hop_state = _active_hop_state(state)
            if hop_state in {"pre_send", "sending", "sent", "waiting"}:
                return now + MINIMUM_DEADLINE_SECONDS
            if hop_state == "responded":
                independent = state.get("independent")
                if isinstance(independent, Mapping) and any(
                    independent.get(key) is not None
                    for key in (
                        "completion_request",
                        "continuation_request",
                        "settings_reset_request",
                    )
                ):
                    return now + MINIMUM_DEADLINE_SECONDS
            return None
        if (
            status not in TERMINAL | {"PAUSED", "BLOCKED", "WAITING"}
            and _active_hop_state(state) in ACTIVE_HOP_STATES
        ):
            return now + MINIMUM_DEADLINE_SECONDS
        if status == "WAITING":
            if str(state.get("waiting_code") or "") == "async_callback":
                return (
                    now + MINIMUM_DEADLINE_SECONDS
                    if self._teamtask_continuation_ready(state)
                    else None
                )
            if self._dependencies_ready(state) and self._team_ready(task_id, state):
                return now + MINIMUM_DEADLINE_SECONDS
            return None
        return None

    def _reschedule_all(self, now: float) -> None:
        self._due_at = {
            task_id: deadline
            for task_id, state in self.tasks_by_id.items()
            if (deadline := self._deadline(task_id, state, now)) is not None
        }

    def next_due_at(self) -> float | None:
        return min(self._due_at.values(), default=None)

    def due_task_ids(self, now: float) -> tuple[str, ...]:
        due = [task_id for task_id, due_at in self._due_at.items() if due_at <= now]
        due.sort(key=lambda task_id: dispatch_order_key(self.tasks_by_id[task_id]))
        return tuple(due)

    def affected_by(self, task_id: str) -> set[str]:
        affected = {task_id}
        affected.update(self.dependency_children.get(task_id, ()))
        # TeamTask-source continuation links are derived from canonical task state;
        # do not persist a parallel dependency graph.
        for target_id, target in self.tasks_by_id.items():
            registry = target.get("async_continuations")
            if not isinstance(registry, Mapping):
                continue
            if any(
                isinstance(record, Mapping)
                and record.get("source_kind") == "teamtask"
                and task_id in tuple(str(item) for item in record.get("source_refs") or ())
                and str(record.get("state") or "") not in {"delivered", "cancelled", "superseded"}
                for record in registry.values()
            ):
                affected.add(target_id)
        state = self.tasks_by_id.get(task_id)
        if state is not None:
            affected.update(self.team_members.get(str(state.get("team") or ""), ()))
        return affected

    def update_task(
        self,
        state: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> set[str]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("runtime registry update is missing task_id")
        previous = self.tasks_by_id.get(task_id)
        previous_immutable = set(self.immutable_task_ids)
        if previous is not None:
            previous_team = str(previous.get("team") or "")
            self.team_members.get(previous_team, set()).discard(task_id)
            for children in self.dependency_children.values():
                children.discard(task_id)
        current = dict(state)
        self.tasks_by_id[task_id] = current
        self.paths_by_id[task_id] = Path(str(current["manifest_path"])).expanduser().resolve()
        self.team_members.setdefault(str(current.get("team") or ""), set()).add(task_id)
        for parent_id in current.get("depends_on_task_ids") or []:
            self.dependency_children.setdefault(str(parent_id), set()).add(task_id)
        self.immutable_task_ids = {
            str(item.get("replaces_task_id"))
            for item in self.tasks_by_id.values()
            if item.get("replaces_task_id")
            and str(item.get("replaces_task_id")) in self.tasks_by_id
        }
        affected = self.affected_by(task_id)
        affected.update(previous_immutable ^ self.immutable_task_ids)
        reschedule_at = 0.0 if now is None else now
        for affected_id in affected:
            self.reschedule_task(affected_id, now=reschedule_at)
        return affected

    def reschedule_task(self, task_id: str, *, now: float) -> None:
        state = self.tasks_by_id.get(task_id)
        deadline = self._deadline(task_id, state, now) if state is not None else None
        if deadline is None:
            self._due_at.pop(task_id, None)
        else:
            self._due_at[task_id] = deadline
