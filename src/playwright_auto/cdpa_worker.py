from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import inspect
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlparse
from uuid import UUID

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .cdpa_actions import (
    AcquiredRole,
    BranchBootstrapError,
    BranchTargetUnresolvedError,
    CDPATabActions,
    RoleOwnershipError,
    TeamCloseError,
    is_transient_page_lifecycle_error,
)
from .cdpa_bootstraps import (
    BootstrapCatalog,
    normalize_bootstrap_donor,
    normalize_bootstrap_record,
    resolve_default_bootstrap_id,
)
from .cdpa_commands import (
    RepairRequest,
    WorkerCommand,
    command_snapshot,
    conversation_identity,
    validate_worker_command,
    workflow_control_eligibility,
)
from .cdpa_config import CDPAConfig, load_cdpa_config, remote_repository_from_task
from .cdpa_browser_projection import build_browser_projection
from .cdpa_endpoint_pool import EndpointPool
from .cdpa_dependencies import dependency_readiness
from .cdpa_endpoints import normalize_endpoint_record
from .cdpa_managed_browser import ManagedBrowserLifecycle
from .cdpa_independent import (
    BUILTIN_MAINTAINERS_PROMPT,
    BUILTIN_MONITOR_PROMPT,
    INDEPENDENT_COLUMN,
    INDEPENDENT_ROLE,
    canonical_independent_events,
    claim_oldest_event,
    independent_tags,
    is_independent_task,
    normalize_agent_name,
    refresh_recovery_warmup_on_enable,
    validate_trigger_settings,
)
from .cdpa_projection import (
    build_dashboard_actions,
    build_task_projection,
    build_waiting_order,
)
from .cdpa_runtime_db import RuntimeDB
from .cdpa_runtime_registry import CDPARuntimeRegistry, MINIMUM_DEADLINE_SECONDS
from .cdpa_prompts import PromptBuilder
from .file_lock import exclusive_file_lock
from .role_runtime import Action, Policy, RoleController
from .playwright_driver_patch import ensure_playwright_dialog_autoclose_guard
from .cdpa_response import (
    begin_refresh,
    finish_refresh,
    observe_response_activity,
    observe_responding,
    parse_time,
    recover_incomplete_refresh,
    refresh_due,
    remaining_timeout_ms,
    start_wait_budget,
)
from .cdpa_safety import sanitize_exception, sanitize_text, sanitize_value
from .cdpa_routes import (
    InlineReportMaterializationError,
    RouteContractError,
    RouteDecision,
    expected_report_relative,
    materialize_inline_report,
    parse_role_response,
    parse_route_response,
    validate_file_report,
)
from .cdpa_store import (
    TaskStore,
    governing_semantic_for_hop,
    normalize_governing_contract,
    is_replaced_immutable_history,
    task_goal_for_hop,
    report_mode_from_options,
    utc_now,
)
from .cdpa_team import cleanup_eligible, has_other_nonterminal_team_work
from .role_indicator import WINDOW_NAME_PREFIX
from .observability import append_live_event, configure_live_event_log
from .cdpa_workflow_agents import task_workflow_definitions
from .chatgpt import (
    ChatGPTAutomationError,
    ChatGPTPage,
    ChoicePromptBlockedError,
    ComposerConflictError,
    PageOwnershipError,
    SendRecoveryError,
    TaskBindingError,
    attachment_names_match,
    backend_create_project,
    backend_projects,
    backend_set_conversation_project,
    capture_message_baseline,
    capture_response_recovery_baseline,
    configure_action_delays,
    ConversationTranscriptNotReadyError,
    IncompleteResponseTimeoutError,
    ManualInputPendingError,
    merge_response_recovery_baselines,
    RateLimitBlockedError,
    MessageSnapshot,
    PageBinding,
    receipt_user_message_seen,
    response_activity_signature,
    response_transport_ui_active,
    SendReceipt,
    StableMalformedResponseError,
    UnsafePageStateError,
    unique_new_user_message,
    visible_text_matches,
)
from .chatgpt_graph import (
    BackendAuthError,
    BackendError,
    BackendNotReadyError,
    BackendSchemaError,
    BackendUnavailableError,
    GraphIdentityError,
    rendered_user_text_matches,
    resolve_completed_file_write,
    resolve_exact_new_user_message,
    resolve_exact_user_message,
    resolve_unique_exact_user_message,
    resolve_terminal_assistant,
    resolve_bootstrap_donor,
    resolve_inherited_assistant,
    resolve_latest_terminal_assistant,
)
from .connection import is_cdp_disconnect, is_local_cdp_endpoint, normalize_cdp_endpoint
from .durable import (
    DurableRecoveryState,
    DurableRequestError,
    RequestLedger,
    RequestStatus,
    build_idempotency_key,
    classify_recovery_state,
)
from .durable_blocks import DurableSendBlock
from .upload import UploadIdentityChangedError, UploadReceipt, collect_file_identities
from .workflow import WorkflowContext
from .workspace import ChatGPTWorkspace

TERMINAL = frozenset({"DONE", "STOPPED"})
IN_FLIGHT = frozenset({"sending", "sent", "waiting"})
_STREAM_STATUS_RECOVERY_SECONDS = 30.0
_LEGACY_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE = "consecutive_self_route_limit"
_LEGACY_NO_PROGRESS_ROUTE_CYCLE_BLOCK_CODE = "no_progress_route_cycle"
_AUTONOMOUS_EXECUTION_BUDGET_BLOCK_CODE = "autonomous_execution_budget_exhausted"
_AUTONOMOUS_EXECUTION_LIMIT = 12
_RECOVERY_SNAPSHOT_MAX_CHARS = 12_000
_RECOVERY_GOAL_MAX_CHARS = 1_000
_EXECUTION_RECOVERY_SNAPSHOT_MAX_CHARS = 2_400
_EXECUTION_RECOVERY_PROMPT_MAX_CHARS = 8_000
_STATUS_RECOVERY_GRAPH_SECONDS = 300.0
_TERMINAL_GRAPH_RETRY_SECONDS = 120.0
_TERMINAL_GRAPH_MAX_ATTEMPTS = 3
CLEANUP_IDENTICAL_FAILURE_LIMIT = 3


class _AcceptedConversationIdentityConflict(GraphIdentityError):
    """Exact accepted page/user now proves a conflicting canonical identity."""


_CLEANUP_RETRY_DELAYS_SECONDS = (30.0, 120.0)
_DOM_FALLBACK_SETTLE_SECONDS = 30.0
_POST_REFRESH_RESPONSE_PROBE_SECONDS = 60.0
_POST_REFRESH_FINAL_RECONCILE_SECONDS = 120.0
_STREAM_STATUS_POLL_MIN_SECONDS = 30.0
_STREAM_STATUS_POLL_MAX_SECONDS = 35.0
_DOM_WAIT_PROBE_MS = 5_000
_MCP_ALLOW_STABLE_SECONDS = 5.0
_AMBIENT_DOM_FALLBACK_SECONDS = 5.0
_MCP_ALLOW_POST_CLICK_SECONDS = 5.0
_CONTROLLER_STALL_REFRESH_SECONDS = 600.0
_AUTOMATED_SEND_SPACING_SECONDS = 10.0
_SENDING_CONTINUATION_STARTED = "bounded continuation started after proven atomic non-acceptance"
_PROVEN_ATOMIC_NONACCEPTANCE_ERRORS = (
    "ComposerConflictError: composer changed or became unavailable inside the atomic send boundary",
    "ComposerConflictError: attachment identity changed inside the atomic send boundary",
    "PageOwnershipError: page ownership changed inside the atomic send boundary:",
    "UnsafePageStateError: page state changed inside the atomic send boundary:",
)
_RATE_LIMIT_BLOCK_MESSAGE = (
    "Too many requests; shared browser-profile cooldown is active"
)
_ENDPOINTLESS_CONTROL_ACTIONS = frozenset({"pause", "stop", "release_required_browser_endpoint"})


def _format_execution_recovery_handoff(snapshot: Mapping[str, Any]) -> str:
    return (
        "Continue this existing role/task from durable state. Read the durable paths below, "
        "continue only unfinished work, and do not replay the prior accepted request.\n\n"
        "RECOVERY_SNAPSHOT\n"
        + json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class IneffectiveControlError(RuntimeError):
    """The primitive ran, but its required operational postcondition did not hold."""


def _active_hop(state: Mapping[str, Any]) -> dict[str, Any]:
    active = state.get("active_hop_id")
    for hop in state.get("hops") or []:
        if isinstance(hop, dict) and hop.get("hop_id") == active:
            return hop
    raise RuntimeError(f"active hop {active!r} does not exist")


def _next_recovering_resume_control(state: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(state.get("status") or "").upper() in TERMINAL:
        return None
    recovering = [
        item
        for item in state.get("controls") or []
        if isinstance(item, dict)
        and str(item.get("action") or "") == "resume"
        and str(item.get("status") or "") == "recovering"
    ]
    return recovering[-1] if recovering else None


def _next_requested_control(state: Mapping[str, Any]) -> dict[str, Any] | None:
    requested = [
        item
        for item in state.get("controls") or []
        if isinstance(item, dict) and item.get("status") == "requested"
    ]
    if is_independent_task(state):
        reset = next(
            (
                item
                for item in reversed(requested)
                if str(item.get("action") or "") == "reset"
            ),
            None,
        )
        if reset is not None:
            return reset
    else:
        stop = next(
            (item for item in requested if str(item.get("action") or "") == "stop"),
            None,
        )
        if stop is not None:
            return stop
    return requested[0] if requested else None


def _control_can_run_endpointless(
    state: Mapping[str, Any], control: Mapping[str, Any] | None
) -> bool:
    if control is None:
        return False
    action = str(control.get("action") or "").strip().lower()
    if action in _ENDPOINTLESS_CONTROL_ACTIONS:
        return True
    if action == "reset" and is_independent_task(state):
        role = str(state.get("active_role") or INDEPENDENT_ROLE)
        record = (state.get("roles") or {}).get(role)
        if isinstance(record, Mapping) and not record.get("page_id") and not record.get("page_url"):
            return True
    if action != "resume" or state.get("active_hop_id") is None:
        return False
    try:
        hop = _active_hop(state)
    except RuntimeError:
        return False
    hop_state = str(hop.get("state") or "").strip().lower()
    if hop_state == "responded":
        return True
    return hop_state == "routed" and str(hop.get("route") or "").strip().upper() == "PAUSE"


def _waiting_working_copy(state: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only branches the steady waiting path may mutate.

    Historical hops, task text, prompts, reports, maintenance history, and other
    inert manifest data remain shared. The active hop and role/error branches
    are isolated so a no-op wait can be compared to the loaded baseline without
    serializing the complete manifest on every poll.
    """
    active_hop_id = state.get("active_hop_id")
    working = dict(state)
    hops = list(state.get("hops") or ())
    active_found = False
    for index, hop in enumerate(hops):
        if isinstance(hop, Mapping) and hop.get("hop_id") == active_hop_id:
            hops[index] = copy.deepcopy(dict(hop))
            active_found = True
            break
    if not active_found:
        raise RuntimeError(f"active hop {active_hop_id!r} does not exist")
    working["hops"] = hops
    roles = state.get("roles")
    if isinstance(roles, Mapping):
        working["roles"] = {
            str(role): copy.deepcopy(dict(record))
            if isinstance(record, Mapping)
            else record
            for role, record in roles.items()
        }
    for key in (
        "errors",
        "controls",
        "cleanup",
        "maintenance",
        "queue",
        "waiting",
        "route_timeline",
        "dependency_events",
        "queue_events",
        "repair_wait",
    ):
        value = state.get(key)
        if isinstance(value, (dict, list)):
            working[key] = copy.deepcopy(value)
    return working


_ROLE_OFFLINE_TRUE_LIST_CODES = frozenset(
    {"role_offline", "unexpected_error", "role_ownership_ambiguous"}
)
_ROLE_OFFLINE_SUFFIX = " tab is offline; use Open tab for controlled recovery"


def _looks_like_recorded_role_offline_error(message: object) -> bool:
    value = str(message or "").strip()
    if value.startswith("RoleOwnershipError: "):
        value = value.removeprefix("RoleOwnershipError: ")
    return value.startswith("recorded '") and value.endswith(
        "'" + _ROLE_OFFLINE_SUFFIX
    )


def _recoverable_conversation_identity(value: Any) -> str | None:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return None
    identity = conversation_identity(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"chatgpt.com", "www.chatgpt.com"}
        or parsed.query or parsed.fragment
        or identity is None
        or unquote(identity.rsplit("/", 1)[-1]).lower().startswith(("web:", "local-chatgpt:"))
    ):
        return None
    return identity


def _official_saved_url(value: Any) -> tuple[str, bool] | None:
    """Canonical persistent ChatGPT conversation URL: (conversation ID, in project)."""
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"chatgpt.com", "www.chatgpt.com"}
        or parsed.query or parsed.fragment or parsed.params
    ):
        return None
    path = parsed.path.rstrip("/")
    match = re.fullmatch(r"/c/([0-9a-fA-F-]{36})", path)
    project = False
    if match is None:
        match = re.fullmatch(r"/g/[^/]+/c/([0-9a-fA-F-]{36})", path)
        project = True
    if match is None:
        return None
    candidate = match.group(1)
    try:
        canonical = str(UUID(candidate))
    except ValueError:
        return None
    return (canonical, project) if candidate.lower() == canonical else None


def _preferred_saved_url(*values: Any, conversation_id: str | None = None) -> str | None:
    choices = []
    for value in values:
        parsed = _official_saved_url(value)
        if parsed is not None and (conversation_id is None or parsed[0] == conversation_id):
            choices.append((str(value), parsed[1]))
    return next((value for value, project in choices if project), choices[0][0] if choices else None)


def _command_saved_identity(value: Any) -> str | None:
    """Read a relative /c/id command snapshot, ignoring provisional IDs."""
    identity = conversation_identity(value)
    if identity is None or unquote(identity.rsplit("/", 1)[-1]).lower().startswith(
        ("web:", "local-chatgpt:")
    ):
        return None
    return identity


def _provisional_conversation_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in {"chatgpt.com", "www.chatgpt.com"} and (
        parsed.path.startswith("/c/")
        and unquote(parsed.path.rsplit("/", 1)[-1]).lower().startswith("local-chatgpt:")
    )


def _role_ownership_block_code(error: BaseException) -> str | None:
    if not isinstance(error, RoleOwnershipError):
        return None
    code = str(getattr(error, "code", "") or "")
    if code == "role_offline" or _looks_like_recorded_role_offline_error(error):
        return "role_offline"
    return "role_ownership_ambiguous"


def _is_verified_role_offline_block(
    state: Mapping[str, Any],
    role: str,
) -> bool:
    logical_role = str(role or "").upper()
    block_code = str(state.get("block_code") or "")
    if (
        str(state.get("status") or "").upper() != "BLOCKED"
        or block_code not in _ROLE_OFFLINE_TRUE_LIST_CODES
        or str(state.get("active_role") or "").upper() != logical_role
    ):
        return False
    role_record = (state.get("roles") or {}).get(logical_role)
    if not isinstance(role_record, Mapping):
        return False
    if block_code != "role_offline":
        physical_role = str(role_record.get("physical_role") or "")
        expected = f"recorded {physical_role!r}{_ROLE_OFFLINE_SUFFIX}"
        reason = str(state.get("block_reason") or "").strip()
        if reason.startswith("RoleOwnershipError: "):
            reason = reason.removeprefix("RoleOwnershipError: ")
        if reason != expected:
            return False
    try:
        hop = _active_hop(state)
    except RuntimeError:
        return False
    return str(hop.get("target_role") or "").upper() == logical_role


def _column_for(role: str) -> str:
    if role == "PLAN":
        return "PLANNING"
    if role == "DEV":
        return "WORKING"
    return "VERIFYING"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _authorized_mcp_connectors(rendered_prompt: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                match.lower()
                for match in re.findall(r"\bmcp-[A-Za-z0-9._-]+\b", str(rendered_prompt or ""))
            }
        )
    )


def _report_mode(state: Mapping[str, Any]) -> str:
    options = state.get("options")
    if not isinstance(options, Mapping):
        raise ValueError("task options must be a mapping")
    return report_mode_from_options(options)


def _workflow_report_roots(
    config: CDPAConfig, state: Mapping[str, Any]
) -> tuple[Path, Path]:
    repository = Path(str(state.get("repository") or "")).expanduser().resolve()
    try:
        plans_relative = config.plans_root.relative_to(config.repository_root)
    except ValueError as exc:
        raise ValueError(
            "workflow plans_root must be inside the control repository"
        ) from exc
    return repository, (repository / plans_relative).resolve()


_REMOTE_MCP_AUTHORITY = re.compile(r"\bUse\s+@(mcp-[A-Za-z0-9_-]+)\b", re.IGNORECASE)
def _assigned_remote_mcp_recipient(task_text: str) -> str:
    lines = [line.strip() for line in str(task_text or "").splitlines() if line.strip()]
    matches = {
        match.group(1).lower()
        for line in lines[:12]
        for match in _REMOTE_MCP_AUTHORITY.finditer(line)
    }
    if len(matches) != 1:
        raise RouteContractError("remote report task must declare exactly one assigned MCP authority")
    return f"{next(iter(matches))}.write_file"


def _remote_report_path(remote_repository: str, expected_report_path: str) -> str:
    relative = PurePosixPath(str(expected_report_path).strip())
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RouteContractError("remote report path must be one bounded relative path")
    candidate = PureWindowsPath(remote_repository).joinpath(*relative.parts)
    return str(candidate)


class CDPAWorker:
    def __init__(self, config: CDPAConfig, *, store: TaskStore | None = None) -> None:
        self.config = config
        configure_live_event_log(config.repository_root / ".runtime" / "live-events.jsonl")
        configure_action_delays(
            config.delay_minimum_seconds,
            config.delay_maximum_seconds,
            config.delay_multipliers,
        )
        self.store = store or TaskStore(config)
        self.managed_browsers = ManagedBrowserLifecycle(config)
        self.prompts = PromptBuilder(config)
        self.role_controller = RoleController(Policy(
            response_stable_seconds=config.response_stable_ms / 1000,
            timeout_seconds=config.response_refresh_after_seconds,
        ))
        self.runtime_db = RuntimeDB(config.runtime_database)
        self.registry: CDPARuntimeRegistry | None = None
        self.runtime_degraded = False
        self._runtime_ready = False
        self._command_loop_ready = False
        self._registry_publication_lock = threading.Lock()
        self._startup_command_states: dict[str, dict[str, Any]] = {}
        self._last_heartbeat_at = 0.0
        self._worker_started_at = utc_now()
        self._worker_lease_acquired_at: str | None = None
        self._source_identity = self._capture_source_identity()
        self._last_source_drift_check_at = 0.0
        self._source_drift: dict[str, Any] = {"drifted": False, "changed": []}
        self._last_browser_inventory_at = 0.0
        self._last_browser_page_count = -1
        self._browser_projection: dict[str, Any] | None = None
        self._browser_inventory_task: asyncio.Task[Any] | None = None
        self._browser_connected = False
        self._browser_error: str | None = None
        self._browser_cycle_active = False
        self._execution_leases: dict[str, dict[str, Any]] = {}
        self._endpoint_pool: EndpointPool | None = None
        self._endpoint_admission_locks: dict[str, asyncio.Lock] = {}
        self._endpoint_admission_holds: dict[asyncio.Task[Any], tuple[str, asyncio.Lock]] = {}
        self._endpoint_admission_loop: asyncio.AbstractEventLoop | None = None
        self._browser_command_tasks: set[asyncio.Task[Any]] = set()
        self._rate_limit_cooldown: dict[str, Any] | None = None
        self._rate_limit_cooldowns: dict[str, dict[str, Any]] = {}
        self._rate_limit_lock = asyncio.Lock()
        self._send_gate_lock = asyncio.Lock()
        self._last_automated_send_at: float | None = None
        self._ambient_controller_states: dict[int, dict[str, Any]] = {}
        self._ambient_next_probe_at: dict[int, float] = {}
        self._ambient_binding_cache: dict[int, dict[str, Any] | None] = {}
        self._bootstrap_prepare_lock = asyncio.Lock()
        self._repository_project_lock = asyncio.Lock()
        self._repository_project_tasks: set[asyncio.Task[Any]] = set()
        self.rate_limit_cooldown_seconds = config.rate_limit_quiet_seconds
        self._manifest_cache: dict[
            Path, tuple[tuple[int, int, int, int], dict[str, Any]]
        ] = {}

    @staticmethod
    def _manifest_identity(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def _source_identity_paths(self) -> list[Path]:
        package = Path(__file__).resolve().parent
        names = (
            "cdpa_worker.py", "cdpa_actions.py", "cdpa_commands.py", "cdpa_config.py",
            "cdpa_endpoint_pool.py", "cdpa_endpoints.py", "cdpa_managed_browser.py",
            "cdpa_projection.py", "cdpa_prompts.py", "cdpa_routes.py", "cdpa_runtime_db.py",
            "cdpa_runtime_registry.py", "cdpa_store.py", "durable.py", "file_lock.py",
            "observability.py", "role_runtime.py", "dashboard_api.py", "dashboard.html",
        )
        paths = [package / name for name in names]
        dashboard_assets = package / "dashboard_assets"
        if dashboard_assets.is_dir():
            paths.extend(
                sorted(
                    path
                    for path in dashboard_assets.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".js", ".css", ".html"}
                )
            )
        paths.extend(
            path
            for path in (
                self.config.repository_root / "cdpa.yaml",
                self.config.repository_root / "ecosystem.config.cjs",
            )
            if path.exists()
        )
        prompt_root = self.config.repository_root / "prompts" / "cdpa"
        if prompt_root.is_dir():
            paths.extend(sorted(prompt_root.glob("*.md")))
        return paths

    @staticmethod
    def _source_file_identity(path: Path) -> dict[str, Any]:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "sha256": digest,
        }

    def _source_identity_key(self, path: Path) -> str:
        resolved = path.resolve()
        root = self.config.repository_root.resolve()
        if resolved.is_relative_to(root):
            return str(resolved.relative_to(root)).replace("\\", "/")
        return f"runtime/{resolved.name}"

    def _capture_source_identity(self) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for path in self._source_identity_paths():
            relative = self._source_identity_key(path)
            try:
                files[relative] = self._source_file_identity(path)
            except FileNotFoundError:
                files[relative] = {"missing": True}
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {"fingerprint": fingerprint, "files": files, "captured_at": utc_now()}

    def _refresh_source_drift(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and now - self._last_source_drift_check_at < max(5.0, self.config.heartbeat_seconds):
            return self._source_drift
        self._last_source_drift_check_at = now
        changed: list[str] = []
        loaded_files = self._source_identity.get("files") or {}
        current_paths = {self._source_identity_key(path): path for path in self._source_identity_paths()}
        for relative in sorted(set(loaded_files) | set(current_paths)):
            loaded = loaded_files.get(relative)
            path = current_paths.get(relative)
            if path is None:
                changed.append(relative)
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                if not isinstance(loaded, Mapping) or loaded.get("missing") is not True:
                    changed.append(relative)
                continue
            if not isinstance(loaded, Mapping) or loaded.get("missing") is True:
                changed.append(relative)
                continue
            cheap = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            baseline = (loaded.get("size"), loaded.get("mtime_ns"), loaded.get("ctime_ns"))
            if cheap != baseline and hashlib.sha256(path.read_bytes()).hexdigest() != loaded.get("sha256"):
                changed.append(relative)
        self._source_drift = {"drifted": bool(changed), "changed": changed, "checked_at": utc_now()}
        return self._source_drift

    async def _acquire_endpoint_admission(
        self,
        state: Mapping[str, Any],
        role: str | None = None,
        *,
        endpoint_id: str | None = None,
    ) -> None:
        """Serialize endpoint admission; ``advance`` releases only after persistence."""
        endpoint_id = (
            str(endpoint_id).strip() or None
            if endpoint_id is not None
            else self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state)
        )
        if not endpoint_id:
            raise RuntimeError("endpoint admission requires a selected endpoint")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("endpoint admission requires an asyncio task")
        loop = asyncio.get_running_loop()
        if self._endpoint_admission_loop is not loop:
            if self._endpoint_admission_holds:
                raise RuntimeError("endpoint admission loop changed while a gate is held")
            self._endpoint_admission_locks.clear()
            self._endpoint_admission_loop = loop
        held = self._endpoint_admission_holds.get(task)
        if held is not None:
            if held[0] != endpoint_id:
                raise RuntimeError("one admission transaction cannot span multiple endpoints")
            return
        lock = self._endpoint_admission_locks.setdefault(endpoint_id, asyncio.Lock())
        await lock.acquire()
        self._endpoint_admission_holds[task] = (endpoint_id, lock)

    def _release_endpoint_admission(self) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        held = self._endpoint_admission_holds.pop(task, None)
        if held is not None:
            held[1].release()

    def _running_executions(
        self, tasks: Sequence[Mapping[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        source = list(tasks) if tasks is not None else list(
            self.registry.tasks_by_id.values() if self.registry is not None else ()
        )
        running: list[dict[str, Any]] = []
        for state in source:
            if str(state.get("status") or "").upper() != "RUNNING":
                continue
            role = str(state.get("active_role") or "").upper()
            record = (state.get("roles") or {}).get(role) if role else None
            if not isinstance(record, Mapping):
                continue
            endpoint_id = self._execution_endpoint_id(state)
            page_id = str(record.get("page_id") or "").strip()
            if not endpoint_id or not page_id or record.get("online") is not True:
                continue
            try:
                hop = _active_hop(state)
            except RuntimeError:
                continue
            if str(hop.get("target_role") or "").upper() != role:
                continue
            if str(hop.get("state") or "") not in {"pre_send", "sending", "sent", "waiting", "responded"}:
                continue
            running.append(
                {
                    "endpoint_id": endpoint_id,
                    "task_id": str(state.get("task_id") or ""),
                    "team": str(state.get("team") or ""),
                    "role": role,
                    "request_id": str(hop.get("request_id") or ""),
                    "page_id": page_id,
                }
            )
        by_task = {item["task_id"]: item for item in running}
        for task_id, lease in self._execution_leases.items():
            by_task[task_id] = dict(lease)
        return list(by_task.values())

    def _execution_endpoint_id(self, state: Mapping[str, Any], role: str | None = None) -> str | None:
        # Workflow teams own one endpoint for their entire lifetime. Roles do not
        # independently relocate between browser profiles/accounts.
        if not is_independent_task(state):
            endpoint_id = str(state.get("browser_endpoint_id") or "").strip()
            if state.get("endpoint_pinned", True) is True:
                return endpoint_id or self.store.endpoint_id_for(state)
            logical = role or str(state.get("active_role") or "").upper()
            record = (state.get("roles") or {}).get(logical)
            if isinstance(record, Mapping):
                placed = str(record.get("browser_endpoint_id") or "").strip()
                if placed and (record.get("page_id") or record.get("page_url")):
                    return placed
            return None
        logical = role or str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(logical)
        if not isinstance(record, Mapping):
            return None
        endpoint_id = str(record.get("browser_endpoint_id") or "").strip()
        if endpoint_id:
            return endpoint_id
        return self.store.endpoint_id_for(state)

    def _role_endpoint_affinity(self, state: Mapping[str, Any]) -> str | None:
        role = str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(role) if role else None
        if not isinstance(record, Mapping):
            return None
        if not record.get("page_id") or not record.get("page_url") or record.get("online") is not True:
            return None
        return self._execution_endpoint_id(state)

    def _execution_page_observed(self, state: Mapping[str, Any], endpoint_id: str | None) -> bool:
        if endpoint_id is None or not isinstance(self._browser_projection, Mapping):
            return True
        role = str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(role) if role else None
        page_id = str(record.get("page_id") or "").strip() if isinstance(record, Mapping) else ""
        if not page_id:
            return False
        endpoint = next(
            (
                item for item in self._browser_projection.get("endpoints") or ()
                if isinstance(item, Mapping) and str(item.get("endpoint_id") or "") == endpoint_id
            ),
            None,
        )
        if endpoint is None:
            return True
        return any(
            isinstance(page, Mapping) and str(page.get("page_id") or "").strip() == page_id
            for page in endpoint.get("pages") or ()
        )

    def _execution_is_pristine_pre_send(
        self,
        state: Mapping[str, Any],
        *,
        allow_prepared_ledger: bool = False,
    ) -> bool:
        role = str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(role) if role else None
        if not isinstance(record, Mapping):
            return False
        try:
            hop = _active_hop(state)
        except RuntimeError:
            return False
        if (
            str(hop.get("state") or "") != "pre_send"
            or hop.get("receipt") is not None
            or hop.get("message_identity") is not None
        ):
            return False
        uploaded_generation = record.get("attachments_uploaded_generation")
        if (
            uploaded_generation is not None
            and uploaded_generation == record.get("conversation_generation")
        ):
            return False
        request_id = str(hop.get("request_id") or "").strip()
        ledger_path = str(hop.get("ledger_path") or "").strip()
        if bool(request_id) != bool(ledger_path):
            return False
        if request_id:
            try:
                durable = RequestLedger(ledger_path).peek(request_id)
            except Exception:
                return False
            if durable is not None:
                if not allow_prepared_ledger:
                    # Safe restore and relocation keep the stricter contract:
                    # any durable request identity blocks replacement/replay.
                    return False
                if durable.status not in {RequestStatus.NEW, RequestStatus.PROMPT_SET}:
                    return False
                if int(durable.attempts or 0) != 0:
                    return False
                if any(
                    value is not None
                    for value in (
                        durable.binding,
                        durable.baseline,
                        durable.receipt,
                        durable.accepted_at,
                        durable.upload_receipt,
                        durable.response,
                        durable.session_id_before,
                        durable.error,
                    )
                ):
                    return False
        return True

    def _execution_can_relocate(
        self, state: Mapping[str, Any], target_endpoint_id: str
    ) -> bool:
        if not is_independent_task(state):
            if state.get("endpoint_pinned", True) is True:
                home = str(state.get("browser_endpoint_id") or "").strip() or self.store.endpoint_id_for(state)
                return bool(home and target_endpoint_id == home)
            return self._execution_is_pristine_pre_send(state)
        role = str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(role) if role else None
        if not isinstance(record, Mapping):
            return True
        current = self._execution_endpoint_id(state)
        if current == target_endpoint_id:
            return True
        try:
            hop = _active_hop(state)
        except RuntimeError:
            return False
        if str(hop.get("state") or "") == "pre_send":
            if not self._execution_is_pristine_pre_send(state):
                return False
            if current is None:
                return True
            request_id = str(hop.get("request_id") or "").strip()
            ledger_path = str(hop.get("ledger_path") or "").strip()
            if not request_id or not ledger_path:
                return False
            try:
                durable = RequestLedger(ledger_path).peek(request_id)
            except Exception:
                return False
            return (
                durable is not None
                and durable.status in {RequestStatus.NEW, RequestStatus.PROMPT_SET}
                and int(durable.attempts or 0) == 0
            )
        if (
            str(hop.get("state") or "") != "sending"
            or hop.get("receipt") is not None
            or hop.get("message_identity") is not None
        ):
            return False
        uploaded_generation = record.get("attachments_uploaded_generation")
        if uploaded_generation is not None and uploaded_generation == record.get("conversation_generation"):
            return False
        request_id = str(hop.get("request_id") or "").strip()
        ledger_path = str(hop.get("ledger_path") or "").strip()
        if str(hop.get("state") or "") == "sending":
            if not request_id or not ledger_path:
                return False
            try:
                durable = RequestLedger(ledger_path).peek(request_id)
            except Exception:
                return False
            return self._is_pristine_preboundary_sending_record(
                state, hop, durable
            )
        if request_id and ledger_path:
            try:
                durable = RequestLedger(ledger_path).peek(request_id)
            except Exception:
                return False
            if durable is not None:
                return False
        return True

    def _prepare_execution_endpoint(
        self, state: dict[str, Any], endpoint_id: str
    ) -> bool:
        if not is_independent_task(state):
            if state.get("endpoint_pinned", True) is True:
                home = str(state.get("browser_endpoint_id") or "").strip() or self.store.endpoint_id_for(state)
                if endpoint_id != home:
                    return False
            else:
                admission = (
                    state.get("admission")
                    if isinstance(state.get("admission"), Mapping)
                    else {}
                )
                if bool(admission.get("browser_endpoint_explicit", False)):
                    return False
                if bool(admission.get("required_endpoint_explicit", False)):
                    return False
                if not self._execution_is_pristine_pre_send(state):
                    return False
                role = str(state.get("active_role") or "").upper()
                role_record = (state.get("roles") or {}).get(role)
                if (
                    not isinstance(role_record, Mapping)
                    or role_record.get("page_id")
                    or role_record.get("page_url")
                ):
                    return False
                endpoint = self.store.endpoints.get(endpoint_id)
                mode_explicit = bool(
                    admission.get(
                        "conversation_mode_explicit",
                        state.get("conversation_mode_explicit", False),
                    )
                )
                context_explicit = bool(admission.get("context_policy_explicit", False))
                selected_mode = str(state.get("conversation_mode") or "")
                if not mode_explicit:
                    selected_mode = (
                        "temporary"
                        if endpoint.get("temporary_default")
                        else "persistent"
                    )
                if state.get("bootstrap") is not None and selected_mode == "temporary":
                    return False
                selected_context = state.get("context_policy")
                if not context_explicit and state.get("bootstrap") is None:
                    selected_context = "fresh"
                state["browser_endpoint_id"] = endpoint_id
                state["endpoint_pinned"] = True
                state["conversation_mode"] = selected_mode
                if selected_context is not None:
                    state["context_policy"] = selected_context
        role = str(state.get("active_role") or "").upper()
        record = state.get("roles", {}).get(role) if role else None
        if not isinstance(record, dict):
            return True
        current = self._execution_endpoint_id(state)
        if current != endpoint_id and not self._execution_can_relocate(state, endpoint_id):
            return False
        if current != endpoint_id:
            try:
                relocation_hop = _active_hop(state)
            except RuntimeError:
                relocation_hop = None
            pristine_relocation = (
                isinstance(relocation_hop, Mapping)
                and str(relocation_hop.get("state") or "") == "sending"
            )
            if current is not None:
                record["last_endpoint_id"] = current
            record.pop("branch_conversation_id", None)
            record.update(
                browser_endpoint_id=endpoint_id,
                page_id=None,
                page_url=None,
                online=False,
                last_error=None,
            )
            if state.get("conversation_mode") == "temporary":
                record.pop("temporary_document_id", None)
            if str(record.get("status") or "") == "active":
                record["status"] = "pending"
            try:
                hop = _active_hop(state)
            except RuntimeError:
                hop = None
            if isinstance(hop, dict) and str(hop.get("state") or "") in {"pre_send", "sending"}:
                hop["conversation_url"] = None
                hop["browser_endpoint_id"] = endpoint_id
                hop.pop("branch_conversation_id", None)
                if pristine_relocation:
                    hop["rebuild_prompt_after_relocation"] = True
        else:
            record["browser_endpoint_id"] = endpoint_id
            try:
                hop = _active_hop(state)
            except RuntimeError:
                hop = None
            if isinstance(hop, dict):
                hop["browser_endpoint_id"] = endpoint_id
        return True

    @staticmethod
    def _bounded_git_evidence(repository: object) -> dict[str, str | None]:
        root = Path(str(repository or "")).expanduser().resolve()
        result: dict[str, str | None] = {"git_status": None, "git_diff_stat": None}
        for key, args in (
            ("git_status", ("status", "--short")),
            ("git_diff_stat", ("diff", "--stat")),
            ("git_head", ("rev-parse", "HEAD")),
            ("git_branch", ("branch", "--show-current")),
        ):
            try:
                completed = subprocess.run(
                    ["git", "-C", str(root), *args],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode == 0:
                result[key] = completed.stdout.strip()[:8000]
        return result

    @staticmethod
    def _worktree_path_identity(path: object) -> str:
        return os.path.normcase(str(Path(str(path or "")).expanduser().resolve()))

    @staticmethod
    def _git_command(
        repository: Path,
        *args: str,
        check: bool = True,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repository), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"git command failed before completion: {sanitize_exception(exc)}") from exc
        if check and completed.returncode != 0:
            detail = sanitize_text(completed.stderr or completed.stdout or "git command failed", max_chars=1000)
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {detail}")
        return completed

    def _registered_git_worktrees(self) -> dict[str, dict[str, str | None]]:
        control_root = self.config.repository_root.expanduser().resolve()
        completed = self._git_command(control_root, "worktree", "list", "--porcelain")
        records: dict[str, dict[str, str | None]] = {}
        current: dict[str, str | None] | None = None
        for raw_line in completed.stdout.splitlines():
            line = raw_line.rstrip("\r\n")
            if line.startswith("worktree "):
                if current is not None and current.get("path"):
                    records[self._worktree_path_identity(current["path"])] = current
                current = {"path": line[len("worktree "):], "head": None, "branch": None}
            elif current is not None and line.startswith("HEAD "):
                current["head"] = line[len("HEAD "):].strip() or None
            elif current is not None and line.startswith("branch "):
                current["branch"] = line[len("branch "):].strip() or None
            elif current is not None and line == "detached":
                current["branch"] = None
        if current is not None and current.get("path"):
            records[self._worktree_path_identity(current["path"])] = current
        return records

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        try:
            return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
        except OSError:
            return True

    def _archive_team_evidence(
        self,
        *,
        repository: Path,
        team: str,
    ) -> list[dict[str, Any]]:
        control_root = self.config.repository_root.expanduser().resolve()
        source_root = repository / ".plan" / team
        destination_root = control_root / ".plan" / team
        if not source_root.exists():
            return []
        for boundary in (repository / ".plan", source_root):
            if self._is_link_like(boundary):
                raise RuntimeError("worktree evidence namespace must not cross a symlink or junction")
            if not boundary.is_dir():
                raise RuntimeError("worktree evidence namespace is not a directory")
        if destination_root.exists() and (
            self._is_link_like(destination_root) or not destination_root.is_dir()
        ):
            raise RuntimeError("archive destination is not a safe directory")
        destination_root.mkdir(parents=True, exist_ok=True)

        archived: list[dict[str, Any]] = []
        for source in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
            if self._is_link_like(source):
                raise RuntimeError(f"worktree evidence contains a symlink or junction: {source}")
            if source.is_dir():
                continue
            if not source.is_file():
                raise RuntimeError(f"worktree evidence contains an unsupported entry: {source}")
            relative = source.relative_to(source_root)
            destination = destination_root / relative
            current_parent = destination_root
            for part in relative.parts[:-1]:
                current_parent = current_parent / part
                if current_parent.exists() and (
                    self._is_link_like(current_parent) or not current_parent.is_dir()
                ):
                    raise RuntimeError("archive destination parent is not a safe directory")
                current_parent.mkdir(exist_ok=True)
            data = source.read_bytes()
            if destination.exists():
                if self._is_link_like(destination) or not destination.is_file():
                    raise RuntimeError(f"archive destination is not a regular file: {destination}")
                if destination.read_bytes() != data:
                    raise RuntimeError(
                        f"archive destination already contains different evidence: {destination}"
                    )
            else:
                try:
                    with destination.open("xb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                except FileExistsError:
                    if (
                        self._is_link_like(destination)
                        or not destination.is_file()
                        or destination.read_bytes() != data
                    ):
                        raise RuntimeError(
                            f"archive destination changed while evidence was copied: {destination}"
                        )
            archived.append(
                {
                    "path": destination.relative_to(control_root).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
        return archived

    def _assert_worktree_archive_ownership(
        self,
        repository: Path,
        *,
        allocation_locked: bool = False,
    ) -> None:
        tasks, errors = (
            self.store._discover_with_errors_unlocked()
            if allocation_locked
            else self.store.discover_with_errors()
        )
        if errors:
            raise RuntimeError("cannot prove worktree ownership while task discovery has errors")
        target_identity = self._worktree_path_identity(repository)
        owners = [
            str(task.get("task_id") or "")
            for task in tasks
            if str(task.get("status") or "").upper() not in TERMINAL
            and self._worktree_path_identity(task.get("repository")) == target_identity
        ]
        if owners:
            raise RuntimeError(
                "cannot archive a worktree still owned by nonterminal tasks: "
                + ", ".join(sorted(owner for owner in owners if owner))
            )

    def _prepare_worktree_archive(self, state: Mapping[str, Any]) -> dict[str, Any]:
        control_root = self.config.repository_root.expanduser().resolve()
        repository = Path(str(state.get("repository") or "")).expanduser().resolve()
        if self._worktree_path_identity(repository) == self._worktree_path_identity(control_root):
            raise RuntimeError("the controller worktree cannot archive itself")
        worktrees = self._registered_git_worktrees()
        record = worktrees.get(self._worktree_path_identity(repository))
        if record is None:
            raise RuntimeError("archive target is not a registered linked Git worktree")
        head = str(record.get("head") or "").strip()
        if not head:
            raise RuntimeError("archive target has no durable Git HEAD")
        status = self._git_command(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout.strip()
        if status:
            raise RuntimeError("worktree archival refuses modified or untracked repository data")
        self._assert_worktree_archive_ownership(repository)
        evidence_files = self._archive_team_evidence(
            repository=repository,
            team=str(state.get("team") or ""),
        )
        ref_digest = hashlib.sha256(str(state.get("task_id") or "").encode("utf-8")).hexdigest()[:24]
        preservation_ref = f"refs/cdpa/worktree-archive/{ref_digest}"
        existing = self._git_command(
            control_root,
            "rev-parse",
            "--verify",
            preservation_ref,
            check=False,
        )
        if existing.returncode == 0:
            if existing.stdout.strip() != head:
                raise RuntimeError("worktree preservation ref already points at another commit")
        else:
            self._git_command(control_root, "update-ref", preservation_ref, head)
        verified = self._git_command(
            control_root,
            "rev-parse",
            "--verify",
            preservation_ref,
        ).stdout.strip()
        if verified != head:
            raise RuntimeError("worktree preservation ref verification failed")
        return {
            "state": "PREPARED",
            "repository": str(repository),
            "head": head,
            "branch": record.get("branch"),
            "preservation_ref": preservation_ref,
            "evidence_root": f".plan/{state.get('team')}",
            "evidence_files": evidence_files,
            "prepared_at": utc_now(),
            "archived_at": None,
        }

    def _finish_worktree_archive(
        self,
        state: Mapping[str, Any],
        archive: Mapping[str, Any],
    ) -> dict[str, Any]:
        control_root = self.config.repository_root.expanduser().resolve()
        repository = Path(str(archive.get("repository") or state.get("repository") or "")).expanduser().resolve()
        state_repository = Path(str(state.get("repository") or "")).expanduser().resolve()
        if self._worktree_path_identity(repository) != self._worktree_path_identity(state_repository):
            raise RuntimeError("prepared worktree archive repository no longer matches task ownership")
        head = str(archive.get("head") or "").strip()
        preservation_ref = str(archive.get("preservation_ref") or "").strip()
        if not head or not preservation_ref:
            raise RuntimeError("prepared worktree archive is missing Git preservation identity")
        verified = self._git_command(
            control_root,
            "rev-parse",
            "--verify",
            preservation_ref,
        ).stdout.strip()
        if verified != head:
            raise RuntimeError("prepared worktree preservation ref no longer matches archived HEAD")

        target_identity = self._worktree_path_identity(repository)
        with exclusive_file_lock(self.store.allocation_lock):
            worktrees = self._registered_git_worktrees()
            if target_identity in worktrees:
                current_record = worktrees[target_identity]
                if str(current_record.get("head") or "").strip() != head:
                    raise RuntimeError("worktree HEAD changed after archive preparation")
                if current_record.get("branch") != archive.get("branch"):
                    raise RuntimeError("worktree branch identity changed after archive preparation")
                self._assert_worktree_archive_ownership(
                    repository,
                    allocation_locked=True,
                )
                status = self._git_command(
                    repository,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ).stdout.strip()
                if status:
                    raise RuntimeError("worktree became dirty after archive preparation")
                self._git_command(control_root, "worktree", "remove", str(repository))
            elif repository.exists():
                raise RuntimeError("archive target exists but is no longer a registered linked worktree")

            if target_identity in self._registered_git_worktrees() or repository.exists():
                raise RuntimeError("linked worktree removal did not reach the required absent postcondition")
        if self._git_command(
            control_root,
            "rev-parse",
            "--verify",
            preservation_ref,
        ).stdout.strip() != head:
            raise RuntimeError("worktree preservation ref changed during archival")
        result = dict(archive)
        result["state"] = "ARCHIVED"
        result["archived_at"] = utc_now()
        return result

    def _apply_worktree_archive_control(
        self,
        state: Mapping[str, Any],
        manifest_path: Path,
        control: Mapping[str, Any],
    ) -> dict[str, Any]:
        control_id = control.get("control_id")
        if isinstance(control_id, bool) or not isinstance(control_id, int) or control_id < 1:
            raise RuntimeError("worktree archival requires a durable control ID")
        if not bool(control.get("confirmed")):
            raise RuntimeError("worktree archival requires explicit operator confirmation")

        archive = state.get("worktree_archive")
        if archive is not None and not isinstance(archive, Mapping):
            raise RuntimeError("persisted worktree archive state is invalid")

        idempotent = bool(isinstance(archive, Mapping) and archive.get("state") == "ARCHIVED")
        if idempotent:
            completed = dict(archive)
        else:
            if archive is None:
                prepared = self._prepare_worktree_archive(state)

                def persist_prepared(current: dict[str, Any]) -> dict[str, Any]:
                    eligibility = workflow_control_eligibility(current, "archive_worktree")
                    if not eligibility["eligible"]:
                        raise RuntimeError(
                            str(eligibility["reason"] or "worktree archival is no longer eligible")
                        )
                    existing = current.get("worktree_archive")
                    if existing is not None:
                        raise RuntimeError("worktree archive preparation raced with another update")
                    pending = next(
                        (
                            item
                            for item in current.get("controls") or []
                            if isinstance(item, dict) and item.get("control_id") == control_id
                        ),
                        None,
                    )
                    if pending is None or pending.get("status") != "requested":
                        raise RuntimeError("worktree archive control is no longer pending")
                    current["worktree_archive"] = dict(prepared)
                    return current

                prepared_state = self.store.update(manifest_path, persist_prepared)
                archive = prepared_state["worktree_archive"]
                state = prepared_state
            elif archive.get("state") != "PREPARED":
                raise RuntimeError("persisted worktree archive has an unsupported lifecycle state")
            completed = self._finish_worktree_archive(state, archive)

        def persist_completed(current: dict[str, Any]) -> dict[str, Any]:
            current_archive = current.get("worktree_archive")
            if idempotent:
                if not isinstance(current_archive, Mapping) or current_archive.get("state") != "ARCHIVED":
                    raise RuntimeError("completed worktree archive state changed before idempotent replay")
            else:
                if not isinstance(current_archive, Mapping) or current_archive.get("state") != "PREPARED":
                    raise RuntimeError("prepared worktree archive state changed before completion")
                for field in ("repository", "head", "preservation_ref"):
                    if current_archive.get(field) != completed.get(field):
                        raise RuntimeError(f"prepared worktree archive {field} changed before completion")
                current["worktree_archive"] = dict(completed)
            current_control = next(
                (
                    item
                    for item in current.get("controls") or []
                    if isinstance(item, dict) and item.get("control_id") == control_id
                ),
                None,
            )
            if current_control is None:
                raise RuntimeError("worktree archive control disappeared before completion")
            if current_control.get("status") not in {"requested", "applied"}:
                raise RuntimeError("worktree archive control is no longer applicable")
            current_control["status"] = "applied"
            current_control["command_state"] = "APPLIED"
            current_control["result"] = {
                "state": "ARCHIVED",
                "head": completed.get("head"),
                "preservation_ref": completed.get("preservation_ref"),
                "evidence_files": list(completed.get("evidence_files") or []),
                "idempotent": idempotent,
            }
            current_control["applied_at"] = utc_now()
            return current

        return self.store.update(manifest_path, persist_completed)

    def _recovery_current_report(self, state: Mapping[str, Any], hop: Mapping[str, Any]) -> dict[str, Any] | None:
        if is_independent_task(state):
            return None
        repository, plans = _workflow_report_roots(self.config, state)
        expected = expected_report_relative(
            repository_root=repository, plans_root=plans, team=str(state["team"]),
            physical_role=str(hop["physical_role"]), turn=int(hop["turn"]), task_id=str(state["task_id"]),
        )
        result: dict[str, Any] = {"path": expected, "absolute_path": str(repository / expected), "availability": "missing"}
        try:
            if (repository / expected).stat().st_size > 1024 * 1024:
                return {**result, "availability": "too_large", "requires_filesystem_inspection": True}
            report = validate_file_report(
                expected, repository_root=repository, plans_root=plans, team=str(state["team"]),
                physical_role=str(hop["physical_role"]), turn=int(hop["turn"]), task_id=str(state["task_id"]),
            )
            with (repository / report.path).open("rb") as handle:
                body = handle.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024 or hashlib.sha256(body).hexdigest() != report.sha256:
                raise ValueError("report changed while capturing recovery evidence")
            return {**result, "availability": "verified", "sha256": report.sha256, "size": report.size,
                    "content": body.decode("utf-8", errors="replace")[:8000], "content_truncated": len(body) > 8000}
        except FileNotFoundError:
            return result
        except (OSError, ValueError) as exc:
            return {**result, "availability": "invalid", "error": sanitize_text(exc, max_chars=500)}

    def _recovery_checkpoints(self, state: Mapping[str, Any]) -> list[dict[str, Any]]:
        root = Path(str(state["repository"])).expanduser().resolve()
        candidates = ["checkpoint.md", "CHECKPOINT.md"]
        for item in state.get("checkpoints", []) if isinstance(state.get("checkpoints", []), list) else []:
            path = item.get("path") if isinstance(item, Mapping) else item
            if isinstance(path, str):
                candidates.append(path)
        result: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for name in candidates[:6]:
            path = root / name
            try:
                if path in seen or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                seen.add(path)
                if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents) if p != root):
                    continue
                with path.open("rb") as handle:
                    body = handle.read(65537)
                if len(body) > 65536:
                    result.append({"path": str(path), "availability": "too_large", "requires_filesystem_inspection": True})
                    continue
                result.append({"path": str(path), "availability": "observed", "sha256": hashlib.sha256(body).hexdigest(),
                               "size": len(body), "content": body.decode("utf-8", errors="replace")[:8000]})
            except OSError:
                continue
        return result

    def _recovery_recent_logs(self, task_id: str) -> list[dict[str, Any]]:
        path = self.config.repository_root / ".runtime" / "live-events.jsonl"
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 65536))
                lines = handle.read(65536).decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        selected: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, Mapping) or item.get("task_id") != task_id:
                continue
            selected.append({key: sanitize_value(item[key]) for key in ("at", "source", "kind", "role", "values", "changes") if key in item})
        return selected[-12:]

    @staticmethod
    def _compact_recovery_value(
        value: Any,
        *,
        string_limit: int,
        collection_limit: int,
        depth: int = 0,
    ) -> Any:
        if isinstance(value, Mapping):
            items = list(value.items())
            if depth >= 2 and len(items) > collection_limit:
                items = items[:collection_limit]
            return {
                str(key): CDPAWorker._compact_recovery_value(
                    item,
                    string_limit=string_limit,
                    collection_limit=collection_limit,
                    depth=depth + 1,
                )
                for key, item in items
            }
        if isinstance(value, (list, tuple)):
            selected = list(value)[-collection_limit:]
            return [
                CDPAWorker._compact_recovery_value(
                    item,
                    string_limit=string_limit,
                    collection_limit=collection_limit,
                    depth=depth + 1,
                )
                for item in selected
            ]
        if isinstance(value, str):
            return sanitize_text(value, max_chars=string_limit)
        return copy.deepcopy(value)

    @classmethod
    def _bound_execution_recovery_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        def encoded_size(value: Mapping[str, Any]) -> int:
            return len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        if encoded_size(snapshot) <= _RECOVERY_SNAPSHOT_MAX_CHARS:
            return snapshot
        for string_limit, collection_limit in ((1200, 8), (600, 6), (300, 4), (150, 3)):
            candidate = cls._compact_recovery_value(
                snapshot,
                string_limit=string_limit,
                collection_limit=collection_limit,
            )
            if isinstance(candidate, dict):
                candidate["snapshot_truncated"] = True
                if encoded_size(candidate) <= _RECOVERY_SNAPSHOT_MAX_CHARS:
                    return candidate
        raise ValueError("execution recovery snapshot cannot be bounded safely")

    def _build_execution_recovery_snapshot(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        *,
        reason: str,
        compact_continuation: bool = False,
    ) -> dict[str, Any]:
        request_id = str(hop.get("request_id") or "")
        ledger_status = None
        accepted_at = None
        attempts = 0
        response = None
        ledger_path = str(hop.get("ledger_path") or "").strip()
        if request_id and ledger_path:
            try:
                record = RequestLedger(ledger_path).peek(request_id)
            except Exception:
                record = None
            if record is not None:
                ledger_status = record.status.value
                accepted_at = record.accepted_at
                attempts = int(record.attempts or 0)
                response = dict(record.response) if isinstance(record.response, Mapping) else None
        receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else None
        crossed = bool(
            receipt
            or attempts > 0
            or accepted_at is not None
            or ledger_status in {"sending", "sent", "completed"}
        )
        role = str(hop.get("target_role") or state.get("active_role") or "").upper()
        endpoint_id = self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state)
        reports = [
            {
                key: item.get(key)
                for key in ("path", "role", "turn", "sha256", "size", "created_at")
                if key in item
            }
            for item in (state.get("reports") or [])[-12:]
            if isinstance(item, Mapping)
        ]
        repository = str(state.get("repository") or "")
        compact_receipt = {
            key: copy.deepcopy(receipt[key])
            for key in ("prompt_sha256", "binding", "user_message_id", "user_turn_id", "conversation_id", "accepted_via", "attempts", "session_id_before")
            if receipt is not None and key in receipt
        } if receipt is not None else None
        manifest_path = str(state.get("manifest_path") or "").strip()
        if compact_continuation:
            report_ref = str(hop.get("report_path") or hop.get("expected_report_path") or "").strip()
            if not report_ref:
                report_ref = next(
                    (
                        str(item.get("path") or "").strip()
                        for item in reversed(state.get("reports") or [])
                        if isinstance(item, Mapping) and str(item.get("path") or "").strip()
                    ),
                    "",
                )
            compact_snapshot = {
                "schema_version": 1,
                "reason": sanitize_text(reason, max_chars=600),
                "task": {
                    "task_id": str(state.get("task_id") or ""),
                    "team": str(state.get("team") or ""),
                },
                "execution": {
                    "role": role,
                    "physical_role": str(hop.get("physical_role") or ""),
                    "hop_id": hop.get("hop_id"),
                    "turn": hop.get("turn"),
                    "request_id": request_id,
                    "endpoint_id": endpoint_id,
                    "recovery_of_request_id": request_id,
                },
                "send_provenance": {
                    "classification": "accepted_or_ambiguous" if crossed else "pre_send",
                    "boundary": "accepted" if receipt or accepted_at else "ambiguous" if crossed else "not_sent",
                    "ledger_status": ledger_status,
                    "attempts": attempts,
                    "accepted_at": accepted_at,
                    "receipt": compact_receipt,
                },
                "durable_paths": {
                    "workspace": str(Path(repository).expanduser().resolve()) if repository else "",
                    "manifest": manifest_path,
                    "ledger": ledger_path,
                    "report": report_ref or None,
                },
                "instruction": (
                    "Read durable workspace/manifest/ledger/report state, continue only unfinished work, "
                    "and never replay the accepted original request or completed irreversible actions."
                ),
            }
            encoded = json.dumps(
                compact_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(encoded) > _EXECUTION_RECOVERY_SNAPSHOT_MAX_CHARS:
                raise ValueError(
                    "compact execution recovery snapshot exceeds the safe continuation bound"
                )
            return compact_snapshot
        compact_response = {
            key: (str(response[key])[:8000] if key == "text" else response[key])
            for key in ("role", "text", "message_id", "turn_id") if response is not None and key in response
        } if response is not None else None
        goal = task_goal_for_hop(state, int(hop["hop_id"]))
        task_text_ref = f"{manifest_path}#task_text" if manifest_path else f"task:{state.get('task_id') or ''}#task_text"
        snapshot = {
            "schema_version": 1,
            "source_of_truth": ["workspace_reports_checkpoints_git", "durable_request_ledger", "assistant_progress_advisory"],
            "reason": sanitize_text(reason, max_chars=1000),
            "task": {
                "task_id": str(state.get("task_id") or ""),
                "team": str(state.get("team") or ""),
                "goal": sanitize_text(goal, max_chars=_RECOVERY_GOAL_MAX_CHARS),
                "goal_truncated": len(goal) > _RECOVERY_GOAL_MAX_CHARS,
                "task_text_ref": task_text_ref,
                "context_policy": state.get("context_policy"),
                "conversation_mode": str(state.get("conversation_mode") or "persistent"),
            },
            "execution": {
                "role": role,
                "physical_role": str(hop.get("physical_role") or ""),
                "hop_id": hop.get("hop_id"),
                "turn": hop.get("turn"),
                "request_id": request_id,
                "endpoint_id": endpoint_id,
                "hop_state": str(hop.get("state") or ""),
                "handoff": str(hop.get("handoff") or "")[:4000],
                "handoff_truncated": len(str(hop.get("handoff") or "")) > 4000,
            },
            "send_provenance": {
                "classification": "accepted_or_ambiguous" if crossed else "pre_send",
                "boundary": "accepted" if receipt or accepted_at else "ambiguous" if crossed else "not_sent",
                "ledger_status": ledger_status,
                "attempts": attempts,
                "accepted_at": accepted_at,
                "receipt": compact_receipt,
            },
            "durable_paths": {"manifest": str(state.get("manifest_path") or ""), "ledger": ledger_path,
                              "logs": str(self.config.repository_root / ".runtime" / "live-events.jsonl")},
            "workspace": {
                "repository": str(Path(repository).expanduser().resolve()) if repository else "",
                **self._bounded_git_evidence(repository),
            },
            "reports": reports,
            "current_report": self._recovery_current_report(state, hop),
            "checkpoints": self._recovery_checkpoints(state),
            "recent_logs": self._recovery_recent_logs(str(state["task_id"])),
            "validation": {"error": hop.get("validation_error"),
                           "test_evidence": sanitize_value(state.get("test_evidence")),
                           "validation_evidence": sanitize_value(state.get("validation_evidence"))},
            "last_progress": {
                "response": compact_response,
                "assistant": copy.deepcopy((hop.get("wait") or {}).get("last_assistant_progress")),
                "advisory_only": True,
                "hop_response": str(hop.get("response") or "")[:8000] or None,
                "errors": [str(item)[:1000] for item in (hop.get("errors") or [])[-5:]],
            },
        }
        return self._bound_execution_recovery_snapshot(snapshot)

    def _recompact_stale_execution_recovery_handoff(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
    ) -> str:
        if str(hop.get("kind") or "") != "execution_recovery":
            raise DurableRequestError("stale handoff recompact requires execution recovery")

        validation_hop = dict(hop)
        validation_hop["state"] = "sending"
        child_request_id = str(hop.get("request_id") or "").strip()
        child_ledger_path = str(hop.get("ledger_path") or "").strip()
        if not child_request_id or not child_ledger_path:
            raise DurableRequestError("execution recovery child has no durable identity")
        child_record = RequestLedger(child_ledger_path).peek(child_request_id)
        if not self._is_pristine_preboundary_sending_record(
            state, validation_hop, child_record
        ):
            raise DurableRequestError(
                "execution recovery child is no longer provably pristine"
            )

        parent_hop_id = hop.get("parent_hop_id")
        parents = [
            item
            for item in state.get("hops") or []
            if isinstance(item, Mapping) and item.get("hop_id") == parent_hop_id
        ]
        if len(parents) != 1:
            raise DurableRequestError(
                "execution recovery parent hop cannot be resolved exactly"
            )
        parent = parents[0]
        original_request_id = str(hop.get("recovery_of_request_id") or "").strip()
        if (
            not original_request_id
            or str(parent.get("request_id") or "").strip() != original_request_id
            or str(parent.get("physical_role") or "")
            != str(hop.get("physical_role") or "")
            or str(parent.get("target_role") or "").upper()
            != str(hop.get("target_role") or "").upper()
            or str(hop.get("source_role") or "").upper()
            != str(parent.get("target_role") or "").upper()
        ):
            raise DurableRequestError("execution recovery lineage does not match parent")

        lineage = parent.get("execution_recovery")
        if (
            not isinstance(lineage, Mapping)
            or str(lineage.get("original_request_id") or "").strip()
            != original_request_id
        ):
            raise DurableRequestError(
                "execution recovery parent provenance is missing or mismatched"
            )

        parent_ledger_path = str(parent.get("ledger_path") or "").strip()
        if not parent_ledger_path:
            raise DurableRequestError("execution recovery parent ledger is missing")
        parent_record = RequestLedger(parent_ledger_path).peek(original_request_id)
        expected_source = {
            "task_id": state.get("task_id"),
            "team": state.get("team"),
            "hop_id": parent.get("hop_id"),
            "manifest": state.get("manifest_path"),
        }
        if (
            parent_record is None
            or parent_record.request_id != original_request_id
            or parent_record.role != str(parent.get("physical_role") or "")
            or parent_record.source_context != expected_source
        ):
            raise DurableRequestError(
                "execution recovery original durable provenance does not match parent"
            )
        expected_parent_key = build_idempotency_key(
            role=parent_record.role,
            prompt=parent_record.prompt,
            source_context=expected_source,
            role_prompt_hash=parent_record.role_prompt_hash,
            files=parent_record.files,
        )
        if parent_record.idempotency_key != expected_parent_key:
            raise DurableRequestError(
                "execution recovery original durable provenance key is invalid"
            )
        crossed_send = bool(
            parent_record.receipt
            or parent_record.accepted_at is not None
            or int(parent_record.attempts or 0) > 0
            or parent_record.status
            in {RequestStatus.SENDING, RequestStatus.SENT, RequestStatus.COMPLETED}
        )
        if not crossed_send:
            raise DurableRequestError(
                "execution recovery parent has no accepted or ambiguous Send evidence"
            )

        snapshot = self._build_execution_recovery_snapshot(
            state,
            parent,
            reason=str(parent.get("abandon_reason") or "execution recovery continuation"),
            compact_continuation=True,
        )
        if (
            snapshot.get("send_provenance", {}).get("classification")
            != "accepted_or_ambiguous"
            or snapshot.get("execution", {}).get("request_id")
            != original_request_id
        ):
            raise DurableRequestError(
                "execution recovery compact snapshot lost original Send provenance"
            )
        return _format_execution_recovery_handoff(snapshot)

    def _consume_completed_durable_response(
        self, state: dict[str, Any], hop: dict[str, Any]
    ) -> bool:
        request_id = str(hop.get("request_id") or "").strip()
        ledger_path = str(hop.get("ledger_path") or "").strip()
        if not request_id or not ledger_path:
            return False
        try:
            record = RequestLedger(ledger_path).peek(request_id)
        except Exception:
            return False
        if (
            record is None
            or record.status is not RequestStatus.COMPLETED
            or not isinstance(record.response, Mapping)
        ):
            return False
        response = MessageSnapshot.from_dict(record.response)
        if response.role != "assistant" or not response.text.strip():
            return False
        self._record_response(state, hop, response)
        self._responded(state, hop)
        return True

    def _prepare_cross_endpoint_recovery(
        self,
        state: dict[str, Any],
        endpoint_id: str,
        *,
        reason: str,
        force_new_generation: bool = False,
    ) -> bool:
        if not is_independent_task(state):
            home = str(state.get("browser_endpoint_id") or "").strip() or self.store.endpoint_id_for(state)
            if endpoint_id != home:
                self._wait_queue_error(
                    state,
                    "Workflow team is pinned to its original browser endpoint; waiting for that endpoint instead of moving roles across accounts.",
                    code="team_endpoint_unavailable",
                )
                return False
        hop = _active_hop(state)
        try:
            ledger_path = str(hop.get("ledger_path") or "")
            request_id = str(hop.get("request_id") or "")
            if not ledger_path or not request_id:
                raise ValueError("missing exact request ledger identity")
            durable = RequestLedger(ledger_path).peek(request_id)
            if durable is not None:
                expected_source = {"task_id": state["task_id"], "team": state["team"],
                                   "hop_id": hop["hop_id"], "manifest": state["manifest_path"]}
                if durable.role != hop.get("physical_role") or (
                    durable.source_context is not None and durable.source_context != expected_source
                ):
                    raise ValueError("request provenance belongs to another execution")
        except Exception as exc:
            self._block(state, f"Cannot safely recover execution provenance: {exc}",
                        code="execution_recovery_provenance_invalid", retryable=False)
            return False
        if self._consume_completed_durable_response(state, hop):
            return self._prepare_execution_endpoint(state, endpoint_id)
        role = str(hop.get("target_role") or state.get("active_role") or "").upper()
        role_record = state.get("roles", {}).get(role)
        if not isinstance(role_record, dict):
            return False
        if not force_new_generation and self._execution_can_relocate(state, endpoint_id):
            return self._prepare_execution_endpoint(state, endpoint_id)
        snapshot = self._build_execution_recovery_snapshot(
            state, hop, reason=reason, compact_continuation=True
        )
        if snapshot["send_provenance"]["classification"] != "accepted_or_ambiguous":
            return False
        old_hop_id = int(hop["hop_id"])
        original_request_id = str(hop.get("request_id") or "")
        hop.setdefault("browser_endpoint_id", self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state))
        hop["state"] = "abandoned"
        hop["abandon_reason"] = sanitize_text(reason, max_chars=1000)
        hop["execution_recovery"] = {
            "original_request_id": original_request_id,
            "target_endpoint_id": endpoint_id,
            "snapshot": snapshot,
        }
        hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
        handoff = _format_execution_recovery_handoff(snapshot)
        child = self._append_hop(
            state,
            source_role=role,
            target_role=role,
            handoff=handoff,
            kind="independent_job" if is_independent_task(state) else "execution_recovery",
        )
        child["recovery_of_request_id"] = original_request_id
        parent_kind = str(hop.get("kind") or "task")
        child["recovered_parent_kind"] = parent_kind
        if parent_kind in {"route_repair", "report_repair"}:
            for key in (
                "validation_error",
                "locked_route",
                "locked_handoff",
                "expected_report_path",
                "repair_attempt",
            ):
                if key in hop:
                    child[key] = copy.deepcopy(hop[key])
        if parent_kind == "accepted_send_reconciliation":
            for key, value in hop.items():
                if str(key).startswith("reconciliation_"):
                    child[key] = copy.deepcopy(value)
        role_record.pop("branch_conversation_id", None)
        role_record.update(
            last_endpoint_id=self._execution_endpoint_id(state, role),
            browser_endpoint_id=endpoint_id,
            page_id=None,
            page_url=None,
            online=False,
            last_error=None,
            constructor_sent_generation=None,
            attachments_uploaded_generation=None,
        )
        if is_independent_task(state):
            role_record.pop("context_source", None)
        else:
            role_record["context_source"] = "execution_recovery"
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": old_hop_id,
                "source_role": role,
                "route": role,
                "kind": "execution_recovery",
                "new_hop_id": child["hop_id"],
                "reason": sanitize_text(reason, max_chars=1000),
            }
        )
        return True

    def _remember_manifest(self, path: str | Path, state: dict[str, Any]) -> None:
        target = Path(path).expanduser().resolve()
        self._manifest_cache[target] = (self._manifest_identity(target), state)

    def _load_manifest_cached(
        self,
        path: str | Path,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        identity = self._manifest_identity(target)
        cached = self._manifest_cache.get(target)
        if not force and cached is not None and cached[0] == identity:
            return cached[1]
        state = self.store.load(target)
        loaded_identity = self._manifest_identity(target)
        if loaded_identity != identity:
            state = self.store.load(target)
            loaded_identity = self._manifest_identity(target)
        self._manifest_cache[target] = (loaded_identity, state)
        return state

    async def _group_repository_project(
        self, repository: str, conversation_id: str, browser_context: Any
    ) -> None:
        """Attach one saved conversation to the repository-named Project.

        The durable mapping is tried first so concurrent roles/restarts do not create
        duplicate Projects. A 404/not-ready mapping is treated as account-local stale
        state and re-resolved against the current browser context; other failures stay
        fail-soft and never create a second Project speculatively.
        """
        try:
            mappings = self.store.repository_projects()
        except Exception:
            mappings = {}
        mapped = mappings.get(repository)
        if mapped:
            try:
                await backend_set_conversation_project(
                    browser_context, conversation_id, mapped
                )
                return
            except BackendNotReadyError:
                pass
            except Exception:
                return
        try:
            async with self._repository_project_lock:
                try:
                    mappings = self.store.repository_projects()
                except Exception:
                    mappings = {}
                mapped = mappings.get(repository)
                if mapped:
                    try:
                        await backend_set_conversation_project(
                            browser_context, conversation_id, mapped
                        )
                        return
                    except BackendNotReadyError:
                        pass
                    except Exception:
                        return
                name = Path(repository).name
                if any(
                    Path(path).name == name and path != repository
                    for path in mappings
                ):
                    suffix = hashlib.sha256(repository.encode()).hexdigest()[:8]
                    name = f"{name}-{suffix}"
                remote = await backend_projects(browser_context)
                exact = [item for item in remote if item.get("name") == name]
                if len(exact) > 1:
                    return
                if exact:
                    project_id = str(exact[0]["id"])
                else:
                    project_id = await backend_create_project(browser_context, name)
                try:
                    self.store.set_repository_project(repository, project_id)
                except ValueError:
                    pass
            await backend_set_conversation_project(
                browser_context, conversation_id, project_id
            )
        except Exception:
            # Project grouping is organizational metadata and must never block work.
            return

    def _schedule_repository_project(
        self, state: Mapping[str, Any], hop_id: int, browser_context: Any
    ) -> asyncio.Task[Any] | None:
        if is_independent_task(state):
            return
        hop = next(
            (item for item in state.get("hops") or [] if item.get("hop_id") == hop_id),
            None,
        )
        receipt = hop.get("receipt") if isinstance(hop, Mapping) else None
        repository = state.get("repository")
        conversation_id = receipt.get("conversation_id") if isinstance(receipt, Mapping) else None
        if not conversation_id and isinstance(hop, Mapping):
            identity = _recoverable_conversation_identity(hop.get("conversation_url"))
            if identity:
                conversation_id = identity.rsplit("/", 1)[-1]
        if not conversation_id and isinstance(hop, Mapping):
            role = str(hop.get("target_role") or "").upper()
            role_record = (state.get("roles") or {}).get(role)
            if isinstance(role_record, Mapping):
                identity = _recoverable_conversation_identity(role_record.get("page_url"))
                if identity:
                    conversation_id = identity.rsplit("/", 1)[-1]
        if (
            state.get("conversation_mode") == "temporary"
            or not isinstance(hop, Mapping)
            or hop.get("state") != "routed"
            or hop.get("kind") == "route_repair"
            or hop.get("validation_error")
            or not isinstance(repository, str)
            or not repository.strip()
            or not isinstance(conversation_id, str)
            or not conversation_id.strip()
        ):
            return
        canonical = str(Path(repository).expanduser().resolve())
        task = asyncio.create_task(
            self._group_repository_project(canonical, conversation_id, browser_context)
        )
        self._repository_project_tasks.add(task)
        task.add_done_callback(self._repository_project_tasks.discard)
        return task

    def _rate_limit_scope_for_state(self, state: Mapping[str, Any]) -> str:
        role = str(state.get("active_role") or "").upper()
        record = (state.get("roles") or {}).get(role) if role else None
        endpoint_id = (
            str(record.get("browser_endpoint_id") or "").strip()
            if isinstance(record, Mapping)
            else ""
        ) or str(state.get("browser_endpoint_id") or "").strip()
        if endpoint_id:
            try:
                endpoint = self.store.endpoints.get(endpoint_id)
                return str(endpoint.get("rate_limit_scope") or "shared")
            except Exception:
                pass
        return "shared"

    def _rate_limit_gate_active(
        self,
        *,
        state: Mapping[str, Any] | None = None,
        scope: str | None = None,
    ) -> bool:
        selected_scope = scope or (
            self._rate_limit_scope_for_state(state) if state is not None else "shared"
        )
        cooldown = self._rate_limit_cooldowns.get(selected_scope)
        using_legacy = cooldown is None and not self._rate_limit_cooldowns
        if using_legacy:
            cooldown = self._rate_limit_cooldown
        if not isinstance(cooldown, Mapping) or cooldown.get("state") != "active":
            return False
        release_at = parse_time(cooldown.get("release_not_before"))
        if release_at is not None and datetime.now(timezone.utc) >= release_at:
            self._rate_limit_cooldowns.pop(selected_scope, None)
            if using_legacy or (
                isinstance(self._rate_limit_cooldown, Mapping)
                and self._rate_limit_cooldown.get("detected_at") == cooldown.get("detected_at")
                and self._rate_limit_cooldown.get("release_not_before") == cooldown.get("release_not_before")
            ):
                self._rate_limit_cooldown = None
            return False
        return True

    def _active_rate_limit_scopes(self) -> set[str]:
        if self._rate_limit_cooldowns:
            scopes = set(self._rate_limit_cooldowns)
        elif isinstance(self._rate_limit_cooldown, Mapping):
            scopes = {
                str(endpoint.get("rate_limit_scope") or "shared")
                for endpoint in self.store.endpoints.list()
            } or {"shared"}
        else:
            scopes = set()
        return {scope for scope in scopes if self._rate_limit_gate_active(scope=scope)}

    def _restore_rate_limit_cooldown(self) -> None:
        if self._rate_limit_cooldowns or self._rate_limit_cooldown is not None:
            return
        snapshot = self.runtime_db.get_snapshot("worker")
        payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
        scoped = payload.get("rate_limit_cooldowns") if isinstance(payload, Mapping) else None
        if isinstance(scoped, Mapping):
            for scope, cooldown in scoped.items():
                if isinstance(cooldown, Mapping) and cooldown.get("state") == "active":
                    self._rate_limit_cooldowns[str(scope)] = dict(cooldown)
        cooldown = (
            payload.get("rate_limit_cooldown")
            if isinstance(payload, Mapping)
            else None
        )
        if isinstance(cooldown, Mapping) and cooldown.get("state") == "active":
            restored = dict(cooldown)
            self._rate_limit_cooldown = restored
            if restored.get("scope"):
                self._rate_limit_cooldowns.setdefault(str(restored["scope"]), restored)
        self._active_rate_limit_scopes()

    async def _enter_rate_limit_cooldown(
        self,
        state: Mapping[str, Any],
        actions: CDPATabActions,
        error: BaseException | str,
    ) -> dict[str, Any]:
        del actions
        async with self._rate_limit_lock:
            now = datetime.now(timezone.utc)
            role = str(state.get("active_role") or "").upper()
            if not role and state.get("active_hop_id") is not None:
                role = str(_active_hop(state).get("target_role") or "").upper()
            scope = self._rate_limit_scope_for_state(state)
            role_record = (state.get("roles") or {}).get(role) if role else None
            endpoint_id = (
                str(role_record.get("browser_endpoint_id") or "").strip()
                if isinstance(role_record, Mapping)
                else ""
            ) or None
            endpoint_url = None
            if endpoint_id is not None:
                try:
                    endpoint_url = self.store.endpoints.get(endpoint_id).get("cdp_url")
                except Exception:
                    endpoint_url = None
            cooldown_seconds = float(self.rate_limit_cooldown_seconds)
            cooldown = {
                "state": "active",
                "scope": scope,
                "detected_at": now.isoformat(),
                "release_not_before": (
                    now + timedelta(seconds=cooldown_seconds)
                ).isoformat(),
                "reason": sanitize_text(error, max_chars=500),
                "profile": str(endpoint_url or self.config.cdp_url),
                "endpoint_id": endpoint_id,
                "detector_task_id": str(state.get("task_id") or "") or None,
                "detector_role": role or None,
            }
            self._rate_limit_cooldowns[scope] = cooldown
            self._rate_limit_cooldown = {
                key: cooldown[key]
                for key in (
                    "state", "detected_at", "release_not_before", "reason",
                    "profile", "detector_task_id", "detector_role",
                )
            }
            self._publish_heartbeat(force=True)
            return cooldown

    def _apply_rate_limit_to_state(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
    ) -> None:
        del hop
        state["active_action"] = "rate_limit_cooldown"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None

    async def _refresh_rate_limit_cooldown(
        self, browser_context: Any
    ) -> bool:
        del browser_context
        before = set(self._rate_limit_cooldowns) | (
            {"shared"} if self._rate_limit_cooldown is not None else set()
        )
        after = self._active_rate_limit_scopes()
        cleared = bool(before - after)
        if cleared:
            self._publish_heartbeat(force=True)
        return cleared

    async def _dismiss_known_rate_limit_on_existing(
        self, acquired: AcquiredRole
    ) -> None:
        visible = getattr(acquired.client, "known_rate_limit_visible", None)
        dismiss = getattr(acquired.client, "dismiss_known_rate_limit", None)
        if callable(visible) and await visible() and callable(dismiss):
            await dismiss(timeout_ms=15_000)

    @staticmethod
    def _should_observe_existing_response(exc: BaseException) -> bool:
        message = str(exc).strip().casefold()
        if isinstance(exc, UnsafePageStateError) and message == "page is already responding":
            return True
        if isinstance(exc, (TimeoutError, PlaywrightTimeoutError)):
            return "contenteditable" in message and "textbox" in message and "locator.wait_for" in message
        return False

    def _observe_existing_response(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
    ) -> None:
        """Adopt an already-running exact page response without sending again."""
        now = datetime.now(timezone.utc)
        hop["state"] = "waiting"
        hop["observed_existing_response"] = True
        hop.setdefault("timestamps", {})["waiting_at"] = now.isoformat()
        wait = hop.setdefault("wait", {})
        start_wait_budget(
            wait,
            timeout_seconds=self.config.response_timeout_seconds,
            now=now,
        )
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(str(hop.get("target_role") or state.get("active_role") or "PLAN"))
        state["active_action"] = "wait_response"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        state["pause_reason"] = None

    def _block(
        self,
        state: dict[str, Any],
        error: BaseException | str,
        *,
        code: str = "unexpected_error",
        retryable: bool = False,
    ) -> dict[str, Any]:
        message = sanitize_text(error, max_chars=2000)
        if str(code) == "role_offline" and not is_independent_task(state):
            unchanged_wait = (
                state.get("status") == "WAITING"
                and state.get("waiting_code") == "role_reconnect"
                and state.get("waiting_reason") == message
            )
            state["status"] = "WAITING"
            state["kanban_column"] = "WAITING"
            state["waiting_code"] = "role_reconnect"
            state["waiting_reason"] = message
            state["active_action"] = "waiting_role_reconnect"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            waiting = state.setdefault("waiting", {})
            waiting["reason"] = "role_reconnect"
            waiting["since"] = waiting.get("since") or utc_now()
            role = str(state.get("active_role") or "").upper()
            record = (state.get("roles") or {}).get(role)
            if isinstance(record, dict):
                record["online"] = False
                record["last_error"] = message
            if not unchanged_wait:
                state.setdefault("errors", []).append({"at": utc_now(), "error": message})
            return state
        unchanged_active_block = (
            state.get("status") == "BLOCKED"
            and state.get("block_code") == str(code)
            and bool(state.get("block_retryable")) == bool(retryable)
            and state.get("block_reason") == message
        )
        state["status"] = "BLOCKED"
        state["kanban_column"] = "BLOCKED"
        state["block_code"] = str(code)
        state["block_retryable"] = bool(retryable)
        state["block_reason"] = message
        state["active_action"] = "blocked"
        if str(code) == "role_offline":
            role = str(state.get("active_role") or "").upper()
            record = state.get("roles", {}).get(role)
            if isinstance(record, dict):
                record["online"] = False
                record["last_error"] = message
        if not unchanged_active_block:
            state.setdefault("errors", []).append(
                {"at": utc_now(), "error": message}
            )
            try:
                hop = _active_hop(state)
                hop.setdefault("errors", []).append(message)
            except RuntimeError:
                pass
        return state

    def _wait_queue_error(
        self,
        state: dict[str, Any],
        error: BaseException | str,
        *,
        code: str,
    ) -> bool:
        message = sanitize_text(error, max_chars=2000)
        unchanged = (
            state.get("status") == "WAITING"
            and state.get("waiting_code") == code
            and state.get("waiting_reason") == message
        )
        state["status"] = "WAITING"
        state["kanban_column"] = "WAITING"
        state["waiting_code"] = code
        state["waiting_reason"] = message
        state["active_action"] = "waiting_team_recovery"
        waiting = state.setdefault("waiting", {})
        waiting["reason"] = "team_busy"
        waiting["since"] = waiting.get("since") or utc_now()
        if not unchanged:
            state.setdefault("errors", []).append(
                {"at": utc_now(), "error": message}
            )
        return not unchanged

    def _start_wait_budget_from_sent(self, hop: dict[str, Any]) -> None:
        sent_at = parse_time((hop.get("timestamps") or {}).get("sent_at"))
        if sent_at is None:
            raise RuntimeError("sent hop is missing its accepted send timestamp")
        wait = hop["wait"]
        sent_key = sent_at.isoformat()
        # Arm the response-recovery clock exactly once for each accepted Send.
        # Legacy waits without the marker keep a valid post-Send timer, but any
        # pre-Send timestamp is stale and must be reset to the accepted Send.
        armed_for = wait.get("response_recovery_armed_for_sent_at")
        progress_at = parse_time(wait.get("controller_progress_at"))
        stale_pre_send_clock = progress_at is None or progress_at < sent_at
        if armed_for != sent_key and stale_pre_send_clock:
            for key in (
                "controller_progress_at",
                "controller_state",
                "controller_state_since",
                "timeout_refreshed",
                "timeout_status_checked",
                "timeout_status",
                "refresh_ready_at",
                "controller_last_refresh_at",
            ):
                wait.pop(key, None)
            wait["controller_progress_at"] = sent_key
        if armed_for != sent_key:
            wait["response_recovery_armed_for_sent_at"] = sent_key
        start_wait_budget(
            wait,
            timeout_seconds=self.config.response_timeout_seconds,
            now=sent_at,
        )

    def _attachment_files_for_generation(
        self,
        state: dict[str, Any],
        logical_role: str,
    ) -> tuple[str, ...] | None:
        attachments = state.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return ()
        role_record = state["roles"][logical_role]
        generation = int(role_record.get("conversation_generation") or 0)
        if role_record.get("attachments_uploaded_generation") == generation:
            return ()

        paths = tuple(str(item["path"]) for item in attachments)
        for item in attachments:
            path = Path(str(item["path"]))
            name = str(item.get("name") or path.name or "attachment")
            digest_prefix = str(item.get("sha256") or "")[:12]
            if not path.exists():
                self._block(
                    state,
                    f"Attachment {name} is missing (expected {digest_prefix})",
                    code="attachment_missing",
                    retryable=False,
                )
                return None
            if not path.is_file():
                self._block(
                    state,
                    f"Attachment {name} is not a regular file (expected {digest_prefix})",
                    code="attachment_validation_failed",
                    retryable=False,
                )
                return None
        try:
            current = tuple(
                identity.to_dict() for identity in collect_file_identities(paths)
            )
        except Exception as exc:
            self._block(
                state,
                f"Attachment validation failed for {', '.join(str(item.get('name') or 'attachment') for item in attachments)} ({type(exc).__name__})",
                code="attachment_validation_failed",
                retryable=False,
            )
            return None
        expected = tuple(dict(item) for item in attachments)
        if current != expected:
            changed = next(
                (
                    item
                    for item, actual in zip(attachments, current)
                    if dict(item) != actual
                ),
                attachments[0],
            )
            self._block(
                state,
                f"Attachment identity changed for {changed.get('name') or 'attachment'} (expected {str(changed.get('sha256') or '')[:12]})",
                code="attachment_identity_changed",
                retryable=False,
            )
            return None
        return paths

    def _record_acquired(
        self,
        state: dict[str, Any],
        logical_role: str,
        acquired: AcquiredRole,
    ) -> None:
        record = state["roles"][logical_role]
        saved_page_url = str(acquired.url)
        if state.get("conversation_mode") != "temporary":
            existing = None if acquired.new_chat else _official_saved_url(record.get("page_url"))
            if existing is not None:
                saved_page_url = (
                    _preferred_saved_url(acquired.url, record.get("page_url"), conversation_id=existing[0])
                    or str(record["page_url"])
                )
            else:
                saved_page_url = _preferred_saved_url(acquired.url) or saved_page_url
        changed = (
            acquired.created
            or acquired.new_chat
            or record.get("status") != "active"
            or str(record.get("page_id") or "") != str(acquired.page_id)
            or str(record.get("page_url") or "") != saved_page_url
            or record.get("online") is not True
            or record.get("last_error") is not None
            or bool(record.get("reset_requested"))
            or not record.get("last_activity_at")
        )
        if acquired.new_chat:
            record["conversation_generation"] = int(record.get("conversation_generation") or 0) + 1
            record.pop("branch_conversation_id", None)
        if getattr(acquired, "conversation_id", None):
            record["branch_conversation_id"] = acquired.conversation_id
        generation = int(record.get("conversation_generation") or 0)
        if record.get("reset_requested"):
            record["reset_applied_generation"] = generation
            record["reset_requested"] = False
        record.update(
            {
                "status": "active",
                "page_id": acquired.page_id,
                "page_url": saved_page_url,
                "online": True,
                "last_error": None,
            }
        )
        if logical_role == INDEPENDENT_ROLE and is_independent_task(state):
            independent = state.get("independent")
            if isinstance(independent, dict):
                independent["idle_tab_closed_at"] = None
        if changed:
            record["last_activity_at"] = utc_now()
            state["last_role_activity_at"] = record["last_activity_at"]

    def _active_recovery_context(
        self, state: Mapping[str, Any], role: str
    ) -> tuple[Mapping[str, Any], str | None, str | None, bool]:
        logical_role = str(role or "").upper()
        if str(state.get("active_role") or "").upper() != logical_role:
            raise RoleOwnershipError("only the active role may recover automatically")
        hop = _active_hop(state)
        hop_state = str(hop.get("state") or "")
        if (
            str(hop.get("target_role") or "").upper() != logical_role
            or hop_state not in {"pre_send", "sending", "sent", "waiting"}
        ):
            raise RoleOwnershipError("active hop is not at a recoverable role boundary")
        role_record = state["roles"][logical_role]
        candidate_urls = (
            str(hop.get("conversation_url") or "").strip(),
            str(role_record.get("page_url") or "").strip(),
        )
        expected_conversation = next(
            (
                identity
                for value in candidate_urls
                if (identity := _recoverable_conversation_identity(value)) is not None
            ),
            None,
        )
        receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
        binding = receipt.get("binding") if isinstance(receipt, Mapping) else {}
        expected_page_id = str(
            binding.get("page_id") if isinstance(binding, Mapping) else ""
        ).strip() or None
        require_clean_ready = hop_state not in {"sent", "waiting"}
        return hop, expected_conversation, expected_page_id, require_clean_ready

    async def _recover_active_role(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
        *,
        foreground: bool = False,
    ) -> AcquiredRole:
        _, expected_conversation, expected_page_id, require_clean_ready = (
            self._active_recovery_context(state, role)
        )
        if expected_conversation is None:
            raise RoleOwnershipError(
                "active role has no exact saved ChatGPT conversation URL",
                code="role_offline",
            )
        acquired = await actions.reopen(
            state,
            role,
            require_clean_ready=require_clean_ready and foreground,
            foreground=foreground,
        )
        if _recoverable_conversation_identity(acquired.url) != expected_conversation:
            raise RoleOwnershipError(
                "automatic role recovery reopened a different conversation"
            )
        if expected_page_id is not None and str(acquired.page_id) != expected_page_id:
            raise RoleOwnershipError(
                "automatic role recovery did not restore the recorded page identity"
            )
        if not foreground:
            await actions.wake(acquired)
            if require_clean_ready:
                await acquired.client.wait_until_clean_ready(
                    timeout_ms=round(self.config.workspace_timeout_seconds * 1000)
                )
        self._record_acquired(state, role, acquired)
        return acquired

    @staticmethod
    async def _temporary_document_token(client: Any, *, create: bool = False) -> str | None:
        page = getattr(client, "page", None)
        if page is None or not callable(getattr(page, "evaluate", None)):
            return None
        value = await page.evaluate(
            "create => { if (create && !window.__cdpaTemporaryDocument) "
            "window.__cdpaTemporaryDocument = crypto.randomUUID(); "
            "return window.__cdpaTemporaryDocument || null; }", create,
        )
        return str(value) if value else None

    async def _owned_or_block(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
        *,
        admitted: list[bool] | None = None,
    ) -> AcquiredRole | None:
        try:
            hop, expected_conversation, expected_page_id, _ = (
                self._active_recovery_context(state, role)
            )
            ownership_retry_at = parse_time((hop.get("wait") or {}).get("ownership_retry_at"))
            if ownership_retry_at is not None and datetime.now(timezone.utc) < ownership_retry_at:
                state["status"] = "RUNNING"
                state["active_action"] = "wait_role_recovery"
                return None
            if ownership_retry_at is not None:
                hop.setdefault("wait", {}).pop("ownership_retry_at", None)
            if isinstance(hop.get("receipt"), Mapping):
                try:
                    self._reconcile_hop_conversation_identity(state, hop)
                except RuntimeError as exc:
                    self._block(
                        state,
                        exc,
                        code="accepted_receipt_provenance_divergence",
                        retryable=False,
                    )
                    return None
            acquired = await actions.locate_owned(state, role)
            temporary = state.get("conversation_mode") == "temporary"
            receipt_payload = hop.get("receipt")
            accepted_identity_pending = (
                not temporary
                and isinstance(receipt_payload, Mapping)
                and not receipt_payload.get("conversation_id")
                and (
                    _provisional_conversation_url(hop.get("conversation_url"))
                    or (acquired is not None and _official_saved_url(acquired.url) is not None)
                )
            )
            if accepted_identity_pending:
                receipt = SendReceipt.from_dict(receipt_payload)
                if acquired is None or str(acquired.page_id) != str(receipt.binding.page_id):
                    self._block(
                        state,
                        "Accepted Send lost the exact owned page while canonical conversation identity was unresolved.",
                        code="accepted_conversation_identity_unresolved",
                        retryable=False,
                    )
                    return None
                ownership = await acquired.client.assert_ownership()
                if str(ownership.page_id) != str(receipt.binding.page_id):
                    self._block(
                        state,
                        "Accepted Send is no longer on the owned runtime page.",
                        code="accepted_conversation_identity_unresolved",
                        retryable=False,
                    )
                    return None
                try:
                    await self._discover_accepted_conversation_identity(
                        state, hop, actions, receipt
                    )
                except _AcceptedConversationIdentityConflict as exc:
                    wait = hop.setdefault("wait", {})
                    wait["accepted_identity_finished_at"] = datetime.now(timezone.utc).isoformat()
                    wait["accepted_identity_outcome"] = "blocked_unresolved"
                    self._block(
                        state,
                        exc,
                        code="accepted_conversation_identity_unresolved",
                        retryable=False,
                    )
                    return None
                except GraphIdentityError:
                    wait = hop.setdefault("wait", {})
                    now = datetime.now(timezone.utc)
                    started_at = parse_time(wait.get("accepted_identity_started_at"))
                    if started_at is None:
                        started_at = now
                        wait["accepted_identity_started_at"] = now.isoformat()
                        wait["accepted_identity_deadline_at"] = (
                            now + timedelta(seconds=10)
                        ).isoformat()
                        wait["accepted_identity_page_id"] = str(receipt.binding.page_id)
                    settle_deadline = parse_time(wait.get("accepted_identity_deadline_at"))
                    if settle_deadline is not None and now < settle_deadline:
                        state["status"] = "RUNNING"
                        state["active_action"] = "wait_accepted_conversation_identity"
                        return None
                    baseline = receipt.baseline
                    messages = tuple(getattr(ownership, "messages", ()) or ())
                    activity = (
                        bool(getattr(ownership, "stop_visible", False))
                        or bool(getattr(ownership, "response_projection_active", False))
                        or bool(getattr(ownership, "response_activity_text", ""))
                        or bool(getattr(ownership, "response_activity_turn_id", ""))
                        or receipt_user_message_seen(messages, receipt)
                        or any(
                            message.role == "assistant"
                            and message.message_id
                            and message.message_id not in baseline.message_ids
                            for message in messages
                        )
                    )
                    wait["accepted_identity_finished_at"] = now.isoformat()
                    if activity:
                        # An accepted Send may complete while its URL remains provisional.
                        # Let the ordinary response controller decide terminal stability.
                        wait["accepted_identity_outcome"] = "observe_response_without_url"
                        expected_conversation = None
                    else:
                        wait["accepted_identity_outcome"] = "paused_unresolved"
                        state["status"] = "PAUSED"
                        state["kanban_column"] = "PAUSED"
                        state["active_action"] = "paused"
                        state["pause_reason"] = (
                            "Accepted Send has no canonical conversation URL or "
                            "current response activity; inspect the tab and Resume."
                        )
                        return None
                else:
                    wait = hop.setdefault("wait", {})
                    if wait.get("accepted_identity_started_at") is not None:
                        wait["accepted_identity_finished_at"] = datetime.now(timezone.utc).isoformat()
                        wait["accepted_identity_outcome"] = "canonicalized"
                    expected_conversation = _recoverable_conversation_identity(
                        hop.get("conversation_url")
                    )
            if temporary:
                token = state["roles"][role].get("temporary_document_id")
                lost_document = bool(
                    acquired is not None and token
                    and await self._temporary_document_token(acquired.client) != token
                )
                if acquired is None or lost_document:
                    endpoint = self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state)
                    if self._prepare_cross_endpoint_recovery(
                        state, endpoint, reason="Temporary generation lost by tab loss or document reload",
                        force_new_generation=True,
                    ):
                        return None
                    # A truly pristine request can reacquire a surface without being replayed.
                    ledger = RequestLedger(str(hop["ledger_path"]))
                    durable = ledger.peek(str(hop["request_id"]))
                    if self._is_pristine_preboundary_sending_record(state, hop, durable):
                        await self._acquire_endpoint_admission(state, role)
                        if admitted is not None:
                            admitted.append(True)
                        acquired = await actions.fresh_chat(
                            state,
                            role,
                            url="https://chatgpt.com/?temporary-chat=true",
                            clean_for_send=True,
                        )
                        state["roles"][role]["temporary_document_id"] = await self._temporary_document_token(acquired.client, create=True)
                        self._record_acquired(state, role, acquired)
                        return acquired
                # Temporary URLs are generation evidence, never saved-conversation reopen targets.
                expected_conversation = None
            if acquired is not None:
                await self._dismiss_known_rate_limit_on_existing(acquired)
            live_conversation = (
                _recoverable_conversation_identity(acquired.url)
                if acquired is not None
                else None
            )
            if not temporary and acquired is not None and live_conversation is not None:
                if expected_conversation is None:
                    expected_conversation = live_conversation
                if live_conversation == expected_conversation:
                    hop["conversation_url"] = (
                        _preferred_saved_url(
                            acquired.url, hop.get("conversation_url"),
                            state["roles"][role].get("page_url"),
                        )
                        or str(acquired.url)
                    )
            needs_reopen = (
                acquired is None
                or (expected_conversation is not None and live_conversation != expected_conversation)
                or (expected_page_id is not None and str(acquired.page_id) != expected_page_id)
            )
            if needs_reopen:
                role_record = state["roles"][role]
                fresh_policy = state.get("context_policy") == "fresh" or (
                    state.get("bootstrap") is None and state.get("context_policy") != "bootstrap"
                ) or hop.get("kind") == "execution_recovery"
                if acquired is None and fresh_policy and hop.get("state") == "sending" and hop.get("receipt") is None:
                    durable = RequestLedger(str(hop["ledger_path"])).peek(str(hop["request_id"]))
                    pristine = self._is_pristine_preboundary_sending_record(state, hop, durable)
                    owns_attachments = role_record.get("attachments_uploaded_generation") is not None and (
                        role_record.get("attachments_uploaded_generation") == role_record.get("conversation_generation")
                    )
                    if pristine and not owns_attachments and hop.get("message_identity") is None:
                        await self._acquire_endpoint_admission(state, role)
                        if admitted is not None:
                            admitted.append(True)
                        acquired = await actions.fresh_chat(
                            state,
                            role,
                            url="https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com/",
                            clean_for_send=True,
                        )
                        self._record_acquired(state, role, acquired)
                        hop["conversation_url"] = acquired.url
                        hop["conversation_generation"] = role_record.get("conversation_generation", 0)
                        hop["generation_started_here"] = True
                        if temporary:
                            role_record["temporary_document_id"] = await self._temporary_document_token(acquired.client, create=True)
                        return acquired
                if expected_conversation is None:
                    self._wait_queue_error(
                        state,
                        "Owned role tab is temporarily unavailable; waiting to reconnect on the same team endpoint.",
                        code="role_reconnect",
                    )
                    return None
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return None
                await self._acquire_endpoint_admission(state, role)
                if admitted is not None:
                    admitted.append(True)
                acquired = await self._recover_active_role(state, role, actions)
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            state["active_action"] = "rate_limit_cooldown"
            return None
        except Exception as exc:
            if is_cdp_disconnect(exc):
                raise
            if is_transient_page_lifecycle_error(exc):
                wait = hop.setdefault("wait", {})
                wait["ownership_retry_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=5)
                ).isoformat()
                wait["ownership_retry_error"] = sanitize_exception(exc)
                state["status"] = "RUNNING"
                state["active_action"] = "wait_role_recovery"
                return None
            code = _role_ownership_block_code(exc) or "role_ownership_ambiguous"
            self._block(state, exc, code=code, retryable=False)
            return None
        if str(hop.get("state") or "") in IN_FLIGHT:
            receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
            binding = receipt.get("binding") if isinstance(receipt, Mapping) else {}
            sent_page_id = (
                str(binding.get("page_id") or "")
                if isinstance(binding, Mapping)
                else ""
            )
            if sent_page_id and sent_page_id != acquired.page_id:
                self._block(
                    state,
                    "in-flight receipt belongs to another runtime page; refusing transfer",
                    code="inflight_page_identity_changed",
                    retryable=False,
                )
                return None
        self._record_acquired(state, role, acquired)
        return acquired

    def _cleanup_control(
        self,
        state: Mapping[str, Any],
        cleanup: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        control_id = cleanup.get("control_id")
        if control_id is None:
            return None
        return next(
            (
                item
                for item in state.get("controls") or []
                if isinstance(item, dict) and item.get("control_id") == control_id
            ),
            None,
        )

    def _start_cleanup(
        self,
        state: dict[str, Any],
        *,
        control: dict[str, Any] | None,
        target_tabs: int,
        manifest_path: Path,
    ) -> bool:
        candidate = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        cleanup = candidate.setdefault("cleanup", {})
        if cleanup.get("state") == "CLEARING":
            return True
        status_before = str(candidate.get("status") or "").upper()
        now = utc_now()
        active_role = str(candidate.get("active_role") or "").upper() or None
        active_hop_id = candidate.get("active_hop_id")
        control_id = control.get("control_id") if control is not None else None
        working_control = next(
            (
                item
                for item in candidate.get("controls") or []
                if isinstance(item, dict) and item.get("control_id") == control_id
            ),
            None,
        )
        if control_id is not None and working_control is None:
            raise RuntimeError("Clear Team control is no longer present")
        cleanup.update(
            {
                "state": "CLEARING",
                "phase": "stop_pending",
                "clear_requested_at": cleanup.get("clear_requested_at") or now,
                "cleared_at": None,
                "verified_empty_at": None,
                "status_before": status_before,
                "terminal_state_before": candidate.get("terminal_state"),
                "active_role": active_role,
                "active_hop_id": active_hop_id,
                "control_id": control_id,
                "target_tabs": max(0, int(target_tabs)),
                "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                "retry_count": 0,
                "last_error": None,
                "last_error_at": None,
                "failure_fingerprint": None,
                "same_failure_count": 0,
                "next_retry_at": None,
                "retry_suppressed": False,
            }
        )
        if working_control is not None:
            working_control["status"] = "cleanup_pending"
            working_control["result"] = {
                "phase": "stop_pending",
                "status_before": status_before,
            }
            working_control["applied_at"] = now
        if status_before not in TERMINAL:
            if active_hop_id is not None:
                try:
                    hop = _active_hop(candidate)
                except RuntimeError:
                    hop = None
                if hop is not None and str(hop.get("state") or "") not in {
                    "routed",
                    "abandoned",
                }:
                    hop["state"] = "abandoned"
                    hop["abandon_reason"] = "team cleanup started"
                    hop.setdefault("timestamps", {})["abandoned_at"] = now
            candidate["status"] = "STOPPED"
            candidate["terminal_state"] = "STOPPED"
            candidate["kanban_column"] = "DONE_STOPPED"
            candidate["stopped_at"] = candidate.get("stopped_at") or now
            candidate["stop_reason"] = "team cleared"
            candidate["last_role_activity_at"] = now
            candidate["active_role"] = None
            candidate["active_hop_id"] = None
        candidate["active_action"] = "cleanup_stop_pending"
        candidate["pause_reason"] = None
        candidate["block_code"] = None
        candidate["block_retryable"] = False
        candidate["block_reason"] = None
        saved, started = self.store.begin_team_cleanup(
            manifest_path,
            candidate,
            expected_state=state,
            control_id=control_id,
        )
        state.clear()
        state.update(saved)
        return started

    @staticmethod
    def _reset_cleanup_retry_gate(cleanup: dict[str, Any]) -> None:
        cleanup["failure_fingerprint"] = None
        cleanup["same_failure_count"] = 0
        cleanup["next_retry_at"] = None
        cleanup["retry_suppressed"] = False

    @staticmethod
    def _cleanup_retry_due(cleanup: Mapping[str, Any]) -> bool:
        if bool(cleanup.get("retry_suppressed")):
            return False
        retry_at = str(cleanup.get("next_retry_at") or "").strip()
        if not retry_at:
            return True
        try:
            due = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        now = datetime.fromisoformat(utc_now().replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= due

    def _record_cleanup_failure(
        self,
        state: dict[str, Any],
        error: BaseException,
    ) -> None:
        cleanup = state.setdefault("cleanup", {})
        if cleanup.get("phase") == "closing":
            cleanup["phase"] = "close_pending"
        now = utc_now()
        detail = sanitize_exception(error)
        fingerprint = hashlib.sha256(
            f"{cleanup.get('phase') or ''}\n{type(error).__name__}\n{detail}".encode("utf-8")
        ).hexdigest()
        previous = str(cleanup.get("failure_fingerprint") or "")
        same_failure_count = (
            int(cleanup.get("same_failure_count") or 0) + 1
            if previous == fingerprint
            else 1
        )
        cleanup["last_error"] = detail
        cleanup["last_error_at"] = now
        cleanup["retry_count"] = int(cleanup.get("retry_count") or 0) + 1
        cleanup["failure_fingerprint"] = fingerprint
        cleanup["same_failure_count"] = same_failure_count
        if same_failure_count >= CLEANUP_IDENTICAL_FAILURE_LIMIT:
            cleanup["next_retry_at"] = None
            cleanup["retry_suppressed"] = True
        else:
            delay_seconds = _CLEANUP_RETRY_DELAYS_SECONDS[
                min(same_failure_count - 1, len(_CLEANUP_RETRY_DELAYS_SECONDS) - 1)
            ]
            parsed_now = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if parsed_now.tzinfo is None:
                parsed_now = parsed_now.replace(tzinfo=timezone.utc)
            cleanup["next_retry_at"] = (
                parsed_now + timedelta(seconds=delay_seconds)
            ).isoformat()
            cleanup["retry_suppressed"] = False
        state["active_action"] = "cleanup_recoverable"
        control = self._cleanup_control(state, cleanup)
        if control is not None:
            control["status"] = "cleanup_pending"
            control["result"] = {
                "phase": cleanup.get("phase"),
                "retry_count": cleanup["retry_count"],
                "error": detail,
            }
            control["applied_at"] = now

    def _finish_cleanup(self, state: dict[str, Any]) -> dict[str, Any]:
        cleanup = state.setdefault("cleanup", {})
        now = utc_now()
        self._reset_cleanup_retry_gate(cleanup)
        cleanup.update(
            {
                "state": "CLEARED",
                "phase": "cleared",
                "cleared_at": now,
                "verified_empty_at": now,
                "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                "last_error": None,
                "last_error_at": None,
            }
        )
        state["active_action"] = "cleared"
        state["pause_reason"] = None
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        for record in state["roles"].values():
            record["online"] = False
            record["status"] = "cleared"
        control = self._cleanup_control(state, cleanup)
        result = {
            "closed_tabs": int(cleanup.get("closed_tabs") or 0),
            "status_before": cleanup.get("status_before"),
            "retry_count": int(cleanup.get("retry_count") or 0),
        }
        if control is not None:
            control["status"] = "applied"
            control["result"] = result
            control["applied_at"] = now
        return result

    def _control_actions(self, state: Mapping[str, Any], role: str, actions: CDPATabActions) -> CDPATabActions:
        if self._endpoint_pool is None:
            return actions
        endpoint_id = self._execution_endpoint_id(state, role)
        if not endpoint_id:
            return actions
        context = self._endpoint_pool.contexts.get(endpoint_id)
        if context is None:
            raise RoleOwnershipError(f"selected role browser {endpoint_id} is unavailable", code="role_offline")
        return actions if context is actions.browser_context else CDPATabActions(context, self.config)

    async def _admission_preflight(
        self, state: Mapping[str, Any], actions: CDPATabActions
    ) -> list[Any]:
        """Normal Send/Resume admission is exact-role scoped, never browser-global."""
        role = str(state.get("active_role") or "").upper()
        if not role:
            return []
        preflight = getattr(actions, "preflight_role", None)
        if not callable(preflight):
            return []
        return await preflight(state, role)

    async def _cleanup_preflight(self, state: Mapping[str, Any], actions: CDPATabActions) -> list[Any]:
        if self._endpoint_pool is None:
            return await actions.preflight_team(state)
        contexts = self._endpoint_pool.contexts
        for role, record in state.get("roles", {}).items():
            if record.get("page_id"):
                endpoint_id = self._execution_endpoint_id(state, role)
                if endpoint_id and endpoint_id not in contexts:
                    raise RoleOwnershipError(f"cleanup cannot verify unavailable browser {endpoint_id}", code="role_offline")
        pages: list[Any] = []
        for context in contexts.values():
            pages.extend(await CDPATabActions(context, self.config).preflight_team(state))
        return pages

    async def _continue_cleanup(
        self,
        state: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        *,
        preflighted_pages: list[Any] | None = None,
        force_retry: bool = False,
    ) -> bool:
        def persist(
            mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        ) -> dict[str, Any]:
            saved = self.store.update(manifest_path, mutator)
            state.clear()
            state.update(saved)
            return state

        cleanup = state.setdefault("cleanup", {})
        if cleanup.get("state") == "CLEARED":
            return True
        if cleanup.get("state") != "CLEARING":
            raise RuntimeError("cleanup continuation requires CLEARING state")
        if not force_retry and not self._cleanup_retry_due(cleanup):
            return False
        if str(state.get("status") or "").upper() not in TERMINAL:
            def normalize_stopped(current: dict[str, Any]) -> dict[str, Any]:
                current_cleanup = current.setdefault("cleanup", {})
                status = str(current.get("status") or "").upper()
                if status in TERMINAL:
                    return current
                now = utc_now()
                active_role = str(current.get("active_role") or "").upper() or None
                active_hop_id = current.get("active_hop_id")
                current_cleanup["status_before"] = (
                    current_cleanup.get("status_before") or status
                )
                current_cleanup["active_role"] = (
                    current_cleanup.get("active_role") or active_role
                )
                current_cleanup["active_hop_id"] = (
                    current_cleanup.get("active_hop_id") or active_hop_id
                )
                if active_hop_id is not None:
                    try:
                        hop = _active_hop(current)
                    except RuntimeError:
                        hop = None
                    if hop is not None and str(hop.get("state") or "") not in {
                        "routed",
                        "abandoned",
                    }:
                        hop["state"] = "abandoned"
                        hop["abandon_reason"] = "team cleanup resumed"
                        hop.setdefault("timestamps", {})["abandoned_at"] = now
                current["status"] = "STOPPED"
                current["terminal_state"] = "STOPPED"
                current["kanban_column"] = "DONE_STOPPED"
                current["stopped_at"] = current.get("stopped_at") or now
                current["stop_reason"] = current.get("stop_reason") or "team cleared"
                current["last_role_activity_at"] = now
                current["active_role"] = None
                current["active_hop_id"] = None
                return current

            persist(normalize_stopped)
        try:
            phase = str(state.get("cleanup", {}).get("phase") or "stop_pending")
            if phase == "stop_pending":
                active_role = str(
                    state.get("cleanup", {}).get("active_role") or ""
                ).upper()
                stopped = False
                if active_role and active_role in state.get("roles", {}):
                    stop_actions = self._control_actions(state, active_role, actions)
                    acquired = await stop_actions.locate_owned(state, active_role)
                    if acquired is not None:
                        stopped = await stop_actions.stop_if_active(acquired)

                def persist_stop(current: dict[str, Any]) -> dict[str, Any]:
                    current_cleanup = current.setdefault("cleanup", {})
                    current_cleanup["stopped_response"] = bool(stopped)
                    current_cleanup["phase"] = "close_pending"
                    current_cleanup["last_error"] = None
                    current_cleanup["last_error_at"] = None
                    self._reset_cleanup_retry_gate(current_cleanup)
                    current["active_action"] = "cleanup_close_pending"
                    return current

                persist(persist_stop)
                phase = "close_pending"
            if phase in {"close_pending", "closing"}:
                selected = (
                    list(preflighted_pages)
                    if preflighted_pages is not None and self._endpoint_pool is None
                    else await self._cleanup_preflight(state, actions)
                )

                def persist_closing(current: dict[str, Any]) -> dict[str, Any]:
                    current_cleanup = current.setdefault("cleanup", {})
                    current_cleanup["target_tabs"] = max(
                        int(current_cleanup.get("target_tabs") or 0),
                        int(current_cleanup.get("closed_tabs") or 0) + len(selected),
                    )
                    current_cleanup["phase"] = "closing"
                    current["active_action"] = "cleanup_closing"
                    return current

                persist(persist_closing)
                try:
                    closed = await actions.close_team(
                        state,
                        preflighted_pages=selected,
                    )
                except TeamCloseError as exc:
                    def persist_partial_failure(
                        current: dict[str, Any],
                    ) -> dict[str, Any]:
                        current_cleanup = current.setdefault("cleanup", {})
                        current_cleanup["closed_tabs"] = int(
                            current_cleanup.get("closed_tabs") or 0
                        ) + int(exc.closed_tabs)
                        if exc.closed_tabs > 0:
                            current_cleanup["last_error"] = None
                            current_cleanup["last_error_at"] = None
                            self._reset_cleanup_retry_gate(current_cleanup)
                        self._record_cleanup_failure(current, exc)
                        return current

                    persist(persist_partial_failure)
                    return False

                def persist_closed(current: dict[str, Any]) -> dict[str, Any]:
                    current_cleanup = current.setdefault("cleanup", {})
                    current_cleanup["closed_tabs"] = int(
                        current_cleanup.get("closed_tabs") or 0
                    ) + int(closed)
                    current_cleanup["phase"] = "verify_pending"
                    if closed > 0:
                        current_cleanup["last_error"] = None
                        current_cleanup["last_error_at"] = None
                        self._reset_cleanup_retry_gate(current_cleanup)
                    current["active_action"] = "cleanup_verify_pending"
                    return current

                persist(persist_closed)
                phase = "verify_pending"
            if phase == "verify_pending":
                remaining = await self._cleanup_preflight(state, actions)
                if remaining:
                    error = RoleOwnershipError(
                        f"post-close verification found {len(remaining)} assigned team tab(s) still open"
                    )

                    def persist_verification_failure(
                        current: dict[str, Any],
                    ) -> dict[str, Any]:
                        current.setdefault("cleanup", {})["phase"] = "close_pending"
                        self._record_cleanup_failure(current, error)
                        return current

                    persist(persist_verification_failure)
                    return False

                def persist_finished(current: dict[str, Any]) -> dict[str, Any]:
                    self._finish_cleanup(current)
                    return current

                persist(persist_finished)
                return True
            if phase == "cleared":
                def persist_finished(current: dict[str, Any]) -> dict[str, Any]:
                    self._finish_cleanup(current)
                    return current

                persist(persist_finished)
                return True
            raise RuntimeError(f"unknown cleanup phase {phase!r}")
        except Exception as exc:
            def persist_failure(current: dict[str, Any]) -> dict[str, Any]:
                self._record_cleanup_failure(current, exc)
                return current

            persist(persist_failure)
            return False

    @staticmethod
    def _merge_control_delta(
        current: Any,
        before: Any,
        after: Any,
        *,
        path: str,
    ) -> Any:
        if before == after or current == after:
            return current
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            if not isinstance(current, dict):
                raise ValueError(f"task changed at {path}; reload before applying control")
            for key in before.keys() | after.keys():
                child_path = f"{path}.{key}" if path else str(key)
                if key not in before:
                    if key not in current:
                        current[key] = json.loads(
                            json.dumps(after[key], ensure_ascii=False, default=str)
                        )
                    elif current[key] != after[key]:
                        raise ValueError(
                            f"task changed at {child_path}; reload before applying control"
                        )
                elif key not in after:
                    if key not in current:
                        continue
                    if current[key] != before[key]:
                        raise ValueError(
                            f"task changed at {child_path}; reload before applying control"
                        )
                    current.pop(key)
                else:
                    current[key] = CDPAWorker._merge_control_delta(
                        current.get(key),
                        before[key],
                        after[key],
                        path=child_path,
                    )
            return current
        if isinstance(before, list) and isinstance(after, list):
            if not isinstance(current, list):
                raise ValueError(f"task changed at {path}; reload before applying control")
            if len(before) == len(after) == len(current):
                for index, (before_item, after_item) in enumerate(zip(before, after)):
                    current[index] = CDPAWorker._merge_control_delta(
                        current[index],
                        before_item,
                        after_item,
                        path=f"{path}[{index}]",
                    )
                return current
            if current != before:
                raise ValueError(f"task changed at {path}; reload before applying control")
            return json.loads(json.dumps(after, ensure_ascii=False, default=str))
        if current == after:
            return current
        if current != before:
            raise ValueError(f"task changed at {path}; reload before applying control")
        return json.loads(json.dumps(after, ensure_ascii=False, default=str))

    def _record_controller_live_transition(
        self,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        def hop_for(value: Mapping[str, Any]) -> Mapping[str, Any]:
            active = value.get("active_hop_id")
            return next(
                (
                    item
                    for item in value.get("hops") or ()
                    if isinstance(item, Mapping) and item.get("hop_id") == active
                ),
                {},
            )

        before_hop = hop_for(before)
        after_hop = hop_for(after)
        before_values = {
            "status": before.get("status"),
            "active_role": before.get("active_role"),
            "active_action": before.get("active_action"),
            "block_code": before.get("block_code"),
            "waiting_reason": before.get("waiting_reason"),
            "hop_id": before.get("active_hop_id"),
            "hop_state": before_hop.get("state"),
        }
        values = {
            "status": after.get("status"),
            "active_role": after.get("active_role"),
            "active_action": after.get("active_action"),
            "block_code": after.get("block_code"),
            "waiting_reason": after.get("waiting_reason"),
            "hop_id": after.get("active_hop_id"),
            "hop_state": after_hop.get("state"),
        }
        changes = {
            field: {"from": before_values.get(field), "to": value}
            for field, value in values.items()
            if before_values.get(field) != value
        }
        if not changes:
            return
        role = str(after.get("active_role") or before.get("active_role") or "").upper()
        role_record = (after.get("roles") or {}).get(role)
        if not isinstance(role_record, Mapping):
            role_record = (before.get("roles") or {}).get(role)
        role_record = role_record if isinstance(role_record, Mapping) else {}
        append_live_event(
            "CTRL",
            "state_transition",
            page_url=str(role_record.get("page_url") or ""),
            page_id=str(role_record.get("page_id") or "") or None,
            role=role or None,
            task_id=str(after.get("task_id") or before.get("task_id") or "") or None,
            team=str(after.get("team") or before.get("team") or "") or None,
            request_id=str(after_hop.get("request_id") or before_hop.get("request_id") or "") or None,
            generation=int(role_record.get("conversation_generation") or 0),
            changes=changes,
            values=values,
        )

    def _persist_control_result(
        self,
        manifest_path: Path,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        control_id: int,
        action: str,
    ) -> dict[str, Any]:
        before_control = next(
            (
                item
                for item in before.get("controls") or []
                if isinstance(item, Mapping) and item.get("control_id") == control_id
            ),
            None,
        )
        after_control = next(
            (
                item
                for item in after.get("controls") or []
                if isinstance(item, Mapping) and item.get("control_id") == control_id
            ),
            None,
        )
        if before_control is None or after_control is None:
            raise ValueError(f"control {control_id} changed while it was being applied")
        if before_control.get("action") != action or after_control.get("action") != action:
            raise ValueError(f"control {control_id} action changed while it was being applied")

        safe_restore_source_rejection = None
        if action == "safe_restore":
            source_task_id = str(before_control.get("source_task_id") or "").strip()
            source_event_key = str(before_control.get("source_event_key") or "").strip()
            source = self.store.load_task_id(source_task_id) if source_task_id else None
            independent = source.get("independent") if isinstance(source, Mapping) else None
            active_event = (
                independent.get("active_event") if isinstance(independent, Mapping) else None
            )
            current_event_keys = (
                {
                    str(event.get("event_key") or "")
                    for event in canonical_independent_events(source, self.store.discover())
                }
                if isinstance(source, Mapping)
                else set()
            )
            if (
                not source_task_id
                or not source_event_key
                or not isinstance(active_event, Mapping)
                or str(active_event.get("event_key") or "") != source_event_key
                or str(active_event.get("target_task_id") or "")
                != str(before.get("task_id") or "")
                or source_event_key not in current_event_keys
            ):
                safe_restore_source_rejection = (
                    "Safe restore source event changed before persistence."
                )

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            current_control = next(
                (
                    item
                    for item in current.get("controls") or []
                    if isinstance(item, dict) and item.get("control_id") == control_id
                ),
                None,
            )
            if current_control is None or current_control.get("action") != action:
                raise ValueError(f"control {control_id} changed while it was being applied")
            if action == "safe_restore":
                command_value = current_control.get("command")
                command_snapshot_value = (
                    command_value.get("snapshot")
                    if isinstance(command_value, Mapping)
                    else None
                )
                rejection_reason = safe_restore_source_rejection
                if str(current_control.get("origin") or "") != "independent_agent":
                    rejection_reason = "Safe restore requires independent-agent provenance."
                elif str(current.get("status") or "").upper() != "BLOCKED":
                    rejection_reason = "Safe restore target changed before persistence."
                elif not isinstance(command_snapshot_value, Mapping):
                    rejection_reason = "Safe restore command snapshot is missing."
                elif (
                    command_snapshot_value.get("active_hop_id") != current.get("active_hop_id")
                    or command_snapshot_value.get("active_request_id")
                    != (str((_active_hop(current)).get("request_id") or "") or None)
                ):
                    rejection_reason = "Safe restore active execution changed before persistence."
                elif not self._execution_is_pristine_pre_send(current):
                    rejection_reason = (
                        "Safe restore requires provably pristine pre-Send state at persistence."
                    )
                if rejection_reason is not None:
                    current_control["status"] = "rejected"
                    current_control["command_state"] = "REJECTED"
                    current_control["result"] = rejection_reason
                    current_control["applied_at"] = utc_now()
                    return current
            if action == "release_required_browser_endpoint":
                command_value = current_control.get("command")
                command_snapshot_value = (
                    command_value.get("snapshot")
                    if isinstance(command_value, Mapping)
                    else None
                )
                audited_required_endpoint = (
                    str(command_snapshot_value.get("required_browser_endpoint_id") or "").strip()
                    if isinstance(command_snapshot_value, Mapping)
                    else ""
                )
                current_required_endpoint = str(
                    current.get("required_browser_endpoint_id") or ""
                ).strip()
                eligibility = workflow_control_eligibility(
                    current,
                    action,
                    role=current_control.get("role"),
                )
                rejection_reason = None
                if (
                    not audited_required_endpoint
                    or current_required_endpoint != audited_required_endpoint
                ):
                    rejection_reason = (
                        "Required browser endpoint changed after this control was queued."
                    )
                elif not eligibility["eligible"]:
                    rejection_reason = str(
                        eligibility["reason"]
                        or "Required browser endpoint release is no longer eligible."
                    )
                elif not self._execution_is_pristine_pre_send(
                    current, allow_prepared_ledger=True
                ):
                    rejection_reason = (
                        "Required browser endpoint release requires provably pristine "
                        "pre-Send state at persistence."
                    )
                if rejection_reason is not None:
                    current_control["status"] = "rejected"
                    current_control["command_state"] = "REJECTED"
                    current_control["result"] = rejection_reason
                    current_control["applied_at"] = utc_now()
                    return current
            for key in before.keys() | after.keys():
                if key in {"controls", "updated_at"}:
                    continue
                if key not in before:
                    if key not in current:
                        current[key] = json.loads(
                            json.dumps(after[key], ensure_ascii=False, default=str)
                        )
                    elif current[key] != after[key]:
                        raise ValueError(
                            f"task changed at {key}; reload before applying control"
                        )
                elif key not in after:
                    if key not in current:
                        continue
                    if current[key] != before[key]:
                        raise ValueError(
                            f"task changed at {key}; reload before applying control"
                        )
                    current.pop(key)
                else:
                    current[key] = self._merge_control_delta(
                        current.get(key),
                        before[key],
                        after[key],
                        path=key,
                    )
            self._merge_control_delta(
                current_control,
                before_control,
                after_control,
                path=f"controls[{control_id}]",
            )
            if action in {"stop", "reset"}:
                before_controls = {
                    item.get("control_id"): item
                    for item in before.get("controls") or []
                    if isinstance(item, Mapping)
                    and isinstance(item.get("control_id"), int)
                }
                after_controls = {
                    item.get("control_id"): item
                    for item in after.get("controls") or []
                    if isinstance(item, Mapping)
                    and isinstance(item.get("control_id"), int)
                }
                current_controls = {
                    item.get("control_id"): item
                    for item in current.get("controls") or []
                    if isinstance(item, dict)
                    and isinstance(item.get("control_id"), int)
                }
                for prior_id, prior_before in before_controls.items():
                    if prior_id == control_id:
                        continue
                    prior_after = after_controls.get(prior_id)
                    current_prior = current_controls.get(prior_id)
                    if (
                        prior_after is None
                        or current_prior is None
                        or prior_after == prior_before
                    ):
                        continue
                    self._merge_control_delta(
                        current_prior,
                        prior_before,
                        prior_after,
                        path=f"controls[{prior_id}]",
                    )
            return current

        saved = self.store.update_for_control(manifest_path, action, merge)
        self._record_controller_live_transition(before, saved)
        self._remember_manifest(manifest_path, saved)
        return saved

    def _persist_transport_result(
        self,
        manifest_path: Path,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> dict[str, Any]:
        def merge(current: dict[str, Any]) -> dict[str, Any]:
            for key in before.keys() | after.keys():
                if key in {"controls", "updated_at"}:
                    continue
                if key not in before:
                    if key not in current:
                        current[key] = json.loads(
                            json.dumps(after[key], ensure_ascii=False, default=str)
                        )
                    elif current[key] != after[key]:
                        raise ValueError(
                            f"task changed at {key}; reload before applying transport state"
                        )
                elif key not in after:
                    if key not in current:
                        continue
                    if current[key] != before[key]:
                        raise ValueError(
                            f"task changed at {key}; reload before applying transport state"
                        )
                    current.pop(key)
                else:
                    current[key] = self._merge_control_delta(
                        current.get(key),
                        before[key],
                        after[key],
                        path=key,
                    )
            return current

        initial_placement = (
            before.get("endpoint_pinned", True) is False
            and after.get("endpoint_pinned") is True
        )
        saved = (
            self.store.update_initial_placement(manifest_path, merge)
            if initial_placement
            else self.store.update(manifest_path, merge)
        )
        self._record_controller_live_transition(before, saved)
        self._remember_manifest(manifest_path, saved)
        return saved

    async def _apply_independent_control(
        self,
        state: dict[str, Any],
        control: Mapping[str, Any],
        actions: CDPATabActions,
        *,
        role: str,
        hop: dict[str, Any] | None,
    ) -> Any:
        independent = state.get("independent")
        if not isinstance(independent, dict):
            raise RuntimeError("independent task state is missing")
        if role != INDEPENDENT_ROLE:
            raise RuntimeError("independent controls must target AGENT")
        action = str(control.get("action") or "")
        active = independent.get("active_event") is not None
        if action == "clear_team":
            independent["board_projection_hidden_at"] = utc_now()
            return {"board_projection_removed": True, "active_job_preserved": active}
        if action == "pause":
            if str(state.get("status") or "").upper() in TERMINAL:
                raise RuntimeError("cannot pause a terminal independent task")
            independent["enabled"] = False
            state["status"] = "PAUSED"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "paused"
            state["pause_reason"] = control.get("reason") or "independent agent paused"
            state["waiting"] = None
            state["waiting_reason"] = None
            state["waiting_code"] = None
            return {"enabled": False, "active_job_preserved": active}
        if action == "resume":
            if str(state.get("status") or "").upper() in TERMINAL:
                raise RuntimeError("cannot enable a terminal independent task")
            self.store.assert_independent_enable_allowed(state["manifest_path"])
            was_recovery_owner = bool(independent.get("enabled")) and bool(
                validate_trigger_settings(
                    independent.get("trigger_settings")
                )["recovery"]
            )
            at = utc_now()
            independent["enabled"] = True
            refresh_recovery_warmup_on_enable(
                independent,
                was_owner=was_recovery_owner,
                is_owner=bool(
                    validate_trigger_settings(
                        independent.get("trigger_settings")
                    )["recovery"]
                ),
                enabled_at=at,
            )
            state["status"] = "RUNNING" if active else "WAITING"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "resuming" if active else "waiting_trigger"
            state["pause_reason"] = None
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            if active:
                state["waiting"] = None
                state["waiting_reason"] = None
                state["waiting_code"] = None
            else:
                state["waiting"] = {
                    "reason": "trigger",
                    "waiting_on": [],
                    "stopped": [],
                    "missing": [],
                    "since": at,
                }
                state["waiting_reason"] = "waiting for trigger"
                state["waiting_code"] = "trigger"
                independent["idle_since"] = at
            return {"enabled": True, "active_job_preserved": active}
        if action == "retry":
            if str(state.get("status") or "").upper() != "BLOCKED":
                raise RuntimeError("Retry is valid only for a BLOCKED independent job")
            if not active:
                raise RuntimeError("blocked independent task has no active job")
            state["status"] = "RUNNING"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "retrying"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            if hop is not None and hop.get("state") == "abandoned":
                hop["state"] = "pre_send"
            return {"event_key": independent["active_event"].get("event_key")}
        if action == "new_chat":
            status = str(state.get("status") or "").upper()
            if active or status not in {"WAITING", "PAUSED"}:
                raise RuntimeError("Renew requires an idle independent agent")
            if hop is not None and (
                str(hop.get("state") or "") in IN_FLIGHT
                or hop.get("receipt") is not None
            ):
                raise RuntimeError(
                    "cannot Renew across an accepted in-flight request"
                )
            selected = await actions.preflight_team(state)
            closed = await actions.close_team(state, preflighted_pages=selected)
            if await actions.preflight_team(state):
                raise IneffectiveControlError(
                    "Renew did not remove every exact independent-agent tab"
                )
            record = state["roles"][role]
            record["page_id"] = None
            record["page_url"] = None
            record["online"] = False
            record["last_activity_at"] = utc_now()
            if hop is not None:
                hop["conversation_url"] = None
            independent["new_chat_next_job"] = True
            independent["new_chat_deferred_task_id"] = None
            independent["idle_tab_closed_at"] = utc_now()
            return {"renewed": True, "closed_tabs": closed}
        if action == "open_tab":
            acquired = await actions.locate_owned(state, role)
            reopened = False
            created = False
            if acquired is None:
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return {"deferred": True, "reason": "rate_limit_cooldown"}
                role_record = state["roles"][role]
                active_hop_url = str((hop or {}).get("conversation_url") or "").strip()
                saved_identity = bool(
                    str(role_record.get("page_id") or "").strip()
                    and (
                        str(role_record.get("page_url") or "").strip()
                        or active_hop_url
                    )
                )
                if saved_identity:
                    acquired = await actions.reopen(state, role)
                    reopened = True
                else:
                    acquired = await actions.acquire(state, role)
                    created = True
            else:
                await actions.open_tab(acquired)
            self._record_acquired(state, role, acquired)
            return {
                "page_id": acquired.page_id,
                "reopened": reopened,
                "created": created,
            }
        if action == "close_tab":
            if hop is not None and str(hop.get("state") or "") in IN_FLIGHT:
                raise RuntimeError(
                    "cannot close an independent-agent tab across an accepted in-flight request"
                )
            selected = await actions.preflight_team(state)
            closed = await actions.close_team(state, preflighted_pages=selected)
            if await actions.preflight_team(state):
                raise IneffectiveControlError(
                    "Close tab did not remove every exact independent-agent tab"
                )
            record = state["roles"][role]
            record["online"] = False
            record["last_activity_at"] = utc_now()
            return {"closed_tabs": closed}
        if action == "reset":
            role_record = (state.get("roles") or {}).get(role) or {}
            has_owned_page = bool(role_record.get("page_id") or role_record.get("page_url"))
            acquired = await actions.locate_owned(state, role) if has_owned_page else None
            stopped_response = (
                await actions.stop_if_active(acquired) if acquired is not None else False
            )
            reset_id = control.get("control_id")
            if isinstance(reset_id, int):
                for prior in state.get("controls") or []:
                    if not isinstance(prior, dict):
                        continue
                    prior_id = prior.get("control_id")
                    if (
                        isinstance(prior_id, int)
                        and prior_id < reset_id
                        and prior.get("status") == "requested"
                    ):
                        prior["status"] = "rejected"
                        prior["command_state"] = "REJECTED"
                        prior["result"] = {
                            "outcome": "rejected",
                            "action": str(prior.get("action") or ""),
                            "reason_code": "superseded_by_reset",
                            "reason": "Superseded by a later explicit independent-agent Reset.",
                            "postcondition": "superseded_by_reset",
                        }
                        prior["applied_at"] = utc_now()
            event = independent.get("active_event")
            if not isinstance(event, Mapping):
                return {"reset": False, "reason": "no active job"}
            if hop is not None and hop.get("state") not in {
                "responded",
                "routed",
                "abandoned",
            }:
                hop["state"] = "abandoned"
                hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
            self.store._release_independent_job(
                state,
                disposition="RESET",
                now=utc_now(),
                reason=str(control.get("reason") or "independent job reset"),
                preserve_enabled=True,
            )
            return {
                "reset": True,
                "stopped_response": stopped_response,
                "enabled": bool(independent.get("enabled")),
            }
        if action == "stop":
            raise RuntimeError("independent agents use Reset; Stop is not supported")
        raise RuntimeError(f"unsupported independent control action {action!r}")

    async def _release_blocked_workflow_tabs(
        self,
        state: Mapping[str, Any],
        actions: CDPATabActions,
    ) -> None:
        if is_independent_task(state) or str(state.get("status") or "").upper() != "BLOCKED":
            return
        try:
            hop = _active_hop(state)
        except Exception:
            hop = None
        if (
            isinstance(hop, Mapping)
            and str(hop.get("state") or "") == "sending"
            and self._sending_hop_durable_boundary_state(hop) != "preboundary"
        ):
            return
        try:
            selected = await actions.preflight_team(state)
            if selected:
                await actions.close_team(state, preflighted_pages=selected)
        except Exception:
            # BLOCKED truth is already durable. Browser-resource release is best-effort
            # and must never replace the original diagnosis.
            return

    def _accepted_admission_integrity_error(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
    ) -> str | None:
        if self._sending_hop_durable_boundary_state(hop) == "preboundary":
            return None
        task_text = state.get("task_text")
        expected = state.get("task_text_sha256")
        if isinstance(task_text, str) and isinstance(expected, str):
            actual = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
            if actual != expected:
                return "accepted admission integrity corruption: task_text hash mismatch"
        return None

    def _sending_hop_durable_boundary_state(self, hop: Mapping[str, Any]) -> str:
        receipt = hop.get("receipt")
        if isinstance(receipt, Mapping) and receipt:
            return "crossed"
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        if not ledger_path or not request_id:
            return "preboundary"
        try:
            ledger = RequestLedger(ledger_path)
            record = ledger.get(request_id) if ledger.path.exists() else None
        except Exception:
            return "ambiguous"
        if record is None:
            return "preboundary"
        if record.status in {
            RequestStatus.SENDING,
            RequestStatus.SENT,
            RequestStatus.COMPLETED,
        }:
            return "crossed"
        if int(record.attempts or 0) > 0 or any(
            value is not None
            for value in (
                record.binding,
                record.baseline,
                record.receipt,
                record.accepted_at,
                record.response,
                record.session_id_before,
            )
        ):
            return "crossed"
        return "preboundary"

    async def _apply_control(
        self,
        state: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path | None = None,
        *,
        scheduling_tasks: Sequence[Mapping[str, Any]] | None = None,
        persisted_result: list[bool] | None = None,
    ) -> bool:
        control = _next_requested_control(state)
        if control is None:
            return False
        action = str(control.get("action") or "")
        control_id = control.get("control_id")
        baseline = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        baseline_control_id = control.get("control_id")
        role = str(control.get("role") or state.get("active_role") or "PLAN").upper()
        guard_blocked = (
            str(baseline.get("status") or "").upper() == "BLOCKED"
            and str(baseline.get("block_code") or "")
            == _LEGACY_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE
        )
        budget_blocked = (
            str(baseline.get("status") or "").upper() == "BLOCKED"
            and str(baseline.get("block_code") or "")
            in {
                _AUTONOMOUS_EXECUTION_BUDGET_BLOCK_CODE,
                _LEGACY_NO_PROGRESS_ROUTE_CYCLE_BLOCK_CODE,
            }
        )
        resume_completed = False
        result: Any = None
        command: WorkerCommand | None = None
        try:
            if action in {"open_tab", "close_tab", "new_chat", "restart_role"}:
                actions = self._control_actions(state, role, actions)
            command_value = control.get("command")
            if isinstance(command_value, Mapping):
                command = WorkerCommand.from_dict(command_value)
                validate_worker_command(command, state)
                if command.action != action or command.role != control.get("role"):
                    raise ValueError("worker command payload does not match queued control")
                control["command_state"] = "RUNNING"
            hop = _active_hop(state) if state.get("active_hop_id") is not None else None
            hop_state = str(hop.get("state") or "") if hop else ""
            if not is_independent_task(state) and action != "safe_restore":
                eligibility = workflow_control_eligibility(state, action, role=role)
                if not eligibility["eligible"]:
                    raise RuntimeError(
                        str(eligibility["reason"] or f"{action} is not eligible")
                    )
            if is_independent_task(state):
                if action == "open_tab":
                    result = await self._apply_independent_control(
                        state, control, actions, role=role, hop=hop
                    )
                else:
                    result = await self._apply_independent_control(
                        state,
                        control,
                        actions,
                        role=role,
                        hop=hop,
                    )
            elif action == "release_required_browser_endpoint":
                if not self._execution_is_pristine_pre_send(
                    state, allow_prepared_ledger=True
                ):
                    raise RuntimeError(
                        "Required browser endpoint release requires provably pristine pre-Send state."
                    )
                before = command_snapshot(state, role=role)
                state.pop("required_browser_endpoint_id", None)
                result = {
                    "outcome": "applied",
                    "action": action,
                    "reason_code": None,
                    "reason": control.get("reason")
                    or "Operator released the hard browser endpoint requirement.",
                    "next_safe_action": None,
                    "postcondition": "required_browser_endpoint_released",
                    "before": before,
                    "after": command_snapshot(state, role=role),
                }
            elif action == "pause":
                result = self._apply_pause_transition(state, control)
            elif action == "resume":
                status = str(state.get("status") or "").upper()
                if status in TERMINAL:
                    raise RuntimeError("cannot resume a terminal task")
                role_paused = (
                    status == "PAUSED"
                    and hop is not None
                    and hop_state == "routed"
                    and str(hop.get("route") or "").strip().upper() == "PAUSE"
                )
                paused_blocked = (
                    status == "PAUSED"
                    and str(state.get("resume_status") or "").upper() == "BLOCKED"
                )
                if paused_blocked:
                    state["status"] = "BLOCKED"
                    state["kanban_column"] = state.pop("resume_column", "BLOCKED")
                    state["active_action"] = state.pop("resume_action", "blocked") or "blocked"
                    state.pop("resume_status", None)
                    state["pause_reason"] = None
                    result = {
                        "outcome": "applied",
                        "action": "restore_blocked",
                        "reason_code": "operator_pause_released",
                        "reason": (
                            "Operator Resume released the manual pause without clearing "
                            "the existing block."
                        ),
                        "next_safe_action": None,
                        "postcondition": "blocked_state_restored",
                        "before": command_snapshot(baseline, role=role),
                        "after": command_snapshot(state, role=role),
                    }
                    resume_completed = True
                elif role_paused:
                    control_origin = str(
                        control.get("origin")
                        or (command.origin if command is not None else "")
                        or "operator"
                    ).strip().lower()
                    if control_origin != "operator":
                        raise RuntimeError(
                            "role-initiated pause requires explicit operator Resume"
                        )
                    source_role = str(hop.get("target_role") or "").strip().upper()
                    handoff = str(
                        hop.get("expected_report_path")
                        or hop.get("report_path")
                        or hop.get("routed_handoff")
                        or ""
                    ).strip()
                    if source_role not in state.get("roles", {}) or not handoff:
                        raise RuntimeError(
                            "role-initiated pause lost its persisted route handoff"
                        )
                    before = command_snapshot(baseline, role=source_role)
                    solo_resume = str(state.get("workflow_mode") or "team").lower() == "solo"
                    saved_url = None
                    if solo_resume and state.get("conversation_mode") != "temporary":
                        receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
                        conversation_id = str(receipt.get("conversation_id") or "").strip()
                        saved_url = _preferred_saved_url(
                            hop.get("conversation_url"),
                            state["roles"][source_role].get("page_url"),
                            conversation_id=conversation_id or None,
                        )
                        if not saved_url and conversation_id:
                            fallback = f"https://chatgpt.com/c/{conversation_id}"
                            saved_url = fallback if _official_saved_url(fallback) else None
                        if not saved_url:
                            raise RuntimeError(
                                "Solo Resume requires its saved conversation URL; refusing Fresh Chat"
                            )
                    child = self._append_hop(
                        state,
                        source_role=source_role,
                        target_role=source_role,
                        handoff=handoff,
                        kind="solo_resume" if solo_resume else "handoff",
                    )
                    if saved_url:
                        child["conversation_url"] = saved_url
                        state["roles"][source_role]["page_url"] = saved_url
                    state.pop("resume_column", None)
                    state["pause_reason"] = None
                    state["block_code"] = None
                    state["block_retryable"] = False
                    state["block_reason"] = None
                    result = {
                        "outcome": "continued",
                        "action": "release_role_pause",
                        "reason_code": None,
                        "reason": "Explicit operator Resume released the role-initiated pause.",
                        "next_safe_action": None,
                        "postcondition": "child_hop_appended",
                        "before": before,
                        "after": command_snapshot(state, role=source_role),
                        "child_hop_id": child["hop_id"],
                    }
                    resume_completed = True
                elif budget_blocked:
                    control_origin = str(
                        control.get("origin")
                        or (command.origin if command is not None else "")
                        or "operator"
                    ).strip().lower()
                    if control_origin != "operator":
                        raise RuntimeError(
                            "autonomous execution budget renewal requires explicit operator Resume"
                        )
                    if hop is None or hop_state != "routed":
                        raise RuntimeError(
                            "autonomous execution budget renewal requires its persisted routed source hop"
                        )
                    source_role = str(hop.get("target_role") or "").strip().upper()
                    pending = self._pending_budget_continuation(state, hop)
                    if source_role not in state.get("roles", {}):
                        raise RuntimeError(
                            "autonomous execution budget block lost its persisted source role"
                        )
                    before = command_snapshot(baseline, role=source_role)
                    if pending is not None:
                        kind = str(pending.get("kind") or "").strip()
                        pending_source_role = str(
                            pending.get("source_role") or ""
                        ).strip().upper()
                        target_role = str(pending.get("target_role") or "").strip().upper()
                        handoff = str(pending.get("handoff") or "").strip()
                        turn = int(pending.get("turn") or 0)
                        repair_attempt = int(pending.get("repair_attempt") or 0)
                        validation_error = str(
                            pending.get("validation_error") or ""
                        ).strip()
                        if (
                            kind not in {"report_repair", "route_repair"}
                            or pending_source_role != source_role
                            or target_role != source_role
                            or not handoff
                            or turn < 1
                            or repair_attempt < 1
                            or not validation_error
                        ):
                            raise RuntimeError(
                                "autonomous execution budget repair continuation is incomplete"
                            )
                        locked_route = str(
                            pending.get("locked_route") or ""
                        ).strip().upper()
                        locked_handoff = str(
                            pending.get("locked_handoff") or ""
                        ).strip()
                        if kind == "report_repair" and (
                            not locked_route or not locked_handoff
                        ):
                            raise RuntimeError(
                                "autonomous execution budget report repair lost its locked decision"
                            )
                        renewed = self._renew_execution_budget(state)
                        child = self._append_hop(
                            state,
                            source_role=source_role,
                            target_role=target_role,
                            handoff=handoff,
                            kind=kind,
                            turn=turn,
                            repair_attempt=repair_attempt,
                            validation_error=validation_error,
                        )
                        if kind == "report_repair":
                            child["locked_route"] = locked_route
                            child["locked_handoff"] = locked_handoff
                        after_role = target_role
                    else:
                        routed_role = str(hop.get("route") or "").strip().upper()
                        handoff = str(
                            hop.get("report_path") or hop.get("expected_report_path") or ""
                        ).strip()
                        if routed_role not in state.get("roles", {}) or not handoff:
                            raise RuntimeError(
                                "autonomous execution budget block lost its persisted route decision"
                            )
                        renewed = self._renew_execution_budget(state)
                        child = self._append_hop(
                            state,
                            source_role=source_role,
                            target_role=routed_role,
                            handoff=handoff,
                        )
                        after_role = routed_role
                    result = {
                        "outcome": "continued",
                        "action": "renew_execution_budget",
                        "reason_code": None,
                        "reason": "Explicit operator Resume renewed the autonomous execution budget.",
                        "next_safe_action": None,
                        "postcondition": "child_hop_appended",
                        "before": before,
                        "after": command_snapshot(state, role=after_role),
                        "budget_epoch": renewed["epoch"],
                        "child_hop_id": child["hop_id"],
                    }
                    resume_completed = True
                elif guard_blocked:
                    control_origin = str(
                        control.get("origin")
                        or (command.origin if command is not None else "")
                        or "operator"
                    ).strip().lower()
                    if control_origin != "operator":
                        raise RuntimeError(
                            "consecutive self-route guard requires explicit operator Resume"
                        )
                    if hop is None or hop_state != "routed":
                        raise RuntimeError(
                            "consecutive self-route guard requires its persisted routed source hop"
                        )
                    source_role = str(hop.get("target_role") or "").strip().upper()
                    routed_role = str(hop.get("route") or "").strip().upper()
                    handoff = str(
                        (
                            hop.get("expected_report_path")
                            if hop.get("report_sha256") is not None
                            else hop.get("report_path")
                        )
                        or ""
                    ).strip()
                    if (
                        not source_role
                        or routed_role != source_role
                        or routed_role not in state.get("roles", {})
                        or not handoff
                    ):
                        raise RuntimeError(
                            "consecutive self-route guard lost its persisted route handoff"
                        )
                    before = command_snapshot(baseline, role=source_role)
                    self._renew_execution_budget(state)
                    child = self._append_hop(
                        state,
                        source_role=source_role,
                        target_role=routed_role,
                        handoff=handoff,
                    )
                    result = {
                        "outcome": "continued",
                        "action": "release_self_route_guard",
                        "reason_code": None,
                        "reason": "Explicit operator Resume released the consecutive self-route guard.",
                        "next_safe_action": None,
                        "postcondition": "child_hop_appended",
                        "before": before,
                        "after": command_snapshot(state, role=routed_role),
                        "child_hop_id": child["hop_id"],
                    }
                    resume_completed = True
                else:
                    if status == "PAUSED":
                        state["kanban_column"] = state.pop(
                            "resume_column", _column_for(str(state.get("active_role") or "PLAN"))
                        )
                        paused_resume_status = str(
                            state.pop("resume_status", "") or ""
                        ).upper()
                        state.pop("resume_action", None)
                    else:
                        paused_resume_status = ""
                        state["kanban_column"] = _column_for(
                            str(state.get("active_role") or "PLAN")
                        )
                    state["status"] = (
                        "INBOX"
                        if status == "INBOX" or paused_resume_status == "INBOX"
                        else "RUNNING"
                    )
                    state["pause_reason"] = None
                    state["block_code"] = None
                    state["block_retryable"] = False
                    state["block_reason"] = None
                    state["active_action"] = "resuming"
                    result = {
                        "outcome": "recovering",
                        "action": "none",
                        "reason_code": "resume_requested",
                        "reason": "Operator requested verified continuation.",
                        "next_safe_action": None,
                        "postcondition": None,
                        "before": command_snapshot(baseline, role=role),
                        "after": None,
                    }
                    if self.store.enforce_dependency_barrier(
                        state,
                        scheduling_tasks or (),
                    ):
                        result = {
                            "outcome": "applied",
                            "action": "wait_dependency",
                            "reason_code": "dependency_barrier",
                            "reason": (
                                "Resume cleared the operator recovery state, but unfinished "
                                "dependencies keep this task waiting."
                            ),
                            "next_safe_action": (
                                "Complete or remove the exact unfinished parent dependency."
                            ),
                            "postcondition": "waiting_dependency",
                            "before": command_snapshot(baseline, role=role),
                            "after": command_snapshot(state, role=role),
                        }
            elif action == "safe_restore":
                source_task_id = str(control.get("source_task_id") or "").strip()
                source_event_key = str(control.get("source_event_key") or "").strip()
                if not source_task_id or not source_event_key:
                    raise RuntimeError("safe restore requires independent-agent source provenance")
                source = self.store.load_task_id(source_task_id)
                independent = source.get("independent") if isinstance(source, Mapping) else None
                active_event = independent.get("active_event") if isinstance(independent, Mapping) else None
                current_event_keys = {
                    str(event.get("event_key") or "")
                    for event in canonical_independent_events(source, self.store.discover())
                }
                if (
                    not isinstance(active_event, Mapping)
                    or str(active_event.get("event_key") or "") != source_event_key
                    or str(active_event.get("target_task_id") or "") != str(state.get("task_id") or "")
                    or source_event_key not in current_event_keys
                ):
                    raise RuntimeError("safe restore source event is stale or targets different work")
                if state.get("status") != "BLOCKED":
                    raise RuntimeError("safe restore requires a BLOCKED workflow task")
                if not self._execution_is_pristine_pre_send(state):
                    raise RuntimeError(
                        "safe restore requires provably pristine pre-Send state with no durable request"
                    )
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(str(state.get("active_role") or "PLAN"))
                state["block_code"] = None
                state["block_retryable"] = False
                state["block_reason"] = None
                state["pause_reason"] = None
                state["active_action"] = "safe_restore"
                dependency_blocked = self.store.enforce_dependency_barrier(
                    state,
                    scheduling_tasks if scheduling_tasks is not None else self.store.discover(),
                )
                result = {
                    "outcome": "applied",
                    "action": "safe_restore",
                    "postcondition": (
                        "waiting_dependency"
                        if dependency_blocked
                        else "same_task_pristine_pre_send_restored"
                    ),
                    "task_id": state.get("task_id"),
                    "source_event_key": source_event_key,
                }
            elif action == "retry":
                if state.get("status") != "BLOCKED":
                    raise RuntimeError("Retry is valid only for a BLOCKED task")
                if not state.get("block_retryable"):
                    raise RuntimeError("Retry is disabled for this non-retryable block")
                if hop and hop.get("validation_error"):
                    hop["repair_attempt"] = max(
                        0, self.config.route_repair_attempts - 1
                    )
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(
                    str(state.get("active_role") or "PLAN")
                )
                state["block_code"] = None
                state["block_retryable"] = False
                state["block_reason"] = None
                state["active_action"] = "retrying"
                result = {
                    "outcome": "applied",
                    "action": "retry",
                    "postcondition": "retry_rearmed",
                    "hop_id": hop.get("hop_id") if hop is not None else None,
                    "repair_attempt": hop.get("repair_attempt") if hop is not None else None,
                }
            elif action == "stop":
                if state.get("status") in TERMINAL:
                    raise RuntimeError("Stop is invalid for a terminal task")
                self._terminalize_controls_superseded_by_stop(state, control)
                stop_role = str(state.get("active_role") or role).upper()
                stopped_response = False
                browser_stop_error = None
                try:
                    acquired = await actions.locate_owned(state, stop_role)
                    if acquired is not None:
                        stopped_response = await actions.stop_if_active(acquired)
                except Exception as exc:
                    browser_stop_error = sanitize_exception(exc)
                result = {"stopped_response": stopped_response}
                if browser_stop_error:
                    result["browser_stop_error"] = browser_stop_error
                state["status"] = "STOPPED"
                state["terminal_state"] = "STOPPED"
                state["kanban_column"] = "DONE_STOPPED"
                state["stopped_at"] = utc_now()
                state["stop_reason"] = control.get("reason") or "manual stop"
                state["active_role"] = None
                state["active_hop_id"] = None
                state["active_action"] = "stopped"
                state["block_code"] = None
                state["block_retryable"] = False
                state["block_reason"] = None
                state["pause_reason"] = None
                state["waiting"] = None
                state["waiting_reason"] = None
                state["waiting_code"] = None
            elif action == "restart_role":
                if guard_blocked:
                    raise RuntimeError(
                        "Restart Role cannot bypass consecutive self-route guard; explicit operator Resume is required"
                    )
                if state.get("status") in TERMINAL:
                    raise RuntimeError("cannot restart a role for a terminal task")
                blocked_restart = state.get("status") == "BLOCKED"
                if (
                    blocked_restart
                    and (hop is None or str(hop.get("target_role") or "") != role)
                ):
                    raise RuntimeError(
                        "blocked role restart requires the active hop to belong to the selected role"
                    )
                if hop_state in {"sent", "waiting"}:
                    raise RuntimeError(
                        "cannot restart a role across an in-flight send boundary"
                    )
                if hop_state == "sending":
                    boundary_state = self._sending_hop_durable_boundary_state(hop or {})
                    if boundary_state == "crossed":
                        raise RuntimeError(
                            "cannot restart a role across an in-flight send boundary"
                        )
                    if boundary_state == "ambiguous":
                        raise RuntimeError(
                            "cannot restart a role while durable Send-boundary evidence is ambiguous; use Resume"
                        )
                    raise RuntimeError(
                        "cannot restart a role while a pre-acceptance durable request is pending; use Resume to continue the same request"
                    )
                old_page_id = state["roles"][role].get("page_id")
                locator = getattr(actions, "locate_owned", None)
                existing_role = await locator(state, role) if callable(locator) else True
                if existing_role is None and self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
                else:
                    solo_exact_url = (
                        str((hop or {}).get("conversation_url") or "").strip()
                        or str(state["roles"][role].get("page_url") or "").strip()
                    )
                    restart_exact = getattr(actions, "restart_exact", None)
                    if (
                        str(state.get("workflow_mode") or "team").strip().lower() == "solo"
                        and solo_exact_url
                        and callable(restart_exact)
                    ):
                        acquired = await restart_exact(
                            state,
                            role,
                            url=solo_exact_url,
                        )
                    else:
                        acquired = await actions.restart(
                            state,
                            role,
                            known_automated_draft=(
                                str(hop.get("prompt") or "") if hop is not None else None
                            ) or None,
                        )
                    self._record_acquired(state, role, acquired)
                    state["roles"][role]["constructor_sent_generation"] = None
                    if blocked_restart:
                        assert hop is not None
                        old_hop_id = int(hop["hop_id"])
                        abandon_reason = (
                            str(control.get("reason") or "").strip()
                            or "manual role restart"
                        )
                        hop["state"] = "abandoned"
                        hop["abandon_reason"] = abandon_reason
                        hop["timestamps"]["abandoned_at"] = utc_now()
                        restart_source = hop
                        hops_by_id = {
                            int(item.get("hop_id") or 0): item
                            for item in state.get("hops") or []
                            if isinstance(item, Mapping)
                        }
                        for _ in range(len(hops_by_id)):
                            if str(restart_source.get("kind") or "") not in {
                                "route_repair",
                                "report_repair",
                            }:
                                break
                            parent = hops_by_id.get(int(restart_source.get("parent_hop_id") or 0))
                            if not isinstance(parent, Mapping):
                                break
                            restart_source = parent
                        restart_handoff = str(
                            restart_source.get("handoff") or hop["handoff"]
                        )
                        new_hop = self._append_hop(
                            state,
                            source_role=role,
                            target_role=role,
                            handoff=restart_handoff,
                            kind="role_restart",
                        )
                        state["block_code"] = None
                        state["block_retryable"] = False
                        state["block_reason"] = None
                        state["pause_reason"] = None
                        state.setdefault("route_timeline", []).append(
                            {
                                "at": utc_now(),
                                "hop_id": old_hop_id,
                                "source_role": role,
                                "route": role,
                                "kind": "role_restart",
                                "new_hop_id": new_hop["hop_id"],
                                "reason": abandon_reason,
                            }
                        )
                        result = {
                            "old_hop_id": old_hop_id,
                            "new_hop_id": new_hop["hop_id"],
                            "old_page_id": old_page_id,
                            "page_id": acquired.page_id,
                            "new_chat": True,
                        }
                    else:
                        result = {
                            "page_id": acquired.page_id,
                            "old_page_id": old_page_id,
                            "new_chat": True,
                        }
            elif action == "new_chat":
                if state.get("status") in TERMINAL:
                    raise RuntimeError("cannot start a new chat for a terminal task")
                if hop_state in {"sent", "waiting"}:
                    raise RuntimeError("cannot reset a role across an in-flight send boundary")
                if hop_state == "sending":
                    boundary_state = self._sending_hop_durable_boundary_state(hop or {})
                    if boundary_state == "crossed":
                        raise RuntimeError("cannot reset a role across an in-flight send boundary")
                    if boundary_state == "ambiguous":
                        raise RuntimeError(
                            "cannot reset a role while durable Send-boundary evidence is ambiguous; use Resume"
                        )
                    raise RuntimeError(
                        "cannot reset a role while a pre-acceptance durable request is pending; use Resume to continue the same request"
                    )
                locator = getattr(actions, "locate_owned", None)
                existing_role = await locator(state, role) if callable(locator) else True
                if existing_role is None and self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
                else:
                    acquired = await actions.new_chat(state, role)
                    await self._dismiss_known_rate_limit_on_existing(acquired)
                    self._record_acquired(state, role, acquired)
                    state["roles"][role]["constructor_sent_generation"] = None
                    result = {"page_id": acquired.page_id, "new_chat": True}
            elif action == "open_tab":
                if hop is not None and hop_state == "waiting" and isinstance(hop.get("receipt"), Mapping):
                    self._reconcile_hop_conversation_identity(state, hop)
                    saved_identity = _recoverable_conversation_identity(hop.get("conversation_url"))
                    command_identity = (
                        _command_saved_identity(command.snapshot.get("conversation_id"))
                        if command is not None else None
                    )
                    if saved_identity and command_identity and saved_identity != command_identity:
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB canonical command snapshot conflicts with the durable accepted conversation identity"
                        )
                blocked_role_recovery = _is_verified_role_offline_block(state, role)
                role_record = state["roles"][role]
                preboundary_recovery = (
                    hop.get("preboundary_recovery")
                    if isinstance(hop, Mapping)
                    and isinstance(hop.get("preboundary_recovery"), Mapping)
                    else None
                )
                preboundary_acquisition_recovery = bool(
                    str(state.get("status") or "").upper() == "BLOCKED"
                    and str(state.get("active_role") or "").upper() == role
                    and isinstance(hop, Mapping)
                    and str(hop.get("target_role") or "").upper() == role
                    and hop_state == "pre_send"
                    and hop.get("receipt") is None
                    and hop.get("message_identity") is None
                    and isinstance(preboundary_recovery, Mapping)
                    and preboundary_recovery.get("phase") == "recovery_required"
                    and preboundary_recovery.get("stage") == "acquisition"
                    and preboundary_recovery.get("request_id") == hop.get("request_id")
                    and bool(str(role_record.get("page_id") or "").strip())
                    and _recoverable_conversation_identity(role_record.get("page_url"))
                    is not None
                )
                if (
                    isinstance(preboundary_recovery, Mapping)
                    and preboundary_recovery.get("phase") == "recovery_required"
                    and not preboundary_acquisition_recovery
                ):
                    raise IneffectiveControlError(
                        "OPEN_ROLE_TAB pre-Send acquisition provenance is not exact; operational block remains"
                    )
                receiptless_observed_recovery = False
                if blocked_role_recovery:
                    if hop is None or str(hop.get("target_role") or "") != role:
                        raise RuntimeError(
                            "blocked role reopen requires the active hop to belong to the selected role"
                        )
                    if hop_state == "waiting":
                        self._reconcile_hop_conversation_identity(state, hop)
                    receipt = (
                        hop.get("receipt")
                        if isinstance(hop.get("receipt"), Mapping)
                        else {}
                    )
                    binding = (
                        receipt.get("binding")
                        if isinstance(receipt.get("binding"), Mapping)
                        else {}
                    )
                    conversation_url = str(hop.get("conversation_url") or "").strip()
                    accepted_waiting_reopen = (
                        hop_state == "waiting"
                        and bool(str(receipt.get("accepted_via") or "").strip())
                        and (
                            int(receipt.get("attempts") or 0) > 0
                            or bool(receipt.get("user_message_id") or receipt.get("user_turn_id"))
                        )
                        and conversation_url
                        == str(role_record.get("page_url") or "").strip()
                        and str(binding.get("page_id") or "").strip()
                        == str(role_record.get("page_id") or "").strip()
                    )
                    receiptless_observed_recovery = bool(
                        hop_state == "waiting"
                        and hop.get("observed_existing_response") is True
                        and not receipt
                        and hop.get("message_identity") is None
                    )
                    if (
                        hop_state != "pre_send"
                        and not accepted_waiting_reopen
                        and not receiptless_observed_recovery
                    ):
                        raise RuntimeError(
                            "blocked role reopen requires pre_send or an accepted waiting hop "
                            "on the exact recorded conversation"
                        )
                if blocked_role_recovery and hop is not None and hop_state == "waiting":
                    durable_conversation = conversation_identity(
                        hop.get("conversation_url")
                        or state["roles"][role].get("page_url")
                    )
                    command_conversation = (
                        command.snapshot.get("conversation_id")
                        if command is not None
                        else None
                    )
                    command_canonical = _command_saved_identity(command_conversation)
                    if (
                        command_canonical is not None
                        and durable_conversation is not None
                        and command_canonical != durable_conversation
                    ):
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB canonical command snapshot conflicts with the durable accepted conversation identity"
                        )
                    expected_conversation = command_canonical or durable_conversation
                else:
                    expected_conversation = (
                        command.snapshot.get("conversation_id")
                        if command is not None
                        else conversation_identity(
                            (hop.get("conversation_url") if hop is not None else None)
                            or state["roles"][role].get("page_url")
                        )
                    )
                expected_binding_page_id = None
                if blocked_role_recovery and hop is not None and hop_state == "waiting":
                    receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
                    binding = receipt.get("binding") if isinstance(receipt.get("binding"), Mapping) else {}
                    expected_binding_page_id = str(binding.get("page_id") or "").strip() or None
                elif preboundary_acquisition_recovery:
                    durable_conversation = _recoverable_conversation_identity(role_record.get("page_url"))
                    command_conversation = (
                        _command_saved_identity(command.snapshot.get("conversation_id"))
                        if command is not None else None
                    )
                    if command_conversation is not None and command_conversation != durable_conversation:
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB command snapshot conflicts with the durable pre-Send conversation identity"
                        )
                    expected_conversation = durable_conversation
                    expected_binding_page_id = str(role_record.get("page_id") or "").strip() or None

                async def acquire_open_tab() -> AcquiredRole:
                    nonlocal blocked_role_recovery
                    if blocked_role_recovery and receiptless_observed_recovery:
                        hop["state"] = "pre_send"
                        hop.pop("observed_existing_response", None)
                        hop["wait"] = {}
                        hop.get("timestamps", {}).pop("waiting_at", None)
                        acquired = await actions.fresh_chat(
                            state,
                            role,
                            url=(
                                "https://chatgpt.com/?temporary-chat=true"
                                if state.get("conversation_mode") == "temporary"
                                else "https://chatgpt.com/"
                            ),
                            clean_for_send=True,
                        )
                        return acquired
                    if blocked_role_recovery:
                        try:
                            return await self._recover_active_role(
                                state, role, actions, foreground=True
                            )
                        except RoleOwnershipError as exc:
                            raise IneffectiveControlError(str(exc)) from exc
                    if preboundary_acquisition_recovery:
                        if existing_open_tab is not None:
                            await actions.open_tab(existing_open_tab)
                            return existing_open_tab
                        return await actions.reopen(
                            state,
                            role,
                            require_clean_ready=False,
                            foreground=True,
                        )
                    current = await actions.locate_owned(state, role)
                    if current is None:
                        return await actions.reopen(state, role)
                    await actions.open_tab(current)
                    return current

                locator = getattr(actions, "locate_owned", None)
                existing_open_tab = await locator(state, role) if callable(locator) else True
                if existing_open_tab is None and self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
                    acquired = None
                else:
                    try:
                        acquired = await acquire_open_tab()
                    except RateLimitBlockedError as exc:
                        await self._enter_rate_limit_cooldown(state, actions, exc)
                        state["active_action"] = "rate_limit_cooldown"
                        result = {"deferred": True, "reason": "rate_limit_cooldown"}
                        acquired = None
                if acquired is None:
                    recovered = False
                else:
                    await self._dismiss_known_rate_limit_on_existing(acquired)
                    recovered = (
                        blocked_role_recovery
                        or preboundary_acquisition_recovery
                        or bool(acquired.created)
                    )
                if acquired is not None:
                    acquired_conversation = conversation_identity(acquired.url)
                    if expected_conversation and acquired_conversation != expected_conversation:
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB reopened a different conversation; operational block remains"
                        )
                    if (
                        expected_binding_page_id is not None
                        and str(acquired.page_id) != expected_binding_page_id
                    ):
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB did not restore the recorded page binding"
                        )
                    self._record_acquired(state, role, acquired)
                    role_after = state["roles"][role]
                    if (
                        not bool(role_after.get("online"))
                        or str(role_after.get("page_id") or "") != str(acquired.page_id)
                        or (
                            expected_conversation
                            and conversation_identity(role_after.get("page_url"))
                            != expected_conversation
                        )
                    ):
                        raise IneffectiveControlError(
                            "OPEN_ROLE_TAB did not establish the required role ownership postcondition"
                        )
                    result = {"page_id": acquired.page_id, "recovered": recovered}
                    if preboundary_acquisition_recovery:
                        current_recovery = hop.get("preboundary_recovery")
                        if not (
                            isinstance(current_recovery, Mapping)
                            and current_recovery.get("phase") == "recovery_required"
                            and current_recovery.get("stage") == "acquisition"
                            and current_recovery.get("request_id") == hop.get("request_id")
                            and hop.get("receipt") is None
                            and hop.get("message_identity") is None
                        ):
                            raise IneffectiveControlError(
                                "OPEN_ROLE_TAB pre-Send acquisition provenance changed during reconciliation"
                            )
                        hop.pop("preboundary_recovery", None)
                        state["status"] = "RUNNING"
                        state["kanban_column"] = _column_for(role)
                        state["block_code"] = None
                        state["block_retryable"] = False
                        state["block_reason"] = None
                        state["pause_reason"] = None
                        state["active_action"] = "resuming"
                        result.update(
                            resumed=True,
                            hop_id=hop["hop_id"],
                            request_id=hop["request_id"],
                            preboundary_acquisition_reconciled=True,
                        )
                    elif blocked_role_recovery:
                        state["status"] = "RUNNING"
                        state["kanban_column"] = _column_for(role)
                        state["block_code"] = None
                        state["block_retryable"] = False
                        state["block_reason"] = None
                        state["pause_reason"] = None
                        state["active_action"] = "resuming"
                        result.update(
                            resumed=True,
                            hop_id=hop["hop_id"],
                            request_id=hop["request_id"],
                        )
            elif action == "route_plan":
                if guard_blocked:
                    raise RuntimeError(
                        "Route PLAN cannot bypass consecutive self-route guard; explicit operator Resume is required"
                    )
                assert hop is not None
                route_reason = (
                    str(control.get("reason") or "").strip()
                    or "manual route to PLAN"
                )
                if (
                    str(state.get("status") or "").upper() == "BLOCKED"
                    and str(state.get("block_code") or "")
                    == "accepted_conversation_identity_unresolved"
                ):
                    if str(control.get("origin") or "operator").strip().lower() != "operator":
                        raise RuntimeError(
                            "accepted-send reconciliation requires explicit operator Route PLAN"
                        )
                    result = self._route_unresolved_accepted_to_plan(
                        state,
                        hop,
                        reason=route_reason,
                    )
                else:
                    result = self._route_to_plan(
                        state,
                        hop,
                        reason=route_reason,
                        kind="control",
                    )
            elif action == "archive_worktree":
                if manifest_path is None:
                    raise RuntimeError("worktree archival requires a manifest path")
                saved = self._apply_worktree_archive_control(
                    state,
                    manifest_path,
                    control,
                )
                state.clear()
                state.update(saved)
                if persisted_result is not None:
                    persisted_result.append(True)
                return True
            elif action == "clear_team":
                cleanup = state.setdefault("cleanup", {})
                if cleanup.get("state") == "CLEARED":
                    if manifest_path is None:
                        raise RuntimeError("Clear Team requires a manifest path")
                    selected_pages = await actions.preflight_team(state)
                    if selected_pages:
                        if not self._start_cleanup(
                            state,
                            control=control,
                            target_tabs=len(selected_pages),
                            manifest_path=manifest_path,
                        ):
                            if persisted_result is not None:
                                persisted_result.append(True)
                            return True
                        cleanup = state["cleanup"]
                        control = self._cleanup_control(state, cleanup) or control
                        completed = await self._continue_cleanup(
                            state,
                            actions,
                            manifest_path,
                            preflighted_pages=selected_pages,
                        )
                        if completed:
                            def mark_reverified(current: dict[str, Any]) -> dict[str, Any]:
                                current_cleanup = current.setdefault("cleanup", {})
                                if (
                                    current_cleanup.get("state") != "CLEARED"
                                    or not current_cleanup.get("verified_empty_at")
                                ):
                                    raise ValueError(
                                        "cleanup re-verification did not reach a verified CLEARED state"
                                    )
                                current_control = self._cleanup_control(current, current_cleanup)
                                if current_control is not None:
                                    current_result = current_control.get("result")
                                    if not isinstance(current_result, dict):
                                        current_result = {}
                                        current_control["result"] = current_result
                                    current_result["reverified"] = True
                                return current

                            saved = self.store.update(manifest_path, mark_reverified)
                            state.clear()
                            state.update(saved)
                        if persisted_result is not None:
                            persisted_result.append(True)
                        return True
                    else:
                        result = {
                            "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                            "idempotent": True,
                        }
                elif cleanup.get("state") == "CLEARING":
                    if manifest_path is None:
                        raise RuntimeError("cleanup continuation requires a manifest path")

                    def reset_for_explicit_retry(current: dict[str, Any]) -> dict[str, Any]:
                        current_cleanup = current.setdefault("cleanup", {})
                        self._reset_cleanup_retry_gate(current_cleanup)
                        current_cleanup["control_id"] = control_id
                        current_control = next(
                            (
                                item
                                for item in current.get("controls") or []
                                if isinstance(item, dict)
                                and item.get("control_id") == control_id
                            ),
                            None,
                        )
                        if current_control is None:
                            raise RuntimeError("Clear Team control is no longer present")
                        current_control["status"] = "cleanup_pending"
                        current_control["result"] = {
                            "phase": current_cleanup.get("phase"),
                            "explicit_retry": True,
                        }
                        current_control["applied_at"] = utc_now()
                        return current

                    saved = self.store.update(manifest_path, reset_for_explicit_retry)
                    state.clear()
                    state.update(saved)
                    await self._continue_cleanup(
                        state,
                        actions,
                        manifest_path,
                        force_retry=True,
                    )
                    if persisted_result is not None:
                        persisted_result.append(True)
                    return True
                else:
                    status_before = str(state.get("status") or "").upper()
                    if status_before not in TERMINAL and not bool(control.get("confirmed")):
                        raise RuntimeError("Clear Team requires confirmation for a nonterminal task")
                    if manifest_path is None:
                        raise RuntimeError("Clear Team requires a manifest path")
                    selected_pages = await actions.preflight_team(state)
                    if not self._start_cleanup(
                        state,
                        control=control,
                        target_tabs=len(selected_pages),
                        manifest_path=manifest_path,
                    ):
                        if persisted_result is not None:
                            persisted_result.append(True)
                        return True
                    cleanup = state["cleanup"]
                    control = self._cleanup_control(state, cleanup) or control
                    await self._continue_cleanup(
                        state,
                        actions,
                        manifest_path,
                        preflighted_pages=selected_pages,
                    )
                    if persisted_result is not None:
                        persisted_result.append(True)
                    return True
            else:
                raise RuntimeError(f"unsupported control action {action!r}")
        except IneffectiveControlError as exc:
            detail = sanitize_exception(exc)
            control_id = control.get("control_id")
            state.clear()
            state.update(copy.deepcopy(baseline))
            current_control = next(
                (
                    item
                    for item in state.get("controls") or []
                    if isinstance(item, dict) and item.get("control_id") == control_id
                ),
                None,
            )
            if current_control is None:
                raise RuntimeError("ineffective control disappeared during rollback")
            current_control["status"] = "ineffective"
            current_control["command_state"] = "INEFFECTIVE"
            current_control["result"] = detail
            current_control["applied_at"] = utc_now()
            if manifest_path is not None and isinstance(control_id, int):
                saved = self._persist_control_result(
                    manifest_path,
                    baseline,
                    state,
                    control_id=control_id,
                    action=action,
                )
                state.clear()
                state.update(saved)
                if persisted_result is not None:
                    persisted_result.append(True)
            return True
        except Exception as exc:
            detail = sanitize_exception(exc)
            control_id = control.get("control_id")
            control["command_state"] = "REJECTED"
            if (
                action in {"clear_team", "archive_worktree"}
                and manifest_path is not None
                and isinstance(control_id, int)
            ):
                saved = self.store.reject_control(
                    manifest_path,
                    control_id,
                    detail,
                    action=action,
                )
                state.clear()
                state.update(saved)
                if persisted_result is not None:
                    persisted_result.append(True)
                return True
            control["status"] = "rejected"
            control["result"] = detail
            control["applied_at"] = utc_now()
            if manifest_path is not None and isinstance(control_id, int):
                saved = self._persist_control_result(
                    manifest_path,
                    baseline,
                    state,
                    control_id=control_id,
                    action=action,
                )
                state.clear()
                state.update(saved)
                if persisted_result is not None:
                    persisted_result.append(True)
            return True
        if (
            action == "resume"
            and not resume_completed
            and not is_independent_task(state)
            and str(state.get("status") or "").upper() != "WAITING"
        ):
            control["status"] = "recovering"
            control["command_state"] = "RUNNING"
            control["result"] = result
            control["applied_at"] = None
        else:
            control["status"] = "applied"
            control["command_state"] = "APPLIED"
            control["result"] = result
            control["applied_at"] = utc_now()
        if manifest_path is not None and isinstance(control_id, int):
            saved = self._persist_control_result(
                manifest_path,
                baseline,
                state,
                control_id=control_id,
                action=action,
            )
            state.clear()
            state.update(saved)
            if persisted_result is not None:
                persisted_result.append(True)
        return True

    @staticmethod
    def _execution_budget(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("execution_budget")
        if isinstance(existing, dict):
            epoch = existing.get("epoch")
            limit = existing.get("limit")
            charged = existing.get("charged_hop_ids")
            if (
                isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 1
                and isinstance(limit, int)
                and not isinstance(limit, bool)
                and limit >= 1
                and isinstance(charged, list)
                and all(
                    isinstance(item, int) and not isinstance(item, bool) and item >= 1
                    for item in charged
                )
                and len(set(charged)) == len(charged)
            ):
                return existing
        charged_hop_ids = sorted(
            {
                int(hop["hop_id"])
                for hop in state.get("hops") or ()
                if isinstance(hop, Mapping)
                and isinstance(hop.get("hop_id"), int)
                and not isinstance(hop.get("hop_id"), bool)
                and int(hop["hop_id"]) >= 1
            }
        )
        budget = {
            "epoch": 1,
            "limit": _AUTONOMOUS_EXECUTION_LIMIT,
            "charged_hop_ids": charged_hop_ids,
            "renewed_at": None,
        }
        state["execution_budget"] = budget
        return budget

    @classmethod
    def _execution_budget_exhausted(cls, state: dict[str, Any]) -> bool:
        budget = cls._execution_budget(state)
        return len(budget["charged_hop_ids"]) >= int(budget["limit"])

    @staticmethod
    def _pending_budget_continuation(
        state: Mapping[str, Any], hop: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        explicit = hop.get("budget_continuation")
        if isinstance(explicit, Mapping):
            return dict(explicit)
        hop_id = hop.get("hop_id")
        for event in reversed(state.get("route_timeline") or []):
            if not isinstance(event, Mapping) or event.get("hop_id") != hop_id:
                continue
            kind = str(event.get("kind") or "").strip()
            if kind not in {"report_repair", "route_repair"}:
                return None
            role = str(hop.get("target_role") or "").strip().upper()
            continuation = {
                "kind": kind,
                "source_role": role,
                "target_role": role,
                "handoff": kind.replace("_", " "),
                "turn": int(hop.get("turn") or 0),
                "repair_attempt": int(hop.get("repair_attempt") or 0) + 1,
                "validation_error": str(
                    hop.get("validation_error") or event.get("error") or ""
                ),
            }
            if kind == "report_repair":
                continuation["locked_route"] = str(
                    hop.get("validation_route") or hop.get("route") or ""
                ).strip().upper()
                continuation["locked_handoff"] = str(
                    hop.get("validation_handoff")
                    or hop.get("expected_report_path")
                    or ""
                ).strip()
            return continuation
        return None

    @classmethod
    def _renew_execution_budget(cls, state: dict[str, Any]) -> dict[str, Any]:
        previous = cls._execution_budget(state)
        renewed = {
            "epoch": int(previous["epoch"]) + 1,
            "limit": int(previous["limit"]),
            "charged_hop_ids": [],
            "renewed_at": utc_now(),
        }
        state["execution_budget"] = renewed
        return renewed

    def _append_hop(
        self,
        state: dict[str, Any],
        *,
        source_role: str | None,
        target_role: str,
        handoff: str,
        kind: str = "handoff",
        turn: int | None = None,
        repair_attempt: int = 0,
        validation_error: str | None = None,
    ) -> dict[str, Any]:
        target_role = str(target_role).strip().upper()
        if self._execution_budget_exhausted(state):
            raise RuntimeError(
                "autonomous execution budget exhausted; explicit operator Resume is required"
            )
        if target_role not in state["roles"]:
            raise RouteContractError(
                f"route {target_role!r} is not selected for this task"
            )
        parent_hop_id = state.get("active_hop_id")
        parent_hop = next(
            (
                item
                for item in state.get("hops") or ()
                if isinstance(item, Mapping)
                and item.get("hop_id") == parent_hop_id
            ),
            None,
        )
        hop_id = max((int(item.get("hop_id") or 0) for item in state.get("hops") or []), default=0) + 1
        role_record = state["roles"][target_role]
        target_turn = int(turn or (int(role_record.get("turn") or 0) + 1))
        hop = {
            "hop_id": hop_id,
            "parent_hop_id": state.get("active_hop_id"),
            "source_role": source_role,
            "target_role": target_role,
            "physical_role": role_record["physical_role"],
            "turn": target_turn,
            "kind": kind,
            "handoff": str(handoff),
            "state": "pre_send",
            "request_id": f"{state['task_id']}-hop{hop_id}",
            "prompt": None,
            "prompt_sha256": None,
            "rendered_prompt_sha256": None,
            "ledger_path": str(
                (Path(state["manifest_path"]).parent / "requests.json").resolve()
            ),
            "receipt": None,
            "message_identity": None,
            "response": None,
            "response_sha256": None,
            "expected_report_path": None,
            "report_path": None,
            "report_sha256": None,
            "report_size": None,
            "route": None,
            "repair_attempt": int(repair_attempt),
            "validation_error": (
                sanitize_text(validation_error, max_chars=2000)
                if validation_error is not None
                else None
            ),
            "wait": {
                "started_at": None,
                "deadline_at": None,
                "continuous_responding_since": None,
                "activity_signature": None,
                "activity_length": 0,
                "activity_changed_at": None,
                "activity_observed_at": None,
                "transport_ui_active": False,
                "last_stop_visible": False,
                "refresh_count": 0,
                "last_refresh_at": None,
                "refresh_in_progress": None,
                "recovery_baseline": None,
            },
            "timestamps": {"created_at": utc_now()},
            "errors": [],
        }
        if kind == "report_repair" and isinstance(parent_hop, Mapping):
            hop["repair_generation"] = int(
                parent_hop.get("repair_generation") or 0
            )
            hop["repair_lineage_root_hop_id"] = int(
                parent_hop.get("repair_lineage_root_hop_id")
                or parent_hop.get("hop_id")
                or hop_id
            )
            hop["consumed_recovery_ids"] = list(
                parent_hop.get("consumed_recovery_ids") or ()
            )
            if parent_hop.get("repair_recovery_id"):
                hop["repair_recovery_id"] = str(
                    parent_hop["repair_recovery_id"]
                )
        state.setdefault("hops", []).append(hop)
        budget = self._execution_budget(state)
        if hop_id not in budget["charged_hop_ids"]:
            budget["charged_hop_ids"].append(hop_id)
        state["active_hop_id"] = hop_id
        state["active_role"] = target_role
        state["active_action"] = "queued"
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(target_role)
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        state["pause_reason"] = None
        role_record["status"] = "pending"
        return hop

    def _pre_send_request_identity_is_safe(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
    ) -> bool:
        if str(hop.get("state") or "") != "pre_send":
            return True
        if hop.get("receipt") is not None:
            self._block(
                state,
                "pre_send hop already contains accepted receipt evidence; exact request recovery is required before any send",
                code="pre_send_request_recovery_required",
                retryable=False,
            )
            return False
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        if not ledger_path or not request_id or not Path(ledger_path).exists():
            return True
        try:
            record = RequestLedger(ledger_path).get(request_id)
        except Exception as exc:
            self._block(
                state,
                f"pre_send durable request identity cannot be reconciled: {sanitize_exception(exc)}",
                code="pre_send_request_recovery_required",
                retryable=False,
            )
            return False
        if record is None:
            return True
        if hop.get("rebuild_prompt_after_relocation") is True:
            validation_hop = dict(hop)
            validation_hop["state"] = "sending"
            if self._is_pristine_preboundary_sending_record(
                state, validation_hop, record
            ):
                return True
            intent = hop.get("prompt_rebind_intent")
            if isinstance(intent, Mapping):
                intended_prompt = str(intent.get("prompt") or "")
                intended_sha = str(intent.get("prompt_sha256") or "")
                if intended_prompt and intended_sha == _sha(intended_prompt):
                    intended_hop = dict(validation_hop)
                    intended_hop["prompt"] = intended_prompt
                    intended_hop["prompt_sha256"] = intended_sha
                    if self._is_pristine_preboundary_sending_record(
                        state, intended_hop, record
                    ):
                        return True
            self._block(
                state,
                "relocated pre_send request is no longer provably pristine; refusing to rebuild or send",
                code="pre_send_request_recovery_required",
                retryable=False,
            )
            return False
        self._block(
            state,
            f"pre_send hop already has durable request identity in status {record.status.value}; exact request recovery is required before any send",
            code="pre_send_request_recovery_required",
            retryable=False,
        )
        return False

    def _normalize_legacy_pre_send_report_mode(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
    ) -> bool:
        if is_independent_task(state) or str(hop.get("state") or "") != "pre_send":
            return False
        if _report_mode(state) != "inline":
            return False
        options = state.get("options")
        if not isinstance(options, dict):
            raise ValueError("task options must be mutable before report-mode normalization")
        options["report_mode"] = "file"
        return True

    async def _acquire_existing_role(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
    ) -> AcquiredRole | None:
        existing = await actions.locate_owned(state, role)
        if existing is not None:
            await self._dismiss_known_rate_limit_on_existing(existing)
        elif self._rate_limit_gate_active(state=state):
            state["active_action"] = "rate_limit_cooldown"
            return None
        try:
            acquired = await actions.acquire(state, role, clean_for_send=True)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            return acquired
        except RoleOwnershipError as exc:
            if _role_ownership_block_code(exc) != "role_offline":
                raise
            hop, expected_conversation, _, _ = self._active_recovery_context(state, role)
            if expected_conversation is None:
                role_record = state["roles"][role]
                ledger = RequestLedger(str(hop["ledger_path"]))
                durable = ledger.peek(str(hop["request_id"]))
                pristine = (
                    str(hop.get("state") or "") == "pre_send"
                    or self._is_pristine_preboundary_sending_record(state, hop, durable)
                )
                owns_attachments = role_record.get("attachments_uploaded_generation") is not None and (
                    role_record.get("attachments_uploaded_generation") == role_record.get("conversation_generation")
                )
                if pristine and not owns_attachments and hop.get("message_identity") is None:
                    if self._rate_limit_gate_active(state=state):
                        state["active_action"] = "rate_limit_cooldown"
                        return None
                    acquired = await actions.fresh_chat(
                        state,
                        role,
                        url=(
                            "https://chatgpt.com/?temporary-chat=true"
                            if state.get("conversation_mode") == "temporary"
                            else "https://chatgpt.com/"
                        ),
                        clean_for_send=True,
                    )
                    self._record_acquired(state, role, acquired)
                    hop["conversation_url"] = acquired.url
                    return acquired
                self._wait_queue_error(
                    state,
                    "Owned role tab is temporarily unavailable; waiting to reconnect on the same team endpoint.",
                    code="role_reconnect",
                )
                return None
            if self._rate_limit_gate_active(state=state):
                state["active_action"] = "rate_limit_cooldown"
                return None
            acquired = await self._recover_active_role(state, role, actions)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            return acquired
        except UnsafePageStateError as exc:
            if str(exc).strip().casefold() != "page is already responding":
                raise
            # An exact owned page that is already responding is live work, not an
            # unsafe pre-send state. Do not wait for it to become clean and then
            # send another prompt; hand the condition to the worker so it can
            # observe the in-flight response on this exact conversation.
            state["active_action"] = "observe_existing_response"
            raise


    async def _run_automated_send(self, operation: Callable[[], Any]) -> Any:
        async with self._send_gate_lock:
            last = self._last_automated_send_at
            if last is not None:
                remaining = _AUTOMATED_SEND_SPACING_SECONDS - (time.monotonic() - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            try:
                return await operation()
            finally:
                self._last_automated_send_at = time.monotonic()

    def _bootstrap_for_state(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        snapshot = state.get("bootstrap")
        if not isinstance(snapshot, Mapping):
            return None
        bootstrap_id = str(snapshot.get("bootstrap_id") or "").strip()
        if not bootstrap_id:
            return None
        catalog = BootstrapCatalog(self.config.repository_root)
        current = catalog.get(bootstrap_id)
        if current is None or current.get("enabled") is not True:
            return None
        return current

    @staticmethod
    def _bootstrap_donor_candidates(
        state: Mapping[str, Any], bootstrap: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in [
            *(bootstrap.get("donors") or []),
            *(state.get("bootstrap_task_donors") or []),
        ]:
            if not isinstance(raw, Mapping):
                continue
            try:
                donor = normalize_bootstrap_donor(raw)
            except ValueError:
                continue
            identity = (donor["conversation_id"], donor["assistant_message_id"])
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(donor)
        return candidates

    @staticmethod
    def _remove_task_donor(state: dict[str, Any], donor: Mapping[str, Any]) -> None:
        existing = state.get("bootstrap_task_donors")
        if not isinstance(existing, list):
            return
        state["bootstrap_task_donors"] = [item for item in existing if item != donor]

    def _mark_bootstrap_unavailable(
        self, state: dict[str, Any], bootstrap: Mapping[str, Any]
    ) -> None:
        self._block(
            state,
            (
                f"Bootstrap {bootstrap.get('bootstrap_id')!r} has no usable donors and no "
                "prewarm_prompt; select another bootstrap or replace/add a source before Send."
            ),
            code="bootstrap_unavailable",
            retryable=False,
        )

    async def _materialize_bootstrap_source_donor(
        self,
        state: dict[str, Any],
        bootstrap: Mapping[str, Any],
        actions: CDPATabActions,
    ) -> tuple[dict[str, Any], bool]:
        source_id = str(bootstrap.get("source_conversation_id") or "").strip()
        if not source_id or state.get("bootstrap_source_exhausted") is True:
            return dict(bootstrap), False
        # A source conversation ID alone is no longer actively fetched. Bootstrap
        # materialization must come from an already-recorded donor or a normal
        # browser-owned/local transcript path; otherwise fail closed below.
        state["bootstrap_source_requires_local_materialization"] = source_id
        return dict(bootstrap), False

    async def _regenerate_bootstrap_donor(
        self,
        state: dict[str, Any],
        bootstrap: Mapping[str, Any],
        actions: CDPATabActions,
    ) -> dict[str, Any] | None:
        async with self._bootstrap_prepare_lock:
            current = self._bootstrap_for_state(state)
            if current is None:
                self._mark_bootstrap_unavailable(state, bootstrap)
                return None
            if current.get("donors"):
                return current
            prewarm_prompt = str(current.get("prewarm_prompt") or "").strip()
            if not prewarm_prompt:
                return None
            generation = int(state.get("bootstrap_prewarm_generation") or 0) + 1
            bootstrap_id = str(current["bootstrap_id"])
            request_id = "bootstrap-prewarm-" + _sha(
                f"{bootstrap_id}:{state.get('task_id')}:{generation}"
            )[:24]
            ledger_path = (
                self.config.repository_root
                / ".runtime"
                / "cdpa-bootstrap-prewarm-ledger.json"
            )
            ledger = RequestLedger(ledger_path)
            existing = ledger.get(request_id) if ledger.path.exists() else None

            async def acquire_prewarm_role() -> AcquiredRole:
                current_acquired = await actions.acquire_global_role(
                    "BOOTSTRAP", allow_create=not self._rate_limit_gate_active(state=state)
                )
                if existing is None:
                    await current_acquired.client.new_chat(
                        discard_draft=False,
                        discard_attachments=False,
                        stop_first=False,
                        timeout_ms=round(self.config.workspace_timeout_seconds * 1000),
                    )
                    ownership = await current_acquired.client.assert_ownership()
                    if (
                        str(getattr(ownership, "page_role", "") or "") != "BOOTSTRAP"
                        or getattr(ownership, "page_task_id", None)
                        or getattr(ownership, "page_team", None)
                    ):
                        raise RoleOwnershipError(
                            "bootstrap prewarm tab must remain global and task-neutral"
                        )
                return current_acquired

            try:
                acquired = await acquire_prewarm_role()
            except RoleOwnershipError:
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return None
                raise
            await self._dismiss_known_rate_limit_on_existing(acquired)
            block = DurableSendBlock(
                prewarm_prompt,
                ledger_path=ledger_path,
                source_context={
                    "kind": "bootstrap_prewarm",
                    "bootstrap_id": bootstrap_id,
                    "origin_task_id": state.get("task_id"),
                    "generation": generation,
                },
                role_prompt_hash=_sha("bootstrap-prewarm"),
                request_id=request_id,
                wait_for_response=True,
                response_timeout_ms=round(self.config.response_timeout_seconds * 1000),
                block_id="bootstrap_prewarm",
            )

            async def perform_send() -> Any:
                return await block.run(WorkflowContext(acquired.client))

            try:
                output = await self._run_automated_send(perform_send)
            except DurableRequestError as exc:
                self._block(
                    state,
                    sanitize_text(exc, max_chars=1000),
                    code="bootstrap_prewarm_recovery_required",
                    retryable=False,
                )
                return None
            if not isinstance(output, Mapping):
                raise RuntimeError("bootstrap prewarm durable send returned invalid output")
            receipt = output.get("receipt")
            response = output.get("response")
            if not isinstance(receipt, Mapping) or not isinstance(response, Mapping):
                raise RuntimeError("bootstrap prewarm completed without receipt and response")
            conversation_id = str(receipt.get("conversation_id") or "").strip()
            if not conversation_id:
                ownership = await acquired.client.assert_ownership()
                identity = conversation_identity(getattr(ownership, "url", None))
                conversation_id = (
                    identity.rsplit("/", 1)[-1] if identity is not None else ""
                )
            donor = normalize_bootstrap_donor(
                {
                    "conversation_id": conversation_id,
                    "assistant_message_id": str(response.get("message_id") or ""),
                }
            )
            updated = BootstrapCatalog(self.config.repository_root).add_donor(
                bootstrap_id, donor
            )
            state["bootstrap"] = updated
            state["bootstrap_prewarm_generation"] = generation
            state["bootstrap_prewarm_donor"] = donor
            state["active_action"] = "bootstrap_retry"
            return updated

    def _foreign_durable_conversation_owner(
        self,
        state: Mapping[str, Any],
        role: str,
        candidate_url: str,
    ) -> tuple[str, str, str] | None:
        candidate = _recoverable_conversation_identity(candidate_url)
        if candidate is None:
            return None
        target_task = str(state.get("task_id") or "")
        target_team = str(state.get("team") or "")
        target_physical = str(state["roles"][role].get("physical_role") or "")
        for task in self.store.discover():
            if str(task.get("status") or "").upper() in {"DONE", "STOPPED"}:
                continue
            owner_task = str(task.get("task_id") or "")
            owner_team = str(task.get("team") or "")
            roles = task.get("roles")
            if not isinstance(roles, Mapping):
                continue
            for owner_record in roles.values():
                if not isinstance(owner_record, Mapping):
                    continue
                if not str(owner_record.get("page_id") or "").strip():
                    continue
                owner_url = str(owner_record.get("page_url") or "").strip()
                if _recoverable_conversation_identity(owner_url) != candidate:
                    continue
                owner_physical = str(owner_record.get("physical_role") or "")
                if (owner_task, owner_physical, owner_team) == (
                    target_task,
                    target_physical,
                    target_team,
                ):
                    continue
                return owner_task, owner_physical, owner_team
        return None

    async def _reject_foreign_durable_branch_owner(
        self,
        state: Mapping[str, Any],
        role: str,
        acquired: AcquiredRole,
    ) -> None:
        candidate_url = (
            f"https://chatgpt.com/c/{acquired.conversation_id}"
            if acquired.conversation_id
            else acquired.url
        )
        owner = self._foreign_durable_conversation_owner(state, role, candidate_url)
        if owner is None:
            return
        page = getattr(acquired.client, "page", None)
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass
        owner_task, owner_physical, owner_team = owner
        raise BranchBootstrapError(
            "branch target conversation is already writable by foreign durable owner "
            f"{owner_task}/{owner_physical}/{owner_team}"
        )

    async def _acquire_workflow_role(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
    ) -> AcquiredRole | None:
        """Acquire one workflow role without bootstrap branching.

        New workflow roles always start from Fresh context. Existing durable
        role pages/conversations are reused exactly; there is no cross-endpoint
        donor lookup or bootstrap URL dependency.
        """
        role_record = state["roles"][role]
        has_durable_context = bool(
            role_record.get("page_id")
            or role_record.get("page_url")
            or int(role_record.get("conversation_generation") or 0) > 0
        )
        if has_durable_context:
            return await self._acquire_existing_role(state, role, actions)

        owned = await actions.locate_owned(state, role)
        if owned is not None:
            await self._dismiss_known_rate_limit_on_existing(owned)
            if not is_independent_task(state):
                role_record["context_source"] = "fresh"
            role_record.pop("bootstrap_source_donor", None)
            role_record.pop("bootstrap_context_recovery", None)
            return owned
        if self._rate_limit_gate_active(state=state):
            state["active_action"] = "rate_limit_cooldown"
            return None

        acquired = await actions.fresh_chat(
            state,
            role,
            url="https://chatgpt.com/",
            clean_for_send=True,
        )
        if not is_independent_task(state):
            role_record["context_source"] = "fresh"
        role_record.pop("bootstrap_source_donor", None)
        role_record.pop("bootstrap_context_recovery", None)
        return acquired

    def _governing_contract_for_hop(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        workflow_definition: Mapping[str, Any],
    ) -> str:
        try:
            semantic_payload, semantic_sha256, revision, goal_sha256 = (
                governing_semantic_for_hop(state, int(hop["hop_id"]))
            )
            continuation = workflow_definition.get("continuation_contract")
            if not isinstance(continuation, str) or not continuation.strip():
                raise ValueError(
                    "governing_contract_unavailable: role continuation contract is missing"
                )
            if len(continuation) > 2048:
                raise ValueError(
                    "governing_contract_unavailable: role continuation contract exceeds 2048 code points"
                )
            manifest_raw = str(state.get("manifest_path") or "")
            manifest = Path(manifest_raw).expanduser().resolve()
            plans_root = self.config.plans_root.expanduser().resolve()
            if str(manifest) != manifest_raw:
                raise ValueError(
                    "governing_contract_unavailable: manifest_path is not canonical"
                )
            try:
                manifest.relative_to(plans_root)
            except ValueError as exc:
                raise ValueError(
                    "governing_contract_unavailable: manifest_path is outside plans root"
                ) from exc
            if not manifest.is_file():
                raise ValueError(
                    "governing_contract_unavailable: task manifest is unavailable"
                )
            persisted_manifest = self.store.load(manifest)
            if (
                str(persisted_manifest.get("task_id") or "")
                != str(state.get("task_id") or "")
                or str(persisted_manifest.get("manifest_path") or "")
                != manifest_raw
            ):
                raise ValueError(
                    "governing_contract_unavailable: manifest identity does not match the executing task"
                )
            continuation_sha256 = hashlib.sha256(
                continuation.encode("utf-8")
            ).hexdigest()
            facts = json.dumps(
                {
                    "task_id": str(state["task_id"]),
                    "hop_id": int(hop["hop_id"]),
                    "manifest_path": manifest_raw,
                    "repository": str(state["repository"]),
                    "workflow_mode": str(state.get("workflow_mode") or "team"),
                    "home_endpoint": str(state.get("browser_endpoint_id") or ""),
                    "required_endpoint": (
                        str(state.get("required_browser_endpoint_id"))
                        if state.get("required_browser_endpoint_id") is not None
                        else None
                    ),
                    "conversation_mode": str(
                        state.get("conversation_mode") or "persistent"
                    ),
                    "context_policy": str(state.get("context_policy") or "fresh"),
                    "goal_revision": revision,
                    "effective_goal_sha256": goal_sha256,
                    "semantic_sha256": semantic_sha256,
                    "target_role": str(hop["target_role"]),
                    "continuation_sha256": continuation_sha256,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            block = self.prompts.governing_block(
                semantic_payload, continuation, facts
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            if "governing_contract_unavailable" in str(exc):
                raise
            raise ValueError(
                f"governing_contract_unavailable: {exc}"
            ) from exc
        if isinstance(hop, dict):
            hop["governing_contract_ref"] = {
                "semantic_sha256": semantic_sha256,
                "continuation_sha256": continuation_sha256,
                "manifest_path": manifest_raw,
                "goal_revision": revision,
                "resolved": True,
            }
        return block

    @staticmethod
    def _route_repair_stale_error_send_ready(snapshot: Any) -> bool:
        """Allow a route-only repair after a failed generation when the composer is clean."""
        page_state = str(
            getattr(getattr(snapshot, "state", None), "value", getattr(snapshot, "state", "")) or ""
        ).lower()
        return bool(
            page_state == "error"
            and getattr(snapshot, "composer_present", False)
            and getattr(snapshot, "composer_editable", False)
            and not str(getattr(snapshot, "composer_text", "") or "").strip()
            and not tuple(getattr(snapshot, "attachment_markers", ()) or ())
            and not getattr(snapshot, "stop_visible", False)
            and not getattr(snapshot, "manual_input_pending", False)
            and not getattr(snapshot, "choice_prompt_pending", False)
            and not tuple(getattr(snapshot, "blocking_dialogs", ()) or ())
        )

    def _handle_pre_send_fresh_chat_timeout(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        exc: Exception,
    ) -> None:
        """Common handler for fresh_chat TimeoutError in _pre_send branches.

        Sets preboundary_recovery and blocks the task.  The caller must return
        immediately after this call; the page identity was not confirmed.
        """
        hop["preboundary_recovery"] = {
            "phase": "recovery_required",
            "stage": "acquisition",
            "request_id": hop.get("request_id"),
            "attempts": 0,
            "last_failure": sanitize_exception(exc),
            "last_failure_at": utc_now(),
        }
        self._block(
            state,
            "Pre-Send fresh allocation timed out without a returned binding; "
            "exact owned-page reconciliation is required before another acquisition or Send.",
            code="preboundary_acquisition_recovery_required",
            retryable=False,
        )

    async def _pre_send(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        if not self._pre_send_request_identity_is_safe(state, hop):
            return
        retry_at = parse_time((hop.get("wait") or {}).get("pre_send_retry_at"))
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            state["status"] = "RUNNING"
            state["active_action"] = "queued"
            return
        if retry_at is not None:
            hop.setdefault("wait", {}).pop("pre_send_retry_at", None)
        if (hop.get("preboundary_recovery") or {}).get("phase") == "recovery_required":
            # Acquisition did not return a binding; never implicitly allocate again.
            return
        role = str(hop["target_role"])
        hop_kind = str(hop.get("kind") or "")
        prompt_kind = (
            str(hop.get("recovered_parent_kind") or "")
            if hop_kind == "execution_recovery"
            and str(hop.get("recovered_parent_kind") or "") in {"route_repair", "report_repair"}
            else hop_kind
        )
        governing_contract = None
        governing_workflow_definitions = None
        substantive_team_hop = (
            not is_independent_task(state)
            and str(state.get("workflow_mode") or "team").strip().lower() == "team"
            and prompt_kind not in {"route_repair", "report_repair"}
        )
        if substantive_team_hop:
            try:
                governing_workflow_definitions = task_workflow_definitions(
                    state, self.config
                )
                governing_contract = self._governing_contract_for_hop(
                    state,
                    hop,
                    governing_workflow_definitions[role],
                )
            except (KeyError, TypeError, ValueError, OSError) as exc:
                self._block(
                    state,
                    str(exc),
                    code="governing_contract_unavailable",
                    retryable=False,
                )
                return
        await self._admission_preflight(state, actions)
        self._normalize_legacy_pre_send_report_mode(state, hop)
        if self._attachment_files_for_generation(state, role) is None:
            return
        route_repair_owned = None
        route_repair_stale_error_ready = False
        if str(hop.get("kind") or "") == "route_repair":
            route_repair_owned = await actions.locate_owned(state, role)
            if route_repair_owned is not None:
                await self._dismiss_known_rate_limit_on_existing(route_repair_owned)
                route_repair_snapshot = await route_repair_owned.client.assert_ownership()
                route_repair_stale_error_ready = self._route_repair_stale_error_send_ready(
                    route_repair_snapshot
                )
        independent = state.get("independent") if is_independent_task(state) else None
        solo_existing_url = (
            not isinstance(independent, Mapping)
            and str(state.get("workflow_mode") or "team").strip().lower() == "solo"
            and str(hop.get("kind") or "") == "task"
            and bool(str(hop.get("conversation_url") or "").strip())
            and not bool(state["roles"][role].get("page_id"))
        )
        if route_repair_stale_error_ready:
            acquired = route_repair_owned
        elif solo_existing_url:
            candidate_url = str(hop["conversation_url"])
            owner = self._foreign_durable_conversation_owner(
                state,
                role,
                candidate_url,
            )
            if owner is not None:
                owner_task, owner_physical, owner_team = owner
                self._block(
                    state,
                    "Solo conversation is already owned by "
                    f"{owner_task}/{owner_physical}/{owner_team}",
                    code="conversation_owned_by_other_task",
                    retryable=False,
                )
                return
            acquired = await actions.adopt_conversation(
                state,
                role,
                url=candidate_url,
            )
            hop["conversation_url"] = acquired.url
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif isinstance(independent, dict) and independent.get("temporary_chat") is True:
            if hop.get("kind") == "independent_cycle":
                acquired = await actions.locate_owned(state, role)
                if acquired is None:
                    raise RoleOwnershipError(
                        "temporary independent continuation tab is offline",
                        code="role_offline",
                    )
            else:
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return
                try:
                    acquired = await actions.fresh_chat(
                        state,
                        role,
                        url="https://chatgpt.com/?temporary-chat=true",
                        clean_for_send=True,
                    )
                except (TimeoutError, PlaywrightTimeoutError) as exc:
                    if is_cdp_disconnect(exc):
                        raise
                    if not self._pre_send_request_identity_is_safe(state, hop):
                        return
                    self._handle_pre_send_fresh_chat_timeout(state, hop, exc)
                    return
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif not isinstance(independent, Mapping) and state.get("conversation_mode") == "temporary":
            acquired = await actions.locate_owned(state, role)
            token = state["roles"][role].get("temporary_document_id")
            if acquired is not None and token and await self._temporary_document_token(acquired.client) != token:
                acquired = None
            if acquired is None:
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return
                try:
                    acquired = await actions.fresh_chat(
                        state,
                        role,
                        url="https://chatgpt.com/?temporary-chat=true",
                        clean_for_send=True,
                    )
                except (TimeoutError, PlaywrightTimeoutError) as exc:
                    if is_cdp_disconnect(exc):
                        raise
                    if not self._pre_send_request_identity_is_safe(state, hop):
                        return
                    self._handle_pre_send_fresh_chat_timeout(state, hop, exc)
                    return
            else:
                acquired = await actions.acquire(state, role, clean_for_send=True)
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif (
            not isinstance(independent, Mapping)
            and str(state.get("workflow_mode") or "team").lower() == "solo"
            and hop.get("kind") == "solo_resume"
        ):
            saved_url = _preferred_saved_url(
                hop.get("conversation_url"), state["roles"][role].get("page_url")
            )
            if not saved_url:
                self._block(
                    state,
                    "Solo continuation has no saved conversation URL; refusing Fresh Chat",
                    code="conversation_identity_unavailable",
                    retryable=False,
                )
                return
            hop["conversation_url"] = saved_url
            if state["roles"][role].get("page_id"):
                acquired = await actions.reopen(state, role, require_clean_ready=False)
            else:
                acquired = await actions.adopt_conversation(state, role, url=saved_url)
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif not isinstance(independent, Mapping) and (state.get("context_policy") == "fresh" or hop.get("kind") == "execution_recovery"):
            acquired = await actions.locate_owned(state, role)
            if acquired is None:
                if self._rate_limit_gate_active(state=state):
                    state["active_action"] = "rate_limit_cooldown"
                    return
                try:
                    acquired = await actions.fresh_chat(
                        state,
                        role,
                        url="https://chatgpt.com/",
                        clean_for_send=True,
                    )
                except (TimeoutError, PlaywrightTimeoutError) as exc:
                    if is_cdp_disconnect(exc):
                        raise
                    if not self._pre_send_request_identity_is_safe(state, hop):
                        return
                    self._handle_pre_send_fresh_chat_timeout(state, hop, exc)
                    return
                if state.get("context_policy") == "fresh":
                    state["roles"][role]["context_source"] = "fresh"
            elif (
                hop.get("kind") == "execution_recovery"
                or hop.get("rebuild_prompt_after_relocation") is True
            ):
                pass
            else:
                try:
                    acquired = await actions.acquire(state, role, clean_for_send=True)
                except Exception as exc:
                    if (
                        not isinstance(exc, (TimeoutError, PlaywrightTimeoutError))
                        and is_transient_page_lifecycle_error(exc)
                    ):
                        hop.setdefault("wait", {})["pre_send_retry_at"] = (
                            datetime.now(timezone.utc) + timedelta(seconds=5)
                        ).isoformat()
                        hop["wait"]["pre_send_retry_error"] = sanitize_exception(exc)
                        state["status"] = "RUNNING"
                        state["active_action"] = "queued"
                        return
                    if not isinstance(exc, (TimeoutError, PlaywrightTimeoutError)):
                        raise
                    if is_cdp_disconnect(exc):
                        raise
                    if not self._pre_send_request_identity_is_safe(state, hop):
                        return
                    hop["preboundary_recovery"] = {
                        "phase": "recovery_required",
                        "stage": "acquisition",
                        "request_id": hop.get("request_id"),
                        "attempts": 0,
                        "last_failure": sanitize_exception(exc),
                        "last_failure_at": utc_now(),
                    }
                    self._block(
                        state,
                        "Pre-Send clean-ready acquisition timed out without a returned binding; exact owned-page reconciliation is required before another acquisition or Send.",
                        code="preboundary_acquisition_recovery_required",
                        retryable=False,
                    )
                    return
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif (
            isinstance(independent, dict)
            and independent.get("new_chat_next_job") is True
            and independent.get("new_chat_deferred_task_id")
            != str(state.get("task_id") or "")
        ):
            cooldown = self._rate_limit_cooldown
            if (
                isinstance(cooldown, Mapping)
                and cooldown.get("state") == "released"
                and cooldown.get("post_release_acquisition") == "in_progress"
            ):
                state["active_action"] = "rate_limit_cooldown"
                return
            current = await actions.locate_owned(state, role)
            if current is None and self._rate_limit_gate_active(state=state):
                state["active_action"] = "rate_limit_cooldown"
                return
            acquired = await actions.new_chat(state, role)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            independent["new_chat_next_job"] = False
            independent["new_chat_deferred_task_id"] = None
        else:
            try:
                acquired = await self._acquire_workflow_role(state, role, actions)
            except Exception as exc:
                if (
                    not isinstance(exc, (TimeoutError, PlaywrightTimeoutError))
                    and is_transient_page_lifecycle_error(exc)
                ):
                    hop.setdefault("wait", {})["pre_send_retry_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=5)
                    ).isoformat()
                    hop["wait"]["pre_send_retry_error"] = sanitize_exception(exc)
                    state["status"] = "RUNNING"
                    state["active_action"] = "queued"
                    return
                if not isinstance(exc, (TimeoutError, PlaywrightTimeoutError)):
                    raise
                if is_cdp_disconnect(exc):
                    raise
                if not self._pre_send_request_identity_is_safe(state, hop):
                    return
                # Clean-ready can fail after allocation but before returning its
                # binding. There is no durable acquisition token to resume safely,
                # and usually no prepared request yet. Do not manufacture one or
                # enter the pristine-SENDING mutation authorization state machine.
                hop["preboundary_recovery"] = {
                    "phase": "recovery_required",
                    "stage": "acquisition",
                    "request_id": hop.get("request_id"),
                    "attempts": 0,
                    "last_failure": sanitize_exception(exc),
                    "last_failure_at": utc_now(),
                }
                self._block(
                    state,
                    "Pre-Send clean-ready acquisition timed out without a returned binding; exact owned-page reconciliation is required before another acquisition or Send.",
                    code="preboundary_acquisition_recovery_required",
                    retryable=False,
                )
                return
            if acquired is None:
                return
        self._record_acquired(state, role, acquired)
        generation = int(state["roles"][role].get("conversation_generation") or 0)
        if hop.get("conversation_generation") != generation:
            hop["conversation_generation"] = generation
            hop["generation_started_here"] = bool(acquired.new_chat)
        hop["browser_endpoint_id"] = self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state)
        if state["roles"][role].get("branch_conversation_id"):
            hop["branch_conversation_id"] = state["roles"][role]["branch_conversation_id"]
        if state.get("conversation_mode") == "temporary":
            state["roles"][role]["temporary_document_id"] = await self._temporary_document_token(acquired.client, create=True)
        if route_repair_stale_error_ready:
            preparation_reason = "ready"
        else:
            preparation = await self.role_controller.run(
                self, state, hop, acquired, actions, phase="pre_send", wait_ms=0,
            )
            preparation_reason = preparation.reason
        if solo_existing_url and preparation_reason == "response_in_progress":
            now = datetime.now(timezone.utc)
            start_wait_budget(
                hop["wait"],
                timeout_seconds=self.config.response_timeout_seconds,
                now=now,
            )
            hop["state"] = "waiting"
            hop["timestamps"]["waiting_at"] = now.isoformat()
            hop["observed_existing_response"] = True
            state["started_at"] = state.get("started_at") or now.isoformat()
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(role)
            state["active_action"] = "wait_response"
            return
        if solo_existing_url and preparation_reason == "ready":
            acquired = await actions.acquire(state, role, clean_for_send=True)
            self._record_acquired(state, role, acquired)
        if preparation_reason != "ready":
            return
        role_record = state["roles"][role]
        generation = int(role_record.get("conversation_generation") or 0)
        if isinstance(independent, Mapping):
            active_event = independent.get("active_event")
            if not isinstance(active_event, Mapping):
                raise ValueError("independent job has no active trigger event")
            built = self.prompts.build_independent(
                agent_name=str(
                    independent.get("display_name")
                    or independent["agent_name"]
                ),
                system_prompt=str(independent["system_prompt"]),
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                workspace=str(state["repository"]),
                event=active_event,
                cycle=int(independent.get("cycle") or 1),
                max_cycles=int(independent.get("max_cycles") or 0),
                constructor_sent_generation=role_record.get(
                    "constructor_sent_generation"
                ),
                conversation_generation=generation,
            )
            prompt = built.text
            if hop.get("recovery_of_request_id"):
                recovery_handoff = str(hop.get("handoff") or "").strip()
                if recovery_handoff:
                    prompt += "\n\n" + recovery_handoff
            hop["prompt"] = prompt
            hop["prompt_sha256"] = _sha(prompt)
            hop["expected_report_path"] = None
            hop["state"] = "sending"
            hop["timestamps"]["sending_at"] = utc_now()
            role_record["turn"] = max(
                int(role_record.get("turn") or 0), int(hop["turn"])
            )
            if built.constructor_included:
                role_record["constructor_sent_generation"] = generation
            state["started_at"] = state.get("started_at") or utc_now()
            state["status"] = "RUNNING"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "send"
            return
        if str(state.get("workflow_mode") or "team").strip().lower() == "solo":
            # Reusing a conversation is not itself a continuation: the first
            # task hop still owns and must deliver the current Solo Goal.
            continuation = str(hop.get("kind") or "") != "task"
            repair_error = None
            repair_candidate = None
            if str(hop.get("kind") or "") == "route_repair":
                repair_error = str(hop.get("validation_error") or "").strip() or None
                parent_hop_id = hop.get("parent_hop_id")
                parent_hop = next(
                    (
                        item
                        for item in state.get("hops") or ()
                        if isinstance(item, Mapping)
                        and item.get("hop_id") == parent_hop_id
                    ),
                    None,
                )
                if isinstance(parent_hop, Mapping):
                    repair_candidate = sanitize_text(
                        parent_hop.get("response") or "", max_chars=4000
                    )
            built = self.prompts.build_solo(
                task_name=str(state.get("task_title") or state["task_text"]).splitlines()[0],
                logical_role=role,
                goal=task_goal_for_hop(state, int(hop["hop_id"])),
                workspace=str(state["repository"]),
                continuation=continuation,
                repair_error=repair_error,
                repair_candidate=repair_candidate,
            )
            prompt = built.text
            hop["prompt"] = prompt
            hop["prompt_sha256"] = _sha(prompt)
            hop["expected_report_path"] = None
            hop["state"] = "sending"
            hop["timestamps"]["sending_at"] = utc_now()
            role_record["turn"] = max(
                int(role_record.get("turn") or 0), int(hop["turn"])
            )
            state["started_at"] = state.get("started_at") or utc_now()
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(role)
            state["active_action"] = "send"
            return
        report_repository, report_plans_root = _workflow_report_roots(self.config, state)
        expected = (
            str(hop.get("expected_report_path") or "").strip()
            if prompt_kind in {"route_repair", "report_repair"}
            else ""
        ) or expected_report_relative(
            plans_root=report_plans_root,
            repository_root=report_repository,
            team=str(state["team"]),
            physical_role=str(hop["physical_role"]),
            turn=int(hop["turn"]),
            task_id=str(state["task_id"]),
        )
        source_logical = str(hop.get("source_role") or "").upper()
        source_physical = (
            str(state["roles"][source_logical]["physical_role"])
            if source_logical in state["roles"]
            else None
        )
        workflow_definitions = (
            governing_workflow_definitions
            if governing_workflow_definitions is not None
            else task_workflow_definitions(state, self.config)
        )
        workflow_definition = workflow_definitions[role]
        stale_recovery_rebind = (
            hop.get("kind") == "execution_recovery"
            and hop.get("rebuild_prompt_after_relocation") is True
            and len(str(hop.get("prompt") or "")) > _EXECUTION_RECOVERY_PROMPT_MAX_CHARS
        )
        if stale_recovery_rebind:
            legacy_handoff = str(hop.get("handoff") or "")
            compact_handoff = self._recompact_stale_execution_recovery_handoff(
                state, hop
            )
            if compact_handoff != legacy_handoff:
                hop_id = hop["hop_id"]
                request_id = str(hop.get("request_id") or "")

                def persist_compact_handoff(
                    current_state: dict[str, Any],
                ) -> dict[str, Any]:
                    current_hop = next(
                        (
                            item
                            for item in current_state.get("hops") or []
                            if isinstance(item, dict)
                            and item.get("hop_id") == hop_id
                        ),
                        None,
                    )
                    if (
                        current_hop is None
                        or str(current_hop.get("request_id") or "") != request_id
                        or str(current_hop.get("handoff") or "") != legacy_handoff
                    ):
                        raise DurableRequestError(
                            "execution recovery child changed before handoff recompact"
                        )
                    validated = self._recompact_stale_execution_recovery_handoff(
                        current_state, current_hop
                    )
                    if validated != compact_handoff:
                        raise DurableRequestError(
                            "execution recovery handoff changed during recompact"
                        )
                    current_hop["handoff"] = compact_handoff
                    return current_state

                self.store.update(
                    str(state["manifest_path"]), persist_compact_handoff
                )
                hop["handoff"] = compact_handoff
        if (
            hop.get("rebuild_prompt_after_relocation") is True
            and not hop.get("context_recovery_snapshot")
            and not stale_recovery_rebind
        ):
            hop["context_recovery_snapshot"] = self._build_execution_recovery_snapshot(
                state, hop, reason="pristine pre-boundary request relocated to a new conversation",
            )
        bootstrap_inherited = (
            bool(workflow_definition["is_system"])
            and role_record.get("context_source")
            in {"bootstrap_donor", "bootstrap_native", "bootstrap_ui"}
            and generation == 1
        )
        allowed_routes = tuple(workflow_definitions) + ("PAUSE", "DONE")
        if prompt_kind in {"route_repair", "report_repair"} and (
            acquired.new_chat or (acquired.created and role_record.get("last_endpoint_id"))
        ):
            if prompt_kind == "route_repair":
                hop.pop("context_recovery_snapshot", None)
            else:
                hop["context_recovery_snapshot"] = self._build_execution_recovery_snapshot(
                    state, hop, reason="repair continuation acquired a new browser conversation",
                )
        bounded_context = ""
        if hop.get("context_recovery_snapshot"):
            bounded_context = (
                "Inspect the existing workspace, reports, checkpoints and Git state. "
                "Continue only unfinished work; do not repeat completed irreversible actions.\n"
                "RECOVERY_SNAPSHOT\n"
                + json.dumps(hop["context_recovery_snapshot"], ensure_ascii=False, sort_keys=True)
            )
            recovery_metadata = role_record.get("bootstrap_context_recovery")
            if isinstance(recovery_metadata, dict):
                # Legacy persisted fresh fallbacks may still contain executable context.
                # Keep donor/endpoint audit evidence, never re-inject abandoned instructions.
                recovery_metadata.pop("prewarm_context", None)
        if prompt_kind == "route_repair":
            bounded_context = ""  # Format repair never reintroduces the original goal or handoff.
            repair_prompt = self.prompts.repair(
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                validation_error=str(hop["validation_error"]),
                expected_report_path=expected,
                report_mode=_report_mode(state),
                allowed_routes=allowed_routes,
            )
            prompt = (bounded_context + "\n\n" if bounded_context else "") + repair_prompt
            included = False
        elif prompt_kind == "report_repair":
            locked_route = str(hop.get("locked_route") or "").strip().upper()
            locked_handoff = str(hop.get("locked_handoff") or "").strip()
            if locked_handoff != expected:
                raise RouteContractError(
                    "report repair handoff no longer matches the expected role report"
                )
            repair_sections = [
                self.prompts.filesystem_scope(str(state["repository"]), expected),
                self.prompts.tool_authority(),
            ]
            if bounded_context:
                repair_sections.append(bounded_context)
            repair_sections.append(
                self.prompts.report_repair(
                    task_id=str(state["task_id"]),
                    team=str(state["team"]),
                    physical_role=str(hop["physical_role"]),
                    turn=int(hop["turn"]),
                    route=locked_route,
                    handoff=locked_handoff,
                    validation_error=str(hop["validation_error"]),
                )
            )
            prompt = "\n\n".join(repair_sections)
            included = False
        else:
            built = self.prompts.build(
                task_title=str(state.get("task_title") or state["task_text"]).splitlines()[0],
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                logical_role=role,
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                allowed_routes=allowed_routes,
                workspace=str(state["repository"]),
                source_physical_role=source_physical,
                handoff=str(hop["handoff"]),
                goal=task_goal_for_hop(state, int(hop["hop_id"])),
                expected_report_path=expected,
                constructor_sent_generation=role_record.get(
                    "constructor_sent_generation"
                ),
                conversation_generation=generation,
                report_mode=_report_mode(state),
                constructor_text=str(workflow_definition["system_prompt"]),
                is_system_role=bool(workflow_definition["is_system"]),
                bootstrap_inherited=bootstrap_inherited,
                bounded_context=bounded_context or None,
                governing_contract=governing_contract,
            )
            prompt = built.text
            included = built.constructor_included
        if (
            hop.get("kind") == "execution_recovery"
            and len(prompt) > _EXECUTION_RECOVERY_PROMPT_MAX_CHARS
        ):
            raise DurableRequestError(
                "execution recovery prompt exceeds the 8000-character safe composer bound"
            )
        if hop.get("rebuild_prompt_after_relocation") is True:
            ledger_path = str(hop.get("ledger_path") or "").strip()
            request_id = str(hop.get("request_id") or "").strip()
            if not ledger_path or not request_id:
                raise DurableRequestError("relocated pristine request has no durable identity")
            source_context = {
                "task_id": state["task_id"], "team": state["team"],
                "hop_id": hop["hop_id"], "manifest": state["manifest_path"],
            }
            role_prompt_hash = _sha(str(workflow_definition["system_prompt"]))
            intent = {
                "request_id": request_id,
                "role": str(hop["physical_role"]),
                "prompt": prompt,
                "prompt_sha256": _sha(prompt),
                "source_context": source_context,
                "role_prompt_hash": role_prompt_hash,
            }
            existing_intent = hop.get("prompt_rebind_intent")
            if existing_intent is not None and existing_intent != intent:
                raise DurableRequestError("relocated request rebind intent changed after persistence")
            if existing_intent is None:
                hop_id = hop["hop_id"]
                def persist_rebind_intent(current_state: dict[str, Any]) -> dict[str, Any]:
                    current_hop = next(
                        (item for item in current_state.get("hops") or [] if item.get("hop_id") == hop_id),
                        None,
                    )
                    if (
                        not isinstance(current_hop, dict)
                        or str(current_hop.get("request_id") or "") != request_id
                        or str(current_hop.get("state") or "") not in {"sending", "pre_send"}
                        or current_hop.get("receipt") is not None
                    ):
                        raise DurableRequestError("relocated request state changed before rebind intent persistence")
                    prior = current_hop.get("prompt_rebind_intent")
                    if prior is not None and prior != intent:
                        raise DurableRequestError("relocated request has conflicting persisted rebind intent")
                    current_hop["prompt_rebind_intent"] = intent
                    return current_state
                self.store.update(str(state["manifest_path"]), persist_rebind_intent)
                hop["prompt_rebind_intent"] = intent
            durable = RequestLedger(ledger_path)
            current = durable.peek(request_id)
            intended_hop = dict(hop)
            intended_hop.update(state="sending", prompt=prompt, prompt_sha256=intent["prompt_sha256"])
            if not self._is_pristine_preboundary_sending_record(state, intended_hop, current):
                if current is None or current.status not in {RequestStatus.NEW, RequestStatus.PROMPT_SET} or current.attempts != 0:
                    raise DurableRequestError("relocated request is no longer pristine")
                durable.rebind_pristine(
                    request_id,
                    role=intent["role"],
                    prompt=prompt,
                    source_context=source_context,
                    role_prompt_hash=role_prompt_hash,
                )
            hop["rebuild_prompt_after_relocation"] = False
        hop["prompt"] = prompt
        hop["prompt_sha256"] = _sha(prompt)
        hop["expected_report_path"] = expected
        hop["state"] = "sending"
        hop["timestamps"]["sending_at"] = utc_now()
        role_record["turn"] = max(int(role_record.get("turn") or 0), int(hop["turn"]))
        if included:
            role_record["constructor_sent_generation"] = generation
        state["started_at"] = state.get("started_at") or utc_now()
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(role)
        state["active_action"] = "send"

    def _reconcile_pristine_sent_projection(self, state: dict[str, Any], hop: dict[str, Any]) -> bool:
        if hop.get("state") != "sent":
            return False
        try:
            record = RequestLedger(hop["ledger_path"]).peek(hop["request_id"])
        except Exception:
            return False
        if not self._is_pristine_preboundary_sending_record(state, hop, record):
            return False
        hop["state"] = "sending"
        hop["pristine_sent_projection_reconciled_at"] = utc_now()
        state["active_action"] = "send"
        return True

    def _queue_preboundary_recovery(
        self, state: dict[str, Any], hop: dict[str, Any], exc: Exception
    ) -> bool:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        if not isinstance(exc, (TimeoutError, PlaywrightTimeoutError)) or is_cdp_disconnect(exc):
            return False
        try:
            record = RequestLedger(hop["ledger_path"]).peek(hop["request_id"])
        except Exception:
            return False
        if not self._is_pristine_preboundary_sending_record(state, hop, record):
            return False
        recovery = hop.setdefault("preboundary_recovery", {
            "attempts": 0, "failures": 0, "request_id": hop["request_id"],
            "prompt_sha256": hop["prompt_sha256"],
            "endpoint_id": self._execution_endpoint_id(state),
            "generation": state["roles"][hop["target_role"]].get("conversation_generation", 0),
        })
        recovery["failures"] = int(recovery.get("failures") or 0) + 1
        recovery["last_failure"] = sanitize_exception(exc)
        recovery["last_failure_at"] = utc_now()
        hop["state"] = "sending"  # Coarse projection only; the durable ledger is untouched.
        if int(recovery.get("attempts") or 0) >= 3:
            recovery["phase"] = "exhausted"
            self._block(state, "Pristine pre-Send recovery exhausted its persisted budget; request was not sent.",
                        code="preboundary_recovery_exhausted", retryable=False)
        else:
            recovery["phase"] = "pending"
            state["active_action"] = "recover_preboundary"
        return True

    async def _advance_preboundary_recovery(
        self, state: dict[str, Any], hop: dict[str, Any], actions: CDPATabActions, record: Any
    ) -> bool:
        recovery = hop.get("preboundary_recovery")
        if not isinstance(recovery, dict) or recovery.get("phase") == "complete":
            return False
        if record is not None and record.status in {RequestStatus.SENDING, RequestStatus.SENT, RequestStatus.COMPLETED}:
            # The durable block observes/reconciles these states; it never repeats Send.
            recovery["phase"] = "complete"
            return False
        if not self._is_pristine_preboundary_sending_record(state, hop, record):
            self._block(state, "Pre-Send recovery can no longer prove pristine durable ownership.",
                        code="preboundary_recovery_provenance_changed", retryable=False)
            return True
        role = str(hop["target_role"])
        if (recovery.get("request_id") != hop.get("request_id")
                or recovery.get("prompt_sha256") != hop.get("prompt_sha256")
                or recovery.get("endpoint_id") != self._execution_endpoint_id(state)
                or recovery.get("generation") != state["roles"][role].get("conversation_generation", 0)):
            self._block(state, "Pre-Send recovery identity changed; no automatic retry is permitted.",
                        code="preboundary_recovery_provenance_changed", retryable=False)
            return True
        phase = str(recovery.get("phase") or "pending")
        if phase == "ready":
            # Identity and pristine-ledger guards above are sufficient. Recovery
            # mutation already completed, so a new worker continues normal Send.
            return False
        if phase == "interrupted" or (
            phase == "in_progress" and recovery.get("worker_started_at") != self._worker_started_at
        ):
            self._block(
                state,
                "Pristine pre-Send recovery was interrupted after browser mutation was authorized; automatic replay is forbidden.",
                code="preboundary_recovery_interrupted",
                retryable=False,
            )
            return True
        if phase == "pending":
            if int(recovery.get("attempts") or 0) >= 3:
                recovery["phase"] = "exhausted"
                self._block(state, "Pristine pre-Send recovery exhausted its persisted budget; request was not sent.",
                            code="preboundary_recovery_exhausted", retryable=False)
                return True
            # Persist a budget reservation before authorizing any browser allocation.
            recovery["attempts"] = int(recovery.get("attempts") or 0) + 1
            recovery["phase"] = "reserved"
            recovery["reserved_at"] = utc_now()
            state["active_action"] = "reserve_preboundary_recovery"
            return True
        if phase == "reserved":
            # Persist consumption separately. A crash after this point must reconcile
            # or fail closed instead of allocating another replacement page.
            recovery["phase"] = "in_progress"
            recovery["worker_started_at"] = self._worker_started_at
            recovery["consumed_at"] = utc_now()
            state["active_action"] = "recover_preboundary"
            return True
        if phase != "in_progress":
            self._block(state, "Pre-Send recovery has an invalid persisted phase.",
                        code="preboundary_recovery_provenance_changed", retryable=False)
            return True
        control = {"role": role, "origin": "worker", "status": "recovering"}
        try:
            await self._recover_pristine_preboundary_sending(state, hop, control, actions)
        except Exception as exc:
            recovery["last_failure"] = sanitize_exception(exc)
            recovery["last_failure_at"] = utc_now()
            recovery["phase"] = "interrupted"
            self._block(
                state,
                "Pristine pre-Send recovery failed after browser mutation was authorized; automatic replay is forbidden.",
                code="preboundary_recovery_interrupted",
                retryable=False,
            )
            return True
        recovery["result"] = control.get("result")
        recovery["phase"] = "ready" if control.get("status") == "applied" else "recovery_required"
        if recovery["phase"] == "ready":
            state["active_action"] = "retry_preboundary"
        return True

    async def _sending(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        role = str(hop["target_role"])
        ledger = RequestLedger(str(hop["ledger_path"]))
        record = (
            ledger.get(str(hop["request_id"]))
            if ledger.path.exists()
            else None
        )
        if await self._advance_preboundary_recovery(state, hop, actions, record):
            return
        if record is not None and record.status in {
            RequestStatus.SENDING,
            RequestStatus.SENT,
            RequestStatus.COMPLETED,
        }:
            files = tuple(item.path for item in record.files)
        else:
            files = self._attachment_files_for_generation(state, role)
            if files is None:
                return
        admitted: list[bool] = []
        acquired = await self._owned_or_block(state, role, actions, admitted=admitted)
        if acquired is None:
            return
        if hop.get("rebuild_prompt_after_relocation") is True:
            current = ledger.peek(str(hop["request_id"]))
            if current is None:
                hop["rebuild_prompt_after_relocation"] = False
            else:
                if not self._is_pristine_preboundary_sending_record(state, hop, current):
                    raise DurableRequestError("relocated request is no longer pristine")
                hop["state"] = "pre_send"
                hop["relocated_pre_send_at"] = utc_now()
                state["active_action"] = "prepare_send"
                return
        if admitted:
            # Persist the recovered binding before crossing the Send boundary.
            return
        await self._dismiss_known_rate_limit_on_existing(acquired)
        if is_independent_task(state):
            independent = state.get("independent")
            if not isinstance(independent, Mapping):
                raise ValueError("independent task state is missing")
            constructor = "\n\n".join(
                (
                    str(independent.get("system_prompt") or ""),
                    self.config.independent_rule_path.read_text(encoding="utf-8"),
                )
            )
        else:
            constructor = str(
                task_workflow_definitions(state, self.config)[role]["system_prompt"]
            )
        generation = int(state["roles"][role].get("conversation_generation") or 0)
        prior_completed_role_turn = not hop.get("generation_started_here", False) and any(
            isinstance(item, Mapping)
            and item.get("hop_id") != hop.get("hop_id")
            and str(item.get("target_role") or "").upper() == role
            and item.get("conversation_generation", generation) == generation
            and item.get("browser_endpoint_id", hop.get("browser_endpoint_id")) == hop.get("browser_endpoint_id")
            and isinstance(item.get("receipt"), Mapping)
            and bool(item.get("receipt"))
            and (
                str(item.get("response") or "").strip()
                or (
                    isinstance(item.get("response_record"), Mapping)
                    and str(item["response_record"].get("role") or "") == "assistant"
                    and (
                        str(item["response_record"].get("text") or "").strip()
                        or int(item["response_record"].get("image_count") or 0) > 0
                    )
                )
            )
            for item in state.get("hops") or []
        )
        self._arm_passive_request_observer(state, hop, acquired.client)
        block = DurableSendBlock(
            str(hop["prompt"]),
            ledger_path=hop["ledger_path"],
            files=files,
            expected_file_identities=state.get("attachments") if files else None,
            source_context={
                "task_id": state["task_id"],
                "team": state["team"],
                "hop_id": hop["hop_id"],
                "manifest": state["manifest_path"],
            },
            role_prompt_hash=_sha(constructor),
            request_id=str(hop["request_id"]),
            render_request_marker=False,
            wait_for_response=False,
            require_existing_conversation_baseline=prior_completed_role_turn,
            response_timeout_ms=None,
            # Exactly-once: one click only; reload would destroy temporary doc identity and
            # create an ambiguous second send. Reload is never safe for CDPA normal workflow.
            max_attempts=1,
            recovery_reload=False,
        )
        try:
            async def perform_send() -> Any:
                return await block.run(WorkflowContext(acquired.client))

            output = await self._run_automated_send(perform_send)
        except ConversationTranscriptNotReadyError as exc:
            current_record = ledger.get(str(hop["request_id"]))
            if (
                current_record is None
                or current_record.status
                in {RequestStatus.SENDING, RequestStatus.SENT, RequestStatus.COMPLETED}
                or int(current_record.attempts or 0) != 0
            ):
                raise
            self._block(
                state,
                str(exc),
                code="conversation_transcript_not_ready",
                retryable=True,
            )
            return
        except UnsafePageStateError as exc:
            current_record = ledger.peek(str(hop["request_id"]))
            solo_existing_url = (
                str(state.get("workflow_mode") or "team").strip().lower() == "solo"
                and str(hop.get("kind") or "") == "task"
                and bool(str(hop.get("conversation_url") or "").strip())
            )
            if (
                solo_existing_url
                and self._should_observe_existing_response(exc)
                and self._is_pristine_preboundary_sending_record(
                    state, hop, current_record
                )
            ):
                self._observe_existing_response(state, hop)
                return
            raise
        except ComposerConflictError as exc:
            self._block(
                state,
                sanitize_exception(exc),
                code="manual_composer_conflict",
                retryable=True,
            )
            return
        except DurableRequestError as exc:
            current_record = ledger.get(str(hop["request_id"]))
            late_identity = "markerless send reached transport progress without accepted user-message identity"
            if current_record is not None and current_record.status is RequestStatus.SENDING and (
                str(exc) == late_identity or str(current_record.error or "") == late_identity
            ):
                since = parse_time(hop.get("send_identity_reconcile_started_at"))
                now = datetime.now(timezone.utc)
                if since is None:
                    hop["send_identity_reconcile_started_at"] = now.isoformat()
                    since = now
                if (now - since).total_seconds() < 15:
                    # Re-entering the existing SENDING ledger performs observation only.
                    # Never clear/rebind or call Send again to fix a late DOM user ID.
                    state["active_action"] = "reconcile_send"
                    return
                self._block(state, "Accepted user identity did not reconcile from the exact live page.",
                            code="send_acceptance_ambiguous", retryable=False)
                return
            raise
        except UploadIdentityChangedError:
            attachment = next(
                (
                    item
                    for item in state.get("attachments") or []
                    if isinstance(item, Mapping)
                ),
                {},
            )
            self._block(
                state,
                f"Attachment identity changed for {attachment.get('name') or 'attachment'} (expected {str(attachment.get('sha256') or '')[:12]})",
                code="attachment_identity_changed",
                retryable=False,
            )
            return
        except Exception as exc:
            if self._queue_preboundary_recovery(state, hop, exc):
                return
            if not files or is_cdp_disconnect(exc):
                raise
            names = ", ".join(
                str(item.get("name") or "attachment")
                for item in state.get("attachments") or []
                if isinstance(item, Mapping)
            )
            self._block(
                state,
                f"Attachment upload failed for {names or 'task context'}; durable request preserved ({type(exc).__name__})",
                code="attachment_upload_failed",
                retryable=False,
            )
            return
        post_send = await acquired.client.assert_ownership()
        self._record_acquired(
            state,
            role,
            AcquiredRole(
                client=acquired.client,
                page_id=acquired.page_id,
                url=post_send.conversation_url or str(post_send.url),
                created=False,
                new_chat=False,
            ),
        )
        hop["conversation_url"] = (
            str(state["roles"][role]["page_url"])
            if state.get("conversation_mode") != "temporary"
            else post_send.conversation_url or str(post_send.url)
        )
        hop["receipt"] = output["receipt"]
        if isinstance(hop.get("preboundary_recovery"), dict):
            hop["preboundary_recovery"]["phase"] = "complete"
        self._canonicalize_receipt_conversation_url(state, hop)
        try:
            observer_receipt = SendReceipt.from_dict(hop["receipt"])
        except (KeyError, TypeError, ValueError):
            observer_receipt = None
        self._arm_passive_request_observer(
            state,
            hop,
            acquired.client,
            observer_receipt,
        )
        hop["rendered_prompt_sha256"] = output["receipt"]["prompt_sha256"]
        hop["state"] = "sent"
        if files:
            role_record = state["roles"][role]
            role_record["attachments_uploaded_generation"] = int(
                role_record.get("conversation_generation") or 0
            )
        record = output.get("record") if isinstance(output, Mapping) else None
        accepted_epoch = record.get("accepted_at") if isinstance(record, Mapping) else None
        if accepted_epoch is not None:
            accepted_at = datetime.fromtimestamp(float(accepted_epoch), timezone.utc)
        else:
            accepted_at = parse_time((hop.get("timestamps") or {}).get("sending_at"))
        if accepted_at is None:
            raise RuntimeError("sending hop is missing its durable send timestamp")
        hop["timestamps"]["sent_at"] = accepted_at.isoformat()
        self._start_wait_budget_from_sent(hop)
        state["active_action"] = "wait_response"


    def _arm_passive_request_observer(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        client: Any,
        receipt: SendReceipt | None = None,
    ) -> None:
        arm = getattr(client, "arm_passive_observer", None)
        if not callable(arm):
            return
        role = str(hop.get("target_role") or "").upper()
        role_record = (state.get("roles") or {}).get(role)
        generation = (
            int(role_record.get("conversation_generation") or 0)
            if isinstance(role_record, Mapping)
            else 0
        )
        arm(
            request_id=str(hop.get("request_id") or ""),
            generation=generation,
            conversation_id=(receipt.conversation_id if receipt is not None else None),
            accepted_user_message_id=(
                receipt.user_message_id if receipt is not None else None
            ),
            task_id=str(state.get("task_id") or "") or None,
            team=str(state.get("team") or "") or None,
        )

    @staticmethod
    def _release_stream_status_slot(owner: Any, receipt: SendReceipt) -> None:
        conversation_id = str(receipt.conversation_id or "").strip()
        if not conversation_id:
            return
        release = getattr(owner, "release_backend_stream_status", None)
        if callable(release):
            release(conversation_id)

    def _capture_passive_remote_report_if_available(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        acquired: AcquiredRole,
        receipt: SendReceipt,
        response: MessageSnapshot,
    ) -> None:
        if remote_repository_from_task(str(state.get("task_text") or "")) is None:
            return
        observe = getattr(acquired.client, "passive_observation", None)
        if not callable(observe):
            return
        role_record = (state.get("roles") or {}).get(str(hop.get("target_role") or ""))
        generation = (
            int(role_record.get("conversation_generation") or 0)
            if isinstance(role_record, Mapping)
            else 0
        )
        evidence = observe(
            request_id=str(hop.get("request_id") or ""),
            generation=generation,
        )
        if not isinstance(evidence, Mapping) or evidence.get("coverage") != "complete":
            return
        graph = evidence.get("graph")
        if not isinstance(graph, Mapping) or not receipt.user_message_id or not response.message_id:
            return
        if evidence.get("observed_user_message_id") != receipt.user_message_id:
            return
        if receipt.conversation_id and evidence.get("conversation_id") != receipt.conversation_id:
            return
        self._capture_remote_report_mirror(
            state,
            hop,
            graph,
            accepted_user_message_id=receipt.user_message_id,
            terminal_assistant_message_id=response.message_id,
            response_text=response.text,
        )

    def _capture_remote_report_mirror(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        graph: Mapping[str, Any],
        *,
        accepted_user_message_id: str,
        terminal_assistant_message_id: str,
        response_text: str,
    ) -> None:
        task_text = str(state.get("task_text") or "")
        remote_repository = remote_repository_from_task(task_text)
        if remote_repository is None or _report_mode(state) == "inline":
            return
        parsed = parse_role_response(
            response_text,
            source_role=str(hop["target_role"]),
            report_mode=_report_mode(state),
            allowed_routes=tuple(task_workflow_definitions(state, self.config))
            + ("PAUSE", "DONE"),
        )
        if parsed.inline_report is not None:
            return
        expected_handoff = str(hop.get("expected_report_path") or "").strip()
        if parsed.decision.handoff != expected_handoff:
            raise RouteContractError("remote report handoff must exactly match the expected role report")
        remote_path = _remote_report_path(remote_repository, expected_handoff)
        report = resolve_completed_file_write(
            graph,
            accepted_user_message_id,
            terminal_assistant_message_id,
            recipient=_assigned_remote_mcp_recipient(task_text),
            expected_path=remote_path,
        )
        report_repository, report_plans_root = _workflow_report_roots(self.config, state)
        evidence = materialize_inline_report(
            report,
            expected_report_path=expected_handoff,
            repository_root=report_repository,
            plans_root=report_plans_root,
            team=str(state["team"]),
            physical_role=str(hop["physical_role"]),
            turn=int(hop["turn"]),
            task_id=str(state["task_id"]),
        )
        hop["mirrored_report_path"] = evidence.path
        hop["mirrored_report_sha256"] = evidence.sha256
        hop["mirrored_report_size"] = evidence.size
        hop["mirrored_report_handoff"] = expected_handoff
        hop["mirrored_report_response_sha256"] = _sha(response_text)

    def _validated_remote_report_mirror(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        *,
        decision_handoff: str,
    ) -> tuple[str, str, int]:
        expected_handoff = str(hop.get("expected_report_path") or "").strip()
        if decision_handoff != expected_handoff or hop.get("mirrored_report_handoff") != expected_handoff:
            raise RouteContractError("remote report mirror identity does not match the routed handoff")
        if hop.get("mirrored_report_response_sha256") != _sha(str(hop.get("response") or "")):
            raise RouteContractError("remote report mirror belongs to a different terminal response")

        report_repository, _report_plans_root = _workflow_report_roots(self.config, state)
        expected_path = Path(os.path.abspath(report_repository / expected_handoff))
        mirrored_raw = hop.get("mirrored_report_path")
        if not isinstance(mirrored_raw, str) or not mirrored_raw.strip():
            raise RouteContractError("remote report bytes were not durably mirrored")
        mirrored_path = Path(os.path.abspath(Path(mirrored_raw).expanduser()))
        if mirrored_path != expected_path or mirrored_path.is_symlink() or not mirrored_path.is_file():
            raise RouteContractError("remote report mirror path is not the exact control-plane report path")

        expected_sha = hop.get("mirrored_report_sha256")
        expected_size = hop.get("mirrored_report_size")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise RouteContractError("remote report mirror hash is invalid")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
            raise RouteContractError("remote report mirror size is invalid")
        data = mirrored_path.read_bytes()
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha:
            raise RouteContractError("remote report mirror hash or size changed after capture")
        return str(mirrored_path), expected_sha, expected_size

    def _validate_response_candidate(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        response: MessageSnapshot,
    ) -> None:
        if is_independent_task(state):
            text = sanitize_text(response.text, max_chars=200_000).strip()
            if not text:
                raise ValueError("independent response must not be empty")
            return
        if str(hop.get("kind") or "") == "report_repair":
            text = sanitize_text(response.text, max_chars=200_000).strip()
            if not text:
                raise ValueError("report repair response must not be empty")
            return
        role = str(hop["target_role"])
        if str(state.get("workflow_mode") or "team").strip().lower() == "solo":
            parse_route_response(
                response.text,
                source_role=role,
                allowed_routes=("PAUSE", "DONE"),
                allow_non_plan_done=True,
            )
            return
        parse_role_response(
            response.text,
            source_role=role,
            report_mode=_report_mode(state),
            allowed_routes=tuple(task_workflow_definitions(state, self.config))
            + ("PAUSE", "DONE"),
        )

    @staticmethod
    def _canonicalize_receipt_conversation_url(
        state: dict[str, Any], hop: dict[str, Any]
    ) -> None:
        if state.get("conversation_mode") == "temporary":
            return  # Backend identity remains receipt evidence, never a saved reopen URL.
        payload = hop.get("receipt")
        if not isinstance(payload, Mapping):
            return
        conversation_id = str(payload.get("conversation_id") or "").strip()
        fallback = f"https://chatgpt.com/c/{conversation_id}"
        if _official_saved_url(fallback) is None:
            return
        role = str(hop.get("target_role") or "").upper()
        role_record = state.get("roles", {}).get(role)
        exact_url = _preferred_saved_url(
            hop.get("conversation_url"),
            role_record.get("page_url") if isinstance(role_record, Mapping) else None,
            conversation_id=conversation_id,
        ) or fallback
        hop["conversation_url"] = exact_url
        if isinstance(role_record, dict):
            role_record["page_url"] = exact_url

    def _reconcile_hop_conversation_identity(
        self, state: dict[str, Any], hop: dict[str, Any]
    ) -> None:
        ledger_path = hop.get("ledger_path")
        request_id = hop.get("request_id")
        hop_payload = hop.get("receipt")
        if not ledger_path or not request_id or not isinstance(hop_payload, Mapping):
            return
        record = RequestLedger(str(ledger_path)).get(str(request_id))
        if record is None or not isinstance(record.receipt, Mapping):
            return
        try:
            hop_receipt = SendReceipt.from_dict(hop_payload)
            ledger_receipt = SendReceipt.from_dict(record.receipt)
        except Exception as exc:
            raise RuntimeError("durable conversation identity receipt is invalid") from exc
        hop_base = hop_receipt.to_dict()
        ledger_base = ledger_receipt.to_dict()
        hop_base["conversation_id"] = None
        ledger_base["conversation_id"] = None
        if hop_base != ledger_base:
            raise RuntimeError("durable conversation identity immutable receipt diverged")
        if hop_receipt.conversation_id is not None:
            if ledger_receipt.conversation_id != hop_receipt.conversation_id:
                raise RuntimeError("durable conversation identity changed or disappeared")
        elif ledger_receipt.conversation_id is not None:
            hop["receipt"] = ledger_receipt.to_dict()
        self._canonicalize_receipt_conversation_url(state, hop)

    async def _discover_accepted_conversation_identity(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        receipt: SendReceipt,
    ) -> SendReceipt:
        """Canonicalize an accepted conversation only from exact owned local state."""
        if receipt.conversation_id:
            return receipt
        if receipt.attempts < 1 or not receipt.accepted_via:
            raise DurableRequestError(
                "accepted conversation discovery requires a durable accepted Send"
            )
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        if not ledger_path or not request_id:
            raise DurableRequestError(
                "accepted conversation discovery is missing durable request context"
            )
        ledger = RequestLedger(ledger_path)
        record = ledger.get(request_id)
        if (
            record is None
            or record.status is not RequestStatus.SENT
            or record.attempts < 1
            or record.accepted_at is None
            or not isinstance(record.receipt, Mapping)
        ):
            raise DurableRequestError(
                "accepted conversation discovery requires an accepted SENT ledger record"
            )
        durable_receipt = SendReceipt.from_dict(record.receipt)
        durable_base = durable_receipt.to_dict()
        manifest_base = receipt.to_dict()
        durable_base["conversation_id"] = None
        manifest_base["conversation_id"] = None
        if durable_base != manifest_base:
            raise DurableRequestError(
                "accepted conversation discovery found immutable receipt divergence"
            )

        role = str(hop.get("target_role") or "")
        acquired = await actions.locate_owned(state, role)
        if acquired is None or str(acquired.page_id) != str(receipt.binding.page_id):
            raise GraphIdentityError(
                "accepted conversation identity is unknown because the exact owned page is unavailable"
            )
        snapshot = await acquired.client.assert_ownership()
        if snapshot.page_id != receipt.binding.page_id:
            raise GraphIdentityError("accepted conversation page binding is no longer owned")
        saved = _official_saved_url(snapshot.url)
        if saved is None:
            try:
                host = (urlparse(str(snapshot.url or "")).hostname or "").lower()
            except ValueError:
                host = ""
            if host in {"chatgpt.com", "www.chatgpt.com"}:
                raise GraphIdentityError("accepted conversation URL is not canonical yet")
            raise _AcceptedConversationIdentityConflict(
                "accepted conversation navigation left the ChatGPT origin"
            )
        conversation_id = saved[0]
        role_record = state.get("roles", {}).get(role)
        prior_ids = {
            parsed[0]
            for value in (
                hop.get("conversation_url"),
                role_record.get("page_url") if isinstance(role_record, Mapping) else None,
            )
            if (parsed := _official_saved_url(value)) is not None
        }
        if prior_ids and prior_ids != {conversation_id}:
            raise _AcceptedConversationIdentityConflict(
                "live conversation UUID conflicts with the saved durable conversation"
            )
        if snapshot.session_id and str(snapshot.session_id) != conversation_id:
            raise _AcceptedConversationIdentityConflict(
                "canonical ChatGPT URL and page snapshot disagree"
            )
        if isinstance(role_record, dict):
            role_record["page_url"] = (
                _preferred_saved_url(
                    snapshot.url, role_record.get("page_url"),
                    conversation_id=conversation_id,
                ) or str(snapshot.url)
            )
        ledger.enrich_receipt_conversation_id(
            request_id,
            accepted_receipt=receipt,
            conversation_id=conversation_id,
        )
        self._reconcile_hop_conversation_identity(state, hop)
        enriched = SendReceipt.from_dict(hop["receipt"])
        if enriched.conversation_id != conversation_id:
            raise DurableRequestError(
                "accepted conversation identity was not durably canonicalized"
            )
        return enriched

    async def _flush_hop_conversation_identity(
        self, state: dict[str, Any], hop: dict[str, Any]
    ) -> None:
        # Let an already-completed frontend response-body task publish its exact
        # ledger enrichment; never await the body task itself or any backend read.
        await asyncio.sleep(0)
        self._reconcile_hop_conversation_identity(state, hop)

    def _record_response(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        response: MessageSnapshot,
        *,
        validation_error: str | None = None,
    ) -> None:
        self._reconcile_hop_conversation_identity(state, hop)
        hop["response"] = response.text
        hop["response_sha256"] = _sha(response.text)
        hop["response_record"] = response.to_dict()
        hop["message_identity"] = {
            "message_id": response.message_id,
            "turn_id": response.turn_id,
        }
        hop["validation_error"] = (
            sanitize_text(validation_error, max_chars=2000) if validation_error is not None else None
        )
        hop["state"] = "responded"
        hop.setdefault("timestamps", {})["responded_at"] = utc_now()
        state["active_action"] = (
            "await_completion" if is_independent_task(state) else "validate_route"
        )

    def _complete_request_response(self, hop: Mapping[str, Any]) -> None:
        ledger_path = hop.get("ledger_path")
        request_id = hop.get("request_id")
        if not ledger_path or not request_id:
            return
        ledger = RequestLedger(str(ledger_path))
        record = ledger.get(str(request_id))
        if record is None or record.status is RequestStatus.COMPLETED:
            return
        if record.status is not RequestStatus.SENT:
            raise RuntimeError(
                f"cannot complete response from durable status {record.status.value}"
            )
        response = hop.get("response_record")
        if not isinstance(response, Mapping):
            text = str(hop.get("response") or "")
            if not text.strip():
                return
            identity = hop.get("message_identity") if isinstance(hop.get("message_identity"), Mapping) else {}
            response = {
                "role": "assistant",
                "message_id": str(identity.get("message_id") or ""),
                "turn_id": identity.get("turn_id"),
                "text": text,
                "actions": [],
                "image_count": 0,
            }
        if (
            str(response.get("role") or "") != "assistant"
            or (
                not str(response.get("text") or "").strip()
                and int(response.get("image_count") or 0) <= 0
            )
        ):
            return
        ledger.update(
            record.request_id,
            status=RequestStatus.COMPLETED,
            response=dict(response),
            error=None,
        )


    async def _one_shot_stream_status(
        self,
        actions: Any,
        receipt: SendReceipt,
    ) -> str | None:
        conversation_id = str(receipt.conversation_id or "").strip()
        reader = getattr(actions, "backend_stream_status", None)
        if not conversation_id or not callable(reader):
            return None
        try:
            payload = await reader(conversation_id)
        except (BackendError, OSError, TimeoutError):
            return None
        if not isinstance(payload, Mapping):
            return None
        status = str(payload.get("status") or "").strip().upper()
        return status or None

    def _queue_format_repair(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        response: MessageSnapshot | None,
        error: BaseException,
    ) -> None:
        if response is not None:
            self._record_response(
                state,
                hop,
                response,
                validation_error=str(error),
            )
        self._repair_route(state, hop, error)











    async def _capture_bootstrap_role_donor(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
        actions: CDPATabActions,
    ) -> bool:
        bootstrap = self._bootstrap_for_state(state)
        if bootstrap is None:
            return False
        role = str(hop.get("target_role") or "").upper()
        role_record = state.get("roles", {}).get(role)
        if not isinstance(role_record, dict):
            return False
        if (
            role_record.get("context_source") != "bootstrap_donor"
            or int(role_record.get("conversation_generation") or 0) != 1
            or isinstance(role_record.get("bootstrap_donor"), Mapping)
        ):
            return False
        receipt = hop.get("receipt")
        if not isinstance(receipt, Mapping):
            return False
        conversation_id = str(receipt.get("conversation_id") or "").strip()
        user_message_id = str(receipt.get("user_message_id") or "").strip()
        if not conversation_id or not user_message_id:
            return False
        source_donor = role_record.get("bootstrap_source_donor")
        if (
            isinstance(source_donor, Mapping)
            and str(source_donor.get("conversation_id") or "").strip()
            == conversation_id
        ):
            return False
        try:
            acquired = await actions.locate_owned(state, role)
            if acquired is None:
                return False
            snapshot = await acquired.client.assert_ownership()
            messages = tuple(getattr(snapshot, "messages", ()) or ())
            user_index = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if message.role == "user" and message.message_id == user_message_id
                ),
                None,
            )
            if user_index is None:
                return False
            inherited = next(
                (
                    message
                    for message in reversed(messages[:user_index])
                    if message.role == "assistant" and message.message_id
                ),
                None,
            )
            if inherited is None:
                return False
            donor = normalize_bootstrap_donor(
                {
                    "conversation_id": conversation_id,
                    "assistant_message_id": inherited.message_id,
                }
            )
        except (PageOwnershipError, RoleOwnershipError, ValueError):
            return False
        updated = BootstrapCatalog(self.config.repository_root).add_donor(
            str(bootstrap["bootstrap_id"]), donor
        )
        state["bootstrap"] = updated
        task_donors = state.setdefault("bootstrap_task_donors", [])
        task_donors[:] = [donor, *[item for item in task_donors if item != donor]][
            : int(updated["max_backups"])
        ]
        role_record["bootstrap_donor"] = donor
        return True



    async def _waiting(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        transport_baseline: dict[str, Any] | None = None,
    ) -> None:
        if hop.get("receipt"):
            self._start_wait_budget_from_sent(hop)
        elif (
            parse_time((hop.get("wait") or {}).get("started_at")) is None
            or parse_time((hop.get("wait") or {}).get("deadline_at")) is None
        ):
            raise RuntimeError("receiptless waiting hop is missing its existing wait budget")
        self._reconcile_hop_conversation_identity(state, hop)
        admitted: list[bool] = []
        acquired = await self._owned_or_block(
            state, str(hop["target_role"]), actions, admitted=admitted
        )
        if acquired is None or admitted:
            # A reopened page is persisted before ordinary response observation resumes.
            return
        if hop.get("observed_existing_response") is True and not hop.get("receipt"):
            # This activity predates the current request. Observe only whether it
            # is still active; never bind, validate, repair, or route its output.
            decision = await self.role_controller.run(
                self, state, hop, acquired, actions, phase="pre_send", wait_ms=0,
            )
            if decision.reason == "ready":
                hop["state"] = "pre_send"
                hop.pop("observed_existing_response", None)
                hop["wait"] = {}
                hop.get("timestamps", {}).pop("waiting_at", None)
                state["active_action"] = "queued"
            return
        await self.role_controller.run(self, state, hop, acquired, actions)
        if hop.get("state") == "responded" and not is_independent_task(state):
            self._responded(state, hop)



    def _route_unresolved_accepted_to_plan(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, int]:
        if is_independent_task(state):
            raise RuntimeError("accepted-send reconciliation is not available to independent tasks")
        if (
            str(state.get("status") or "").upper() != "BLOCKED"
            or str(state.get("block_code") or "")
            != "accepted_conversation_identity_unresolved"
            or str(hop.get("state") or "") != "waiting"
        ):
            raise RuntimeError(
                "accepted-send reconciliation requires the exact unresolved accepted-wait block"
            )
        receipt_value = hop.get("receipt")
        if not isinstance(receipt_value, Mapping):
            raise RuntimeError("accepted-send reconciliation requires a durable receipt")
        receipt = SendReceipt.from_dict(receipt_value)
        if (
            receipt.conversation_id is not None
            or receipt.attempts < 1
            or not receipt.user_message_id
        ):
            raise RuntimeError(
                "accepted-send reconciliation requires accepted user identity without conversation identity"
            )
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        if not ledger_path or not request_id:
            raise RuntimeError("accepted-send reconciliation lost its durable request identity")
        record = RequestLedger(ledger_path).get(request_id)
        if (
            record is None
            or record.status is not RequestStatus.SENT
            or record.attempts < 1
            or record.accepted_at is None
            or not isinstance(record.receipt, Mapping)
            or SendReceipt.from_dict(record.receipt).to_dict() != receipt.to_dict()
        ):
            raise RuntimeError(
                "accepted-send reconciliation requires the unchanged accepted SENT ledger record"
            )
        retained_reports = [
            item
            for item in state.get("reports") or []
            if isinstance(item, Mapping) and str(item.get("path") or "").strip()
        ]
        latest_report = (
            str(retained_reports[-1]["path"]) if retained_reports else "none recorded"
        )
        reconciliation_handoff = (
            "ACCEPTED-SEND RECONCILIATION ONLY.\n"
            f"Prior request {request_id} / user message {receipt.user_message_id} crossed "
            "the accepted-user boundary exactly once, but its canonical conversation identity "
            "could not be recovered.\n"
            "The prior prompt must not be replayed. Prior external/business side effects must not "
            "be repeated, retracted, or edited for recovery.\n"
            "Use durable role reports, request ledger, tracker, and existing task evidence only; "
            "reconcile bookkeeping and terminalize/route from that evidence.\n"
            f"Latest durable role report: {latest_report}"
        )
        old_hop_id = int(hop["hop_id"])
        source_role = str(hop.get("target_role") or state.get("active_role") or "PLAN").upper()
        hop["state"] = "abandoned"
        hop["abandon_reason"] = reason
        hop["accepted_send_reconciliation"] = {
            "request_id": request_id,
            "user_message_id": receipt.user_message_id,
            "attempts": record.attempts,
            "accepted_at": record.accepted_at,
            "reason": reason,
        }
        hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
        child = self._append_hop(
            state,
            source_role=source_role,
            target_role="PLAN",
            handoff=reconciliation_handoff,
            kind="accepted_send_reconciliation",
        )
        child["reconciliation_original_request_id"] = request_id
        child["reconciliation_user_message_id"] = receipt.user_message_id
        child["reconciliation_no_replay"] = True
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": old_hop_id,
                "source_role": source_role,
                "route": "PLAN",
                "kind": "accepted_send_reconciliation",
                "new_hop_id": child["hop_id"],
                "reason": reason,
            }
        )
        return {"old_hop_id": old_hop_id, "new_hop_id": int(child["hop_id"])}

    def _route_to_plan(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        *,
        reason: str,
        kind: str,
    ) -> dict[str, int]:
        if str(state.get("workflow_mode") or "team").strip().lower() == "solo":
            raise RuntimeError("Route PLAN is unavailable for Solo Task")
        if state.get("status") in TERMINAL:
            raise RuntimeError("cannot route a terminal task to PLAN")
        if str(hop.get("state") or "") in IN_FLIGHT:
            raise RuntimeError("cannot route to PLAN across an in-flight send boundary")
        source_role = str(state.get("active_role") or hop.get("target_role") or "PLAN")
        if source_role == "PLAN":
            raise RuntimeError("PLAN is already the active role")
        retained_reports = [
            item
            for item in state.get("reports") or []
            if isinstance(item, Mapping) and str(item.get("path") or "").strip()
        ]
        next_handoff = (
            str(retained_reports[-1]["path"])
            if retained_reports
            else str(state["task_text"])
        )
        old_hop_id = int(hop["hop_id"])
        hop["state"] = "abandoned"
        hop["abandon_reason"] = reason
        hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
        new_hop = self._append_hop(
            state,
            source_role=source_role,
            target_role="PLAN",
            handoff=next_handoff,
            kind=kind,
        )
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": old_hop_id,
                "source_role": source_role,
                "route": "PLAN",
                "kind": kind,
                "new_hop_id": new_hop["hop_id"],
                "reason": reason,
            }
        )
        return {"old_hop_id": old_hop_id, "new_hop_id": int(new_hop["hop_id"])}

    @staticmethod
    def _tool_failure_from_response(response: Any) -> str | None:
        text = str(response or "")
        if not text.strip() or len(text) > 16_384:
            return None
        compact = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
        # A current error may precede the terminal route object. Only accept
        # that explicit envelope, not an error line followed by arbitrary prose
        # (which can be a historical quotation in an otherwise valid report).
        error_text, separator, route_text = compact.partition("\n")
        if not separator:
            error_text, separator, suffix = compact.partition("{")
            route_text = "{" + suffix if separator else ""
        if separator:
            route_text = route_text.strip()
            if route_text.startswith("```json\n") and route_text.endswith("\n```"):
                route_text = route_text[8:-4].strip()
            try:
                route_object = json.loads(route_text)
            except (ValueError, TypeError):
                route_object = None
            if (
                isinstance(route_object, dict)
                and set(route_object) == {"route", "handoff"}
                and all(isinstance(value, str) and value.strip() for value in route_object.values())
            ):
                compact = error_text.strip()
        compact = compact.casefold()
        internal_failure = re.fullmatch(
            r"(?:(?:script error:\s*)?the tool failed internally[.!]?"
            r"(?:\n(?:write_file|shell_execute|get_skill)(?::|\s).*"
            r"(?:internal failure|failed internally)[.!]?)?"
            r"|(?:write_file|shell_execute|get_skill)\s*:\s*"
            r"(?:internal failure|failed internally)[.!]?)",
            compact,
        )
        if internal_failure is not None:
            return "session_tool_unavailable"
        exact_failure_patterns = (
            ("tool_target_unreachable", (
                r"no devices available[.!]?",
                r"target device (?:unavailable|not found)[.!]?",
                r"device (?:is not available|unavailable)[.!]?",
            )),
            ("tool_authorization_failed", (
                r"(?:unauthorized|forbidden|permission denied|not authorized|authorization failed)[.!]?",
            )),
            ("session_tool_unavailable", (
                r"(?:connector|tool) (?:is )?unavailable[.!]?",
                r"tool call failed[.!]?",
            )),
            ("session_tool_visibility_unknown", (
                r"(?:tool|connector) (?:is )?not (?:exposed|available) in this session[.!]?",
                r"cannot access the connector[.!]?",
                r"connector is not exposed[.!]?",
            )),
        )
        for code, patterns in exact_failure_patterns:
            if any(re.fullmatch(pattern, compact) for pattern in patterns):
                return code
        return None

    def _repair_report(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        error: Exception,
        *,
        decision: RouteDecision,
    ) -> None:
        self._complete_request_response(hop)
        validation_error = sanitize_text(error, max_chars=2000)
        hop["validation_error"] = validation_error
        hop["validation_route"] = decision.route
        hop["validation_handoff"] = decision.handoff
        attempt = int(hop.get("repair_attempt") or 0) + 1
        if attempt > self.config.route_repair_attempts:
            self._block(
                state,
                f"report repair exhausted: {error}",
                code="report_validation_exhausted",
                retryable=True,
            )
            return
        role = str(hop["target_role"])
        hop["state"] = "routed"
        hop["route"] = decision.route
        hop["timestamps"]["routed_at"] = utc_now()
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": hop["hop_id"],
                "source_role": role,
                "route": decision.route,
                "kind": "report_repair",
                "error": validation_error,
            }
        )
        if self._execution_budget_exhausted(state):
            hop["budget_continuation"] = {
                "kind": "report_repair",
                "source_role": role,
                "target_role": role,
                "handoff": "report repair",
                "turn": int(hop["turn"]),
                "repair_attempt": attempt,
                "validation_error": validation_error,
                "locked_route": decision.route,
                "locked_handoff": decision.handoff,
            }
            self._block(
                state,
                "Autonomous execution budget exhausted before report repair; explicit operator Resume is required.",
                code=_AUTONOMOUS_EXECUTION_BUDGET_BLOCK_CODE,
                retryable=False,
            )
            return
        repair = self._append_hop(
            state,
            source_role=role,
            target_role=role,
            handoff="report repair",
            kind="report_repair",
            turn=int(hop["turn"]),
            repair_attempt=attempt,
            validation_error=validation_error,
        )
        repair["locked_route"] = decision.route
        repair["locked_handoff"] = decision.handoff

    def _repair_route(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        error: Exception,
        *,
        decision: Any = None,
    ) -> None:
        self._complete_request_response(hop)
        validation_error = sanitize_text(error, max_chars=2000)
        hop["validation_error"] = validation_error
        if decision is not None:
            hop["validation_route"] = decision.route
            hop["validation_handoff"] = decision.handoff
        attempt = int(hop.get("repair_attempt") or 0) + 1
        if attempt > self.config.route_repair_attempts:
            self._block(
                state,
                f"route repair exhausted: {error}",
                code="route_validation_exhausted",
                retryable=True,
            )
            return
        role = str(hop["target_role"])
        hop["state"] = "routed"
        hop["route"] = role
        hop["timestamps"]["routed_at"] = utc_now()
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": hop["hop_id"],
                "source_role": role,
                "route": role,
                "kind": "route_repair",
                "error": validation_error,
            }
        )
        if self._execution_budget_exhausted(state):
            hop["budget_continuation"] = {
                "kind": "route_repair",
                "source_role": role,
                "target_role": role,
                "handoff": "route repair",
                "turn": int(hop["turn"]),
                "repair_attempt": attempt,
                "validation_error": validation_error,
            }
            self._block(
                state,
                "Autonomous execution budget exhausted before route repair; explicit operator Resume is required.",
                code=_AUTONOMOUS_EXECUTION_BUDGET_BLOCK_CODE,
                retryable=False,
            )
            return
        repair = self._append_hop(
            state,
            source_role=role,
            target_role=role,
            handoff="route repair",
            kind="route_repair",
            turn=int(hop["turn"]),
            repair_attempt=attempt,
            validation_error=validation_error,
        )

    def _responded(self, state: dict[str, Any], hop: dict[str, Any]) -> None:
        if is_independent_task(state):
            if not str(hop.get("response") or "").strip():
                self._block(
                    state,
                    "independent response is empty",
                    code="independent_response_empty",
                    retryable=True,
                )
                return
            self._complete_request_response(hop)
            state["status"] = "RUNNING"
            state["kanban_column"] = INDEPENDENT_COLUMN
            state["active_action"] = "await_completion"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            return
        role = str(hop["target_role"])
        if str(state.get("workflow_mode") or "team").strip().lower() == "solo":
            try:
                decision = parse_route_response(
                    str(hop.get("response") or ""),
                    source_role=role,
                    allowed_routes=("PAUSE", "DONE"),
                    allow_non_plan_done=True,
                )
            except (RouteContractError, ValueError) as exc:
                self._repair_route(state, hop, exc)
                return
            self._complete_request_response(hop)
            hop.update(
                {
                    "route": decision.route,
                    "routed_handoff": decision.handoff,
                    "state": "routed",
                }
            )
            hop["timestamps"]["routed_at"] = utc_now()
            state.setdefault("route_timeline", []).append(
                {
                    "at": utc_now(),
                    "hop_id": hop["hop_id"],
                    "source_role": role,
                    "route": decision.route,
                    "handoff": decision.handoff,
                }
            )
            state["roles"][role]["status"] = "idle"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            state["pause_reason"] = None
            if decision.route == "DONE":
                state["status"] = "DONE"
                state["terminal_state"] = "DONE"
                state["kanban_column"] = "DONE_STOPPED"
                state["completed_at"] = utc_now()
                state["last_role_activity_at"] = state["completed_at"]
                state["active_role"] = None
                state["active_hop_id"] = None
                state["active_action"] = "done"
                return
            state["status"] = "PAUSED"
            state["kanban_column"] = "PAUSED"
            state["active_role"] = role
            state["active_hop_id"] = hop["hop_id"]
            state["active_action"] = "paused"
            state["pause_reason"] = decision.handoff
            return
        response_mode = _report_mode(state)
        legacy_inline = response_mode == "inline"
        decision = None
        inline_report = None
        try:
            parsed = parse_role_response(
                str(hop.get("response") or ""),
                source_role=role,
                report_mode=response_mode,
                allowed_routes=tuple(task_workflow_definitions(state, self.config))
                + ("PAUSE", "DONE"),
            )
            decision = parsed.decision
            inline_report = parsed.inline_report
            if str(hop.get("kind") or "") == "report_repair":
                locked_route = str(hop.get("locked_route") or "").strip().upper()
                locked_handoff = str(hop.get("locked_handoff") or "").strip()
                if not locked_route or not locked_handoff:
                    raise RouteContractError("report repair is missing its locked route or handoff")
                if decision.handoff != locked_handoff:
                    raise RouteContractError(
                        "report repair handoff must preserve the exact expected report path"
                    )
                if locked_route == "PAUSE":
                    decision = RouteDecision("PAUSE", locked_handoff)
            if decision.route not in {"PAUSE", "DONE"} and decision.route not in state["roles"]:
                raise RouteContractError(
                    f"route {decision.route!r} is not selected for this task"
                )
            if inline_report is None:
                expected_handoff = str(hop.get("expected_report_path") or "").strip()
                if decision.handoff != expected_handoff:
                    raise RouteContractError(
                        "report handoff must exactly match the expected role report"
                    )
                routed_handoff = decision.handoff
                try:
                    if remote_repository_from_task(str(state.get("task_text") or "")) is None:
                        report_repository, report_plans_root = _workflow_report_roots(
                            self.config, state
                        )
                        evidence = validate_file_report(
                            decision.handoff,
                            repository_root=report_repository,
                            plans_root=report_plans_root,
                            team=str(state["team"]),
                            physical_role=str(hop["physical_role"]),
                            turn=int(hop["turn"]),
                            task_id=str(state["task_id"]),
                        )
                        report_path = decision.handoff
                        report_sha256 = evidence.sha256
                        report_size = evidence.size
                    else:
                        report_path, report_sha256, report_size = self._validated_remote_report_mirror(
                            state,
                            hop,
                            decision_handoff=decision.handoff,
                        )
                except (RouteContractError, OSError) as exc:
                    self._repair_report(state, hop, exc, decision=decision)
                    return
            else:
                report_repository, report_plans_root = _workflow_report_roots(
                    self.config, state
                )
                try:
                    evidence = materialize_inline_report(
                        inline_report,
                        expected_report_path=str(hop.get("expected_report_path") or ""),
                        repository_root=report_repository,
                        plans_root=report_plans_root,
                        team=str(state["team"]),
                        physical_role=str(hop["physical_role"]),
                        turn=int(hop["turn"]),
                        task_id=str(state["task_id"]),
                    )
                except (RouteContractError, OSError) as exc:
                    raise InlineReportMaterializationError(
                        "legacy inline report materialization failed"
                    ) from exc
                routed_handoff = str(hop["expected_report_path"])
                report_path = evidence.path
                report_sha256 = evidence.sha256
                report_size = evidence.size
        except InlineReportMaterializationError:
            self._block(
                state,
                "legacy inline report materialization failed",
                code="inline_report_materialization_failed",
                retryable=False,
            )
            return
        except (RouteContractError, ValueError, OSError) as exc:
            if legacy_inline:
                options = state.get("options")
                if isinstance(options, dict):
                    options["report_mode"] = "file"
            self._repair_route(state, hop, exc, decision=decision)
            return

        if legacy_inline:
            options = state.get("options")
            if not isinstance(options, dict):
                raise ValueError("task options must be mutable during legacy report migration")
            options["report_mode"] = "file"
        self._complete_request_response(hop)
        hop.update(
            {
                "report_path": report_path,
                "report_sha256": report_sha256,
                "report_size": report_size,
                "route": decision.route,
                "state": "routed",
            }
        )
        hop["timestamps"]["routed_at"] = utc_now()
        if not any(
            item.get("path") == report_path
            and item.get("role") == role
            and item.get("physical_role") == hop["physical_role"]
            and item.get("turn") == hop["turn"]
            for item in state.get("reports") or []
            if isinstance(item, Mapping)
        ):
            state.setdefault("reports", []).append(
                {
                    "report_id": len(state.get("reports") or []) + 1,
                    "role": role,
                    "physical_role": hop["physical_role"],
                    "turn": hop["turn"],
                    "path": report_path,
                    "sha256": report_sha256,
                    "size": report_size,
                    "created_at": utc_now(),
                }
            )
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": hop["hop_id"],
                "source_role": role,
                "route": decision.route,
                "report_path": report_path,
            }
        )
        state["roles"][role]["status"] = "idle"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        state["pause_reason"] = None
        if decision.route == "DONE":
            state["status"] = "DONE"
            state["terminal_state"] = "DONE"
            state["kanban_column"] = "DONE_STOPPED"
            state["completed_at"] = utc_now()
            state["last_role_activity_at"] = state["completed_at"]
            state["active_role"] = None
            state["active_hop_id"] = None
            state["active_action"] = "done"
            return
        if decision.route == "PAUSE":
            state["status"] = "PAUSED"
            state["kanban_column"] = "PAUSED"
            state["active_role"] = role
            state["active_hop_id"] = hop["hop_id"]
            state["active_action"] = "paused"
            return
        if self._execution_budget_exhausted(state):
            self._block(
                state,
                (
                    "Autonomous execution budget exhausted after preserving the valid report "
                    "and route decision; explicit operator Resume is required before another execution."
                ),
                code=_AUTONOMOUS_EXECUTION_BUDGET_BLOCK_CODE,
                retryable=False,
            )
            return
        self._append_hop(
            state,
            source_role=role,
            target_role=decision.route,
            handoff=routed_handoff,
        )

    @staticmethod
    def _resume_before(control: Mapping[str, Any]) -> dict[str, Any] | None:
        result = control.get("result")
        before = result.get("before") if isinstance(result, Mapping) else None
        return dict(before) if isinstance(before, Mapping) else None

    def _finish_resume_control(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        *,
        outcome: str,
        action: str,
        reason_code: str | None,
        reason: str,
        postcondition: str | None,
        next_safe_action: str | None = None,
    ) -> None:
        normalized_outcome = str(outcome).strip().lower()
        status_by_outcome = {
            "continued": ("applied", "APPLIED"),
            "recovery_required": ("recovery_required", "RECOVERY_REQUIRED"),
            "failed": ("failed", "FAILED"),
        }
        try:
            control_status, command_state = status_by_outcome[normalized_outcome]
        except KeyError as exc:
            raise ValueError(f"unsupported Resume outcome {outcome!r}") from exc
        role = str(control.get("role") or state.get("active_role") or "PLAN").upper()
        control["status"] = control_status
        control["command_state"] = command_state
        control["result"] = {
            "outcome": normalized_outcome,
            "action": str(action or "none"),
            "reason_code": str(reason_code or "") or None,
            "reason": sanitize_text(reason, max_chars=1200),
            "next_safe_action": (
                sanitize_text(next_safe_action, max_chars=500)
                if next_safe_action
                else None
            ),
            "postcondition": str(postcondition or "") or None,
            "before": self._resume_before(control),
            "after": command_snapshot(state, role=role),
        }
        control["applied_at"] = utc_now()

    def _require_resume_recovery(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        *,
        action: str,
        reason_code: str,
        reason: str,
        next_safe_action: str,
    ) -> None:
        self._block(state, reason, code=reason_code, retryable=False)
        self._finish_resume_control(
            state,
            control,
            outcome="recovery_required",
            action=action,
            reason_code=reason_code,
            reason=reason,
            next_safe_action=next_safe_action,
            postcondition=None,
        )

    def _record_resume_send_acceptance(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        ledger: RequestLedger,
        record: Any,
        receipt: SendReceipt,
    ) -> None:
        accepted_at = time.time()
        ledger.update(
            record.request_id,
            status=RequestStatus.SENT,
            accepted_at=accepted_at,
            attempts=max(1, int(record.attempts or 0)),
            receipt=receipt.to_dict(),
            error=None,
        )
        hop["receipt"] = receipt.to_dict()
        hop["rendered_prompt_sha256"] = receipt.prompt_sha256
        hop["state"] = "waiting"
        hop.setdefault("timestamps", {})["sent_at"] = datetime.fromtimestamp(
            accepted_at, timezone.utc
        ).isoformat()
        hop["timestamps"]["waiting_at"] = utc_now()
        start_wait_budget(
            hop["wait"],
            timeout_seconds=self.config.response_timeout_seconds,
            now=datetime.fromtimestamp(accepted_at, timezone.utc),
        )
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(str(hop["target_role"]))
        state["active_action"] = "wait_response"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None

    async def _resume_exact_owned_role(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
        actions: CDPATabActions,
        *,
        expected_page_id: str | None,
    ) -> tuple[AcquiredRole, bool] | None:
        role = str(hop["target_role"])
        acquired = await actions.locate_owned(state, role)
        reopened = acquired is None
        if acquired is None:
            if self._rate_limit_gate_active(state=state):
                state["active_action"] = "rate_limit_cooldown"
                return None
            await self._acquire_endpoint_admission(state, role)
            acquired = await actions.reopen(state, role)
        await self._dismiss_known_rate_limit_on_existing(acquired)
        if expected_page_id and str(acquired.page_id) != expected_page_id:
            raise PageOwnershipError(
                "recovered tab does not match the durable accepted-send page binding"
            )
        expected_conversation = conversation_identity(
            hop.get("conversation_url")
            or state["roles"][role].get("page_url")
        )
        if (
            expected_conversation
            and conversation_identity(acquired.url) != expected_conversation
        ):
            raise PageOwnershipError(
                "recovered tab does not match the durable accepted conversation"
            )
        return acquired, reopened

    async def _recover_resume_waiting(self, state, hop, control, actions) -> None:
        # Resume an accepted request by observing the saved conversation. A new
        # operator prompt/Retry is valid; no original-user or backend proof is needed.
        if not isinstance(hop.get("receipt"), Mapping):
            if hop.get("kind") in {"route_repair", "report_repair"}:
                ledger = RequestLedger(str(hop["ledger_path"]))
                record = ledger.peek(str(hop["request_id"]))
                pristine_repair = (
                    record is None
                    or (
                        int(getattr(record, "attempts", 0) or 0) == 0
                        and getattr(record, "receipt", None) is None
                    )
                )
                if pristine_repair:
                    hop["state"] = "pre_send"
                    hop.setdefault("timestamps", {}).pop("waiting_at", None)
                    state["status"] = "RUNNING"
                    state["kanban_column"] = _column_for(str(hop["target_role"]))
                    state["active_action"] = "queued"
                    state["block_code"] = None
                    state["block_retryable"] = False
                    state["block_reason"] = None
                    self._finish_resume_control(
                        state,
                        control,
                        outcome="continued",
                        action="restore_repair_presend",
                        reason_code=None,
                        reason="Pristine repair hop restored to pre-send; no accepted Send exists.",
                        postcondition="repair_presend_restored",
                    )
                    return
            self._require_resume_recovery(
                state, control, action="none", reason_code="accepted_send_receipt_missing",
                reason="The durable Send receipt is missing.",
                next_safe_action="Restore the receipt; do not resend accepted work.",
            )
            return
        missing_terminal_result_recovery = (
            str(state.get("block_code") or "") == "response_without_terminal_result"
        )
        if not missing_terminal_result_recovery:
            self._start_wait_budget_from_sent(hop)
        try:
            self._reconcile_hop_conversation_identity(state, hop)
        except RuntimeError as exc:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="accepted_receipt_provenance_divergence",
                reason=sanitize_exception(exc),
                next_safe_action=(
                    "Explicit Stop is the only legal in-task exit; preserve both "
                    "divergent accepted receipt records for forensic review."
                ),
            )
            return
        wait = hop["wait"]
        if (
            remaining_timeout_ms(wait) <= 0
            and not missing_terminal_result_recovery
            and wait.get("accepted_identity_started_at") is None
        ):
            wait["deadline_at"] = (datetime.now(timezone.utc) + timedelta(
                seconds=self.config.response_timeout_seconds)).isoformat()
            for key in ("timeout_refreshed", "timeout_status_checked", "timeout_status"):
                wait.pop(key, None)
        role = str(hop["target_role"])
        admitted: list[bool] = []
        acquired = await self._owned_or_block(state, role, actions, admitted=admitted)
        if state.get("active_hop_id") != hop.get("hop_id") and state.get("status") != "BLOCKED":
            self._finish_resume_control(
                state, control, outcome="continued", action="recover_execution",
                reason_code=None, reason="Durable recovery continuation was queued without replaying the accepted request.",
                postcondition="recovery_continuation_queued",
            )
            return
        if acquired is None:
            if (
                str(state.get("status") or "").upper() == "RUNNING"
                and state.get("active_action") == "wait_accepted_conversation_identity"
            ):
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="observe_accepted_identity_settle",
                    reason_code=None,
                    reason=(
                        "The exact accepted page and user remain proven while canonical "
                        "conversation identity settles within the persisted deadline."
                    ),
                    postcondition="accepted_identity_settle_preserved",
                )
                return
            self._require_resume_recovery(
                state, control, action="reopen_saved_tab",
                reason_code=str(state.get("block_code") or "role_offline"),
                reason=str(state.get("block_reason") or "Saved conversation is unavailable."),
                next_safe_action="Restore the saved conversation, then Resume.",
            )
            return
        if missing_terminal_result_recovery:
            self._queue_format_repair(
                state,
                hop,
                None,
                RouteContractError(
                    "accepted execution produced no terminal result; recover the route "
                    "from the existing workspace/report state without replaying the Send"
                ),
            )
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="queue_route_repair",
                reason_code=None,
                reason=(
                    "The accepted execution already exhausted terminal-result recovery; "
                    "a route-only repair continuation was queued without replaying the Send."
                ),
                postcondition="route_repair_queued",
            )
            return
        if admitted:
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="reopen_saved_tab",
                reason_code=None,
                reason="The exact saved role tab was reopened and durably rebound before observation.",
                postcondition="tab_reopened",
            )
            return
        wait["resume_allow_missing_completion_action"] = True
        try:
            decision = await self.role_controller.run(self, state, hop, acquired, actions, wait_ms=0)
        finally:
            wait.pop("resume_allow_missing_completion_action", None)
        if state.get("status") == "BLOCKED":
            self._require_resume_recovery(
                state, control, action="observe_saved_tab",
                reason_code=str(state.get("block_code") or "observation_blocked"),
                reason=str(state.get("block_reason") or decision.reason),
                next_safe_action="Resolve the displayed prerequisite, then Resume.",
            )
            return
        state["status"] = "RUNNING"
        state["block_code"] = None
        state["block_reason"] = None
        state["block_retryable"] = False
        state["kanban_column"] = _column_for(role)
        if hop.get("state") == "responded":
            self._responded(state, hop)
        self._finish_resume_control(
            state, control, outcome="continued", action="resume_role_controller",
            reason_code=None, reason="The saved conversation is handled by the shared role controller.",
            postcondition="response_consumed" if decision.action is Action.ACCEPT else "observation_rearmed",
        )

    def _is_pristine_preboundary_sending_record(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        record: Any,
    ) -> bool:
        if is_independent_task(state):
            return False
        if str(hop.get("state") or "") not in {"sending", "sent"}:
            return False
        if record is None or record.status not in {RequestStatus.NEW, RequestStatus.PROMPT_SET}:
            return False
        if int(record.attempts or 0) != 0:
            return False
        if any(
            value is not None
            for value in (
                record.binding,
                record.baseline,
                record.receipt,
                record.accepted_at,
                record.upload_receipt,
                record.response,
                record.session_id_before,
                record.error,
                hop.get("receipt"),
                hop.get("message_identity"),
                hop.get("response"),
            )
        ):
            return False
        request_id = str(hop.get("request_id") or "")
        prompt = str(hop.get("prompt") or "")
        physical_role = str(hop.get("physical_role") or "")
        logical_role = str(hop.get("target_role") or "").upper()
        if (
            not request_id
            or record.request_id != request_id
            or not prompt
            or str(hop.get("prompt_sha256") or "") != _sha(prompt)
            or record.role != physical_role
            or logical_role not in state.get("roles", {})
        ):
            return False
        expected_source = {
            "task_id": state.get("task_id"),
            "team": state.get("team"),
            "hop_id": hop.get("hop_id"),
            "manifest": state.get("manifest_path"),
        }
        if record.source_context != expected_source:
            return False
        constructor = str(
            task_workflow_definitions(state, self.config)[logical_role]["system_prompt"]
        )
        role_prompt_hash = _sha(constructor)
        if record.role_prompt_hash != role_prompt_hash:
            return False
        attachments = state.get("attachments") or []
        if not isinstance(attachments, list) or any(
            not isinstance(item, Mapping) for item in attachments
        ):
            return False
        try:
            current_files = sorted(
                (
                    str(item["path"]),
                    str(item["name"]),
                    int(item["size"]),
                    str(item["sha256"]),
                    str(item.get("mime_type") or "application/octet-stream"),
                )
                for item in attachments
            )
        except (KeyError, TypeError, ValueError):
            return False
        durable_files = sorted(
            (item.path, item.name, item.size, item.sha256, item.mime_type)
            for item in record.files
        )
        if current_files != durable_files:
            return False
        expected_key = build_idempotency_key(
            role=physical_role,
            prompt=prompt,
            source_context=expected_source,
            role_prompt_hash=role_prompt_hash,
            files=record.files,
        )
        return (
            record.idempotency_key == expected_key
            and record.rendered_prompt == record.prompt
        )

    def _proven_operator_new_chat_preboundary_context(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        role: str,
    ) -> bool:
        role_record = (state.get("roles") or {}).get(role)
        if not isinstance(role_record, Mapping):
            return False
        if role_record.get("conversation_generation") != 1:
            return False
        current_page_id = str(role_record.get("page_id") or "").strip()
        if not current_page_id:
            return False
        if any(
            _recoverable_conversation_identity(value) is not None
            for value in (hop.get("conversation_url"), role_record.get("page_url"))
        ):
            return False
        if hop.get("receipt") is not None or hop.get("message_identity") is not None:
            return False
        expected_handoff_sha = _sha(str(hop.get("handoff") or ""))
        controls = state.get("controls") or ()
        if not isinstance(controls, (list, tuple)):
            return False
        matches: list[int] = []
        for index, candidate in enumerate(controls):
            if not isinstance(candidate, Mapping):
                continue
            if (
                candidate.get("action") != "new_chat"
                or str(candidate.get("role") or "").upper() != role
                or str(candidate.get("origin") or "").lower() != "operator"
                or candidate.get("status") != "applied"
                or candidate.get("command_state") != "APPLIED"
            ):
                continue
            command = candidate.get("command")
            result = candidate.get("result")
            if not isinstance(command, Mapping) or not isinstance(result, Mapping):
                continue
            snapshot = command.get("snapshot")
            if not isinstance(snapshot, Mapping):
                continue
            if (
                command.get("action") != "new_chat"
                or str(command.get("origin") or "").lower() != "operator"
                or command.get("task_id") != state.get("task_id")
                or command.get("team") != state.get("team")
                or str(command.get("role") or "").upper() != role
                or result.get("new_chat") is not True
                or str(result.get("page_id") or "") != current_page_id
                or snapshot.get("active_hop_id") != hop.get("hop_id")
                or snapshot.get("active_request_id") != hop.get("request_id")
                or str(snapshot.get("active_role") or "").upper() != role
                or str(snapshot.get("role") or "").upper() != role
                or snapshot.get("physical_role") != hop.get("physical_role")
                or snapshot.get("hop_state") != "pre_send"
                or snapshot.get("hop_turn") != hop.get("turn")
                or snapshot.get("handoff_sha256") != expected_handoff_sha
                or snapshot.get("conversation_generation") != 0
                or snapshot.get("page_id") is not None
                or snapshot.get("conversation_id") is not None
                or snapshot.get("receipt_sha256") is not None
            ):
                continue
            matches.append(index)
        if len(matches) != 1:
            return False
        matched_index = matches[0]
        for candidate in controls[matched_index + 1 :]:
            if not isinstance(candidate, Mapping):
                continue
            if (
                str(candidate.get("role") or "").upper() == role
                and candidate.get("action") in {"new_chat", "restart_role"}
                and candidate.get("status") == "applied"
            ):
                return False
        return True

    async def _restart_pristine_resume_role(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
        *,
        reason: str,
    ) -> None:
        """Last safe Resume fallback before Send: replace the role context, not the request."""
        role = str(hop["target_role"])
        if state.get("attachments"):
            self._require_resume_recovery(
                state,
                control,
                action="restart_role_fallback",
                reason_code="preboundary_context_unrecoverable",
                reason="The pristine pre-Send role owns attachments and cannot be replaced safely.",
                next_safe_action="Restore the exact attachment-owning page before Resume.",
            )
            return
        await self._acquire_endpoint_admission(state, role)
        try:
            acquired = await actions.restart(
                state,
                role,
                known_automated_draft=str(hop.get("prompt") or "") or None,
            )
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            self._apply_rate_limit_to_state(state, hop)
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="defer_tab_open",
                reason_code=None,
                reason="Role restart is paused for the request-rate-limit cooldown.",
                postcondition="tab_open_deferred",
            )
            return
        except (ComposerConflictError, ManualInputPendingError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="restart_role_fallback",
                reason_code="manual_composer_conflict",
                reason=sanitize_exception(exc),
                next_safe_action="Resolve the manual composer or attachments before Resume.",
            )
            return
        except Exception as exc:
            new_chat = getattr(actions, "new_chat", None)
            if not callable(new_chat):
                self._require_resume_recovery(
                    state,
                    control,
                    action="restart_role_fallback",
                    reason_code="preboundary_context_unrecoverable",
                    reason=sanitize_exception(exc),
                    next_safe_action="Restore the role context or create a new role chat, then Resume.",
                )
                return
            try:
                acquired = await new_chat(state, role)
            except (ComposerConflictError, ManualInputPendingError) as nested:
                self._require_resume_recovery(
                    state,
                    control,
                    action="new_chat_fallback",
                    reason_code="manual_composer_conflict",
                    reason=sanitize_exception(nested),
                    next_safe_action="Resolve the manual composer or attachments before Resume.",
                )
                return
            except Exception as nested:
                self._require_resume_recovery(
                    state,
                    control,
                    action="new_chat_fallback",
                    reason_code="preboundary_context_unrecoverable",
                    reason=sanitize_exception(nested),
                    next_safe_action="Restore a usable role context, then Resume.",
                )
                return

        request_id = str(hop.get("request_id") or "").strip()
        ledger_path = str(hop.get("ledger_path") or "").strip()
        if request_id and ledger_path:
            ledger = RequestLedger(ledger_path)
            record = ledger.peek(request_id)
            if record is not None:
                if record.status not in {RequestStatus.NEW, RequestStatus.PROMPT_SET} or record.attempts != 0:
                    raise RuntimeError("Resume restart fallback requires a pristine pre-Send request")
                ledger.update(
                    request_id,
                    status=RequestStatus.FAILED_FINAL,
                    error="abandoned before Send by Resume role restart",
                )

        self._record_acquired(state, role, acquired)
        state["roles"][role]["constructor_sent_generation"] = None
        old_hop_id = int(hop["hop_id"])
        hop["state"] = "abandoned"
        hop["abandon_reason"] = sanitize_text(reason, max_chars=1200)
        hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
        child = self._append_hop(
            state,
            source_role=role,
            target_role=role,
            handoff=str(hop.get("handoff") or "Resume role restart"),
            kind="role_restart",
        )
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        state["pause_reason"] = None
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": old_hop_id,
                "source_role": role,
                "route": role,
                "kind": "resume_role_restart",
                "new_hop_id": child["hop_id"],
                "reason": sanitize_text(reason, max_chars=1200),
            }
        )
        self._finish_resume_control(
            state,
            control,
            outcome="continued",
            action="restart_role_fallback",
            reason_code=None,
            reason="Resume exhausted equivalent pre-Send recovery and restarted the role safely.",
            postcondition="child_hop_appended",
        )

    async def _recover_pristine_preboundary_sending(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        role = str(hop["target_role"])
        role_record = state["roles"][role]
        generation = int(role_record.get("conversation_generation") or 0)
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        if not ledger_path or not request_id:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_provenance_missing",
                reason="Resume cannot prove the exact pre-Send request without its durable ledger identity.",
                next_safe_action="Restore the durable request ledger; do not relocate or resend the prompt.",
            )
            return
        try:
            durable = RequestLedger(ledger_path).peek(request_id)
        except Exception:
            durable = None
        if durable is None:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_provenance_missing",
                reason="Resume cannot prove pristine pre-Send ownership because the durable request record is missing.",
                next_safe_action="Restore the durable request record; do not relocate or resend the prompt.",
            )
            return
        if durable.status not in {RequestStatus.NEW, RequestStatus.PROMPT_SET} or int(durable.attempts or 0) != 0:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_provenance_missing",
                reason="Resume cannot classify this request as pristine pre-Send from its durable record.",
                next_safe_action="Reconcile the exact durable request boundary; do not resend the prompt.",
            )
            return
        acquired = await actions.locate_owned(state, role)
        action = "confirm_preboundary_role"
        postcondition = "ownership_confirmed_before_send"
        explicit_fresh = state.get("context_policy") == "fresh"
        temporary_recovery = state.get("conversation_mode") == "temporary"
        refresh_stale_recovery_prompt = (
            hop.get("kind") == "execution_recovery"
            and hop.get("rebuild_prompt_after_relocation") is True
            and len(str(hop.get("prompt") or "")) > _EXECUTION_RECOVERY_PROMPT_MAX_CHARS
        )
        if (
            temporary_recovery
            and hop.get("rebuild_prompt_after_relocation") is True
            and not role_record.get("temporary_document_id")
        ):
            acquired = None
        proven_operator_fresh = False
        if acquired is None:
            await self._admission_preflight(state, actions)
            if state.get("attachments"):
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="preboundary_context_unrecoverable",
                    reason="The lost pristine pre-boundary role owns attachments and cannot transfer to a replacement page.",
                    next_safe_action="Restore the exact attachment-owning pre-send page before Resume.",
                )
                return
            exact_donor = (
                not temporary_recovery
                and role_record.get("context_source") == "bootstrap_donor"
            )
            if temporary_recovery:
                proven_operator_fresh = True
            elif exact_donor:
                bootstrap = self._bootstrap_for_state(state)
                try:
                    donor = normalize_bootstrap_donor(role_record.get("bootstrap_source_donor"))
                except Exception:
                    donor = None
                if bootstrap is None or donor not in self._bootstrap_donor_candidates(state, bootstrap):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="bootstrap_unavailable",
                        reason="The selected bootstrap or its exact donor is unavailable for this execution endpoint.",
                        next_safe_action="Restore a compatible exact bootstrap selection; do not open a replacement page.",
                    )
                    return
                donor_candidates = [donor]
            else:
                proven_operator_fresh = explicit_fresh or self._proven_operator_new_chat_preboundary_context(
                    state, hop, role
                )
                if not explicit_fresh and (
                    role_record.get("context_source") not in {None, ""}
                    or role_record.get("bootstrap_source_donor") is not None
                    or (generation != 0 and not proven_operator_fresh)
                ):
                    await self._restart_pristine_resume_role(
                        state,
                        hop,
                        control,
                        actions,
                        reason="The lost pristine pre-boundary role cannot prove an equivalent reusable context.",
                    )
                    return
                if not proven_operator_fresh:
                    bootstrap = self._bootstrap_for_state(state)
                    donor_candidates = (
                        self._bootstrap_donor_candidates(state, bootstrap)
                        if bootstrap is not None
                        else []
                    )
                    if not donor_candidates:
                        await self._restart_pristine_resume_role(
                            state,
                            hop,
                            control,
                            actions,
                            reason="The lost pristine pre-boundary role has no current bootstrap donor available.",
                        )
                        return
                    # The recorded donor is validated only when the browser performs
                    # the bounded branch/reacquire operation below. Resume must not
                    # fetch a full conversation graph as a preflight.
            if self._rate_limit_gate_active(state=state):
                state["active_action"] = "rate_limit_cooldown"
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="defer_tab_open",
                    reason_code=None,
                    reason="New tab creation is paused for the request-rate-limit cooldown.",
                    postcondition="tab_open_deferred",
                )
                return
            await self._acquire_endpoint_admission(state, role)
            if proven_operator_fresh:
                try:
                    if explicit_fresh or temporary_recovery:
                        acquired = await actions.fresh_chat(
                            state,
                            role,
                            url=(
                                "https://chatgpt.com/?temporary-chat=true"
                                if temporary_recovery
                                else "https://chatgpt.com/"
                            ),
                            clean_for_send=True,
                        )
                        if explicit_fresh:
                            role_record["context_source"] = "fresh"
                    else:
                        acquired = await actions.restart(
                            state,
                            role,
                            known_automated_draft=str(hop.get("prompt") or ""),
                        )
                except RateLimitBlockedError as exc:
                    await self._enter_rate_limit_cooldown(state, actions, exc)
                    self._apply_rate_limit_to_state(state, hop)
                    self._finish_resume_control(
                        state,
                        control,
                        outcome="continued",
                        action="defer_tab_open",
                        reason_code=None,
                        reason="New tab creation is paused for the request-rate-limit cooldown.",
                        postcondition="tab_open_deferred",
                    )
                    return
                except (ComposerConflictError, ManualInputPendingError) as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="reacquire_preboundary_role",
                        reason_code="manual_composer_conflict",
                        reason=sanitize_exception(exc),
                        next_safe_action="Resolve the manual composer or attachments before Resume.",
                    )
                    return
                except Exception as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="reacquire_preboundary_role",
                        reason_code="preboundary_context_unrecoverable",
                        reason=sanitize_exception(exc),
                        next_safe_action="Restore or reacquire the proven fresh pre-send role before Resume.",
                    )
                    return
            if not proven_operator_fresh:
                branch_errors: list[str] = []
                acquired = None
                for donor in donor_candidates:
                    try:
                        acquired = await actions.branch_from_anchor(
                            state,
                            role,
                            source_conversation_id=donor["conversation_id"],
                            assistant_message_id=donor["assistant_message_id"],
                        )
                        break
                    except RateLimitBlockedError as exc:
                        await self._enter_rate_limit_cooldown(state, actions, exc)
                        self._apply_rate_limit_to_state(state, hop)
                        self._finish_resume_control(
                            state,
                            control,
                            outcome="continued",
                            action="defer_tab_open",
                            reason_code=None,
                            reason="New tab creation is paused for the request-rate-limit cooldown.",
                            postcondition="tab_open_deferred",
                        )
                        return
                    except (ComposerConflictError, ManualInputPendingError) as exc:
                        self._require_resume_recovery(
                            state,
                            control,
                            action="reacquire_preboundary_role",
                            reason_code="manual_composer_conflict",
                            reason=sanitize_exception(exc),
                            next_safe_action="Resolve the manual composer or attachments before Resume.",
                        )
                        return
                    except (BranchTargetUnresolvedError, BranchBootstrapError) as exc:
                        branch_errors.append(sanitize_exception(exc))
                        continue
                if acquired is None:
                    await self._restart_pristine_resume_role(
                        state,
                        hop,
                        control,
                        actions,
                        reason=(
                            "All safe bootstrap donor recovery attempts failed: "
                            + "; ".join(branch_errors[-3:])
                        ),
                    )
                    return
            action = "reacquire_preboundary_role"
            postcondition = "ownership_reacquired_before_send"
        self._record_acquired(
            state,
            role,
            AcquiredRole(
                client=acquired.client,
                page_id=acquired.page_id,
                url=acquired.url,
                created=False,
                new_chat=False,
            ),
        )
        if int(role_record.get("conversation_generation") or 0) != generation:
            raise RuntimeError("pre-boundary role replacement changed conversation generation")
        if temporary_recovery and (acquired.created or acquired.new_chat):
            role_record["temporary_document_id"] = await self._temporary_document_token(
                acquired.client, create=True
            )
        hop["conversation_url"] = acquired.url
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(role)
        if refresh_stale_recovery_prompt:
            hop["state"] = "pre_send"
            state["active_action"] = "prepare_send"
            action = "refresh_stale_execution_recovery_prompt"
            postcondition = "same_request_prompt_rebuild_ready"
        else:
            state["active_action"] = "send"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        self._finish_resume_control(
            state,
            control,
            outcome="continued",
            action=action,
            reason_code=None,
            reason="The exact pre-boundary request ownership is ready without crossing Send.",
            postcondition=postcondition,
        )

    async def _recover_lost_sending_nonacceptance(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
        ledger: RequestLedger,
        record: Any,
        *,
        persist_admission: Callable[[], None] | None = None,
    ) -> None:
        if record.files:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="attachment_ownership_missing",
                reason="A lost SENDING page cannot transfer exact live attachment ownership.",
                next_safe_action="Restore the exact attachment page; do not re-upload or resend.",
            )
            return
        role = str(hop["target_role"])
        role_record = state["roles"][role]
        if state.get("context_policy") != "fresh":
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="bootstrap_disabled",
                reason="Bootstrap recovery is disabled; lost SENDING work cannot branch from a donor.",
                next_safe_action="Restore the exact page or restart the task in Fresh context; do not branch from bootstrap.",
            )
            return
        await self._admission_preflight(state, actions)
        generation = int(role_record.get("conversation_generation") or 0)
        if self._rate_limit_gate_active(state=state):
            state["active_action"] = "rate_limit_cooldown"
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="defer_tab_open",
                reason_code=None,
                reason="New tab creation is paused for the request-rate-limit cooldown.",
                postcondition="tab_open_deferred",
            )
            return
        await self._acquire_endpoint_admission(state, role)
        try:
            acquired = await actions.fresh_chat(
                state,
                role,
                url=(
                    "https://chatgpt.com/?temporary-chat=true"
                    if state.get("conversation_mode") == "temporary"
                    else "https://chatgpt.com/"
                ),
                clean_for_send=True,
            )
            role_record["context_source"] = "fresh"
            role_record.pop("bootstrap_source_donor", None)
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            state["active_action"] = "rate_limit_cooldown"
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="defer_tab_open",
                reason_code=None,
                reason="New tab creation is paused for the request-rate-limit cooldown.",
                postcondition="tab_open_deferred",
            )
            return
        except (ComposerConflictError, ManualInputPendingError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="reacquire_sending_role",
                reason_code="manual_composer_conflict",
                reason=sanitize_exception(exc),
                next_safe_action="Preserve the composer; do not open another replacement tab.",
            )
            return
        except Exception as exc:
            self._require_resume_recovery(
                state,
                control,
                action="reacquire_sending_role",
                reason_code="sending_replacement_context_unrecoverable",
                reason=sanitize_exception(exc),
                next_safe_action="Restore or reacquire a Fresh replacement on the same endpoint.",
            )
            return
        snapshot = await acquired.client.assert_ownership()
        binding = acquired.client.binding
        if binding is None:
            raise RuntimeError("bounded SENDING replacement has no page binding")
        baseline = capture_message_baseline(
            snapshot.messages, snapshot.logical_turn_ids
        )
        self._record_acquired(
            state,
            role,
            AcquiredRole(acquired.client, acquired.page_id, acquired.url, False, False),
        )
        if int(role_record.get("conversation_generation") or 0) != generation:
            raise RuntimeError("bounded SENDING replacement changed conversation generation")
        hop["conversation_url"] = getattr(snapshot, "conversation_url", None) or acquired.url
        record = ledger.update(
            record.request_id,
            binding=binding,
            baseline=baseline,
            session_id_before=getattr(snapshot, "session_id", None),
            error=_SENDING_CONTINUATION_STARTED,
        )
        if persist_admission is not None:
            persist_admission()
        else:
            self._release_endpoint_admission()
        self._arm_passive_request_observer(state, hop, acquired.client)
        try:
            receipt = await self._run_automated_send(
                lambda: acquired.client.send(
                    record.rendered_prompt,
                    wait_for_stop=False,
                    max_attempts=1,
                    recovery_reload=False,
                    expected_task_id=str(state["task_id"]),
                    expected_team=str(state["team"]),
                    expected_attachment_count=0,
                    expected_attachment_names=(),
                )
            )
        except Exception as exc:
            if isinstance(exc, RateLimitBlockedError):
                await self._enter_rate_limit_cooldown(state, actions, exc)
            ledger.update(
                record.request_id,
                error=f"{_SENDING_CONTINUATION_STARTED}: {type(exc).__name__}: {exc}",
            )
            self._require_resume_recovery(
                state,
                control,
                action="accept_owned_draft",
                reason_code="send_acceptance_ambiguous",
                reason=sanitize_exception(exc),
                next_safe_action="Inspect durable/backend provenance; do not send again.",
            )
            return
        if receipt.binding != binding or receipt.baseline != baseline or not (
            receipt.user_message_id or receipt.user_turn_id
        ):
            ledger.update(
                record.request_id,
                error=f"{_SENDING_CONTINUATION_STARTED}: accepted provenance mismatch",
            )
            self._require_resume_recovery(
                state,
                control,
                action="accept_owned_draft",
                reason_code="send_acceptance_ambiguous",
                reason="The bounded continuation did not return exact accepted-user provenance.",
                next_safe_action="Inspect durable/backend provenance; do not send again.",
            )
            return
        self._arm_passive_request_observer(state, hop, acquired.client, receipt)
        self._record_resume_send_acceptance(state, hop, ledger, record, receipt)
        self._finish_resume_control(
            state,
            control,
            outcome="continued",
            action="accept_owned_draft",
            reason_code=None,
            reason="Proven non-acceptance allowed one bounded continuation of the same request.",
            postcondition="draft_accepted_once",
        )

    async def _recover_failed_upload_before_ready(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
        ledger: RequestLedger,
        record: Any,
    ) -> bool:
        if record is None or record.status is not RequestStatus.UPLOADING:
            return False

        def recovery_required(reason_code: str, reason: str, next_safe_action: str) -> bool:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code=reason_code,
                reason=reason,
                next_safe_action=next_safe_action,
            )
            return True

        if int(record.attempts or 0) != 0 or any(
            value is not None
            for value in (
                record.binding,
                record.baseline,
                record.receipt,
                record.accepted_at,
                record.upload_receipt,
                record.response,
                record.session_id_before,
            )
        ):
            return recovery_required(
                "attachment_upload_boundary_ambiguous",
                "The UPLOADING request contains durable Send-boundary evidence and cannot be replayed.",
                "Inspect the exact durable request; do not upload, rebind, or send it again.",
            )

        role = str(hop.get("target_role") or "").upper()
        role_record = state.get("roles", {}).get(role)
        attachments = state.get("attachments")
        if not isinstance(role_record, Mapping) or not isinstance(attachments, list):
            return recovery_required(
                "attachment_upload_identity_mismatch",
                "The failed upload no longer has exact role or attachment snapshot metadata.",
                "Restore the immutable task attachment snapshot before Resume.",
            )
        try:
            state_files = tuple(dict(item) for item in attachments)
        except (TypeError, ValueError):
            state_files = ()
        if state_files != tuple(item.to_dict() for item in record.files):
            return recovery_required(
                "attachment_upload_identity_mismatch",
                "The failed upload durable file identities do not match the task attachment snapshot.",
                "Restore the immutable task attachment snapshot before Resume.",
            )

        expected_source = {
            "task_id": state.get("task_id"),
            "team": state.get("team"),
            "hop_id": hop.get("hop_id"),
            "manifest": state.get("manifest_path"),
        }
        prompt = str(hop.get("prompt") or "")
        if (
            record.request_id != str(hop.get("request_id") or "")
            or record.role != str(hop.get("physical_role") or "")
            or record.source_context != expected_source
            or record.prompt != prompt
            or record.rendered_prompt != prompt
        ):
            return recovery_required(
                "attachment_upload_identity_mismatch",
                "The failed upload prompt or request identity no longer matches this hop.",
                "Restore the exact durable request identity before Resume.",
            )

        expected_page_id = str(role_record.get("page_id") or "").strip()
        expected_page_url = str(role_record.get("page_url") or "").strip()
        if not expected_page_id or not expected_page_url:
            return recovery_required(
                "attachment_upload_owner_missing",
                "The failed upload has no exact live role page to continue safely.",
                "Restore the exact owned role tab; do not create or rebind a replacement chat.",
            )
        try:
            acquired = await actions.locate_owned(state, role)
        except Exception as exc:
            return recovery_required(
                "attachment_upload_owner_missing",
                sanitize_exception(exc),
                "Restore the exact owned role tab; do not create or rebind a replacement chat.",
            )
        if (
            acquired is None
            or str(acquired.page_id) != expected_page_id
            or str(acquired.url) != expected_page_url
        ):
            return recovery_required(
                "attachment_upload_owner_missing",
                "The failed upload is not attached to the exact recorded live role page.",
                "Restore the exact owned role tab; do not create or rebind a replacement chat.",
            )
        try:
            snapshot = await acquired.client.assert_ownership()
        except Exception as exc:
            return recovery_required(
                "attachment_upload_owner_missing",
                sanitize_exception(exc),
                "Restore exact page ownership before Resume.",
            )
        if (
            str(getattr(snapshot, "page_id", "") or "") != expected_page_id
            or str(getattr(snapshot, "page_task_id", "") or "")
            != str(state.get("task_id") or "")
            or str(getattr(snapshot, "page_team", "") or "")
            != str(state.get("team") or "")
        ):
            return recovery_required(
                "attachment_upload_owner_mismatch",
                "The live page ownership does not match the failed upload task/team/page identity.",
                "Restore the exact owned role tab; preserve the current composer unchanged.",
            )
        recovery = classify_recovery_state(record, snapshot)
        if recovery not in {
            DurableRecoveryState.COMPOSER_PROMPT_MISSING_ATTACHMENTS,
            DurableRecoveryState.UPLOAD_READY_NOT_SENT,
        }:
            return recovery_required(
                "attachment_upload_recovery_ambiguous",
                f"The failed upload composer is {recovery.value}; automatic continuation is unsafe.",
                "Preserve the current composer and attachments; resolve ownership ambiguity manually.",
            )
        if recovery is DurableRecoveryState.COMPOSER_PROMPT_MISSING_ATTACHMENTS:
            authorization_prefix = f"{DurableSendBlock.UPLOAD_RETRY_AUTHORIZATION}:"
            retry_authorized = str(record.error or "").startswith(
                authorization_prefix
            )
            if not retry_authorized and not str(record.error or "").strip():
                return recovery_required(
                    "attachment_upload_outcome_ambiguous",
                    "The UPLOADING request has no completed failure evidence; the prior browser upload outcome is unknown.",
                    "Do not re-upload automatically. Restore explicit failure/readiness evidence before Resume.",
                )
            if not retry_authorized:
                control_id = control.get("control_id")
                if not isinstance(control_id, int) or isinstance(control_id, bool):
                    return recovery_required(
                        "attachment_upload_retry_unauthorized",
                        "The failed upload retry has no durable Resume control identity.",
                        "Issue one normal operator Resume for this exact failed upload.",
                    )
                record = ledger.update(
                    record.request_id,
                    error=f"{authorization_prefix}{control_id}",
                )

        self._record_acquired(state, role, acquired)
        hop["conversation_url"] = acquired.url
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(role)
        state["active_action"] = "send"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        self._finish_resume_control(
            state,
            control,
            outcome="continued",
            action="continue_failed_upload",
            reason_code=None,
            reason="The exact failed pre-acceptance upload remains owned and can continue on the same durable request.",
            postcondition="same_request_upload_recovery_ready",
        )
        return True

    async def _recover_resume_sending(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
        *,
        persist_admission: Callable[[], None] | None = None,
    ) -> None:
        ledger = RequestLedger(str(hop.get("ledger_path") or ""))
        ledger_exists = ledger.path.exists()
        record = ledger.get(str(hop.get("request_id") or "")) if ledger_exists else None
        if record is None:
            await self._recover_pristine_preboundary_sending(
                state, hop, control, actions
            )
            return
        if await self._recover_failed_upload_before_ready(
            state, hop, control, actions, ledger, record
        ):
            return
        if self._is_pristine_preboundary_sending_record(state, hop, record):
            if (
                hop.get("kind") == "execution_recovery"
                and len(str(hop.get("prompt") or "")) > _EXECUTION_RECOVERY_PROMPT_MAX_CHARS
            ):
                hop["rebuild_prompt_after_relocation"] = True
            await self._recover_pristine_preboundary_sending(
                state, hop, control, actions
            )
            return
        if record is None or record.status is not RequestStatus.SENDING:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_provenance_missing",
                reason="Resume cannot reconcile the sending hop from an exact durable SENDING record.",
                next_safe_action="Restore the durable request record; do not resend the prompt.",
            )
            return
        source = record.source_context if isinstance(record.source_context, Mapping) else {}
        if (
            source.get("task_id") != state.get("task_id")
            or source.get("team") != state.get("team")
            or source.get("hop_id") != hop.get("hop_id")
            or str(source.get("manifest") or "") != str(state.get("manifest_path") or "")
            or record.binding is None
            or record.baseline is None
        ):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_binding_mismatch",
                reason="The durable SENDING record does not exactly bind to this task, team, hop, and manifest.",
                next_safe_action="Repair the durable binding before attempting continuation.",
            )
            return
        def accept_proven(receipt: SendReceipt, reason: str) -> None:
            self._record_resume_send_acceptance(state, hop, ledger, record, receipt)
            self._canonicalize_receipt_conversation_url(state, hop)
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="observe_progress",
                reason_code=None,
                reason=reason,
                postcondition="generation_progress",
            )

        if record.receipt is not None:
            try:
                durable_receipt = SendReceipt.from_dict(record.receipt)
            except Exception:
                durable_receipt = None
            if durable_receipt is not None and (
                durable_receipt.prompt == record.rendered_prompt
                and durable_receipt.binding == record.binding
                and durable_receipt.baseline == record.baseline
                and durable_receipt.attempts == max(1, int(record.attempts or 0))
                and (durable_receipt.user_message_id or durable_receipt.user_turn_id)
            ):
                accept_proven(
                    durable_receipt,
                    "The durable accepted receipt proves SENDING acceptance; no Send was replayed.",
                )
                return
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="sending_provenance_ambiguous",
                reason="A durable accepted receipt exists but does not exactly match this SENDING record.",
                next_safe_action="Repair the crossed receipt provenance; never rebind or replay it.",
            )
            return

        session_id = str(record.session_id_before or "").strip()
        provisional_session = session_id.startswith("WEB:")
        role = str(hop["target_role"])
        # Recovery never fetches full conversation history to infer whether a
        # SENDING boundary crossed. The exact owned page/durable record below is
        # the only admissible acceptance evidence; ambiguity remains fail-closed.

        acquired: AcquiredRole | None = None
        owned_lookup_performed = False
        if provisional_session:
            owned_lookup_performed = True
            try:
                acquired = await actions.locate_owned(state, role)
            except (RoleOwnershipError, PageOwnershipError) as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="sending_provenance_ambiguous",
                    reason=sanitize_exception(exc),
                    next_safe_action="Restore the exact provisional page binding; do not recreate or resend.",
                )
                return
            if acquired is not None:
                if (
                    getattr(acquired.client, "binding", None) != record.binding
                    or str(acquired.page_id) != str(record.binding.page_id)
                ):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="durable_send_binding_mismatch",
                        reason="The canonicalized live page does not match the durable provisional SENDING binding.",
                        next_safe_action="Restore the exact physical/logical page binding; do not resend.",
                    )
                    return
                snapshot = await acquired.client.assert_ownership()
                if (
                    str(getattr(snapshot, "page_id", "") or "")
                    != str(record.binding.page_id)
                    or str(getattr(snapshot, "page_role", "") or "")
                    != str(record.binding.role)
                    or getattr(snapshot, "page_task_id", None) != state.get("task_id")
                    or getattr(snapshot, "page_team", None) != state.get("team")
                ):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason="The canonicalized live page ownership does not exactly match the durable provisional request.",
                        next_safe_action="Restore the exact page/task/team binding; do not rebind or resend.",
                    )
                    return
                live_values = [
                    acquired.url,
                    getattr(snapshot, "url", None),
                    getattr(snapshot, "conversation_url", None),
                ]
                snapshot_session = str(getattr(snapshot, "session_id", "") or "").strip()
                if snapshot_session:
                    live_values.append(f"https://chatgpt.com/c/{snapshot_session}")
                live_backend_ids = {
                    identity
                    for value in live_values
                    if (identity := _recoverable_conversation_identity(value)) is not None
                }
                if len(live_backend_ids) != 1:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason=(
                            "The exact provisional SENDING page has no unique canonical local identity "
                            "with positive acceptance provenance."
                        ),
                        next_safe_action="Preserve the exact page/request and restore unique local provenance; do not resend.",
                    )
                    return
                canonical_identity = next(iter(live_backend_ids))
                canonical_conversation_id = canonical_identity.rsplit("/", 1)[-1]
                accepted_user = unique_new_user_message(snapshot.messages, record.baseline)
                if accepted_user is None or not rendered_user_text_matches(
                    accepted_user.text, record.rendered_prompt
                ):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason="The exact provisional page does not prove one matching post-baseline user turn.",
                        next_safe_action="Preserve the exact canonicalized page and request; do not resend.",
                    )
                    return
                accepted_id = str(accepted_user.message_id or "").strip()
                if not accepted_id:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason="The matching local user turn lacks an exact message identity.",
                        next_safe_action="Preserve the exact page/request and restore message provenance; do not resend.",
                    )
                    return
                accept_proven(
                    SendReceipt(
                        prompt=record.rendered_prompt,
                        prompt_sha256=_sha(record.rendered_prompt),
                        binding=record.binding,
                        baseline=record.baseline,
                        attempts=max(1, int(record.attempts or 0)),
                        accepted_via="user_message_identity",
                        session_id_before=record.session_id_before,
                        user_message_id=accepted_id,
                        conversation_id=canonical_conversation_id,
                    ),
                    "Same-page canonical local provenance proves provisional SENDING acceptance; no Send was replayed.",
                )
                return

        if not owned_lookup_performed:
            try:
                acquired = await actions.locate_owned(state, role)
            except (RoleOwnershipError, PageOwnershipError) as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="sending_provenance_ambiguous",
                    reason=sanitize_exception(exc),
                    next_safe_action="Restore positive accepted/non-accepted provenance; do not recreate or resend.",
                )
                return
        if acquired is None:
            if any(
                str(record.error or "").startswith(prefix)
                for prefix in _PROVEN_ATOMIC_NONACCEPTANCE_ERRORS
            ):
                await self._recover_lost_sending_nonacceptance(
                    state,
                    hop,
                    control,
                    actions,
                    ledger,
                    record,
                    persist_admission=persist_admission,
                )
                return
            if self._endpoint_pool is not None:
                endpoint_id = self._execution_endpoint_id(state, role) or self.store.endpoint_id_for(state)
                if self._prepare_cross_endpoint_recovery(
                    state,
                    endpoint_id,
                    reason="The exact SENDING page is gone and acceptance/non-acceptance is unproven.",
                    force_new_generation=True,
                ):
                    self._finish_resume_control(
                        state,
                        control,
                        outcome="continued",
                        action="recover_execution",
                        reason_code=None,
                        reason="Durable recovery continuation was queued without replaying the ambiguous request.",
                        postcondition="recovery_continuation_queued",
                    )
                    return
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="sending_provenance_ambiguous",
                reason="The exact SENDING page is gone and acceptance/non-acceptance is unproven.",
                next_safe_action="Restore positive provenance; do not reopen, branch, Restart, New Chat, or resend.",
            )
            return
        await self._dismiss_known_rate_limit_on_existing(acquired)
        if getattr(acquired.client, "binding", None) != record.binding:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="durable_send_binding_mismatch",
                reason="The live client binding does not match the durable SENDING binding.",
                next_safe_action="Restore the exact physical/logical tab binding.",
            )
            return
        snapshot = await acquired.client.assert_ownership()
        if (
            getattr(snapshot, "page_task_id", None) != state.get("task_id")
            or getattr(snapshot, "page_team", None) != state.get("team")
        ):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="task_binding_mismatch",
                reason="The live page task/team binding does not match the durable request.",
                next_safe_action="Restore the exact task/team binding before Resume.",
            )
            return
        accepted_user = unique_new_user_message(snapshot.messages, record.baseline)
        if accepted_user is not None and rendered_user_text_matches(
            accepted_user.text, record.rendered_prompt
        ):
            receipt = SendReceipt(
                prompt=record.rendered_prompt,
                prompt_sha256=_sha(record.rendered_prompt),
                binding=record.binding,
                baseline=record.baseline,
                attempts=min(2, max(1, int(record.attempts or 1))),
                accepted_via="user_message_identity",
                session_id_before=record.session_id_before,
                user_message_id=accepted_user.message_id,
                user_turn_id=accepted_user.turn_id,
            )
            self._record_resume_send_acceptance(state, hop, ledger, record, receipt)
            try:
                await acquired.client.clear_exact_owned_composer(record.rendered_prompt)
            except Exception:
                pass
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="observe_progress",
                reason_code=None,
                reason="The transcript proves that the durable SENDING request was already accepted.",
                postcondition="generation_progress",
            )
            return

        if str(record.error or "").startswith(_SENDING_CONTINUATION_STARTED):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="sending_provenance_ambiguous",
                reason=(
                    "The bounded continuation was durably marked started, but no positive "
                    "acceptance provenance is available; automatic Send replay is unsafe."
                ),
                next_safe_action=(
                    "Preserve the exact page/request and restore positive acceptance provenance; "
                    "do not Send again."
                ),
            )
            return

        proven_nonacceptance = any(
            str(record.error or "").startswith(prefix)
            for prefix in _PROVEN_ATOMIC_NONACCEPTANCE_ERRORS
        )
        if not proven_nonacceptance:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="sending_provenance_ambiguous",
                reason=(
                    "No durable user-turn or accepted assistant-output provenance proves this "
                    "SENDING request was accepted; automatic Send replay is unsafe."
                ),
                next_safe_action="Preserve the exact page/request and do not Send again.",
            )
            return

        expected_names = tuple(item.name for item in record.files)
        actual_names = tuple(getattr(snapshot, "attachment_markers", ()) or ())
        if not visible_text_matches(
            getattr(snapshot, "composer_text", ""), record.rendered_prompt
        ):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="manual_composer_conflict",
                reason="The composer does not match the exact persisted durable prompt.",
                next_safe_action="Preserve the composer and resolve the manual-input mismatch.",
            )
            return
        if capture_message_baseline(
            snapshot.messages, snapshot.logical_turn_ids
        ) != record.baseline:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="transcript_baseline_mismatch",
                reason="The transcript baseline changed before the durable SENDING draft was accepted.",
                next_safe_action="Inspect the transcript; do not send until accepted-turn provenance is unambiguous.",
            )
            return
        if not attachment_names_match(actual_names, expected_names):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="attachment_mismatch",
                reason="The live attachment set does not match the durable SENDING record.",
                next_safe_action="Restore the exact owned attachments without clearing or replacing them.",
            )
            return
        if (
            not bool(getattr(snapshot, "send_enabled", False))
            or bool(getattr(snapshot, "stop_visible", False))
            or tuple(getattr(snapshot, "blocking_dialogs", ()) or ())
        ):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="send_not_ready",
                reason="The exact durable draft is not in a safe send-ready state.",
                next_safe_action="Restore a clean exact draft with Send enabled, then Resume again.",
            )
            return

        ownership_token: str | None = None
        if record.files:
            try:
                upload_receipt = UploadReceipt.from_dict(dict(record.upload_receipt or {}))
            except Exception:
                upload_receipt = None
            if (
                upload_receipt is None
                or upload_receipt.files != record.files
                or upload_receipt.attachment_count != len(record.files)
                or not upload_receipt.ownership_token
            ):
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="attachment_ownership_missing",
                    reason="The durable attachment ownership receipt is missing or inconsistent.",
                    next_safe_action="Restore exact attachment ownership before Resume.",
                )
                return
            ownership_token = upload_receipt.ownership_token
        try:
            async def resume_send() -> Any:
                return await acquired.client.send(
                    record.rendered_prompt,
                    wait_for_stop=False,
                    max_attempts=1,
                    recovery_reload=False,
                    expected_task_id=str(state["task_id"]),
                    expected_team=str(state["team"]),
                    expected_attachment_ownership_token=ownership_token,
                    expected_attachment_count=len(record.files),
                    expected_attachment_names=expected_names,
                )

            receipt = await self._run_automated_send(resume_send)
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            self._require_resume_recovery(
                state,
                control,
                action="accept_owned_draft",
                reason_code="send_acceptance_ambiguous",
                reason=sanitize_exception(exc),
                next_safe_action="Inspect durable/backend provenance; do not send again.",
            )
            return
        except (ComposerConflictError, PageOwnershipError, TaskBindingError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="accept_owned_draft",
                reason_code="manual_composer_conflict"
                if isinstance(exc, ComposerConflictError)
                else "ownership_conflict",
                reason=sanitize_exception(exc),
                next_safe_action="Restore exact ownership and draft evidence before Resume.",
            )
            return
        if (
            receipt.binding != record.binding
            or receipt.baseline != record.baseline
            or not (receipt.user_message_id or receipt.user_turn_id)
        ):
            self._require_resume_recovery(
                state,
                control,
                action="accept_owned_draft",
                reason_code="send_acceptance_ambiguous",
                reason="The bounded send did not return exact accepted-user provenance.",
                next_safe_action="Inspect the transcript; do not click Send again.",
            )
            return
        self._record_resume_send_acceptance(state, hop, ledger, record, receipt)
        self._finish_resume_control(
            state,
            control,
            outcome="continued",
            action="accept_owned_draft",
            reason_code=None,
            reason="The exact owned durable draft was accepted once.",
            postcondition="draft_accepted_once",
        )

    async def _recover_resume_control(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
        *,
        persist_admission: Callable[[], None] | None = None,
    ) -> None:
        hop = _active_hop(state)
        hop_state = str(hop.get("state") or "")
        try:
            if hop_state == "pre_send":
                await self._acquire_endpoint_admission(
                    state, str(hop["target_role"])
                )
                before_hop = command_snapshot(state, role=str(hop["target_role"]))
                await self._pre_send(state, hop, actions)
                after_hop = command_snapshot(state, role=str(hop["target_role"]))
                if state.get("status") == "BLOCKED" or before_hop == after_hop:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="prepare_hop",
                        reason_code=str(state.get("block_code") or "resume_progress_unverified"),
                        reason=str(
                            state.get("block_reason")
                            or "Resume did not advance the active pre-send hop."
                        ),
                        next_safe_action="Resolve the active ownership or composer evidence, then Resume again.",
                    )
                    return
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="prepare_hop",
                    reason_code=None,
                    reason="The active hop advanced from pre-send under exact ownership.",
                    postcondition="hop_advanced",
                )
                return
            if hop_state == "sending":
                await self._recover_resume_sending(
                    state,
                    hop,
                    control,
                    actions,
                    persist_admission=persist_admission,
                )
                return
            if hop_state == "sent":
                if self._reconcile_pristine_sent_projection(state, hop):
                    await self._recover_resume_sending(state, hop, control, actions)
                    return
                self._start_wait_budget_from_sent(hop)
                hop["state"] = "waiting"
                hop.setdefault("timestamps", {})["waiting_at"] = utc_now()
                state["active_action"] = "wait_response"
                await self._recover_resume_waiting(state, hop, control, actions)
                return
            if hop_state == "waiting":
                await self._recover_resume_waiting(state, hop, control, actions)
                return
            if (
                hop_state == "routed"
                and str(state.get("workflow_mode") or "team").strip().lower()
                == "team"
                and str(hop.get("route") or "").strip().upper() == "PAUSE"
            ):
                control_origin = str(control.get("origin") or "operator").strip().lower()
                if control_origin != "operator":
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="role_pause_requires_operator",
                        reason="Role-initiated PAUSE requires explicit operator Resume.",
                        next_safe_action="Resume this task explicitly as an operator after the prerequisite is satisfied.",
                    )
                    return
                role = str(hop.get("target_role") or "").strip().upper()
                handoff = str(
                    hop.get("report_path")
                    or hop.get("expected_report_path")
                    or hop.get("routed_handoff")
                    or ""
                ).strip()
                if role not in state.get("roles", {}) or not handoff:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="role_pause_handoff_missing",
                        reason="The routed PAUSE lost its persisted role/report handoff.",
                        next_safe_action="Restore the durable PAUSE report/handoff before resuming.",
                    )
                    return
                child = self._append_hop(
                    state,
                    source_role=role,
                    target_role=role,
                    handoff=handoff,
                )
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(role)
                state["pause_reason"] = None
                state["block_code"] = None
                state["block_retryable"] = False
                state["block_reason"] = None
                state["active_action"] = "queued"
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="release_role_pause",
                    reason_code=None,
                    reason=(
                        "Explicit operator Resume released the routed role PAUSE after "
                        "its durable report was reconciled."
                    ),
                    postcondition="child_hop_appended",
                )
                return
            if (
                hop_state == "routed"
                and str(state.get("workflow_mode") or "team").strip().lower()
                == "solo"
                and str(hop.get("route") or "").strip().upper() == "PAUSE"
            ):
                role = str(hop["target_role"]).strip().upper()
                handoff = str(
                    hop.get("routed_handoff")
                    or state.get("pause_reason")
                    or "Continue from the current state."
                ).strip()
                child = self._append_hop(
                    state,
                    source_role=role,
                    target_role=role,
                    handoff=handoff,
                    kind="solo_resume",
                )
                durable_url = str(
                    hop.get("conversation_url")
                    or state["roles"][role].get("page_url")
                    or ""
                ).strip()
                if durable_url:
                    child["conversation_url"] = durable_url
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(role)
                state["pause_reason"] = None
                state["active_action"] = "queued"
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="resume_solo_role",
                    reason_code=None,
                    reason="Solo PAUSE resumed on the same role and durable conversation.",
                    postcondition="child_hop_appended",
                )
                return
            if hop_state == "responded":
                before_hop_id = state.get("active_hop_id")
                before_status = str(state.get("status") or "")
                before_block = str(state.get("block_code") or "")
                self._responded(state, hop)
                progressed = (
                    state.get("active_hop_id") != before_hop_id
                    or str(state.get("status") or "") != before_status
                    or str(state.get("block_code") or "") != before_block
                )
                if not progressed:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="consume_response",
                        reason_code="resume_progress_unverified",
                        reason=(
                            "Resume consumed no new durable postcondition; the same "
                            "hop and failure state remain active."
                        ),
                        next_safe_action=(
                            "Resolve the active prerequisite before another Resume."
                        ),
                    )
                    return
                if state.get("status") == "BLOCKED":
                    self._require_resume_recovery(
                        state,
                        control,
                        action="consume_response",
                        reason_code=str(
                            state.get("block_code") or "resume_progress_unverified"
                        ),
                        reason=str(
                            state.get("block_reason")
                            or "Response consumption reached another blocked state."
                        ),
                        next_safe_action=(
                            "Resolve the newly classified prerequisite before Resume."
                        ),
                    )
                    return
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="consume_response",
                    reason_code=None,
                    reason="The persisted assistant response established a new durable postcondition.",
                    postcondition=(
                        "hop_advanced"
                        if state.get("active_hop_id") != before_hop_id
                        else "state_advanced"
                    ),
                )
                return
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="resume_state_unsupported",
                reason=f"Resume cannot safely continue hop state {hop_state!r}.",
                next_safe_action="Inspect the active hop and restore a supported durable state.",
            )
        except (UnsafePageStateError, TimeoutError, PlaywrightTimeoutError) as exc:
            if not self._should_observe_existing_response(exc):
                raise
            self._observe_existing_response(state, hop)
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="observe_existing_response",
                reason_code=None,
                reason="The exact role page is already active; CDPA will observe and route that response without sending again.",
                postcondition="response_observation_started",
            )
            return
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            self._apply_rate_limit_to_state(state, hop)
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="defer_tab_open",
                reason_code=None,
                reason="New tab creation is paused for the request-rate-limit cooldown; existing tabs remain runnable.",
                postcondition="tab_open_deferred",
            )
        except (ComposerConflictError, ManualInputPendingError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="manual_composer_conflict",
                reason=sanitize_exception(exc),
                next_safe_action="Resolve manual input or attachments without overwriting them.",
            )
        except (RoleOwnershipError, PageOwnershipError, TaskBindingError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="reopen_exact_tab",
                reason_code="role_offline",
                reason=sanitize_exception(exc),
                next_safe_action="Open or rebind the exact recorded role tab, then Resume again.",
            )
        except Exception as exc:
            self._block(
                state,
                sanitize_exception(exc),
                code="resume_recovery_failed",
                retryable=False,
            )
            self._finish_resume_control(
                state,
                control,
                outcome="failed",
                action="none",
                reason_code="resume_recovery_failed",
                reason=sanitize_exception(exc),
                next_safe_action="Inspect the recorded recovery evidence before another Resume attempt.",
                postcondition=None,
            )

    def _terminalize_controls_superseded_by_stop(
        self,
        state: dict[str, Any],
        stop_control: Mapping[str, Any],
    ) -> None:
        stop_id = stop_control.get("control_id")
        if not isinstance(stop_id, int):
            return
        for prior in state.get("controls") or []:
            if not isinstance(prior, dict):
                continue
            prior_id = prior.get("control_id")
            if not isinstance(prior_id, int) or prior_id >= stop_id:
                continue
            if prior.get("status") != "requested":
                continue
            command_ids: list[str] = []
            external = str(prior.get("external_command_id") or "").strip()
            if external:
                command_ids.append(external)
            for item in prior.get("external_commands") or []:
                if isinstance(item, Mapping):
                    command_id = str(item.get("command_id") or "").strip()
                    if command_id and command_id not in command_ids:
                        command_ids.append(command_id)
            terminal_command = next(
                (
                    command
                    for command_id in command_ids
                    if (command := self.runtime_db.get_command(command_id)) is not None
                    and command.get("status")
                    in {"applied", "recovery_required", "failed"}
                ),
                None,
            )
            if terminal_command is not None:
                terminal_status = str(terminal_command["status"])
                prior["status"] = terminal_status
                prior["command_state"] = terminal_status.upper()
                terminal_result = terminal_command.get("result")
                prior["result"] = (
                    dict(terminal_result)
                    if isinstance(terminal_result, Mapping)
                    else {
                        "outcome": terminal_status,
                        "action": str(prior.get("action") or ""),
                        "reason": str(terminal_command.get("error") or ""),
                    }
                )
                prior["applied_at"] = terminal_command.get("finished_at") or utc_now()
                continue
            prior["status"] = "rejected"
            prior["command_state"] = "REJECTED"
            prior["result"] = {
                "outcome": "rejected",
                "action": str(prior.get("action") or ""),
                "reason_code": "superseded_by_stop",
                "reason": "Superseded by a later explicit operator Stop.",
                "postcondition": "superseded_by_stop",
            }
            prior["applied_at"] = utc_now()

    def _sync_control_commands(self, state: Mapping[str, Any]) -> None:
        if not self.runtime_db.path.exists():
            return
        for control in state.get("controls") or []:
            if not isinstance(control, Mapping):
                continue
            status = str(control.get("status") or "")
            if status not in {
                "applied",
                "recovery_required",
                "failed",
                "rejected",
                "ineffective",
            }:
                continue
            command_ids: list[str] = []
            external = str(control.get("external_command_id") or "").strip()
            if external:
                command_ids.append(external)
            for item in control.get("external_commands") or []:
                if isinstance(item, Mapping):
                    command_id = str(item.get("command_id") or "").strip()
                    if command_id and command_id not in command_ids:
                        command_ids.append(command_id)
            result = dict(control.get("result") or {}) if isinstance(
                control.get("result"), Mapping
            ) else {"outcome": "failed", "reason": str(control.get("result") or "")}
            result.update(
                task_id=state.get("task_id"),
                team=state.get("team"),
            )
            for command_id in command_ids:
                command = self.runtime_db.get_command(command_id)
                if command is None or command.get("status") not in {"queued", "running"}:
                    continue
                if status == "applied":
                    self.runtime_db.finish_command(
                        command_id,
                        result=result,
                    )
                elif status == "recovery_required":
                    self.runtime_db.require_command_recovery(
                        command_id,
                        error=str(result.get("reason") or "Resume recovery required"),
                        result=result,
                    )
                else:
                    self.runtime_db.finish_command(
                        command_id,
                        result=result,
                        error=str(result.get("reason") or "Resume failed"),
                    )

    async def advance(
        self,
        manifest_path: str | Path,
        browser_context: Any,
        *,
        scheduling_tasks: Sequence[Mapping[str, Any]] | None = None,
        endpoint_id: str | None = None,
        expected_endpointless_control: tuple[int, str] | None = None,
    ) -> dict[str, Any] | None:
        path = Path(manifest_path).resolve()
        try:
            with self.store.task_run_lock(path, blocking=False):
                try:
                    loaded_state = self._load_manifest_cached(path)
                    state, scheduling_changed = await asyncio.to_thread(
                        self.store.refresh_scheduling,
                        path,
                        tasks=scheduling_tasks,
                        state=loaded_state,
                    )
                    if scheduling_changed:
                        self._remember_manifest(path, state)
                except Exception as exc:
                    failure_baseline = self._load_manifest_cached(path, force=True)
                    state = json.loads(
                        json.dumps(failure_baseline, ensure_ascii=False, default=str)
                    )
                    queue = state.get("queue")
                    code = (
                        "team_owner_conflict"
                        if "multiple active owners" in str(exc)
                        else "queue_release_failed"
                        if isinstance(queue, Mapping)
                        and queue.get("reuse_team") is True
                        and queue.get("released_at") is None
                        else "dependency_invalid"
                    )
                    if code in {"team_owner_conflict", "queue_release_failed"}:
                        changed = self._wait_queue_error(
                            state,
                            f"{type(exc).__name__}: {exc}",
                            code=code,
                        )
                        if not changed:
                            return self._load_manifest_cached(path)
                    else:
                        self._block(
                            state,
                            f"{type(exc).__name__}: {exc}",
                            code=code,
                            retryable=False,
                        )
                    saved = self._persist_transport_result(
                        path,
                        failure_baseline,
                        state,
                    )
                    state.clear()
                    state.update(saved)
                    await self._release_blocked_workflow_tabs(
                        saved,
                        CDPATabActions(browser_context, self.config),
                    )
                    return saved
                if expected_endpointless_control is not None:
                    expected_control_id, expected_action = expected_endpointless_control
                    pending_control = _next_requested_control(state)
                    if (
                        pending_control is None
                        or pending_control.get("control_id") != expected_control_id
                        or str(pending_control.get("action") or "") != expected_action
                        or not _control_can_run_endpointless(state, pending_control)
                    ):
                        return state
                recovering_resume_present = _next_recovering_resume_control(state) is not None
                requested_control = _next_requested_control(state) is not None
                control_in_flight = requested_control or recovering_resume_present
                if scheduling_changed and not control_in_flight:
                    return state
                if state.get("status") == "WAITING" and not control_in_flight:
                    return state
                transport_baseline = state
                active_hop_id = state.get("active_hop_id")
                initial_hop = (
                    _active_hop(state) if active_hop_id is not None else None
                )
                if (
                    initial_hop is not None
                    and str(initial_hop.get("state") or "") == "waiting"
                ):
                    state = _waiting_working_copy(state)
                else:
                    state = json.loads(
                        json.dumps(state, ensure_ascii=False, default=str)
                    )
                current_hop = _active_hop(state) if state.get("active_hop_id") is not None else None
                pending_forensic_control = _next_requested_control(state)
                stop_forensic_control = (
                    str(state.get("status") or "").upper() == "BLOCKED"
                    and str(state.get("block_code") or "")
                    == "accepted_admission_integrity_corruption"
                    and isinstance(pending_forensic_control, Mapping)
                    and str(pending_forensic_control.get("action") or "") == "stop"
                )
                if isinstance(current_hop, Mapping) and not stop_forensic_control:
                    integrity_error = self._accepted_admission_integrity_error(
                        state, current_hop
                    )
                    if integrity_error is not None:
                        self._block(
                            state,
                            integrity_error,
                            code="accepted_admission_integrity_corruption",
                            retryable=False,
                        )
                        saved = self._persist_transport_result(
                            path,
                            transport_baseline,
                            state,
                        )
                        state.clear()
                        state.update(saved)
                        return saved
                if endpoint_id is not None and not self._prepare_execution_endpoint(state, endpoint_id):
                    if not self._prepare_cross_endpoint_recovery(
                        state,
                        endpoint_id,
                        reason="previous endpoint is unavailable for the crossed Send boundary",
                    ):
                        if state.get("status") != "BLOCKED":
                            self._block(state, "No safe execution relocation path: reconcile request and page-owned effects.",
                                        code="execution_recovery_requires_reconciliation", retryable=True)
                        saved = self._persist_transport_result(path, transport_baseline, state)
                        await self._release_blocked_workflow_tabs(
                            saved,
                            CDPATabActions(browser_context, self.config),
                        )
                        return saved
                if endpoint_id is not None and state != transport_baseline:
                    saved_endpoint_state = self._persist_transport_result(
                        path, transport_baseline, state
                    )
                    transport_baseline = saved_endpoint_state
                    state = json.loads(
                        json.dumps(saved_endpoint_state, ensure_ascii=False, default=str)
                    )
                actions = CDPATabActions(browser_context, self.config)
                pending_before_blocked_release = _next_requested_control(state)
                if (
                    state.get("status") == "BLOCKED"
                    and pending_before_blocked_release is None
                ):
                    await self._release_blocked_workflow_tabs(state, actions)
                if state.get("cleanup", {}).get("state") == "CLEARING":
                    requested_clear = next(
                        (
                            item
                            for item in state.get("controls") or []
                            if isinstance(item, Mapping)
                            and item.get("status") == "requested"
                            and item.get("action") == "clear_team"
                        ),
                        None,
                    )
                    if requested_clear is not None:
                        persisted_control: list[bool] = []
                        await self._apply_control(
                            state,
                            actions,
                            path,
                            scheduling_tasks=scheduling_tasks,
                            persisted_result=persisted_control,
                        )
                    else:
                        await self._continue_cleanup(state, actions, path)
                    saved = self.store.load(path)
                    self._sync_control_commands(saved)
                    return saved
                pending_control = _next_requested_control(state)
                recovering_resume = _next_recovering_resume_control(state)
                pending_control_id = (
                    pending_control.get("control_id") if pending_control is not None else None
                )
                pending_action = (
                    str(pending_control.get("action") or "")
                    if pending_control is not None
                    else ""
                )
                resumed_control = recovering_resume if pending_control is None else None
                resume_baseline: dict[str, Any] | None = (
                    json.loads(json.dumps(state, ensure_ascii=False, default=str))
                    if resumed_control is not None
                    else None
                )
                persisted_control: list[bool] = []
                if pending_action in {"open_tab", "restart_role", "new_chat"}:
                    control_role = str(
                        (pending_control or {}).get("role")
                        or state.get("active_role")
                        or "PLAN"
                    ).upper()
                    control_endpoint_id = (
                        self._execution_endpoint_id(state, control_role) or endpoint_id
                    )
                    await self._acquire_endpoint_admission(
                        state,
                        control_role,
                        endpoint_id=control_endpoint_id,
                    )
                if await self._apply_control(
                    state,
                    actions,
                    path,
                    scheduling_tasks=scheduling_tasks,
                    persisted_result=persisted_control,
                ):
                    applied_control = next(
                        (
                            item
                            for item in state.get("controls") or []
                            if isinstance(item, dict)
                            and item.get("control_id") == pending_control_id
                        ),
                        None,
                    )
                    if (
                        pending_action == "resume"
                        and applied_control is not None
                        and applied_control.get("status") == "recovering"
                        and state.get("status") not in TERMINAL
                    ):
                        resumed_control = applied_control
                        resume_baseline = json.loads(
                            json.dumps(state, ensure_ascii=False, default=str)
                        )
                    elif persisted_control:
                        saved = self._load_manifest_cached(path)
                        self._sync_control_commands(saved)
                        return saved
                    else:
                        raise RuntimeError("control result was not persisted atomically")
                if state.get("status") == "WAITING":
                    return state
                if state.get("status") in TERMINAL:
                    if is_independent_task(state):
                        return state
                    if scheduling_tasks is None:
                        raise RuntimeError("terminal cleanup requires runtime registry tasks")
                    queued_team_work = has_other_nonterminal_team_work(
                        scheduling_tasks,
                        str(state.get("team") or ""),
                        exclude_task_id=str(state.get("task_id") or ""),
                    )
                    if (
                        not state.get("cleanup", {}).get("cleared_at")
                        and self._cleanup_retry_due(state.get("cleanup", {}))
                        and cleanup_eligible(
                            state,
                            idle_seconds=self.config.cleanup_terminal_idle_seconds,
                            queued_team_work=queued_team_work,
                        )
                    ):
                        try:
                            selected_pages = await actions.preflight_team(state)
                        except Exception as exc:
                            def record_failure(current: dict[str, Any]) -> dict[str, Any]:
                                self._record_cleanup_failure(current, exc)
                                return current

                            return self.store.update(path, record_failure)
                        if not self._start_cleanup(
                            state,
                            control=None,
                            target_tabs=len(selected_pages),
                            manifest_path=path,
                        ):
                            return state
                        await self._continue_cleanup(
                            state,
                            actions,
                            path,
                            preflighted_pages=selected_pages,
                        )
                        return self.store.load(path)
                    return state
                if resumed_control is not None:
                    assert resume_baseline is not None

                    def persist_resume_admission() -> None:
                        nonlocal resume_baseline
                        assert resume_baseline is not None
                        persisted = self._persist_control_result(
                            path,
                            resume_baseline,
                            state,
                            control_id=int(resumed_control["control_id"]),
                            action="resume",
                        )
                        resume_baseline = persisted
                        self._release_endpoint_admission()

                    await self._recover_resume_control(
                        state,
                        resumed_control,
                        actions,
                        persist_admission=persist_resume_admission,
                    )
                    saved = self._persist_control_result(
                        path,
                        resume_baseline,
                        state,
                        control_id=int(resumed_control["control_id"]),
                        action="resume",
                    )
                    state.clear()
                    state.update(saved)
                    self._sync_control_commands(saved)
                    return saved
                if state.get("status") in {"PAUSED", "BLOCKED"}:
                    return state
                hop = _active_hop(state)
                completed_hop_id: int | None = None
                try:
                    if hop["state"] == "pre_send":
                        await self._acquire_endpoint_admission(
                            state, str(hop["target_role"])
                        )
                        await self._pre_send(state, hop, actions)
                    elif hop["state"] == "sending":
                        await self._sending(state, hop, actions)
                    elif hop["state"] == "sent":
                        if not self._reconcile_pristine_sent_projection(state, hop):
                            self._start_wait_budget_from_sent(hop)
                            hop["state"] = "waiting"
                            hop["timestamps"]["waiting_at"] = utc_now()
                            state["active_action"] = "wait_response"
                    elif hop["state"] == "waiting":
                        waiting_hop_id = int(hop["hop_id"])
                        await self._waiting(
                            state,
                            hop,
                            actions,
                            path,
                            transport_baseline,
                        )
                        # _waiting() consumes a valid response and routes ordinary
                        # workflow hops in the same tick. Capture that completed
                        # parent here so post-route Project grouping is not skipped.
                        if str(hop.get("state") or "") == "routed":
                            completed_hop_id = waiting_hop_id
                    elif hop["state"] == "responded":
                        completed_hop_id = int(hop["hop_id"])
                        self._responded(state, hop)
                        if is_independent_task(state):
                            pending = state["independent"]
                            settings_reset = pending.get("settings_reset_request")
                            completion = pending.get("completion_request")
                            continuation = pending.get("continuation_request")
                            if settings_reset is not None:
                                reset = self.store.finalize_independent_settings_reset(path)
                                self._publish_command_state(reset)
                                return reset
                            if completion is not None:
                                completed = self.store.complete_independent_task(
                                    path,
                                    outcome=str(completion.get("outcome") or ""),
                                    summary=str(completion.get("summary") or ""),
                                    target_task_id=completion.get("target_task_id"),
                                    repair_task_id=completion.get("repair_task_id"),
                                )
                                self._publish_command_state(completed)
                                return completed
                            if continuation is not None:
                                continued = self.store.continue_independent_task(
                                    path,
                                    reason=str(continuation.get("reason") or ""),
                                )
                                self._publish_command_state(continued)
                                return continued
                    elif hop["state"] == "routed":
                        # A routed parent is complete. The active pointer must already
                        # reference its durable child; otherwise fail closed.
                        if state.get("active_hop_id") == hop.get("hop_id"):
                            self._block(
                                state,
                                "routed hop has no durable child",
                                code="route_state_corrupt",
                                retryable=False,
                            )
                    else:
                        self._block(
                            state,
                            f"unknown hop state {hop.get('state')!r}",
                            code="hop_state_unknown",
                            retryable=False,
                        )
                except RateLimitBlockedError as exc:
                    await self._enter_rate_limit_cooldown(state, actions, exc)
                    self._apply_rate_limit_to_state(state, hop)
                except Exception:
                    raise
                if state == transport_baseline:
                    return transport_baseline
                saved = self._persist_transport_result(path, transport_baseline, state)
                state.clear()
                state.update(saved)
                await self._release_blocked_workflow_tabs(saved, actions)
                if completed_hop_id is not None:
                    project_task = self._schedule_repository_project(
                        saved, completed_hop_id, browser_context
                    )
                    if project_task is not None:
                        await project_task
                return saved
        except BlockingIOError:
            return None
        except Exception as exc:
            if is_cdp_disconnect(exc):
                raise
            try:
                state = self.store.load(path)
                if self._should_observe_existing_response(exc):
                    failure_baseline = json.loads(
                        json.dumps(state, ensure_ascii=False, default=str)
                    )
                    hop = _active_hop(state)
                    if str(hop.get("state") or "") == "pre_send":
                        self._observe_existing_response(state, hop)
                        saved = self._persist_transport_result(
                            path, failure_baseline, state
                        )
                        state.clear()
                        state.update(saved)
                        return saved
                if isinstance(exc, RateLimitBlockedError):
                    failure_baseline = json.loads(
                        json.dumps(state, ensure_ascii=False, default=str)
                    )
                    actions = CDPATabActions(browser_context, self.config)
                    hop = _active_hop(state)
                    await self._enter_rate_limit_cooldown(state, actions, exc)
                    self._apply_rate_limit_to_state(state, hop)
                    saved = self._persist_transport_result(
                        path, failure_baseline, state
                    )
                    state.clear()
                    state.update(saved)
                    return saved
                failure_baseline = json.loads(
                    json.dumps(state, ensure_ascii=False, default=str)
                )
                queue = state.get("queue")
                queue_rebind = (
                    isinstance(queue, Mapping)
                    and queue.get("reuse_team") is True
                    and queue.get("released_at") is not None
                    and str((_active_hop(state)).get("state") or "") == "pre_send"
                )
                self._block(
                    state,
                    f"{type(exc).__name__}: {exc}",
                    code=(
                        _role_ownership_block_code(exc)
                        or ("queue_rebind_failed" if queue_rebind else "unexpected_error")
                    ),
                    retryable=False,
                )
                saved = self._persist_transport_result(
                    path,
                    failure_baseline,
                    state,
                )
                state.clear()
                state.update(saved)
                await self._release_blocked_workflow_tabs(
                    saved,
                    CDPATabActions(browser_context, self.config),
                )
                return saved
            except Exception:
                raise
        finally:
            self._release_endpoint_admission()

    def _activate_independent_agent_command(
        self,
        source: Mapping[str, Any],
        *,
        agent_name: str,
        target_task_id: str,
        command_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not is_independent_task(source):
            raise ValueError("activation source is not an independent task")
        source_independent = source.get("independent")
        if not isinstance(source_independent, Mapping) or not isinstance(
            source_independent.get("active_event"), Mapping
        ):
            raise ValueError("activation source has no active independent job")
        _display, target_key, _team = normalize_agent_name(agent_name)
        source_key = str(source_independent.get("agent_key") or "")
        if target_key == source_key:
            raise ValueError("an independent agent cannot activate itself")
        tasks = self.store.discover()
        targets = [
            item
            for item in tasks
            if is_independent_task(item)
            and str(item.get("status") or "").upper() not in TERMINAL
            and isinstance(item.get("independent"), Mapping)
            and item["independent"].get("agent_key") == target_key
        ]
        if len(targets) != 1:
            raise ValueError("target independent agent is missing or ambiguous")
        target = targets[0]
        target_independent = target["independent"]
        if target_independent.get("enabled") is not True:
            raise ValueError("target independent agent is disabled")
        matching_events = [
            event
            for event in canonical_independent_events(target, tasks)
            if event.get("trigger_type") == "recovery"
            and event.get("target_task_id") == target_task_id
        ]
        active = target_independent.get("active_event")
        if isinstance(active, Mapping):
            if (
                active.get("trigger_type") != "recovery"
                or active.get("target_task_id") != target_task_id
            ):
                raise ValueError("target independent agent already has another active job")
            claimed = target
        else:
            if not matching_events:
                raise ValueError("target task has no eligible canonical recovery event")
            claimed = self.store.update(
                target["manifest_path"],
                lambda current: claim_oldest_event(
                    current,
                    matching_events,
                    all_tasks=tasks,
                ),
            )
            claimed_event = claimed["independent"].get("active_event")
            if not isinstance(claimed_event, Mapping) or (
                claimed_event.get("target_task_id") != target_task_id
            ):
                raise ValueError("canonical recovery event is already claimed")
        recorded = self.store.record_independent_command(
            source["manifest_path"], command_id
        )
        return recorded, claimed

    def _bind_independent_event_target_command(
        self,
        source: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        command_id: str,
    ) -> dict[str, Any]:
        if not is_independent_task(source):
            raise ValueError("bind source is not an independent task")
        return self.store.bind_independent_event_target(
            source["manifest_path"],
            event_key=str(payload.get("event_key") or ""),
            target_task_id=str(payload.get("target_task_id") or ""),
            external_command_id=command_id,
        )

    def _create_independent_repair_command(
        self,
        source: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        command_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not is_independent_task(source):
            raise ValueError("repair source is not an independent task")
        independent = source.get("independent")
        active = independent.get("active_event") if isinstance(independent, Mapping) else None
        if not isinstance(active, Mapping):
            raise ValueError("repair source has no active independent job")
        target_task_id = str(active.get("target_task_id") or "")
        if not target_task_id:
            raise ValueError("active independent event has no repair target")
        affected = self.store.load_task_id(target_task_id)
        if affected is None or is_independent_task(affected):
            raise ValueError("active independent repair target is missing or invalid")
        trigger_type = str(active.get("trigger_type") or "").lower()
        if (
            trigger_type in {"interval", "check_all"}
            and str(affected.get("status") or "").upper() != "BLOCKED"
        ):
            raise ValueError("bound periodic independent repair target is no longer blocked")
        bound_hop_id = active.get("target_hop_id")
        if (
            bound_hop_id is not None
            and affected.get("active_hop_id") != bound_hop_id
        ):
            raise ValueError("active independent repair target hop is stale")
        request = RepairRequest.create(
            root_cause=str(payload.get("root_cause") or ""),
            affected_state=affected,
            repair_repository=str(self.config.repository_root),
            incident_id=str(active.get("event_key") or ""),
            disposition=str(payload.get("disposition") or ""),
            reason=str(payload.get("reason") or ""),
            reproduction=str(payload.get("reproduction") or ""),
            source_areas=tuple(payload.get("source_areas") or ()),
            required_tests=tuple(payload.get("required_tests") or ()),
            lesson=payload.get("lesson"),
        )
        if trigger_type in {"interval", "check_all"}:
            bundle = self.store.create_or_gate_repair(
                request,
                source_manifest_path=source["manifest_path"],
                source_event_key=str(active.get("event_key") or ""),
                require_affected_status="BLOCKED",
            )
        else:
            bundle = self.store.create_or_gate_repair(request)
        recorded = self.store.record_independent_command(
            source["manifest_path"], command_id
        )
        return recorded, bundle["affected"], bundle["repair"]

    def _queue_independent_task_control(
        self,
        source: Mapping[str, Any],
        *,
        payload: Mapping[str, Any],
        command_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not is_independent_task(source):
            raise ValueError("control source is not an independent task")
        independent = source.get("independent")
        active = independent.get("active_event") if isinstance(independent, Mapping) else None
        if not isinstance(active, Mapping):
            raise ValueError("control source has no active independent job")
        target_task_id = str(payload.get("target_task_id") or "").strip()
        if target_task_id != str(active.get("target_task_id") or ""):
            raise ValueError("independent control target is outside the active event")
        action = str(payload.get("action") or "").strip().lower()
        if action not in {
            "resume",
            "retry",
            "restart_role",
            "open_tab",
            "new_chat",
            "route_plan",
            "safe_restore",
        }:
            raise ValueError("unsupported independent task control")
        tasks = self.store.discover()
        event_key = str(active.get("event_key") or "")
        trigger_type = str(active.get("trigger_type") or "").lower()
        durable_periodic_bind = (
            trigger_type in {"interval", "check_all"}
            and bool(str(active.get("target_task_id") or ""))
        )
        if not durable_periodic_bind:
            current_event_keys = {
                str(event.get("event_key") or "")
                for event in canonical_independent_events(source, tasks)
            }
            if event_key not in current_event_keys:
                raise ValueError(
                    "active independent event is no longer eligible for automatic control"
                )
        target = next(
            (item for item in tasks if str(item.get("task_id") or "") == target_task_id),
            None,
        )
        if target is None or is_independent_task(target):
            raise ValueError("independent control target is missing or invalid")
        if durable_periodic_bind and str(target.get("status") or "").upper() != "BLOCKED":
            raise ValueError("bound periodic independent target is no longer blocked")
        bound_hop_id = active.get("target_hop_id")
        if bound_hop_id is not None and target.get("active_hop_id") != bound_hop_id:
            raise ValueError("independent control target hop is stale")

        if action == "safe_restore" and str(target.get("status") or "").upper() == "STOPPED":
            for historical_hop in target.get("hops") or ():
                if not isinstance(historical_hop, Mapping):
                    raise ValueError("safe restore requires valid historical hop provenance")
                historical_state = str(historical_hop.get("state") or "")
                if historical_state in IN_FLIGHT:
                    raise ValueError(
                        "safe restore cannot replace work with an unresolved Send boundary"
                    )
                if (
                    historical_hop.get("receipt") is not None
                    or historical_hop.get("message_identity") is not None
                ):
                    raise ValueError(
                        "safe restore cannot replace work with accepted or ambiguous Send evidence"
                    )
                if historical_state != "pre_send":
                    raise ValueError(
                        "safe restore requires provably pristine pre-Send hop history"
                    )
                request_id = str(historical_hop.get("request_id") or "").strip()
                ledger_path = str(historical_hop.get("ledger_path") or "").strip()
                if bool(request_id) != bool(ledger_path):
                    raise ValueError(
                        "safe restore cannot replace work with incomplete durable request provenance"
                    )
                if request_id:
                    try:
                        durable = RequestLedger(ledger_path).peek(request_id)
                    except Exception as exc:
                        raise ValueError(
                            "safe restore cannot verify the durable Send boundary"
                        ) from exc
                    if durable is not None:
                        raise ValueError(
                            "safe restore cannot replace work with a pending durable request"
                        )
            operator_stop = any(
                isinstance(item, Mapping)
                and str(item.get("action") or "").lower() == "stop"
                and str(
                    item.get("origin")
                    or (
                        (item.get("command") or {}).get("origin")
                        if isinstance(item.get("command"), Mapping)
                        else ""
                    )
                    or "operator"
                ).lower()
                == "operator"
                and str(item.get("status") or "") in {"requested", "applied"}
                for item in target.get("controls") or ()
            )
            if operator_stop:
                raise ValueError("safe restore cannot override an explicit operator Stop")
            source_task_id = str(source.get("task_id") or "")
            incident_id = f"independent-safe-restore:{source_task_id}:{event_key}"
            bundle = self.store.replace_task_and_rewire(
                target_task_id,
                str(payload.get("reason") or "Continue the repaired workflow safely."),
                reuse_team=True,
                rewire_children=True,
                incident_id=incident_id,
                source_task_id=source_task_id,
                source_event_key=event_key,
            )
            recorded = self.store.record_independent_command(
                source["manifest_path"], command_id
            )
            replacement = bundle["replacement"]
            return recorded, replacement, {
                "target_task_id": target_task_id,
                "replacement_task_id": replacement["task_id"],
                "restore_mode": "replacement",
                "control_id": None,
            }

        if durable_periodic_bind:
            recorded, queued, control = self.store.queue_bound_independent_control(
                source["manifest_path"],
                event_key=event_key,
                target_task_id=target_task_id,
                target_hop_id=bound_hop_id,
                action=action,
                role=payload.get("role"),
                reason=str(payload.get("reason") or ""),
                confirmed=bool(payload.get("confirmed")),
                external_command_id=command_id,
            )
            return recorded, queued, {
                "target_task_id": queued["task_id"],
                "control_id": control["control_id"],
            }

        def queue(current: dict[str, Any]) -> dict[str, Any]:
            self.store._queue_control(
                current,
                action,
                role=payload.get("role"),
                reason=str(payload.get("reason") or ""),
                confirmed=bool(payload.get("confirmed")),
                origin="independent_agent",
                source_task_id=str(source.get("task_id") or ""),
                source_event_key=event_key,
            )
            return current

        queued = self.store.update(target["manifest_path"], queue)
        recorded = self.store.record_independent_command(
            source["manifest_path"], command_id
        )
        return recorded, queued, {
            "target_task_id": queued["task_id"],
            "control_id": queued["controls"][-1]["control_id"],
            **({"restore_mode": "same_task"} if action == "safe_restore" else {}),
        }

    def _ensure_builtin_independent_agents(
        self,
        discovered: Sequence[Mapping[str, Any]] | None = None,
    ) -> bool:
        if not self.config.independent_seed_builtins:
            return False
        existing_keys: set[str] = set()
        if discovered is not None:
            existing_keys = {
                str(item["independent"].get("agent_key") or "")
                for item in discovered
                if is_independent_task(item)
                and isinstance(item.get("independent"), Mapping)
                and str(item["independent"].get("agent_key") or "")
            }
        seeded = False
        for name, prompt, trigger_settings in (
            (
                "Maintainers",
                BUILTIN_MAINTAINERS_PROMPT,
                {"recovery": True},
            ),
            (
                "Monitor",
                BUILTIN_MONITOR_PROMPT,
                {"interval_minutes": 30, "check_all": True},
            ),
        ):
            _display, agent_key, _team = normalize_agent_name(name)
            if agent_key in existing_keys:
                continue
            self.store.seed_independent_agent(
                name,
                system_prompt=prompt,
                trigger_settings=trigger_settings,
                max_cycles=0,
            )
            existing_keys.add(agent_key)
            seeded = True
        return seeded

    def _activate_independent_agents(self) -> set[str]:
        if self.registry is None:
            return set()
        changed: set[str] = set()
        tasks = list(self.registry.tasks_by_id.values())
        candidates = sorted(
            (
                state
                for state in tasks
                if is_independent_task(state)
                and str(state.get("status") or "").upper() == "WAITING"
                and isinstance(state.get("independent"), Mapping)
                and state["independent"].get("enabled") is True
                and state["independent"].get("active_event") is None
            ),
            key=lambda state: str(state["independent"].get("agent_key") or ""),
        )
        for snapshot in candidates:
            task_id = str(snapshot["task_id"])
            path = self.registry.paths_by_id[task_id]
            events = canonical_independent_events(snapshot, tasks)
            if not events:
                continue

            def claim(current: dict[str, Any]) -> dict[str, Any]:
                return claim_oldest_event(current, events, all_tasks=tasks)

            saved = self.store.update(path, claim)
            if saved == snapshot:
                continue
            changed.update(self.registry.update_task(saved, now=time.time()))
            tasks = list(self.registry.tasks_by_id.values())
        self._publish_affected(changed)
        return changed

    async def _close_idle_independent_tabs(
        self,
        actions: CDPATabActions,
        *,
        now_epoch: float | None = None,
        endpoint_id: str | None = None,
    ) -> set[str]:
        if self.registry is None:
            return set()
        current_epoch = float(now_epoch if now_epoch is not None else time.time())
        changed: set[str] = set()
        for snapshot in list(self.registry.tasks_by_id.values()):
            if not is_independent_task(snapshot):
                continue
            if str(snapshot.get("status") or "").upper() not in {"WAITING", "PAUSED"}:
                continue
            independent = snapshot.get("independent")
            if not isinstance(independent, Mapping):
                continue
            if independent.get("active_event") is not None:
                continue
            if independent.get("idle_tab_closed_at"):
                continue
            idle_since = parse_time(independent.get("idle_since"))
            if idle_since is None:
                continue
            immediate_close = independent.get("close_tab_when_idle") is True
            keep_open_until = parse_time(independent.get("tab_keep_open_until"))
            if keep_open_until is not None:
                if not immediate_close and current_epoch < keep_open_until.timestamp():
                    continue
            elif (
                not immediate_close
                and current_epoch - idle_since.timestamp()
                < self.config.independent_idle_close_seconds
            ):
                continue
            role_record = (snapshot.get("roles") or {}).get(INDEPENDENT_ROLE)
            if not isinstance(role_record, Mapping):
                continue
            if endpoint_id is not None and str(role_record.get("browser_endpoint_id") or "") != endpoint_id:
                continue
            if not role_record.get("page_id") or not role_record.get("page_url"):
                continue
            selected = await actions.preflight_team(snapshot)
            closed = await actions.close_team(
                snapshot,
                preflighted_pages=selected,
            )
            if await actions.preflight_team(snapshot):
                raise TeamCloseError(
                    "idle independent-agent close verification found assigned tabs",
                    closed_tabs=closed,
                )
            task_id = str(snapshot["task_id"])
            path = self.registry.paths_by_id[task_id]

            def record_closed(state: dict[str, Any]) -> dict[str, Any]:
                current = state.get("independent")
                if not isinstance(current, dict):
                    return state
                if (
                    str(state.get("status") or "").upper() not in {"WAITING", "PAUSED"}
                    or current.get("active_event") is not None
                    or current.get("idle_since") != independent.get("idle_since")
                ):
                    return state
                current["idle_tab_closed_at"] = utc_now()
                current["close_tab_when_idle"] = False
                current["tab_keep_open_until"] = None
                current["idle_tab_closed_count"] = int(
                    current.get("idle_tab_closed_count") or 0
                ) + int(closed)
                record = state["roles"][INDEPENDENT_ROLE]
                record["online"] = False
                record["last_activity_at"] = utc_now()
                return state

            saved = self.store.update(path, record_closed)
            if saved != snapshot:
                changed.update(self.registry.update_task(saved, now=current_epoch))
        self._publish_affected(changed)
        return changed

    def hydrate_runtime(
        self, *, read_only: bool = False, startup: bool = True
    ) -> dict[str, Any]:
        """Discover once, validate, hydrate the registry, and publish projections."""
        self.runtime_db.ensure_schema()
        self._restore_rate_limit_cooldown()
        if not read_only and startup:
            self.runtime_db.requeue_running_commands()
            self.store.recover_phase4_replacement()
        if not read_only:
            self.store.normalize_legacy_independent_agents()
        tasks, errors = self.store.discover_with_errors()
        if not read_only and self._ensure_builtin_independent_agents(tasks):
            tasks, errors = self.store.discover_with_errors()
        identity_groups: dict[str, list[Mapping[str, Any]]] = {}
        recovery_owners: list[Mapping[str, Any]] = []
        for task in tasks:
            if not is_independent_task(task):
                continue
            independent = task.get("independent")
            if not isinstance(independent, Mapping) or independent.get("deleted_at"):
                continue
            agent_key = str(independent.get("agent_key") or "")
            if str(task.get("status") or "").upper() not in TERMINAL:
                identity_groups.setdefault(agent_key, []).append(task)
            if (
                independent.get("enabled") is True
                and str(task.get("status") or "").upper() not in TERMINAL
                and validate_trigger_settings(independent.get("trigger_settings"))[
                    "recovery"
                ]
            ):
                recovery_owners.append(task)
        for agent_key, owners in identity_groups.items():
            if len(owners) > 1:
                errors.append(
                    {
                        "manifest_path": owners[-1].get("manifest_path"),
                        "error": (
                            "multiple nonterminal Independent Agent identities for "
                            f"{agent_key!r}"
                        ),
                    }
                )
        if len(recovery_owners) > 1:
            errors.append(
                {
                    "manifest_path": recovery_owners[-1].get("manifest_path"),
                    "error": "multiple enabled Independent Agents own the Recovery trigger",
                }
            )
        catalog = {
            "discovered_at": utc_now(),
            "complete": not errors,
            "errors": [
                {
                    "manifest": Path(str(item.get("manifest_path") or "unknown")).name,
                    "error": sanitize_text(item.get("error"), max_chars=1000),
                }
                for item in errors
            ],
            "control_repository": str(self.config.repository_root),
        }
        if errors:
            self.runtime_degraded = True
            self.runtime_db.replace_task_projections([], catalog=catalog)
            self.runtime_db.put_snapshot(
                "dashboard_actions",
                {
                    "degraded": True,
                    "dependency_teams": [],
                    "resume_teams": [],
                    "reuse_teams": [],
                },
            )
            self._publish_agents(tasks)
            self._runtime_ready = True
            self.runtime_db.put_snapshot(
                "worker",
                {
                    "pid": os.getpid(),
                    "heartbeat_at": utc_now(),
                    "degraded": True,
                    "runtime_ready": self._runtime_ready,
                    "command_loop_ready": self._command_loop_ready,
                    "error": "catalog discovery is incomplete",
                    "rate_limit_cooldown": self._rate_limit_cooldown,
                    "rate_limit_cooldowns": dict(self._rate_limit_cooldowns),
                },
            )
            return catalog
        records = [
            (Path(str(task["manifest_path"])).expanduser().resolve(), task)
            for task in tasks
        ]
        self.store.validate_repository_integrity(records)
        if not read_only:
            reconciled_tasks: list[dict[str, Any]] = []
            scheduling_snapshot = list(tasks)
            for task in tasks:
                reconciled, _changed = self.store.refresh_scheduling(
                    task["manifest_path"],
                    tasks=scheduling_snapshot,
                    state=dict(task),
                )
                reconciled_tasks.append(reconciled)
            tasks = reconciled_tasks
        latest_independent: dict[str, Mapping[str, Any]] = {}
        runtime_tasks: list[Mapping[str, Any]] = []
        for task in tasks:
            if not is_independent_task(task):
                runtime_tasks.append(task)
                continue
            independent = task.get("independent")
            if not isinstance(independent, Mapping) or independent.get("deleted_at"):
                continue
            agent_key = str(independent.get("agent_key") or "")
            previous = latest_independent.get(agent_key)
            candidate_rank = (
                str(task.get("status") or "").upper() not in TERMINAL,
                int(independent.get("agent_generation") or 0),
            )
            previous_rank = (
                (
                    str(previous.get("status") or "").upper() not in TERMINAL,
                    int(
                        (previous.get("independent") or {}).get(
                            "agent_generation"
                        )
                        or 0
                    ),
                )
                if previous is not None
                else None
            )
            if previous_rank is None or candidate_rank > previous_rank:
                latest_independent[agent_key] = task
        runtime_tasks.extend(latest_independent.values())
        with self._registry_publication_lock:
            if self._startup_command_states:
                startup_states = dict(self._startup_command_states)
                task_positions = {
                    str(task.get("task_id") or ""): index
                    for index, task in enumerate(tasks)
                }
                runtime_positions = {
                    str(task.get("task_id") or ""): index
                    for index, task in enumerate(runtime_tasks)
                }
                for task_id, startup_state in startup_states.items():
                    if task_id in task_positions:
                        tasks[task_positions[task_id]] = dict(startup_state)
                    else:
                        tasks.append(dict(startup_state))
                    if task_id in runtime_positions:
                        runtime_tasks[runtime_positions[task_id]] = dict(startup_state)
                    else:
                        runtime_tasks.append(dict(startup_state))
            self._manifest_cache.clear()
            runtime_records = [
                (Path(str(task["manifest_path"])).expanduser().resolve(), task)
                for task in runtime_tasks
            ]
            for path, task in runtime_records:
                self._remember_manifest(path, task)
            self.registry = CDPARuntimeRegistry.hydrate(
                runtime_tasks,
                now=time.time(),
                cleanup_idle_seconds=self.config.cleanup_terminal_idle_seconds,
            )
            self._startup_command_states.clear()
        # Re-snapshot and publish under the same publication lock. A startup
        # command may update the registry after hydrate() releases its initial
        # merge lock; publishing an earlier local snapshot after that command
        # would overwrite the newer command projection.
        with self._registry_publication_lock:
            if self.registry is None:
                raise RuntimeError("runtime registry disappeared during hydrate")
            runtime_tasks = [
                dict(task) for task in self.registry.tasks_by_id.values()
            ]
            runtime_by_id = {
                str(task.get("task_id") or ""): task for task in runtime_tasks
            }
            projection_context = [
                runtime_by_id.get(str(task.get("task_id") or ""), task)
                for task in tasks
            ]
            for task_id, task in runtime_by_id.items():
                if not any(
                    str(item.get("task_id") or "") == task_id
                    for item in projection_context
                ):
                    projection_context.append(task)
            waiting_order = build_waiting_order(runtime_tasks)
            projections = [
                build_task_projection(
                    task,
                    tasks=projection_context,
                    waiting_order=waiting_order,
                    repository_allowed_roots=self.config.repository_allowed_roots,
                )
                for task in runtime_tasks
            ]
            self.runtime_db.replace_task_projections(
                projections, catalog=catalog
            )
        self.runtime_degraded = False
        self._runtime_ready = True
        self._publish_agents(runtime_tasks)
        self._publish_dashboard_actions()
        self._publish_heartbeat(force=True)
        return catalog

    def _publish_agents(
        self, tasks: Sequence[Mapping[str, Any]] | None = None
    ) -> None:
        current_tasks = list(
            tasks
            if tasks is not None
            else self.registry.tasks_by_id.values()
            if self.registry is not None
            else ()
        )
        latest: dict[str, Mapping[str, Any]] = {}
        for state in current_tasks:
            if not is_independent_task(state):
                continue
            independent = state.get("independent")
            if not isinstance(independent, Mapping) or independent.get("deleted_at"):
                continue
            agent_key = str(independent.get("agent_key") or "")
            generation = int(independent.get("agent_generation") or 0)
            previous = latest.get(agent_key)
            if previous is None or generation > int(
                previous["independent"].get("agent_generation") or 0
            ):
                latest[agent_key] = state
        independent_agents = []
        for agent_key, state in sorted(latest.items()):
            independent = state["independent"]
            independent_agents.append(
                {
                    "task_id": str(state.get("task_id") or ""),
                    "agent_key": agent_key,
                    "name": str(
                        independent.get("display_name")
                        or independent.get("agent_name")
                        or ""
                    ),
                    "identity_name": str(independent.get("agent_name") or ""),
                    "system_prompt": str(independent.get("system_prompt") or ""),
                    "trigger_settings": dict(
                        independent.get("trigger_settings") or {}
                    ),
                    "enabled": bool(independent.get("enabled")),
                    "status": str(state.get("status") or ""),
                    "generation": int(independent.get("agent_generation") or 0),
                    "tags": independent_tags(independent.get("trigger_settings")),
                    "max_cycles": int(independent.get("max_cycles") or 0),
                    "cycle": int(independent.get("cycle") or 0),
                    "browser_endpoint_id": independent.get("browser_endpoint_id"),
                    "tab_open": bool(
                        isinstance((state.get("roles") or {}).get(INDEPENDENT_ROLE), Mapping)
                        and (state.get("roles") or {})[INDEPENDENT_ROLE].get("online")
                    ),
                    "tab_keep_open_until": independent.get("tab_keep_open_until"),
                    "is_builtin": agent_key.casefold() in {"maintainers", "monitor"},
                }
            )
        self.runtime_db.put_snapshot(
            "agents",
            {
                "workflow": self.store.list_workflow_agents(),
                "independent": independent_agents,
            },
        )

    def _publish_dashboard_actions(self) -> None:
        if self.registry is None:
            return
        tasks = list(self.registry.tasks_by_id.values())
        browser = self._browser_projection or {}
        self.runtime_db.put_snapshot(
            "dashboard_actions",
            build_dashboard_actions(
                tasks,
                browser_pages=browser.get("pages", ()),
                browser_connected=browser.get("connected"),
            ),
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            self._publish_heartbeat(force=True)

    def _publish_heartbeat(
        self,
        *,
        force: bool = False,
        browser_connected: bool | None = None,
        browser_error: BaseException | str | None = None,
    ) -> None:
        if browser_connected is not None:
            self._browser_connected = bool(browser_connected)
            self._browser_error = (
                sanitize_text(browser_error, max_chars=500)
                if browser_error is not None
                else None
            )
        now = time.time()
        if not force and now - self._last_heartbeat_at < self.config.heartbeat_seconds:
            return
        self._last_heartbeat_at = now
        source_drift = self._refresh_source_drift()
        self.runtime_db.put_snapshot(
            "worker",
            {
                "pid": os.getpid(),
                "heartbeat_at": utc_now(),
                "started_at": self._worker_started_at,
                "lease": {
                    "acquired": self._worker_lease_acquired_at is not None,
                    "acquired_at": self._worker_lease_acquired_at,
                    "owner_pid": os.getpid() if self._worker_lease_acquired_at else None,
                },
                "source_identity": {
                    "fingerprint": self._source_identity.get("fingerprint"),
                    "captured_at": self._source_identity.get("captured_at"),
                },
                "source_drift": source_drift,
                "reload_required": bool(source_drift.get("drifted")),
                "degraded": self.runtime_degraded,
                "runtime_ready": self._runtime_ready,
                "command_loop_ready": self._command_loop_ready,
                "task_count": len(self.registry.tasks_by_id) if self.registry else 0,
                "browser_connected": self._browser_connected,
                "browser_error": self._browser_error,
                "rate_limit_cooldown": self._rate_limit_cooldown,
                "rate_limit_cooldowns": dict(self._rate_limit_cooldowns),
            },
        )

    def _command_task_state(self, task_id: str) -> dict[str, Any] | None:
        if self.registry is not None and task_id in self.registry.tasks_by_id:
            return dict(self.registry.tasks_by_id[task_id])
        return self.store.load_task_id(task_id)

    @staticmethod
    def _apply_pause_transition(
        state: dict[str, Any], control: Mapping[str, Any]
    ) -> dict[str, Any]:
        if state.get("status") not in {"INBOX", "RUNNING", "BLOCKED"}:
            raise RuntimeError("Pause is valid only for an INBOX, RUNNING, or BLOCKED task")
        state["resume_status"] = state.get("status")
        state["resume_column"] = state.get("kanban_column")
        state["resume_action"] = state.get("active_action")
        state["status"] = "PAUSED"
        state["kanban_column"] = "PAUSED"
        state["pause_reason"] = control.get("reason") or "manual pause"
        return {
            "outcome": "applied",
            "action": "pause",
            "postcondition": "task_paused",
            "reason": state["pause_reason"],
        }

    def _apply_degraded_pause_control(
        self, state: dict[str, Any], *, command_id: str
    ) -> dict[str, Any]:
        if is_independent_task(state):
            raise RuntimeError(
                "worker catalog is degraded; independent Pause requires catalog recovery"
            )
        control = next(
            (
                item
                for item in state.get("controls") or []
                if isinstance(item, dict)
                and item.get("status") == "requested"
                and item.get("action") == "pause"
                and item.get("external_command_id") == command_id
            ),
            None,
        )
        if control is None:
            raise RuntimeError("degraded Pause control was not durably queued")
        role = str(control.get("role") or state.get("active_role") or "PLAN").upper()
        eligibility = workflow_control_eligibility(state, "pause", role=role)
        if not eligibility["eligible"]:
            raise RuntimeError(str(eligibility["reason"] or "pause is not eligible"))
        baseline = copy.deepcopy(state)
        result = self._apply_pause_transition(state, control)
        control["status"] = "applied"
        control["command_state"] = "APPLIED"
        control["result"] = result
        control["applied_at"] = utc_now()
        saved = self._persist_control_result(
            Path(str(state["manifest_path"])),
            baseline,
            state,
            control_id=int(control["control_id"]),
            action="pause",
        )
        self._publish_command_state(saved)
        self._sync_control_commands(saved)
        return saved

    def _repository_allowed(self, value: object) -> Path:
        repository = Path(str(value or self.config.repository_root)).expanduser().resolve()
        if not any(repository.is_relative_to(root) for root in self.config.repository_allowed_roots):
            raise ValueError("target repository is outside repositories.allowed_roots")
        return repository

    def _publish_command_state(self, state: Mapping[str, Any]) -> None:
        snapshot = dict(state)
        with self._registry_publication_lock:
            if self.registry is None:
                task_id = str(snapshot.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("command state is missing task_id")
                self._startup_command_states[task_id] = snapshot
                waiting_order = build_waiting_order([snapshot])
                self.runtime_db.upsert_task_projections(
                    [
                        build_task_projection(
                            snapshot,
                            tasks=[snapshot],
                            waiting_order=waiting_order,
                            browser_pages=(self._browser_projection or {}).get("pages", ()),
                            browser_connected=(self._browser_projection or {}).get("connected"),
                            endpoint_connected=(self._browser_projection or {}).get("endpoint_connected"),
                            repository_allowed_roots=self.config.repository_allowed_roots,
                        )
                    ]
                )
                return
            affected = self.registry.update_task(snapshot, now=time.time())
        self._publish_affected(affected)
        self._publish_agents()

    def _command_replay_state(
        self, command: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Return a manifest that proves this durable command already mutated state."""
        command_id = str(command.get("command_id") or "")
        kind = str(command.get("kind") or "")
        task_id = str(command.get("task_id") or "") or None
        payload = command.get("payload")
        if not command_id or not isinstance(payload, Mapping):
            raise RuntimeError("command replay record is incomplete")
        candidates: list[dict[str, Any]] = []
        if task_id is not None:
            state = self.store.load_task_id(task_id)
            if state is not None:
                candidates.append(state)
        elif kind == "create_independent_agent":
            candidates.extend(self.store.discover())
        elif kind == "resume_team" and self.registry is not None:
            team = str(payload.get("team") or "")
            for path in self.registry.paths_by_id.values():
                state = self.store.load(path)
                if (
                    str(state.get("team") or "") == team
                    and command_id in state.get("applied_command_ids", [])
                ):
                    candidates.append(state)
        proven = [
            state
            for state in candidates
            if command_id in state.get("applied_command_ids", [])
        ]
        if not proven:
            return None
        if len(proven) != 1:
            raise RuntimeError("command provenance is ambiguous across manifests")
        state = proven[0]
        if kind == "independent_bind_target":
            if not is_independent_task(state):
                raise RuntimeError("independent command provenance belongs to a workflow task")
            independent = state.get("independent")
            active = independent.get("active_event") if isinstance(independent, Mapping) else None
            if (
                not isinstance(active, Mapping)
                or str(active.get("event_key") or "") != str(payload.get("event_key") or "")
                or str(active.get("target_task_id") or "")
                != str(payload.get("target_task_id") or "")
            ):
                raise RuntimeError("independent bind replay no longer matches the active event")
            return state
        if kind in {
            "create_independent_agent",
            "delete_independent_agent",
            "independent_complete",
            "independent_continue",
            "independent_run_now",
            "independent_reset",
            "independent_settings",
            "independent_activate_agent",
            "independent_create_repair",
            "independent_task_control",
        }:
            if not is_independent_task(state):
                raise RuntimeError("independent command provenance belongs to a workflow task")
            return state
        if kind == "change_goal":
            revisions = [
                item for item in state.get("goal_revisions") or []
                if isinstance(item, Mapping)
                and item.get("external_command_id") == command_id
            ]
            try:
                expected_semantic, expected_semantic_sha256 = normalize_governing_contract(
                    payload.get("governing_objective"),
                    payload.get("governing_directives")
                    if "governing_directives" in payload
                    else None,
                )
            except ValueError as exc:
                raise RuntimeError(
                    "change-goal command provenance does not match its payload"
                ) from exc
            if (
                len(revisions) != 1
                or revisions[0].get("goal") != payload.get("goal")
                or revisions[0].get("governing_semantic_payload") != expected_semantic
                or revisions[0].get("governing_semantic_sha256")
                != expected_semantic_sha256
            ):
                raise RuntimeError("change-goal command provenance does not match its payload")
            return state
        if kind == "remove_parent_dependency":
            events = [
                item
                for item in state.get("dependency_events") or []
                if isinstance(item, Mapping)
                and item.get("status") == "PARENT_REMOVED"
                and item.get("external_command_id") == command_id
            ]
            if (
                len(events) != 1
                or events[0].get("parent_task_id")
                != payload.get("parent_task_id")
            ):
                raise RuntimeError(
                    "parent-removal command provenance does not match its payload"
                )
            return state
        if kind == "create_task":
            if (
                str(state.get("task_id") or "") != task_id
                or str(state.get("task_text") or "")
                != str(payload.get("task") or "")
                or str(Path(str(state.get("repository") or "")).resolve())
                != str(self._repository_allowed(payload.get("repository")))
            ):
                raise RuntimeError("create command provenance does not match its payload")
            if "governing_objective" in payload or "governing_directives" in payload:
                try:
                    expected_semantic, expected_semantic_sha256 = normalize_governing_contract(
                        payload.get("governing_objective"),
                        payload.get("governing_directives")
                        if "governing_directives" in payload
                        else None,
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        "create command governing provenance is invalid"
                    ) from exc
                if (
                    state.get("governing_semantic_payload") != expected_semantic
                    or state.get("governing_semantic_sha256")
                    != expected_semantic_sha256
                ):
                    raise RuntimeError(
                        "create command governing provenance does not match its payload"
                    )
            if "roles" in payload:
                expected_roles = {
                    str(role).strip().upper() for role in payload["roles"]
                }
                actual_roles = set(state.get("roles", {}))
                if actual_roles != expected_roles:
                    raise RuntimeError(
                        "create command role provenance does not match its payload"
                    )
            requested_bootstrap_id = (
                str(payload.get("bootstrap_id") or "") or None
            )
            snapshot = state.get("bootstrap")
            snapshot_bootstrap_id = (
                str(snapshot.get("bootstrap_id") or "")
                if isinstance(snapshot, Mapping)
                else None
            )
            if requested_bootstrap_id != snapshot_bootstrap_id:
                raise RuntimeError(
                    "create command bootstrap provenance does not match its payload"
                )
            requested_context_policy = payload.get("context_policy")
            if requested_context_policy is not None and state.get("context_policy") != requested_context_policy:
                raise RuntimeError(
                    "create command context policy provenance does not match its payload"
                )
            definition = payload.get("bootstrap_definition")
            if definition is not None:
                if not isinstance(definition, Mapping) or not isinstance(snapshot, Mapping):
                    raise RuntimeError(
                        "create command bootstrap definition provenance is missing"
                    )
                expected_definition = {
                    "bootstrap_id": definition.get("bootstrap_id"),
                    "name": definition.get("name"),
                    "source_conversation_id": definition.get("source_conversation_id"),
                    "prewarm_prompt": definition.get("prewarm_prompt"),
                    "max_backups": definition.get("max_backups"),
                }
                actual_definition = {
                    key: snapshot.get(key) for key in expected_definition
                }
                if actual_definition != expected_definition:
                    raise RuntimeError(
                        "create command bootstrap definition provenance does not match its payload"
                    )
            return state
        if kind not in {"task_control", "resume_team"}:
            return None
        controls = [
            item
            for item in state.get("controls") or []
            if isinstance(item, Mapping)
            and (
                item.get("external_command_id") == command_id
                or any(
                    isinstance(entry, Mapping)
                    and entry.get("command_id") == command_id
                    for entry in item.get("external_commands") or []
                )
            )
        ]
        if len(controls) != 1:
            raise RuntimeError("command provenance lacks one exact control record")
        control = controls[0]
        expected_action = (
            "resume" if kind == "resume_team" else str(payload.get("action") or "")
        )
        expected_reason = payload.get("reason")
        if kind == "resume_team":
            expected_reason = expected_reason or "exact-team resume"
            if str(state.get("team") or "") != str(payload.get("team") or ""):
                raise RuntimeError("resume command provenance belongs to another team")
        elif expected_action == "resume":
            expected_reason = expected_reason or "resume requested"
        provenance_reason = control.get("reason")
        for entry in control.get("external_commands") or []:
            if isinstance(entry, Mapping) and entry.get("command_id") == command_id:
                provenance_reason = entry.get("reason")
                break
        if (
            str(control.get("action") or "") != expected_action
            or (provenance_reason or None) != (expected_reason or None)
        ):
            raise RuntimeError("control command provenance does not match its payload")
        if kind == "task_control":
            expected_role = str(payload.get("role") or "").strip().upper() or None
            if expected_role is not None and control.get("role") != expected_role:
                raise RuntimeError("control command provenance has a different role")
            if bool(control.get("confirmed")) != bool(payload.get("confirmed")):
                raise RuntimeError("control command provenance has different confirmation")
        return state

    def _browser_endpoint_has_running_execution(self, endpoint_id: str) -> bool:
        return any(
            str(item.get("endpoint_id") or "") == endpoint_id
            for item in self._running_executions()
        ) or self._browser_endpoint_has_unresolved_execution(endpoint_id)

    def _browser_endpoint_has_unresolved_execution(self, endpoint_id: str) -> bool:
        """Protect draining endpoints from durable execution evidence, not UI cache state."""
        if self.registry is None:
            return False
        for state in self.registry.tasks_by_id.values():
            try:
                hop = _active_hop(state)
            except RuntimeError:
                continue
            hop_state = str(hop.get("state") or "")
            if hop_state not in {"sending", "sent", "waiting", "responded"}:
                continue
            referenced_endpoint = str(hop.get("browser_endpoint_id") or "").strip() or self._execution_endpoint_id(state)
            if referenced_endpoint != endpoint_id:
                continue
            ledger_path = str(hop.get("ledger_path") or "").strip()
            request_id = str(hop.get("request_id") or "").strip()
            record = None
            if ledger_path and request_id:
                try:
                    record = RequestLedger(ledger_path).peek(request_id)
                except Exception:
                    return True
            if hop_state == "sending" and self._is_pristine_preboundary_sending_record(state, hop, record):
                continue
            if record is not None and record.status in {RequestStatus.COMPLETED, RequestStatus.FAILED_FINAL}:
                continue
            return True
        return False

    def _finalize_drained_endpoints(self) -> list[str]:
        finalized: list[str] = []
        for endpoint in self.store.endpoints.list():
            endpoint_id = str(endpoint.get("endpoint_id") or "")
            if not endpoint_id or endpoint.get("draining") is not True:
                continue
            if self._browser_endpoint_has_running_execution(endpoint_id):
                continue
            removed = self.store.endpoints.remove(
                endpoint_id, has_nonterminal_tasks=False
            )
            if removed.get("removed") is True:
                finalized.append(endpoint_id)
        return finalized

    @staticmethod
    def _browser_endpoint_add_settings(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        address = normalize_cdp_endpoint(str(payload.get("address") or ""))
        settings = {key: value for key, value in payload.items()
                    if key not in {"address", "clone_from_endpoint_id", "start"}}
        normalize_endpoint_record({**settings, "cdp_url": address}, endpoint_id="admission-validation")
        if settings.get("management_mode", "external") == "external" and any(
            key in payload for key in ("profile_path", "executable_path", "clone_from_endpoint_id", "start")
        ):
            raise ValueError("external browsers are attach-only")
        return address, settings

    async def _probe_browser_endpoint(self, address: str) -> dict[str, Any]:
        pool = self._endpoint_pool or EndpointPool(self.store.endpoints)
        return await pool.probe_address(address)

    async def _complete_browser_endpoint_add(self, command_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        address = None
        try:
            address, settings = self._browser_endpoint_add_settings(payload)
            probe = None
            existing = any(item["cdp_url"] == address for item in self.store.endpoints.list())
            if settings.get("management_mode", "external") == "external" and not existing:
                probe = await self._probe_browser_endpoint(address)
                if probe.get("health") != "online":
                    raise ConnectionError(probe.get("error") or "CDP endpoint is unavailable")
            if settings.get("management_mode") == "managed-local":
                endpoint = await asyncio.to_thread(self._apply_browser_endpoint_add, payload)
            else:
                endpoint = self._apply_browser_endpoint_add(payload)
            self.runtime_db.finish_command(command_id, result={
                "endpoint_id": endpoint["endpoint_id"], "endpoint": endpoint, "probe": probe,
            })
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if address and is_local_cdp_endpoint(address) and isinstance(exc, (ConnectionError, OSError)):
                detail += "; for an offline local browser choose managed-local and a new, existing, or safe cloned profile"
            self.runtime_db.finish_command(command_id, error=sanitize_text(detail, max_chars=2000))
        return self.runtime_db.get_command(command_id)

    def _apply_browser_endpoint_add(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        address, settings = self._browser_endpoint_add_settings(payload)
        for existing in self.store.endpoints.list(include_removed=True):
            if existing.get("removed") or existing.get("cdp_url") != address:
                continue
            if any(existing.get(key) != value for key, value in settings.items()):
                raise ValueError("browser endpoint address is already registered with different settings")
            if payload.get("start") is True:
                self.managed_browsers.start(existing)
            return existing
        if settings.get("management_mode") == "managed-local":
            clone_id = str(payload.get("clone_from_endpoint_id") or "").strip()
            source = self.store.endpoints.get(clone_id) if clone_id else None
            source_running = bool(
                source and self.managed_browsers.read_pid(str(source["endpoint_id"]))
            )
            self.managed_browsers.prepare_profile(
                profile_path=str(settings.get("profile_path") or ""),
                clone_from=source,
                source_running=source_running,
            )
        endpoint = self.store.endpoints.add(address, **settings)
        if payload.get("start") is True:
            self.managed_browsers.start(endpoint)
        return endpoint

    def _apply_browser_endpoint_update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        endpoint_id = str(payload.get("endpoint_id") or "").strip()
        if not endpoint_id:
            raise ValueError("browser endpoint update requires endpoint_id")
        changes = {key: value for key, value in payload.items() if key != "endpoint_id"}
        if "cdp_url" in changes and normalize_cdp_endpoint(changes["cdp_url"]) != self.store.endpoints.get(endpoint_id)["cdp_url"]:
            if self._browser_endpoint_has_running_execution(endpoint_id):
                raise ValueError("cannot retarget a running endpoint; drain its executions first")
        return self.store.endpoints.update(endpoint_id, **changes)

    def _apply_browser_endpoint_remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        endpoint_id = str(payload.get("endpoint_id") or "").strip()
        if not endpoint_id:
            raise ValueError("browser endpoint removal requires endpoint_id")
        return self.store.endpoints.remove(
            endpoint_id,
            has_nonterminal_tasks=self._browser_endpoint_has_running_execution(endpoint_id),
        )

    def _apply_next_command(
        self, *, allowed_kinds: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        command = self.runtime_db.claim_next_command(kinds=allowed_kinds)
        if command is None:
            return None
        command_id = str(command["command_id"])
        task_id = str(command.get("task_id") or "") or None
        try:
            try:
                replay_state = self._command_replay_state(command)
            except Exception as exc:
                self.runtime_db.require_command_recovery(
                    command_id,
                    error=sanitize_text(
                        f"{type(exc).__name__}: {exc}", max_chars=2000
                    ),
                )
                return self.runtime_db.get_command(command_id)
            if replay_state is not None:
                self._publish_command_state(replay_state)
                if str(command.get("kind") or "") in {"resume_team", "task_control"}:
                    self._sync_control_commands(replay_state)
                    return self.runtime_db.get_command(command_id)
                self.runtime_db.finish_command(
                    command_id,
                    result={
                        "task_id": replay_state.get("task_id"),
                        "status": replay_state.get("status"),
                        "reconciled": True,
                    },
                )
                return self.runtime_db.get_command(command_id)
            expected = command.get("expected_task_version")
            if expected is not None:
                if task_id is None:
                    raise ValueError("expected_task_version requires task_id")
                current_version = self.runtime_db.get_task_version(task_id)
                if current_version != int(expected):
                    raise ValueError(
                        f"stale task version: expected {expected}, current {current_version}"
                    )
            payload = command["payload"]
            kind = str(command["kind"])
            state: dict[str, Any] | None = None
            if kind == "reload_catalog":
                catalog = self.hydrate_runtime(startup=False)
                if catalog.get("complete") is not True:
                    raise RuntimeError(
                        "reload catalog is incomplete: "
                        f"{len(catalog.get('errors') or [])} discovery errors"
                    )
                result = {"catalog": catalog}
            elif kind == "browser_endpoint_add":
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return asyncio.run(self._complete_browser_endpoint_add(command_id, payload))
                task = loop.create_task(self._complete_browser_endpoint_add(command_id, payload))
                self._browser_command_tasks.add(task)
                task.add_done_callback(self._browser_command_tasks.discard)
                return self.runtime_db.get_command(command_id)
            elif kind == "browser_endpoint_update":
                endpoint = self._apply_browser_endpoint_update(payload)
                result = {"endpoint_id": endpoint["endpoint_id"], "endpoint": endpoint}
            elif kind == "browser_endpoint_remove":
                endpoint = self._apply_browser_endpoint_remove(payload)
                result = {
                    "endpoint_id": endpoint["endpoint_id"],
                    "draining": endpoint["draining"],
                    "removed": endpoint["removed"],
                }
            elif kind in {"browser_endpoint_start", "browser_endpoint_stop"}:
                endpoint_id = str(payload.get("endpoint_id") or "").strip()
                endpoint = self.store.endpoints.get(endpoint_id)
                if kind == "browser_endpoint_start":
                    self.managed_browsers.start(endpoint)
                    result = {"endpoint_id": endpoint_id, "running": True}
                else:
                    stopped = self.managed_browsers.stop(endpoint)
                    result = {"endpoint_id": endpoint_id, "running": False, "stopped": stopped}
            elif kind == "create_task":
                if task_id is None:
                    raise ValueError("create_task requires preallocated task_id")
                existing = self._command_task_state(task_id)
                if existing is not None:
                    if command_id not in existing.get("applied_command_ids", []):
                        raise ValueError(f"task_id already exists without command provenance: {task_id}")
                    state = existing
                else:
                    bootstrap = None
                    if payload.get("bootstrap_definition") is not None or payload.get("bootstrap_id") not in {None, ""}:
                        raise ValueError(
                            "bootstrap context is temporarily disabled; new tasks use Fresh context"
                        )
                    state = self.store.create_task(
                        str(payload.get("task") or ""),
                        requested_team=payload.get("requested_team"),
                        reuse_team=payload.get("reuse_team") or None,
                        roles=(
                            tuple(payload.get("roles") or ())
                            if "roles" in payload
                            else None
                        ),
                        new_roles=tuple(payload.get("new_roles") or ()),
                        new_all=bool(payload.get("new_all")),
                        repository=self._repository_allowed(payload.get("repository")),
                        task_id=task_id,
                        report_mode=str(payload.get("report_mode") or "file"),
                        depends_on_task_ids=tuple(payload.get("depends_on_task_ids") or ()),
                        upload_paths=tuple(payload.get("upload_paths") or ()),
                        bootstrap=bootstrap,
                        browser_endpoint_id=payload.get("browser_endpoint_id") or None,
                        required_browser_endpoint_id=(
                            payload.get("required_browser_endpoint_id") or None
                        ),
                        conversation_mode=payload.get("conversation_mode") or None,
                        context_policy=payload.get("context_policy") or None,
                        workflow_mode=str(payload.get("workflow_mode") or "team"),
                        goal=payload.get("goal"),
                        conversation_url=payload.get("conversation_url") or None,
                        governing_objective=payload.get("governing_objective"),
                        governing_directives=payload.get("governing_directives"),
                        admission_provenance=payload.get("admission_provenance"),
                        external_command_id=command_id,
                    )
                self._publish_command_state(state)
                result = {"task_id": task_id, "status": state.get("status")}
            elif kind == "create_workflow_agent":
                definition = self.store.create_workflow_agent(
                    display_name=payload.get("name"),
                    system_prompt=payload.get("system_prompt"),
                    continuation_contract=payload.get("continuation_contract"),
                    external_command_id=command_id,
                )
                self._publish_agents()
                result = {
                    "route_key": definition["route_key"],
                    "display_name": definition["display_name"],
                }
            elif kind == "update_workflow_agent":
                definition = self.store.update_workflow_agent(
                    str(payload.get("route_key") or ""),
                    display_name=payload.get("name"),
                    system_prompt=payload.get("system_prompt"),
                    continuation_contract=payload.get("continuation_contract"),
                    external_command_id=command_id,
                )
                self._publish_agents()
                result = {
                    "route_key": definition["route_key"],
                    "display_name": definition["display_name"],
                }
            elif kind == "delete_workflow_agent":
                definition = self.store.delete_workflow_agent(
                    str(payload.get("route_key") or ""),
                    external_command_id=command_id,
                )
                self._publish_agents()
                result = {
                    "route_key": definition["route_key"],
                    "deleted": True,
                }
            elif kind == "create_independent_agent":
                state = self.store.create_independent_agent(
                    str(payload.get("name") or ""),
                    system_prompt=str(payload.get("system_prompt") or ""),
                    trigger_settings=payload.get("trigger_settings"),
                    repository=self._repository_allowed(payload.get("repository")),
                    enabled=bool(payload.get("enabled", True)),
                    max_cycles=(
                        int(payload["max_cycles"])
                        if "max_cycles" in payload
                        else None
                    ),
                    temporary_chat=(
                        payload["temporary_chat"] if "temporary_chat" in payload else None
                    ),
                    browser_endpoint_id=payload.get("browser_endpoint_id") or None,
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {"task_id": state["task_id"], "status": state["status"]}
            elif kind == "delete_independent_agent":
                if task_id is None:
                    raise ValueError("delete_independent_agent requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.delete_independent_agent(
                    current["manifest_path"],
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {"task_id": task_id, "deleted": True}
            elif kind == "independent_complete":
                if task_id is None:
                    raise ValueError("independent_complete requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                requested = self.store.request_independent_completion(
                    current["manifest_path"],
                    outcome=str(payload.get("outcome") or ""),
                    summary=str(payload.get("summary") or ""),
                    target_task_id=payload.get("target_task_id"),
                    repair_task_id=payload.get("repair_task_id"),
                    external_command_id=command_id,
                )
                hop = _active_hop(requested)
                if hop.get("state") == "responded":
                    completed = self.store.complete_independent_task(
                        requested["manifest_path"],
                        outcome=str(payload.get("outcome") or ""),
                        summary=str(payload.get("summary") or ""),
                        target_task_id=payload.get("target_task_id"),
                        repair_task_id=payload.get("repair_task_id"),
                    )
                    self._publish_command_state(completed)
                    state = completed
                    result = {
                        "task_id": completed["task_id"],
                        "status": completed["status"],
                    }
                else:
                    self._publish_command_state(requested)
                    state = requested
                    result = {
                        "task_id": requested["task_id"],
                        "status": requested["status"],
                        "queued_until_response": True,
                    }
            elif kind == "independent_continue":
                if task_id is None:
                    raise ValueError("independent_continue requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                requested = self.store.request_independent_continuation(
                    current["manifest_path"],
                    reason=str(payload.get("reason") or ""),
                    external_command_id=command_id,
                )
                hop = _active_hop(requested)
                if hop.get("state") == "responded":
                    state = self.store.continue_independent_task(
                        requested["manifest_path"],
                        reason=str(payload.get("reason") or ""),
                    )
                else:
                    state = requested
                self._publish_command_state(state)
                result = {
                    "task_id": state["task_id"],
                    "status": state["status"],
                    "queued_until_response": hop.get("state") != "responded",
                }
            elif kind == "independent_run_now":
                if task_id is None:
                    raise ValueError("independent_run_now requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.run_independent_now(
                    current["manifest_path"],
                    trigger_type=str(payload.get("trigger_type") or "manual"),
                    instruction=(
                        str(payload.get("instruction"))
                        if payload.get("instruction") is not None
                        else None
                    ),
                    required_browser_endpoint_id=(
                        str(payload.get("required_browser_endpoint_id"))
                        if payload.get("required_browser_endpoint_id") is not None
                        else None
                    ),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {"task_id": state["task_id"], "status": state["status"]}
            elif kind == "independent_reset":
                if task_id is None:
                    raise ValueError("independent_reset requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.request_control(
                    current["manifest_path"],
                    "reset",
                    role=INDEPENDENT_ROLE,
                    reason=str(payload.get("reason") or "Operator reset"),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {
                    "task_id": state["task_id"],
                    "status": state["status"],
                    "queued": True,
                }
            elif kind == "independent_activate_agent":
                if task_id is None:
                    raise ValueError("independent_activate_agent requires source task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state, activated = self._activate_independent_agent_command(
                    current,
                    agent_name=str(payload.get("agent_name") or ""),
                    target_task_id=str(payload.get("target_task_id") or ""),
                    command_id=command_id,
                )
                self._publish_command_state(state)
                self._publish_command_state(activated)
                result = {
                    "task_id": state["task_id"],
                    "activated_task_id": activated["task_id"],
                    "event_key": activated["independent"]["active_event"]["event_key"],
                }
            elif kind == "independent_bind_target":
                if task_id is None:
                    raise ValueError("independent_bind_target requires source task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self._bind_independent_event_target_command(
                    current,
                    payload=payload,
                    command_id=command_id,
                )
                self._publish_command_state(state)
                active = state["independent"]["active_event"]
                result = {
                    "task_id": state["task_id"],
                    "event_key": active["event_key"],
                    "target_task_id": active["target_task_id"],
                    "target_hop_id": active.get("target_hop_id"),
                }
            elif kind == "independent_create_repair":
                if task_id is None:
                    raise ValueError("independent_create_repair requires source task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state, affected, repair = self._create_independent_repair_command(
                    current,
                    payload=payload,
                    command_id=command_id,
                )
                self._publish_command_state(state)
                self._publish_command_state(affected)
                self._publish_command_state(repair)
                result = {
                    "task_id": state["task_id"],
                    "affected_task_id": affected["task_id"],
                    "repair_task_id": repair["task_id"],
                    "disposition": payload.get("disposition"),
                }
            elif kind == "independent_task_control":
                if task_id is None:
                    raise ValueError("independent_task_control requires source task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state, target, control_result = self._queue_independent_task_control(
                    current,
                    payload=payload,
                    command_id=command_id,
                )
                self._publish_command_state(state)
                self._publish_command_state(target)
                result = {
                    "task_id": state["task_id"],
                    **control_result,
                }
            elif kind == "independent_settings":
                if task_id is None:
                    raise ValueError("independent_settings requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                settings_kwargs = {
                    "enabled": payload.get("enabled") if "enabled" in payload else None,
                    "display_name": payload.get("display_name"),
                    "system_prompt": payload.get("system_prompt"),
                    "trigger_settings": (
                        payload.get("trigger_settings")
                        if "trigger_settings" in payload
                        else None
                    ),
                    "max_cycles": (
                        int(payload["max_cycles"])
                        if "max_cycles" in payload
                        else None
                    ),
                    "new_chat_next_job": (
                        payload.get("new_chat_next_job")
                        if "new_chat_next_job" in payload
                        else None
                    ),
                    "external_command_id": command_id,
                }
                if "browser_endpoint_id" in payload:
                    settings_kwargs["browser_endpoint_id"] = payload.get("browser_endpoint_id")
                state = self.store.update_independent_agent(
                    current["manifest_path"], **settings_kwargs
                )
                self._publish_command_state(state)
                result = {"task_id": state["task_id"], "status": state["status"]}
            elif kind == "change_goal":
                if task_id is None:
                    raise ValueError("change_goal requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.change_goal(
                    current["manifest_path"],
                    str(payload.get("goal") or ""),
                    governing_objective=payload.get("governing_objective"),
                    governing_directives=payload.get("governing_directives"),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {
                    "task_id": task_id,
                    "status": state.get("status"),
                    "goal_revision": len(state.get("goal_revisions") or []),
                }
            elif kind == "remove_parent_dependency":
                if task_id is None:
                    raise ValueError("remove_parent_dependency requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.remove_parent_dependency(
                    current["manifest_path"],
                    str(payload.get("parent_task_id") or ""),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {
                    "task_id": task_id,
                    "status": state.get("status"),
                    "depends_on_task_ids": list(
                        state.get("depends_on_task_ids") or []
                    ),
                }
            elif kind == "resume_team":
                state = self.store.resume_team(
                    str(payload.get("team") or ""),
                    reason=payload.get("reason"),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                self._sync_control_commands(state)
                return self.runtime_db.get_command(command_id)
            elif kind == "task_control":
                if task_id is None:
                    raise ValueError("task_control requires task_id")
                action = str(payload.get("action") or "").strip().lower()
                if self.runtime_degraded and action != "pause":
                    raise RuntimeError(
                        "worker catalog is degraded; repair discovery and submit "
                        "reload_catalog before this command can run"
                    )
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.request_control(
                    current["manifest_path"],
                    action,
                    role=payload.get("role"),
                    reason=payload.get("reason"),
                    confirmed=bool(payload.get("confirmed")),
                    external_command_id=command_id,
                )
                if self.runtime_degraded:
                    self._apply_degraded_pause_control(state, command_id=command_id)
                    return self.runtime_db.get_command(command_id)
                self._publish_command_state(state)
                self._sync_control_commands(state)
                return self.runtime_db.get_command(command_id)
            else:
                raise ValueError(f"unsupported command kind: {kind}")
            self.runtime_db.finish_command(command_id, result=result)
        except Exception as exc:
            self.runtime_db.finish_command(
                command_id, error=sanitize_text(f"{type(exc).__name__}: {exc}", max_chars=2000)
            )
        return self.runtime_db.get_command(command_id)

    def dispatch_command_once(self) -> dict[str, Any] | None:
        endpoint_commands = (
            "browser_endpoint_add",
            "browser_endpoint_update",
            "browser_endpoint_remove",
            "browser_endpoint_start",
            "browser_endpoint_stop",
        )
        if self.runtime_degraded:
            recovery = self._apply_next_command(
                allowed_kinds=("reload_catalog", *endpoint_commands)
            )
            if recovery is not None:
                return recovery
            degraded_control = self._apply_next_command(allowed_kinds=("task_control",))
            if degraded_control is not None:
                return degraded_control
            rejected = self.runtime_db.claim_next_command(
                kinds=(
                    "create_task",
                    "change_goal",
                    "remove_parent_dependency",
                    "create_workflow_agent",
                    "update_workflow_agent",
                    "delete_workflow_agent",
                    "create_independent_agent",
                    "delete_independent_agent",
                    "independent_complete",
                    "independent_continue",
                    "independent_run_now",
                    "independent_reset",
                    "independent_settings",
                    "independent_activate_agent",
                    "independent_bind_target",
                    "independent_create_repair",
                    "independent_task_control",
                    "resume_team",
                )
            )
            if rejected is None:
                return None
            self.runtime_db.finish_command(
                str(rejected["command_id"]),
                error=(
                    "worker catalog is degraded; repair discovery and submit "
                    "reload_catalog before this command can run"
                ),
            )
            return self.runtime_db.get_command(str(rejected["command_id"]))
        if self.registry is None:
            return self._apply_next_command(
                allowed_kinds=(
                    "change_goal",
                    "remove_parent_dependency",
                    "resume_team",
                    "task_control",
                    *endpoint_commands,
                )
            )
        browser_safe_commands = (
            "reload_catalog",
            "create_task",
            "change_goal",
            "remove_parent_dependency",
            "create_workflow_agent",
            "update_workflow_agent",
            "delete_workflow_agent",
            "create_independent_agent",
            "delete_independent_agent",
            "independent_complete",
            "independent_continue",
            "independent_run_now",
            "independent_reset",
            "independent_settings",
            "independent_activate_agent",
            "independent_bind_target",
            "independent_create_repair",
            "independent_task_control",
            "resume_team",
            "task_control",
            "browser_endpoint_add",
            "browser_endpoint_update",
            "browser_endpoint_remove",
            "browser_endpoint_start",
            "browser_endpoint_stop",
        )
        if self._browser_cycle_active:
            allowed = browser_safe_commands
        else:
            allowed = None
        return self._apply_next_command(allowed_kinds=allowed)

    async def run_command_loop(self) -> None:
        self._command_loop_ready = True
        self._publish_heartbeat(force=True)
        try:
            while True:
                command = await asyncio.to_thread(self.dispatch_command_once)
                if command is None:
                    await asyncio.sleep(self.config.command_poll_seconds)
                else:
                    await asyncio.sleep(0)
        finally:
            self._command_loop_ready = False
            self._publish_heartbeat(force=True)

    def _publish_browser_projection(self, projection: Mapping[str, Any]) -> None:
        normalized = dict(projection)
        previous = self._browser_projection
        if normalized == previous:
            return
        self._browser_projection = normalized
        self.runtime_db.put_snapshot("browser", normalized)
        if self.registry is None:
            return
        tasks = list(self.registry.tasks_by_id.values())
        waiting_order = build_waiting_order(tasks)
        self.runtime_db.upsert_task_projections(
            [
                build_task_projection(
                    task,
                    tasks=tasks,
                    waiting_order=waiting_order,
                    browser_pages=normalized.get("pages", ()),
                    browser_connected=normalized.get("connected"),
                    endpoint_connected=normalized.get("endpoint_connected"),
                    repository_allowed_roots=self.config.repository_allowed_roots,
                )
                for task in tasks
            ]
        )
        self._publish_dashboard_actions()

    def _publish_browser_disconnected(self) -> None:
        if self._browser_projection is not None and self._browser_projection.get("connected") is False:
            return
        self._last_browser_inventory_at = 0.0
        self._last_browser_page_count = -1
        self._publish_browser_projection(
            {
                "connected": False,
                "observed_at": utc_now(),
                "page_count": None,
                "pages": [],
            }
        )

    async def _publish_browser_pool_inventory(
        self,
        contexts: Mapping[str, Any],
        runtime_snapshot: Mapping[str, Any],
    ) -> None:
        inventories = await asyncio.gather(
            *(
                build_browser_projection(
                    context,
                    previous=None,
                    endpoint_id=endpoint_id,
                )
                for endpoint_id, context in contexts.items()
            )
        )
        by_id = {str(item.get("endpoint_id") or ""): item for item in inventories}
        pages: list[dict[str, Any]] = []
        endpoints: list[dict[str, Any]] = []
        for raw in runtime_snapshot.get("endpoints") or ():
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            endpoint_id = str(item.get("endpoint_id") or "")
            inventory = by_id.get(endpoint_id)
            endpoint_pages = list((inventory or {}).get("pages") or ())
            observed_page_ids = {
                str(page.get("page_id") or "").strip()
                for page in endpoint_pages
                if isinstance(page, Mapping) and str(page.get("page_id") or "").strip()
            }
            running_executions = [
                {
                    **dict(execution),
                    "page_observed": (
                        not str(execution.get("page_id") or "").strip()
                        or str(execution.get("page_id") or "").strip() in observed_page_ids
                    ),
                }
                for execution in item.get("running_executions") or ()
                if isinstance(execution, Mapping)
            ]
            capacity = max(0, int(item.get("capacity") or 0))
            active_count = min(capacity, len(running_executions))
            item["connected"] = inventory is not None
            item["page_count"] = len(endpoint_pages)
            item["pages"] = endpoint_pages
            item["running_executions"] = running_executions
            item["active_count"] = active_count
            item["available_slots"] = max(0, capacity - active_count)
            if item.get("management_mode") == "managed-local":
                item.update(self.managed_browsers.process_state(endpoint_id))
            else:
                item.update(
                    managed_running=None,
                    process_ownership=None,
                    process_error=None,
                )
            endpoints.append(item)
            pages.extend(endpoint_pages)
        projection = {
            "connected": bool(contexts),
            "observed_at": utc_now(),
            "page_count": len(pages),
            "pages": pages,
            "default_endpoint_id": runtime_snapshot.get("default_endpoint_id"),
            "endpoints": endpoints,
            "endpoint_connected": {
                str(item.get("endpoint_id") or ""): bool(item.get("connected"))
                for item in endpoints
                if str(item.get("endpoint_id") or "")
            },
        }
        self._last_browser_inventory_at = time.time()
        self._last_browser_page_count = len(pages)
        self._publish_browser_projection(projection)

    async def _publish_browser_inventory(self, browser_context: Any) -> None:
        now = time.time()
        page_count = len(list(getattr(browser_context, "pages", ())))
        if (
            self._browser_projection is not None
            and self._browser_projection.get("connected") is True
            and page_count == self._last_browser_page_count
            and now - self._last_browser_inventory_at < self.config.browser_inventory_seconds
        ):
            return
        if self._browser_inventory_task is not None:
            return
        task = asyncio.create_task(
            build_browser_projection(browser_context, previous=self._browser_projection)
        )
        self._browser_inventory_task = task

        def consume(done: asyncio.Task[Any]) -> None:
            if not done.cancelled():
                done.exception()
            if self._browser_inventory_task is done:
                self._browser_inventory_task = None

        task.add_done_callback(consume)
        done, _pending = await asyncio.wait({task}, timeout=1.5)
        if task not in done:
            return
        projection = task.result()
        self._last_browser_inventory_at = now
        self._last_browser_page_count = page_count
        self._publish_browser_projection(projection)

    def _publish_affected(self, task_ids: set[str]) -> None:
        if self.registry is None or not task_ids:
            return
        tasks = list(self.registry.tasks_by_id.values())
        waiting_order = build_waiting_order(tasks)
        projection_ids = set(task_ids)
        projection_ids.update(
            str(task.get("task_id") or "")
            for task in tasks
            if str(task.get("status") or "").upper() == "WAITING"
        )
        projections = [
            build_task_projection(
                self.registry.tasks_by_id[task_id],
                tasks=tasks,
                waiting_order=waiting_order,
                browser_pages=(self._browser_projection or {}).get("pages", ()),
                browser_connected=(self._browser_projection or {}).get("connected"),
                endpoint_connected=(self._browser_projection or {}).get("endpoint_connected"),
                repository_allowed_roots=self.config.repository_allowed_roots,
            )
            for task_id in sorted(projection_ids)
            if task_id in self.registry.tasks_by_id
        ]
        if projections:
            self.runtime_db.upsert_task_projections(projections)
            self._publish_dashboard_actions()

    def _ambient_page_has_active_controller(
        self, binding: Mapping[str, Any], *, endpoint_id: str | None = None
    ) -> bool:
        if self.registry is None:
            return False
        task_id = str(binding.get("taskId") or "").strip()
        page_id = str(binding.get("pageId") or "").strip()
        if not task_id or not page_id:
            return False
        task = self.registry.tasks_by_id.get(task_id)
        if not isinstance(task, Mapping) or str(task.get("status") or "").upper() != "RUNNING":
            return False
        active_hop_id = task.get("active_hop_id")
        active_hop = next(
            (
                item
                for item in task.get("hops") or ()
                if isinstance(item, Mapping) and item.get("hop_id") == active_hop_id
            ),
            None,
        )
        if not isinstance(active_hop, Mapping):
            return False
        logical_role = str(active_hop.get("target_role") or "").upper()
        if not logical_role:
            return False
        role_record = (task.get("roles") or {}).get(logical_role)
        return bool(
            isinstance(role_record, Mapping)
            and (endpoint_id is None or str(role_record.get("browser_endpoint_id") or "") == endpoint_id)
            and str(role_record.get("page_id") or "").strip() == page_id
            and str(active_hop.get("state") or "") in {
                "pre_send", "sending", "sent", "waiting", "responded"
            }
        )

    async def _maintain_ambient_page_automation(
        self, browser_context: Any, *, endpoint_id: str | None = None
    ) -> None:
        """Install once on CDPA-history pages; active/history consumers share evidence."""
        from .role_runtime.page_observer import observer_for
        from urllib.parse import urlparse
        now = time.monotonic()
        live = set()
        known_urls = None
        for page in tuple(browser_context.pages):
            if page.is_closed():
                continue
            key = id(page)
            live.add(key)
            due = now >= self._ambient_next_probe_at.get(key, 0)
            if key not in self._ambient_binding_cache or due:
                try:
                    name = str(await page.evaluate("() => window.name || ''"))
                    binding = json.loads(name[len(WINDOW_NAME_PREFIX):]) if name.startswith(WINDOW_NAME_PREFIX) else None
                except Exception:
                    binding = None
                if not isinstance(binding, Mapping) or not binding.get("role"):
                    # A manually reopened historical conversation has a new Page and
                    # no window.name. The existing registry supplies its URL/team mapping.
                    if known_urls is None:
                        known_urls = {}
                        for task in (self.registry.tasks_by_id.values() if self.registry else ()):
                            for record in (task.get("roles") or {}).values():
                                if endpoint_id is not None and str(record.get("browser_endpoint_id") or "") != endpoint_id:
                                    continue
                                url = str(record.get("page_url") or "")
                                if url:
                                    known_urls[url] = {"team": task.get("team"), "taskId": task.get("task_id"),
                                                       "history_only": True}
                    binding = known_urls.get(str(page.url))
                self._ambient_binding_cache[key] = dict(binding) if isinstance(binding, Mapping) else None
            binding = self._ambient_binding_cache.get(key)
            if not isinstance(binding, Mapping):
                self._ambient_next_probe_at[key] = now + 5
                continue
            if self.registry is not None:
                task_id = str(binding.get("taskId") or "").strip()
                if not task_id:
                    continue
                owner = self.registry.tasks_by_id.get(task_id)
                if not isinstance(owner, Mapping):
                    continue  # Another worker's or unrelated tab is never our automation surface.
                if str(owner.get("status") or "").upper() in TERMINAL:
                    continue
                retired = False
                for hop in owner.get("hops", []):
                    if not isinstance(hop, Mapping) or hop.get("state") != "abandoned":
                        continue
                    hop_endpoint = hop.get("browser_endpoint_id", self.store.endpoint_id_for(owner))
                    if endpoint_id is not None and hop_endpoint != endpoint_id:
                        continue
                    retired_page_id = None
                    receipt = hop.get("receipt")
                    if isinstance(receipt, Mapping) and isinstance(receipt.get("binding"), Mapping):
                        retired_page_id = receipt["binding"].get("page_id")
                    if retired_page_id is None and hop.get("ledger_path") and hop.get("request_id"):
                        try:
                            durable = RequestLedger(str(hop["ledger_path"])).peek(str(hop["request_id"]))
                            retired_page_id = getattr(getattr(durable, "binding", None), "page_id", None)
                        except Exception:
                            retired_page_id = None
                    if retired_page_id is None or binding.get("history_only") or retired_page_id == binding.get("pageId"):
                        retired = True
                        break
                if retired:
                    continue  # Never approve delayed tools on an execution superseded by recovery.
            client = ChatGPTPage(page, timeout_ms=min(15_000, round(self.config.workspace_timeout_seconds * 1000)))
            if binding.get("pageId") and binding.get("role"):
                client.binding = PageBinding(str(binding["pageId"]), str(binding["role"]))
            try:
                await client.install_page_observer()
                if urlparse(str(page.url)).hostname not in {"chatgpt.com", "www.chatgpt.com"}:
                    continue
                if self._ambient_page_has_active_controller(binding, endpoint_id=endpoint_id):
                    continue
                observer = observer_for(page)
                if not due and not observer.dom_wake.is_set() and not observer.network_wake.is_set():
                    continue
                observer.dom_wake.clear()
                observer.network_wake.clear()
                self._ambient_next_probe_at[key] = now + 5
                wait = self._ambient_controller_states.setdefault(key, {})
                await self.role_controller.maintain_history(client, wait)
            except Exception as exc:
                if is_cdp_disconnect(exc):
                    raise
                self._ambient_next_probe_at[key] = now + 5
        for mapping in (self._ambient_controller_states, self._ambient_next_probe_at, self._ambient_binding_cache):
            for key in set(mapping) - live:
                mapping.pop(key, None)

    def _set_pool_wait(self, state: Mapping[str, Any], *, waiting: bool) -> dict[str, Any]:
        """Resource waiting is a durable pause of the same hop, never a task replacement."""
        def mutate(current: dict[str, Any]) -> None:
            prior = current.get("waiting") or {}
            is_pool_wait = current.get("status") == "WAITING" and prior.get("reason") == "browser_pool"
            if waiting:
                if current.get("status") not in {"INBOX", "RUNNING"}:
                    return
                current["waiting"] = {
                    "reason": "browser_pool", "since": utc_now(), "waiting_on": [], "stopped": [], "missing": [],
                    "resume_status": current["status"], "resume_column": current["kanban_column"],
                    "resume_action": current["active_action"],
                }
                current.update(status="WAITING", kanban_column="WAITING", active_action="waiting_browser_capacity",
                               waiting_code="browser_pool", waiting_reason="Waiting for usable browser capacity (health, login, cooldown or slots).")
            elif is_pool_wait:
                current.update(status=prior["resume_status"], kanban_column=prior["resume_column"],
                               active_action=prior["resume_action"], waiting_code=None, waiting_reason=None,
                               waiting={"reason": None, "since": None, "waiting_on": [], "stopped": [], "missing": []})
        saved = self.store.update(str(state["manifest_path"]), mutate)
        self._remember_manifest(Path(str(saved["manifest_path"])), saved)
        if self.registry is not None:
            self.registry.update_task(saved, now=time.time())
        return saved

    async def run_once(
        self,
        browser_context: Any | None,
        *,
        endpoint_id: str | None = None,
        publish_browser: bool = True,
        endpoint_pool: Any | None = None,
    ) -> list[dict[str, Any] | None]:
        if self._browser_cycle_active:
            return []
        hydrated_now = False
        if self.registry is None and not self.runtime_degraded:
            self.hydrate_runtime()
            hydrated_now = True
        if self.runtime_degraded or self.registry is None:
            self._publish_heartbeat()
            return []
        contexts: dict[str, Any] = dict(
            endpoint_pool.contexts if endpoint_pool is not None else {}
        )
        pool_errors: list[str] = []
        if endpoint_pool is None and browser_context is None:
            raise ValueError("single-endpoint cycle requires a browser context")
        self._browser_cycle_active = True
        cycle_completed = False
        running: dict[asyncio.Task[Any], tuple[str, Path, str | None]] = {}
        try:
            if endpoint_pool is not None:
                for current_endpoint_id, context in list(contexts.items()):
                    try:
                        await self._maintain_ambient_page_automation(context, endpoint_id=current_endpoint_id)
                    except Exception as exc:
                        if not is_cdp_disconnect(exc):
                            raise
                        pool_errors.append(f"{current_endpoint_id}: {type(exc).__name__}: {exc}")
                        contexts.pop(current_endpoint_id, None)
                        await endpoint_pool.mark_unavailable(current_endpoint_id, exc)
                if publish_browser:
                    await self._publish_browser_pool_inventory(
                        contexts,
                        endpoint_pool.snapshot(
                            running_executions=self._running_executions(),
                            cooldown_scopes=self._active_rate_limit_scopes(),
                        ),
                    )
                if contexts:
                    await self._refresh_rate_limit_cooldown(
                        next(iter(contexts.values()))
                    )
            else:
                assert browser_context is not None
                await self._maintain_ambient_page_automation(browser_context)
                if publish_browser:
                    await self._publish_browser_inventory(browser_context)
                await self._refresh_rate_limit_cooldown(browser_context)
            self._activate_independent_agents()
            if endpoint_pool is not None:
                for current_endpoint_id, context in list(contexts.items()):
                    try:
                        await self._close_idle_independent_tabs(
                            CDPATabActions(context, self.config), endpoint_id=current_endpoint_id,
                        )
                    except Exception as exc:
                        if not is_cdp_disconnect(exc):
                            raise
                        pool_errors.append(f"{current_endpoint_id}: {type(exc).__name__}: {exc}")
                        contexts.pop(current_endpoint_id, None)
                        await endpoint_pool.mark_unavailable(current_endpoint_id, exc)
            else:
                assert browser_context is not None
                await self._close_idle_independent_tabs(
                    CDPATabActions(browser_context, self.config)
                )
            now = time.time()
            due_at = now + MINIMUM_DEADLINE_SECONDS if hydrated_now else now
            results: list[dict[str, Any] | None] = []
            disconnect: BaseException | None = None
            affected: set[str] = set()
            running: dict[
                asyncio.Task[Any], tuple[str, Path, str | None]
            ] = {}
            scheduled_versions: dict[str, str] = {}
            reservations: dict[str, int] = {}

            def execution_lease_endpoints() -> dict[str, str]:
                return {
                    task_id: str(lease.get("endpoint_id") or "")
                    for task_id, lease in self._execution_leases.items()
                }

            def schedule_due(at: float, *, controls_only: bool = False) -> None:
                scheduling_tasks = list(self.registry.tasks_by_id.values())
                active_ids = {task_id for task_id, _path, _endpoint in running.values()}
                durable_running = self._running_executions(scheduling_tasks)
                for task_id in self.registry.due_task_ids(at):
                    state = self.registry.tasks_by_id[task_id]
                    pending_control = _next_requested_control(state)
                    if controls_only and pending_control is None:
                        continue
                    version = str(state.get("updated_at") or "")
                    if task_id in active_ids or scheduled_versions.get(task_id) == version:
                        continue
                    selected_endpoint = endpoint_id
                    selected_context = browser_context
                    dependency_release_only = (
                        endpoint_pool is not None
                        and str(state.get("status") or "").upper() == "WAITING"
                        and str((state.get("waiting") or {}).get("reason") or "")
                        == "dependency"
                        and bool(state.get("depends_on_task_ids"))
                        and dependency_readiness(state, scheduling_tasks).ready
                    )
                    if endpoint_pool is not None:
                        independent = state.get("independent") if isinstance(state.get("independent"), Mapping) else None
                        active_event = (
                            independent.get("active_event")
                            if independent is not None
                            and isinstance(independent.get("active_event"), Mapping)
                            else None
                        )
                        event_required_endpoint = (
                            str(active_event.get("required_browser_endpoint_id") or "").strip() or None
                            if active_event is not None
                            else None
                        )
                        pinned_endpoint = (
                            str(independent.get("browser_endpoint_id") or "").strip() or None
                            if independent is not None
                            else None
                        )
                        workflow_required_endpoint = (
                            (str(state.get("browser_endpoint_id") or "").strip() or None)
                            if independent is None and state.get("endpoint_pinned", True) is True
                            else None
                        )
                        required_endpoint = (
                            event_required_endpoint
                            or pinned_endpoint
                            or workflow_required_endpoint
                        )
                        previous_endpoint = self._execution_endpoint_id(state)
                        current_hop = next((item for item in state.get("hops", []) if item.get("hop_id") == state.get("active_hop_id")), {})
                        crossed = current_hop.get("state") in {"sent", "waiting", "responded"} or (
                            current_hop.get("state") == "sending"
                            and self._sending_hop_durable_boundary_state(current_hop) != "preboundary"
                        )
                        previous_page_observed = self._execution_page_observed(state, previous_endpoint)
                        # Accepted/ambiguous Send ownership outranks a pre-execution hard
                        # placement requirement. A missing page may recover on a peer only
                        # when no explicit required-endpoint contract would conflict with
                        # the already-accepted exact execution.
                        previous_page_usable = previous_page_observed or (
                            crossed and required_endpoint is not None
                        )
                        control_affinity = None
                        if pending_control is not None and str(pending_control.get("action") or "") in {
                            "open_tab",
                            "restart_role",
                            "new_chat",
                        }:
                            control_role = str(
                                pending_control.get("role")
                                or state.get("active_role")
                                or "PLAN"
                            ).upper()
                            control_affinity = self._execution_endpoint_id(
                                state, control_role
                            )
                        affinity = (
                            required_endpoint
                            or control_affinity
                            or self._role_endpoint_affinity(state)
                        )
                        if (
                            required_endpoint is None
                            and control_affinity is None
                            and affinity is not None
                            and not self._execution_page_observed(state, affinity)
                        ):
                            affinity = None
                        if affinity is None and not crossed and state.get("active_hop_id") == 1:
                            affinity = str(state.get("browser_endpoint_id") or "") or None
                        running_others = [
                            item
                            for item in durable_running
                            if str(item.get("task_id") or "") not in active_ids | {task_id}
                        ]
                        control_affinity_conflict = (
                            control_affinity is not None
                            and required_endpoint is not None
                            and control_affinity != required_endpoint
                        )
                        selected_endpoint = (
                            None
                            if control_affinity_conflict
                            else endpoint_pool.select_endpoint(
                                running_others,
                                affinity_endpoint_id=affinity,
                                hard_affinity=(
                                    required_endpoint is not None
                                    or control_affinity is not None
                                ),
                                cooldown_scopes=self._active_rate_limit_scopes(),
                                reservations=reservations,
                                excluded_endpoint_ids=(previous_endpoint,) if crossed and not previous_page_usable and previous_endpoint else (),
                            )
                        )
                        if required_endpoint is not None and selected_endpoint != required_endpoint:
                            selected_endpoint = None
                        protected = any(item.get("task_id") == task_id for item in durable_running)
                        previous_ready = next((item for item in endpoint_pool.snapshot()["endpoints"] if item["endpoint_id"] == previous_endpoint and item["chatgpt_ready"]), None)
                        if previous_endpoint in contexts and previous_page_usable and (
                            crossed or (previous_ready and protected and previous_ready["rate_limit_scope"] not in self._active_rate_limit_scopes())
                        ):
                            # Receiving/reconciling an existing request is not a new allocation.
                            selected_endpoint = previous_endpoint
                        endpointless_control: tuple[int, str] | None = None
                        if pending_control is not None:
                            pending_action = str(pending_control.get("action") or "")
                            pending_control_id = pending_control.get("control_id")
                            if (
                                _control_can_run_endpointless(state, pending_control)
                                and isinstance(pending_control_id, int)
                            ):
                                endpointless_control = (pending_control_id, pending_action)
                                selected_endpoint = None
                        if dependency_release_only:
                            selected_endpoint = None
                        if (
                            selected_endpoint is None
                            and endpointless_control is None
                            and not dependency_release_only
                        ):
                            if state.get("status") in {"INBOX", "RUNNING"}:
                                state = self._set_pool_wait(state, waiting=True)
                                affected.add(task_id)
                            continue
                        if (
                            selected_endpoint is not None
                            and state.get("status") == "WAITING"
                            and (state.get("waiting") or {}).get("reason") == "browser_pool"
                        ):
                            state = self._set_pool_wait(state, waiting=False)
                            affected.add(task_id)
                            scheduling_tasks = list(self.registry.tasks_by_id.values())
                        selected_context = contexts.get(selected_endpoint) if selected_endpoint else SimpleNamespace(pages=[])
                        if selected_context is None:
                            continue
                    if selected_context is None:
                        continue
                    path = self.registry.paths_by_id[task_id]
                    kwargs: dict[str, Any] = {
                        "scheduling_tasks": scheduling_tasks
                    }
                    if selected_endpoint is not None:
                        kwargs["endpoint_id"] = selected_endpoint
                    elif endpoint_pool is not None and endpointless_control is not None:
                        kwargs["expected_endpointless_control"] = endpointless_control
                    if task_id in self._execution_leases:
                        continue
                    canonical_status = str(state.get("status") or "").upper()
                    if selected_endpoint is not None and canonical_status not in TERMINAL:
                        # Runtime-registry snapshots can lag a terminal manifest while
                        # cleanup is still due. Capacity is a physical execution lease,
                        # so confirm the canonical task status before publishing LOAD.
                        latest = self.store.load(path)
                        canonical_status = str(latest.get("status") or "").upper()
                    consumes_capacity = (
                        selected_endpoint is not None
                        and canonical_status not in TERMINAL
                    )
                    if consumes_capacity:
                        hop = next((item for item in state.get("hops", []) if item.get("hop_id") == state.get("active_hop_id")), {})
                        self._execution_leases[task_id] = {
                            "endpoint_id": selected_endpoint, "task_id": task_id, "team": str(state.get("team") or ""),
                            "role": state.get("active_role"), "request_id": hop.get("request_id"), "page_id": None,
                        }
                    task = asyncio.create_task(
                        self.advance(path, selected_context, **kwargs)
                    )
                    running[task] = (task_id, path, selected_endpoint)
                    active_ids.add(task_id)
                    if endpoint_pool is not None and consumes_capacity:
                        reservations[selected_endpoint] = (
                            reservations.get(selected_endpoint, 0) + 1
                        )
                    scheduled_versions[task_id] = version

            lease_endpoints_before = execution_lease_endpoints()
            schedule_due(due_at)
            if (
                endpoint_pool is not None
                and publish_browser
                and execution_lease_endpoints() != lease_endpoints_before
            ):
                await self._publish_browser_pool_inventory(
                    contexts,
                    endpoint_pool.snapshot(
                        running_executions=self._running_executions(),
                        cooldown_scopes=self._active_rate_limit_scopes(),
                    ),
                )
            while running:
                lease_endpoints_before = execution_lease_endpoints()
                done, _pending = await asyncio.wait(
                    tuple(running),
                    timeout=MINIMUM_DEADLINE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task_id, path, selected_endpoint = running.pop(task)
                    self._execution_leases.pop(task_id, None)
                    if endpoint_pool is not None and selected_endpoint is not None:
                        reservations[selected_endpoint] = max(
                            0, reservations.get(selected_endpoint, 0) - 1
                        )
                    try:
                        result = task.result()
                    except BaseException as exc:
                        result = exc
                    if isinstance(result, BaseException):
                        if is_cdp_disconnect(result):
                            if endpoint_pool is None:
                                disconnect = disconnect or result
                            else:
                                pool_errors.append(
                                    f"{selected_endpoint}: {type(result).__name__}: {result}"
                                )
                                contexts.pop(selected_endpoint, None)
                                await endpoint_pool.mark_unavailable(selected_endpoint, result)
                        else:
                            print(
                                f"cdpa-worker: manifest {path}: {type(result).__name__}: {result}",
                                file=sys.stderr,
                            )
                        results.append(None)
                    else:
                        results.append(result)
                        if isinstance(result, Mapping):
                            previous = self.registry.tasks_by_id.get(task_id)
                            merged = result
                            if previous != result and previous is not None and (
                                str(previous.get("updated_at") or "")
                                != str(result.get("updated_at") or "")
                            ):
                                canonical = self.store.load(path)
                                if str(canonical.get("updated_at") or "") != str(
                                    result.get("updated_at") or ""
                                ):
                                    merged = canonical
                            if previous != merged:
                                affected.update(
                                    self.registry.update_task(merged, now=time.time())
                                )
                        else:
                            affected.add(task_id)
                # A completed execution releases endpoint capacity. Rescan the
                # full due frontier so browser-pool waiters can enter before
                # unrelated running tasks drain; active/version guards prevent
                # duplicate scheduling.
                schedule_due(time.time())
                if (
                    endpoint_pool is not None
                    and publish_browser
                    and execution_lease_endpoints() != lease_endpoints_before
                ):
                    await self._publish_browser_pool_inventory(
                        contexts,
                        endpoint_pool.snapshot(
                            running_executions=self._running_executions(),
                            cooldown_scopes=self._active_rate_limit_scopes(),
                        ),
                    )
            if disconnect is not None:
                raise disconnect
            self._publish_affected(affected)
            self._activate_independent_agents()
            if endpoint_pool is not None:
                if publish_browser:
                    await self._publish_browser_pool_inventory(
                        contexts,
                        endpoint_pool.snapshot(
                            running_executions=self._running_executions(),
                            cooldown_scopes=self._active_rate_limit_scopes(),
                        ),
                    )
                self._publish_heartbeat(
                    browser_connected=bool(contexts),
                    browser_error="; ".join(pool_errors) or None,
                )
            elif publish_browser:
                self._publish_heartbeat(browser_connected=True)
            cycle_completed = True
            return results
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            for task_id, _path, _endpoint in running.values():
                self._execution_leases.pop(task_id, None)
            self._browser_cycle_active = False
            if not cycle_completed and endpoint_pool is not None and publish_browser:
                self._publish_browser_disconnected()

    async def run_once_pool(self, endpoint_pool: Any) -> list[dict[str, Any] | None]:
        self._endpoint_pool = endpoint_pool
        return await self.run_once(None, endpoint_pool=endpoint_pool)

    async def run_pool_forever(self, endpoint_pool: Any) -> None:
        first_refresh = True
        while True:
            self._finalize_drained_endpoints()
            await endpoint_pool.refresh(wait=first_refresh)
            first_refresh = False
            await self.run_once_pool(endpoint_pool)
            if not endpoint_pool.contexts:
                self._publish_heartbeat(force=True, browser_connected=False, browser_error="no usable browser endpoint")
            now = time.time()
            next_due = self.registry.next_due_at() if self.registry else None
            delay = self.config.command_poll_seconds
            if next_due is not None:
                delay = min(delay, max(0.5, next_due - now))
            await asyncio.sleep(delay)

    async def run_forever(self, browser_context: Any) -> None:
        browser = getattr(browser_context, "browser", None)
        while True:
            if browser is not None and not browser.is_connected():
                raise ConnectionError("CDP browser disconnected")
            await self.run_once(browser_context)
            if browser is not None and not browser.is_connected():
                raise ConnectionError("CDP browser disconnected")
            now = time.time()
            next_due = self.registry.next_due_at() if self.registry else None
            delay = self.config.command_poll_seconds
            if next_due is not None:
                delay = min(delay, max(0.5, next_due - now))
            await asyncio.sleep(delay)


async def _run(config: CDPAConfig) -> None:
    worker = CDPAWorker(config)
    lease_path = (config.repository_root / ".runtime" / "worker-scheduler.lock").resolve()
    with exclusive_file_lock(lease_path, blocking=True, poll_seconds=0.1):
        ensure_playwright_dialog_autoclose_guard()
        worker._worker_lease_acquired_at = utc_now()
        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        command_task: asyncio.Task[Any] | None = None
        endpoint_pool: EndpointPool | None = None
        try:
            worker.runtime_db.ensure_schema()
            worker._restore_rate_limit_cooldown()
            worker.runtime_db.requeue_running_commands()
            await asyncio.to_thread(worker.store.recover_phase4_replacement)
            command_task = asyncio.create_task(worker.run_command_loop())
            worker._publish_heartbeat(force=True)
            await asyncio.to_thread(worker.hydrate_runtime, startup=False)
            worker._publish_heartbeat(force=True)
            endpoint_pool = EndpointPool(
                worker.store.endpoints,
                retry_delay=max(1.0, min(15.0, config.worker_poll_seconds * 2)),
            )
            worker._endpoint_pool = endpoint_pool
            await worker.run_pool_forever(endpoint_pool)
        finally:
            if command_task is not None:
                command_task.cancel()
            heartbeat_task.cancel()
            pending = [heartbeat_task]
            if command_task is not None:
                pending.append(command_task)
            await asyncio.gather(*pending, return_exceptions=True)
            for task in tuple(worker._browser_command_tasks):
                task.cancel()
            await asyncio.gather(*worker._browser_command_tasks, return_exceptions=True)
            if endpoint_pool is not None:
                await endpoint_pool.close()
            worker.runtime_db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the persistent CDPA task worker")
    parser.add_argument("--config", default=None)
    parser.add_argument("--repository", default=".")
    parser.add_argument("--hydrate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_cdpa_config(
            args.config,
            repository_root=Path(args.repository).expanduser().resolve(),
        )
        if args.hydrate_only:
            CDPAWorker(config).hydrate_runtime(read_only=True)
        else:
            asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"cdpa-worker: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
