from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_actions import CDPATabActions
from .cdpa_config import CDPAConfig
from .cdpa_routes import ReportEvidence
from .cdpa_store import TaskStore, retained_report_references, utc_now
from .cdpa_team import validate_exact_team
from .connection import is_cdp_disconnect
from .durable import RequestLedger, RequestStatus
from .durable_blocks import DurableSendBlock
from .file_lock import exclusive_file_lock, fsync_parent_directory
from .workflow import WorkflowContext

MAINTAINER_ROLE = "MAINTAINERS"
_INCIDENT_STATES = frozenset({"OPEN", "RUNNING", "RESOLVED", "ESCALATED"})
_ACTIONS = frozenset(
    {
        "WAIT",
        "RESUME_TASK",
        "RETRY_HOP",
        "RESTART_ROLE",
        "NEW_CHAT_ROLE",
        "OPEN_ROLE_TAB",
        "ROUTE_PLAN",
        "REPLACE_TASK",
    }
)
_ROLE_ACTIONS = frozenset({"RESTART_ROLE", "NEW_CHAT_ROLE", "OPEN_ROLE_TAB"})
_CONTROL_ACTIONS = {
    "RESUME_TASK": "resume",
    "RETRY_HOP": "retry",
    "RESTART_ROLE": "restart_role",
    "NEW_CHAT_ROLE": "new_chat",
    "OPEN_ROLE_TAB": "open_tab",
    "ROUTE_PLAN": "route_plan",
}
_DECISION_KEYS = frozenset({"action", "reason", "role", "lesson", "replacement"})
_REPLACEMENT_KEYS = frozenset({"target_task_id", "task", "reuse_team", "rewire_children"})
_JSON_FENCE = re.compile(r"```json\s*(\{.*\})\s*```\s*$", re.DOTALL | re.IGNORECASE)
_JSON_LABEL = re.compile(r"(?:^|\s)json\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class MaintenanceDecision:
    action: str
    reason: str
    role: str | None = None
    lesson: str | None = None
    replacement: dict[str, object] | None = None


def _maintenance_operational_key(state: Mapping[str, Any]) -> str | None:
    cleanup = state.get("cleanup")
    if isinstance(cleanup, Mapping) and str(cleanup.get("state") or "").upper() in {
        "CLEARING",
        "CLEARED",
    }:
        return None
    status = str(state.get("status") or "").upper()
    terminal = str(state.get("terminal_state") or "").upper()
    code = str(state.get("block_code") or "")
    if status == "BLOCKED":
        if code.lower().startswith("maintainer_"):
            return None
        reason = str(state.get("block_reason") or "")
    elif status == "WAITING" and state.get("waiting_code") in {
        "dependency_missing",
        "team_owner_conflict",
        "queue_release_failed",
    }:
        code = str(state.get("waiting_code") or "waiting")
        reason = str(state.get("waiting_reason") or "Operational wait")
    elif status == "STOPPED" and terminal != "DONE":
        code = code or "task_stopped"
        reason = str(state.get("stop_reason") or "")
    else:
        return None
    return "|".join(
        (
            str(state.get("task_id") or ""),
            status,
            code,
            str(state.get("active_hop_id") or ""),
            str(state.get("active_role") or ""),
            reason,
        )
    )


def _incident_operational_key(incident: Mapping[str, Any]) -> str:
    value = incident.get("operational_key")
    if isinstance(value, str) and value:
        return value
    return "|".join(
        (
            str(incident.get("task_id") or ""),
            str(incident.get("trigger_status") or "").upper(),
            str(incident.get("trigger_code") or ""),
            str(incident.get("source_hop_id") or ""),
            str(incident.get("source_role") or ""),
            str(incident.get("trigger_reason") or ""),
        )
    )


def _conversation_identity(url: Any) -> str | None:
    try:
        path = urlparse(str(url or "")).path.rstrip("/")
    except ValueError:
        return None
    return path if path.startswith("/c/") and len(path) > 3 else None


def maintenance_incident_key(state: Mapping[str, Any]) -> str | None:
    operational_key = _maintenance_operational_key(state)
    if operational_key is None:
        return None
    updated_at = str(state.get("updated_at") or "")
    maintenance = state.get("maintenance")
    if isinstance(maintenance, Mapping):
        worker_updated_at = str(maintenance.get("worker_updated_at") or "")
        if worker_updated_at and worker_updated_at == updated_at:
            updated_at = str(
                maintenance.get("observed_task_updated_at") or updated_at
            )
    return f"{operational_key}|{updated_at}"


def ensure_maintenance_incident(state: dict[str, Any]) -> dict[str, Any] | None:
    key = maintenance_incident_key(state)
    existing_maintenance = state.get("maintenance")
    if key is None:
        if isinstance(existing_maintenance, dict):
            existing_maintenance.pop("suppressed_operational_key", None)
        return None
    maintenance = state.setdefault(
        "maintenance",
        {"active_incident_id": None, "incidents": [], "last_resolved_at": None},
    )
    incidents = maintenance.setdefault("incidents", [])
    operational_key = _maintenance_operational_key(state)
    assert operational_key is not None
    suppressed_key = maintenance.get("suppressed_operational_key")
    if suppressed_key and suppressed_key != operational_key:
        maintenance.pop("suppressed_operational_key", None)
    if maintenance.get("suppressed_operational_key") == operational_key:
        return next(
            (
                incident
                for incident in reversed(incidents)
                if isinstance(incident, dict)
                and incident.get("state") == "ESCALATED"
                and _incident_operational_key(incident) == operational_key
            ),
            None,
        )
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        if incident.get("key") == key:
            decision = incident.get("decision")
            waiting = (
                isinstance(decision, Mapping)
                and str(decision.get("action") or "").upper() == "WAIT"
            )
            if incident.get("state") in {"OPEN", "RUNNING"} and not waiting:
                maintenance["active_incident_id"] = incident["incident_id"]
            return incident
    now = utc_now()
    incident = {
        "incident_id": f"maint-{hashlib.sha256(key.encode()).hexdigest()[:16]}",
        "key": key,
        "operational_key": operational_key,
        "task_id": str(state.get("task_id") or ""),
        "team": str(state.get("team") or ""),
        "trigger_status": str(state.get("status") or "").upper(),
        "trigger_code": str(
            state.get("block_code")
            or state.get("waiting_code")
            or "task_stopped"
        ),
        "trigger_reason": str(
            state.get("block_reason")
            or state.get("waiting_reason")
            or state.get("stop_reason")
            or ""
        ),
        "source_hop_id": state.get("active_hop_id"),
        "source_role": state.get("active_role"),
        "state": "OPEN",
        "turn": 0,
        "decision": None,
        "report_path": None,
        "report_sha256": None,
        "report_size": None,
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
        "last_error": None,
        "applied_snapshot_key": None,
    }
    incidents.append(incident)
    maintenance["active_incident_id"] = incident["incident_id"]
    return incident


def _decode_decision(source: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate maintenance field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(source, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid maintenance JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("maintenance decision must be a JSON object")
    return value


def _contains_json_object(source: str) -> bool:
    decoder = json.JSONDecoder()
    for index, character in enumerate(source):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return True
    return False


def _split_terminal_decision(source: str) -> tuple[str, str]:
    match = _JSON_FENCE.search(source)
    if match is not None:
        report = source[: match.start()].strip()
        decision_source = match.group(1)
    else:
        decoder = json.JSONDecoder()
        terminal: list[tuple[int, int]] = []
        for index, character in enumerate(source):
            if character != "{":
                continue
            try:
                _value, end = decoder.raw_decode(source[index:])
            except json.JSONDecodeError:
                continue
            if not source[index + end :].strip():
                terminal.append((index, index + end))
        if len(terminal) != 1:
            if source.startswith("{") and source.endswith("}"):
                raise ValueError(
                    "maintenance response requires a non-empty Markdown report"
                )
            raise ValueError("maintenance response must end with one JSON decision")
        start, end = terminal[0]
        report = _JSON_LABEL.sub("", source[:start].rstrip()).strip()
        decision_source = source[start:end]
    if not report:
        raise ValueError("maintenance response requires a non-empty Markdown report")
    if _contains_json_object(report):
        raise ValueError(
            "maintenance response must contain exactly one terminal JSON decision"
        )
    return report, decision_source


def parse_maintenance_response(
    text: str,
    *,
    configured_roles: Sequence[str] = ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"),
) -> tuple[str, MaintenanceDecision]:
    source = str(text).strip()
    report, decision_source = _split_terminal_decision(source)
    value = _decode_decision(decision_source)
    if set(value) != _DECISION_KEYS:
        raise ValueError(
            f"maintenance decision must contain exactly {sorted(_DECISION_KEYS)!r}"
        )
    if not isinstance(value.get("action"), str):
        raise ValueError("maintenance action must be a string")
    if not isinstance(value.get("reason"), str):
        raise ValueError("maintenance reason must be a string")
    if value.get("role") is not None and not isinstance(value.get("role"), str):
        raise ValueError("maintenance role must be a string or null")
    if value.get("lesson") is not None and not isinstance(value.get("lesson"), str):
        raise ValueError("maintenance lesson must be a string or null")
    action = value["action"].strip().upper()
    reason = value["reason"].strip()
    if action not in _ACTIONS:
        raise ValueError(f"unsupported maintenance action {action!r}")
    if not reason:
        raise ValueError("maintenance reason must not be empty")
    raw_role = value.get("role")
    role = str(raw_role).strip().upper() if raw_role is not None else None
    roles = {str(item).strip().upper() for item in configured_roles}
    if action in _ROLE_ACTIONS:
        if role not in roles or role == MAINTAINER_ROLE:
            raise ValueError("role action requires a normal configured role")
    elif role is not None:
        raise ValueError("role is allowed only for role actions")
    raw_replacement = value.get("replacement")
    replacement: dict[str, object] | None = None
    if action == "REPLACE_TASK":
        if not isinstance(raw_replacement, Mapping):
            raise ValueError("REPLACE_TASK requires a replacement object")
        if set(raw_replacement) != _REPLACEMENT_KEYS:
            raise ValueError(
                f"replacement must contain exactly {sorted(_REPLACEMENT_KEYS)!r}"
            )
        target_task_id = raw_replacement.get("target_task_id")
        task = raw_replacement.get("task")
        reuse_team = raw_replacement.get("reuse_team")
        rewire_children = raw_replacement.get("rewire_children")
        if not isinstance(target_task_id, str) or not target_task_id.strip():
            raise ValueError("replacement target_task_id must be a non-empty string")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("replacement task must be a non-empty string")
        if not isinstance(reuse_team, bool):
            raise ValueError("replacement reuse_team must be a boolean")
        if not isinstance(rewire_children, bool):
            raise ValueError("replacement rewire_children must be a boolean")
        replacement = {
            "target_task_id": target_task_id.strip(),
            "task": task.strip(),
            "reuse_team": reuse_team,
            "rewire_children": rewire_children,
        }
    elif raw_replacement is not None:
        raise ValueError("replacement must be null unless action is REPLACE_TASK")
    raw_lesson = value.get("lesson")
    lesson = str(raw_lesson).strip() if raw_lesson is not None else None
    if lesson:
        if "\n" in lesson or "\r" in lesson:
            raise ValueError("lesson must be one paragraph")
        if len(lesson) > 600:
            raise ValueError("lesson must be at most 600 characters")
    return report + "\n", MaintenanceDecision(
        action=action,
        reason=reason,
        role=role,
        lesson=lesson or None,
        replacement=replacement,
    )


def maintenance_report_relative(team: str, turn: int, at: datetime) -> str:
    team = validate_exact_team(team)
    if int(turn) < 1:
        raise ValueError("maintenance report turn must be positive")
    moment = at.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f".plan/maintainers/{team}_turn{int(turn)}_{stamp}.md"


def write_maintenance_report(
    repository_root: str | Path,
    *,
    team: str,
    turn: int,
    report: str,
    at: datetime | None = None,
) -> ReportEvidence:
    body = str(report)
    if not body.strip():
        raise ValueError("maintenance report must not be empty")
    root = Path(repository_root).expanduser().resolve()
    relative = maintenance_report_relative(team, turn, at or datetime.now(timezone.utc))
    target = (root / relative).resolve()
    maintainers_root = (root / ".plan" / "maintainers").resolve()
    target.relative_to(maintainers_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    data = body.encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    fsync_parent_directory(target)
    return ReportEvidence(
        path=str(target),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


class MaintenanceStateStore:
    def __init__(self, plans_root: str | Path) -> None:
        self.root = Path(plans_root).expanduser().resolve() / "maintainers"
        self.path = self.root / "state.json"
        self.lock_path = self.root / "state.json.lock"
        self.run_lock_path = self.root / "run.lock"

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": 1,
            "physical_role": MAINTAINER_ROLE,
            "page_id": None,
            "page_url": None,
            "conversation_generation": 0,
            "constructor_sent_generation": None,
            "turn": 0,
            "active_incident": None,
            "history": [],
            "last_error": None,
            "updated_at": utc_now(),
        }

    @staticmethod
    def _validate(value: Mapping[str, Any]) -> None:
        if value.get("version") != 1:
            raise ValueError("unsupported Maintainers state version")
        if value.get("physical_role") != MAINTAINER_ROLE:
            raise ValueError("Maintainers state has invalid physical role")
        for key in ("conversation_generation", "turn"):
            item = value.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"Maintainers state {key} must be a non-negative integer")
        if not isinstance(value.get("history"), list):
            raise ValueError("Maintainers state history must be a list")

    def load(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            if not self.path.exists():
                return self._default()
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("Maintainers state root must be an object")
            self._validate(value)
            return dict(value)

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        value["version"] = 1
        value["physical_role"] = MAINTAINER_ROLE
        value["updated_at"] = utc_now()
        self._validate(value)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            temporary = self.path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            fsync_parent_directory(self.path)
        return value


class MaintainerCoordinator:
    def __init__(self, config: CDPAConfig, *, store: TaskStore | None = None) -> None:
        self.config = config
        self.store = store or TaskStore(config)
        self.state_store = MaintenanceStateStore(config.plans_root)

    @staticmethod
    def _incident(state: Mapping[str, Any], incident_id: str) -> dict[str, Any] | None:
        maintenance = state.get("maintenance")
        if not isinstance(maintenance, Mapping):
            return None
        return next(
            (
                item
                for item in maintenance.get("incidents") or []
                if isinstance(item, dict) and item.get("incident_id") == incident_id
            ),
            None,
        )

    @staticmethod
    def _decision_dict(decision: MaintenanceDecision) -> dict[str, Any]:
        return {
            "action": decision.action,
            "reason": decision.reason,
            "role": decision.role,
            "lesson": decision.lesson,
            "replacement": decision.replacement,
        }

    @staticmethod
    def _history_entry(
        task: Mapping[str, Any],
        incident: Mapping[str, Any],
        *,
        recorded_at: str | None = None,
    ) -> dict[str, Any] | None:
        decision = incident.get("decision")
        request_id = str(incident.get("request_id") or "").strip()
        report_path = str(incident.get("report_path") or "").strip()
        report_sha256 = str(incident.get("report_sha256") or "").strip()
        turn = incident.get("turn")
        if (
            not isinstance(decision, Mapping)
            or not str(decision.get("action") or "").strip()
            or not request_id
            or not report_path
            or not report_sha256
            or isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 1
        ):
            return None
        return {
            "task_id": task["task_id"],
            "team": task["team"],
            "incident_id": incident["incident_id"],
            "turn": turn,
            "request_id": request_id,
            "action": str(decision["action"]),
            "report_path": report_path,
            "report_sha256": report_sha256,
            "recorded_at": recorded_at
            or str(incident.get("report_at") or incident.get("updated_at") or utc_now()),
        }

    @classmethod
    def _active_projection(
        cls,
        task: Mapping[str, Any],
        incident: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        maintenance = task.get("maintenance")
        decision = incident.get("decision")
        history_entry = cls._history_entry(task, incident)
        if (
            not isinstance(maintenance, Mapping)
            or maintenance.get("active_incident_id") != incident.get("incident_id")
            or incident.get("state") not in {"OPEN", "RUNNING"}
            or not isinstance(decision, Mapping)
            or str(decision.get("action") or "").upper() == "WAIT"
            or history_entry is None
        ):
            return None
        return {
            "task_id": task["task_id"],
            "team": task["team"],
            "incident_id": incident["incident_id"],
            "turn": incident["turn"],
            "request_id": incident["request_id"],
            "action": decision["action"],
            "report_path": incident["report_path"],
            "report_sha256": incident["report_sha256"],
        }

    def _reconcile_global_state(
        self,
        loaded: Sequence[tuple[Path, dict[str, Any]]],
    ) -> bool:
        global_state = self.state_store.load()
        changed = False
        history = list(global_state.get("history") or [])
        history_by_request = {
            str(item.get("request_id")): index
            for index, item in enumerate(history)
            if isinstance(item, Mapping) and item.get("request_id")
        }
        tasks_by_id = {str(state["task_id"]): state for _path, state in loaded}
        active_projections: list[dict[str, Any]] = []
        max_turn = int(global_state.get("turn") or 0)
        for item in history:
            if not isinstance(item, Mapping):
                continue
            item_turn = item.get("turn")
            if isinstance(item_turn, int) and not isinstance(item_turn, bool):
                max_turn = max(max_turn, item_turn)
        generation = int(global_state.get("conversation_generation") or 0)
        generation_proven = bool(str(global_state.get("page_id") or "").strip()) and (
            _conversation_identity(global_state.get("page_url")) is not None
        )
        recovered_constructor_generation: int | None = None

        for _path, task in loaded:
            maintenance = task.get("maintenance")
            if not isinstance(maintenance, Mapping):
                continue
            for incident in maintenance.get("incidents") or []:
                if not isinstance(incident, Mapping):
                    continue
                incident_turn = incident.get("turn")
                if isinstance(incident_turn, int) and not isinstance(incident_turn, bool):
                    max_turn = max(max_turn, incident_turn)
                request_id = str(incident.get("request_id") or "")
                existing_index = history_by_request.get(request_id)
                existing = (
                    history[existing_index]
                    if existing_index is not None
                    and isinstance(history[existing_index], Mapping)
                    else None
                )
                entry = self._history_entry(
                    task,
                    incident,
                    recorded_at=(
                        str(existing.get("recorded_at") or "") or None
                        if existing is not None
                        else None
                    ),
                )
                if entry is not None and existing is not None:
                    for field in (
                        "application_state",
                        "replacement_task_id",
                        "resolved_at",
                        "application_error",
                    ):
                        if field in existing:
                            entry[field] = existing[field]
                if entry is not None:
                    if existing_index is None:
                        history_by_request[request_id] = len(history)
                        history.append(entry)
                        changed = True
                    elif dict(existing) != entry:
                        history[existing_index] = entry
                        changed = True
                    if (
                        generation_proven
                        and incident.get("constructor_included") is True
                        and incident.get("prompt_generation") == generation
                    ):
                        recovered_constructor_generation = generation
                replacement_applied = bool(
                    existing is not None
                    and existing.get("application_state") == "RESOLVED"
                    and existing.get("replacement_task_id")
                )
                projection = (
                    None
                    if replacement_applied
                    else self._active_projection(task, incident)
                )
                if projection is not None:
                    active_projections.append(projection)

        if len(history) > 100:
            history = history[-100:]
            changed = True
        if global_state.get("history") != history:
            global_state["history"] = history
            changed = True
        if int(global_state.get("turn") or 0) < max_turn:
            global_state["turn"] = max_turn
            changed = True
        constructor_watermark = global_state.get("constructor_sent_generation")
        if (
            recovered_constructor_generation is not None
            and (
                constructor_watermark is None
                or (
                    isinstance(constructor_watermark, int)
                    and not isinstance(constructor_watermark, bool)
                    and constructor_watermark < recovered_constructor_generation
                )
            )
        ):
            global_state["constructor_sent_generation"] = recovered_constructor_generation
            changed = True

        active = global_state.get("active_incident")
        desired_active: Mapping[str, Any] | None = None
        if isinstance(active, Mapping):
            task = tasks_by_id.get(str(active.get("task_id") or ""))
            incident = (
                self._incident(task, str(active.get("incident_id") or ""))
                if task is not None
                else None
            )
            applied = next(
                (
                    item
                    for item in history
                    if isinstance(item, Mapping)
                    and item.get("request_id") == active.get("request_id")
                    and item.get("application_state") == "RESOLVED"
                    and item.get("replacement_task_id")
                ),
                None,
            )
            projection = (
                self._active_projection(task, incident)
                if task is not None and incident is not None and applied is None
                else None
            )
            if projection is not None:
                desired_active = projection
            elif (
                task is not None
                and incident is not None
                and incident.get("state") in {"OPEN", "RUNNING"}
                and not isinstance(incident.get("decision"), Mapping)
                and isinstance(task.get("maintenance"), Mapping)
                and task["maintenance"].get("active_incident_id")
                == incident.get("incident_id")
                and active.get("request_id") == incident.get("request_id")
            ):
                desired_active = active
        elif len(active_projections) == 1:
            desired_active = active_projections[0]

        if global_state.get("active_incident") != desired_active:
            global_state["active_incident"] = desired_active
            changed = True
        if changed:
            self.state_store.save(global_state)
        return changed

    def _resolve_incident(
        self,
        state: dict[str, Any],
        incident: dict[str, Any],
    ) -> None:
        now = utc_now()
        incident["state"] = "RESOLVED"
        incident["resolved_at"] = now
        incident["updated_at"] = now
        maintenance = state["maintenance"]
        maintenance["active_incident_id"] = None
        maintenance["last_resolved_at"] = now
        decision_value = incident.get("decision")
        if isinstance(decision_value, Mapping):
            decision = MaintenanceDecision(
                action=str(decision_value.get("action") or ""),
                reason=str(decision_value.get("reason") or ""),
                role=decision_value.get("role"),
                lesson=decision_value.get("lesson"),
                replacement=(
                    dict(decision_value["replacement"])
                    if isinstance(decision_value.get("replacement"), Mapping)
                    else None
                ),
            )
            learning_path = self.config.repository_root / "LEARNING.md"
            if learning_path.is_file():
                append_resolved_lesson(
                    learning_path,
                    incident,
                    decision,
                )

    @staticmethod
    def _control_matches_incident(
        control: Mapping[str, Any],
        incident: Mapping[str, Any],
    ) -> bool:
        decision = incident.get("decision")
        if not isinstance(decision, Mapping):
            return False
        expected = _CONTROL_ACTIONS.get(str(decision.get("action") or "").upper())
        if (
            expected is None
            or control.get("action") != expected
            or control.get("maintenance_incident_id") != incident.get("incident_id")
            or control.get("maintenance_request_id") != incident.get("request_id")
            or str(control.get("reason") or "").strip()
            != str(decision.get("reason") or "").strip()
        ):
            return False
        return expected in {"resume", "retry", "route_plan"} or control.get(
            "role"
        ) == decision.get("role")

    @classmethod
    def _matching_control(
        cls,
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        return next(
            (
                item
                for item in reversed(state.get("controls") or [])
                if isinstance(item, Mapping)
                and cls._control_matches_incident(item, incident)
            ),
            None,
        )

    def _reconcile_active(
        self,
        path: Path,
        state: dict[str, Any],
        *,
        tasks: Sequence[Mapping[str, Any]] | None = None,
    ) -> bool:
        maintenance = state.get("maintenance")
        if not isinstance(maintenance, Mapping):
            return False
        active_id = maintenance.get("active_incident_id")
        if not isinstance(active_id, str) or not active_id:
            return False
        incident = self._incident(state, active_id)
        if incident is None or incident.get("state") not in {"OPEN", "RUNNING"}:
            return False
        decision = incident.get("decision")
        if not isinstance(decision, Mapping):
            return False
        current_key = _maintenance_operational_key(state)
        applied_key = _incident_operational_key(incident)
        action = str(decision.get("action") or "").upper()
        if current_key is None:
            self._resolve_incident(state, incident)
            self.store.save_maintenance(path, state)
            self._clear_global_active(str(incident["incident_id"]))
            return True
        if action == "WAIT":
            if not incident.get("wait_snapshot_key"):
                incident["wait_snapshot_key"] = maintenance_incident_key(state)
            state["maintenance"]["active_incident_id"] = None
            incident["updated_at"] = utc_now()
            self.store.save_maintenance(path, state)
            self._clear_global_active(str(incident["incident_id"]))
            return True
        if action == "REPLACE_TASK":
            replacement = decision.get("replacement")
            global_state = self.state_store.load()
            applied = next(
                (
                    item
                    for item in global_state.get("history") or []
                    if isinstance(item, Mapping)
                    and item.get("incident_id") == incident.get("incident_id")
                    and item.get("request_id") == incident.get("request_id")
                    and item.get("application_state") == "RESOLVED"
                    and item.get("replacement_task_id")
                ),
                None,
            )
            if applied is not None:
                replacement_id = str(applied.get("replacement_task_id") or "")
                operational_tasks = (
                    list(tasks)
                    if tasks is not None
                    else self.store.discover_with_errors()[0]
                )
                if any(
                    task.get("task_id") == replacement_id
                    and task.get("replacement_incident_id") == incident.get("incident_id")
                    for task in operational_tasks
                ):
                    active = global_state.get("active_incident")
                    if (
                        isinstance(active, Mapping)
                        and active.get("incident_id") == incident.get("incident_id")
                    ):
                        global_state["active_incident"] = None
                        self.state_store.save(global_state)
                    return False
            try:
                if not isinstance(replacement, Mapping):
                    raise ValueError("replacement decision is missing its payload")
                parent_before = path.read_bytes()
                result = self.store.replace_task_and_rewire(
                    str(replacement.get("target_task_id") or ""),
                    str(replacement.get("task") or ""),
                    reuse_team=replacement.get("reuse_team"),
                    rewire_children=replacement.get("rewire_children"),
                    incident_id=str(incident.get("incident_id") or ""),
                )
                if path.read_bytes() != parent_before:
                    raise RuntimeError("replacement mutated immutable parent history")
                history_entry = self._history_entry(state, incident)
                if history_entry is None:
                    raise RuntimeError("replacement lost maintenance history provenance")
                replacement_id = str(result["replacement"]["task_id"])
                history_entry.update(
                    application_state="RESOLVED",
                    replacement_task_id=replacement_id,
                    resolved_at=utc_now(),
                    application_error=None,
                )
                history = list(global_state.get("history") or [])
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(history)
                        if isinstance(item, Mapping)
                        and item.get("request_id") == incident.get("request_id")
                    ),
                    None,
                )
                if existing_index is None:
                    history.append(history_entry)
                else:
                    history[existing_index] = history_entry
                global_state["history"] = history[-100:]
                global_state["active_incident"] = None
                global_state["last_error"] = None
                self.state_store.save(global_state)
                decision_value = MaintenanceDecision(
                    action="REPLACE_TASK",
                    reason=str(decision.get("reason") or ""),
                    role=None,
                    lesson=decision.get("lesson"),
                    replacement=dict(replacement),
                )
                append_resolved_lesson(
                    self.config.repository_root / "LEARNING.md",
                    {"state": "RESOLVED"},
                    decision_value,
                )
                return True
            except Exception as exc:
                incident["state"] = "ESCALATED"
                state["maintenance"]["suppressed_operational_key"] = current_key
                incident["last_error"] = f"{type(exc).__name__}: {exc}"
                incident["updated_at"] = utc_now()
                state["maintenance"]["active_incident_id"] = None
                self.store.save_maintenance(path, state)
                self._clear_global_active(str(incident["incident_id"]))
                return True
        control_id = incident.get("control_id")
        control = next(
            (
                item
                for item in state.get("controls") or []
                if isinstance(item, Mapping)
                and item.get("control_id") == control_id
                and self._control_matches_incident(item, incident)
            ),
            None,
        )
        if control is None:
            control = self._matching_control(state, incident)
        if control is None and control_id is None:
            try:
                decision_value = MaintenanceDecision(
                    action=action,
                    reason=str(decision.get("reason") or ""),
                    role=decision.get("role"),
                    lesson=decision.get("lesson"),
                    replacement=None,
                )
                controlled = self._dispatch(
                    path,
                    state,
                    decision_value,
                    incident=incident,
                )
            except Exception as exc:
                incident["state"] = "ESCALATED"
                state["maintenance"]["suppressed_operational_key"] = current_key
                incident["last_error"] = f"{type(exc).__name__}: {exc}"
                incident["updated_at"] = utc_now()
                state["maintenance"]["active_incident_id"] = None
                self.store.save_maintenance(path, state)
                self._clear_global_active(str(incident["incident_id"]))
                return True
            if controlled is not None:
                control = controlled["controls"][-1]
                state = self.store.load(path)
                incident = self._incident(state, str(incident["incident_id"]))
                if incident is None:
                    raise RuntimeError("maintenance incident disappeared after control recovery")
                incident["control_id"] = control["control_id"]
                incident["updated_at"] = utc_now()
                self.store.save_maintenance(path, state)
                return True
        elif control is not None and control_id is None:
            incident["control_id"] = control["control_id"]
            incident["updated_at"] = utc_now()
            self.store.save_maintenance(path, state)
            return True
        if control is None:
            incident["state"] = "ESCALATED"
            state["maintenance"]["suppressed_operational_key"] = current_key
            incident["last_error"] = "maintenance recovery control disappeared"
        elif control.get("status") in {"requested", "cleanup_pending"}:
            return False
        elif control.get("status") == "applied":
            if current_key == applied_key:
                incident["state"] = "ESCALATED"
                state["maintenance"]["suppressed_operational_key"] = current_key
                incident["last_error"] = (
                    "maintenance recovery applied but operational block remained unchanged"
                )
            else:
                self._resolve_incident(state, incident)
                self.store.save_maintenance(path, state)
                self._clear_global_active(str(incident["incident_id"]))
                return True
        else:
            incident["state"] = "ESCALATED"
            state["maintenance"]["suppressed_operational_key"] = current_key
            incident["last_error"] = str(control.get("result") or "recovery control failed")
        incident["updated_at"] = utc_now()
        state["maintenance"]["active_incident_id"] = None
        self.store.save_maintenance(path, state)
        self._clear_global_active(str(incident["incident_id"]))
        return True

    def _clear_global_active(self, incident_id: str) -> None:
        global_state = self.state_store.load()
        active = global_state.get("active_incident")
        if isinstance(active, Mapping) and active.get("incident_id") == incident_id:
            global_state["active_incident"] = None
            self.state_store.save(global_state)

    def _prompt(
        self,
        task: Mapping[str, Any],
        incident: Mapping[str, Any],
        *,
        include_constructor: bool,
        tasks: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        active_hop = next(
            (
                item
                for item in task.get("hops") or []
                if isinstance(item, Mapping)
                and item.get("hop_id") == task.get("active_hop_id")
            ),
            None,
        )
        task_id = str(task.get("task_id") or "")
        task_index = {
            str(item.get("task_id") or ""): item
            for item in tasks
            if isinstance(item, Mapping) and str(item.get("task_id") or "")
        }
        parents = []
        for parent_id in task.get("depends_on_task_ids") or ():
            parent = task_index.get(str(parent_id))
            parents.append({
                "task_id": str(parent_id),
                "status": str(parent.get("status") or "UNKNOWN") if parent else "MISSING",
                "team": (str(parent.get("team") or "") or None) if parent else None,
            })
        children = [
            {
                "task_id": str(item.get("task_id") or ""),
                "status": str(item.get("status") or "UNKNOWN"),
                "team": str(item.get("team") or "") or None,
            }
            for item in tasks
            if isinstance(item, Mapping)
            and task_id in [str(parent) for parent in item.get("depends_on_task_ids") or ()]
        ]
        role_status = {
            str(role): {
                "physical_role": record.get("physical_role"),
                "status": record.get("status"),
                "online": record.get("online"),
                "last_error": record.get("last_error"),
            }
            for role, record in (task.get("roles") or {}).items()
            if isinstance(record, Mapping)
        }
        snapshot = {
            "task_id": task.get("task_id"),
            "team": task.get("team"),
            "task_text": task.get("task_text"),
            "repository": task.get("repository"),
            "retained_reports": retained_report_references(task),
            "status": task.get("status"),
            "block_code": task.get("block_code"),
            "block_reason": task.get("block_reason"),
            "stop_reason": task.get("stop_reason"),
            "active_hop_id": task.get("active_hop_id"),
            "active_role": task.get("active_role"),
            "block_retryable": bool(task.get("block_retryable")),
            "updated_at": task.get("updated_at"),
            "incident_id": incident.get("incident_id"),
            "incident_turn": incident.get("turn"),
            "active_hop": active_hop,
            "roles": role_status,
            "cleanup": task.get("cleanup"),
            "dependencies": {
                "parents": parents,
                "children": children,
                "waiting": task.get("waiting"),
            },
            "latest_control": (task.get("controls") or [None])[-1],
        }
        sections = [
            "CDPA_MAINTENANCE_INCIDENT\n"
            + json.dumps(snapshot, ensure_ascii=False, indent=2)
        ]
        if include_constructor:
            sections.append(
                self.config.maintainers_constructor_path.read_text(encoding="utf-8").strip()
            )
            learning = self.config.repository_root / "LEARNING.md"
            if learning.is_file():
                sections.append(
                    "CURRENT LEARNING.md\n\n"
                    + learning.read_text(encoding="utf-8").strip()
                )
        sections.append(
            "Return a non-empty Markdown maintenance report followed by exactly one "
            "terminal fenced JSON decision with keys action, reason, role, lesson, replacement."
        )
        return "\n\n".join(sections).strip()

    def _commit_response(
        self,
        path: Path,
        *,
        incident_id: str,
        expected_incident_key: str,
        request_id: str,
        turn: int,
        report: str,
        report_at: datetime,
        decision: MaintenanceDecision,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        ReportEvidence | None,
        Mapping[str, Any] | None,
        bool,
    ]:
        outcome: dict[str, Any] = {}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            maintenance = current.get("maintenance")
            if not isinstance(maintenance, dict):
                raise RuntimeError("maintenance state disappeared before response commit")
            current_incident = self._incident(current, incident_id)
            if current_incident is None:
                raise RuntimeError("maintenance incident disappeared before response commit")
            current_key = maintenance_incident_key(current)
            active_id = maintenance.get("active_incident_id")
            report_data = str(report).encode("utf-8")
            expected_evidence = ReportEvidence(
                path=str(
                    (
                        self.config.repository_root
                        / maintenance_report_relative(
                            str(current["team"]), turn, report_at
                        )
                    ).resolve()
                ),
                sha256=hashlib.sha256(report_data).hexdigest(),
                size=len(report_data),
            )
            decision_value = self._decision_dict(decision)
            if (
                current_incident.get("key") == expected_incident_key
                and current_key == expected_incident_key
                and current_incident.get("request_id") == request_id
                and current_incident.get("turn") == turn
                and current_incident.get("decision") == decision_value
                and current_incident.get("report_path") == expected_evidence.path
                and current_incident.get("report_sha256") == expected_evidence.sha256
                and current_incident.get("report_size") == expected_evidence.size
            ):
                control = next(
                    (
                        item
                        for item in current.get("controls") or []
                        if isinstance(item, Mapping)
                        and item.get("control_id") == current_incident.get("control_id")
                    ),
                    None,
                )
                outcome.update(
                    evidence=expected_evidence,
                    control=control,
                    stale=False,
                )
                return current
            if (
                current_incident.get("key") != expected_incident_key
                or active_id != incident_id
                or current_key != expected_incident_key
            ):
                now = utc_now()
                current_incident.update(
                    {
                        "state": "RESOLVED",
                        "decision": None,
                        "report_path": None,
                        "report_sha256": None,
                        "report_size": None,
                        "control_id": None,
                        "resolved_at": now,
                        "updated_at": now,
                        "last_error": None,
                        "superseded_at": now,
                        "superseded_by_key": current_key,
                        "superseded_response_request_id": request_id,
                        "superseded_reason": (
                            "maintenance response discarded because task incident "
                            "changed before commit"
                        ),
                    }
                )
                if active_id == incident_id:
                    maintenance["active_incident_id"] = None
                maintenance["last_resolved_at"] = now
                outcome["stale"] = True
                return current

            if decision.action == "RETRY_HOP" and not current.get("block_retryable"):
                raise ValueError("RETRY_HOP requires a retryable task block")
            if decision.action == "REPLACE_TASK":
                replacement = decision.replacement or {}
                if replacement.get("target_task_id") != current.get("task_id"):
                    raise ValueError("replacement target must equal the active incident task")
                status = str(current.get("status") or "").upper()
                if status not in {"STOPPED", "BLOCKED"}:
                    raise ValueError("replacement target must be STOPPED or BLOCKED")
                cleanup = current.get("cleanup")
                if isinstance(cleanup, Mapping) and str(cleanup.get("state") or "").upper() in {
                    "CLEARING", "CLEARED"
                }:
                    raise ValueError("replacement target cleanup is already in progress")
                if replacement.get("reuse_team") is True and status != "STOPPED":
                    raise ValueError("reuse_team requires a STOPPED replacement target")
            control: Mapping[str, Any] | None = None
            if decision.action not in {"WAIT", "REPLACE_TASK"}:
                control = self.store._queue_control(
                    current,
                    _CONTROL_ACTIONS[decision.action],
                    role=decision.role,
                    reason=decision.reason,
                    maintenance_incident_id=incident_id,
                    maintenance_request_id=request_id,
                )
            evidence = write_maintenance_report(
                self.config.repository_root,
                team=str(current["team"]),
                turn=turn,
                report=report,
                at=report_at,
            )
            current_incident.update(
                {
                    "state": "OPEN" if decision.action == "WAIT" else "RUNNING",
                    "decision": decision_value,
                    "report_path": evidence.path,
                    "report_sha256": evidence.sha256,
                    "report_size": evidence.size,
                    "applied_snapshot_key": _maintenance_operational_key(current),
                    "updated_at": utc_now(),
                    "last_error": None,
                }
            )
            if decision.action == "WAIT":
                current_incident["wait_snapshot_key"] = expected_incident_key
                maintenance["active_incident_id"] = None
            elif control is not None:
                current_incident["control_id"] = control["control_id"]
            outcome.update(evidence=evidence, control=control, stale=False)
            return current

        saved = self.store.update_maintenance(path, mutate)
        incident = self._incident(saved, incident_id)
        if incident is None:
            raise RuntimeError("maintenance incident disappeared after response commit")
        return (
            saved,
            incident,
            outcome.get("evidence"),
            outcome.get("control"),
            bool(outcome.get("stale")),
        )

    def _dispatch(
        self,
        path: Path,
        state: Mapping[str, Any],
        decision: MaintenanceDecision,
        *,
        incident: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if decision.action in {"WAIT", "REPLACE_TASK"}:
            return None
        if decision.action == "RETRY_HOP" and not state.get("block_retryable"):
            raise ValueError("RETRY_HOP requires a retryable task block")
        incident_id = str(incident.get("incident_id") or "").strip()
        request_id = str(incident.get("request_id") or "").strip()
        if not incident_id or not request_id:
            raise ValueError("maintenance control requires incident/request provenance")
        action = _CONTROL_ACTIONS[decision.action]
        return self.store.request_control(
            path,
            action,
            role=decision.role,
            reason=decision.reason,
            maintenance_incident_id=incident_id,
            maintenance_request_id=request_id,
        )

    async def advance(
        self,
        tasks: Sequence[tuple[Path, dict[str, Any]]],
        browser_context: Any,
    ) -> bool:
        operation_lock = exclusive_file_lock(
            self.state_store.run_lock_path, blocking=False
        )
        try:
            operation_lock.__enter__()
        except BlockingIOError:
            return False
        try:
            return await self._advance_locked(tasks, browser_context)
        finally:
            operation_lock.__exit__(None, None, None)

    async def _advance_locked(
        self,
        tasks: Sequence[tuple[Path, dict[str, Any]]],
        browser_context: Any,
    ) -> bool:
        candidates: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
        loaded = [
            (Path(raw_path).resolve(), self.store.load(Path(raw_path).resolve()))
            for raw_path, _snapshot in tasks
        ]
        global_changed = self._reconcile_global_state(loaded)
        operational_tasks = [state for _path, state in loaded]
        for path, state in loaded:
            if self._reconcile_active(path, state, tasks=operational_tasks):
                return True
            active_id = (
                state.get("maintenance", {}).get("active_incident_id")
                if isinstance(state.get("maintenance"), Mapping)
                else None
            )
            active = self._incident(state, active_id) if isinstance(active_id, str) else None
            if active is None or active.get("state") not in {"OPEN", "RUNNING"}:
                continue
            if active.get("decision") is not None:
                return global_changed
            candidates.append((path, state, active))
        if global_changed:
            return True
        if not candidates:
            for path, state in loaded:
                maintenance = state.get("maintenance")
                suppressed_before = (
                    maintenance.get("suppressed_operational_key")
                    if isinstance(maintenance, Mapping)
                    else None
                )
                before = (
                    len(state.get("maintenance", {}).get("incidents", []))
                    if isinstance(state.get("maintenance"), Mapping)
                    else 0
                )
                incident = ensure_maintenance_incident(state)
                current_maintenance = state.get("maintenance")
                suppressed_after = (
                    current_maintenance.get("suppressed_operational_key")
                    if isinstance(current_maintenance, Mapping)
                    else None
                )
                maintenance_changed = suppressed_before != suppressed_after
                if incident is None:
                    if maintenance_changed:
                        self.store.save_maintenance(path, state)
                        return True
                    continue
                if (
                    len(state["maintenance"]["incidents"]) != before
                    or maintenance_changed
                ):
                    state = self.store.save_maintenance(path, state)
                    incident = self._incident(state, str(incident["incident_id"]))
                    assert incident is not None
                if (
                    incident.get("state") in {"OPEN", "RUNNING"}
                    and incident.get("decision") is None
                ):
                    candidates.append((path, state, incident))
        if not candidates:
            return False
        path, state, incident = min(
            candidates,
            key=lambda item: (
                str(item[2].get("created_at") or ""),
                str(item[1].get("task_id") or ""),
            ),
        )
        incident_id = str(incident["incident_id"])
        expected_incident_key = str(incident["key"])
        global_state = self.state_store.load()
        actions = CDPATabActions(browser_context, self.config)
        try:
            acquired = await actions.acquire_global_role(MAINTAINER_ROLE)
            prior_page_id = str(global_state.get("page_id") or "")
            prior_conversation = _conversation_identity(global_state.get("page_url"))
            current_conversation = _conversation_identity(acquired.url)
            generation = int(global_state.get("conversation_generation") or 0)
            generation_changed = False
            if prior_page_id and prior_page_id != acquired.page_id:
                generation += 1
                generation_changed = True
            elif prior_page_id == acquired.page_id and prior_conversation:
                if current_conversation != prior_conversation:
                    generation += 1
                    generation_changed = True
            global_state["conversation_generation"] = generation
            global_state["page_id"] = acquired.page_id
            global_state["page_url"] = acquired.url
            if int(incident.get("turn") or 0) < 1:
                global_state["turn"] = int(global_state.get("turn") or 0) + 1
                incident["turn"] = global_state["turn"]
            turn = int(incident["turn"])
            base_request_id = f"{incident['incident_id']}-turn{turn}"
            request_id = str(incident.get("request_id") or base_request_id)
            incident["report_at"] = incident.get("report_at") or utc_now()
            incident["state"] = "RUNNING"
            incident["updated_at"] = utc_now()
            include_constructor = (
                global_state.get("constructor_sent_generation") != generation
            )
            prompt = str(incident.get("prompt") or "")
            prompt_generation = incident.get("prompt_generation")
            if prompt and prompt_generation is None:
                prompt_generation = generation - 1 if generation_changed else generation
                incident["prompt_generation"] = prompt_generation
            if prompt and prompt_generation != generation:
                ledger_path = self.config.plans_root / "maintainers" / "requests.json"
                record = RequestLedger(ledger_path).get(request_id)
                pre_send_safe = record is None or (
                    record.status in {RequestStatus.NEW, RequestStatus.PROMPT_SET}
                    and record.attempts == 0
                    and record.accepted_at is None
                    and record.receipt is None
                    and record.prompt.strip() == prompt.strip()
                )
                if not pre_send_safe:
                    record_status = record.status.value if record is not None else "unknown"
                    detail = (
                        "Maintainers conversation generation changed "
                        f"from {prompt_generation} to {generation} while durable request "
                        f"{request_id} is {record_status}; refusing resend"
                    )
                    incident["state"] = "ESCALATED"
                    incident["last_error"] = detail
                    incident["updated_at"] = utc_now()
                    state["maintenance"]["active_incident_id"] = None
                    operational_key = _maintenance_operational_key(state)
                    if operational_key is not None:
                        state["maintenance"]["suppressed_operational_key"] = operational_key
                    global_state["active_incident"] = None
                    global_state["last_error"] = detail
                    self.store.save_maintenance(path, state)
                    self.state_store.save(global_state)
                    return True
                request_id = f"{base_request_id}-g{generation}"
                prompt = ""
            if not prompt:
                prompt = self._prompt(
                    state,
                    incident,
                    include_constructor=include_constructor,
                    tasks=[item for _path, item in loaded],
                )
                incident["prompt"] = prompt
                incident["prompt_sha256"] = hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest()
                incident["prompt_generation"] = generation
                incident["constructor_included"] = include_constructor
            incident["request_id"] = request_id
            global_state["active_incident"] = {
                "task_id": state["task_id"],
                "incident_id": incident["incident_id"],
                "turn": turn,
                "request_id": request_id,
            }
            global_state["last_error"] = None
            self.store.save_maintenance(path, state)
            self.state_store.save(global_state)
            constructor = self.config.maintainers_constructor_path.read_text(
                encoding="utf-8"
            )

            def validate_candidate(candidate: Any) -> None:
                parse_maintenance_response(
                    str(candidate.text),
                    configured_roles=self.config.roles,
                )

            block = DurableSendBlock(
                prompt,
                ledger_path=self.config.plans_root / "maintainers" / "requests.json",
                source_context={
                    "task_id": state["task_id"],
                    "team": state["team"],
                    "incident_id": incident["incident_id"],
                },
                role_prompt_hash=hashlib.sha256(constructor.encode()).hexdigest(),
                request_id=request_id,
                render_request_marker=False,
                wait_for_response=True,
                response_timeout_ms=round(
                    self.config.maintenance_timeout_seconds * 1000
                ),
                stable_ms=self.config.maintenance_stable_ms,
                poll_ms=self.config.maintenance_poll_ms,
                active_reload_after_ms=round(
                    self.config.maintenance_refresh_after_seconds * 1000
                ),
                candidate_validator=validate_candidate,
                minimum_samples=2,
                invalid_grace_ms=max(1_000, self.config.maintenance_stable_ms),
            )
            output = await block.run(WorkflowContext(acquired.client))
            response = output.get("response") if isinstance(output, Mapping) else None
            if isinstance(response, Mapping):
                text = str(response.get("text") or "")
            else:
                text = str(getattr(response, "text", ""))
            report, decision = parse_maintenance_response(
                text,
                configured_roles=self.config.roles,
            )
            report_at = datetime.fromisoformat(
                str(incident["report_at"]).replace("Z", "+00:00")
            )
            state, incident, evidence, _control, stale = self._commit_response(
                path,
                incident_id=incident_id,
                expected_incident_key=expected_incident_key,
                request_id=request_id,
                turn=turn,
                report=report,
                report_at=report_at,
                decision=decision,
            )
            if stale:
                active_global = global_state.get("active_incident")
                if (
                    isinstance(active_global, Mapping)
                    and active_global.get("incident_id") == incident_id
                ):
                    global_state["active_incident"] = None
                if (
                    incident.get("constructor_included") is True
                    and incident.get("prompt_generation") == generation
                ):
                    global_state["constructor_sent_generation"] = generation
                global_state["last_error"] = None
                self.state_store.save(global_state)
                return True
            if evidence is None:
                raise RuntimeError("maintenance response commit lost report evidence")
            history_entry = self._history_entry(state, incident)
            if history_entry is None:
                raise RuntimeError("maintenance response commit lost global history evidence")
            history = global_state.setdefault("history", [])
            existing_index = next(
                (
                    index
                    for index, item in enumerate(history)
                    if isinstance(item, Mapping)
                    and item.get("request_id") == request_id
                ),
                None,
            )
            if existing_index is None:
                history.append(history_entry)
            else:
                history[existing_index] = history_entry
            global_state["history"] = history[-100:]
            global_state["active_incident"] = self._active_projection(state, incident)
            if (
                incident.get("constructor_included") is True
                and incident.get("prompt_generation") == generation
            ):
                global_state["constructor_sent_generation"] = generation
            global_state["last_error"] = None
            self.state_store.save(global_state)
            return True
        except Exception as exc:
            if is_cdp_disconnect(exc):
                raise
            detail = f"{type(exc).__name__}: {exc}"
            state = self.store.load(path)
            current = self._incident(state, str(incident["incident_id"]))
            if current is not None:
                escalated = current.get("last_error") == detail
                if escalated:
                    current["state"] = "ESCALATED"
                    state["maintenance"]["suppressed_operational_key"] = (
                        _maintenance_operational_key(state)
                    )
                    state["maintenance"]["active_incident_id"] = None
                current["last_error"] = detail
                current["updated_at"] = utc_now()
                self.store.save_maintenance(path, state)
                if escalated:
                    global_state["active_incident"] = None
            global_state["last_error"] = detail
            self.state_store.save(global_state)
            return True


def append_resolved_lesson(
    learning_path: str | Path,
    incident: Mapping[str, Any],
    decision: MaintenanceDecision,
) -> bool:
    lesson = str(decision.lesson or "").strip()
    if incident.get("state") != "RESOLVED" or not lesson:
        return False
    if "\n" in lesson or "\r" in lesson or len(lesson) > 600:
        raise ValueError("maintenance lesson must be one paragraph and at most 600 characters")
    path = Path(learning_path).expanduser().resolve()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        current = path.read_text(encoding="utf-8")
        if lesson in current:
            return False
        with path.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(f"- {lesson}\n")
            handle.flush()
            os.fsync(handle.fileno())
        fsync_parent_directory(path)
    return True
