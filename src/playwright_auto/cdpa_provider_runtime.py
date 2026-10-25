from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableMapping, Protocol, Sequence

from .cdpa_endpoints import BrowserEndpointRegistry
from .cdpa_materials import register_material
from .role_runtime.controller import RoleController

EXECUTION_MODES = frozenset({"browser_dom", "acp", "exec", "http", "custom"})
PROVIDER_JOB_STATES = frozenset(
    {"QUEUED", "RUNNING", "SUCCEEDED", "TIMEOUT", "FAILED", "CANCELLED", "PARTIAL"}
)
TERMINAL_PROVIDER_JOB_STATES = PROVIDER_JOB_STATES - {"QUEUED", "RUNNING"}
_RESULT_STATUS = {
    "SUCCEEDED": "succeeded",
    "TIMEOUT": "timeout",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "PARTIAL": "partial",
}


class ProviderRuntimeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderRegistryEntry:
    provider_id: str
    module_path: str
    execution_modes: tuple[str, ...]
    capabilities: frozenset[str] = frozenset()
    enabled: bool = True
    default_timeout_seconds: int = 900
    default_output_limit_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class ProviderInvocation:
    provider_id: str
    task_id: str
    role_key: str
    task_text: str
    execution_mode: str
    material_refs: tuple[str, ...] = ()
    workspace: str | None = None
    timeout_seconds: int | None = None
    output_limit_bytes: int | None = None
    environment_policy: str = "inherit-safe"
    endpoint_id: str | None = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProviderInvocation":
        refs = raw.get("material_refs") or ()
        if not isinstance(refs, (list, tuple)):
            raise ProviderRuntimeError("PROVIDER_INVOCATION_INVALID", "material_refs must be an array")
        options = raw.get("provider_options") or {}
        if not isinstance(options, Mapping):
            raise ProviderRuntimeError("PROVIDER_INVOCATION_INVALID", "provider_options must be an object")
        return cls(
            provider_id=str(raw.get("provider_id") or "").strip(),
            task_id=str(raw.get("task_id") or "").strip(),
            role_key=str(raw.get("role_key") or "").strip().upper(),
            task_text=str(raw.get("task_text") or ""),
            execution_mode=str(raw.get("execution_mode") or "").strip().lower(),
            material_refs=tuple(str(item) for item in refs),
            workspace=(str(raw["workspace"]).strip() if raw.get("workspace") else None),
            timeout_seconds=_optional_positive_int(raw.get("timeout_seconds"), "timeout_seconds"),
            output_limit_bytes=_optional_positive_int(raw.get("output_limit_bytes"), "output_limit_bytes"),
            environment_policy=str(raw.get("environment_policy") or "inherit-safe"),
            endpoint_id=(str(raw["endpoint_id"]).strip() if raw.get("endpoint_id") else None),
            provider_options=dict(options),
        )


@dataclass(frozen=True)
class ProviderResult:
    provider_job_id: str
    provider_id: str
    status: str
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    result_ref: str | None = None
    error: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderWorkSelection:
    owner: str
    reason: str


class ProviderJobStore(Protocol):
    def load_task_id(self, task_id: str) -> dict[str, Any] | None: ...

    def update(self, path: str, mutator: Any) -> dict[str, Any]: ...


DEFAULT_PROVIDER_REGISTRY: dict[str, ProviderRegistryEntry] = {
    "chatgpt_web": ProviderRegistryEntry(
        provider_id="chatgpt_web",
        module_path="playwright_auto.role_runtime.controller:RoleController",
        execution_modes=("browser_dom",),
        capabilities=frozenset({"browser", "material_api", "adopted_url", "direct_chat"}),
        default_timeout_seconds=7200,
    ),
    "codex": ProviderRegistryEntry(
        provider_id="codex",
        module_path="playwright_auto.cdpa_provider_runtime",
        execution_modes=("acp", "exec"),
        capabilities=frozenset({"coding", "material_api"}),
        default_timeout_seconds=1800,
    ),
    "pi": ProviderRegistryEntry(
        provider_id="pi",
        module_path="playwright_auto.cdpa_provider_runtime",
        execution_modes=("exec",),
        capabilities=frozenset({"long_timeout", "material_api"}),
        default_timeout_seconds=25200,
    ),
    "custom": ProviderRegistryEntry(
        provider_id="custom",
        module_path="playwright_auto.cdpa_provider_runtime",
        execution_modes=("exec", "http", "custom"),
        capabilities=frozenset({"material_api"}),
        default_timeout_seconds=1800,
    ),
}


def select_provider(
    registry: Mapping[str, ProviderRegistryEntry] = DEFAULT_PROVIDER_REGISTRY,
    *,
    explicit_provider_id: str | None = None,
    trigger_provider_id: str | None = None,
    required_capabilities: Sequence[str] = (),
    execution_mode: str | None = None,
) -> ProviderRegistryEntry:
    """Select from explicit request, then trigger policy, then capability policy."""
    requested = str(explicit_provider_id or trigger_provider_id or "").strip()
    mode = str(execution_mode or "").strip().lower() or None
    if mode is not None and mode not in EXECUTION_MODES:
        raise ProviderRuntimeError("PROVIDER_MODE_INVALID", f"unsupported execution_mode: {mode}")
    if requested:
        entry = registry.get(requested)
        if entry is None or not entry.enabled:
            raise ProviderRuntimeError("PROVIDER_UNAVAILABLE", f"provider is not enabled: {requested}")
        _require_capabilities(entry, required_capabilities)
        if mode is not None and mode not in entry.execution_modes:
            raise ProviderRuntimeError(
                "PROVIDER_MODE_UNSUPPORTED",
                f"provider {entry.provider_id!r} does not support {mode!r}",
            )
        return entry
    required = frozenset(str(item).strip() for item in required_capabilities if str(item).strip())
    matches = [
        entry
        for _provider_id, entry in sorted(registry.items())
        if entry.enabled
        and required.issubset(entry.capabilities)
        and (mode is None or mode in entry.execution_modes)
    ]
    if not matches:
        raise ProviderRuntimeError("PROVIDER_UNAVAILABLE", "no provider satisfies required capabilities")
    return matches[0]


def select_work_owner(
    *,
    delegated_role: bool = False,
    independent_route_contract: bool = False,
) -> ProviderWorkSelection:
    """Keep delegation, independent workflow work, and provider execution distinct."""
    if delegated_role and independent_route_contract:
        raise ProviderRuntimeError(
            "PROVIDER_WORK_INVALID",
            "role delegation and independent RouteContract work are distinct operations",
        )
    if delegated_role:
        return ProviderWorkSelection("role_slot", "same TeamTask workflow delegation")
    if independent_route_contract:
        return ProviderWorkSelection("team_task", "independent work owns its own RouteContract/DONE")
    return ProviderWorkSelection("provider_job", "single provider execution has no RouteContract/DONE")


class ProviderJobManager:
    """Canonical durable lifecycle/result owner for non-browser provider execution."""

    def __init__(self, store: ProviderJobStore) -> None:
        self.store = store

    def create(self, invocation: ProviderInvocation, entry: ProviderRegistryEntry) -> dict[str, Any]:
        _validate_invocation(invocation, entry, browser=False)
        task = self._task(invocation.task_id)
        provider_job_id = provider_job_id_for(invocation)
        now = _utc_now()
        timeout = invocation.timeout_seconds or entry.default_timeout_seconds
        record = {
            "provider_job_id": provider_job_id,
            "owner_task_id": invocation.task_id,
            "provider_id": entry.provider_id,
            "execution_mode": invocation.execution_mode,
            "state": "QUEUED",
            "created_at": now,
            "started_at": None,
            "deadline_at": _deadline(now, timeout),
            "completed_at": None,
            "stdout_ref": None,
            "stderr_ref": None,
            "result_ref": None,
            "error": None,
            "metadata": {"material_refs": list(invocation.material_refs)},
        }

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            jobs = state.setdefault("provider_jobs", {})
            if not isinstance(jobs, dict):
                raise ProviderRuntimeError("PROVIDER_JOB_STORE_INVALID", "provider_jobs registry is not an object")
            existing = jobs.get(provider_job_id)
            if existing is not None:
                if _job_identity(existing) != _job_identity(record):
                    raise ProviderRuntimeError("PROVIDER_JOB_CONFLICT", "provider job id belongs to different work")
                return state
            jobs[provider_job_id] = record
            return state

        saved = self.store.update(str(task["manifest_path"]), mutate)
        return dict(saved["provider_jobs"][provider_job_id])

    def transition(
        self,
        task_id: str,
        provider_job_id: str,
        state_name: str,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
        result: str | None = None,
        error: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        desired = str(state_name).upper()
        if desired not in PROVIDER_JOB_STATES:
            raise ProviderRuntimeError("PROVIDER_JOB_STATE_INVALID", f"unsupported provider job state: {desired}")
        task = self._task(task_id)

        def mutate(owner: dict[str, Any]) -> dict[str, Any]:
            jobs = owner.get("provider_jobs")
            job = jobs.get(provider_job_id) if isinstance(jobs, dict) else None
            if not isinstance(job, dict):
                raise ProviderRuntimeError("PROVIDER_JOB_UNKNOWN", f"unknown provider job: {provider_job_id}")
            current = str(job.get("state") or "")
            _validate_job_transition(current, desired)
            # Terminal evidence is immutable; duplicate callbacks are idempotent.
            if current in TERMINAL_PROVIDER_JOB_STATES:
                return owner
            now = _utc_now()
            if desired == "RUNNING" and not job.get("started_at"):
                job["started_at"] = now
            if desired in TERMINAL_PROVIDER_JOB_STATES:
                job["completed_at"] = job.get("completed_at") or now
                job["stdout_ref"] = _result_material(owner, job, "stdout", stdout, "log")
                job["stderr_ref"] = _result_material(owner, job, "stderr", stderr, "log")
                job["result_ref"] = _result_material(owner, job, "result", result, "artifact")
                job["error"] = dict(error) if error else None
            if metadata:
                job.setdefault("metadata", {}).update(dict(metadata))
            job["state"] = desired
            if desired in TERMINAL_PROVIDER_JOB_STATES:
                _observe_provider_terminal(owner, job, now=now)
            return owner

        saved = self.store.update(str(task["manifest_path"]), mutate)
        return dict(saved["provider_jobs"][provider_job_id])

    def result(self, task_id: str, provider_job_id: str) -> ProviderResult:
        task = self._task(task_id)
        jobs = task.get("provider_jobs")
        job = jobs.get(provider_job_id) if isinstance(jobs, Mapping) else None
        if not isinstance(job, Mapping):
            raise ProviderRuntimeError("PROVIDER_JOB_UNKNOWN", f"unknown provider job: {provider_job_id}")
        state_name = str(job.get("state") or "")
        if state_name not in TERMINAL_PROVIDER_JOB_STATES:
            raise ProviderRuntimeError("PROVIDER_JOB_NOT_TERMINAL", "ProviderResult exists only for terminal ProviderJob")
        return _project_result(job)

    def execute(self, invocation: ProviderInvocation, entry: ProviderRegistryEntry) -> ProviderResult:
        job = self.create(invocation, entry)
        provider_job_id = str(job["provider_job_id"])
        current_state = str(job.get("state") or "")
        if current_state in TERMINAL_PROVIDER_JOB_STATES:
            return self.result(invocation.task_id, provider_job_id)
        if current_state == "RUNNING":
            raise ProviderRuntimeError(
                "PROVIDER_JOB_IN_FLIGHT",
                "durable ProviderJob is already RUNNING; reconcile it instead of replaying execution",
            )
        command = _provider_command(invocation)
        self.transition(invocation.task_id, provider_job_id, "RUNNING")
        timeout = invocation.timeout_seconds or entry.default_timeout_seconds
        output_limit = invocation.output_limit_bytes or entry.default_output_limit_bytes
        try:
            completed = subprocess.run(
                command,
                cwd=invocation.workspace or None,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.transition(
                invocation.task_id,
                provider_job_id,
                "TIMEOUT",
                stdout=_bounded_text(exc.stdout, output_limit),
                stderr=_bounded_text(exc.stderr, output_limit),
                error={"code": "PROVIDER_TIMEOUT", "message": f"provider exceeded {timeout}s"},
            )
        except OSError as exc:
            self.transition(
                invocation.task_id,
                provider_job_id,
                "FAILED",
                error={"code": "PROVIDER_EXEC_FAILED", "message": str(exc)},
            )
        else:
            terminal = "SUCCEEDED" if completed.returncode == 0 else "FAILED"
            self.transition(
                invocation.task_id,
                provider_job_id,
                terminal,
                stdout=_bounded_text(completed.stdout, output_limit),
                stderr=_bounded_text(completed.stderr, output_limit),
                result=json.dumps({"exit_code": completed.returncode}, sort_keys=True),
                error=(
                    None
                    if completed.returncode == 0
                    else {"code": "PROVIDER_EXIT_NONZERO", "message": f"exit code {completed.returncode}"}
                ),
                metadata={"exit_code": completed.returncode},
            )
        return self.result(invocation.task_id, provider_job_id)

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.store.load_task_id(task_id)
        if task is None:
            raise ProviderRuntimeError("PROVIDER_TASK_UNKNOWN", f"owner TeamTask does not exist: {task_id}")
        return task


class ProviderDispatcher:
    """Single provider selector. Browser turns remain owned by RoleController."""

    def __init__(
        self,
        *,
        endpoints: BrowserEndpointRegistry,
        jobs: ProviderJobManager,
        registry: Mapping[str, ProviderRegistryEntry] | None = None,
        role_controller: RoleController | None = None,
    ) -> None:
        self.endpoints = endpoints
        self.jobs = jobs
        self.registry = dict(registry or DEFAULT_PROVIDER_REGISTRY)
        self.role_controller = role_controller or RoleController()

    async def dispatch(
        self,
        invocation: ProviderInvocation | Mapping[str, Any],
        *,
        browser_context: Mapping[str, Any] | None = None,
        trigger_provider_id: str | None = None,
        required_capabilities: Sequence[str] = (),
    ) -> Any:
        request = (
            invocation
            if isinstance(invocation, ProviderInvocation)
            else ProviderInvocation.from_mapping(invocation)
        )
        entry = select_provider(
            self.registry,
            explicit_provider_id=request.provider_id or None,
            trigger_provider_id=trigger_provider_id,
            required_capabilities=required_capabilities,
            execution_mode=request.execution_mode,
        )
        if not request.provider_id:
            request = replace(request, provider_id=entry.provider_id)
        _validate_invocation(request, entry, browser=request.execution_mode == "browser_dom")
        if request.execution_mode == "browser_dom":
            return await self._dispatch_browser(request, browser_context)
        return await asyncio.to_thread(self.jobs.execute, request, entry)

    async def _dispatch_browser(
        self,
        invocation: ProviderInvocation,
        browser_context: Mapping[str, Any] | None,
    ) -> Any:
        if not invocation.endpoint_id:
            raise ProviderRuntimeError(
                "BROWSER_ENDPOINT_REQUIRED", "browser_dom requires a registered endpoint_id"
            )
        if any(key in invocation.provider_options for key in ("cdp_url", "address", "browser")):
            raise ProviderRuntimeError(
                "BROWSER_ENDPOINT_BYPASS_REJECTED", "raw browser transport bypass is not allowed"
            )
        endpoint = self.endpoints.get(invocation.endpoint_id)
        if endpoint.get("removed") or endpoint.get("draining"):
            raise ProviderRuntimeError("BROWSER_ENDPOINT_UNAVAILABLE", "browser endpoint is draining or removed")
        if browser_context is None:
            raise ProviderRuntimeError(
                "BROWSER_CONTEXT_REQUIRED", "browser_dom delegates an existing RoleController turn context"
            )
        required = ("worker", "state", "hop", "acquired", "actions")
        missing = [key for key in required if key not in browser_context]
        if missing:
            raise ProviderRuntimeError(
                "BROWSER_CONTEXT_INVALID", f"RoleController context is missing: {missing!r}"
            )
        acquired = browser_context["acquired"]
        acquired_endpoint = getattr(acquired, "endpoint_id", None)
        if acquired_endpoint is not None and str(acquired_endpoint) != str(endpoint["endpoint_id"]):
            raise ProviderRuntimeError("BROWSER_ENDPOINT_MISMATCH", "acquired page belongs to another endpoint")
        kwargs = dict(browser_context.get("controller_kwargs") or {})
        return await self.role_controller.run(
            browser_context["worker"],
            browser_context["state"],
            browser_context["hop"],
            acquired,
            browser_context["actions"],
            **kwargs,
        )


def adopt_external_script_spawn(
    owner: MutableMapping[str, Any],
    continuation: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Adopt a confirmed Phase-1.5 script spawn into canonical ProviderJob state."""
    task_id = str(owner.get("task_id") or "")
    continuation_id = str(continuation.get("continuation_id") or "")
    provider_job_id = _compat_provider_job_id(task_id, continuation_id)
    now = str(continuation.get("spawn_confirmed_at") or continuation.get("registered_at") or _utc_now())
    record = {
        "provider_job_id": provider_job_id,
        "owner_task_id": task_id,
        "provider_id": "custom",
        "execution_mode": "custom",
        "state": "RUNNING",
        "created_at": str(continuation.get("registered_at") or now),
        "started_at": now,
        "deadline_at": None,
        "completed_at": None,
        "stdout_ref": None,
        "stderr_ref": None,
        "result_ref": None,
        "error": None,
        "metadata": {
            "compatibility_source": "phase1.5_external_script",
            "external_job_id": continuation.get("job_id"),
            "continuation_id": continuation_id,
        },
    }
    jobs = owner.setdefault("provider_jobs", {})
    if not isinstance(jobs, dict):
        raise ProviderRuntimeError("PROVIDER_JOB_STORE_INVALID", "provider_jobs registry is not an object")
    existing = jobs.get(provider_job_id)
    if existing is not None:
        if _job_identity(existing) != _job_identity(record):
            raise ProviderRuntimeError("PROVIDER_JOB_CONFLICT", "legacy spawn maps to conflicting ProviderJob")
        continuation["provider_job_id"] = provider_job_id
        continuation["source_kind"] = "provider_job"
        continuation["source_refs"] = [provider_job_id]
        return existing
    jobs[provider_job_id] = record
    continuation["provider_job_id"] = provider_job_id
    continuation["source_kind"] = "provider_job"
    continuation["source_refs"] = [provider_job_id]
    return record


def adopt_external_script_callback(
    owner: MutableMapping[str, Any],
    continuation: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Terminalize the ProviderJob mirrored from a Phase-1.5 script callback."""
    callback = continuation.get("callback")
    if not isinstance(callback, Mapping):
        raise ProviderRuntimeError("PROVIDER_COMPAT_INVALID", "legacy continuation has no callback")
    task_id = str(owner.get("task_id") or "")
    continuation_id = str(continuation.get("continuation_id") or "")
    provider_job_id = _compat_provider_job_id(task_id, continuation_id)
    jobs = owner.setdefault("provider_jobs", {})
    if not isinstance(jobs, dict):
        raise ProviderRuntimeError("PROVIDER_JOB_STORE_INVALID", "provider_jobs registry is not an object")
    record = jobs.get(provider_job_id)
    if record is None:
        record = adopt_external_script_spawn(owner, continuation)
    if not isinstance(record, dict):
        raise ProviderRuntimeError("PROVIDER_JOB_STORE_INVALID", "compatibility ProviderJob is invalid")
    status = str(callback.get("status") or "").lower()
    state_name = {
        "succeeded": "SUCCEEDED",
        "timeout": "TIMEOUT",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
        "partial": "PARTIAL",
    }.get(status)
    if state_name is None:
        raise ProviderRuntimeError("PROVIDER_COMPAT_INVALID", "legacy callback status is not terminal")
    current = str(record.get("state") or "")
    if current in TERMINAL_PROVIDER_JOB_STATES:
        if current != state_name:
            raise ProviderRuntimeError("PROVIDER_JOB_CONFLICT", "legacy callback conflicts with terminal ProviderJob")
        _observe_compat_provider_terminal(owner, continuation, record)
        return record
    _validate_job_transition(current, state_name)
    material_ids = [str(item) for item in callback.get("material_ids") or ()]
    record.update(
        state=state_name,
        completed_at=str(callback.get("received_at") or _utc_now()),
        stdout_ref=next((item for item in material_ids if item.endswith("-stdout")), None),
        stderr_ref=next((item for item in material_ids if item.endswith("-stderr")), None),
        result_ref=next((item for item in material_ids if "-result-" in item), None),
        error=(
            None
            if state_name == "SUCCEEDED"
            else {"code": f"EXTERNAL_SCRIPT_{state_name}", "message": str(callback.get("summary") or status)}
        ),
    )
    record.setdefault("metadata", {}).update(
        exit_code=callback.get("exit_code"), material_ids=material_ids
    )
    continuation["provider_job_id"] = provider_job_id
    _observe_compat_provider_terminal(owner, continuation, record)
    return record


def _observe_compat_provider_terminal(
    owner: MutableMapping[str, Any],
    continuation: MutableMapping[str, Any],
    job: Mapping[str, Any],
) -> None:
    from . import cdpa_async_continuation as async_continuation

    status = _RESULT_STATUS.get(str(job.get("state") or ""))
    continuation_id = str(continuation.get("continuation_id") or "")
    if status is None or async_continuation.get(owner, continuation_id) is None:
        return
    material_ids = tuple(
        str(value)
        for value in (job.get("stdout_ref"), job.get("stderr_ref"), job.get("result_ref"))
        if value
    )
    async_continuation.record_source_terminal(
        owner,
        str(continuation.get("continuation_id") or ""),
        str(job.get("provider_job_id") or ""),
        status=status,
        material_ids=material_ids,
        summary=f"ProviderJob {job.get('provider_job_id')} reached {job.get('state')}",
        now=str(job.get("completed_at") or _utc_now()),
    )


def _compat_provider_job_id(task_id: str, continuation_id: str) -> str:
    digest = hashlib.sha256(f"{task_id}\0{continuation_id}".encode("utf-8")).hexdigest()
    return f"pjob-compat-{digest[:17]}"


def provider_job_id_for(invocation: ProviderInvocation) -> str:
    payload = {
        "provider_id": invocation.provider_id,
        "task_id": invocation.task_id,
        "role_key": invocation.role_key,
        "task_text": invocation.task_text,
        "execution_mode": invocation.execution_mode,
        "material_refs": list(invocation.material_refs),
        "workspace": invocation.workspace,
        "timeout_seconds": invocation.timeout_seconds,
        "output_limit_bytes": invocation.output_limit_bytes,
        "environment_policy": invocation.environment_policy,
        "provider_options": dict(invocation.provider_options),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"pjob-{digest[:24]}"


def _provider_command(invocation: ProviderInvocation) -> list[str]:
    options = dict(invocation.provider_options)
    configured = options.get("command")
    if configured is not None:
        if not isinstance(configured, (list, tuple)) or not configured or any(
            not isinstance(item, str) or not item for item in configured
        ):
            raise ProviderRuntimeError("PROVIDER_COMMAND_INVALID", "provider command must be a non-empty string array")
        return list(configured)
    if invocation.provider_id == "codex":
        if invocation.execution_mode == "acp":
            return ["codex", "acp", invocation.task_text]
        return ["codex", "exec", invocation.task_text]
    if invocation.provider_id == "pi":
        return ["pi", invocation.task_text]
    raise ProviderRuntimeError(
        "PROVIDER_COMMAND_REQUIRED",
        f"provider {invocation.provider_id!r} requires provider_options.command",
    )


def _validate_invocation(
    invocation: ProviderInvocation,
    entry: ProviderRegistryEntry,
    *,
    browser: bool,
) -> None:
    if not invocation.task_id or not invocation.role_key or not invocation.provider_id:
        raise ProviderRuntimeError("PROVIDER_INVOCATION_INVALID", "provider_id, task_id, and role_key are required")
    if invocation.execution_mode not in EXECUTION_MODES:
        raise ProviderRuntimeError("PROVIDER_MODE_INVALID", f"unsupported execution_mode: {invocation.execution_mode}")
    if invocation.execution_mode not in entry.execution_modes:
        raise ProviderRuntimeError(
            "PROVIDER_MODE_UNSUPPORTED",
            f"provider {entry.provider_id!r} does not support {invocation.execution_mode!r}",
        )
    if browser != (invocation.execution_mode == "browser_dom"):
        raise ProviderRuntimeError("PROVIDER_MODE_INVALID", "browser execution classification mismatch")
    if not browser and invocation.endpoint_id is not None:
        raise ProviderRuntimeError("PROVIDER_INVOCATION_INVALID", "non-browser ProviderJob cannot bind a browser endpoint")
    binding_fields = {"conversation_url", "binding_id"} & set(invocation.provider_options)
    if binding_fields and "adopted_url" not in entry.capabilities:
        raise ProviderRuntimeError(
            "PROVIDER_CAPABILITY_MISSING",
            f"provider does not support adopted-url binding fields: {sorted(binding_fields)!r}",
        )


def _require_capabilities(entry: ProviderRegistryEntry, required: Sequence[str]) -> None:
    missing = {str(item).strip() for item in required if str(item).strip()} - entry.capabilities
    if missing:
        raise ProviderRuntimeError(
            "PROVIDER_CAPABILITY_MISSING", f"provider lacks capabilities: {sorted(missing)!r}"
        )


def _validate_job_transition(current: str, desired: str) -> None:
    if current == desired:
        return
    if current in TERMINAL_PROVIDER_JOB_STATES:
        raise ProviderRuntimeError("PROVIDER_JOB_TERMINAL", "terminal ProviderJob cannot transition again")
    allowed = {"QUEUED": {"RUNNING", "CANCELLED"}, "RUNNING": TERMINAL_PROVIDER_JOB_STATES}
    if desired not in allowed.get(current, set()):
        raise ProviderRuntimeError("PROVIDER_JOB_STATE_INVALID", f"invalid ProviderJob transition {current}->{desired}")


def _observe_provider_terminal(
    owner: MutableMapping[str, Any], job: MutableMapping[str, Any], *, now: str
) -> None:
    """Project ProviderJob terminal evidence into matching continuations after job truth is set."""
    from . import cdpa_async_continuation as async_continuation

    registry = owner.get("async_continuations")
    if not isinstance(registry, Mapping):
        return
    provider_job_id = str(job.get("provider_job_id") or "")
    status = _RESULT_STATUS.get(str(job.get("state") or ""))
    if not provider_job_id or status is None:
        return
    material_ids = tuple(
        str(value)
        for value in (job.get("stdout_ref"), job.get("stderr_ref"), job.get("result_ref"))
        if value
    )
    errors: list[dict[str, str]] = []
    for continuation_id, record in list(registry.items()):
        if (
            not isinstance(record, Mapping)
            or record.get("source_kind") != "provider_job"
            or provider_job_id not in tuple(str(item) for item in record.get("source_refs") or ())
            or str(record.get("state") or "") in {"delivered", "cancelled", "superseded"}
        ):
            continue
        try:
            async_continuation.record_source_terminal(
                owner,
                str(continuation_id),
                provider_job_id,
                status=status,
                material_ids=material_ids,
                summary=f"ProviderJob {provider_job_id} reached {job.get('state')}",
                now=now,
            )
        except async_continuation.AsyncContinuationError as exc:
            errors.append({"continuation_id": str(continuation_id), "code": exc.code, "message": exc.message})
    if errors:
        job.setdefault("metadata", {})["continuation_observation_errors"] = errors


def _project_result(job: Mapping[str, Any]) -> ProviderResult:
    state = str(job.get("state") or "")
    return ProviderResult(
        provider_job_id=str(job.get("provider_job_id") or ""),
        provider_id=str(job.get("provider_id") or ""),
        status=_RESULT_STATUS[state],
        stdout_ref=(str(job["stdout_ref"]) if job.get("stdout_ref") else None),
        stderr_ref=(str(job["stderr_ref"]) if job.get("stderr_ref") else None),
        result_ref=(str(job["result_ref"]) if job.get("result_ref") else None),
        error=(dict(job["error"]) if isinstance(job.get("error"), Mapping) else None),
        metadata=(dict(job["metadata"]) if isinstance(job.get("metadata"), Mapping) else {}),
    )


def _result_material(
    owner: MutableMapping[str, Any],
    job: Mapping[str, Any],
    suffix: str,
    content: str | None,
    material_type: str,
) -> str | None:
    if content is None:
        return None
    material = register_material(
        owner,
        source_kind="provider_result",
        material_type=material_type,
        version=str(job["provider_job_id"]),
        content=content,
        role_key=None,
        material_id=f"{job['provider_job_id']}-{suffix}",
    )
    return str(material["material_id"])


def _job_identity(job: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(job.get("provider_job_id") or ""),
        str(job.get("owner_task_id") or ""),
        str(job.get("provider_id") or ""),
        str(job.get("execution_mode") or ""),
    )


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProviderRuntimeError("PROVIDER_INVOCATION_INVALID", f"{name} must be a positive integer")
    return value


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", errors="replace")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deadline(start: str, timeout_seconds: int) -> str:
    return (datetime.fromisoformat(start) + timedelta(seconds=timeout_seconds)).isoformat()
