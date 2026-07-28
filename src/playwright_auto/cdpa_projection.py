from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_dependencies import dependency_parent_ids
from .cdpa_independent import is_independent_task
from .cdpa_safety import sanitize_text, sanitize_value
from .cdpa_team import (
    exact_team_ready_waiters,
    exact_team_reuse_eligible,
    exact_team_waiter_order_key,
    is_active_team_owner,
    is_team_availability_barrier,
)

TERMINAL = frozenset({"DONE", "STOPPED"})
PUBLIC_ROLES = frozenset({"PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "AGENT"})
_PUBLIC_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(
    r"(?:/(?:[^/\s'\"\\]+)){2,}|[A-Za-z]:\\(?:[^\s'\"\\]+\\)+[^\s'\"\\]*"
)


def _public_text(value: object, *, max_chars: int | None) -> str:
    raw = str(value or "")
    text = sanitize_text(raw, max_chars=max_chars or max(1, len(raw)))
    urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"__CDPA_PUBLIC_URL_{len(urls) - 1}__"

    text = _PUBLIC_URL.sub(protect_url, text)
    text = _ABSOLUTE_PATH.sub("[PRIVATE_PATH]", text)
    for index, url in enumerate(urls):
        text = text.replace(f"__CDPA_PUBLIC_URL_{index}__", url)
    return text


def _public_role(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    upper = text.upper()
    if upper in PUBLIC_ROLES:
        return upper
    tail = re.split(r"[-_:]", text)[-1].upper()
    return tail if tail in PUBLIC_ROLES else None


_PRIVATE_IDENTITY_KEYS = frozenset(
    {
        "accepted_user_message_id",
        "accepted_user_turn_id",
        "active_request_id",
        "binding_page_id",
        "conversation_id",
        "conversation_url",
        "page_id",
        "page_url",
        "request_id",
        "user_message_id",
        "user_turn_id",
    }
)

_PRIVATE_PUBLIC_KEYS = frozenset(
    {
        "active_request_id",
        "conversation_id",
        "conversation_url",
        "control_repository",
        "cwd",
        "handoff_sha256",
        "ledger_path",
        "manifest_path",
        "page_id",
        "page_url",
        "prompt",
        "prompt_sha256",
        "receipt",
        "receipt_sha256",
        "report_path",
        "repository",
        "request_id",
        "response",
        "response_sha256",
        "root",
        "system_prompt",
        "user_message_id",
        "user_turn_id",
        "workspace",
    }
)


def _public_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key).casefold() not in _PRIVATE_PUBLIC_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        return _public_text(value, max_chars=4000)
    return sanitize_value(value)


def _private_identity_values(value: object) -> tuple[str, ...]:
    identities: set[str] = set()

    def collect(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in _PRIVATE_IDENTITY_KEYS and isinstance(child, str):
                    identity = child.strip()
                    if len(identity) >= 8:
                        identities.add(identity)
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    return tuple(sorted(identities, key=len, reverse=True))


def _redact_private_identities(value: object, identities: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _redact_private_identities(item, identities)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_private_identities(item, identities) for item in value]
    if isinstance(value, str):
        for identity in identities:
            value = value.replace(identity, "[PRIVATE_IDENTITY]")
    return value


def _public_handoff(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text.replace("\\", "/")).name or None


def _public_hop(hop: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hop_id": hop.get("hop_id"),
        "kind": str(hop.get("kind") or "normal"),
        "state": str(hop.get("state") or ""),
        "logical_role": _public_role(hop.get("target_role") or hop.get("physical_role")),
        "source_role": _public_role(hop.get("source_role")),
        "route": _public_role(hop.get("route")),
        "turn": hop.get("turn"),
        "input": _public_text(hop.get("prompt"), max_chars=None),
        "handoff": _public_handoff(hop.get("handoff")),
    }


def _role_inputs(raw: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    latest: dict[str, dict[str, Any]] = {}
    active_hop_id = raw.get("active_hop_id")
    active: dict[str, Any] | None = None
    for hop in raw.get("hops") or []:
        if not isinstance(hop, Mapping):
            continue
        public = _public_hop(hop)
        role = public.get("logical_role")
        if role and (public.get("input") or public.get("handoff")):
            previous = latest.get(role)
            current_order = (int(public.get("turn") or 0), int(public.get("hop_id") or 0))
            previous_order = (
                int((previous or {}).get("turn") or 0),
                int((previous or {}).get("hop_id") or 0),
            )
            if previous is None or current_order >= previous_order:
                latest[role] = public
        if hop.get("hop_id") == active_hop_id:
            active = public
    return latest, active


@dataclass(frozen=True)
class TaskProjection:
    task_id: str
    team: str
    status: str
    surface: str
    active_role: str | None
    updated_at: str
    summary: dict[str, Any]
    detail: dict[str, Any]
    private: dict[str, Any]


def _roles(
    raw: Mapping[str, Any],
    browser_pages: Sequence[Mapping[str, Any]],
    browser_connected: bool | None,
) -> list[dict[str, Any]]:
    pages = {str(page.get("page_id") or ""): page for page in browser_pages}
    source = raw.get("roles") if isinstance(raw.get("roles"), Mapping) else {}
    result: list[dict[str, Any]] = []
    for logical_role, value in source.items():
        if not isinstance(value, Mapping):
            continue
        page_id = str(value.get("page_id") or "") or None
        page = pages.get(page_id or "")
        if browser_connected is False:
            online: bool | None = None
        elif browser_connected is True:
            online = bool((page or {}).get("online"))
        else:
            online = bool((page or {}).get("online", value.get("online")))
        result.append(
            {
                "logical_role": str(logical_role),
                "physical_role": str(value.get("physical_role") or logical_role),
                "status": str(value.get("status") or "unknown"),
                "turn": int(value.get("turn") or 0),
                "online": online,
                "last_error": sanitize_text(value.get("last_error"), max_chars=500) or None,
                "last_activity_at": value.get("last_activity_at"),
            }
        )
    return result


def _timeline(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for field, level in (("route_timeline", "ROUTE"), ("dependency_events", "DEPENDENCY"), ("queue_events", "QUEUE")):
        for index, event in enumerate(raw.get(field) or []):
            if not isinstance(event, Mapping):
                continue
            kind = str(event.get("kind") or event.get("event") or level.casefold())
            source = _public_role(event.get("source_role"))
            destination = _public_role(event.get("route") or event.get("target_role"))
            if level == "ROUTE" and source and destination:
                message = f"{source} → {destination}"
            elif level == "ROUTE" and destination:
                message = f"Route → {destination}"
            else:
                message = _public_text(
                    event.get("message") or event.get("reason") or event.get("error") or kind.replace("_", " "),
                    max_chars=1000,
                )
            items.append(
                {
                    "key": f"{field}:{index}",
                    "at": str(event.get("at") or event.get("updated_at") or event.get("created_at") or ""),
                    "level": level,
                    "kind": kind,
                    "status": str(event.get("status") or ""),
                    "message": message,
                    "source_role": source,
                    "route": destination,
                    "hop_id": event.get("hop_id") or event.get("new_hop_id"),
                }
            )
    for index, error in enumerate(raw.get("errors") or []):
        if isinstance(error, Mapping):
            at = str(error.get("at") or "")
            message = error.get("error") or error.get("message")
            kind = str(error.get("kind") or error.get("code") or "error")
        else:
            at = ""
            message = error
            kind = "error"
        items.append(
            {
                "key": f"error:{index}",
                "at": at,
                "level": "ERROR",
                "kind": kind,
                "status": "",
                "message": _public_text(message, max_chars=1000),
                "source_role": None,
                "route": None,
                "hop_id": None,
            }
        )
    items.sort(key=lambda item: (item["at"], item["key"]), reverse=True)
    return items


def _surface(
    raw: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    roles: Sequence[Mapping[str, Any]],
    browser_connected: bool | None,
) -> tuple[str, str]:
    task_id = str(raw.get("task_id") or "")
    replaced = bool(raw.get("replaced_by_task_id")) or any(
        str(item.get("replaces_task_id") or "") == task_id for item in tasks
    )
    status = str(raw.get("status") or "INBOX").upper()
    if browser_connected is False:
        return (
            ("offline_recoverable", "unknown")
            if replaced or status in TERMINAL
            else ("active", "unknown")
        )
    online = any(bool(role.get("online")) for role in roles)
    if replaced or status in TERMINAL:
        return ("offline_recoverable", "terminal_tab_present") if online else ("history", "terminal")
    return ("active", "online") if online else ("offline_recoverable", "offline")


def _problem(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    status = str(raw.get("status") or "").upper()
    if status == "BLOCKED":
        return {
            "kind": "BLOCKED",
            "code": str(raw.get("block_code") or "blocked"),
            "message": _public_text(raw.get("block_reason") or "Task is blocked", max_chars=1000),
            "role": raw.get("active_role"),
            "hop_id": raw.get("active_hop_id"),
            "at": str(raw.get("updated_at") or ""),
        }
    if status == "WAITING":
        waiting = raw.get("waiting") if isinstance(raw.get("waiting"), Mapping) else {}
        return {
            "kind": "WAITING",
            "code": str(raw.get("waiting_code") or waiting.get("reason") or "waiting"),
            "message": _public_text(raw.get("waiting_reason") or waiting.get("reason") or "Task is waiting", max_chars=1000),
            "role": raw.get("active_role"),
            "hop_id": raw.get("active_hop_id"),
            "at": str(raw.get("updated_at") or ""),
        }
    return None


def _int_or_zero(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _report_rows(
    raw: Mapping[str, Any],
    *,
    public_task_id: str | None = None,
    report_id_prefix: str = "",
    source_task_id: str | None = None,
    generation: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    task_id = str(public_task_id or raw.get("task_id") or "")
    source_id = str(source_task_id or raw.get("task_id") or "")
    public: list[dict[str, Any]] = []
    private: dict[str, dict[str, Any]] = {}
    for index, report in enumerate(raw.get("reports") or [], start=1):
        if not isinstance(report, Mapping):
            continue
        original_id = str(report.get("report_id") or index)
        report_id = f"{report_id_prefix}{original_id}"
        content = report.get("content")
        encoded = str(content).encode("utf-8") if content is not None else None
        public.append({
            "report_id": report_id,
            "physical_role": str(report.get("physical_role") or "") or None,
            "role": str(report.get("role") or "") or None,
            "turn": report.get("turn"),
            "created_at": report.get("created_at") or report.get("at"),
            "summary": _public_text(report.get("summary"), max_chars=1000) or None,
            "outcome": str(report.get("outcome") or "") or None,
            "source_task_id": source_id,
            "generation": generation,
            "url": f"/api/reports/{task_id}/{report_id}",
        })
        if encoded is not None:
            private[report_id] = {
                "content": str(content),
                "sha256": str(report.get("sha256") or hashlib.sha256(encoded).hexdigest()),
                "size": _int_or_zero(report.get("size")) or len(encoded),
            }
        else:
            private[report_id] = {
                "path": str(report.get("path") or ""),
                "sha256": str(report.get("sha256") or ""),
                "size": _int_or_zero(report.get("size")),
            }
    return public, private


def _independent_response_rows(
    raw: Mapping[str, Any],
    *,
    public_task_id: str,
    report_id_prefix: str,
    generation: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    represented_hops: set[str] = set()
    legacy_hashes: set[str] = set()
    existing_ids: set[str] = set()
    for index, report in enumerate(raw.get("reports") or [], start=1):
        if not isinstance(report, Mapping):
            continue
        existing_ids.add(str(report.get("report_id") or index))
        hop_id = str(report.get("hop_id") or "").strip()
        if hop_id:
            represented_hops.add(hop_id)
            continue
        content = report.get("content")
        if content is not None:
            legacy_hashes.add(hashlib.sha256(str(content).encode("utf-8")).hexdigest())
            continue
        declared_hash = str(report.get("sha256") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", declared_hash):
            legacy_hashes.add(declared_hash)

    independent = raw.get("independent")
    last_outcome = (
        independent.get("last_outcome")
        if isinstance(independent, Mapping)
        and isinstance(independent.get("last_outcome"), Mapping)
        else {}
    )
    synthetic: list[dict[str, Any]] = []
    for hop in raw.get("hops") or []:
        if not isinstance(hop, Mapping) or str(hop.get("state") or "") != "responded":
            continue
        content = str(hop.get("response") or "")
        hop_id = _int_or_zero(hop.get("hop_id"))
        if not content.strip() or not hop_id:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        report_id = f"hop{hop_id}-response"
        if (
            str(hop_id) in represented_hops
            or digest in legacy_hashes
            or report_id in existing_ids
        ):
            continue
        timestamps = hop.get("timestamps")
        if not isinstance(timestamps, Mapping):
            timestamps = {}
        declared_hash = str(hop.get("response_sha256") or "").lower()
        synthetic.append(
            {
                "report_id": report_id,
                "physical_role": hop.get("physical_role"),
                "role": hop.get("target_role"),
                "turn": hop.get("turn"),
                "created_at": (
                    timestamps.get("responded_at")
                    or raw.get("updated_at")
                    or raw.get("created_at")
                ),
                "content": content,
                "sha256": declared_hash if declared_hash == digest else digest,
                "summary": last_outcome.get("summary"),
                "outcome": last_outcome.get("outcome"),
            }
        )
    return _report_rows(
        {"task_id": raw.get("task_id"), "reports": synthetic},
        public_task_id=public_task_id,
        report_id_prefix=report_id_prefix,
        source_task_id=str(raw.get("task_id") or ""),
        generation=generation,
    )


def _independent_lifecycle(
    raw: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    independent = raw.get("independent")
    if not isinstance(independent, Mapping):
        return [], [], {}
    agent_key = str(independent.get("agent_key") or "")
    generations = (
        [raw]
        if not agent_key
        else [
            item for item in tasks
            if isinstance(item.get("independent"), Mapping)
            and str(item["independent"].get("agent_key") or "") == agent_key
        ]
    )
    generations.sort(
        key=lambda item: (
            _int_or_zero(item["independent"].get("agent_generation")),
            str(item.get("updated_at") or item.get("created_at") or ""),
        ),
        reverse=True,
    )
    history: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    private: dict[str, dict[str, Any]] = {}
    selected_task_id = str(raw.get("task_id") or "")
    for item in generations:
        metadata = item["independent"]
        generation = _int_or_zero(metadata.get("agent_generation"))
        item_reports, item_private = _report_rows(
            item,
            public_task_id=selected_task_id,
            report_id_prefix=f"g{generation}-",
            source_task_id=str(item.get("task_id") or ""),
            generation=generation,
        )
        response_reports, response_private = _independent_response_rows(
            item,
            public_task_id=selected_task_id,
            report_id_prefix=f"g{generation}-",
            generation=generation,
        )
        item_reports.extend(response_reports)
        item_private.update(response_private)
        reports.extend(item_reports)
        private.update(item_private)
        active_event = metadata.get("active_event")
        if not isinstance(active_event, Mapping):
            active_event = {}
        history.append({
            "task_id": str(item.get("task_id") or ""),
            "agent_key": agent_key,
            "generation": generation,
            "status": str(item.get("status") or "").upper(),
            "active_action": str(item.get("active_action") or "") or None,
            "created_at": str(item.get("created_at") or "") or None,
            "started_at": str(item.get("started_at") or "") or None,
            "updated_at": str(item.get("updated_at") or "") or None,
            "completed_at": str(item.get("completed_at") or "") or None,
            "stopped_at": str(item.get("stopped_at") or "") or None,
            "previous_task_id": str(metadata.get("previous_task_id") or "") or None,
            "successor_task_id": str(metadata.get("successor_task_id") or "") or None,
            "enabled": bool(metadata.get("enabled")),
            "cycle": _int_or_zero(metadata.get("cycle")),
            "max_cycles": _int_or_zero(metadata.get("max_cycles")),
            "trigger_type": str(active_event.get("trigger_type") or "") or None,
            "target_task_id": str(active_event.get("target_task_id") or "") or None,
            "last_outcome": _public_value(metadata.get("last_outcome")),
            "report_count": len(item_reports),
        })
    reports.sort(
        key=lambda item: (
            _int_or_zero(item.get("generation")),
            str(item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return history, reports, private


def _maintenance_reports(raw: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    task_id = str(raw.get("task_id") or "")
    maintenance = raw.get("maintenance") if isinstance(raw.get("maintenance"), Mapping) else {}
    public: list[dict[str, Any]] = []
    private: dict[str, dict[str, Any]] = {}
    for incident in maintenance.get("incidents") or []:
        if not isinstance(incident, Mapping) or not incident.get("report_path"):
            continue
        incident_id = str(incident.get("incident_id") or "")
        if not incident_id:
            continue
        public.append(
            {
                "incident_id": incident_id,
                "state": incident.get("state"),
                "updated_at": incident.get("updated_at"),
                "url": f"/api/maintenance-reports/{task_id}/{incident_id}",
            }
        )
        private[incident_id] = {
            "path": str(incident.get("report_path") or ""),
            "sha256": str(incident.get("report_sha256") or ""),
            "size": _int_or_zero(incident.get("report_size")),
        }
    return public, private


_TASK_STATUSES = frozenset(
    {"INBOX", "RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED"}
)


def _waiting_intervention(message: str) -> str:
    return _public_text(f"Intervention required: {message}", max_chars=240)


def build_waiting_order(
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    normal_waiting = [
        task
        for task in tasks
        if str(task.get("status") or "").upper() == "WAITING"
        and not is_independent_task(task)
    ]
    if not normal_waiting:
        return {}

    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for task in tasks:
        raw_task_id = task.get("task_id")
        if isinstance(raw_task_id, str) and raw_task_id.strip():
            buckets.setdefault(raw_task_id.strip(), []).append(task)

    ordered_waiting = sorted(normal_waiting, key=exact_team_waiter_order_key)
    fifo_by_id: dict[str, int] = {}
    for task in ordered_waiting:
        task_id = str(task.get("task_id") or "").strip()
        if task_id and task_id not in fifo_by_id:
            fifo_by_id[task_id] = len(fifo_by_id)

    records = {
        task_id: {"rank": None, "fifo": fifo, "intervention": None}
        for task_id, fifo in fifo_by_id.items()
    }
    ambiguous_ids = {
        task_id for task_id, values in buckets.items() if len(values) != 1
    }
    task_by_id = {
        task_id: values[0]
        for task_id, values in buckets.items()
        if len(values) == 1
    }
    local_invalid: dict[str, str] = {}
    parents_by_id: dict[str, tuple[str, ...]] = {}
    for task_id, task in task_by_id.items():
        status = str(task.get("status") or "").upper()
        if status not in _TASK_STATUSES:
            local_invalid[task_id] = _waiting_intervention(
                f"dependency {task_id} has an invalid state"
            )
            parents_by_id[task_id] = ()
            continue
        if status == "DONE":
            parents_by_id[task_id] = ()
            continue
        try:
            parent_ids = dependency_parent_ids(task)
        except ValueError:
            local_invalid[task_id] = _waiting_intervention(
                f"task {task_id} has malformed dependencies"
            )
            parents_by_id[task_id] = ()
            continue
        if len(parent_ids) != len(set(parent_ids)):
            local_invalid[task_id] = _waiting_intervention(
                f"task {task_id} has ambiguous dependencies"
            )
            parents_by_id[task_id] = ()
            continue
        parents_by_id[task_id] = parent_ids

    validation_cache: dict[str, tuple[bool, str | None]] = {}

    def validate(task_id: str, trail: tuple[str, ...] = ()) -> tuple[bool, str | None]:
        cached = validation_cache.get(task_id)
        if cached is not None:
            return cached
        if task_id in trail:
            return False, _waiting_intervention("dependency graph contains a cycle")
        if task_id in ambiguous_ids:
            result = (
                False,
                _waiting_intervention(f"dependency {task_id} is ambiguous"),
            )
            validation_cache[task_id] = result
            return result
        task = task_by_id.get(task_id)
        if task is None:
            return False, _waiting_intervention(f"missing dependency {task_id}")
        if task_id in local_invalid:
            result = False, local_invalid[task_id]
            validation_cache[task_id] = result
            return result
        if str(task.get("status") or "").upper() == "DONE":
            validation_cache[task_id] = (True, None)
            return True, None
        for parent_id in parents_by_id.get(task_id, ()):
            if parent_id in ambiguous_ids:
                result = (
                    False,
                    _waiting_intervention(f"dependency {parent_id} is ambiguous"),
                )
                validation_cache[task_id] = result
                return result
            parent = task_by_id.get(parent_id)
            if parent is None:
                result = (
                    False,
                    _waiting_intervention(f"missing dependency {parent_id}"),
                )
                validation_cache[task_id] = result
                return result
            if str(parent.get("status") or "").upper() == "STOPPED":
                result = (
                    False,
                    _waiting_intervention(f"dependency {parent_id} is STOPPED"),
                )
                validation_cache[task_id] = result
                return result
            valid, reason = validate(parent_id, (*trail, task_id))
            if not valid:
                result = False, reason
                validation_cache[task_id] = result
                return result
        validation_cache[task_id] = (True, None)
        return True, None

    predecessors: dict[str, set[str]] = {task_id: set() for task_id in records}
    for task_id in records:
        valid, reason = validate(task_id)
        if not valid:
            records[task_id]["intervention"] = reason
            continue
        for parent_id in parents_by_id.get(task_id, ()):
            parent = task_by_id.get(parent_id)
            if (
                parent_id in records
                and parent is not None
                and str(parent.get("status") or "").upper() == "WAITING"
                and not is_independent_task(parent)
            ):
                predecessors[task_id].add(parent_id)

    team_waiters: dict[str, list[Mapping[str, Any]]] = {}
    for task in ordered_waiting:
        team = str(task.get("team") or "").strip()
        task_id = str(task.get("task_id") or "").strip()
        if not team or not task_id:
            if task_id in records:
                records[task_id]["intervention"] = _waiting_intervention(
                    "task has no exact-team identity"
                )
            continue
        team_waiters.setdefault(team, []).append(task)
    graph_tasks = list(task_by_id.values())
    for team, waiters in team_waiters.items():
        valid_waiters = [
            task
            for task in waiters
            if not records[str(task.get("task_id") or "").strip()]["intervention"]
        ]
        ready_ids = {
            str(task.get("task_id") or "").strip()
            for task in exact_team_ready_waiters(
                valid_waiters,
                team,
                dependency_tasks=graph_tasks,
            )
        }
        ready = [
            task
            for task in valid_waiters
            if str(task.get("task_id") or "").strip() in ready_ids
        ]
        blocked = [
            task
            for task in valid_waiters
            if str(task.get("task_id") or "").strip() not in ready_ids
        ]
        previous_id: str | None = None
        seen: set[str] = set()
        for task in (*ready, *blocked):
            task_id = str(task.get("task_id") or "").strip()
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            if previous_id is not None:
                predecessors[task_id].add(previous_id)
            previous_id = task_id

    rank_cache: dict[str, tuple[int | None, str | None]] = {}

    def rank_for(
        task_id: str,
        trail: tuple[str, ...] = (),
    ) -> tuple[int | None, str | None]:
        cached = rank_cache.get(task_id)
        if cached is not None:
            return cached
        reason = records[task_id]["intervention"]
        if reason:
            result = None, str(reason)
            rank_cache[task_id] = result
            return result
        if task_id in trail:
            return None, _waiting_intervention("projected order contains a cycle")
        highest = 0
        for parent_id in sorted(predecessors[task_id]):
            parent_rank, parent_reason = rank_for(parent_id, (*trail, task_id))
            if parent_rank is None:
                result = None, parent_reason
                rank_cache[task_id] = result
                records[task_id]["intervention"] = parent_reason
                return result
            highest = max(highest, parent_rank)
        result = highest + 1, None
        rank_cache[task_id] = result
        return result

    for task_id in records:
        rank, reason = rank_for(task_id)
        records[task_id]["rank"] = rank
        records[task_id]["intervention"] = reason
    return records


def build_task_projection(
    raw: Mapping[str, Any],
    *,
    tasks: Sequence[Mapping[str, Any]],
    waiting_order: Mapping[str, Mapping[str, Any]] | None = None,
    browser_pages: Sequence[Mapping[str, Any]] = (),
    browser_connected: bool | None = None,
) -> TaskProjection:
    task_id = str(raw.get("task_id") or "")
    team = str(raw.get("team") or "")
    status = str(raw.get("status") or "INBOX").upper()
    replacement_task_id = next(
        (
            str(item.get("task_id") or "")
            for item in tasks
            if str(item.get("replaces_task_id") or "") == task_id
        ),
        None,
    )
    updated_at = str(raw.get("updated_at") or raw.get("created_at") or "")
    roles = _roles(raw, browser_pages, browser_connected)
    surface, availability = _surface(raw, tasks, roles, browser_connected)
    independent = raw.get("independent") if isinstance(raw.get("independent"), Mapping) else None
    if independent is not None:
        independent_history, reports, report_private = _independent_lifecycle(raw, tasks)
    else:
        independent_history = []
        reports, report_private = _report_rows(raw)
    maintenance_reports, maintenance_private = _maintenance_reports(raw)
    timeline = _timeline(raw)
    problem = _problem(raw)
    role_inputs, active_input = _role_inputs(raw)
    if independent is not None:
        role_inputs = {
            role: {**value, "input": None, "handoff": None}
            for role, value in role_inputs.items()
        }
        if active_input is not None:
            active_input = {**active_input, "input": None, "handoff": None}
    queue = raw.get("queue") if isinstance(raw.get("queue"), Mapping) else {}
    title = _public_text(
        str(raw.get("task_text") or raw.get("task_title") or "").splitlines()[0],
        max_chars=240,
    )
    column = str(raw.get("kanban_column") or status).upper()
    if status in TERMINAL:
        column = status
    compact_roles = [
        {
            "logical_role": role["logical_role"],
            "physical_role": role["physical_role"],
            "status": role["status"],
            "turn": role["turn"],
            "online": role["online"],
        }
        for role in roles
    ]
    summary = {
        "task_id": task_id,
        "team": team,
        "status": status,
        "column": column,
        "surface": surface,
        "active_role": raw.get("active_role"),
        "active_hop_id": raw.get("active_hop_id"),
        "active_action": raw.get("active_action"),
        "created_at": str(raw.get("created_at") or "") or None,
        "started_at": str(raw.get("started_at") or "") or None,
        "updated_at": updated_at,
        "effective_activity_at": str(raw.get("last_role_activity_at") or updated_at),
        "availability": availability,
        "primary_problem": problem,
        "waiting_reason": _public_text(raw.get("waiting_reason"), max_chars=500) or None,
        "block_code": raw.get("block_code"),
        "queue_position": queue.get("position"),
        "queue_length": queue.get("length"),
        "roles": compact_roles,
        "has_reports": bool(reports or maintenance_reports),
        "task_title": title,
        "version": 0,
    }
    if status == "WAITING" and independent is None:
        order_map = (
            build_waiting_order(tasks) if waiting_order is None else waiting_order
        )
        summary["waiting_order"] = dict(
            order_map.get(task_id)
            or {
                "rank": None,
                "fifo": len(order_map),
                "intervention": _waiting_intervention(
                    "execution order is unavailable"
                ),
            }
        )
    if independent is not None:
        active_event = (
            independent.get("active_event")
            if isinstance(independent.get("active_event"), Mapping)
            else {}
        )
        summary["task_mode"] = "independent"
        summary["agent"] = {
            "name": _public_text(independent.get("agent_name"), max_chars=80),
            "generation": _int_or_zero(independent.get("agent_generation")),
            "enabled": bool(independent.get("enabled")),
            "trigger_type": str(active_event.get("trigger_type") or "") or None,
            "target_team": str(active_event.get("target_team") or "") or None,
            "target_task_id": str(active_event.get("target_task_id") or "") or None,
            "occurrence_count": _int_or_zero(active_event.get("occurrence_count")),
            "check_count": _int_or_zero(active_event.get("check_count")),
            "cycle": _int_or_zero(independent.get("cycle")),
            "max_cycles": _int_or_zero(independent.get("max_cycles")),
            "trigger_settings": _public_value(
                independent.get("trigger_settings") or {}
            ),
            "new_chat_next_job": bool(
                independent.get("new_chat_next_job")
            ),
            "idle_tab_closed_at": str(
                independent.get("idle_tab_closed_at") or ""
            ) or None,
            "last_outcome": _public_value(independent.get("last_outcome")),
        }
    detail = {
        **summary,
        "task_text": _public_text(raw.get("task_text"), max_chars=None),
        "roles": roles,
        "active_hop": active_input,
        "active_input": active_input,
        "role_inputs": role_inputs,
        "timeline": timeline[:50],
        "timeline_total": len(timeline),
        "reports": reports,
        "independent_history": independent_history,
        "maintenance_reports": maintenance_reports,
        "depends_on_task_ids": [str(item) for item in raw.get("depends_on_task_ids") or []],
        "replaces_task_id": str(raw.get("replaces_task_id") or "") or None,
        "replacement_task_id": replacement_task_id,
        "immutable_history": bool(replacement_task_id),
        "controls": _public_value([item for item in raw.get("controls") or [] if isinstance(item, Mapping)]),
        "cleanup": _public_value(dict(raw.get("cleanup") or {})),
        "attachments": [
            {"name": str(item.get("name") or Path(str(item.get("path") or "")).name)}
            for item in raw.get("attachments") or []
            if isinstance(item, Mapping)
        ],
    }
    private = {
        "manifest_path": str(raw.get("manifest_path") or ""),
        "reports": report_private,
        "maintenance_reports": maintenance_private,
        "timeline": timeline,
    }
    private_identities = _private_identity_values(raw)
    summary = _redact_private_identities(summary, private_identities)
    detail = _redact_private_identities(detail, private_identities)
    # Stable content fingerprints drive no-op DB writes and client detail caching.
    summary["projection_sha256"] = hashlib.sha256(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    detail["projection_sha256"] = hashlib.sha256(
        json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TaskProjection(
        task_id=task_id,
        team=team,
        status=status,
        surface=surface,
        active_role=str(raw.get("active_role")) if raw.get("active_role") else None,
        updated_at=updated_at,
        summary=summary,
        detail=detail,
        private=private,
    )


def _dashboard_action_task(raw: Mapping[str, Any]) -> dict[str, str]:
    title = _public_text(
        str(raw.get("task_text") or raw.get("task_title") or "").splitlines()[0],
        max_chars=240,
    )
    return {
        "task_id": str(raw.get("task_id") or ""),
        "title": title,
        "status": str(raw.get("status") or "INBOX").upper(),
    }


def build_dashboard_actions(
    tasks: Sequence[Mapping[str, Any]],
    *,
    browser_pages: Sequence[Mapping[str, Any]] = (),
    browser_connected: bool | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for task in tasks:
        if is_independent_task(task):
            continue
        team = str(task.get("team") or "")
        task_id = str(task.get("task_id") or "")
        if not team or not task_id:
            continue
        grouped.setdefault(team, []).append(task)

    dependency_teams: list[dict[str, Any]] = []
    resume_teams: list[dict[str, Any]] = []
    reuse_teams: list[dict[str, str]] = []
    for team in sorted(grouped):
        team_tasks = sorted(
            grouped[team],
            key=lambda item: str(item.get("task_id") or ""),
        )
        dependency_tasks = [
            _dashboard_action_task(item)
            for item in team_tasks
            if str(item.get("status") or "").upper() in {"INBOX", "RUNNING", "WAITING"}
        ]
        if dependency_tasks:
            dependency_teams.append(
                {
                    "team": team,
                    "ambiguous": len(dependency_tasks) != 1,
                    "tasks": dependency_tasks,
                }
            )

        barriers = [
            item for item in team_tasks if is_team_availability_barrier(item)
        ]
        if len(barriers) == 1 and is_active_team_owner(barriers[0]):
            owner = barriers[0]
            status = str(owner.get("status") or "INBOX").upper()
            reason: str | None = None
            if status == "BLOCKED":
                reason = "blocked"
            elif status == "PAUSED":
                reason = "paused"
            elif status in {"INBOX", "RUNNING"} and browser_connected is True:
                projection = build_task_projection(
                    owner,
                    tasks=tasks,
                    browser_pages=browser_pages,
                    browser_connected=True,
                )
                if projection.summary.get("availability") == "offline":
                    reason = "offline"
            if reason is not None:
                resume_teams.append(
                    {
                        "team": team,
                        **_dashboard_action_task(owner),
                        "reason": reason,
                    }
                )

        if exact_team_reuse_eligible(team_tasks, team):
            reuse_teams.append({"team": team, "status": "available"})

    return {
        "dependency_teams": dependency_teams,
        "resume_teams": resume_teams,
        "reuse_teams": reuse_teams,
    }
