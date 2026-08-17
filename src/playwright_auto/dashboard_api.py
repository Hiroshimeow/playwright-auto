from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .cdpa_bootstraps import BootstrapCatalog, normalize_bootstrap_record
from .cdpa_commands import RepairRequest
from .cdpa_config import CDPAConfig, declared_repository_from_task, load_cdpa_config
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
from .cdpa_workflow_agents import (
    normalize_workflow_display_name,
    ordered_system_routes,
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
        self.db = db or RuntimeDB(config.runtime_database)
        self.db.ensure_schema()
        self._system_lock = threading.Lock()
        self._previous_cpu = self._read_cpu()

    @staticmethod
    def _read_cpu() -> tuple[int, int] | None:
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
            disk = os.statvfs(self.config.repository_root)
            disk_free_bytes: int | None = disk.f_bavail * disk.f_frsize
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
        return {
            "worker_online": worker is not None and not stale,
            "worker_stale": stale,
            "worker_heartbeat_at": heartbeat,
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
        record = BootstrapCatalog(self.config.repository_root).get("general-team-bootstrap")
        return "general-team-bootstrap" if record and record.get("enabled") is True else None

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
        if "PLAN" not in roles:
            raise APIError(400, "invalid_request", "roles must include PLAN")
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
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        task = str(raw.get("task") or "").strip()
        if not task:
            raise APIError(400, "invalid_request", "task must not be empty")
        requested_repository = raw.get("repository")
        if requested_repository is None or (
            isinstance(requested_repository, str) and not requested_repository.strip()
        ):
            requested_repository = self._declared_repository(task)
        repository = self._repository(requested_repository)
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
        if "roles" in raw:
            normalized["roles"] = self._workflow_roles(raw.get("roles"))
        definition = (
            self._bootstrap_definition(raw.get("bootstrap_definition"))
            if "bootstrap_definition" in raw
            else None
        )
        if definition is not None:
            requested_id = raw.get("bootstrap_id")
            if requested_id not in {None, definition["bootstrap_id"]}:
                raise APIError(
                    400,
                    "invalid_request",
                    "bootstrap_id must match bootstrap_definition.bootstrap_id",
                )
            normalized["bootstrap_id"] = definition["bootstrap_id"]
            normalized["bootstrap_definition"] = definition
        elif "bootstrap_id" not in raw:
            normalized["bootstrap_id"] = self._default_bootstrap_id()
        elif raw.get("bootstrap_id") is None:
            normalized["bootstrap_id"] = None
        else:
            bootstrap_id = raw.get("bootstrap_id")
            if not isinstance(bootstrap_id, str) or not bootstrap_id.strip():
                raise APIError(
                    400,
                    "invalid_request",
                    "bootstrap_id must be a non-empty string or null",
                )
            normalized["bootstrap_id"] = bootstrap_id
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

    def normalize_independent_create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "system_prompt", "mode", "trigger_settings", "max_cycles", "temporary_chat"}
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
        unknown = set(raw) - {"trigger_type", "instruction"}
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
    def _limit(query: Mapping[str, list[str]], default: int = 50) -> int:
        try:
            value = int(query.get("limit", [str(default)])[0])
        except ValueError as exc:
            raise APIError(400, "invalid_limit", "limit must be an integer") from exc
        if not 1 <= value <= 100:
            raise APIError(400, "invalid_limit", "limit must be between 1 and 100")
        return value

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
            if len(parts) == 4 and parts[:2] == ["api", "workflow-agents"]:
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
