from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..cdpa_workflow_agents import workflow_closing_role
from .audit import AuditTimeline
from .config import RuntimeConfig
from .state import RuntimeStateStore

_TEAM_ROLE_ORDER = ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT")
_TEAM_TASK_FIELDS = frozenset({"task", "team", "browser", "roles", "start_role", "workflow_mode"})


class RuntimeTaskRunnerError(RuntimeError):
    """Raised when runtime-v2 cannot submit a real CDPA team task safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class TaskSubmissionRequest:
    """Validated request for a real CDPA team task submitted through runtime-v2."""

    task: str
    team: str
    browser: str
    start_role: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeTaskRunner:
    """Owner of runtime-v2 real team-task submission orchestration.

    Runtime-v2 no longer submits Solo Task or Independent Agent work. This owner
    submits only Team Task requests through the existing CDPA API while legacy
    CDPA still owns current Send/bind/route/browser execution.
    """

    config: RuntimeConfig
    state_store: RuntimeStateStore
    audit_timeline: AuditTimeline

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._validate_payload(payload)
        runtime = self.ensure_legacy_runtime()
        result = self._submit_team_with_legacy_api(request)
        record = self._build_task_record(request, runtime, result)
        self.state_store.save_task_record(record)
        self.audit_timeline.record_event(
            source="actions",
            kind="team_task_submitted",
            message=f"Submitted real CDPA team task {record['runtime_task_id']}",
            payload={
                "runtime_task_id": record["runtime_task_id"],
                "legacy_task_id": record.get("legacy_task_id"),
                "team": request.team,
                "browser": request.browser,
                "roles": list(request.roles),
                "start_role": request.start_role,
            },
        )
        return record

    def ensure_legacy_runtime(self) -> dict[str, Any]:
        if self._legacy_api_is_healthy():
            return {"status": "already_running", "api": self._legacy_api_url()}
        if self._legacy_api_port_is_bound():
            # A slow legacy API still owns the port; a second runtime would only fail to bind.
            self._wait_for_legacy_api(None)
            return {"status": "already_running", "api": self._legacy_api_url()}
        process = self._start_legacy_runtime_process()
        self._wait_for_legacy_api(process.pid)
        return {"status": "started", "api": self._legacy_api_url(), "pid": process.pid}

    def _validate_payload(self, payload: dict[str, Any]) -> TaskSubmissionRequest:
        unsupported = sorted(set(payload) - _TEAM_TASK_FIELDS)
        if unsupported:
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                f"runtime-v2 accepts Team Task only; unsupported fields: {unsupported!r}",
            )
        if "workflow_mode" in payload and str(payload.get("workflow_mode")).lower() != "team":
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                "runtime-v2 accepts Team Task only; solo/independent modes are disabled",
            )
        task = str(payload.get("task") or "").strip()
        if not task:
            raise RuntimeTaskRunnerError("invalid_task_request", "task is required")
        team = str(payload.get("team") or f"hcuw-v2-team-{uuid.uuid4().hex[:8]}").strip()
        browser = str(payload.get("browser") or "9222").strip()
        if not team:
            raise RuntimeTaskRunnerError("invalid_task_request", "team is required")
        roles = self._validate_roles(payload.get("roles"))
        start_role = str(payload.get("start_role") or roles[0]).strip().upper()
        if start_role not in roles:
            raise RuntimeTaskRunnerError(
                "invalid_task_request", "start_role must be included in roles"
            )
        return TaskSubmissionRequest(
            task=task,
            team=team,
            browser=browser,
            start_role=start_role,
            roles=roles,
        )

    def _validate_roles(self, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list) or not value:
            raise RuntimeTaskRunnerError(
                "invalid_task_request", "roles must be a non-empty array of workflow roles"
            )
        requested = [str(item).strip().upper() for item in value]
        if len(set(requested)) != len(requested):
            raise RuntimeTaskRunnerError("invalid_task_request", "roles must not contain duplicates")
        unknown = set(requested) - set(_TEAM_ROLE_ORDER)
        if unknown:
            raise RuntimeTaskRunnerError("invalid_task_request", f"unknown roles: {sorted(unknown)!r}")
        if workflow_closing_role(requested) is None:
            raise RuntimeTaskRunnerError(
                "invalid_task_request", "roles must include PLAN or REVIEW to close the task"
            )
        return tuple(role for role in _TEAM_ROLE_ORDER if role in requested)

    def _legacy_api_is_healthy(self) -> bool:
        try:
            with urlopen(Request(f"{self._legacy_api_url()}/health", method="GET"), timeout=1.0):
                return True
        except URLError:
            return False
        except OSError:
            return False

    def _legacy_api_port_is_bound(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.config.legacy_api_port), timeout=1.0):
                return True
        except OSError:
            return False

    def _start_legacy_runtime_process(self) -> subprocess.Popen[Any]:
        log_path = self.state_store.task_runner_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
        command = [
            sys.executable,
            "-m",
            "playwright_auto.cdpa_cli",
            "start",
            "--repository",
            str(self.config.repository_root),
            "--config",
            self._legacy_config_path(),
            "--no-open",
        ]
        process = subprocess.Popen(
            command,
            cwd=self.config.repository_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        self.state_store.save_task_runner_process(
            {
                "pid": process.pid,
                "command": command,
                "log_path": str(log_path),
                "started_at": _utc_now(),
            }
        )
        self.audit_timeline.record_event(
            source="actions",
            kind="legacy_runtime_start",
            message="Started legacy CDPA runtime for real team task execution",
            payload={"pid": process.pid, "api": self._legacy_api_url()},
        )
        return process

    def _wait_for_legacy_api(self, pid: int | None) -> None:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self._legacy_api_is_healthy():
                return
            time.sleep(0.5)
        owner = f"after starting pid {pid}" if pid is not None else "while its port stayed bound"
        raise RuntimeTaskRunnerError(
            "legacy_runtime_unavailable",
            f"legacy CDPA API did not become healthy {owner}",
        )

    def _submit_team_with_legacy_api(self, request: TaskSubmissionRequest) -> dict[str, Any]:
        payload = {
            "workflow_mode": "team",
            "task": request.task,
            "requested_team": request.team,
            "repository": str(self.config.repository_root),
            "browser_endpoint_id": request.browser,
            "roles": list(request.roles),
            "start_role": request.start_role,
            "report_mode": "file",
        }
        command = self._post_legacy_api("/api/tasks", payload)
        return {
            "command": ["POST", f"{self._legacy_api_url()}/api/tasks"],
            "returncode": 0,
            "stdout": json.dumps(command, ensure_ascii=False, sort_keys=True),
            "stderr": "",
            "parsed": {
                "command_id": str(command.get("command_id") or ""),
                "task_id": str(command.get("task_id") or ""),
                "status": str(command.get("status") or ""),
            },
        }

    def _post_legacy_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._legacy_api_url()}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            raise RuntimeTaskRunnerError(
                "team_task_submit_failed",
                _legacy_error_message(raw) or exc.reason,
            ) from exc
        except URLError as exc:
            raise RuntimeTaskRunnerError("team_task_submit_failed", str(exc.reason)) from exc
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeTaskRunnerError("team_task_submit_failed", "legacy API returned non-object")
        return value

    def _build_task_record(
        self,
        request: TaskSubmissionRequest,
        runtime: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        parsed = dict(result.get("parsed") or {})
        now = _utc_now()
        return {
            "runtime_task_id": f"rtask-{uuid.uuid4().hex[:12]}",
            "mode": "team",
            "status": "submitted",
            "created_at": now,
            "updated_at": now,
            "team": request.team,
            "browser": request.browser,
            "roles": list(request.roles),
            "start_role": request.start_role,
            "task": request.task,
            "legacy_runtime": runtime,
            "legacy_task_id": parsed.get("task_id"),
            "legacy_command_id": parsed.get("command_id"),
            "legacy_status": parsed.get("status"),
            "submit_stdout": result.get("stdout", ""),
            "submit_stderr": result.get("stderr", ""),
            "submit_command": result.get("command", []),
        }

    def _legacy_config_path(self) -> str:
        bridge_path = self.state_store.task_runner_dir() / "legacy-cdpa-bridge.json"
        bridge_path.parent.mkdir(parents=True, exist_ok=True)
        source = self._read_source_config()
        repositories = dict(source.get("repositories") or {})
        allowed_roots = list(repositories.get("allowed_roots") or [])
        repository_root = str(self.config.repository_root)
        if repository_root not in allowed_roots:
            allowed_roots.append(repository_root)
        repositories["allowed_roots"] = allowed_roots
        source["repositories"] = repositories
        independent_agents = dict(source.get("independent_agents") or {})
        independent_agents["seed_builtins"] = False
        source["independent_agents"] = independent_agents
        bridge_path.write_text(
            json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return str(bridge_path)

    def _read_source_config(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config.endpoint_config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeTaskRunnerError("legacy_config_bridge_failed", str(exc)) from exc
        if not isinstance(value, dict):
            raise RuntimeTaskRunnerError(
                "legacy_config_bridge_failed",
                "legacy config bridge source must be a JSON object",
            )
        return value

    def _legacy_api_url(self) -> str:
        return f"http://127.0.0.1:{self.config.legacy_api_port}"


def _legacy_error_message(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")
    if not isinstance(value, dict):
        return str(value)
    error = value.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

