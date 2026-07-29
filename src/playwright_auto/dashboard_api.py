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

from .cdpa_commands import RepairRequest
from .cdpa_config import CDPAConfig, load_cdpa_config
from .cdpa_identity import generate_idempotent_task_id, validate_task_id
from .cdpa_independent import (
    normalize_agent_name,
    normalize_completion_request,
    normalize_manual_instruction,
    normalize_system_prompt,
    validate_trigger_settings,
)
from .cdpa_runtime_db import IdempotencyConflict, RuntimeDB, RuntimeDBError

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
    def _list(value: object, field: str) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise APIError(400, "invalid_request", f"{field} must be an array")
        return value

    def normalize_create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "task",
            "requested_team",
            "reuse_team",
            "new_roles",
            "new_all",
            "repository",
            "report_mode",
            "depends_on_task_ids",
            "upload_paths",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        task = str(raw.get("task") or "").strip()
        if not task:
            raise APIError(400, "invalid_request", "task must not be empty")
        return {
            "task": task,
            "requested_team": raw.get("requested_team"),
            "reuse_team": raw.get("reuse_team") or None,
            "new_roles": self._list(raw.get("new_roles"), "new_roles"),
            "new_all": bool(raw.get("new_all")),
            "repository": self._repository(raw.get("repository")),
            "report_mode": str(raw.get("report_mode") or "file"),
            "depends_on_task_ids": self._list(
                raw.get("depends_on_task_ids"), "depends_on_task_ids"
            ),
            "upload_paths": self._list(raw.get("upload_paths"), "upload_paths"),
        }

    def normalize_independent_create(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"name", "system_prompt", "mode"}
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        mode = str(raw.get("mode") or "").strip()
        if mode.casefold() != "independent":
            raise APIError(400, "invalid_request", "mode must be Independent")
        try:
            name, _key, _team = normalize_agent_name(raw.get("name"))
            prompt = normalize_system_prompt(raw.get("system_prompt"))
        except ValueError as exc:
            raise APIError(400, "invalid_request", str(exc)) from exc
        return {"name": name, "system_prompt": prompt, "mode": "Independent"}

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
        if role is not None and role not in self.config.roles:
            raise APIError(400, "invalid_request", "unsupported control role")
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
            "system_prompt",
            "trigger_settings",
            "new_chat_next_job",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise APIError(400, "invalid_request", f"unknown fields: {sorted(unknown)!r}")
        if not raw:
            raise APIError(400, "invalid_request", "at least one setting is required")
        payload: dict[str, Any] = {}
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
        raw_path = Path(str(locator.get("path") or "")).expanduser()
        plans_root = self.config.plans_root.resolve()
        lexical_path = Path(os.path.abspath(raw_path))
        try:
            lexical_relative = lexical_path.relative_to(plans_root)
        except ValueError as exc:
            raise APIError(403, "report_escape", "report path escapes the plans root") from exc
        current = plans_root
        for part in lexical_relative.parts:
            current /= part
            if current.is_symlink():
                raise APIError(403, "report_symlink", "report symlinks are forbidden")
        try:
            candidate = lexical_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise APIError(404, "report_not_found", "report does not exist") from exc
        if not candidate.is_relative_to(plans_root):
            raise APIError(403, "report_escape", "report path escapes the plans root")
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
            if len(parts) == 4 and parts[:2] == ["api", "independent-agents"]:
                task_id = validate_task_id(parts[2])
                operation = parts[3]
                if operation == "complete":
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
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "goal":
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
