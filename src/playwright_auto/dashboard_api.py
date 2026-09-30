from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .cdpa_bootstraps import (
    BootstrapCatalog,
    normalize_bootstrap_record,
    resolve_default_bootstrap_id,
)
from .cdpa_commands import RepairRequest
from .cdpa_config import CDPAConfig, declared_repository_from_task, load_cdpa_config
from .cdpa_endpoints import BrowserEndpointRegistry
from .cdpa_identity import generate_idempotent_task_id, validate_task_id
from .cdpa_independent import (
    normalize_agent_name,
    normalize_completion_request,
    normalize_manual_instruction,
    normalize_max_cycles,
    normalize_system_prompt,
    validate_trigger_settings,
)
from .cdpa_routes import effective_report_mode
from .cdpa_runtime_db import IdempotencyConflict, RuntimeDB, RuntimeDBError
from .observability import mcp_allow_click_summary, read_recent_live_events
from .live_stream import LiveEnvelope, LiveStreamHub
from .cdpa_workflow_agents import (
    normalize_workflow_display_name,
    ordered_system_routes,
    require_team_closing_role,
    validate_workflow_route_key,
)

STATUSES = ("RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED")
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _iso_timestamp(value: object) -> float | None:
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


def _cursor_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _cursor_decode(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


class APIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class DashboardAPI:
    def __init__(
        self,
        config: CDPAConfig,
        *,
        db: RuntimeDB | None = None,
    ) -> None:
        self.config = config
        self.live_event_log = config.repository_root / ".runtime" / "live-events.jsonl"
        self.live_stream_hub = LiveStreamHub(self.live_event_log)
        self.db = db or RuntimeDB(config.runtime_database)
        self.db.ensure_schema()
        self._system_lock = threading.Lock()
        self._previous_cpu = self._read_cpu()

    @staticmethod
    def _read_cpu() -> tuple[int, int] | None:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                idle = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return None
                def ticks(value: wintypes.FILETIME) -> int:
                    return (value.dwHighDateTime << 32) | value.dwLowDateTime

                return ticks(kernel) + ticks(user), ticks(idle)
            except (AttributeError, OSError, ValueError):
                return None

        try:
            with Path("/proc/stat").open(encoding="utf-8") as handle:
                values = [int(value) for value in handle.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        if len(values) < 4:
            return None
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)

    def system_status(self) -> dict[str, Any]:
        current = self._read_cpu()
        with self._system_lock:
            previous = self._previous_cpu
            self._previous_cpu = current
        cpu_percent: float | None = None
        if current is not None and previous is not None:
            total_delta = current[0] - previous[0]
            idle_delta = current[1] - previous[1]
            if total_delta > 0:
                cpu_percent = round(
                    max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta)),
                    1,
                )
        try:
            disk_free_bytes: int | None = shutil.disk_usage(self.config.repository_root).free
        except OSError:
            disk_free_bytes = None
        return {"cpu_percent": cpu_percent, "disk_free_bytes": disk_free_bytes}

    def _worker_snapshot(self) -> dict[str, Any] | None:
        snapshot = self.db.get_snapshot("worker")
        return dict(snapshot["payload"]) if snapshot else None

    def worker_health(self) -> dict[str, Any]:
        worker = self._worker_snapshot()
        heartbeat = (worker or {}).get("heartbeat_at")
        timestamp = _iso_timestamp(heartbeat)
        stale = timestamp is None or time.time() - timestamp > self.config.worker_stale_seconds
        source_drift = dict((worker or {}).get("source_drift") or {})
        reload_required = bool((worker or {}).get("reload_required"))
        runtime_ready = bool((worker or {}).get("runtime_ready", True))
        command_loop_ready = bool((worker or {}).get("command_loop_ready", True))
        ready = runtime_ready and command_loop_ready
        status = (
            "OFFLINE"
            if worker is None
            else "STALE"
            if stale
            else "SOURCE_DRIFT"
            if reload_required
            else "STARTING"
            if not ready
            else "HEALTHY"
        )
        return {
            "worker_online": worker is not None and not stale,
            "worker_stale": stale,
            "worker_ready": ready,
            "worker_runtime_ready": runtime_ready,
            "worker_command_loop_ready": command_loop_ready,
            "worker_heartbeat_at": heartbeat,
            "worker_status": status,
            "worker_source_drift": source_drift,
            "worker_reload_required": reload_required,
            "worker_source_identity": dict((worker or {}).get("source_identity") or {}),
            "worker_lease": dict((worker or {}).get("lease") or {}),
        }

    @staticmethod
    def _stale_resume_result() -> dict[str, Any]:
        return {
            "outcome": "recovery_required",
            "action": "none",
            "reason_code": "stale_worker",
            "reason": "Resume cannot run because the worker heartbeat is stale.",
            "next_safe_action": "pm2 restart playwright-cdpa-worker",
            "postcondition": None,
            "before": None,
            "after": None,
        }

    def _fail_stale_resume(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if (
            command.get("kind") == "resume_team"
            and command.get("status") in {"queued", "running"}
            and self.worker_health()["worker_stale"]
        ):
            result = self._stale_resume_result()
            self.db.require_command_recovery(
                str(command["command_id"]),
                error=str(result["reason"]),
                result=result,
            )
            refreshed = self.db.get_command(str(command["command_id"]))
            assert refreshed is not None
            return refreshed
        return dict(command)

    def _repository(self, value: object) -> str:
        repository = Path(str(value or self.config.repository_root)).expanduser().resolve()
        if not any(repository.is_relative_to(root) for root in self.config.repository_allowed_roots):
            raise APIError(400, "repository_not_allowed", "repository is outside configured allowed roots")
        return str(repository)

    @staticmethod
    def _declared_repository(task: str) -> str | None:
        try:
            return declared_repository_from_task(task)
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc

    @staticmethod
    def _list(value: object, field: str) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise APIError(400, "invalid_request", f"{field} must be an array")
        return value

    def _default_bootstrap_id(self) -> str | None:
        return resolve_default_bootstrap_id(BootstrapCatalog(self.config.repository_root))

    def _active_workflow_routes(self) -> tuple[str, ...]:
        snapshot = self.db.get_snapshot("agents")
        payload = snapshot.get("payload") if snapshot is not None else None
        workflow = payload.get("workflow") if isinstance(payload, Mapping) else None
        if not isinstance(workflow, list):
            return ordered_system_routes(self.config)
        routes = []
        for item in workflow:
            if not isinstance(item, Mapping) or item.get("deleted_at") is not None:
                continue
            routes.append(validate_workflow_route_key(item.get("route_key")))
        return tuple(routes) or ordered_system_routes(self.config)

    def _workflow_roles(self, value: object) -> list[str]:
        raw_roles = self._list(value, "roles")
        roles: list[str] = []
        for item in raw_roles:
            if not isinstance(item, str) or not item.strip():
                raise APIError(
                    400,
                    "invalid_request",
                    "roles must contain non-empty strings",
                )
            roles.append(item.strip().upper())
        if not roles:
            raise APIError(400, "invalid_request", "roles must not be empty")
        if len(set(roles)) != len(roles):
            raise APIError(
                400,
                "invalid_request",
                "roles must not contain duplicates",
            )
        available = self._active_workflow_routes()
        unknown = set(roles) - set(available)
        if unknown:
            raise APIError(
                400,
                "invalid_request",
                f"unknown workflow roles: {sorted(unknown)!r}",
            )
        try:
            require_team_closing_role(roles)
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        selected = set(roles)
        return [role for role in available if role in selected]

    def _bootstrap_definition(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise APIError(400, "invalid_request", "bootstrap_definition must be an object")
        allowed = {"bootstrap_id", "name", "source", "prewarm_prompt", "max_backups"}
        unknown = set(value) - allowed
        if unknown:
            raise APIError(
                400,
                "invalid_request",
                f"unknown bootstrap_definition fields: {sorted(unknown)!r}",
            )
        source = value.get("source")
        if isinstance(source, str) and not source.strip():
            source = None
        prewarm_prompt = value.get("prewarm_prompt")
        if isinstance(prewarm_prompt, str) and not prewarm_prompt.strip():
            prewarm_prompt = None
        try:
            normalized = normalize_bootstrap_record(
                {
                    "bootstrap_id": value.get("bootstrap_id"),
                    "name": value.get("name"),
                    "description": "",
                    "source_conversation_id": source,
                    "prewarm_prompt": prewarm_prompt,
                    "max_backups": value.get("max_backups", 7),
                    "donors": [],
                    "enabled": True,
                    "tags": [],
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "updated_at": "2000-01-01T00:00:00+00:00",
                }
            )
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {
            "bootstrap_id": normalized["bootstrap_id"],
            "name": normalized["name"],
            "source_conversation_id": normalized["source_conversation_id"],
            "prewarm_prompt": normalized["prewarm_prompt"],
            "max_backups": normalized["max_backups"],
        }

    def normalize_create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "task",
            "requested_team",
            "reuse_team",
            "roles",
            "new_roles",
            "new_all",
            "repository",
            "report_mode",
            "depends_on_task_ids",
            "upload_paths",
            "bootstrap_id",
            "bootstrap_definition",
            "temporary",
            "browser_endpoint_id",
            "required_browser_endpoint_id",
            "workflow_mode",
            "role",
            "goal",
            "conversation_url",
            "start_role",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        workflow_mode = str(raw.get("workflow_mode") or "team").strip().lower()
        if workflow_mode not in {"team", "solo"}:
            raise APIError(400, "invalid_request", "workflow_mode must be team or solo")
        task = str(raw.get("task") or "").strip()
        if not task and workflow_mode == "solo":
            existing_url = str(raw.get("conversation_url") or "").strip()
            if existing_url:
                role_hint = str(raw.get("role") or "solo").strip().upper() or "SOLO"
                task = f"Continue existing {role_hint} conversation {existing_url.rsplit('/', 1)[-1]}"
        if not task:
            raise APIError(400, "invalid_request", "task must not be empty")
        requested_repository = raw.get("repository")
        inferred_repository = requested_repository is None or (
            isinstance(requested_repository, str) and not requested_repository.strip()
        )
        if inferred_repository:
            requested_repository = self._declared_repository(task)
        try:
            repository = self._repository(requested_repository)
        except APIError as exc:
            requested_path = Path(str(requested_repository or ""))
            control_drive = self.config.repository_root.drive.casefold()
            requested_drive = requested_path.drive.casefold()
            remote_drive = bool(requested_drive and control_drive and requested_drive != control_drive)
            if not inferred_repository or exc.code != "repository_not_allowed" or not remote_drive:
                raise
            # Cross-drive repository prose is a remote product hint for this
            # control runtime, not an implicit local execution-root override.
            repository = self._repository(None)
        try:
            report_mode = effective_report_mode(
                raw.get("report_mode") or "file",
                control_repository=self.config.repository_root,
                execution_repository=repository,
            )
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        normalized = {
            "task": task,
            "requested_team": raw.get("requested_team"),
            "reuse_team": raw.get("reuse_team") or None,
            "new_roles": self._list(raw.get("new_roles"), "new_roles"),
            "new_all": bool(raw.get("new_all")),
            "repository": repository,
            "report_mode": report_mode,
            "depends_on_task_ids": self._list(
                raw.get("depends_on_task_ids"), "depends_on_task_ids"
            ),
            "upload_paths": self._list(raw.get("upload_paths"), "upload_paths"),
        }
        if workflow_mode == "solo":
            conflicting = {
                "roles",
                "reuse_team",
                "new_roles",
                "new_all",
                "bootstrap_id",
                "bootstrap_definition",
                "temporary",
                "required_browser_endpoint_id",
                "start_role",
            } & set(raw)
            if conflicting:
                raise APIError(
                    400,
                    "invalid_request",
                    f"solo task does not accept Team Task fields: {sorted(conflicting)!r}",
                )
            role = str(raw.get("role") or "").strip().upper()
            if not role:
                raise APIError(400, "invalid_request", "solo role must not be empty")
            if role not in self._active_workflow_routes():
                raise APIError(400, "invalid_request", f"unknown workflow role: {role!r}")
            normalized["workflow_mode"] = "solo"
            normalized["roles"] = [role]
            normalized["goal"] = str(raw.get("goal") or "").strip()
        elif "role" in raw or "goal" in raw or "conversation_url" in raw:
            raise APIError(
                400,
                "invalid_request",
                "role, goal, and conversation_url are valid only for Solo Task",
            )
        elif "roles" in raw:
            normalized["roles"] = self._workflow_roles(raw.get("roles"))
        if workflow_mode == "team" and "start_role" in raw:
            start_role = raw.get("start_role")
            if not isinstance(start_role, str) or not start_role.strip():
                raise APIError(400, "invalid_request", "start_role must be a non-empty string")
            normalized["start_role"] = start_role.strip().upper()
            if "roles" in normalized and normalized["start_role"] not in normalized["roles"]:
                raise APIError(
                    400, "invalid_request", "start_role must be one of the selected workflow roles"
                )
        if "temporary" in raw:
            if type(raw.get("temporary")) is not bool:
                raise APIError(400, "invalid_request", "temporary must be a boolean")
            if raw.get("temporary") is True:
                normalized["conversation_mode"] = "temporary"
        if "browser_endpoint_id" in raw:
            endpoint = raw.get("browser_endpoint_id")
            if not isinstance(endpoint, str) or not endpoint.strip():
                raise APIError(400, "invalid_request", "browser_endpoint_id must be a non-empty string")
            normalized["browser_endpoint_id"] = endpoint.strip()
        if workflow_mode == "solo":
            if "browser_endpoint_id" not in normalized:
                raise APIError(
                    400,
                    "invalid_request",
                    "Solo Task requires a browser_endpoint_id",
                )
            endpoint_id = self._resolved_browser_endpoint_id(
                normalized["browser_endpoint_id"]
            )
            normalized["browser_endpoint_id"] = endpoint_id
            normalized["required_browser_endpoint_id"] = endpoint_id
            conversation_url = raw.get("conversation_url")
            if conversation_url is not None:
                if not isinstance(conversation_url, str) or not conversation_url.strip():
                    raise APIError(
                        400,
                        "invalid_request",
                        "conversation_url must be an exact ChatGPT /c/... URL",
                    )
                parsed = urlsplit(conversation_url.strip())
                parts = [part for part in parsed.path.split("/") if part]
                if (
                    parsed.scheme != "https"
                    or (parsed.hostname or "").lower()
                    not in {"chatgpt.com", "www.chatgpt.com"}
                    or len(parts) != 2
                    or parts[0] != "c"
                    or not parts[1]
                    or parsed.query
                    or parsed.fragment
                ):
                    raise APIError(
                        400,
                        "invalid_request",
                        "conversation_url must be an exact ChatGPT /c/... URL",
                    )
                normalized["conversation_url"] = conversation_url.strip()
            normalized["bootstrap_id"] = None
            normalized["context_policy"] = "fresh"
            return normalized
        if "required_browser_endpoint_id" in raw:
            required_endpoint = self._resolved_browser_endpoint_id(
                raw.get("required_browser_endpoint_id")
            )
            selected_endpoint = normalized.get("browser_endpoint_id")
            selected_resolved = (
                self._resolved_browser_endpoint_id(selected_endpoint)
                if selected_endpoint is not None
                else None
            )
            if selected_resolved is not None and selected_resolved != required_endpoint:
                raise APIError(
                    400,
                    "invalid_request",
                    "workflow team uses one endpoint; browser_endpoint_id and required_browser_endpoint_id must match",
                )
            normalized["browser_endpoint_id"] = required_endpoint
            normalized["required_browser_endpoint_id"] = required_endpoint
        if raw.get("bootstrap_definition") is not None:
            raise APIError(
                400,
                "invalid_request",
                "bootstrap context is temporarily disabled; new workflow tasks always use Fresh context",
            )
        if raw.get("bootstrap_id") not in {None, ""}:
            raise APIError(
                400,
                "invalid_request",
                "bootstrap context is temporarily disabled; new workflow tasks always use Fresh context",
            )
        normalized["bootstrap_id"] = None
        normalized["context_policy"] = "fresh"
        return normalized

    def normalize_workflow_agent_create(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(raw) - {"name", "system_prompt"}:
            raise APIError(
                400,
                "invalid_request",
                "workflow agent accepts only name and system_prompt",
            )
        try:
            name = normalize_workflow_display_name(raw.get("name"))
            prompt = normalize_system_prompt(raw.get("system_prompt"))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {"name": name, "system_prompt": prompt}

    def normalize_workflow_agent_update(
        self, route_key: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = self.normalize_workflow_agent_create(raw)
        payload["route_key"] = validate_workflow_route_key(route_key)
        return payload

    @staticmethod
    def normalize_empty(raw: Mapping[str, Any]) -> dict[str, Any]:
        if raw:
            raise APIError(400, "invalid_request", "request body must be empty")
        return {}

    @staticmethod
    def _browser_endpoint_id(value: object) -> str:
        endpoint_id = str(value or "").strip()
        if not endpoint_id or len(endpoint_id) > 128:
            raise APIError(400, "invalid_request", "browser endpoint id is invalid")
        return endpoint_id

    def _resolved_browser_endpoint_id(self, value: object) -> str:
        try:
            return BrowserEndpointRegistry(self.config).resolve(
                self._browser_endpoint_id(value)
            )["endpoint_id"]
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc

    def normalize_browser_endpoint_add(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "address", "label", "temporary_default", "management_mode",
            "profile_path", "executable_path", "rate_limit_scope",
            "max_active_tabs", "clone_from_endpoint_id", "start",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        address = raw.get("address")
        if not isinstance(address, str) or not address.strip():
            raise APIError(400, "invalid_request", "address must be a non-empty string")
        payload: dict[str, Any] = {"address": address.strip()}
        mode = raw.get("management_mode", "external")
        if mode not in {"external", "managed-local"}:
            raise APIError(400, "invalid_request", "management_mode must be external or managed-local")
        if "management_mode" in raw:
            payload["management_mode"] = mode
        for name in ("label", "profile_path", "executable_path", "rate_limit_scope", "clone_from_endpoint_id"):
            if name not in raw:
                continue
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise APIError(400, "invalid_request", f"{name} must be a non-empty string")
            payload[name] = value.strip()
        for name in ("temporary_default", "start"):
            if name in raw:
                if type(raw.get(name)) is not bool:
                    raise APIError(400, "invalid_request", f"{name} must be a boolean")
                payload[name] = raw[name]
        if "max_active_tabs" in raw:
            capacity = raw.get("max_active_tabs")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
                raise APIError(400, "invalid_request", "max_active_tabs must be a positive integer")
            payload["max_active_tabs"] = capacity
        managed_only = {"profile_path", "executable_path", "clone_from_endpoint_id", "start"}
        if mode == "external" and any(name in payload for name in managed_only):
            raise APIError(400, "invalid_request", "external browsers are attach-only")
        if mode == "managed-local" and "profile_path" not in payload:
            raise APIError(400, "invalid_request", "managed-local browsers require profile_path")
        return payload

    def normalize_browser_endpoint_update(self, endpoint_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "cdp_url", "label", "is_default", "temporary_default",
            "rate_limit_scope", "max_active_tabs", "draining",
        }
        unknown = set(raw) - allowed
        if unknown or not raw:
            raise APIError(400, "invalid_request", "browser settings contain unsupported or empty fields")
        payload: dict[str, Any] = {"endpoint_id": self._browser_endpoint_id(endpoint_id)}
        for name in ("cdp_url", "label", "rate_limit_scope"):
            if name in raw:
                value = raw.get(name)
                if not isinstance(value, str) or not value.strip():
                    raise APIError(400, "invalid_request", f"{name} must be a non-empty string")
                payload[name] = value.strip()
        for name in ("is_default", "temporary_default"):
            if name in raw:
                if type(raw.get(name)) is not bool:
                    raise APIError(400, "invalid_request", f"{name} must be a boolean")
                payload[name] = raw[name]
        if "draining" in raw:
            if raw.get("draining") is not False:
                raise APIError(400, "invalid_request", "browser settings may only restore draining=false; use Remove to drain")
            payload["draining"] = False
        if "max_active_tabs" in raw:
            capacity = raw.get("max_active_tabs")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
                raise APIError(400, "invalid_request", "max_active_tabs must be a positive integer")
            payload["max_active_tabs"] = capacity
        return payload

    def normalize_independent_create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "system_prompt", "mode", "trigger_settings", "max_cycles", "temporary_chat", "browser_endpoint_id"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        mode = str(raw.get("mode") or "").strip()
        if mode.casefold() != "independent":
            raise APIError(400, "invalid_request", "mode must be Independent")
        try:
            name, _key, _team = normalize_agent_name(raw.get("name"))
            prompt = normalize_system_prompt(raw.get("system_prompt"))
            settings = (
                validate_trigger_settings(raw.get("trigger_settings"))
                if "trigger_settings" in raw
                else None
            )
            max_cycles = (
                normalize_max_cycles(raw.get("max_cycles"))
                if "max_cycles" in raw
                else None
            )
            temporary_chat = raw.get("temporary_chat", True)
            if not isinstance(temporary_chat, bool):
                raise ValueError("temporary_chat must be a boolean")
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        payload = {
            "name": name,
            "system_prompt": prompt,
            "mode": "Independent",
            "temporary_chat": temporary_chat,
        }
        if settings is not None:
            payload["trigger_settings"] = settings
        if max_cycles is not None:
            payload["max_cycles"] = max_cycles
        if "browser_endpoint_id" in raw:
            browser_endpoint_id = raw.get("browser_endpoint_id")
            payload["browser_endpoint_id"] = (
                None
                if browser_endpoint_id is None
                or (isinstance(browser_endpoint_id, str) and not browser_endpoint_id.strip())
                else self._resolved_browser_endpoint_id(browser_endpoint_id)
            )
        return payload

    def normalize_independent_completion(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {"outcome", "summary", "target_task_id", "repair_task_id"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        try:
            return normalize_completion_request(raw)
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc

    def normalize_independent_activation(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {"agent_name", "target_task_id"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        try:
            agent_name, _key, _team = normalize_agent_name(raw.get("agent_name"))
            target_task_id = validate_task_id(str(raw.get("target_task_id") or ""))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {"agent_name": agent_name, "target_task_id": target_task_id}

    def normalize_independent_bind_target(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {"event_key", "target_task_id"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        event_key = str(raw.get("event_key") or "").strip()
        if not event_key or len(event_key) > 1000:
            raise APIError(
                400,
                "invalid_request",
                "event_key must contain 1 to 1000 characters",
            )
        try:
            target_task_id = validate_task_id(str(raw.get("target_task_id") or ""))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {"event_key": event_key, "target_task_id": target_task_id}

    def normalize_independent_repair(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "root_cause",
            "disposition",
            "reason",
            "reproduction",
            "source_areas",
            "required_tests",
            "lesson",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        try:
            return RepairRequest.validate_proposal(
                root_cause=raw.get("root_cause"),
                disposition=raw.get("disposition"),
                reason=raw.get("reason"),
                reproduction=raw.get("reproduction"),
                source_areas=raw.get("source_areas"),
                required_tests=raw.get("required_tests"),
                lesson=raw.get("lesson"),
            )
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc

    def normalize_independent_task_control(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {"target_task_id", "action", "role", "reason", "confirmed"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        action = str(raw.get("action") or "").strip().lower()
        allowed_actions = {
            "resume",
            "retry",
            "restart_role",
            "open_tab",
            "new_chat",
            "route_plan",
            "safe_restore",
        }
        if action not in allowed_actions:
            raise APIError(400, "invalid_request", "unsupported independent task control")
        role_value = raw.get("role")
        role = str(role_value or "").strip().upper() or None
        if role is not None:
            try:
                role = validate_workflow_route_key(role)
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
        reason = str(raw.get("reason") or "").strip()
        if not reason or len(reason) > 1200:
            raise APIError(
                400,
                "invalid_request",
                "reason must contain 1 to 1200 characters",
            )
        confirmed = raw.get("confirmed", False)
        if not isinstance(confirmed, bool):
            raise APIError(400, "invalid_request", "confirmed must be a boolean")
        try:
            target_task_id = validate_task_id(str(raw.get("target_task_id") or ""))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {
            "target_task_id": target_task_id,
            "action": action,
            "role": role,
            "reason": reason,
            "confirmed": confirmed,
        }

    def normalize_independent_run(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(raw) - {
            "trigger_type",
            "instruction",
            "required_browser_endpoint_id",
        }
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        trigger_type = str(raw.get("trigger_type") or "manual").strip().lower()
        if trigger_type not in {"manual", "check_all"}:
            raise APIError(
                400,
                "invalid_request",
                "trigger_type must be manual or check_all",
            )
        payload = {"trigger_type": trigger_type}
        if "instruction" in raw:
            try:
                instruction = normalize_manual_instruction(raw.get("instruction"))
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
            if trigger_type != "manual":
                raise APIError(
                    400,
                    "invalid_request",
                    "instruction is valid only for a manual run",
                )
            payload["instruction"] = instruction
        if "required_browser_endpoint_id" in raw:
            if trigger_type != "manual":
                raise APIError(
                    400,
                    "invalid_request",
                    "required_browser_endpoint_id is valid only for a manual run",
                )
            payload["required_browser_endpoint_id"] = self._resolved_browser_endpoint_id(
                raw.get("required_browser_endpoint_id")
            )
        return payload

    def normalize_independent_settings(
        self, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        allowed = {
            "enabled",
            "display_name",
            "system_prompt",
            "trigger_settings",
            "new_chat_next_job",
            "max_cycles",
            "browser_endpoint_id",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        if not raw:
            raise APIError(400, "invalid_request", "at least one setting is required")
        payload: dict[str, Any] = {}
        if "display_name" in raw:
            try:
                payload["display_name"] = normalize_workflow_display_name(
                    raw["display_name"]
                )
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                raise APIError(400, "invalid_request", "enabled must be a boolean")
            payload["enabled"] = raw["enabled"]
        if "system_prompt" in raw:
            try:
                payload["system_prompt"] = normalize_system_prompt(raw["system_prompt"])
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
        if "trigger_settings" in raw:
            try:
                payload["trigger_settings"] = validate_trigger_settings(
                    raw["trigger_settings"]
                )
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
        if "max_cycles" in raw:
            try:
                payload["max_cycles"] = normalize_max_cycles(raw["max_cycles"])
            except ValueError as exc:
                raise APIError(400, "invalid_request", str(exc)) from exc
        if "browser_endpoint_id" in raw:
            browser_endpoint_id = raw.get("browser_endpoint_id")
            payload["browser_endpoint_id"] = (
                None
                if browser_endpoint_id is None
                or (isinstance(browser_endpoint_id, str) and not browser_endpoint_id.strip())
                else self._resolved_browser_endpoint_id(browser_endpoint_id)
            )
        if "new_chat_next_job" in raw:
            if not isinstance(raw["new_chat_next_job"], bool):
                raise APIError(
                    400, "invalid_request", "new_chat_next_job must be a boolean"
                )
            payload["new_chat_next_job"] = raw["new_chat_next_job"]
        return payload

    @staticmethod
    def normalize_goal_change(raw: Mapping[str, Any]) -> tuple[dict[str, Any], int | None]:
        if set(raw) - {"goal", "expected_task_version"}:
            raise APIError(400, "invalid_request", "goal change accepts only goal and expected_task_version")
        goal = raw.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise APIError(400, "invalid_request", "goal must not be blank")
        expected = raw.get("expected_task_version")
        if expected is not None:
            if isinstance(expected, bool):
                raise APIError(400, "invalid_request", "expected_task_version must be an integer")
            try:
                expected = int(expected)
            except (TypeError, ValueError) as exc:
                raise APIError(400, "invalid_request", "expected_task_version must be an integer") from exc
            if expected < 0:
                raise APIError(400, "invalid_request", "expected_task_version must be non-negative")
        return {"goal": goal}, expected

    @staticmethod
    def normalize_parent_removal(raw: Mapping[str, Any]) -> int:
        if set(raw) != {"expected_task_version"}:
            raise APIError(
                400,
                "invalid_request",
                "parent removal requires only expected_task_version",
            )
        expected = raw.get("expected_task_version")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise APIError(
                400,
                "invalid_request",
                "expected_task_version must be an integer",
            )
        if expected < 0:
            raise APIError(
                400,
                "invalid_request",
                "expected_task_version must be non-negative",
            )
        return expected

    def enqueue(
        self,
        *,
        idempotency_key: str,
        kind: str,
        task_id: str | None,
        payload: Mapping[str, Any],
        expected_task_version: int | None = None,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise APIError(400, "idempotency_key_required", "Idempotency-Key header is required")
        if len(key) > 200:
            raise APIError(400, "invalid_idempotency_key", "Idempotency-Key is too long")
        normalized_payload = dict(payload)
        if kind == "create_task" and task_id is None:
            task_id = generate_idempotent_task_id(
                str(normalized_payload.get("task") or ""), key
            )
        existing = self.db.get_command_by_idempotency_key(key)
        if existing is not None:
            same = (
                existing["kind"] == kind
                and existing["payload"] == normalized_payload
                and existing.get("expected_task_version") == expected_task_version
            )
            if kind != "create_task":
                same = same and existing.get("task_id") == task_id
            if not same:
                raise APIError(409, "idempotency_conflict", "Idempotency-Key belongs to another request")
            return self._fail_stale_resume(existing)
        command_id = f"cmd-{uuid.uuid4()}"
        try:
            command = self.db.enqueue_command(
                command_id=command_id,
                idempotency_key=key,
                kind=kind,
                task_id=task_id,
                expected_task_version=expected_task_version,
                payload=normalized_payload,
            )
            return self._fail_stale_resume(command)
        except IdempotencyConflict as exc:
            raise APIError(409, "idempotency_conflict", str(exc)) from exc

    def report_bytes(self, task_id: str, report_id: str, *, maintenance: bool) -> bytes:
        private = self.db.get_task_private(validate_task_id(task_id))
        if private is None:
            raise APIError(404, "task_not_found", "task does not exist")
        bucket = "maintenance_reports" if maintenance else "reports"
        locators = private.get(bucket)
        locator = locators.get(report_id) if isinstance(locators, Mapping) else None
        if not isinstance(locator, Mapping):
            raise APIError(404, "report_not_found", "report does not exist")
        if "content" in locator:
            data = str(locator.get("content") or "").encode("utf-8")
            expected_size = int(locator.get("size") or 0)
            expected_hash = str(locator.get("sha256") or "")
            if expected_size != len(data) or hashlib.sha256(data).hexdigest() != expected_hash:
                raise APIError(409, "report_changed", "report content changed after projection")
            return data
        availability = str(locator.get("availability") or "")
        if availability == "remote_unmirrored":
            raise APIError(
                409,
                "report_remote_unmirrored",
                "report is stored on a remote execution host and is not mirrored",
            )
        if availability == "unavailable":
            raise APIError(404, "report_not_found", "report is not locally available")

        raw_path = Path(str(locator.get("path") or "")).expanduser()
        control_repository = self.config.repository_root.resolve()
        control_plans_root = self.config.plans_root.resolve()
        plans_relative = control_plans_root.relative_to(control_repository)
        execution_repository = Path(
            str(locator.get("repository") or private.get("repository") or control_repository)
        ).expanduser().resolve()
        if not any(
            execution_repository.is_relative_to(root.resolve())
            for root in self.config.repository_allowed_roots
        ):
            raise APIError(403, "report_escape", "report repository is outside configured allowed roots")
        execution_plans_root = execution_repository / plans_relative
        team = str(locator.get("team") or "").strip()
        if not team and not maintenance:
            manifest_path = Path(str(private.get("manifest_path") or "")).expanduser()
            try:
                manifest_relative = Path(os.path.abspath(manifest_path)).relative_to(control_plans_root)
            except ValueError:
                manifest_relative = Path()
            if len(manifest_relative.parts) >= 2:
                team = manifest_relative.parts[0]
        if maintenance:
            containment_roots = (control_plans_root, execution_plans_root)
        else:
            if not team:
                raise APIError(409, "report_locator_invalid", "report locator is missing team identity")
            containment_roots = (execution_plans_root / team,)
        lexical_path = Path(
            os.path.abspath(raw_path if raw_path.is_absolute() else execution_repository / raw_path)
        )
        containment_root = next(
            (root for root in containment_roots if lexical_path.is_relative_to(root)),
            None,
        )
        if containment_root is None:
            raise APIError(403, "report_escape", "report path escapes the report team root")
        path_repository = (
            control_repository
            if maintenance and containment_root == control_plans_root
            else execution_repository
        )
        try:
            repository_relative = lexical_path.relative_to(path_repository)
        except ValueError as exc:
            raise APIError(403, "report_escape", "report path escapes the report repository") from exc
        current = path_repository
        for part in repository_relative.parts:
            current /= part
            if current.is_symlink():
                raise APIError(403, "report_symlink", "report symlinks are forbidden")
        try:
            candidate = lexical_path.resolve(strict=True)
            resolved_containment_root = containment_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise APIError(404, "report_not_found", "report does not exist") from exc
        if not candidate.is_relative_to(resolved_containment_root):
            raise APIError(403, "report_escape", "report path escapes the report team root")
        if not candidate.is_file():
            raise APIError(404, "report_not_found", "report does not exist")
        body = candidate.read_bytes()
        expected_size = int(locator.get("size") or -1)
        expected_hash = str(locator.get("sha256") or "")
        if expected_size < 0 or not expected_hash:
            raise APIError(409, "report_locator_invalid", "report locator is incomplete")
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_hash:
            raise APIError(409, "report_changed", "report changed after projection publication")
        return body


class DashboardAPIHandler(BaseHTTPRequestHandler):
    server_version = "CDPAAPI/1"

    @property
    def application(self) -> DashboardAPI:
        return self.server.application  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str = "application/json; charset=utf-8",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        if status != 304:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body and status != 304:
            self.wfile.write(body)

    def _json(self, status: int, value: Mapping[str, Any], *, headers=None) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            headers=headers,
        )

    def _error(self, error: APIError) -> None:
        self._json(error.status, {"error": {"code": error.code, "message": str(error)}})

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise APIError(400, "invalid_request", "invalid Content-Length") from exc
        if size <= 0 or size > 1024 * 1024:
            raise APIError(400, "invalid_request", "JSON body is required and must be <= 1 MiB")
        try:
            value = json.loads(self.rfile.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError(400, "invalid_json", "request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise APIError(400, "invalid_request", "request body must be an object")
        return value

    @staticmethod
    def _limit(
        query: Mapping[str, list[str]], default: int = 50, *, maximum: int = 100
    ) -> int:
        try:
            value = int(query.get("limit", [str(default)])[0])
        except ValueError as exc:
            raise APIError(400, "invalid_limit", "limit must be an integer") from exc
        if not 1 <= value <= maximum:
            raise APIError(
                400, "invalid_limit", f"limit must be between 1 and {maximum}"
            )
        return value

    @staticmethod
    def _live_role(query: Mapping[str, list[str]]) -> str | None:
        role = str(query.get("role", [""])[0] or "").strip().upper() or None
        if role is not None and not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", role):
            raise APIError(400, "invalid_role", "role is invalid")
        return role

    def _write_sse(self, event_type: str, data: Mapping[str, Any], *, event_id: str | None = None) -> None:
        lines: list[str] = []
        if event_id:
            lines.append(f"id: {event_id}")
        lines.append(f"event: {event_type}")
        lines.append(
            "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )
        self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_live(self, task_id: str, role: str | None) -> None:
        hub = self.application.live_stream_hub
        subscription, control = hub.subscribe(
            task_id,
            role,
            last_event_id=self.headers.get("Last-Event-ID"),
        )
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            if control is not None:
                self._write_sse(str(control["type"]), control)
            while not subscription.closed:
                try:
                    item = subscription.get(timeout=20.0)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if isinstance(item, LiveEnvelope):
                    self._write_sse(
                        item.type,
                        item.as_data(),
                        event_id=f"{item.stream_epoch}:{item.seq}",
                    )
                else:
                    self._write_sse(str(item.get("type") or "stream.reset"), item)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            hub.unsubscribe(subscription)

    def do_GET(self) -> None:
        try:
            self._get()
        except APIError as exc:
            self._error(exc)
        except ValueError as exc:
            self._error(APIError(400, "invalid_request", str(exc)))
        except RuntimeDBError as exc:
            self._error(APIError(500, "runtime_error", str(exc)))

    def _get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        app = self.application
        db = app.db
        if path == "/health":
            health = app.worker_health()
            self._json(
                200,
                {
                    "ok": True,
                    "database": "ok",
                    "pid": os.getpid(),
                    **health,
                },
            )
            return
        if path == "/api/dashboard-actions":
            snapshot = db.get_snapshot("dashboard_actions")
            version = int((snapshot or {}).get("version") or 0)
            etag = f'"dashboard-actions-{version}"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, headers={"ETag": etag})
                return
            payload = (
                dict(snapshot["payload"])
                if snapshot is not None
                else {
                    "degraded": True,
                    "dependency_teams": [],
                    "resume_teams": [],
                    "reuse_teams": [],
                }
            )
            self._json(200, payload, headers={"ETag": etag})
            return
        if path == "/api/bootstraps":
            entries = [
                entry
                for entry in BootstrapCatalog(app.config.repository_root).list()
                if entry.get("enabled") is True
            ]
            items = [
                {
                    "bootstrap_id": entry["bootstrap_id"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "tags": list(entry.get("tags") or ()),
                }
                for entry in entries
            ]
            default_id = app._default_bootstrap_id()
            self._json(
                200,
                {"items": items, "default_id": default_id},
                headers={"Cache-Control": "no-store"},
            )
            return
        if path == "/api/agents":
            snapshot = db.get_snapshot("agents")
            version = int((snapshot or {}).get("version") or 0)
            etag = f'"agents-{version}"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, headers={"ETag": etag})
                return
            payload = (
                dict(snapshot["payload"])
                if snapshot is not None
                else {"degraded": True, "workflow": [], "independent": []}
            )
            self._json(200, payload, headers={"ETag": etag})
            return
        if path == "/api/tasks":
            version = db.get_snapshot_version("board") or 0
            etag = f'"board-{version}"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, headers={"ETag": etag})
                return
            board = db.get_board()
            board["counts"] = {status: int(board["counts"].get(status, 0)) for status in STATUSES}
            self._json(200, board, headers={"ETag": etag})
            return
        if path == "/api/history":
            limit = self._limit(query)
            cursor = query.get("cursor", [None])[0]
            decoded = _cursor_decode(cursor) if cursor else None
            items = db.list_history(limit=limit, cursor=decoded)
            next_cursor = None
            if len(items) == limit:
                last = items[-1]
                next_cursor = _cursor_encode(
                    f"{last.get('updated_at', '')}\0{last.get('task_id', '')}"
                )
            self._json(200, {"items": items, "next_cursor": next_cursor})
            return
        if path == "/api/state":
            worker = db.get_snapshot("worker")
            browser = db.get_snapshot("browser")
            self._json(
                200,
                {
                    "worker": (worker or {}).get("payload") or {},
                    "browser": (browser or {}).get("payload") or {},
                    "settings": {},
                    "semantic_stale_after_seconds": app.config.response_refresh_after_seconds,
                    "auto_allow": mcp_allow_click_summary(),
                    **app.worker_health(),
                },
            )
            return
        if path == "/api/system":
            self._json(200, app.system_status(), headers={"Cache-Control": "no-store"})
            return
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "commands"]:
            command = db.get_command(parts[2])
            if command is None:
                raise APIError(404, "command_not_found", "command does not exist")
            command = app._fail_stale_resume(command)
            self._json(
                200,
                {
                    "command_id": command["command_id"],
                    "status": command["status"],
                    "task_id": command["task_id"],
                    "result": command["result"],
                    "error": command["error"],
                },
            )
            return
        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            task_id = validate_task_id(parts[2])
            version = db.get_task_version(task_id)
            if version is None:
                raise APIError(404, "task_not_found", "task does not exist")
            etag = f'"task-{task_id}-{version}"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, headers={"ETag": etag})
                return
            detail = db.get_task_detail(task_id)
            assert detail is not None
            self._json(200, detail, headers={"ETag": etag})
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "timeline":
            task_id = validate_task_id(parts[2])
            private = db.get_task_private(task_id)
            if private is None:
                raise APIError(404, "task_not_found", "task does not exist")
            timeline = private.get("timeline") if isinstance(private.get("timeline"), list) else []
            limit = self._limit(query)
            before = query.get("before", [None])[0]
            start = int(_cursor_decode(before)) if before else 0
            if start < 0 or start > len(timeline):
                raise APIError(400, "invalid_cursor", "timeline cursor is invalid")
            items = timeline[start : start + limit]
            next_cursor = _cursor_encode(str(start + limit)) if start + limit < len(timeline) else None
            self._json(200, {"items": items, "next_cursor": next_cursor})
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "tasks"]
            and parts[3:] == ["live", "stream"]
        ):
            task_id = validate_task_id(parts[2])
            if db.get_task_version(task_id) is None:
                raise APIError(404, "task_not_found", "task does not exist")
            self._stream_live(task_id, self._live_role(query))
            return
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "live":
            task_id = validate_task_id(parts[2])
            if db.get_task_version(task_id) is None:
                raise APIError(404, "task_not_found", "task does not exist")
            limit = self._limit(query, default=100, maximum=1000)
            role = self._live_role(query)
            items = read_recent_live_events(
                app.live_event_log,
                limit=limit,
                task_id=task_id,
                role=role,
            )
            self._json(
                200,
                {"task_id": task_id, "role": role, "items": items},
                headers={"Cache-Control": "no-store"},
            )
            return
        if len(parts) == 4 and parts[:2] == ["api", "reports"]:
            body = app.report_bytes(parts[2], parts[3], maintenance=False)
            self._send(200, body, content_type="text/markdown; charset=utf-8")
            return
        if len(parts) == 4 and parts[:2] == ["api", "maintenance-reports"]:
            body = app.report_bytes(parts[2], parts[3], maintenance=True)
            self._send(200, body, content_type="text/markdown; charset=utf-8")
            return
        raise APIError(404, "not_found", "endpoint does not exist")

    def do_POST(self) -> None:
        try:
            self._post()
        except APIError as exc:
            self._error(exc)
        except ValueError as exc:
            self._error(APIError(400, "invalid_request", str(exc)))

    def _post(self) -> None:
        path = urlsplit(self.path).path
        app = self.application
        raw = self._body()
        key = self.headers.get("Idempotency-Key", "")
        task_id: str | None = None
        if path == "/api/tasks":
            payload = app.normalize_create(raw)
            command = app.enqueue(
                idempotency_key=key,
                kind="create_task",
                task_id=None,
                payload=payload,
            )
        elif path == "/api/browser-endpoints":
            payload = app.normalize_browser_endpoint_add(raw)
            command = app.enqueue(
                idempotency_key=key,
                kind="browser_endpoint_add",
                task_id=None,
                payload=payload,
            )
        elif path == "/api/workflow-agents":
            payload = app.normalize_workflow_agent_create(raw)
            command = app.enqueue(
                idempotency_key=key,
                kind="create_workflow_agent",
                task_id=None,
                payload=payload,
            )
        elif path == "/api/independent-agents":
            payload = app.normalize_independent_create(raw)
            command = app.enqueue(
                idempotency_key=key,
                kind="create_independent_agent",
                task_id=None,
                payload=payload,
            )
        elif path == "/api/tasks/resume":
            team = str(raw.get("team") or "").strip()
            if not team:
                raise APIError(400, "invalid_request", "team must not be empty")
            command = app.enqueue(
                idempotency_key=key,
                kind="resume_team",
                task_id=None,
                payload={"team": team, "reason": raw.get("reason")},
            )
        elif path == "/api/runtime/reload":
            if raw:
                raise APIError(400, "invalid_request", "reload body must be empty")
            command = app.enqueue(
                idempotency_key=key,
                kind="reload_catalog",
                task_id=None,
                payload={},
            )
        else:
            parts = [unquote(part) for part in path.strip("/").split("/") if part]
            if len(parts) == 4 and parts[:2] == ["api", "browser-endpoints"]:
                endpoint_id = app._browser_endpoint_id(parts[2])
                operation = parts[3]
                if operation == "settings":
                    payload = app.normalize_browser_endpoint_update(endpoint_id, raw)
                    kind = "browser_endpoint_update"
                elif operation in {"remove", "start", "stop"}:
                    payload = {**app.normalize_empty(raw), "endpoint_id": endpoint_id}
                    kind = f"browser_endpoint_{operation}"
                else:
                    raise APIError(404, "not_found", "endpoint does not exist")
                command = app.enqueue(
                    idempotency_key=key,
                    kind=kind,
                    task_id=None,
                    payload=payload,
                )
            elif len(parts) == 4 and parts[:2] == ["api", "workflow-agents"]:
                route_key = validate_workflow_route_key(parts[2])
                operation = parts[3]
                if operation == "settings":
                    payload = app.normalize_workflow_agent_update(route_key, raw)
                    kind = "update_workflow_agent"
                elif operation == "delete":
                    payload = {**app.normalize_empty(raw), "route_key": route_key}
                    kind = "delete_workflow_agent"
                else:
                    raise APIError(404, "not_found", "endpoint does not exist")
                command = app.enqueue(
                    idempotency_key=key,
                    kind=kind,
                    task_id=None,
                    payload=payload,
                )
            elif len(parts) == 4 and parts[:2] == ["api", "independent-agents"]:
                task_id = validate_task_id(parts[2])
                operation = parts[3]
                if operation == "delete":
                    payload = app.normalize_empty(raw)
                    kind = "delete_independent_agent"
                elif operation == "complete":
                    payload = app.normalize_independent_completion(raw)
                    kind = "independent_complete"
                elif operation == "continue":
                    if set(raw) - {"reason"}:
                        raise APIError(400, "invalid_request", "continue accepts only reason")
                    reason = str(raw.get("reason") or "").strip()
                    if not reason:
                        raise APIError(400, "invalid_request", "reason must not be empty")
                    payload = {"reason": reason}
                    kind = "independent_continue"
                elif operation == "run":
                    payload = app.normalize_independent_run(raw)
                    kind = "independent_run_now"
                elif operation == "reset":
                    if set(raw) - {"reason"}:
                        raise APIError(400, "invalid_request", "reset accepts only reason")
                    reason = str(raw.get("reason") or "Operator reset").strip()
                    if not reason or len(reason) > 1200:
                        raise APIError(
                            400,
                            "invalid_request",
                            "reason must contain 1 to 1200 characters",
                        )
                    payload = {"reason": reason}
                    kind = "independent_reset"
                elif operation == "activate":
                    payload = app.normalize_independent_activation(raw)
                    kind = "independent_activate_agent"
                elif operation == "bind":
                    payload = app.normalize_independent_bind_target(raw)
                    kind = "independent_bind_target"
                elif operation == "repair":
                    payload = app.normalize_independent_repair(raw)
                    kind = "independent_create_repair"
                elif operation == "control":
                    payload = app.normalize_independent_task_control(raw)
                    kind = "independent_task_control"
                elif operation == "settings":
                    payload = app.normalize_independent_settings(raw)
                    kind = "independent_settings"
                else:
                    raise APIError(404, "not_found", "endpoint does not exist")
                command = app.enqueue(
                    idempotency_key=key,
                    kind=kind,
                    task_id=task_id,
                    payload=payload,
                )
            else:
                if (
                    len(parts) == 6
                    and parts[:2] == ["api", "tasks"]
                    and parts[3] == "parents"
                    and parts[5] == "remove"
                ):
                    task_id = validate_task_id(parts[2])
                    parent_task_id = validate_task_id(parts[4])
                    expected = app.normalize_parent_removal(raw)
                    command = app.enqueue(
                        idempotency_key=key,
                        kind="remove_parent_dependency",
                        task_id=task_id,
                        expected_task_version=expected,
                        payload={"parent_task_id": parent_task_id},
                    )
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "goal":
                    task_id = validate_task_id(parts[2])
                    payload, expected = app.normalize_goal_change(raw)
                    command = app.enqueue(
                        idempotency_key=key,
                        kind="change_goal",
                        task_id=task_id,
                        expected_task_version=expected,
                        payload=payload,
                    )
                else:
                    if len(parts) != 4 or parts[:2] != ["api", "tasks"] or parts[3] != "controls":
                        raise APIError(404, "not_found", "endpoint does not exist")
                    task_id = validate_task_id(parts[2])
                    action = str(raw.get("action") or "").strip().lower()
                    if not action:
                        raise APIError(400, "invalid_request", "action must not be empty")
                    expected = raw.get("expected_task_version")
                    if expected is not None:
                        try:
                            expected = int(expected)
                        except (TypeError, ValueError) as exc:
                            raise APIError(400, "invalid_request", "expected_task_version must be an integer") from exc
                    command = app.enqueue(
                        idempotency_key=key,
                        kind="task_control",
                        task_id=task_id,
                        expected_task_version=expected,
                        payload={
                            "action": action,
                            "role": raw.get("role"),
                            "reason": raw.get("reason"),
                            "confirmed": bool(raw.get("confirmed")),
                        },
                    )
        self._json(
            202,
            {
                "command_id": command["command_id"],
                "task_id": command["task_id"],
                "status": command["status"],
            },
        )


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    application: DashboardAPI

    def server_close(self) -> None:
        self.application.live_stream_hub.close()
        super().server_close()


def create_server(
    config: CDPAConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    db: RuntimeDB | None = None,
) -> APIServer:
    bind_host = host or config.dashboard_api_host
    if bind_host not in LOOPBACK:
        raise ValueError("CDPA API must bind to loopback")
    server = APIServer((bind_host, config.dashboard_api_port if port is None else port), DashboardAPIHandler)
    server.application = DashboardAPI(config, db=db)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the loopback CDPA dashboard API")
    parser.add_argument("--config", default=None)
    parser.add_argument("--repository", default=".")
    args = parser.parse_args(argv)
    config = load_cdpa_config(
        args.config,
        repository_root=Path(args.repository).expanduser().resolve(),
    )
    server = create_server(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        server.application.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
