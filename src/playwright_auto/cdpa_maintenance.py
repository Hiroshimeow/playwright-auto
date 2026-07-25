from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_actions import CDPATabActions
from .cdpa_commands import RepairRequest
from .cdpa_config import CDPAConfig
from .cdpa_safety import (
    extract_probeable_url,
    project_maintenance_incident,
    sanitize_exception,
    sanitize_text,
    sanitize_url,
    sanitize_value,
)
from .cdpa_routes import ReportEvidence
from .cdpa_store import TaskStore, retained_report_references, utc_now
from .cdpa_team import validate_exact_team
from .connection import is_cdp_disconnect
from .durable import RequestLedger, RequestStatus
from .durable_blocks import DurableSendBlock
from .file_lock import exclusive_file_lock, fsync_parent_directory
from .workflow import WorkflowContext

MAINTAINER_ROLE = "MAINTAINERS"
_INCIDENT_STATES = frozenset({"OPEN", "RUNNING", "SUSPENDED", "RESOLVED", "ESCALATED"})
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
_V2_RECOVERY_ACTIONS = frozenset(
    {
        "RESUME_TASK",
        "RETRY_HOP",
        "RESTART_ROLE",
        "NEW_CHAT_ROLE",
        "OPEN_ROLE_TAB",
        "ROUTE_PLAN",
    }
)
_CONTROL_ACTIONS = {
    "RESUME_TASK": "resume",
    "RETRY_HOP": "retry",
    "RESTART_ROLE": "restart_role",
    "NEW_CHAT_ROLE": "new_chat",
    "OPEN_ROLE_TAB": "open_tab",
    "ROUTE_PLAN": "route_plan",
    "CREATE_REPAIR_TASK": "create_repair_task",
}
_DECISION_KEYS = frozenset({"action", "reason", "role", "lesson", "replacement"})
_DECISION_V2_KEYS = frozenset({"version", "recovery", "repair", "lesson"})
_RECOVERY_KEYS = frozenset({"action", "reason", "role"})
_REPAIR_KEYS = frozenset(
    {"root_cause", "reason", "disposition", "reproduction", "source_areas", "required_tests"}
)
_REPLACEMENT_KEYS = frozenset({"target_task_id", "task", "reuse_team", "rewire_children"})
_TOOLING_PROBE_KEYS = frozenset(
    {"version", "kind", "dependency", "endpoint", "method", "auth_profile", "required_tools"}
)
_TOOLING_PROBE_KIND = "mcp_http_jsonrpc_tools_list"
_TOOLING_PROBE_METHOD = "tools/list"
_TOOLING_PROBE_PROTOCOL_VERSION = "2025-03-26"
_TOOLING_PROBE_TIMEOUT_SECONDS = 2.5
_TOOLING_PROBE_MAX_RESPONSE_BYTES = 1_048_576
_TOOLING_PROBE_INITIALIZE_ID = "cdpa-maintainers-initialize"
_TOOLING_PROBE_LIST_ID = "cdpa-maintainers-tools-list"
_TOOLING_PROBE_ACCEPT = "application/json, text/event-stream"
_TOOLING_AUTH_PROFILES = {
    "none": (),
    "local_mcp_static_bearer": ("MCP_BEARER_TOKEN", "MCP_AUTH_PASSWORD"),
}
_JSON_FENCE = re.compile(r"```json\s*(\{.*\})\s*```\s*$", re.DOTALL | re.IGNORECASE)
_JSON_LABEL = re.compile(r"(?:^|\s)json\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ToolingProbeDescriptor:
    version: int
    kind: str
    dependency: str
    endpoint: str
    method: str
    auth_profile: str
    required_tools: tuple[str, ...]

    @classmethod
    def mcp_tools_list(
        cls,
        *,
        dependency: str,
        endpoint: str,
        auth_profile: str = "none",
        required_tools: Sequence[str] = ("shell_execute",),
    ) -> "ToolingProbeDescriptor":
        return cls.from_mapping(
            {
                "version": 1,
                "kind": _TOOLING_PROBE_KIND,
                "dependency": dependency,
                "endpoint": endpoint,
                "method": _TOOLING_PROBE_METHOD,
                "auth_profile": auth_profile,
                "required_tools": list(required_tools),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolingProbeDescriptor":
        if not isinstance(value, Mapping) or set(value) != _TOOLING_PROBE_KEYS:
            raise ValueError(
                f"tooling probe descriptor must contain exactly {sorted(_TOOLING_PROBE_KEYS)!r}"
            )
        version = value.get("version")
        if isinstance(version, bool) or version != 1:
            raise ValueError("tooling probe descriptor version must be 1")
        kind = str(value.get("kind") or "").strip()
        method = str(value.get("method") or "").strip()
        dependency = str(value.get("dependency") or "").strip()
        endpoint = str(value.get("endpoint") or "").strip()
        auth_profile = str(value.get("auth_profile") or "").strip()
        required_raw = value.get("required_tools")
        if kind != _TOOLING_PROBE_KIND:
            raise ValueError(f"unsupported tooling probe kind {kind!r}")
        if method != _TOOLING_PROBE_METHOD:
            raise ValueError(f"unsupported tooling probe method {method!r}")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", dependency) is None:
            raise ValueError("tooling probe dependency must be a bounded identifier")
        if auth_profile not in _TOOLING_AUTH_PROFILES:
            raise ValueError(f"unsupported tooling auth profile {auth_profile!r}")
        if (
            not isinstance(required_raw, list)
            or not required_raw
            or len(required_raw) > 16
        ):
            raise ValueError("tooling probe required_tools must contain 1 to 16 names")
        required_tools = tuple(str(item).strip() for item in required_raw)
        if any(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item) is None
            for item in required_tools
        ):
            raise ValueError("tooling probe required_tools contains an invalid name")
        if len(set(required_tools)) != len(required_tools):
            raise ValueError("tooling probe required_tools must be unique")
        try:
            parsed = urlparse(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("tooling probe endpoint is invalid") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or port is None
            or not (1 <= port <= 65535)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("tooling probe endpoint must be exact loopback HTTP with an explicit port")
        path = parsed.path or "/"
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        canonical = f"http://{host}:{port}{path}"
        if endpoint != canonical:
            raise ValueError("tooling probe endpoint must be canonical")
        return cls(
            version=1,
            kind=kind,
            dependency=dependency,
            endpoint=endpoint,
            method=method,
            auth_profile=auth_profile,
            required_tools=required_tools,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "dependency": self.dependency,
            "endpoint": self.endpoint,
            "method": self.method,
            "auth_profile": self.auth_profile,
            "required_tools": list(self.required_tools),
        }


class ToolingUnavailableError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        probe: ToolingProbeDescriptor,
        probe_result: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if not isinstance(probe, ToolingProbeDescriptor):
            raise TypeError("probe must be a ToolingProbeDescriptor")
        self.probe = ToolingProbeDescriptor.from_mapping(probe.to_dict())
        self.probe_result = dict(probe_result) if isinstance(probe_result, Mapping) else None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirectHandler()).open(
        request,
        timeout=timeout,
    )


@dataclass(frozen=True)
class MaintenanceStep:
    action: str
    reason: str
    role: str | None = None


@dataclass(frozen=True)
class MaintenanceDecision:
    action: str
    reason: str
    role: str | None = None
    lesson: str | None = None
    replacement: dict[str, object] | None = None
    recovery: tuple[MaintenanceStep, ...] = ()
    repair: dict[str, object] | None = None
    version: int = 1


def _maintenance_operational_key(state: Mapping[str, Any]) -> str | None:
    cleanup = state.get("cleanup")
    if isinstance(cleanup, Mapping) and str(cleanup.get("state") or "").upper() in {
        "CLEARING",
        "CLEARED",
    }:
        return None
    status = str(state.get("status") or "").upper()
    terminal = str(state.get("terminal_state") or "").upper()
    operator_controls = [
        item
        for item in state.get("controls") or []
        if isinstance(item, Mapping) and item.get("origin") == "operator"
    ]
    if any(
        item.get("status") == "requested"
        and item.get("action")
        in {"pause", "stop", "restart_role", "new_chat", "clear_team"}
        for item in operator_controls
    ):
        return None
    if status == "STOPPED" and any(
        item.get("action") == "stop" and item.get("status") == "applied"
        for item in operator_controls
    ):
        return None
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


def _parse_lesson(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("maintenance lesson must be a string or null")
    lesson = str(value).strip() if value is not None else None
    if lesson:
        if "\n" in lesson or "\r" in lesson:
            raise ValueError("lesson must be one paragraph")
        if len(lesson) > 600:
            raise ValueError("lesson must be at most 600 characters")
    return lesson or None


def _parse_recovery_step(
    value: Any,
    *,
    configured_roles: Sequence[str],
) -> MaintenanceStep:
    if not isinstance(value, Mapping) or set(value) != _RECOVERY_KEYS:
        raise ValueError(
            f"recovery step must contain exactly {sorted(_RECOVERY_KEYS)!r}"
        )
    if not isinstance(value.get("action"), str) or not isinstance(value.get("reason"), str):
        raise ValueError("recovery action and reason must be strings")
    raw_role = value.get("role")
    if raw_role is not None and not isinstance(raw_role, str):
        raise ValueError("recovery role must be a string or null")
    action = value["action"].strip().upper()
    reason = value["reason"].strip()
    role = str(raw_role).strip().upper() if raw_role is not None else None
    if action not in _V2_RECOVERY_ACTIONS:
        raise ValueError(f"unsupported recovery action {action!r}")
    if not reason:
        raise ValueError("recovery reason must not be empty")
    roles = {str(item).strip().upper() for item in configured_roles}
    if action in _ROLE_ACTIONS:
        if role not in roles or role == MAINTAINER_ROLE:
            raise ValueError("role action requires a normal configured role")
    elif role is not None:
        raise ValueError("role is allowed only for role actions")
    return MaintenanceStep(action=action, reason=reason, role=role)


def _parse_repair(value: Any) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _REPAIR_KEYS:
        raise ValueError(f"repair must contain exactly {sorted(_REPAIR_KEYS)!r}")
    source_areas = value.get("source_areas")
    required_tests = value.get("required_tests")
    if not isinstance(source_areas, list):
        raise ValueError("repair source_areas must be a list")
    if not isinstance(required_tests, list):
        raise ValueError("repair required_tests must be a list")
    proposal = RepairRequest.validate_proposal(
        root_cause=value.get("root_cause"),
        disposition=value.get("disposition"),
        reason=value.get("reason"),
        reproduction=value.get("reproduction"),
        source_areas=source_areas,
        required_tests=required_tests,
        lesson=None,
    )
    return {
        "root_cause": str(proposal["root_cause"]),
        "reason": str(proposal["reason"]),
        "disposition": str(proposal["disposition"]),
        "reproduction": str(proposal["reproduction"]),
        "source_areas": list(proposal["source_areas"]),
        "required_tests": list(proposal["required_tests"]),
    }


def parse_maintenance_response(
    text: str,
    *,
    configured_roles: Sequence[str] = ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"),
) -> tuple[str, MaintenanceDecision]:
    source = str(text).strip()
    report, decision_source = _split_terminal_decision(source)
    value = _decode_decision(decision_source)
    if set(value) == _DECISION_V2_KEYS:
        if value.get("version") != 2:
            raise ValueError("maintenance decision version must be 2")
        raw_recovery = value.get("recovery")
        if not isinstance(raw_recovery, list):
            raise ValueError("maintenance recovery must be a list")
        if len(raw_recovery) > 3:
            raise ValueError("maintenance recovery may contain at most three steps")
        recovery = tuple(
            _parse_recovery_step(item, configured_roles=configured_roles)
            for item in raw_recovery
        )
        repair = _parse_repair(value.get("repair"))
        lesson = _parse_lesson(value.get("lesson"))
        if not recovery and repair is None:
            raise ValueError("maintenance v2 decision requires recovery or repair")
        first = recovery[0] if recovery else None
        return report + "\n", MaintenanceDecision(
            action=first.action if first else "CREATE_REPAIR_TASK",
            reason=first.reason if first else str(repair["reason"]),
            role=first.role if first else None,
            lesson=lesson,
            replacement=None,
            recovery=recovery,
            repair=repair,
            version=2,
        )
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
    lesson = _parse_lesson(value.get("lesson"))
    return report + "\n", MaintenanceDecision(
        action=action,
        reason=reason,
        role=role,
        lesson=lesson,
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
    body = sanitize_text(str(report))
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
        value = sanitize_value(
            json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        )
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
        value = {
            "action": decision.action,
            "reason": decision.reason,
            "role": decision.role,
            "lesson": decision.lesson,
            "replacement": decision.replacement,
        }
        if decision.version == 2:
            value.update(
                version=2,
                recovery=[
                    {"action": step.action, "reason": step.reason, "role": step.role}
                    for step in decision.recovery
                ],
                repair=decision.repair,
            )
        return value

    @staticmethod
    def _decision_from_mapping(value: Mapping[str, Any]) -> MaintenanceDecision:
        recovery = tuple(
            MaintenanceStep(
                action=str(item.get("action") or "").upper(),
                reason=str(item.get("reason") or ""),
                role=(str(item.get("role") or "").upper() or None),
            )
            for item in value.get("recovery") or []
            if isinstance(item, Mapping)
        )
        return MaintenanceDecision(
            action=str(value.get("action") or ""),
            reason=str(value.get("reason") or ""),
            role=value.get("role"),
            lesson=value.get("lesson"),
            replacement=(
                dict(value["replacement"])
                if isinstance(value.get("replacement"), Mapping)
                else None
            ),
            recovery=recovery,
            repair=(dict(value["repair"]) if isinstance(value.get("repair"), Mapping) else None),
            version=int(value.get("version") or 1),
        )

    @staticmethod
    def _repair_request(
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
        decision: MaintenanceDecision,
    ) -> RepairRequest:
        repair = decision.repair
        if not isinstance(repair, Mapping):
            raise ValueError("maintenance repair proposal is missing")
        return RepairRequest.create(
            root_cause=repair["root_cause"],
            affected_state=state,
            incident_id=incident["incident_id"],
            disposition=repair["disposition"],
            reason=repair["reason"],
            reproduction=repair["reproduction"],
            source_areas=repair["source_areas"],
            required_tests=repair["required_tests"],
            lesson=decision.lesson,
        )

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

    @staticmethod
    def _environment_prerequisite(error: BaseException) -> str | None:
        detail = f"{type(error).__name__}: {error}".casefold()
        tooling_tokens = (
            "mcp",
            "tool unavailable",
            "tooling unavailable",
            "tooling",
            "tool gateway",
            "capability unavailable",
        )
        if any(token in detail for token in tooling_tokens):
            return "tooling"
        if is_cdp_disconnect(error):
            return "browser_cdp"
        browser_tokens = (
            "cdp",
            "browser",
            "websocket",
            "target closed",
            "page closed",
            "127.0.0.1:9222",
            "localhost:9222",
        )
        if any(token in detail for token in browser_tokens):
            return "browser_cdp"
        filesystem_tokens = (
            "filesystem",
            "permission denied",
            "read-only file system",
            "no space left",
            "file not found",
        )
        if any(token in detail for token in filesystem_tokens):
            return "filesystem"
        if isinstance(error, ConnectionRefusedError):
            return "network"
        if isinstance(error, TimeoutError) or any(
            token in detail
            for token in ("network", "dns", "timeout", "timed out", "connection refused")
        ):
            return "network"
        if isinstance(error, ConnectionError):
            return "network"
        if isinstance(error, OSError):
            if getattr(error, "errno", None) in {
                errno.ENOSPC,
                errno.EROFS,
                errno.EACCES,
                errno.EPERM,
                errno.ENOENT,
            }:
                return "filesystem"
            return "network"
        return None

    @staticmethod
    def _browser_environment_probe(browser_context: Any) -> dict[str, Any]:
        pages_value: list[Any] = []
        browser_pages: list[dict[str, str | None]] = []
        browser_error: str | None = None
        browser_is_connected: bool | None = None
        browser = getattr(browser_context, "browser", None)
        if browser is None:
            browser_error = "browser context has no browser connection"
        else:
            try:
                browser_is_connected = bool(browser.is_connected())
            except Exception as exc:
                browser_error = sanitize_exception(exc)
        try:
            pages_value = list(browser_context.pages)
            browser_pages = sorted(
                (
                    {
                        "page_id": str(getattr(page, "page_id", "") or "") or None,
                        "url": sanitize_url(str(getattr(page, "url", "") or ""))
                        if getattr(page, "url", None)
                        else None,
                    }
                    for page in pages_value
                ),
                key=lambda item: (
                    str(item["page_id"] or ""),
                    str(item["url"] or ""),
                ),
            )
        except Exception as exc:
            if browser_error is None:
                browser_error = sanitize_exception(exc)
        return {
            "available": browser_error is None and browser_is_connected is True,
            "pages": pages_value,
            "browser_pages": browser_pages,
            "browser_error": browser_error,
            "browser_is_connected": browser_is_connected,
        }

    async def _live_browser_environment_probe(
        self,
        browser_context: Any,
    ) -> dict[str, Any]:
        browser = self._browser_environment_probe(browser_context)
        live_error: str | None = None
        live_succeeded = False
        if browser["available"]:
            cookies = getattr(browser_context, "cookies", None)
            if not callable(cookies):
                live_error = "browser context has no callable cookies probe"
            else:
                try:
                    result = cookies()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=2.5)
                    live_succeeded = True
                except Exception as exc:
                    live_error = sanitize_exception(exc)
        elif browser.get("browser_error"):
            live_error = str(browser["browser_error"])
        elif browser.get("browser_is_connected") is False:
            live_error = "browser.is_connected() is false"
        return {
            **browser,
            "available": bool(browser["available"] and live_succeeded),
            "live_operation": "context.cookies",
            "live_timeout_seconds": 2.5,
            "live_succeeded": live_succeeded,
            "live_error": live_error,
        }

    @staticmethod
    def _extract_endpoint_from_detail(detail: str | None) -> str | None:
        return extract_probeable_url(detail)

    @staticmethod
    def _network_environment_probe(
        pages: Sequence[Any],
        *,
        failure_detail: str | None = None,
    ) -> dict[str, Any]:
        attempted: list[dict[str, str | None]] = []
        endpoint = MaintainerCoordinator._extract_endpoint_from_detail(failure_detail)
        for page in pages:
            if isinstance(page, Mapping):
                url = sanitize_url(str(page.get("url") or "")) or ""
                page_id = str(page.get("page_id") or "") or None
            else:
                url = sanitize_url(str(getattr(page, "url", "") or "")) or ""
                page_id = str(getattr(page, "page_id", "") or "") or None
            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            host = str(parsed.hostname or "").casefold()
            if host not in {"chatgpt.com", "www.chatgpt.com"}:
                continue
            if len(attempted) >= 3:
                break
            attempted.append(
                {
                    "page_id": page_id,
                    "url": url or None,
                }
            )
            if endpoint is None:
                endpoint = f"{parsed.scheme or 'https'}://{parsed.netloc}/"
        if endpoint is None:
            return {
                "available": False,
                "attempted_pages": attempted,
                "evidence": None,
                "errors": ["no exact network endpoint available for readiness probe"],
            }

        request = urllib.request.Request(
            endpoint,
            method="HEAD",
            headers={"User-Agent": "playwright-auto-cdpa-maintainers-health/1"},
        )
        response: Any = None
        try:
            response = _open_no_redirect(request, timeout=2.5)
            status = int(getattr(response, "status", response.getcode()))
            available = 0 < status < 500
            error = None if available else f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            response = exc
            status = int(exc.code)
            available = 0 < status < 500
            error = None if available else f"HTTP {status}"
        except Exception as exc:
            status = None
            available = False
            error = sanitize_exception(exc)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        evidence = {
            "endpoint": endpoint,
            "method": "HEAD",
            "timeout_seconds": 2.5,
            "status": status,
            "error": error,
        }
        return {
            "available": available,
            "attempted_pages": attempted,
            "evidence": evidence,
            "errors": [] if available else [str(error or "network readiness unavailable")],
        }

    @staticmethod
    def _tooling_probe_descriptor(error: BaseException) -> dict[str, object] | None:
        if not isinstance(error, ToolingUnavailableError):
            return None
        return error.probe.to_dict()

    @staticmethod
    def _tooling_probe_result(error: BaseException) -> dict[str, Any] | None:
        if not isinstance(error, ToolingUnavailableError):
            return None
        return dict(error.probe_result) if isinstance(error.probe_result, Mapping) else None

    def _configured_tooling_descriptor(self) -> ToolingProbeDescriptor | None:
        configured = self.config.maintenance_tooling_probe
        if configured is None:
            return None
        return ToolingProbeDescriptor.mcp_tools_list(
            dependency=configured.dependency,
            endpoint=configured.endpoint,
            auth_profile=configured.auth_profile,
            required_tools=configured.required_tools,
        )

    async def _require_configured_tooling(self) -> dict[str, Any] | None:
        descriptor = self._configured_tooling_descriptor()
        if descriptor is None:
            return None
        result = await asyncio.to_thread(
            self._tooling_environment_probe,
            f"configured worker dependency {descriptor.dependency}",
            descriptor.to_dict(),
            execute=True,
        )
        if result.get("available") is True:
            return result
        reason = str(result.get("error") or "required MCP capability is unavailable")
        raise ToolingUnavailableError(
            f"{descriptor.dependency} configured tooling preflight failed: {reason}",
            probe=descriptor,
            probe_result=result,
        )

    @staticmethod
    def _resolve_tooling_bearer(auth_profile: str) -> str | None:
        env_names = _TOOLING_AUTH_PROFILES.get(auth_profile)
        if env_names is None:
            raise ValueError(f"unsupported tooling auth profile {auth_profile!r}")
        if not env_names:
            return None
        for name in env_names:
            value = str(os.environ.get(name) or "").strip()
            if value:
                return value
        raise RuntimeError(
            f"tooling auth profile {auth_profile!r} has no runtime credential"
        )

    @staticmethod
    def _decode_mcp_messages(raw: bytes) -> tuple[Mapping[str, Any], ...]:
        text = raw.decode("utf-8").strip()
        if not text:
            raise ValueError("MCP response body is empty")
        values: list[Any] = []
        if text.startswith("{"):
            values.append(json.loads(text))
        else:
            for event in re.split(r"\r?\n\r?\n", text):
                data_lines = [
                    line[5:].strip()
                    for line in event.splitlines()
                    if line.startswith("data:") and line[5:].strip()
                ]
                if data_lines:
                    values.append(json.loads("\n".join(data_lines)))
        if not values:
            raise ValueError("MCP response contains no JSON message")
        messages: list[Mapping[str, Any]] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("MCP response message must be a JSON object")
            messages.append(value)
        return tuple(messages)

    @staticmethod
    def _decode_mcp_response(raw: bytes, *, expected_id: object) -> Mapping[str, Any]:
        for message in MaintainerCoordinator._decode_mcp_messages(raw):
            if message.get("id") == expected_id:
                return message
        raise ValueError("MCP response identity does not match request")

    @staticmethod
    def _tooling_environment_probe(
        failure_detail: str | None,
        descriptor: Mapping[str, Any] | None,
        *,
        execute: bool,
    ) -> dict[str, Any]:
        detail = str(failure_detail or "tooling prerequisite unavailable").strip()
        identity_match = re.search(
            r"\b(?:mcp[-_.a-z0-9]*|tool(?:ing)?(?: gateway)?)\b",
            detail,
            re.I,
        )
        endpoint_match = re.search(
            r"""(?:https?://[^\s'"<>]+|(?:127\.0\.0\.1|localhost):\d+)""",
            detail,
        )
        extracted_identity = identity_match.group(0) if identity_match else None
        extracted_endpoint = (
            endpoint_match.group(0).rstrip(".,);]") if endpoint_match else None
        )
        if descriptor is None:
            return {
                "available": False,
                "probe_supported": False,
                "descriptor": None,
                "dependency_identity": extracted_identity,
                "endpoint": extracted_endpoint,
                "auth_profile": None,
                "credential_resolved": False,
                "capability_succeeded": False,
                "required_tools": [],
                "matched_tools": [],
                "missing_tools": [],
                "tool_count": None,
                "catalog_sha256": None,
                "protocol_version": None,
                "session_mode": None,
                "session_id_sha256": None,
                "cleanup_attempted": False,
                "cleanup_succeeded": None,
                "evidence": None,
                "error": (
                    "no worker-owned tooling probe descriptor was supplied; "
                    "the incident remains suspended"
                ),
            }
        try:
            parsed = ToolingProbeDescriptor.from_mapping(descriptor)
        except (TypeError, ValueError) as exc:
            return {
                "available": False,
                "probe_supported": False,
                "descriptor": dict(descriptor) if isinstance(descriptor, Mapping) else None,
                "dependency_identity": extracted_identity,
                "endpoint": extracted_endpoint,
                "auth_profile": None,
                "credential_resolved": False,
                "capability_succeeded": False,
                "required_tools": [],
                "matched_tools": [],
                "missing_tools": [],
                "tool_count": None,
                "catalog_sha256": None,
                "protocol_version": None,
                "session_mode": None,
                "session_id_sha256": None,
                "cleanup_attempted": False,
                "cleanup_succeeded": None,
                "evidence": None,
                "error": f"invalid tooling probe descriptor: {exc}",
            }
        normalized = parsed.to_dict()
        base = {
            "descriptor": normalized,
            "dependency_identity": parsed.dependency,
            "endpoint": parsed.endpoint,
            "auth_profile": parsed.auth_profile,
            "required_tools": list(parsed.required_tools),
        }
        if not execute:
            return {
                **base,
                "available": False,
                "probe_supported": True,
                "credential_resolved": False,
                "capability_succeeded": False,
                "matched_tools": [],
                "missing_tools": list(parsed.required_tools),
                "tool_count": None,
                "catalog_sha256": None,
                "protocol_version": None,
                "session_mode": None,
                "session_id_sha256": None,
                "cleanup_attempted": False,
                "cleanup_succeeded": None,
                "evidence": {
                    "endpoint": parsed.endpoint,
                    "transport": "mcp_streamable_http",
                    "method": parsed.method,
                    "auth_profile": parsed.auth_profile,
                    "required_tools": list(parsed.required_tools),
                    "timeout_seconds": _TOOLING_PROBE_TIMEOUT_SECONDS,
                    "executed": False,
                },
                "error": "failed operation is authoritative; readiness probe deferred",
            }

        opener = urllib.request.build_opener(_NoRedirectHandler())
        steps: list[dict[str, Any]] = []
        bearer: str | None = None
        credential_resolved = False
        protocol_version: str | None = None
        session_id: str | None = None
        session_mode = "stateless"
        session_id_sha256: str | None = None
        matched_tools: list[str] = []
        missing_tools = list(parsed.required_tools)
        tool_count: int | None = None
        catalog_sha256: str | None = None
        capability_succeeded = False
        cleanup_attempted = False
        cleanup_succeeded: bool | None = None
        cleanup_error: str | None = None
        error: str | None = None

        def request_headers(*, session: str | None = None) -> dict[str, str]:
            headers = {
                "Content-Type": "application/json",
                "Accept": _TOOLING_PROBE_ACCEPT,
                "User-Agent": "playwright-auto-cdpa-maintainers-health/1",
                "MCP-Protocol-Version": protocol_version or _TOOLING_PROBE_PROTOCOL_VERSION,
            }
            if bearer is not None:
                headers["Authorization"] = f"Bearer {bearer}"
            if session is not None:
                headers["MCP-Session-Id"] = session
            return headers

        def post_jsonrpc(
            stage: str,
            payload: Mapping[str, Any],
            *,
            expected_id: object | None,
            session: str | None = None,
            allow_empty: bool = False,
        ) -> tuple[Mapping[str, Any] | None, Mapping[str, str]]:
            request = urllib.request.Request(
                parsed.endpoint,
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                method="POST",
                headers=request_headers(session=session),
            )
            response: Any = None
            step: dict[str, Any] = {
                "stage": stage,
                "http_method": "POST",
                "rpc_method": payload.get("method"),
                "status": None,
                "content_type": None,
                "response_bytes": 0,
                "error": None,
            }
            try:
                response = opener.open(request, timeout=_TOOLING_PROBE_TIMEOUT_SECONDS)
                status = int(getattr(response, "status", response.getcode()))
                step["status"] = status
                if not (200 <= status < 300):
                    raise ValueError(f"MCP {stage} returned HTTP {status}")
                content_type = str(response.headers.get("Content-Type") or "")
                step["content_type"] = content_type
                raw = response.read(_TOOLING_PROBE_MAX_RESPONSE_BYTES + 1)
                step["response_bytes"] = len(raw)
                if len(raw) > _TOOLING_PROBE_MAX_RESPONSE_BYTES:
                    raise ValueError("MCP response exceeds bounded size")
                if not raw and allow_empty:
                    return None, dict(response.headers.items())
                lowered = content_type.casefold()
                if not (
                    lowered.startswith("application/json")
                    or lowered.startswith("text/event-stream")
                ):
                    raise ValueError("MCP response must be JSON or text/event-stream")
                if expected_id is None:
                    messages = MaintainerCoordinator._decode_mcp_messages(raw)
                    for message in messages:
                        if message.get("jsonrpc") != "2.0":
                            raise ValueError(
                                f"MCP {stage} non-empty response jsonrpc must be 2.0"
                            )
                        if message.get("error") is not None:
                            raise ValueError(
                                f"MCP {stage} returned a JSON-RPC error"
                            )
                    raise ValueError(
                        f"MCP {stage} returned an unexpected non-empty response"
                    )
                value = MaintainerCoordinator._decode_mcp_response(
                    raw,
                    expected_id=expected_id,
                )
                if value.get("jsonrpc") != "2.0":
                    raise ValueError("MCP response jsonrpc must be 2.0")
                if value.get("error") is not None:
                    raise ValueError(f"MCP {stage} returned a JSON-RPC error")
                return value, dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                step["status"] = int(exc.code)
                step["content_type"] = str(exc.headers.get("Content-Type") or "")
                step["error"] = f"HTTP {exc.code}"
                raise RuntimeError(f"MCP {stage} HTTP {exc.code}") from exc
            except Exception as exc:
                step["error"] = sanitize_exception(exc)
                raise
            finally:
                steps.append(step)
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

        try:
            bearer = MaintainerCoordinator._resolve_tooling_bearer(parsed.auth_profile)
            credential_resolved = parsed.auth_profile == "none" or bearer is not None
            initialize, headers = post_jsonrpc(
                "initialize",
                {
                    "jsonrpc": "2.0",
                    "id": _TOOLING_PROBE_INITIALIZE_ID,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _TOOLING_PROBE_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "playwright-auto-cdpa-maintainers",
                            "version": "1.0.0",
                        },
                    },
                },
                expected_id=_TOOLING_PROBE_INITIALIZE_ID,
            )
            result = initialize.get("result") if isinstance(initialize, Mapping) else None
            if not isinstance(result, Mapping) or not isinstance(result.get("serverInfo"), Mapping):
                raise ValueError("MCP initialize did not return serverInfo")
            protocol_version = str(result.get("protocolVersion") or "").strip()
            if not protocol_version:
                raise ValueError("MCP initialize did not return protocolVersion")
            session_id = next(
                (
                    str(value).strip()
                    for name, value in headers.items()
                    if name.casefold() == "mcp-session-id" and str(value).strip()
                ),
                None,
            )
            if session_id is not None:
                if len(session_id) > 256:
                    raise ValueError("MCP session ID exceeds bounded length")
                session_mode = "stateful"
                session_id_sha256 = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
                post_jsonrpc(
                    "initialized",
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    expected_id=None,
                    session=session_id,
                    allow_empty=True,
                )
            listed, _headers = post_jsonrpc(
                "tools_list",
                {
                    "jsonrpc": "2.0",
                    "id": _TOOLING_PROBE_LIST_ID,
                    "method": parsed.method,
                    "params": {},
                },
                expected_id=_TOOLING_PROBE_LIST_ID,
                session=session_id,
            )
            list_result = listed.get("result") if isinstance(listed, Mapping) else None
            tools = list_result.get("tools") if isinstance(list_result, Mapping) else None
            if not isinstance(tools, list) or not all(isinstance(item, Mapping) for item in tools):
                raise ValueError("MCP tools/list did not return a tools array")
            names = sorted(
                {
                    str(item.get("name") or "").strip()
                    for item in tools
                    if str(item.get("name") or "").strip()
                }
            )
            tool_count = len(names)
            catalog_sha256 = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
            required = set(parsed.required_tools)
            matched_tools = sorted(required.intersection(names))
            missing_tools = sorted(required.difference(names))
            if missing_tools:
                raise ValueError(
                    "MCP tools/list is missing required capabilities: "
                    + ", ".join(missing_tools)
                )
            capability_succeeded = True
        except Exception as exc:
            error = sanitize_exception(exc)
        finally:
            if session_id is not None:
                cleanup_attempted = True
                response: Any = None
                step = {
                    "stage": "session_delete",
                    "http_method": "DELETE",
                    "rpc_method": None,
                    "status": None,
                    "content_type": None,
                    "response_bytes": 0,
                    "error": None,
                }
                try:
                    request = urllib.request.Request(
                        parsed.endpoint,
                        method="DELETE",
                        headers=request_headers(session=session_id),
                    )
                    response = opener.open(
                        request,
                        timeout=_TOOLING_PROBE_TIMEOUT_SECONDS,
                    )
                    status = int(getattr(response, "status", response.getcode()))
                    step["status"] = status
                    cleanup_succeeded = 200 <= status < 300
                    if not cleanup_succeeded:
                        raise ValueError(f"MCP session delete returned HTTP {status}")
                except urllib.error.HTTPError as exc:
                    step["status"] = int(exc.code)
                    cleanup_error = f"HTTP {exc.code}"
                    step["error"] = cleanup_error
                    cleanup_succeeded = False
                except Exception as exc:
                    cleanup_error = sanitize_exception(exc)
                    step["error"] = cleanup_error
                    cleanup_succeeded = False
                finally:
                    steps.append(step)
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass

        if cleanup_attempted and cleanup_succeeded is not True:
            capability_succeeded = False
            if error is None:
                error = f"RuntimeError: MCP session cleanup failed: {cleanup_error or 'unknown error'}"

        evidence = {
            "endpoint": parsed.endpoint,
            "transport": "mcp_streamable_http",
            "method": parsed.method,
            "auth_profile": parsed.auth_profile,
            "credential_resolved": credential_resolved,
            "timeout_seconds": _TOOLING_PROBE_TIMEOUT_SECONDS,
            "protocol_version": protocol_version,
            "session_mode": session_mode,
            "session_id_sha256": session_id_sha256,
            "required_tools": list(parsed.required_tools),
            "matched_tools": matched_tools,
            "missing_tools": missing_tools,
            "tool_count": tool_count,
            "catalog_sha256": catalog_sha256,
            "cleanup_attempted": cleanup_attempted,
            "cleanup_succeeded": cleanup_succeeded,
            "cleanup_error": cleanup_error,
            "steps": steps,
            "error": error,
            "executed": True,
        }
        return {
            **base,
            "available": capability_succeeded,
            "probe_supported": True,
            "credential_resolved": credential_resolved,
            "capability_succeeded": capability_succeeded,
            "matched_tools": matched_tools,
            "missing_tools": missing_tools,
            "tool_count": tool_count,
            "catalog_sha256": catalog_sha256,
            "protocol_version": protocol_version,
            "session_mode": session_mode,
            "session_id_sha256": session_id_sha256,
            "cleanup_attempted": cleanup_attempted,
            "cleanup_succeeded": cleanup_succeeded,
            "evidence": evidence,
            "error": error,
        }

    def _filesystem_environment_probe(self) -> dict[str, Any]:
        probe_path = self.config.plans_root / ".cdpa-maintainers-filesystem-probe"
        descriptor: int | None = None
        error: str | None = None
        wrote = False
        try:
            self.config.plans_root.mkdir(parents=True, exist_ok=True)
            probe_path.unlink(missing_ok=True)
            descriptor = os.open(
                probe_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            payload = b"cdpa-maintainers-filesystem-health\n"
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError(f"short filesystem probe write: {written}/{len(payload)}")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            wrote = True
            probe_path.unlink()
            fsync_parent_directory(probe_path)
        except Exception as exc:
            error = sanitize_exception(exc)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                probe_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                if error is None:
                    error = sanitize_exception(cleanup_exc)
        return {
            "available": error is None and wrote and not probe_path.exists(),
            "probe_path": str(probe_path),
            "write_fsync_delete": error is None and wrote and not probe_path.exists(),
            "error": error,
        }

    def _environment_signature(
        self,
        browser_context: Any,
        prerequisite: str,
        *,
        failure_observed: bool = False,
        failure_detail: str | None = None,
        browser_snapshot: Mapping[str, Any] | None = None,
        tooling_probe_descriptor: Mapping[str, Any] | None = None,
        tooling_probe_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        browser = (
            dict(browser_snapshot)
            if browser_snapshot is not None
            else self._browser_environment_probe(browser_context)
        )
        repository_available = self.config.repository_root.is_dir()
        plans_available = self.config.plans_root.is_dir() and os.access(
            self.config.plans_root, os.R_OK | os.W_OK | os.X_OK
        )
        network_probe: dict[str, Any] | None = None
        tooling_probe: dict[str, Any] | None = None
        filesystem_probe: dict[str, Any] | None = None
        if prerequisite == "browser_cdp":
            health_probe = "browser_cdp_live_context"
            probe_available = False
        elif prerequisite == "network":
            health_probe = "network_exact_endpoint_head"
            network_probe = self._network_environment_probe(
                browser["browser_pages"], failure_detail=failure_detail
            )
            probe_available = bool(network_probe["available"])
        elif prerequisite == "tooling":
            health_probe = "tooling_exact_capability_list"
            tooling_probe = (
                dict(tooling_probe_result)
                if isinstance(tooling_probe_result, Mapping)
                else self._tooling_environment_probe(
                    failure_detail,
                    tooling_probe_descriptor,
                    execute=not failure_observed,
                )
            )
            probe_available = bool(tooling_probe["available"])
        elif prerequisite == "filesystem":
            health_probe = "filesystem_write_fsync_delete"
            filesystem_probe = self._filesystem_environment_probe()
            probe_available = bool(filesystem_probe["available"])
        else:
            raise ValueError(f"unsupported environment prerequisite {prerequisite!r}")
        available = False if failure_observed else probe_available
        return {
            "version": 3,
            "prerequisite": prerequisite,
            "health_probe": health_probe,
            "available": available,
            "probe_available": probe_available,
            "failure_observed": failure_observed,
            "browser_available": bool(browser["available"]),
            "browser_is_connected": browser["browser_is_connected"],
            "browser_pages": browser["browser_pages"],
            "browser_error": browser["browser_error"],
            "browser_live_operation": None,
            "browser_live_succeeded": None,
            "browser_live_error": None,
            "repository_available": repository_available,
            "plans_available": plans_available,
            "network_available": (
                network_probe.get("available") if network_probe is not None else None
            ),
            "network_evidence": (
                network_probe.get("evidence") if network_probe is not None else None
            ),
            "network_errors": (
                network_probe.get("errors") if network_probe is not None else []
            ),
            "tooling_probe_supported": (
                tooling_probe.get("probe_supported") if tooling_probe is not None else None
            ),
            "tooling_dependency_identity": (
                tooling_probe.get("dependency_identity") if tooling_probe is not None else None
            ),
            "tooling_endpoint": (
                tooling_probe.get("endpoint") if tooling_probe is not None else None
            ),
            "tooling_probe_descriptor": (
                tooling_probe.get("descriptor") if tooling_probe is not None else None
            ),
            "tooling_capability_succeeded": (
                tooling_probe.get("capability_succeeded") if tooling_probe is not None else None
            ),
            "tooling_tool_count": (
                tooling_probe.get("tool_count") if tooling_probe is not None else None
            ),
            "tooling_auth_profile": (
                tooling_probe.get("auth_profile") if tooling_probe is not None else None
            ),
            "tooling_credential_resolved": (
                tooling_probe.get("credential_resolved") if tooling_probe is not None else None
            ),
            "tooling_required_tools": (
                tooling_probe.get("required_tools") if tooling_probe is not None else []
            ),
            "tooling_matched_tools": (
                tooling_probe.get("matched_tools") if tooling_probe is not None else []
            ),
            "tooling_missing_tools": (
                tooling_probe.get("missing_tools") if tooling_probe is not None else []
            ),
            "tooling_catalog_sha256": (
                tooling_probe.get("catalog_sha256") if tooling_probe is not None else None
            ),
            "tooling_protocol_version": (
                tooling_probe.get("protocol_version") if tooling_probe is not None else None
            ),
            "tooling_session_mode": (
                tooling_probe.get("session_mode") if tooling_probe is not None else None
            ),
            "tooling_session_id_sha256": (
                tooling_probe.get("session_id_sha256") if tooling_probe is not None else None
            ),
            "tooling_cleanup_attempted": (
                tooling_probe.get("cleanup_attempted") if tooling_probe is not None else False
            ),
            "tooling_cleanup_succeeded": (
                tooling_probe.get("cleanup_succeeded") if tooling_probe is not None else None
            ),
            "tooling_evidence": (
                tooling_probe.get("evidence") if tooling_probe is not None else None
            ),
            "tooling_error": (
                tooling_probe.get("error") if tooling_probe is not None else None
            ),
            "filesystem_write_fsync_delete": (
                filesystem_probe.get("write_fsync_delete")
                if filesystem_probe is not None
                else None
            ),
            "filesystem_error": (
                filesystem_probe.get("error") if filesystem_probe is not None else None
            ),
        }

    async def _environment_signature_async(
        self,
        browser_context: Any,
        prerequisite: str,
        *,
        failure_detail: str | None = None,
        tooling_probe_descriptor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if prerequisite != "browser_cdp":
            browser = self._browser_environment_probe(browser_context)
            return await asyncio.to_thread(
                self._environment_signature,
                None,
                prerequisite,
                failure_detail=failure_detail,
                browser_snapshot=browser,
                tooling_probe_descriptor=tooling_probe_descriptor,
            )
        browser = await self._live_browser_environment_probe(browser_context)
        return {
            "version": 3,
            "prerequisite": prerequisite,
            "health_probe": "browser_cdp_live_context",
            "available": bool(browser["available"]),
            "probe_available": bool(browser["available"]),
            "failure_observed": False,
            "browser_available": bool(browser["available"]),
            "browser_is_connected": browser["browser_is_connected"],
            "browser_pages": browser["browser_pages"],
            "browser_error": browser["browser_error"],
            "browser_live_operation": browser["live_operation"],
            "browser_live_succeeded": browser["live_succeeded"],
            "browser_live_error": browser["live_error"],
            "repository_available": self.config.repository_root.is_dir(),
            "plans_available": self.config.plans_root.is_dir()
            and os.access(self.config.plans_root, os.R_OK | os.W_OK | os.X_OK),
            "network_available": None,
            "network_evidence": None,
            "network_errors": [],
            "tooling_probe_supported": None,
            "tooling_dependency_identity": None,
            "tooling_endpoint": None,
            "tooling_probe_descriptor": None,
            "tooling_capability_succeeded": None,
            "tooling_tool_count": None,
            "tooling_auth_profile": None,
            "tooling_credential_resolved": None,
            "tooling_required_tools": [],
            "tooling_matched_tools": [],
            "tooling_missing_tools": [],
            "tooling_catalog_sha256": None,
            "tooling_protocol_version": None,
            "tooling_session_mode": None,
            "tooling_session_id_sha256": None,
            "tooling_cleanup_attempted": False,
            "tooling_cleanup_succeeded": None,
            "tooling_evidence": None,
            "tooling_error": None,
            "filesystem_write_fsync_delete": None,
            "filesystem_error": None,
        }

    def _record_environment_failure(
        self,
        path: Path,
        *,
        incident_id: str,
        error: BaseException,
        browser_context: Any,
        global_state: dict[str, Any],
    ) -> bool:
        prerequisite = self._environment_prerequisite(error)
        if prerequisite is None:
            return False
        raw_detail = f"{type(error).__name__}: {error}"
        detail = sanitize_exception(error)
        tooling_probe_descriptor = (
            self._tooling_probe_descriptor(error) if prerequisite == "tooling" else None
        )
        tooling_probe_result = (
            self._tooling_probe_result(error) if prerequisite == "tooling" else None
        )
        signature = self._environment_signature(
            browser_context,
            prerequisite,
            failure_observed=True,
            failure_detail=raw_detail,
            tooling_probe_descriptor=tooling_probe_descriptor,
            tooling_probe_result=tooling_probe_result,
        )
        outcome = {"suspended": False}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            incident = self._incident(current, incident_id)
            if incident is None:
                return current
            attempts = int(incident.get("environment_attempts") or 0) + 1
            at = utc_now()
            incident["environment_attempts"] = attempts
            incident["environment_prerequisite"] = prerequisite
            incident["environment_probe_descriptor"] = tooling_probe_descriptor
            incident["environment_signature"] = signature
            incident["environment_last_error"] = detail
            incident["environment_last_attempt_at"] = at
            incident.setdefault("environment_evidence", []).append(
                {
                    "attempt": attempts,
                    "at": at,
                    "error": detail,
                    "signature": signature,
                }
            )
            incident["environment_evidence"] = incident["environment_evidence"][-10:]
            incident["last_error"] = detail
            incident["updated_at"] = at
            if attempts >= 3:
                incident["state"] = "SUSPENDED"
                incident["environment_suspended_at"] = at
                current["maintenance"]["active_incident_id"] = None
                outcome["suspended"] = True
            else:
                incident["state"] = "OPEN"
                current["maintenance"]["active_incident_id"] = incident_id
            return current

        self.store.update_maintenance(path, mutate)
        global_state["last_error"] = detail
        if outcome["suspended"]:
            active = global_state.get("active_incident")
            if isinstance(active, Mapping) and active.get("incident_id") == incident_id:
                global_state["active_incident"] = None
        self.state_store.save(global_state)
        return True

    async def _resume_suspended_environments(
        self,
        loaded: Sequence[tuple[Path, dict[str, Any]]],
        browser_context: Any,
    ) -> bool:
        for path, state in loaded:
            maintenance = state.get("maintenance")
            if not isinstance(maintenance, Mapping):
                continue
            if _maintenance_operational_key(state) is None:
                continue
            for incident in maintenance.get("incidents") or []:
                if not isinstance(incident, Mapping) or incident.get("state") != "SUSPENDED":
                    continue
                prerequisite = str(incident.get("environment_prerequisite") or "")
                if not prerequisite:
                    continue
                previous = incident.get("environment_signature")
                current_signature = await self._environment_signature_async(
                    browser_context,
                    prerequisite,
                    failure_detail=str(incident.get("environment_last_error") or ""),
                    tooling_probe_descriptor=(
                        incident.get("environment_probe_descriptor")
                        if isinstance(incident.get("environment_probe_descriptor"), Mapping)
                        else None
                    ),
                )
                if not current_signature.get("available"):
                    if incident.get("environment_last_probe_signature") != current_signature:
                        checked_at = utc_now()

                        def record_failed_probe(current: dict[str, Any]) -> dict[str, Any]:
                            current_incident = self._incident(
                                current,
                                str(incident.get("incident_id") or ""),
                            )
                            if (
                                current_incident is None
                                or current_incident.get("state") != "SUSPENDED"
                            ):
                                return current
                            current_incident["environment_last_probe_at"] = checked_at
                            current_incident["environment_last_probe_signature"] = current_signature
                            checks = current_incident.setdefault("environment_probe_checks", [])
                            checks.append(
                                {
                                    "at": checked_at,
                                    "signature": current_signature,
                                }
                            )
                            current_incident["environment_probe_checks"] = checks[-10:]
                            current_incident["updated_at"] = checked_at
                            return current

                        self.store.update_maintenance(path, record_failed_probe)
                    continue
                # SUSPENDED plus durable failure evidence proves the failed side
                # of the transition. The current prerequisite-specific probe
                # proves restoration; browser page-set differences are irrelevant.
                if not isinstance(previous, Mapping) or not incident.get(
                    "environment_last_error"
                ):
                    continue
                incident_id = str(incident.get("incident_id") or "")
                resumed_at = utc_now()

                def resume(current: dict[str, Any]) -> dict[str, Any]:
                    current_incident = self._incident(current, incident_id)
                    if current_incident is None or current_incident.get("state") != "SUSPENDED":
                        return current
                    current_incident["state"] = "OPEN"
                    current_incident["environment_resumed_at"] = resumed_at
                    current_incident["environment_last_probe_at"] = resumed_at
                    current_incident["environment_last_probe_signature"] = current_signature
                    current_incident["environment_resume_signature"] = current_signature
                    current_incident["last_error"] = None
                    current_incident["updated_at"] = resumed_at
                    current["maintenance"]["active_incident_id"] = incident_id
                    return current

                self.store.update_maintenance(path, resume)
                return True
        return False

    def _finalize_repair_lessons(
        self,
        loaded: Sequence[tuple[Path, dict[str, Any]]],
    ) -> bool:
        tasks_by_id = {
            str(state.get("task_id") or ""): state
            for _path, state in loaded
            if str(state.get("task_id") or "")
        }
        learning_path = self.config.repository_root / "LEARNING.md"
        if not learning_path.is_file():
            return False
        for path, state in loaded:
            maintenance = state.get("maintenance")
            if not isinstance(maintenance, Mapping):
                continue
            for incident in maintenance.get("incidents") or []:
                if not isinstance(incident, Mapping):
                    continue
                lesson = str(incident.get("pending_lesson") or "").strip()
                repair_task_id = str(incident.get("repair_task_id") or "").strip()
                if (
                    incident.get("state") != "RESOLVED"
                    or not lesson
                    or not repair_task_id
                    or incident.get("lesson_finalized_at")
                ):
                    continue
                repair_task = tasks_by_id.get(repair_task_id)
                if (
                    repair_task is None
                    or str(repair_task.get("status") or "").upper() != "DONE"
                ):
                    continue
                disposition = str(incident.get("repair_disposition") or "").upper()
                if disposition == "HOLD_FOR_REPAIR":
                    repair_wait = state.get("repair_wait")
                    if (
                        not isinstance(repair_wait, Mapping)
                        or repair_wait.get("repair_task_id") != repair_task_id
                        or repair_wait.get("state") != "RELEASED"
                    ):
                        continue
                decision_value = incident.get("decision")
                if not isinstance(decision_value, Mapping):
                    continue
                decision = self._decision_from_mapping(decision_value)
                if decision.lesson != lesson:
                    decision = MaintenanceDecision(
                        action=decision.action,
                        reason=decision.reason,
                        role=decision.role,
                        lesson=lesson,
                        replacement=decision.replacement,
                        recovery=decision.recovery,
                        repair=decision.repair,
                        version=decision.version,
                    )
                appended = append_resolved_lesson(
                    learning_path,
                    {"state": "RESOLVED"},
                    decision,
                )
                finalized_at = utc_now()
                incident_id = str(incident.get("incident_id") or "")

                def finalize(current: dict[str, Any]) -> dict[str, Any]:
                    current_incident = self._incident(current, incident_id)
                    if current_incident is None:
                        raise RuntimeError(
                            "repair lesson incident disappeared before finalization"
                        )
                    if current_incident.get("lesson_finalized_at"):
                        return current
                    if str(current_incident.get("pending_lesson") or "").strip() != lesson:
                        raise RuntimeError(
                            "repair lesson changed before worker finalization"
                        )
                    current_incident["pending_lesson"] = None
                    current_incident["lesson_finalized_at"] = finalized_at
                    current_incident["lesson_append_state"] = (
                        "appended" if appended else "deduplicated"
                    )
                    current_incident["lesson_repair_task_id"] = repair_task_id
                    current_incident["updated_at"] = finalized_at
                    return current

                self.store.update_maintenance(path, finalize)
                return True
        return False

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
            decision = self._decision_from_mapping(decision_value)
            learning_path = self.config.repository_root / "LEARNING.md"
            if decision.repair is not None:
                incident["pending_lesson"] = decision.lesson
            elif learning_path.is_file():
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
            or not str(control.get("maintenance_request_id") or "").startswith(
                str(incident.get("request_id") or "")
            )
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

    def _queue_next_v2_control(
        self,
        path: Path,
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
        decision: MaintenanceDecision,
        *,
        skip_recovery: bool,
    ) -> bool:
        if decision.version != 2:
            return False
        outcome = {"queued": False}

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            current_incident = self._incident(current, str(incident["incident_id"]))
            if current_incident is None:
                raise RuntimeError("maintenance incident disappeared before follow-up")
            if current_incident.get("state") not in {"OPEN", "RUNNING"}:
                return current
            current_index = int(current_incident.get("command_index", -1))
            next_index = len(decision.recovery) if skip_recovery else current_index + 1
            control_action: str | None = None
            control_role: str | None = None
            control_reason: str | None = None
            control_request_id: str | None = None
            repair_request: RepairRequest | None = None
            if next_index < len(decision.recovery):
                step = decision.recovery[next_index]
                control_action = _CONTROL_ACTIONS[step.action]
                control_role = step.role
                control_reason = step.reason
                control_request_id = f"{current_incident['request_id']}-step{next_index + 1}"
                current_incident["command_index"] = next_index
            elif decision.repair is not None and not current_incident.get(
                "repair_control_queued"
            ):
                disposition = str(decision.repair.get("disposition") or "")
                currently_blocked = _maintenance_operational_key(current) is not None
                if disposition == "CONTINUE_IN_PARALLEL" and currently_blocked:
                    raise ValueError(
                        "CONTINUE_IN_PARALLEL requires the affected task to be operational"
                    )
                if disposition == "HOLD_FOR_REPAIR" and not currently_blocked:
                    raise ValueError(
                        "HOLD_FOR_REPAIR requires an unresolved operational block"
                    )
                control_action = "create_repair_task"
                control_reason = str(decision.repair.get("reason") or "")
                control_request_id = f"{current_incident['request_id']}-repair"
                repair_request = self._repair_request(current, current_incident, decision)
                current_incident["repair_control_queued"] = True
            if control_action is None or control_request_id is None:
                return current
            control = self.store._queue_control(
                current,
                control_action,
                role=control_role,
                reason=control_reason,
                origin="maintainers",
                maintenance_incident_id=str(current_incident["incident_id"]),
                maintenance_request_id=control_request_id,
                repair=repair_request,
            )
            current_incident["control_id"] = control["control_id"]
            current_incident.setdefault("control_ids", []).append(control["control_id"])
            current_incident["updated_at"] = utc_now()
            outcome["queued"] = True
            return current

        self.store.update_maintenance(path, mutate)
        return bool(outcome["queued"])

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
        decision_object = self._decision_from_mapping(decision)
        if decision_object.version == 2 and current_key is None:
            control_id = incident.get("control_id")
            current_control = next(
                (
                    item
                    for item in state.get("controls") or []
                    if isinstance(item, Mapping)
                    and item.get("control_id") == control_id
                ),
                None,
            )
            if current_control is None or current_control.get("status") in {
                "requested",
                "cleanup_pending",
            }:
                return False
            if current_control.get("action") == "create_repair_task":
                if current_control.get("status") != "applied":
                    incident["state"] = "ESCALATED"
                    incident["last_error"] = str(
                        current_control.get("result") or "repair task creation failed"
                    )
                    state["maintenance"]["active_incident_id"] = None
                    self.store.save_maintenance(path, state)
                    self._clear_global_active(str(incident["incident_id"]))
                    return True
                result = current_control.get("result")
                if isinstance(result, Mapping):
                    incident["repair_task_id"] = result.get("repair_task_id")
                    incident["repair_team"] = result.get("repair_team")
                    incident["repair_disposition"] = result.get("disposition")
                self._resolve_incident(state, incident)
                self.store.save_maintenance(path, state)
                self._clear_global_active(str(incident["incident_id"]))
                return True
            try:
                if self._queue_next_v2_control(
                    path,
                    state,
                    incident,
                    decision_object,
                    skip_recovery=True,
                ):
                    return True
            except Exception as exc:
                incident["state"] = "ESCALATED"
                incident["last_error"] = sanitize_exception(exc)
                incident["updated_at"] = utc_now()
                state["maintenance"]["active_incident_id"] = None
                self.store.save_maintenance(path, state)
                self._clear_global_active(str(incident["incident_id"]))
                return True
            self._resolve_incident(state, incident)
            self.store.save_maintenance(path, state)
            self._clear_global_active(str(incident["incident_id"]))
            return True
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
                incident["last_error"] = sanitize_exception(exc)
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
                and (
                    decision_object.version == 2
                    or self._control_matches_incident(item, incident)
                )
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
                incident["last_error"] = sanitize_exception(exc)
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
        if decision_object.version == 2 and control is not None:
            if control.get("status") in {"requested", "cleanup_pending"}:
                return False
            if control.get("action") == "create_repair_task":
                if control.get("status") == "applied":
                    result = control.get("result")
                    if isinstance(result, Mapping):
                        incident["repair_task_id"] = result.get("repair_task_id")
                        incident["repair_team"] = result.get("repair_team")
                        incident["repair_disposition"] = result.get("disposition")
                    self._resolve_incident(state, incident)
                    self.store.save_maintenance(path, state)
                    self._clear_global_active(str(incident["incident_id"]))
                    return True
            else:
                try:
                    if self._queue_next_v2_control(
                        path,
                        state,
                        incident,
                        decision_object,
                        skip_recovery=False,
                    ):
                        return True
                except Exception as exc:
                    incident["state"] = "ESCALATED"
                    incident["last_error"] = sanitize_exception(exc)
                    incident["updated_at"] = utc_now()
                    state["maintenance"]["active_incident_id"] = None
                    self.store.save_maintenance(path, state)
                    self._clear_global_active(str(incident["incident_id"]))
                    return True
                if control.get("status") == "applied" and current_key != applied_key:
                    self._resolve_incident(state, incident)
                    self.store.save_maintenance(path, state)
                    self._clear_global_active(str(incident["incident_id"]))
                    return True
            incident["state"] = "ESCALATED"
            state["maintenance"]["suppressed_operational_key"] = current_key
            incident["last_error"] = str(
                control.get("result") or "bounded maintenance recovery exhausted"
            )
            incident["updated_at"] = utc_now()
            state["maintenance"]["active_incident_id"] = None
            self.store.save_maintenance(path, state)
            self._clear_global_active(str(incident["incident_id"]))
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
                "page_id": record.get("page_id"),
                "page_url": record.get("page_url"),
                "conversation_generation": record.get("conversation_generation"),
                "constructor_sent_generation": record.get("constructor_sent_generation"),
                "last_seen_at": record.get("last_seen_at"),
                "last_activity_at": record.get("last_activity_at"),
                "last_error": record.get("last_error"),
            }
            for role, record in (task.get("roles") or {}).items()
            if isinstance(record, Mapping)
        }
        receipt = (
            active_hop.get("receipt")
            if isinstance(active_hop, Mapping)
            and isinstance(active_hop.get("receipt"), Mapping)
            else None
        )
        controls = [
            {
                "control_id": item.get("control_id"),
                "action": item.get("action"),
                "role": item.get("role"),
                "origin": item.get("origin"),
                "status": item.get("status"),
                "command_state": item.get("command_state"),
                "requested_at": item.get("requested_at"),
                "applied_at": item.get("applied_at"),
                "reason": item.get("reason"),
                "result": item.get("result"),
            }
            for item in (task.get("controls") or [])[-10:]
            if isinstance(item, Mapping)
        ]
        recent_errors = [
            item
            for item in (task.get("errors") or [])[-10:]
            if isinstance(item, (str, Mapping))
        ]
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
            "durable_send_boundary": {
                "hop_id": active_hop.get("hop_id") if isinstance(active_hop, Mapping) else None,
                "request_id": active_hop.get("request_id") if isinstance(active_hop, Mapping) else None,
                "state": active_hop.get("state") if isinstance(active_hop, Mapping) else None,
                "turn": active_hop.get("turn") if isinstance(active_hop, Mapping) else None,
                "prompt_sha256": active_hop.get("prompt_sha256") if isinstance(active_hop, Mapping) else None,
                "conversation_url": active_hop.get("conversation_url") if isinstance(active_hop, Mapping) else None,
                "conversation_generation": active_hop.get("conversation_generation") if isinstance(active_hop, Mapping) else None,
                "receipt": receipt,
            },
            "roles": role_status,
            "runtime_availability": {
                "roles_online": {
                    role: bool(record.get("online"))
                    for role, record in role_status.items()
                },
                "repository_available": self.config.repository_root.is_dir(),
                "plans_available": self.config.plans_root.is_dir(),
            },
            "cleanup": task.get("cleanup"),
            "dependencies": {
                "parents": parents,
                "children": children,
                "waiting": task.get("waiting"),
                "depends_on_task_ids": task.get("depends_on_task_ids"),
            },
            "queue": task.get("queue"),
            "priority": task.get("priority"),
            "repair": task.get("repair"),
            "repair_wait": task.get("repair_wait"),
            "repair_links": task.get("repair_links"),
            "controls": controls,
            "recent_errors": recent_errors,
            "recent_route_timeline": [
                item
                for item in (task.get("route_timeline") or [])[-10:]
                if isinstance(item, Mapping)
            ],
            "recent_dependency_events": [
                item
                for item in (task.get("dependency_events") or [])[-10:]
                if isinstance(item, Mapping)
            ],
            "recent_maintenance_incidents": [
                projected
                for item in ((task.get("maintenance") or {}).get("incidents") or [])[-5:]
                if isinstance(item, Mapping)
                if (projected := project_maintenance_incident(item)) is not None
            ] if isinstance(task.get("maintenance"), Mapping) else [],
        }
        snapshot = sanitize_value(snapshot)
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
            "Return a non-empty Markdown maintenance report followed by exactly one terminal "
            "fenced JSON v2 decision with exact keys version, recovery, repair, lesson. "
            "Use version=2; recovery is an ordered list of zero to three exact objects with "
            "keys action, reason, role; repair is null or one exact bounded repair proposal."
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
            if (
                decision.version == 2
                and decision.repair is not None
                and not decision.recovery
                and str(decision.repair.get("disposition") or "")
                == "CONTINUE_IN_PARALLEL"
            ):
                raise ValueError(
                    "CONTINUE_IN_PARALLEL requires recovery to make the affected task operational first"
                )
            control: Mapping[str, Any] | None = None
            if decision.action not in {"WAIT", "REPLACE_TASK"}:
                repair_request: RepairRequest | None = None
                maintenance_control_request_id = request_id
                control_action = decision.action
                control_role = decision.role
                control_reason = decision.reason
                if decision.version == 2:
                    if decision.recovery:
                        step = decision.recovery[0]
                        control_action = step.action
                        control_role = step.role
                        control_reason = step.reason
                        maintenance_control_request_id = f"{request_id}-step1"
                        current_incident["command_index"] = 0
                    else:
                        control_action = "CREATE_REPAIR_TASK"
                        control_role = None
                        control_reason = str((decision.repair or {}).get("reason") or decision.reason)
                        maintenance_control_request_id = f"{request_id}-repair"
                        repair_request = self._repair_request(
                            current, current_incident, decision
                        )
                        current_incident["repair_control_queued"] = True
                control = self.store._queue_control(
                    current,
                    _CONTROL_ACTIONS[control_action],
                    role=control_role,
                    reason=control_reason,
                    origin="maintainers",
                    maintenance_incident_id=incident_id,
                    maintenance_request_id=maintenance_control_request_id,
                    repair=repair_request,
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
                current_incident.setdefault("control_ids", []).append(control["control_id"])
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
        if await self._resume_suspended_environments(loaded, browser_context):
            return True
        if self._finalize_repair_lessons(loaded):
            return True
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
        try:
            await self._require_configured_tooling()
            actions = CDPATabActions(browser_context, self.config)
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
                self._record_environment_failure(
                    path,
                    incident_id=str(incident["incident_id"]),
                    error=exc,
                    browser_context=browser_context,
                    global_state=global_state,
                )
                raise
            if self._record_environment_failure(
                path,
                incident_id=str(incident["incident_id"]),
                error=exc,
                browser_context=browser_context,
                global_state=global_state,
            ):
                return True
            detail = sanitize_exception(exc)
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


def _normalized_lesson_identity(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", text)
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


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
    identity = _normalized_lesson_identity(lesson)
    if not identity:
        raise ValueError("maintenance lesson must contain reusable text")
    path = Path(learning_path).expanduser().resolve()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        current = path.read_text(encoding="utf-8")
        existing_identities = {
            _normalized_lesson_identity(line)
            for line in current.splitlines()
            if _normalized_lesson_identity(line)
        }
        if identity in existing_identities:
            return False
        with path.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(f"- {lesson}\n")
            handle.flush()
            os.fsync(handle.fileno())
        fsync_parent_directory(path)
    return True
