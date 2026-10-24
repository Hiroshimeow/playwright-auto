from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..cdpa_commands import teamtask_trigger_idempotency_key
from ..cdpa_config import load_cdpa_config
from ..cdpa_provider_runtime import ProviderRuntimeError, select_provider, select_work_owner
from ..cdpa_role_policy import RolePolicyError, RolePolicyResult, build_role_policy
from ..cdpa_workflow_agents import (
    WorkflowAgentCatalog,
    semantic_routes_from_workflow_definitions,
)
from .audit import AuditTimeline
from .config import RuntimeConfig
from .state import RuntimeStateStore

_TEAM_TASK_FIELDS = frozenset(
    {
        "task",
        "team",
        "browser",
        "roles",
        "route_targets",
        "start_role",
        "closing_role",
        "max_routes",
        "max_route_repairs",
        "self_route_policy",
        "provider_id",
        "depends_on_task_ids",
        "workflow_mode",
    }
)
_LEGACY_PUBLIC_FIELDS = frozenset(
    {
        "solo_role",
        "role",
        "goal",
        "conversation_url",
        "task_mode",
        "independent",
        "independent_agent",
        "independent_agent_name",
    }
)
_DERIVED_TASK_FIELDS = frozenset(
    {
        "task_id",
        "submitted_roles",
        "reachable_roles",
        "route_contract_id",
        "semantic_routes",
        "self_route_counts",
        "route_seq",
        "active_role",
        "status",
        "created_at",
        "updated_at",
        "role_slots",
    }
)


class RuntimeTaskRunnerError(RuntimeError):
    """Raised when runtime-v2 cannot submit a real CDPA team task safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class TaskSubmissionRequest:
    """Validated Gate-A TeamTask request submitted through runtime-v2."""

    task: str
    team: str
    browser: str
    provider_id: str
    depends_on_task_ids: tuple[str, ...]
    policy: RolePolicyResult

    @property
    def roles(self) -> tuple[str, ...]:
        return self.policy.roles

    @property
    def start_role(self) -> str:
        return self.policy.start_role

    @property
    def closing_role(self) -> str:
        return self.policy.closing_role


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

    def submit_task(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(trigger, dict) and trigger.get("kind") == "child_teamtask":
            if select_work_owner(independent_route_contract=True).owner != "team_task":
                raise RuntimeTaskRunnerError(
                    "invalid_task_request",
                    "independent routed sub-work must be owned by a child TeamTask",
                )
        stable_key = _required_idempotency_key(idempotency_key)
        request = self._validate_payload(payload, idempotency_key=stable_key)
        existing = self._record_for_idempotency_key(stable_key)
        if existing is not None:
            if existing.get("request_fingerprint") != self._request_fingerprint(request):
                raise RuntimeTaskRunnerError(
                    "idempotency_conflict",
                    "runtime-v2 idempotency key belongs to another TeamTask request",
                )
            return existing
        runtime = self.ensure_legacy_runtime()
        result = self._submit_team_with_legacy_api(request, idempotency_key=stable_key)
        record = self._build_task_record(
            request, runtime, result, idempotency_key=stable_key, trigger=trigger
        )
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
                "trigger": trigger,
            },
        )
        return record

    def submit_cron_job(
        self, cron_id: str, scheduled_for: str, task_payload: dict[str, Any]
    ) -> dict[str, Any]:
        cron = _required_identity(cron_id, "cron_id")
        occurrence = _normalized_timestamp(scheduled_for, "scheduled_for")
        key = teamtask_trigger_idempotency_key("cron", cron, occurrence)
        return self.submit_task(
            task_payload,
            idempotency_key=key,
            trigger={"kind": "cron", "cron_id": cron, "scheduled_for": occurrence},
        )

    def submit_heartbeat_recovery(
        self,
        subject_kind: str,
        subject_id: str,
        stale_episode_started_at: str,
        task_payload: dict[str, Any],
    ) -> dict[str, Any]:
        kind = _required_identity(subject_kind, "subject_kind")
        subject = _required_identity(subject_id, "subject_id")
        episode = _normalized_timestamp(stale_episode_started_at, "stale_episode_started_at")
        key = teamtask_trigger_idempotency_key(
            "heartbeat", subject, episode, subject_kind=kind
        )
        return self.submit_task(
            task_payload,
            idempotency_key=key,
            trigger={
                "kind": "stale_heartbeat",
                "subject_kind": kind,
                "subject_id": subject,
                "stale_episode_started_at": episode,
            },
        )

    def _record_for_idempotency_key(self, key: str) -> dict[str, Any] | None:
        matches = [
            record
            for record in self.state_store.list_task_records()
            if record.get("idempotency_key") == key
        ]
        if len(matches) > 1:
            raise RuntimeTaskRunnerError(
                "state_conflict", f"duplicate runtime-v2 idempotency record: {key}"
            )
        return matches[0] if matches else None

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

    def _validate_payload(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> TaskSubmissionRequest:
        supplied = set(payload)
        legacy_fields = sorted(supplied & _LEGACY_PUBLIC_FIELDS)
        derived_fields = sorted(supplied & _DERIVED_TASK_FIELDS)
        unsupported = sorted(supplied - _TEAM_TASK_FIELDS - _LEGACY_PUBLIC_FIELDS - _DERIVED_TASK_FIELDS)
        if legacy_fields:
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                "runtime-v2 accepts Team Task only; migrate Solo Task to roles=[<one role>] "
                f"and Independent Agent work to TeamTask/CronJob. Legacy fields: {legacy_fields!r}",
            )
        if derived_fields:
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                f"runtime-derived TeamTask fields are not client-writable: {derived_fields!r}",
            )
        if unsupported:
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                f"runtime-v2 accepts TeamTask only; unsupported fields: {unsupported!r}",
            )
        if "workflow_mode" in payload and str(payload.get("workflow_mode")).strip().lower() != "team":
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                "runtime-v2 accepts Team Task only; use roles=[<one role>] instead of Solo Task",
            )
        task = str(payload.get("task") or "").strip()
        if not task:
            raise RuntimeTaskRunnerError("invalid_task_request", "task is required")
        team = str(
            payload.get("team")
            or (
                _default_team_for_idempotency_key(idempotency_key)
                if idempotency_key
                else f"hcuw-v2-team-{uuid.uuid4().hex[:8]}"
            )
        ).strip()
        browser = str(payload.get("browser") or "9222").strip()
        provider_id = str(payload.get("provider_id") or "chatgpt_web").strip()
        if not team:
            raise RuntimeTaskRunnerError("invalid_task_request", "team is required")
        if not browser:
            raise RuntimeTaskRunnerError("invalid_task_request", "browser is required")
        try:
            provider = select_provider(
                explicit_provider_id=provider_id, required_capabilities=("browser",)
            )
        except ProviderRuntimeError as exc:
            raise RuntimeTaskRunnerError(
                "invalid_task_request",
                "TeamTask role turns require a browser provider; non-browser execution belongs to ProviderJob",
            ) from exc
        provider_id = provider.provider_id

        _definitions, semantic_routes, default_repairs = self._role_policy_source()
        try:
            policy = build_role_policy(
                payload.get("roles"),
                route_targets=payload.get("route_targets") or (),
                semantic_routes=semantic_routes,
                start_role=payload.get("start_role"),
                closing_role=payload.get("closing_role"),
                max_routes=payload.get("max_routes", 8),
                max_route_repairs=payload.get("max_route_repairs", default_repairs),
                self_route_policy=payload.get("self_route_policy", "forbidden"),
            )
        except RolePolicyError as exc:
            raise RuntimeTaskRunnerError("invalid_task_request", f"{exc.code}: {exc.message}") from exc
        dependencies = payload.get("depends_on_task_ids") or []
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item.strip() for item in dependencies
        ):
            raise RuntimeTaskRunnerError(
                "invalid_task_request", "depends_on_task_ids must be an array of non-empty TeamTask ids"
            )
        if len(set(dependencies)) != len(dependencies):
            raise RuntimeTaskRunnerError("invalid_task_request", "depends_on_task_ids must not contain duplicates")
        return TaskSubmissionRequest(
            task=task,
            team=team,
            browser=browser,
            provider_id=provider_id,
            depends_on_task_ids=tuple(item.strip() for item in dependencies),
            policy=policy,
        )

    def _role_policy_source(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]], int]:
        config_path = (
            self.config.endpoint_config_path
            if self.config.endpoint_config_path.is_file()
            else None
        )
        config = load_cdpa_config(
            config_path,
            repository_root=self.config.repository_root,
        )
        catalog = WorkflowAgentCatalog(config)
        route_keys = tuple(item["route_key"] for item in catalog.list_agents())
        definitions = catalog.resolve(route_keys)
        semantic_routes = semantic_routes_from_workflow_definitions(definitions)
        return definitions, semantic_routes, int(config.route_repair_attempts)

    def _legacy_api_is_healthy(self) -> bool:
        try:
            with urlopen(Request(f"{self._legacy_api_url()}/health", method="GET"), timeout=1.0) as response:
                health = json.load(response)
        except (URLError, OSError):
            return False
        except (ValueError, UnicodeError) as exc:
            raise RuntimeTaskRunnerError(
                "legacy_runtime_incompatible", "legacy health response is not valid JSON"
            ) from exc
        # HTTP 200 alone cannot prove this is our backend or that obsolete
        # Independent Agent execution has been disabled on an existing process.
        if not isinstance(health, dict) or health.get("ok") is not True:
            raise RuntimeTaskRunnerError(
                "legacy_runtime_incompatible", "legacy health contract is incomplete"
            )
        if health.get("independent_activation_enabled") is not False:
            raise RuntimeTaskRunnerError(
                "legacy_runtime_incompatible", "legacy backend must disable Independent Agent execution"
            )
        repository = health.get("repository_root")
        try:
            matches_repository = (
                isinstance(repository, str)
                and Path(repository).resolve() == self.config.repository_root
            )
        except (OSError, ValueError):
            matches_repository = False
        if not matches_repository:
            raise RuntimeTaskRunnerError(
                "legacy_runtime_incompatible", "legacy backend owns a different or unknown repository"
            )
        if health.get("worker_reload_required") is True:
            raise RuntimeTaskRunnerError(
                "legacy_runtime_source_drift", "legacy worker source drift requires an operator restart"
            )
        return True

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

    def _submit_team_with_legacy_api(
        self, request: TaskSubmissionRequest, *, idempotency_key: str
    ) -> dict[str, Any]:
        payload = {
            "workflow_mode": "team",
            "task": request.task,
            "requested_team": request.team,
            "repository": str(self.config.repository_root),
            "browser_endpoint_id": request.browser,
            "roles": list(request.roles),
            "route_targets": list(request.policy.route_targets),
            "start_role": request.start_role,
            "closing_role": request.closing_role,
            "provider_id": request.provider_id,
            "max_routes": request.policy.max_routes,
            "max_route_repairs": request.policy.max_route_repairs,
            "self_route_policy": request.policy.self_route_policy,
            "depends_on_task_ids": list(request.depends_on_task_ids),
            "report_mode": "file",
        }
        command = self._post_legacy_api(
            "/api/tasks", payload, idempotency_key=idempotency_key
        )
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

    def _post_legacy_api(
        self, path: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._legacy_api_url()}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
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
        *,
        idempotency_key: str | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed = dict(result.get("parsed") or {})
        now = _utc_now()
        return {
            "runtime_task_id": (
                f"rtask-{hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:16]}"
                if idempotency_key
                else f"rtask-{uuid.uuid4().hex[:12]}"
            ),
            "idempotency_key": idempotency_key,
            "request_fingerprint": self._request_fingerprint(request),
            "trigger": trigger,
            "mode": "team",
            "status": "submitted",
            "created_at": now,
            "updated_at": now,
            "team": request.team,
            "browser": request.browser,
            "provider_id": request.provider_id,
            "submitted_roles": list(request.policy.submitted_roles),
            "roles": list(request.roles),
            "route_targets": list(request.policy.route_targets),
            "reachable_roles": list(request.policy.reachable_roles),
            "start_role": request.start_role,
            "closing_role": request.closing_role,
            "route_contract_id": request.policy.route_contract_id,
            "semantic_routes": request.policy.semantic_routes,
            "max_routes": request.policy.max_routes,
            "max_route_repairs": request.policy.max_route_repairs,
            "route_seq": 0,
            "self_route_policy": request.policy.self_route_policy,
            "self_route_counts": {role: 0 for role in request.policy.reachable_roles},
            "role_slots": request.policy.initial_role_slots(),
            "depends_on_task_ids": list(request.depends_on_task_ids),
            "task": request.task,
            "legacy_runtime": runtime,
            "legacy_task_id": parsed.get("task_id"),
            "legacy_command_id": parsed.get("command_id"),
            "legacy_status": parsed.get("status"),
            "submit_stdout": result.get("stdout", ""),
            "submit_stderr": result.get("stderr", ""),
            "submit_command": result.get("command", []),
        }

    @staticmethod
    def _request_fingerprint(request: TaskSubmissionRequest) -> str:
        payload = {
            "task": request.task,
            "team": request.team,
            "browser": request.browser,
            "provider_id": request.provider_id,
            "depends_on_task_ids": list(request.depends_on_task_ids),
            "roles": list(request.roles),
            "route_targets": list(request.policy.route_targets),
            "start_role": request.start_role,
            "closing_role": request.closing_role,
            "max_routes": request.policy.max_routes,
            "max_route_repairs": request.policy.max_route_repairs,
            "self_route_policy": request.policy.self_route_policy,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

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
        independent_agents["activation_enabled"] = False
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


def _required_identity(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200:
        raise RuntimeTaskRunnerError(
            "invalid_task_request", f"{field} must contain 1 to 200 characters"
        )
    return text


def _required_idempotency_key(value: object) -> str:
    key = str(value or "").strip()
    if not key:
        raise RuntimeTaskRunnerError(
            "idempotency_key_required", "Idempotency-Key is required for TeamTask submission"
        )
    if len(key) > 200:
        raise RuntimeTaskRunnerError(
            "invalid_idempotency_key", "Idempotency-Key must contain at most 200 characters"
        )
    return key


def _default_team_for_idempotency_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
    return f"hcuw-v2-team-{digest}"


def _normalized_timestamp(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeTaskRunnerError(
            "invalid_task_request", f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


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

