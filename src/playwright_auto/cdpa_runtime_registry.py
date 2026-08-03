from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_independent import is_independent_task

TERMINAL = frozenset({"DONE", "STOPPED"})
ACTIVE_HOP_STATES = frozenset({"pre_send", "sending", "sent", "waiting", "responded", "routed"})
MINIMUM_DEADLINE_SECONDS = 0.5


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

    def _team_ready(self, task_id: str, state: Mapping[str, Any]) -> bool:
        team = str(state.get("team") or "")
        for sibling_id in self.team_members.get(team, ()):
            if sibling_id == task_id:
                continue
            sibling_status = str(
                self.tasks_by_id[sibling_id].get("status") or ""
            ).upper()
            if sibling_status not in TERMINAL and sibling_status != "WAITING":
                return False
        return True

    def _deadline(self, task_id: str, state: Mapping[str, Any], now: float) -> float | None:
        if task_id in self.immutable_task_ids:
            return None
        controls = state.get("controls") if isinstance(state.get("controls"), list) else []
        if any(
            isinstance(item, Mapping) and item.get("status") == "requested"
            for item in controls
        ):
            return now + MINIMUM_DEADLINE_SECONDS
        cleanup = state.get("cleanup") if isinstance(state.get("cleanup"), Mapping) else {}
        cleanup_state = str(cleanup.get("state") or "ACTIVE").upper()
        if cleanup_state == "CLEARING":
            return now + MINIMUM_DEADLINE_SECONDS
        status = str(state.get("status") or "INBOX").upper()
        if status in TERMINAL:
            if cleanup_state == "CLEARED":
                return None
            base = _timestamp(
                state.get("completed_at")
                or state.get("stopped_at")
                or state.get("updated_at")
            )
            deadline = (base if base is not None else now) + self.cleanup_idle_seconds
            return max(deadline, now + MINIMUM_DEADLINE_SECONDS)
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
        return tuple(sorted(task_id for task_id, due_at in self._due_at.items() if due_at <= now))

    def affected_by(self, task_id: str) -> set[str]:
        affected = {task_id}
        affected.update(self.dependency_children.get(task_id, ()))
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
            affected_state = self.tasks_by_id.get(affected_id)
            if affected_state is None:
                self._due_at.pop(affected_id, None)
                continue
            deadline = self._deadline(affected_id, affected_state, reschedule_at)
            if deadline is None:
                self._due_at.pop(affected_id, None)
            else:
                self._due_at[affected_id] = deadline
        return affected
