from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import os
import random
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

from .cdpa_actions import (
    AcquiredRole,
    BranchBootstrapError,
    BranchTargetUnresolvedError,
    CDPATabActions,
    RoleOwnershipError,
    TeamCloseError,
    is_transient_page_lifecycle_error,
)
from .cdpa_bootstraps import BootstrapCatalog, normalize_bootstrap_donor, normalize_bootstrap_record
from .cdpa_commands import (
    RepairRequest,
    WorkerCommand,
    command_snapshot,
    conversation_identity,
    validate_worker_command,
)
from .cdpa_config import CDPAConfig, load_cdpa_config, remote_repository_from_task
from .cdpa_browser_projection import build_browser_projection
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
from .cdpa_safety import sanitize_exception, sanitize_text
from .cdpa_routes import (
    InlineReportMaterializationError,
    RouteContractError,
    expected_report_relative,
    materialize_inline_report,
    parse_role_response,
)
from .cdpa_store import (
    TaskStore,
    is_replaced_immutable_history,
    task_goal_for_hop,
    report_mode_from_options,
    utc_now,
)
from .cdpa_team import cleanup_eligible, has_other_nonterminal_team_work
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
    resolve_completed_file_write,
    resolve_exact_new_user_message,
    resolve_exact_user_message,
    resolve_unique_exact_user_message,
    resolve_terminal_assistant,
    resolve_bootstrap_donor,
    resolve_inherited_assistant,
    resolve_latest_terminal_assistant,
)
from .connection import connected_browser, is_cdp_disconnect
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
_CONSECUTIVE_SELF_ROUTE_LIMIT = 3
_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE = "consecutive_self_route_limit"
_STATUS_RECOVERY_GRAPH_SECONDS = 300.0
_TERMINAL_GRAPH_RETRY_SECONDS = 120.0
_TERMINAL_GRAPH_MAX_ATTEMPTS = 3
_DOM_FALLBACK_SETTLE_SECONDS = 30.0
_POST_REFRESH_RESPONSE_PROBE_SECONDS = 60.0
_POST_REFRESH_REROUTE_SECONDS = 120.0
_STALL_REROUTE_MAX_ATTEMPTS = 3
_STREAM_STATUS_POLL_MIN_SECONDS = 10.0
_STREAM_STATUS_POLL_MAX_SECONDS = 15.0
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


class IneffectiveControlError(RuntimeError):
    """The primitive ran, but its required operational postcondition did not hold."""


class BootstrapUIBranchError(RuntimeError):
    """The bounded semantic UI bootstrap branch failed before Send."""


def _active_hop(state: Mapping[str, Any]) -> dict[str, Any]:
    active = state.get("active_hop_id")
    for hop in state.get("hops") or []:
        if isinstance(hop, dict) and hop.get("hop_id") == active:
            return hop
    raise RuntimeError(f"active hop {active!r} does not exist")


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
    identity = conversation_identity(value)
    if identity is None or identity.rsplit("/", 1)[-1].startswith("WEB:"):
        return None
    return identity


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
        configure_action_delays(
            config.delay_minimum_seconds,
            config.delay_maximum_seconds,
            config.delay_multipliers,
        )
        self.store = store or TaskStore(config)
        self.prompts = PromptBuilder(config)
        self.runtime_db = RuntimeDB(config.runtime_database)
        self.registry: CDPARuntimeRegistry | None = None
        self.runtime_degraded = False
        self._last_heartbeat_at = 0.0
        self._last_browser_inventory_at = 0.0
        self._last_browser_page_count = -1
        self._browser_projection: dict[str, Any] | None = None
        self._browser_inventory_task: asyncio.Task[Any] | None = None
        self._browser_connected = False
        self._browser_error: str | None = None
        self._browser_cycle_active = False
        self._rate_limit_cooldown: dict[str, Any] | None = None
        self._rate_limit_lock = asyncio.Lock()
        self._send_gate_lock = asyncio.Lock()
        self._last_automated_send_at: float | None = None
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
        try:
            mappings = self.store.repository_projects()
            project_id = mappings.get(repository)
            if project_id is None:
                async with self._repository_project_lock:
                    mappings = self.store.repository_projects()
                    project_id = mappings.get(repository)
                    if project_id is None:
                        name = Path(repository).name
                        if any(Path(path).name == name for path in mappings if path != repository):
                            suffix = hashlib.sha256(repository.encode()).hexdigest()[:8]
                            name = f"{name}-{suffix}"
                        remote = await backend_projects(browser_context)
                        if any(item["name"] == name for item in remote):
                            return
                        project_id = await backend_create_project(browser_context, name)
                        self.store.set_repository_project(repository, project_id)
            await backend_set_conversation_project(browser_context, conversation_id, project_id)
        except Exception:
            return

    def _schedule_repository_project(
        self, state: Mapping[str, Any], hop_id: int, browser_context: Any
    ) -> None:
        if is_independent_task(state):
            return
        hop = next(
            (item for item in state.get("hops") or [] if item.get("hop_id") == hop_id),
            None,
        )
        receipt = hop.get("receipt") if isinstance(hop, Mapping) else None
        repository = state.get("repository")
        conversation_id = receipt.get("conversation_id") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(hop, Mapping)
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

    def _rate_limit_gate_active(self) -> bool:
        cooldown = self._rate_limit_cooldown
        if not isinstance(cooldown, Mapping) or cooldown.get("state") != "active":
            return False
        release_at = parse_time(cooldown.get("release_not_before"))
        if release_at is not None and datetime.now(timezone.utc) >= release_at:
            self._rate_limit_cooldown = None
            return False
        return True

    def _restore_rate_limit_cooldown(self) -> None:
        if self._rate_limit_cooldown is not None:
            return
        snapshot = self.runtime_db.get_snapshot("worker")
        payload = snapshot.get("payload") if isinstance(snapshot, Mapping) else None
        cooldown = (
            payload.get("rate_limit_cooldown")
            if isinstance(payload, Mapping)
            else None
        )
        if isinstance(cooldown, Mapping):
            restored = dict(cooldown)
            if restored.get("state") == "active":
                self._rate_limit_cooldown = restored
                self._rate_limit_gate_active()

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
            cooldown_seconds = float(self.rate_limit_cooldown_seconds)
            self._rate_limit_cooldown = {
                "state": "active",
                "detected_at": now.isoformat(),
                "release_not_before": (
                    now + timedelta(seconds=cooldown_seconds)
                ).isoformat(),
                "reason": sanitize_text(error, max_chars=500),
                "profile": str(self.config.cdp_url),
                "detector_task_id": str(state.get("task_id") or "") or None,
                "detector_role": role or None,
            }
            self._publish_heartbeat(force=True)
            return self._rate_limit_cooldown

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
        had_cooldown = self._rate_limit_cooldown is not None
        active = self._rate_limit_gate_active()
        cleared = had_cooldown and not active and self._rate_limit_cooldown is None
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

    def _block(
        self,
        state: dict[str, Any],
        error: BaseException | str,
        *,
        code: str = "unexpected_error",
        retryable: bool = False,
    ) -> dict[str, Any]:
        message = sanitize_text(error, max_chars=2000)
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
        start_wait_budget(
            hop["wait"],
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
        changed = (
            acquired.created
            or acquired.new_chat
            or record.get("status") != "active"
            or str(record.get("page_id") or "") != str(acquired.page_id)
            or str(record.get("page_url") or "") != str(acquired.url)
            or record.get("online") is not True
            or record.get("last_error") is not None
            or bool(record.get("reset_requested"))
            or not record.get("last_activity_at")
        )
        if acquired.new_chat:
            record["conversation_generation"] = int(record.get("conversation_generation") or 0) + 1
        generation = int(record.get("conversation_generation") or 0)
        if record.get("reset_requested"):
            record["reset_applied_generation"] = generation
            record["reset_requested"] = False
        record.update(
            {
                "status": "active",
                "page_id": acquired.page_id,
                "page_url": acquired.url,
                "online": True,
                "last_error": None,
            }
        )
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

    async def _owned_or_block(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
    ) -> AcquiredRole | None:
        try:
            hop, expected_conversation, expected_page_id, _ = (
                self._active_recovery_context(state, role)
            )
            acquired = await actions.locate_owned(state, role)
            if acquired is not None:
                await self._dismiss_known_rate_limit_on_existing(acquired)
            live_conversation = (
                _recoverable_conversation_identity(acquired.url)
                if acquired is not None
                else None
            )
            if acquired is not None and live_conversation is not None and expected_conversation is None:
                expected_conversation = live_conversation
                hop["conversation_url"] = acquired.url
            needs_reopen = (
                acquired is None
                or (expected_conversation is not None and live_conversation != expected_conversation)
                or (expected_page_id is not None and str(acquired.page_id) != expected_page_id)
            )
            if needs_reopen:
                if expected_conversation is None:
                    physical_role = state["roles"][role]["physical_role"]
                    raise RoleOwnershipError(
                        f"owned {physical_role} tab is offline", code="role_offline"
                    )
                if self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    return None
                acquired = await self._recover_active_role(state, role, actions)
        except RateLimitBlockedError as exc:
            await self._enter_rate_limit_cooldown(state, actions, exc)
            state["active_action"] = "rate_limit_cooldown"
            return None
        except Exception as exc:
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
        cleanup["last_error"] = detail
        cleanup["last_error_at"] = now
        cleanup["retry_count"] = int(cleanup.get("retry_count") or 0) + 1
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

    async def _continue_cleanup(
        self,
        state: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        *,
        preflighted_pages: list[Any] | None = None,
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
                    acquired = await actions.locate_owned(state, active_role)
                    if acquired is not None:
                        stopped = await actions.stop_if_active(acquired)

                def persist_stop(current: dict[str, Any]) -> dict[str, Any]:
                    current_cleanup = current.setdefault("cleanup", {})
                    current_cleanup["stopped_response"] = bool(stopped)
                    current_cleanup["phase"] = "close_pending"
                    current_cleanup["last_error"] = None
                    current_cleanup["last_error_at"] = None
                    current["active_action"] = "cleanup_close_pending"
                    return current

                persist(persist_stop)
                phase = "close_pending"
            if phase in {"close_pending", "closing"}:
                selected = (
                    list(preflighted_pages)
                    if preflighted_pages is not None
                    else await actions.preflight_team(state)
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
                    current["active_action"] = "cleanup_verify_pending"
                    return current

                persist(persist_closed)
                phase = "verify_pending"
            if phase == "verify_pending":
                remaining = await actions.preflight_team(state)
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
            return current

        saved = self.store.update(manifest_path, merge)
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

        saved = self.store.update(manifest_path, merge)
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
                if self._rate_limit_gate_active():
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
            acquired = await actions.locate_owned(state, role)
            stopped_response = (
                await actions.stop_if_active(acquired) if acquired is not None else False
            )
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
        control = next(
            (
                item
                for item in state.get("controls") or []
                if isinstance(item, dict) and item.get("status") == "requested"
            ),
            None,
        )
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
            == _CONSECUTIVE_SELF_ROUTE_BLOCK_CODE
        )
        resume_completed = False
        result: Any = None
        command: WorkerCommand | None = None
        try:
            command_value = control.get("command")
            if isinstance(command_value, Mapping):
                command = WorkerCommand.from_dict(command_value)
                validate_worker_command(command, state)
                if command.action != action or command.role != control.get("role"):
                    raise ValueError("worker command payload does not match queued control")
                control["command_state"] = "RUNNING"
            hop = _active_hop(state) if state.get("active_hop_id") is not None else None
            hop_state = str(hop.get("state") or "") if hop else ""
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
            elif action == "pause":
                if state.get("status") not in {"INBOX", "RUNNING"}:
                    raise RuntimeError("Pause is valid only for an INBOX or RUNNING task")
                state["resume_column"] = state.get("kanban_column")
                state["status"] = "PAUSED"
                state["kanban_column"] = "PAUSED"
                state["pause_reason"] = control.get("reason") or "manual pause"
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
                if role_paused:
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
                        (
                            hop.get("expected_report_path")
                            if hop.get("report_sha256") is not None
                            else hop.get("report_path")
                        )
                        or ""
                    ).strip()
                    if source_role not in state.get("roles", {}) or not handoff:
                        raise RuntimeError(
                            "role-initiated pause lost its persisted route handoff"
                        )
                    before = command_snapshot(baseline, role=source_role)
                    child = self._append_hop(
                        state,
                        source_role=source_role,
                        target_role=source_role,
                        handoff=handoff,
                    )
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
                    else:
                        state["kanban_column"] = _column_for(
                            str(state.get("active_role") or "PLAN")
                        )
                    state["status"] = "RUNNING" if status != "INBOX" else "INBOX"
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
            elif action == "stop":
                if state.get("status") in TERMINAL:
                    raise RuntimeError("Stop is invalid for a terminal task")
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
                if existing_role is None and self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
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
                        new_hop = self._append_hop(
                            state,
                            source_role=role,
                            target_role=role,
                            handoff=str(hop["handoff"]),
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
                if existing_role is None and self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
                else:
                    acquired = await actions.new_chat(state, role)
                    await self._dismiss_known_rate_limit_on_existing(acquired)
                    self._record_acquired(state, role, acquired)
                    state["roles"][role]["constructor_sent_generation"] = None
                    result = {"page_id": acquired.page_id, "new_chat": True}
            elif action == "open_tab":
                blocked_role_recovery = _is_verified_role_offline_block(state, role)
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
                    role_record = state["roles"][role]
                    conversation_url = str(hop.get("conversation_url") or "").strip()
                    accepted_waiting_reopen = (
                        hop_state == "waiting"
                        and bool(
                            str(receipt.get("user_message_id") or "").strip()
                            or str(receipt.get("user_turn_id") or "").strip()
                        )
                        and conversation_url
                        == str(role_record.get("page_url") or "").strip()
                        and str(binding.get("page_id") or "").strip()
                        == str(role_record.get("page_id") or "").strip()
                    )
                    if hop_state != "pre_send" and not accepted_waiting_reopen:
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
                    command_canonical = _recoverable_conversation_identity(
                        command_conversation
                    )
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

                async def acquire_open_tab() -> AcquiredRole:
                    nonlocal blocked_role_recovery
                    if blocked_role_recovery:
                        try:
                            return await self._recover_active_role(
                                state, role, actions, foreground=True
                            )
                        except RoleOwnershipError as exc:
                            raise IneffectiveControlError(str(exc)) from exc
                    current = await actions.locate_owned(state, role)
                    if current is None:
                        return await actions.reopen(state, role)
                    await actions.open_tab(current)
                    return current

                locator = getattr(actions, "locate_owned", None)
                existing_open_tab = await locator(state, role) if callable(locator) else True
                if existing_open_tab is None and self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    result = {"deferred": True, "reason": "rate_limit_cooldown"}
                    acquired = None
                else:
                    acquired = await acquire_open_tab()
                if acquired is None:
                    recovered = False
                else:
                    await self._dismiss_known_rate_limit_on_existing(acquired)
                    recovered = blocked_role_recovery or bool(acquired.created)
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
                            "OPEN_ROLE_TAB did not restore the accepted-send page binding"
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
                    if blocked_role_recovery:
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
                    await self._continue_cleanup(state, actions, manifest_path)
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
                action == "clear_team"
                and manifest_path is not None
                and isinstance(control_id, int)
            ):
                saved = self.store.reject_control(
                    manifest_path,
                    control_id,
                    detail,
                    action="clear_team",
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
        if target_role not in state["roles"]:
            raise RouteContractError(
                f"route {target_role!r} is not selected for this task"
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
        state.setdefault("hops", []).append(hop)
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
        elif self._rate_limit_gate_active():
            state["active_action"] = "rate_limit_cooldown"
            return None
        try:
            acquired = await actions.acquire(state, role)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            return acquired
        except RoleOwnershipError as exc:
            if _role_ownership_block_code(exc) != "role_offline":
                raise
            _, expected_conversation, _, _ = self._active_recovery_context(state, role)
            if expected_conversation is None:
                raise
            if self._rate_limit_gate_active():
                state["active_action"] = "rate_limit_cooldown"
                return None
            acquired = await self._recover_active_role(state, role, actions)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            return acquired
        except UnsafePageStateError as exc:
            if str(exc).strip().casefold() != "page is already responding":
                raise
            acquired = await actions.locate_owned(state, role)
            if acquired is None:
                raise RoleOwnershipError(
                    "active response page disappeared during pre-send reconciliation"
                ) from exc
            state["active_action"] = "reconcile_page_response"
            try:
                await acquired.client.wait_until_clean_ready(
                    timeout_ms=min(
                        3_000,
                        max(500, int(self.config.worker_poll_seconds * 1000)),
                    ),
                    poll_ms=100,
                )
            except TimeoutError:
                return None
            try:
                return await actions.acquire(state, role)
            except UnsafePageStateError as retry_exc:
                if str(retry_exc).strip().casefold() == "page is already responding":
                    state["active_action"] = "reconcile_page_response"
                    return None
                raise

    @staticmethod
    def _stream_status_poll_delay() -> float:
        return random.uniform(
            _STREAM_STATUS_POLL_MIN_SECONDS, _STREAM_STATUS_POLL_MAX_SECONDS
        )

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
        current = BootstrapCatalog(self.config.repository_root).get(bootstrap_id)
        if current is not None and current.get("enabled") is True:
            return current
        try:
            normalized = normalize_bootstrap_record(snapshot)
        except ValueError:
            return None
        return normalized if normalized.get("enabled") is True else None

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
        try:
            graph = await actions.backend_conversation(source_id)
            assistant = resolve_latest_terminal_assistant(graph)
        except BackendUnavailableError as exc:
            if exc.status_code in {404, 410}:
                state["bootstrap_source_exhausted"] = True
                return dict(bootstrap), False
            state["active_action"] = "bootstrap_retry"
            return dict(bootstrap), True
        except BackendNotReadyError:
            state["active_action"] = "bootstrap_retry"
            return dict(bootstrap), True
        except BackendError:
            state["active_action"] = "bootstrap_retry"
            return dict(bootstrap), True

        donor = normalize_bootstrap_donor(
            {
                "conversation_id": source_id,
                "assistant_message_id": assistant.message_id,
            }
        )
        updated = BootstrapCatalog(self.config.repository_root).add_donor(
            str(bootstrap["bootstrap_id"]), donor
        )
        state["bootstrap"] = updated
        state["bootstrap_source_materialized"] = donor
        return updated, False

    async def _regenerate_bootstrap_donor(
        self,
        state: dict[str, Any],
        bootstrap: Mapping[str, Any],
        actions: CDPATabActions,
    ) -> dict[str, Any] | None:
        prewarm_prompt = str(bootstrap.get("prewarm_prompt") or "").strip()
        if not prewarm_prompt:
            return None
        async with self._bootstrap_prepare_lock:
            current = self._bootstrap_for_state(state) or dict(bootstrap)
            if current.get("donors"):
                return current
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
                    "BOOTSTRAP", allow_create=not self._rate_limit_gate_active()
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
                if self._rate_limit_gate_active():
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

    async def _branch_from_bootstrap_ui(
        self,
        state: Mapping[str, Any],
        role: str,
        actions: CDPATabActions,
        donor: Mapping[str, Any],
    ) -> AcquiredRole:
        if self._rate_limit_gate_active():
            raise RateLimitBlockedError(_RATE_LIMIT_BLOCK_MESSAGE)
        role_record = state["roles"][role]
        physical = str(role_record["physical_role"])
        task_id = str(state["task_id"])
        team = str(state["team"])
        conversation_id = str(donor["conversation_id"])
        message_id = str(donor["assistant_message_id"])
        source_url = f"https://chatgpt.com/c/{conversation_id}"
        timeout_ms = round(self.config.workspace_timeout_seconds * 1000)
        page = None
        response_listener = None
        try:
            page = await actions.browser_context.new_page()
            await page.goto(source_url, wait_until="domcontentloaded", timeout=timeout_ms)
            assistant = page.locator(
                f'[data-message-author-role="assistant"][data-message-id="{message_id}"]'
            ).first
            await assistant.wait_for(state="visible", timeout=timeout_ms)
            turn = assistant.locator(
                "xpath=ancestor::section[starts-with(@data-testid, 'conversation-turn-')][1]"
            )
            await turn.hover()
            await turn.get_by_role("button", name="More actions", exact=True).click()
            branch_response, response_listener = actions._watch_new_branch_response(page)
            await page.get_by_role(
                "menuitem", name="Branch in new chat", exact=True
            ).click()
            await page.wait_for_url(
                lambda url: str(url).split("?", 1)[0].rstrip("/") != source_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            workspace = ChatGPTWorkspace()
            client = await workspace.bind(
                physical,
                page,
                timeout_ms=timeout_ms,
                force_new_page_id=True,
            )
            await actions._sanitize_new_branch_composer(client, timeout_ms=timeout_ms)
            await client.wait_until_clean_ready(timeout_ms=timeout_ms)
            candidate_id = await actions._new_branch_conversation_id(
                branch_response, timeout_ms=timeout_ms
            )
            await actions.validate_branch_target(
                client,
                source_conversation_id=conversation_id,
                candidate_conversation_id=candidate_id,
                physical_role=physical,
                task_id=task_id,
                team=team,
            )
            await client.bind_task_identity(task_id, team)
            snapshot = await client.assert_ownership()
            binding = client.binding
            if (
                binding is None
                or str(snapshot.page_id or "") != binding.page_id
                or str(snapshot.page_role or "") != physical
                or str(snapshot.page_task_id or "") != task_id
                or str(snapshot.page_team or "") != team
            ):
                raise BootstrapUIBranchError(
                    "UI bootstrap branch target ownership did not persist"
                )
            return AcquiredRole(
                client=client,
                page_id=binding.page_id,
                url=str(snapshot.url),
                created=True,
                new_chat=True,
                conversation_id=candidate_id,
            )
        except Exception as exc:
            rate_limit_error = None
            if page is not None and not page.is_closed() and not isinstance(
                exc,
                (
                    BootstrapUIBranchError,
                    BranchBootstrapError,
                    ChatGPTAutomationError,
                ),
            ):
                try:
                    if await ChatGPTPage(page, timeout_ms=timeout_ms).known_rate_limit_visible():
                        rate_limit_error = RateLimitBlockedError(
                            "request rate limit blocks UI bootstrap branch"
                        )
                except Exception:
                    pass
            if page is not None and not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass
            if rate_limit_error is not None:
                raise rate_limit_error from exc
            if isinstance(
                exc,
                (
                    BootstrapUIBranchError,
                    ComposerConflictError,
                    ManualInputPendingError,
                    RateLimitBlockedError,
                ),
            ):
                raise
            raise BootstrapUIBranchError(
                f"UI bootstrap branch failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if page is not None and response_listener is not None:
                page.remove_listener("response", response_listener)

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
        bootstrap = self._bootstrap_for_state(state)
        role_record = state["roles"][role]
        first_allocation = (
            isinstance(state.get("bootstrap"), Mapping)
            and not role_record.get("page_id")
            and not role_record.get("page_url")
            and int(role_record.get("conversation_generation") or 0) == 0
            and role_record.get("context_source") is None
        )
        if not first_allocation:
            return await self._acquire_existing_role(state, role, actions)
        if bootstrap is None:
            self._block(
                state,
                "Selected bootstrap record is unavailable; select another bootstrap before Send.",
                code="bootstrap_unavailable",
                retryable=False,
            )
            return None

        owned = await actions.locate_owned(state, role)
        if owned is not None:
            await self._dismiss_known_rate_limit_on_existing(owned)
            return owned
        if self._rate_limit_gate_active():
            state["active_action"] = "rate_limit_cooldown"
            return None

        catalog = BootstrapCatalog(self.config.repository_root)
        saw_transient = False
        for donor in self._bootstrap_donor_candidates(state, bootstrap):
            try:
                graph = await actions.backend_conversation(donor["conversation_id"])
                resolve_bootstrap_donor(graph, donor["assistant_message_id"])
            except BackendUnavailableError as exc:
                if exc.status_code in {404, 410}:
                    updated = catalog.remove_donor(str(bootstrap["bootstrap_id"]), donor)
                    state["bootstrap"] = updated
                    bootstrap = updated
                    self._remove_task_donor(state, donor)
                    if donor["conversation_id"] == str(
                        bootstrap.get("source_conversation_id") or ""
                    ):
                        state["bootstrap_source_exhausted"] = True
                    continue
                saw_transient = True
                continue
            except GraphIdentityError:
                updated = catalog.remove_donor(str(bootstrap["bootstrap_id"]), donor)
                state["bootstrap"] = updated
                bootstrap = updated
                self._remove_task_donor(state, donor)
                if donor["conversation_id"] == str(
                    bootstrap.get("source_conversation_id") or ""
                ):
                    state["bootstrap_source_exhausted"] = True
                continue
            except BackendError:
                saw_transient = True
                continue

            try:
                acquired = await actions.branch_from_anchor(
                    state,
                    role,
                    source_conversation_id=donor["conversation_id"],
                    assistant_message_id=donor["assistant_message_id"],
                )
                await self._reject_foreign_durable_branch_owner(state, role, acquired)
            except BranchTargetUnresolvedError as exc:
                self._block(
                    state,
                    exc,
                    code="branch_target_unresolved",
                    retryable=False,
                )
                return None
            except BranchBootstrapError:
                try:
                    acquired = await self._branch_from_bootstrap_ui(
                        state, role, actions, donor
                    )
                    try:
                        await self._reject_foreign_durable_branch_owner(
                            state, role, acquired
                        )
                    except BranchBootstrapError as exc:
                        raise BootstrapUIBranchError(str(exc)) from exc
                except BootstrapUIBranchError:
                    saw_transient = True
                    continue
            role_record["context_source"] = "bootstrap_donor"
            role_record["bootstrap_source_donor"] = dict(donor)
            return acquired

        current = self._bootstrap_for_state(state) or bootstrap
        if current.get("donors") or saw_transient:
            state["active_action"] = "bootstrap_retry"
            return None
        current, source_transient = await self._materialize_bootstrap_source_donor(
            state, current, actions
        )
        if current.get("donors"):
            return await self._acquire_workflow_role(state, role, actions)
        if source_transient:
            return None
        if str(current.get("prewarm_prompt") or "").strip():
            state["active_action"] = "bootstrap_regenerate"
            regenerated = await self._regenerate_bootstrap_donor(
                state, current, actions
            )
            if regenerated is not None and regenerated.get("donors"):
                return await self._acquire_workflow_role(state, role, actions)
            return None
        self._mark_bootstrap_unavailable(state, current)
        return None

    async def _pre_send(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        if not self._pre_send_request_identity_is_safe(state, hop):
            return
        self._normalize_legacy_pre_send_report_mode(state, hop)
        role = str(hop["target_role"])
        if self._attachment_files_for_generation(state, role) is None:
            return
        independent = state.get("independent") if is_independent_task(state) else None
        if isinstance(independent, dict) and independent.get("temporary_chat") is True:
            if hop.get("kind") == "independent_cycle":
                acquired = await actions.locate_owned(state, role)
                if acquired is None:
                    raise RoleOwnershipError(
                        "temporary independent continuation tab is offline",
                        code="role_offline",
                    )
            else:
                if self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    return
                acquired = await actions.fresh_chat(
                    state,
                    role,
                    url="https://chatgpt.com/?temporary-chat=true",
                )
            await self._dismiss_known_rate_limit_on_existing(acquired)
        elif (
            isinstance(independent, dict)
            and independent.get("new_chat_next_job") is True
            and independent.get("new_chat_deferred_task_id")
            != str(state.get("task_id") or "")
        ):
            current = await actions.locate_owned(state, role)
            if current is None and self._rate_limit_gate_active():
                state["active_action"] = "rate_limit_cooldown"
                return
            acquired = await actions.new_chat(state, role)
            await self._dismiss_known_rate_limit_on_existing(acquired)
            independent["new_chat_next_job"] = False
            independent["new_chat_deferred_task_id"] = None
        else:
            acquired = await self._acquire_workflow_role(state, role, actions)
            if acquired is None:
                return
        self._record_acquired(state, role, acquired)
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
            hop["prompt"] = built.text
            hop["prompt_sha256"] = _sha(built.text)
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
        report_repository, report_plans_root = _workflow_report_roots(self.config, state)
        expected = expected_report_relative(
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
        workflow_definitions = task_workflow_definitions(state, self.config)
        workflow_definition = workflow_definitions[role]
        bootstrap_inherited = (
            bool(workflow_definition["is_system"])
            and role_record.get("context_source")
            in {"bootstrap_donor", "bootstrap_native", "bootstrap_ui"}
            and generation == 1
        )
        allowed_routes = tuple(workflow_definitions) + ("PAUSE", "DONE")
        if hop.get("kind") == "route_repair":
            prompt = self.prompts.repair(
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                validation_error=str(hop["validation_error"]),
                report_mode=_report_mode(state),
                allowed_routes=allowed_routes,
            )
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
                constructor_sent_generation=role_record.get(
                    "constructor_sent_generation"
                ),
                conversation_generation=generation,
                report_mode=_report_mode(state),
                constructor_text=str(workflow_definition["system_prompt"]),
                is_system_role=bool(workflow_definition["is_system"]),
                bootstrap_inherited=bootstrap_inherited,
            )
            prompt = built.text
            included = built.constructor_included
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
        acquired = await self._owned_or_block(state, role, actions)
        if acquired is None:
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
        prior_accepted_role_turn = any(
            isinstance(item, Mapping)
            and item.get("hop_id") != hop.get("hop_id")
            and str(item.get("target_role") or "").upper() == role
            and isinstance(item.get("receipt"), Mapping)
            and bool(item.get("receipt"))
            for item in state.get("hops") or []
        )
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
            require_existing_conversation_baseline=prior_accepted_role_turn,
            response_timeout_ms=None,
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
        hop["conversation_url"] = post_send.conversation_url or str(post_send.url)
        hop["receipt"] = output["receipt"]
        self._canonicalize_receipt_conversation_url(state, hop)
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

    def _upgrade_legacy_receipt(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        receipt: SendReceipt,
        snapshot: Any,
        manifest_path: Path,
        persistence_baseline: dict[str, Any] | None = None,
    ) -> SendReceipt | None:
        if receipt.user_message_id or receipt.user_turn_id:
            return receipt
        accepted_user = unique_new_user_message(snapshot.messages, receipt.baseline)
        if accepted_user is None:
            self._block(
                state,
                "accepted user-message provenance is missing or ambiguous",
                code="accepted_user_provenance_ambiguous",
                retryable=False,
            )
            return None
        upgraded = replace(
            receipt,
            accepted_via="user_message_identity",
            user_message_id=accepted_user.message_id,
            user_turn_id=accepted_user.turn_id,
        )
        ledger = RequestLedger(hop["ledger_path"])
        record = ledger.get(str(hop["request_id"]))
        if record is None:
            raise RuntimeError("durable request disappeared while upgrading receipt")
        baseline = json.loads(
            json.dumps(
                persistence_baseline if persistence_baseline is not None else state,
                ensure_ascii=False,
                default=str,
            )
        )
        ledger.update(
            record.request_id,
            receipt=upgraded.to_dict(),
            error=None,
        )
        hop["receipt"] = upgraded.to_dict()
        saved = self._persist_transport_result(manifest_path, baseline, state)
        state["updated_at"] = saved["updated_at"]
        if persistence_baseline is not None:
            persistence_baseline.clear()
            persistence_baseline.update(
                json.loads(json.dumps(state, ensure_ascii=False, default=str))
            )
        return upgraded

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
        role = str(hop["target_role"])
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
        payload = hop.get("receipt")
        if not isinstance(payload, Mapping):
            return
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            return
        exact_url = f"https://chatgpt.com/c/{conversation_id}"
        hop["conversation_url"] = exact_url
        role = str(hop.get("target_role") or "").upper()
        role_record = state.get("roles", {}).get(role)
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
        if receipt.conversation_id:
            return receipt
        if receipt.attempts < 1 or not receipt.user_message_id:
            raise DurableRequestError(
                "accepted conversation discovery requires exact accepted user identity"
            )
        ledger_path = str(hop.get("ledger_path") or "").strip()
        request_id = str(hop.get("request_id") or "").strip()
        task_id = str(state.get("task_id") or "").strip()
        if not ledger_path or not request_id or not task_id:
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
        candidates = await actions.backend_search_conversations(
            task_id, max_candidates=25
        )
        verified: list[str] = []
        for conversation_id in candidates:
            graph = await actions.backend_conversation(conversation_id)
            try:
                resolve_exact_user_message(
                    graph,
                    receipt.user_message_id,
                    record.rendered_prompt,
                )
            except GraphIdentityError:
                continue
            verified.append(conversation_id)
            if len(verified) > 1:
                break
        if len(verified) != 1:
            raise GraphIdentityError(
                "accepted conversation identity could not be uniquely verified from backend search"
            )
        ledger.enrich_receipt_conversation_id(
            request_id,
            accepted_receipt=receipt,
            conversation_id=verified[0],
        )
        self._reconcile_hop_conversation_identity(state, hop)
        enriched = SendReceipt.from_dict(hop["receipt"])
        if enriched.conversation_id != verified[0]:
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
            identity = hop.get("message_identity") if isinstance(hop.get("message_identity"), Mapping) else {}
            response = {
                "role": "assistant",
                "message_id": str(identity.get("message_id") or ""),
                "turn_id": identity.get("turn_id"),
                "text": str(hop.get("response") or ""),
                "actions": [],
                "image_count": 0,
            }
        ledger.update(
            record.request_id,
            status=RequestStatus.COMPLETED,
            response=dict(response),
            error=None,
        )

    async def _final_response_reconciliation(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        acquired: AcquiredRole,
        receipt: SendReceipt,
        wait: dict[str, Any],
    ) -> bool:
        def validate_candidate(response: MessageSnapshot) -> None:
            self._validate_response_candidate(state, hop, response)

        try:
            response = await acquired.client.wait_for_response(
                receipt,
                timeout_ms=max(
                    1_000,
                    self.config.response_stable_ms
                    + (2 * self.config.response_poll_ms),
                ),
                stable_ms=self.config.response_stable_ms,
                poll_ms=self.config.response_poll_ms,
                active_reload_after_ms=None,
                stale_response_baseline=wait.get("recovery_baseline"),
                candidate_validator=validate_candidate,
                minimum_samples=2,
                invalid_grace_ms=max(1_000, self.config.response_stable_ms),
            )
        except StableMalformedResponseError as exc:
            wait["recovery_baseline"] = None
            await self._flush_hop_conversation_identity(state, hop)
            self._record_response(
                state,
                hop,
                exc.candidate,
                validation_error=str(exc.validation_error),
            )
            return True
        except (TimeoutError, IncompleteResponseTimeoutError):
            return False
        except ManualInputPendingError as exc:
            state["status"] = "PAUSED"
            state["kanban_column"] = "PAUSED"
            state["pause_reason"] = sanitize_text(exc, max_chars=2000)
            return True
        except ChoicePromptBlockedError as exc:
            self._block(
                state,
                exc,
                code="choice_prompt_blocked",
                retryable=False,
            )
            return True
        wait["recovery_baseline"] = None
        await self._flush_hop_conversation_identity(state, hop)
        self._record_response(state, hop, response)
        return True

    async def _final_dom_response_reconciliation(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        acquired: AcquiredRole,
        receipt: SendReceipt,
        wait: dict[str, Any],
    ) -> bool | None:
        try:
            return await self._final_response_reconciliation(
                state, hop, acquired, receipt, wait
            )
        except Exception as exc:
            if not is_transient_page_lifecycle_error(exc):
                raise
            hop["state"] = "waiting"
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(str(hop["target_role"]))
            state["active_action"] = "wait_response"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            return None

    async def _recover_dom_observation_failure(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        acquired: AcquiredRole,
        manifest_path: Path,
        persistence_baseline: dict[str, Any],
        exc: BaseException,
        transport_baseline: dict[str, Any] | None,
    ) -> AcquiredRole | None:
        if not is_transient_page_lifecycle_error(exc):
            return None
        wait = hop["wait"]
        last_result = wait.get("last_refresh_result")
        last_refresh = parse_time(wait.get("last_refresh_at"))
        if (
            isinstance(last_result, Mapping)
            and last_result.get("reason") == "dom_observation_recovery"
            and last_refresh is not None
            and (datetime.now(timezone.utc) - last_refresh).total_seconds()
            < float(self.config.response_refresh_after_seconds)
        ):
            hop["state"] = "waiting"
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(str(hop["target_role"]))
            state["active_action"] = "wait_response"
            return acquired

        refresh_baseline = json.loads(
            json.dumps(persistence_baseline, ensure_ascii=False, default=str)
        )
        begin_refresh(wait)
        progress = wait.get("refresh_in_progress")
        if isinstance(progress, dict):
            progress["reason"] = "dom_observation_recovery"
        self._persist_transport_result(manifest_path, refresh_baseline, state)
        refresh_baseline = json.loads(
            json.dumps(state, ensure_ascii=False, default=str)
        )
        recovered: AcquiredRole | None = None
        try:
            recovered = await actions.refresh(
                acquired,
                manifest=state,
                logical_role=str(hop["target_role"]),
                recover=True,
                skip_precheck=True,
            )
        except RoleOwnershipError as refresh_exc:
            finish_refresh(wait, error=sanitize_exception(refresh_exc))
            self._persist_transport_result(manifest_path, refresh_baseline, state)
            raise
        except Exception as refresh_exc:
            finish_refresh(wait, error=sanitize_exception(refresh_exc))
            self._persist_transport_result(manifest_path, refresh_baseline, state)
            if not is_transient_page_lifecycle_error(refresh_exc):
                raise
        else:
            finish_refresh(wait)
            saved = self._persist_transport_result(manifest_path, refresh_baseline, state)
            if transport_baseline is not None:
                state.clear()
                state.update(saved)
                transport_baseline.clear()
                transport_baseline.update(
                    json.loads(json.dumps(saved, ensure_ascii=False, default=str))
                )
                hop = _active_hop(state)
                wait = hop["wait"]

        hop["state"] = "waiting"
        state["status"] = "RUNNING"
        state["kanban_column"] = _column_for(str(hop["target_role"]))
        state["active_action"] = "wait_response"
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        return recovered

    async def _waiting_dom_snapshot(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        acquired: AcquiredRole,
        manifest_path: Path,
        persistence_baseline: dict[str, Any],
        receipt: SendReceipt,
        *,
        transport_baseline: dict[str, Any] | None,
        force_full: bool = False,
        probe_wait_ms: int = 0,
    ) -> tuple[Any | None, AcquiredRole | None]:
        try:
            snapshot = await self._waiting_snapshot(
                acquired.client,
                receipt,
                force_full=force_full,
                probe_wait_ms=probe_wait_ms,
            )
            return snapshot, acquired
        except Exception as exc:
            recovered = await self._recover_dom_observation_failure(
                state,
                hop,
                actions,
                acquired,
                manifest_path,
                persistence_baseline,
                exc,
                transport_baseline,
            )
            if is_transient_page_lifecycle_error(exc):
                return None, recovered
            raise

    @staticmethod
    async def _waiting_snapshot(
        client: Any,
        receipt: SendReceipt,
        *,
        force_full: bool = False,
        probe_wait_ms: int = 0,
    ) -> Any:
        wait_snapshot = getattr(client, "wait_snapshot", None)
        if callable(wait_snapshot):
            parameters = inspect.signature(wait_snapshot).parameters.values()
            accepts_probe_wait = any(
                parameter.name == "probe_wait_ms"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            kwargs = {"force_full": force_full}
            if accepts_probe_wait:
                kwargs["probe_wait_ms"] = max(0, int(probe_wait_ms))
            return await wait_snapshot(receipt, **kwargs)
        return await client.assert_ownership()

    def _proven_foreign_durable_user_message_ids(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        conversation_id: str,
    ) -> frozenset[str]:
        current_task = str(state.get("task_id") or "")
        current_request = str(hop.get("request_id") or "")
        proven: set[str] = set()
        for task in self.store.discover():
            task_id = str(task.get("task_id") or "")
            team = str(task.get("team") or "")
            manifest = str(task.get("manifest_path") or "")
            candidates = task.get("hops")
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                request_id = str(candidate.get("request_id") or "")
                ledger_path = str(candidate.get("ledger_path") or "")
                if not request_id or not ledger_path:
                    continue
                if task_id == current_task and request_id == current_request:
                    continue
                try:
                    record = RequestLedger(ledger_path).peek(request_id)
                except Exception:
                    continue
                if (
                    record is None
                    or record.status not in {RequestStatus.SENT, RequestStatus.COMPLETED}
                    or record.attempts < 1
                    or record.accepted_at is None
                    or record.role != str(candidate.get("physical_role") or "")
                ):
                    continue
                expected_source = {
                    "task_id": task_id,
                    "team": team,
                    "hop_id": candidate.get("hop_id"),
                    "manifest": manifest,
                }
                if record.source_context != expected_source:
                    continue
                durable_receipt = record.receipt
                if not isinstance(durable_receipt, Mapping):
                    continue
                if str(durable_receipt.get("conversation_id") or "") != conversation_id:
                    continue
                user_message_id = str(
                    durable_receipt.get("user_message_id") or ""
                ).strip()
                if not user_message_id:
                    continue
                manifest_receipt = candidate.get("receipt")
                if isinstance(manifest_receipt, Mapping):
                    manifest_user = str(
                        manifest_receipt.get("user_message_id") or ""
                    ).strip()
                    manifest_conversation = str(
                        manifest_receipt.get("conversation_id") or ""
                    ).strip()
                    if manifest_user and manifest_user != user_message_id:
                        continue
                    if manifest_conversation and manifest_conversation != conversation_id:
                        continue
                proven.add(user_message_id)
        return frozenset(proven)

    @staticmethod
    def _backend_failure_category(error: BaseException, *, prefix: str) -> str:
        if isinstance(error, BackendUnavailableError):
            return f"{prefix}_unavailable"
        if isinstance(error, BackendAuthError):
            return f"{prefix}_auth"
        if isinstance(error, BackendNotReadyError):
            return f"{prefix}_not_ready"
        if isinstance(error, GraphIdentityError):
            return f"{prefix}_identity"
        if isinstance(error, BackendSchemaError):
            return f"{prefix}_schema"
        return f"{prefix}_error"

    async def _ensure_backend_wait_source(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        receipt: SendReceipt,
    ) -> AcquiredRole | None:
        role = str(hop["target_role"])
        conversation_id = str(receipt.conversation_id or "").strip()
        expected_url = f"https://chatgpt.com/c/{conversation_id}"
        expected_conversation = _recoverable_conversation_identity(expected_url)
        hop["conversation_url"] = expected_url
        state["roles"][role]["page_url"] = expected_url
        try:
            metadata_locator = getattr(actions, "locate_owned_metadata", None)
            acquired = await (
                metadata_locator(state, role)
                if callable(metadata_locator)
                else actions.locate_owned(state, role)
            )
            live_conversation = (
                _recoverable_conversation_identity(acquired.url)
                if acquired is not None
                else None
            )
            if (
                acquired is None
                or live_conversation != expected_conversation
                or str(acquired.page_id) != str(receipt.binding.page_id)
            ):
                if self._rate_limit_gate_active():
                    state["active_action"] = "rate_limit_cooldown"
                    return None
                acquired = await actions.reopen(
                    state,
                    role,
                    require_clean_ready=False,
                    foreground=False,
                )
            await self._dismiss_known_rate_limit_on_existing(acquired)
            if _recoverable_conversation_identity(acquired.url) != expected_conversation:
                raise RoleOwnershipError(
                    "backend completion fallback reopened a different conversation"
                )
            if str(acquired.page_id) != str(receipt.binding.page_id):
                raise RoleOwnershipError(
                    "backend completion fallback changed the in-flight page identity"
                )
        except Exception as exc:
            self._block(
                state,
                exc,
                code=_role_ownership_block_code(exc) or "role_ownership_ambiguous",
                retryable=False,
            )
            return None
        self._record_acquired(state, role, acquired)
        return acquired

    async def _begin_backend_dom_fallback(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        receipt: SendReceipt,
        *,
        category: str,
        now: datetime,
    ) -> None:
        wait = hop["wait"]
        wait["completion_mode"] = "dom_fallback"
        wait["backend_fallback_category"] = category
        if category == "graph_not_ready":
            unresolved = wait.get("terminal_continuation_unresolved")
            if not isinstance(unresolved, Mapping) or unresolved.get("request_id") != str(
                hop["request_id"]
            ):
                wait["terminal_continuation_unresolved"] = {
                    "request_id": str(hop["request_id"]),
                    "started_at": now.isoformat(),
                    "refresh_baseline": int(wait.get("refresh_count") or 0),
                    "block_ready_at": None,
                }
        acquired = await self._ensure_backend_wait_source(state, hop, actions, receipt)
        if acquired is None:
            return
        await actions.wake(acquired)
        wait["dom_fallback_ready_at"] = (
            now + timedelta(seconds=_DOM_FALLBACK_SETTLE_SECONDS)
        ).isoformat()

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
            graph = await actions.backend_conversation(conversation_id)
            inherited = resolve_inherited_assistant(graph, user_message_id)
            donor = normalize_bootstrap_donor(
                {
                    "conversation_id": conversation_id,
                    "assistant_message_id": inherited.message_id,
                }
            )
        except BackendError:
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

    async def _waiting_backend_step(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        receipt: SendReceipt,
        *,
        persist_transport_state: Callable[[], None],
        resume_recovery: bool = False,
    ) -> tuple[str, str | None]:
        wait = hop["wait"]
        now = datetime.now(timezone.utc)
        mode = str(wait.get("completion_mode") or "stream_status")
        request_id = str(hop["request_id"])

        if resume_recovery and mode == "dom_fallback":
            mode = "stream_status"
            wait["completion_mode"] = mode
            wait.pop("dom_fallback_ready_at", None)
            wait["stream_status_next_poll_at"] = now.isoformat()

        if wait.get("terminal_graph_request_id") == request_id:
            wait.pop("terminal_graph_request_id", None)
            attempts = max(1, int(wait.get("terminal_graph_attempts") or 0))
            wait["terminal_graph_attempts"] = attempts
            wait["completion_mode"] = "terminal_graph_retry"
            if parse_time(wait.get("terminal_graph_ready_at")) is None:
                wait["terminal_graph_ready_at"] = (
                    now + timedelta(seconds=_TERMINAL_GRAPH_RETRY_SECONDS)
                ).isoformat()
            if attempts >= _TERMINAL_GRAPH_MAX_ATTEMPTS:
                return "dom_fallback", "graph_attempt_interrupted"
            if not resume_recovery:
                return "waiting", None
            mode = "terminal_graph_retry"

        deadline_expired = remaining_timeout_ms(wait, now=now) <= 0
        unresolved = wait.get("terminal_continuation_unresolved")
        terminal_continuation_recovery = bool(
            isinstance(unresolved, Mapping)
            and unresolved.get("request_id") == request_id
            and int(wait.get("refresh_count") or 0)
            > int(unresolved.get("refresh_baseline") or 0)
        )

        graph_mode: str | None = None
        if mode == "terminal_graph_retry":
            graph_ready_at = parse_time(wait.get("terminal_graph_ready_at"))
            if (
                graph_ready_at is not None
                and now < graph_ready_at
                and not deadline_expired
                and not resume_recovery
            ):
                return "waiting", None
            graph_mode = "terminal"
        else:
            if mode not in {"stream_status", "status_recovery"}:
                mode = "stream_status"
            wait["completion_mode"] = mode
            next_poll = parse_time(wait.get("stream_status_next_poll_at"))
            if next_poll is None:
                if mode == "status_recovery":
                    next_poll = now
                else:
                    sent_at = (
                        parse_time((hop.get("timestamps") or {}).get("sent_at"))
                        or now
                    )
                    next_poll = sent_at + timedelta(
                        seconds=self._stream_status_poll_delay()
                    )
                wait["stream_status_next_poll_at"] = next_poll.isoformat()
            if resume_recovery or (deadline_expired and now < next_poll):
                next_poll = now
                wait["stream_status_next_poll_at"] = now.isoformat()

            if now >= next_poll:
                wait["stream_status_last_poll_at"] = now.isoformat()
                wait["stream_status_poll_count"] = (
                    int(wait.get("stream_status_poll_count") or 0) + 1
                )
                try:
                    status_payload = await actions.backend_stream_status(
                        receipt.conversation_id
                    )
                    status = status_payload.get("status")
                    if status not in {"IS_STREAMING", "COMPLETE"}:
                        raise BackendSchemaError(
                            "stream_status response has unknown status"
                        )
                except BackendError as exc:
                    if resume_recovery and isinstance(exc, BackendSchemaError):
                        raise
                    mode = "status_recovery"
                    wait["completion_mode"] = mode
                    wait["backend_fallback_category"] = (
                        self._backend_failure_category(exc, prefix="status")
                    )
                    wait["stream_status_next_poll_at"] = (
                        now + timedelta(seconds=_STREAM_STATUS_RECOVERY_SECONDS)
                    ).isoformat()
                    if parse_time(wait.get("status_recovery_graph_next_at")) is None:
                        wait["status_recovery_graph_next_at"] = (
                            now + timedelta(seconds=_STATUS_RECOVERY_GRAPH_SECONDS)
                        ).isoformat()
                    persist_transport_state()
                    if deadline_expired or resume_recovery:
                        category = (
                            str(wait["backend_fallback_category"])
                            if resume_recovery
                            else "response_deadline"
                        )
                        return "dom_fallback", category
                else:
                    if status == "IS_STREAMING":
                        wait["completion_mode"] = "stream_status"
                        wait["stream_status_next_poll_at"] = (
                            now
                            + timedelta(seconds=self._stream_status_poll_delay())
                        ).isoformat()
                        for key in (
                            "backend_fallback_category",
                            "status_recovery_graph_next_at",
                            "terminal_complete_seen_at",
                            "terminal_graph_ready_at",
                            "terminal_graph_attempts",
                            "terminal_graph_request_id",
                            "terminal_graph_attempted_at",
                        ):
                            wait.pop(key, None)
                        if deadline_expired:
                            if resume_recovery:
                                wait["deadline_at"] = (
                                    now
                                    + timedelta(seconds=self.config.response_timeout_seconds)
                                ).isoformat()
                                persist_transport_state()
                            else:
                                return "dom_fallback", "response_deadline"
                        return "waiting", None

                    if terminal_continuation_recovery and mode == "status_recovery":
                        wait["completion_mode"] = "status_recovery"
                        wait["backend_fallback_category"] = "graph_not_ready"
                        wait["stream_status_next_poll_at"] = (
                            now + timedelta(seconds=_STREAM_STATUS_RECOVERY_SECONDS)
                        ).isoformat()
                        if deadline_expired:
                            persist_transport_state()
                            return "dom_fallback", "response_deadline"
                    else:
                        wait["completion_mode"] = "terminal_graph_retry"
                        wait["terminal_complete_seen_at"] = now.isoformat()
                        wait["terminal_graph_attempts"] = 0
                        wait.pop("terminal_graph_request_id", None)
                        wait.pop("status_recovery_graph_next_at", None)
                        settle_seconds = (
                            0.0
                            if resume_recovery
                            else float(
                                self.config.response_stream_status_terminal_settle_seconds
                            )
                        )
                        wait["terminal_graph_ready_at"] = (
                            now + timedelta(seconds=settle_seconds)
                        ).isoformat()
                        persist_transport_state()
                        if settle_seconds > 0:
                            return "waiting", None
                        graph_mode = "terminal"

            if graph_mode is None and mode == "status_recovery":
                graph_ready_at = parse_time(wait.get("status_recovery_graph_next_at"))
                if graph_ready_at is None:
                    wait["status_recovery_graph_next_at"] = (
                        now + timedelta(seconds=_STATUS_RECOVERY_GRAPH_SECONDS)
                    ).isoformat()
                    return "waiting", None
                if now < graph_ready_at and not resume_recovery:
                    return "waiting", None
                wait["status_recovery_graph_next_at"] = (
                    now + timedelta(seconds=_STATUS_RECOVERY_GRAPH_SECONDS)
                ).isoformat()
                persist_transport_state()
                graph_mode = "recovery"
            elif graph_mode is None:
                return "waiting", None

        if graph_mode == "terminal":
            attempts = int(wait.get("terminal_graph_attempts") or 0) + 1
            wait["terminal_graph_attempts"] = attempts
            wait["terminal_graph_request_id"] = request_id
            wait["terminal_graph_attempted_at"] = now.isoformat()
            wait["terminal_graph_ready_at"] = (
                now + timedelta(seconds=_TERMINAL_GRAPH_RETRY_SECONDS)
            ).isoformat()
            persist_transport_state()

        try:
            graph = await actions.backend_conversation(receipt.conversation_id)
            if resume_recovery:
                resolved = resolve_terminal_assistant(
                    graph,
                    receipt.user_message_id,
                    proven_later_human_message_ids=(
                        self._proven_foreign_durable_user_message_ids(
                            state,
                            hop,
                            receipt.conversation_id,
                        )
                    ),
                    allow_manual_steering=True,
                    allow_detached_branch=True,
                )
            else:
                try:
                    resolved = resolve_terminal_assistant(graph, receipt.user_message_id)
                except GraphIdentityError:
                    proven_later_humans = self._proven_foreign_durable_user_message_ids(
                        state,
                        hop,
                        receipt.conversation_id,
                    )
                    if not proven_later_humans:
                        raise
                    resolved = resolve_terminal_assistant(
                        graph,
                        receipt.user_message_id,
                        proven_later_human_message_ids=proven_later_humans,
                    )
        except BackendError as exc:
            if resume_recovery and isinstance(exc, (GraphIdentityError, BackendSchemaError)):
                raise
            wait["backend_fallback_category"] = self._backend_failure_category(
                exc, prefix="graph"
            )
            if deadline_expired or resume_recovery:
                wait.pop("terminal_graph_request_id", None)
                category = (
                    str(wait["backend_fallback_category"])
                    if resume_recovery
                    else "response_deadline"
                )
                return "dom_fallback", category
            if graph_mode == "terminal":
                wait.pop("terminal_graph_request_id", None)
                attempts = int(wait.get("terminal_graph_attempts") or 0)
                if attempts >= _TERMINAL_GRAPH_MAX_ATTEMPTS:
                    return "dom_fallback", str(wait["backend_fallback_category"])
                wait["completion_mode"] = "terminal_graph_retry"
            else:
                wait["completion_mode"] = "status_recovery"
            persist_transport_state()
            return "waiting", None

        response = MessageSnapshot(
            role="assistant",
            message_id=resolved.message_id,
            turn_id=None,
            text=resolved.text,
            actions=(),
        )
        validation_error: str | None = None
        try:
            self._validate_response_candidate(state, hop, response)
            self._capture_remote_report_mirror(
                state,
                hop,
                graph,
                accepted_user_message_id=receipt.user_message_id,
                terminal_assistant_message_id=resolved.message_id,
                response_text=resolved.text,
            )
        except Exception as exc:
            validation_error = sanitize_exception(exc)
        self._record_response(
            state,
            hop,
            response,
            validation_error=validation_error,
        )
        return "responded", None

    async def _waiting(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        transport_baseline: dict[str, Any] | None = None,
    ) -> None:
        self._start_wait_budget_from_sent(hop)
        self._reconcile_hop_conversation_identity(state, hop)
        receipt = SendReceipt.from_dict(hop["receipt"])
        settings = (
            self.runtime_db.get_snapshot("settings")
            if self.runtime_db.path.exists()
            else None
        )
        dom_only = (
            isinstance(settings, Mapping)
            and isinstance(settings.get("payload"), Mapping)
            and settings["payload"].get("dom_only") is True
        )
        if dom_only or not receipt.conversation_id or not receipt.user_message_id:
            await self._waiting_dom(
                state,
                hop,
                actions,
                manifest_path,
                transport_baseline,
            )
            return

        await self._capture_bootstrap_role_donor(state, hop, actions)
        wait = hop["wait"]
        now = datetime.now(timezone.utc)
        backend_before = copy.deepcopy(state)
        persistence_baseline = (
            transport_baseline if transport_baseline is not None else backend_before
        )

        def persist_transport_state() -> None:
            nonlocal persistence_baseline
            saved = self._persist_transport_result(
                manifest_path, persistence_baseline, state
            )
            state["updated_at"] = saved["updated_at"]
            persistence_baseline = copy.deepcopy(state)

        repair_wait = state.get("repair_wait")
        unresolved = wait.get("terminal_continuation_unresolved")
        repair_release_rearm = (
            isinstance(repair_wait, dict)
            and repair_wait.get("state") == "RELEASED"
            and repair_wait.get("original_block_code")
            == "terminal_continuation_unresolved"
            and repair_wait.get("preserved_hop_id") == hop.get("hop_id")
            and repair_wait.get("preserved_request_id") == hop.get("request_id")
            and repair_wait.get("transport_rearmed_request_id")
            != hop.get("request_id")
            and str(wait.get("completion_mode") or "") == "dom_fallback"
            and wait.get("backend_fallback_category") == "graph_not_ready"
            and isinstance(unresolved, Mapping)
            and unresolved.get("request_id") == str(hop["request_id"])
        )
        if repair_release_rearm:
            recover_incomplete_refresh(wait)
            repair_wait["transport_rearmed_request_id"] = str(hop["request_id"])
            repair_wait["transport_rearmed_at"] = now.isoformat()
            wait["completion_mode"] = "status_recovery"
            wait["backend_fallback_category"] = "graph_not_ready"
            wait["stream_status_next_poll_at"] = now.isoformat()
            wait["status_recovery_graph_next_at"] = now.isoformat()
            wait["deadline_at"] = (
                now + timedelta(seconds=self.config.response_timeout_seconds)
            ).isoformat()
            for key in (
                "dom_fallback_ready_at",
                "terminal_complete_seen_at",
                "terminal_graph_ready_at",
                "terminal_graph_attempts",
                "terminal_graph_request_id",
                "terminal_graph_attempted_at",
            ):
                wait.pop(key, None)
            persist_transport_state()

        if str(wait.get("completion_mode") or "stream_status") == "dom_fallback":
            ready_at = parse_time(wait.get("dom_fallback_ready_at"))
            if ready_at is not None and now < ready_at:
                return
            await self._waiting_dom(
                state,
                hop,
                actions,
                manifest_path,
                transport_baseline,
            )
            return

        outcome, fallback_category = await self._waiting_backend_step(
            state,
            hop,
            actions,
            receipt,
            persist_transport_state=persist_transport_state,
        )
        if outcome == "dom_fallback":
            await self._begin_backend_dom_fallback(
                state,
                hop,
                actions,
                receipt,
                category=str(fallback_category or "backend_unavailable"),
                now=datetime.now(timezone.utc),
            )
            persist_transport_state()

    async def _waiting_dom(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        transport_baseline: dict[str, Any] | None = None,
    ) -> None:
        role = str(hop["target_role"])
        persistence_baseline = (
            transport_baseline
            if transport_baseline is not None
            else json.loads(json.dumps(state, ensure_ascii=False, default=str))
        )
        acquired = await self._owned_or_block(state, role, actions)
        if acquired is None:
            return
        wait = hop["wait"]
        recover_incomplete_refresh(wait)
        self._start_wait_budget_from_sent(hop)
        self._reconcile_hop_conversation_identity(state, hop)
        receipt = SendReceipt.from_dict(hop["receipt"])
        snapshot, recovered = await self._waiting_dom_snapshot(
            state,
            hop,
            actions,
            acquired,
            manifest_path,
            persistence_baseline,
            receipt,
            transport_baseline=transport_baseline,
            probe_wait_ms=12_000,
        )
        if recovered is not None:
            acquired = recovered
        if snapshot is None:
            hop = _active_hop(state)
            wait = hop["wait"]
            if recovered is not None and remaining_timeout_ms(wait) <= 0:
                reconciled = await self._final_dom_response_reconciliation(
                    state, hop, acquired, receipt, wait
                )
                if reconciled is None or reconciled:
                    return
                self._block(
                    state,
                    "response timeout budget exhausted after final response reconciliation",
                    code="response_timeout",
                    retryable=False,
                )
            return
        receipt = self._upgrade_legacy_receipt(
            state,
            hop,
            receipt,
            snapshot,
            manifest_path,
            persistence_baseline,
        )
        if receipt is None:
            return
        signature, length = response_activity_signature(snapshot, receipt.baseline)
        observe_response_activity(wait, signature=signature, length=length)
        wait["transport_ui_active"] = response_transport_ui_active(snapshot)
        wait["last_stop_visible"] = bool(snapshot.stop_visible)
        observe_responding(
            wait,
            stop_visible=snapshot.stop_visible,
            composer_empty=snapshot.composer_empty,
            manual_input_pending=snapshot.manual_input_pending,
        )
        refresh_count = int(wait.get("refresh_count") or 0)
        last_refresh_result = wait.get("last_refresh_result")
        refresh_finished_at = (
            parse_time(last_refresh_result.get("finished_at"))
            if isinstance(last_refresh_result, Mapping)
            and last_refresh_result.get("status") == "completed"
            else None
        )
        if refresh_count > 0 and refresh_finished_at is not None:
            refresh_age = (datetime.now(timezone.utc) - refresh_finished_at).total_seconds()
            final_checked = int(wait.get("stall_final_refresh_count") or 0)
            probe_checked = int(wait.get("stall_probe_refresh_count") or 0)
            if refresh_count > final_checked and refresh_age >= _POST_REFRESH_REROUTE_SECONDS:
                reconciled = await self._final_dom_response_reconciliation(
                    state, hop, acquired, receipt, wait
                )
                if reconciled is None or reconciled:
                    return
                wait["stall_probe_refresh_count"] = refresh_count
                wait["stall_final_refresh_count"] = refresh_count
                latest, recovered = await self._waiting_dom_snapshot(
                    state,
                    hop,
                    actions,
                    acquired,
                    manifest_path,
                    persistence_baseline,
                    receipt,
                    transport_baseline=transport_baseline,
                    force_full=True,
                )
                if recovered is not None:
                    acquired = recovered
                if latest is None:
                    return
                if latest.stop_visible:
                    hop["state"] = "waiting"
                    state["active_action"] = "wait_response"
                    return
                attempt = int(hop.get("stall_reroute_attempt") or 0)
                if attempt >= _STALL_REROUTE_MAX_ATTEMPTS:
                    self._block(
                        state,
                        "stalled response reroute exhausted after three attempts",
                        code="stall_reroute_exhausted",
                        retryable=False,
                    )
                    return
                RequestLedger(str(hop["ledger_path"])).update(
                    str(hop["request_id"]),
                    status=RequestStatus.FAILED_FINAL,
                    error="stalled response rerouted after refresh",
                )
                hop["state"] = "abandoned"
                hop["abandon_reason"] = "stalled response rerouted after refresh"
                hop.setdefault("timestamps", {})["abandoned_at"] = utc_now()
                child = self._append_hop(
                    state,
                    source_role=str(hop["target_role"]),
                    target_role=str(hop["target_role"]),
                    handoff=str(hop["handoff"]),
                    kind="stall_reroute",
                    turn=int(hop["turn"]),
                )
                child["stall_reroute_attempt"] = attempt + 1
                return
            if (
                refresh_count > probe_checked
                and refresh_age >= _POST_REFRESH_RESPONSE_PROBE_SECONDS
            ):
                reconciled = await self._final_dom_response_reconciliation(
                    state, hop, acquired, receipt, wait
                )
                if reconciled is None or reconciled:
                    return
                wait["stall_probe_refresh_count"] = refresh_count
        unresolved = wait.get("terminal_continuation_unresolved")
        unresolved_complete = (
            isinstance(unresolved, dict)
            and wait.get("backend_fallback_category") == "graph_not_ready"
            and unresolved.get("request_id") == str(hop["request_id"])
        )
        unresolved_refresh_used = bool(
            unresolved_complete
            and int(wait.get("refresh_count") or 0)
            > int(unresolved.get("refresh_baseline") or 0)
        )
        unresolved_block_ready_at = (
            parse_time(unresolved.get("block_ready_at"))
            if unresolved_complete
            else None
        )
        if (
            unresolved_refresh_used
            and unresolved_block_ready_at is not None
            and datetime.now(timezone.utc) >= unresolved_block_ready_at
        ):
            reconciled = await self._final_dom_response_reconciliation(
                state, hop, acquired, receipt, wait
            )
            if reconciled is None or reconciled:
                return
            if remaining_timeout_ms(wait) <= 0:
                self._block(
                    state,
                    "response timeout budget exhausted after final response reconciliation",
                    code="response_timeout",
                    retryable=False,
                )
                return
            now = datetime.now(timezone.utc)
            wait["completion_mode"] = "status_recovery"
            wait["backend_fallback_category"] = "graph_not_ready"
            wait["stream_status_next_poll_at"] = (
                now + timedelta(seconds=_STREAM_STATUS_RECOVERY_SECONDS)
            ).isoformat()
            wait["status_recovery_graph_next_at"] = (
                now + timedelta(seconds=_STATUS_RECOVERY_GRAPH_SECONDS)
            ).isoformat()
            wait.pop("dom_fallback_ready_at", None)
            for key in (
                "terminal_complete_seen_at",
                "terminal_graph_ready_at",
                "terminal_graph_attempts",
                "terminal_graph_request_id",
                "terminal_graph_attempted_at",
            ):
                wait.pop(key, None)
            hop["state"] = "waiting"
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(role)
            state["active_action"] = "wait_response"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            return
        remaining = remaining_timeout_ms(wait)
        refreshed_this_cycle = False
        should_refresh = refresh_due(
            wait,
            refresh_after_seconds=self.config.response_refresh_after_seconds,
            composer_empty=snapshot.composer_empty,
            manual_input_pending=snapshot.manual_input_pending,
        )
        if unresolved_complete:
            should_refresh = bool(
                not unresolved_refresh_used
                and snapshot.composer_empty
                and not snapshot.manual_input_pending
            )
        if remaining <= 0 or should_refresh:
            reconciled = await self._final_dom_response_reconciliation(
                state, hop, acquired, receipt, wait
            )
            if reconciled is None or reconciled:
                return
            snapshot, recovered = await self._waiting_dom_snapshot(
                state,
                hop,
                actions,
                acquired,
                manifest_path,
                persistence_baseline,
                receipt,
                transport_baseline=transport_baseline,
                force_full=True,
            )
            if recovered is not None:
                acquired = recovered
            if snapshot is None:
                return
            signature, length = response_activity_signature(snapshot, receipt.baseline)
            observe_response_activity(wait, signature=signature, length=length)
            wait["transport_ui_active"] = response_transport_ui_active(snapshot)
            wait["last_stop_visible"] = bool(snapshot.stop_visible)
            observe_responding(
                wait,
                stop_visible=snapshot.stop_visible,
                composer_empty=snapshot.composer_empty,
                manual_input_pending=snapshot.manual_input_pending,
            )
            remaining = remaining_timeout_ms(wait)
            should_refresh = refresh_due(
                wait,
                refresh_after_seconds=self.config.response_refresh_after_seconds,
                composer_empty=snapshot.composer_empty,
                manual_input_pending=snapshot.manual_input_pending,
            )
            if unresolved_complete:
                should_refresh = bool(
                    not unresolved_refresh_used
                    and snapshot.composer_empty
                    and not snapshot.manual_input_pending
                )
            if remaining <= 0:
                self._block(
                    state,
                    "response timeout budget exhausted after final response reconciliation",
                    code="response_timeout",
                    retryable=False,
                )
                return
        if should_refresh:
            refresh_baseline = json.loads(
                json.dumps(persistence_baseline, ensure_ascii=False, default=str)
            )
            wait["recovery_baseline"] = merge_response_recovery_baselines(
                wait.get("recovery_baseline"),
                capture_response_recovery_baseline(
                    snapshot.messages,
                    receipt.baseline,
                ),
            )
            if unresolved_complete:
                unresolved["block_ready_at"] = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=_DOM_FALLBACK_SETTLE_SECONDS)
                ).isoformat()
            begin_refresh(wait)
            self._persist_transport_result(manifest_path, refresh_baseline, state)
            refresh_baseline = json.loads(
                json.dumps(state, ensure_ascii=False, default=str)
            )
            try:
                acquired = await actions.refresh(
                    acquired,
                    manifest=state,
                    logical_role=role,
                    recover=True,
                )
            except RoleOwnershipError as exc:
                finish_refresh(wait, error=sanitize_exception(exc))
                self._persist_transport_result(manifest_path, refresh_baseline, state)
                raise
            except Exception as exc:
                finish_refresh(wait, error=sanitize_exception(exc))
                saved = self._persist_transport_result(
                    manifest_path, refresh_baseline, state
                )
                if not is_transient_page_lifecycle_error(exc):
                    raise
                if transport_baseline is not None:
                    state.clear()
                    state.update(saved)
                    hop = _active_hop(state)
                    wait = hop["wait"]
                    transport_baseline.clear()
                    transport_baseline.update(
                        json.loads(json.dumps(saved, ensure_ascii=False, default=str))
                    )
                hop["state"] = "waiting"
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(role)
                state["active_action"] = "wait_response"
                return
            finish_refresh(wait)
            refreshed_this_cycle = True
            saved = self._persist_transport_result(manifest_path, refresh_baseline, state)
            if transport_baseline is not None:
                state.clear()
                state.update(saved)
                hop = _active_hop(state)
                wait = hop["wait"]
                transport_baseline.clear()
                transport_baseline.update(
                    json.loads(json.dumps(saved, ensure_ascii=False, default=str))
                )
        remaining = remaining_timeout_ms(wait)
        if remaining <= 0:
            reconciled = await self._final_dom_response_reconciliation(
                state, hop, acquired, receipt, wait
            )
            if reconciled is None or reconciled:
                return
            self._block(
                state,
                "response timeout budget exhausted after final response reconciliation",
                code="response_timeout",
                retryable=False,
            )
            return
        if int(wait.get("refresh_count") or 0) > 0 and not receipt.accepted_via.startswith("post_reload:"):
            receipt = replace(receipt, accepted_via=f"post_reload:{receipt.accepted_via}")
        if not refreshed_this_cycle and (
            snapshot.stop_visible
            or bool(getattr(snapshot, "response_activity_turn_id", None))
        ):
            hop["state"] = "waiting"
            state["active_action"] = "wait_response"
            return

        def validate_candidate(response: MessageSnapshot) -> None:
            self._validate_response_candidate(state, hop, response)

        try:
            response = await acquired.client.wait_for_response(
                receipt,
                timeout_ms=min(
                    max(
                        3_000,
                        self.config.response_stable_ms
                        + (2 * self.config.response_poll_ms),
                    ),
                    remaining,
                ),
                stable_ms=self.config.response_stable_ms,
                poll_ms=self.config.response_poll_ms,
                active_reload_after_ms=None,
                stale_response_baseline=wait.get("recovery_baseline"),
                candidate_validator=validate_candidate,
                minimum_samples=2,
                invalid_grace_ms=max(1_000, self.config.response_stable_ms),
            )
        except StableMalformedResponseError as exc:
            wait["recovery_baseline"] = None
            await self._flush_hop_conversation_identity(state, hop)
            self._record_response(
                state,
                hop,
                exc.candidate,
                validation_error=str(exc.validation_error),
            )
            return
        except (TimeoutError, IncompleteResponseTimeoutError):
            if remaining_timeout_ms(wait) <= 0:
                reconciled = await self._final_dom_response_reconciliation(
                    state, hop, acquired, receipt, wait
                )
                if reconciled is None or reconciled:
                    return
                self._block(
                    state,
                    "response timeout budget exhausted after final response reconciliation",
                    code="response_timeout",
                    retryable=False,
                )
            else:
                hop["state"] = "waiting"
                state["active_action"] = "wait_response"
            return
        except ManualInputPendingError as exc:
            state["status"] = "PAUSED"
            state["kanban_column"] = "PAUSED"
            state["pause_reason"] = sanitize_text(exc, max_chars=2000)
            return
        except ChoicePromptBlockedError as exc:
            self._block(
                state,
                exc,
                code="choice_prompt_blocked",
                retryable=False,
            )
            return
        wait["recovery_baseline"] = None
        await self._flush_hop_conversation_identity(state, hop)
        self._record_response(state, hop, response)

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
        self._append_hop(
            state,
            source_role=role,
            target_role=role,
            handoff="route repair",
            kind="route_repair",
            turn=int(hop["turn"]),
            repair_attempt=attempt,
            validation_error=validation_error,
        )

    @staticmethod
    def _self_route_reset_hop_id(state: Mapping[str, Any]) -> int:
        boundary = 1
        for revision in state.get("goal_revisions") or ():
            if not isinstance(revision, Mapping):
                continue
            applies_from = revision.get("applies_from_hop_id")
            if isinstance(applies_from, int) and not isinstance(applies_from, bool):
                boundary = max(boundary, applies_from)
        for control in state.get("controls") or ():
            if not isinstance(control, Mapping) or control.get("action") != "resume":
                continue
            origin = str(control.get("origin") or "operator").strip().lower()
            if origin != "operator":
                continue
            result = control.get("result")
            if not isinstance(result, Mapping):
                continue
            before = result.get("before")
            after = result.get("after")
            outcome = str(result.get("outcome") or "").strip().lower()
            status = str(control.get("status") or "").strip().lower()
            if (
                isinstance(before, Mapping)
                and str(before.get("status") or "").upper() == "PAUSED"
                and status == "applied"
                and outcome in {"continued", "applied"}
            ):
                hop_id = before.get("active_hop_id")
                if isinstance(hop_id, int) and not isinstance(hop_id, bool):
                    boundary = max(boundary, hop_id)
            if (
                isinstance(before, Mapping)
                and str(before.get("block_code") or "")
                == _CONSECUTIVE_SELF_ROUTE_BLOCK_CODE
                and status == "applied"
                and outcome == "continued"
                and isinstance(after, Mapping)
            ):
                hop_id = after.get("active_hop_id")
                if isinstance(hop_id, int) and not isinstance(hop_id, bool):
                    boundary = max(boundary, hop_id)
        return boundary

    @classmethod
    def _consecutive_self_route_streak(
        cls, state: Mapping[str, Any]
    ) -> tuple[str | None, int]:
        boundary = cls._self_route_reset_hop_id(state)
        hops = {
            hop.get("hop_id"): hop
            for hop in state.get("hops") or ()
            if isinstance(hop, Mapping)
        }
        streak_role: str | None = None
        streak = 0
        for event in state.get("route_timeline") or ():
            if not isinstance(event, Mapping) or event.get("kind"):
                continue
            hop_id = event.get("hop_id")
            if (
                isinstance(hop_id, bool)
                or not isinstance(hop_id, int)
                or hop_id < boundary
            ):
                continue
            source_hop = hops.get(hop_id)
            if not isinstance(source_hop, Mapping) or str(source_hop.get("kind") or "") not in {
                "task",
                "handoff",
            }:
                continue
            source_role = str(event.get("source_role") or "").strip().upper()
            route = str(event.get("route") or "").strip().upper()
            if not source_role or not route:
                continue
            if route == "DONE":
                return None, 0
            if source_role != route:
                streak_role = None
                streak = 0
                continue
            if streak_role == source_role:
                streak += 1
            else:
                streak_role = source_role
                streak = 1
        return streak_role, streak

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
        response_mode = _report_mode(state)
        legacy_inline = response_mode == "inline"
        decision = None
        try:
            parsed = parse_role_response(
                str(hop.get("response") or ""),
                source_role=role,
                report_mode=response_mode,
                allowed_routes=tuple(task_workflow_definitions(state, self.config))
                + ("PAUSE", "DONE"),
            )
            decision = parsed.decision
            if decision.route not in {"PAUSE", "DONE"} and decision.route not in state["roles"]:
                raise RouteContractError(
                    f"route {decision.route!r} is not selected for this task"
                )
            if parsed.inline_report is None:
                routed_handoff = decision.handoff
                if remote_repository_from_task(str(state.get("task_text") or "")) is None:
                    report_path = decision.handoff
                    report_sha256 = None
                    report_size = None
                else:
                    report_path, report_sha256, report_size = self._validated_remote_report_mirror(
                        state,
                        hop,
                        decision_handoff=decision.handoff,
                    )
            else:
                report_repository, report_plans_root = _workflow_report_roots(
                    self.config, state
                )
                try:
                    evidence = materialize_inline_report(
                        parsed.inline_report,
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
        streak_role, streak = self._consecutive_self_route_streak(state)
        if (
            str(decision.route).strip().upper() == role.strip().upper()
            and streak_role == role.strip().upper()
            and streak >= _CONSECUTIVE_SELF_ROUTE_LIMIT
        ):
            self._block(
                state,
                (
                    f"Consecutive self-route limit reached for role {streak_role}: "
                    f"streak {streak}; explicit operator Resume is required to continue."
                ),
                code=_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE,
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
            if self._rate_limit_gate_active():
                state["active_action"] = "rate_limit_cooldown"
                return None
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

    async def _recover_resume_waiting(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        receipt_value = hop.get("receipt")
        if not isinstance(receipt_value, Mapping):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="accepted_send_provenance_missing",
                reason="Resume cannot verify an accepted request because its receipt is missing.",
                next_safe_action="Restore the durable receipt before resuming this hop.",
            )
            return
        self._start_wait_budget_from_sent(hop)
        self._reconcile_hop_conversation_identity(state, hop)
        receipt = SendReceipt.from_dict(hop["receipt"])
        settings = (
            self.runtime_db.get_snapshot("settings")
            if self.runtime_db.path.exists()
            else None
        )
        dom_only = (
            isinstance(settings, Mapping)
            and isinstance(settings.get("payload"), Mapping)
            and settings["payload"].get("dom_only") is True
        )
        role = str(hop.get("target_role") or "").upper()
        recorded_recovery_url = (
            hop.get("conversation_url")
            or state.get("roles", {}).get(role, {}).get("page_url")
        )
        if (
            not dom_only
            and receipt.conversation_id is None
            and conversation_identity(recorded_recovery_url) is None
            and receipt.attempts > 0
            and receipt.user_message_id
        ):
            try:
                receipt = await self._discover_accepted_conversation_identity(
                    state, hop, actions, receipt
                )
            except (BackendError, DurableRequestError, KeyError, ValueError) as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="accepted_conversation_identity_unresolved",
                    reason=(
                        "Accepted request identity is unresolved after bounded backend "
                        f"reconciliation: {sanitize_exception(exc)}"
                    ),
                    next_safe_action=(
                        "Use Route PLAN only if durable side effects are already reconciled; "
                        "do not Resume, Restart Role, New Chat, retry, or resend this accepted request."
                    ),
                )
                return
        if not dom_only and receipt.conversation_id and receipt.user_message_id:
            try:
                backend_outcome, _fallback_category = await self._waiting_backend_step(
                    state,
                    hop,
                    actions,
                    receipt,
                    persist_transport_state=lambda: None,
                    resume_recovery=True,
                )
            except (GraphIdentityError, BackendSchemaError) as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="backend_evidence_ambiguous",
                    reason=sanitize_exception(exc),
                    next_safe_action=(
                        "Inspect the exact durable backend identity/evidence; do not reopen, "
                        "rebind, retry, or resend this accepted request."
                    ),
                )
                return
            if backend_outcome == "responded":
                old_hop_id = state.get("active_hop_id")
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="consume_response",
                    reason_code=None,
                    reason="The exact backend terminal response was consumed without source-tab recovery.",
                    postcondition=None,
                )
                self._responded(state, hop)
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="consume_response",
                    reason_code=None,
                    reason="The exact backend terminal response was consumed without source-tab recovery.",
                    postcondition=(
                        "hop_advanced"
                        if state.get("active_hop_id") != old_hop_id
                        else "response_consumed"
                    ),
                )
                return
            if backend_outcome == "waiting":
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(str(hop["target_role"]))
                state["active_action"] = "wait_response"
                state["block_code"] = None
                state["block_retryable"] = False
                state["block_reason"] = None
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="rearm_backend_wait",
                    reason_code=None,
                    reason="The exact accepted request remains active in backend state.",
                    postcondition="backend_wait_rearmed",
                )
                return

        try:
            acquired_result = await self._resume_exact_owned_role(
                state,
                hop,
                actions,
                expected_page_id=receipt.binding.page_id,
            )
            if acquired_result is None:
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
            acquired, reopened = acquired_result
        except (RoleOwnershipError, PageOwnershipError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="reopen_exact_tab",
                reason_code="role_offline",
                reason=sanitize_exception(exc),
                next_safe_action="Open or rebind the exact recorded role tab, then Resume again.",
            )
            return
        snapshot = await acquired.client.assert_ownership()
        if tuple(getattr(snapshot, "blocking_dialogs", ()) or ()):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="blocking_dialog",
                reason="Resume found a blocking dialog on the exact accepted request tab.",
                next_safe_action="Resolve the visible dialog, then Resume again.",
            )
            return
        if not receipt_user_message_seen(snapshot.messages, receipt):
            self._require_resume_recovery(
                state,
                control,
                action="reopen_exact_tab" if reopened else "none",
                reason_code="accepted_user_provenance_ambiguous",
                reason="The exact accepted user turn is not present on the owned conversation.",
                next_safe_action="Restore the exact recorded conversation; do not resend the prompt.",
            )
            return

        response: MessageSnapshot | None = None
        response_validation_error: str | None = None
        try:
            response = await acquired.client.wait_for_response(
                receipt,
                timeout_ms=max(
                    1_000,
                    self.config.response_stable_ms
                    + (2 * self.config.response_poll_ms),
                ),
                stable_ms=self.config.response_stable_ms,
                poll_ms=self.config.response_poll_ms,
                active_reload_after_ms=None,
                resolve_choice_prompt=False,
                candidate_validator=lambda candidate: self._validate_response_candidate(
                    state, hop, candidate
                ),
                minimum_samples=2,
                invalid_grace_ms=max(1_000, self.config.response_stable_ms),
            )
        except StableMalformedResponseError as exc:
            response = exc.candidate
            response_validation_error = str(exc.validation_error)
        except (TimeoutError, IncompleteResponseTimeoutError):
            response = None
        if response is not None:
            old_hop_id = state.get("active_hop_id")
            await self._flush_hop_conversation_identity(state, hop)
            self._record_response(
                state,
                hop,
                response,
                validation_error=response_validation_error,
            )
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="consume_response",
                reason_code=None,
                reason="A stable existing assistant response was consumed without another send.",
                postcondition=None,
            )
            self._responded(state, hop)
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="consume_response",
                reason_code=None,
                reason="A stable existing assistant response was consumed without another send.",
                postcondition=(
                    "hop_advanced"
                    if state.get("active_hop_id") != old_hop_id
                    else "response_consumed"
                ),
            )
            return

        signature, length = response_activity_signature(snapshot, receipt.baseline)
        if bool(getattr(snapshot, "stop_visible", False)) or (
            length > 0 and response_transport_ui_active(snapshot)
        ):
            wait = hop.setdefault("wait", {})
            wait["activity_signature"] = signature
            wait["activity_length"] = length
            wait["activity_observed_at"] = utc_now()
            state["status"] = "RUNNING"
            state["kanban_column"] = _column_for(str(hop["target_role"]))
            state["active_action"] = "wait_response"
            self._finish_resume_control(
                state,
                control,
                outcome="continued",
                action="observe_progress",
                reason_code=None,
                reason="The accepted request has verified generation progress.",
                postcondition="generation_progress",
            )
            return

        self._require_resume_recovery(
            state,
            control,
            action="reopen_exact_tab" if reopened else "none",
            reason_code="resume_progress_unverified",
            reason="Resume found no stable response or verified generation progress on the exact fallback conversation.",
            next_safe_action="Inspect the exact accepted conversation; do not retry generation or resend the request.",
        )

    def _is_pristine_preboundary_sending_record(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        record: Any,
    ) -> bool:
        if is_independent_task(state):
            return False
        if str(hop.get("state") or "") != "sending":
            return False
        if record is None or record.status is not RequestStatus.NEW:
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
        acquired = await actions.locate_owned(state, role)
        action = "confirm_preboundary_role"
        postcondition = "ownership_confirmed_before_send"
        if acquired is None:
            exact_donor = role_record.get("context_source") == "bootstrap_donor"
            if exact_donor:
                try:
                    donor = normalize_bootstrap_donor(role_record.get("bootstrap_source_donor"))
                except Exception:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="preboundary_context_unrecoverable",
                        reason="The recorded bootstrap donor for the lost pre-boundary role is invalid.",
                        next_safe_action="Restore the exact recorded bootstrap donor before Resume.",
                    )
                    return
            else:
                if (
                    role_record.get("context_source") not in {None, ""}
                    or role_record.get("bootstrap_source_donor") is not None
                    or generation != 0
                ):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="preboundary_context_unrecoverable",
                        reason="The lost pristine pre-boundary role is not the legacy donorless generation-zero case.",
                        next_safe_action="Restore the exact recorded pre-send context before Resume.",
                    )
                    return
                bootstrap = self._bootstrap_for_state(state)
                if bootstrap is None and not isinstance(state.get("bootstrap"), Mapping):
                    current_default = BootstrapCatalog(self.config.repository_root).get(
                        "general-team-bootstrap"
                    )
                    bootstrap = (
                        current_default
                        if current_default is not None
                        and current_default.get("enabled") is True
                        else None
                    )
                candidates = (
                    self._bootstrap_donor_candidates(state, bootstrap)
                    if bootstrap is not None
                    else []
                )
                if not candidates:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="preboundary_context_unrecoverable",
                        reason="The lost pristine pre-boundary role has no current bootstrap donor available.",
                        next_safe_action="Restore an enabled bootstrap donor before Resume.",
                    )
                    return
                donor = candidates[0]
                try:
                    graph = await actions.backend_conversation(donor["conversation_id"])
                    resolve_bootstrap_donor(graph, donor["assistant_message_id"])
                except Exception as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="reacquire_preboundary_role",
                        reason_code="preboundary_context_unrecoverable",
                        reason=sanitize_exception(exc),
                        next_safe_action="Restore the current bootstrap donor before Resume.",
                    )
                    return
            use_ui_branch = not exact_donor
            if self._rate_limit_gate_active():
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
            if exact_donor:
                try:
                    acquired = await actions.branch_from_anchor(
                        state,
                        role,
                        source_conversation_id=donor["conversation_id"],
                        assistant_message_id=donor["assistant_message_id"],
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
                except BranchTargetUnresolvedError as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="reacquire_preboundary_role",
                        reason_code="branch_target_unresolved",
                        reason=sanitize_exception(exc),
                        next_safe_action=(
                            "Inspect the provisional bootstrap branch target; "
                            "do not fan out or retry this Resume automatically."
                        ),
                    )
                    return
                except BranchBootstrapError:
                    use_ui_branch = True
            if use_ui_branch:
                try:
                    acquired = await self._branch_from_bootstrap_ui(
                        state, role, actions, donor
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
                except BootstrapUIBranchError as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="reacquire_preboundary_role",
                        reason_code="preboundary_context_unrecoverable",
                        reason=sanitize_exception(exc),
                        next_safe_action=(
                            "Restore the exact recorded bootstrap donor before Resume."
                            if exact_donor
                            else "Restore the current bootstrap donor before Resume."
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
        try:
            if role_record.get("context_source") != "bootstrap_donor":
                raise ValueError("missing exact bootstrap donor")
            donor = normalize_bootstrap_donor(role_record.get("bootstrap_source_donor"))
        except Exception as exc:
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="sending_replacement_context_unrecoverable",
                reason=sanitize_exception(exc),
                next_safe_action="Restore the exact recorded donor; do not open another replacement page.",
            )
            return
        generation = int(role_record.get("conversation_generation") or 0)
        if self._rate_limit_gate_active():
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
        try:
            acquired = await actions.branch_from_anchor(
                state,
                role,
                source_conversation_id=donor["conversation_id"],
                assistant_message_id=donor["assistant_message_id"],
            )
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
                next_safe_action="Preserve the composer; do not open another donor tab.",
            )
            return
        except (BranchBootstrapError, BranchTargetUnresolvedError) as exc:
            self._require_resume_recovery(
                state,
                control,
                action="reacquire_sending_role",
                reason_code="sending_replacement_context_unrecoverable",
                reason=sanitize_exception(exc),
                next_safe_action="Restore the exact donor; do not fan out to another replacement page.",
            )
            return
        snapshot = await acquired.client.assert_ownership()
        binding = acquired.client.binding
        if binding is None:
            raise RuntimeError("bounded SENDING replacement has no page binding")
        baseline = capture_message_baseline(snapshot.messages)
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
    ) -> None:
        ledger = RequestLedger(str(hop.get("ledger_path") or ""))
        ledger_exists = ledger.path.exists()
        record = ledger.get(str(hop.get("request_id") or "")) if ledger_exists else None
        if record is None and ledger_exists:
            await self._recover_pristine_preboundary_sending(
                state, hop, control, actions
            )
            return
        if await self._recover_failed_upload_before_ready(
            state, hop, control, actions, ledger, record
        ):
            return
        if self._is_pristine_preboundary_sending_record(state, hop, record):
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
        role_record = state["roles"][role]
        role_url = role_record.get("page_url")
        backend_ids = {
            identity
            for value in (hop.get("conversation_url"), role_url)
            if (identity := _recoverable_conversation_identity(value)) is not None
        }
        backend_conversation = getattr(actions, "backend_conversation", None)
        if (
            session_id
            and not provisional_session
            and backend_ids == {f"/c/{session_id}"}
            and callable(backend_conversation)
        ):
            empty_baseline = not (
                record.baseline.message_ids
                or record.baseline.turn_ids
                or record.baseline.assistant_turn_ids
                or record.baseline.user_message_ids
            )
            if empty_baseline and str(role_record.get("page_id") or "") != str(
                record.binding.page_id
            ):
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="sending_provenance_ambiguous",
                    reason=(
                        "The damaged empty-baseline SENDING record no longer retains "
                        "the exact durable page binding."
                    ),
                    next_safe_action="Restore exact durable binding provenance; do not resend.",
                )
                return
            try:
                graph = await backend_conversation(session_id)
                if empty_baseline:
                    accepted_id = resolve_unique_exact_user_message(
                        graph,
                        record.rendered_prompt,
                    )
                else:
                    accepted_id = resolve_exact_new_user_message(
                        graph,
                        record.rendered_prompt,
                        excluded_message_ids=(
                            frozenset(record.baseline.message_ids)
                            | frozenset(record.baseline.user_message_ids)
                        ),
                    )
            except GraphIdentityError as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="none",
                    reason_code="sending_provenance_ambiguous",
                    reason=sanitize_exception(exc),
                    next_safe_action="Resolve the conflicting backend user turn; do not resend.",
                )
                return
            except BackendError as exc:
                if empty_baseline:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason=sanitize_exception(exc),
                        next_safe_action="Restore unique exact backend prompt provenance; do not resend.",
                    )
                    return
                pass
            else:
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
                        conversation_id=session_id,
                    ),
                    "Backend transcript provenance proves SENDING acceptance; no Send was replayed.",
                )
                return

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
                if len(live_backend_ids) != 1 or not callable(backend_conversation):
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason=(
                            "The exact provisional SENDING page has no unique canonical backend identity "
                            "with positive acceptance provenance."
                        ),
                        next_safe_action="Preserve the exact page/request and restore unique backend provenance; do not resend.",
                    )
                    return
                canonical_identity = next(iter(live_backend_ids))
                canonical_conversation_id = canonical_identity.rsplit("/", 1)[-1]
                try:
                    graph = await backend_conversation(canonical_conversation_id)
                    excluded_message_ids = (
                        frozenset(record.baseline.message_ids)
                        | frozenset(record.baseline.user_message_ids)
                    )
                    if not excluded_message_ids:
                        source_donor = state["roles"][role].get("bootstrap_source_donor")
                        if source_donor is not None:
                            donor = normalize_bootstrap_donor(source_donor)
                            resolve_bootstrap_donor(graph, donor["assistant_message_id"])
                            excluded_message_ids = frozenset({donor["assistant_message_id"]})
                    accepted_id = resolve_exact_new_user_message(
                        graph,
                        record.rendered_prompt,
                        excluded_message_ids=excluded_message_ids,
                    )
                except (BackendError, ValueError) as exc:
                    self._require_resume_recovery(
                        state,
                        control,
                        action="none",
                        reason_code="sending_provenance_ambiguous",
                        reason=sanitize_exception(exc),
                        next_safe_action="Preserve the exact canonicalized page and resolve backend acceptance provenance; do not resend.",
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
                    "Same-page canonical backend provenance proves provisional SENDING acceptance; no Send was replayed.",
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
                    state, hop, control, actions, ledger, record
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
        if accepted_user is not None and visible_text_matches(
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
        if capture_message_baseline(snapshot.messages) != record.baseline:
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
    ) -> None:
        hop = _active_hop(state)
        hop_state = str(hop.get("state") or "")
        try:
            if hop_state == "pre_send":
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
                await self._recover_resume_sending(state, hop, control, actions)
                return
            if hop_state == "sent":
                self._start_wait_budget_from_sent(hop)
                hop["state"] = "waiting"
                hop.setdefault("timestamps", {})["waiting_at"] = utc_now()
                state["active_action"] = "wait_response"
                await self._recover_resume_waiting(state, hop, control, actions)
                return
            if hop_state == "waiting":
                await self._recover_resume_waiting(state, hop, control, actions)
                return
            if hop_state == "responded":
                old_hop_id = state.get("active_hop_id")
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="consume_response",
                    reason_code=None,
                    reason="The persisted assistant response was consumed.",
                    postcondition=None,
                )
                self._responded(state, hop)
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="consume_response",
                    reason_code=None,
                    reason="The persisted assistant response was consumed.",
                    postcondition=(
                        "hop_advanced"
                        if state.get("active_hop_id") != old_hop_id
                        else "response_consumed"
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
    ) -> dict[str, Any] | None:
        path = Path(manifest_path).resolve()
        try:
            with self.store.task_run_lock(path, blocking=False):
                try:
                    loaded_state = self._load_manifest_cached(path)
                    state, scheduling_changed = self.store.refresh_scheduling(
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
                    return saved
                requested_control = any(
                    isinstance(item, Mapping) and item.get("status") == "requested"
                    for item in state.get("controls") or []
                )
                if scheduling_changed and not requested_control:
                    return state
                if state.get("status") == "WAITING" and not requested_control:
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
                actions = CDPATabActions(browser_context, self.config)
                if state.get("cleanup", {}).get("state") == "CLEARING":
                    await self._continue_cleanup(state, actions, path)
                    saved = self.store.load(path)
                    self._sync_control_commands(saved)
                    return saved
                pending_control = next(
                    (
                        item for item in state.get("controls") or []
                        if isinstance(item, dict) and item.get("status") == "requested"
                    ),
                    None,
                )
                pending_control_id = (
                    pending_control.get("control_id") if pending_control is not None else None
                )
                pending_action = (
                    str(pending_control.get("action") or "")
                    if pending_control is not None
                    else ""
                )
                resumed_control = None
                resume_baseline: dict[str, Any] | None = None
                persisted_control: list[bool] = []
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
                    await self._recover_resume_control(state, resumed_control, actions)
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
                        await self._pre_send(state, hop, actions)
                    elif hop["state"] == "sending":
                        await self._sending(state, hop, actions)
                    elif hop["state"] == "sent":
                        self._start_wait_budget_from_sent(hop)
                        hop["state"] = "waiting"
                        hop["timestamps"]["waiting_at"] = utc_now()
                        state["active_action"] = "wait_response"
                    elif hop["state"] == "waiting":
                        await self._waiting(
                            state,
                            hop,
                            actions,
                            path,
                            transport_baseline,
                        )
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
                if completed_hop_id is not None:
                    self._schedule_repository_project(saved, completed_hop_id, browser_context)
                return saved
        except BlockingIOError:
            return None
        except Exception as exc:
            if is_cdp_disconnect(exc):
                raise
            try:
                state = self.store.load(path)
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
                return saved
            except Exception:
                raise

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
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
        }:
            raise ValueError("unsupported independent task control")
        tasks = self.store.discover()
        current_event_keys = {
            str(event.get("event_key") or "")
            for event in canonical_independent_events(source, tasks)
        }
        event_key = str(active.get("event_key") or "")
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
        return recorded, queued

    def _ensure_builtin_independent_agents(self) -> None:
        if not self.config.independent_seed_builtins:
            return
        self.store.seed_independent_agent(
            "Maintainers",
            system_prompt=BUILTIN_MAINTAINERS_PROMPT,
            trigger_settings={"recovery": True},
            max_cycles=0,
        )
        self.store.seed_independent_agent(
            "Monitor",
            system_prompt=BUILTIN_MONITOR_PROMPT,
            trigger_settings={"interval_minutes": 30, "check_all": True},
            max_cycles=0,
        )

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
            self._ensure_builtin_independent_agents()
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
            self.runtime_db.put_snapshot(
                "worker",
                {
                    "pid": os.getpid(),
                    "heartbeat_at": utc_now(),
                    "degraded": True,
                    "error": "catalog discovery is incomplete",
                    "rate_limit_cooldown": self._rate_limit_cooldown,
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
        waiting_order = build_waiting_order(runtime_tasks)
        projections = [
            build_task_projection(
                task,
                tasks=tasks,
                waiting_order=waiting_order,
                repository_allowed_roots=self.config.repository_allowed_roots,
            )
            for task in runtime_tasks
        ]
        self.runtime_db.replace_task_projections(projections, catalog=catalog)
        self.runtime_degraded = False
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
        self.runtime_db.put_snapshot(
            "worker",
            {
                "pid": os.getpid(),
                "heartbeat_at": utc_now(),
                "degraded": self.runtime_degraded,
                "task_count": len(self.registry.tasks_by_id) if self.registry else 0,
                "browser_connected": self._browser_connected,
                "browser_error": self._browser_error,
                "rate_limit_cooldown": self._rate_limit_cooldown,
            },
        )

    def _command_task_state(self, task_id: str) -> dict[str, Any] | None:
        if self.registry is not None and task_id in self.registry.tasks_by_id:
            return dict(self.registry.tasks_by_id[task_id])
        return self.store.load_task_id(task_id)

    def _repository_allowed(self, value: object) -> Path:
        repository = Path(str(value or self.config.repository_root)).expanduser().resolve()
        if not any(repository.is_relative_to(root) for root in self.config.repository_allowed_roots):
            raise ValueError("target repository is outside repositories.allowed_roots")
        return repository

    def _publish_command_state(self, state: Mapping[str, Any]) -> None:
        if self.registry is None:
            self.hydrate_runtime(startup=False)
            return
        affected = self.registry.update_task(state, now=time.time())
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
            if len(revisions) != 1 or revisions[0].get("goal") != payload.get("goal"):
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
                    definition = payload.get("bootstrap_definition")
                    if definition is not None:
                        if not isinstance(definition, Mapping):
                            raise ValueError("bootstrap_definition must be an object")
                        bootstrap_id = str(payload.get("bootstrap_id") or "")
                        if bootstrap_id != str(definition.get("bootstrap_id") or ""):
                            raise ValueError(
                                "bootstrap_definition must match requested bootstrap_id"
                            )
                        catalog = BootstrapCatalog(self.config.repository_root)
                        existing_bootstrap = catalog.get(bootstrap_id)
                        if existing_bootstrap is None:
                            now = utc_now()
                            bootstrap = catalog.upsert(
                                {
                                    "bootstrap_id": bootstrap_id,
                                    "name": definition.get("name"),
                                    "description": "",
                                    "source_conversation_id": definition.get(
                                        "source_conversation_id"
                                    ),
                                    "prewarm_prompt": definition.get("prewarm_prompt"),
                                    "max_backups": definition.get("max_backups", 7),
                                    "donors": [],
                                    "enabled": True,
                                    "tags": [],
                                    "created_at": now,
                                    "updated_at": now,
                                }
                            )
                        else:
                            expected_definition = {
                                "bootstrap_id": bootstrap_id,
                                "name": definition.get("name"),
                                "source_conversation_id": definition.get(
                                    "source_conversation_id"
                                ),
                                "prewarm_prompt": definition.get("prewarm_prompt"),
                                "max_backups": definition.get("max_backups"),
                            }
                            actual_definition = {
                                key: existing_bootstrap.get(key)
                                for key in expected_definition
                            }
                            if actual_definition != expected_definition:
                                raise ValueError(
                                    f"bootstrap already exists with different definition: {bootstrap_id}"
                                )
                            if existing_bootstrap.get("enabled") is not True:
                                raise ValueError(
                                    f"requested bootstrap is disabled: {bootstrap_id}"
                                )
                            bootstrap = existing_bootstrap
                    elif "bootstrap_id" in payload and payload.get("bootstrap_id") is not None:
                        bootstrap_id = str(payload.get("bootstrap_id") or "")
                        bootstrap = BootstrapCatalog(self.config.repository_root).get(
                            bootstrap_id
                        )
                        if bootstrap is None:
                            raise ValueError(
                                f"requested bootstrap does not exist: {bootstrap_id}"
                            )
                        if bootstrap.get("enabled") is not True:
                            raise ValueError(
                                f"requested bootstrap is disabled: {bootstrap_id}"
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
                        external_command_id=command_id,
                    )
                self._publish_command_state(state)
                result = {"task_id": task_id, "status": state.get("status")}
            elif kind == "create_workflow_agent":
                definition = self.store.create_workflow_agent(
                    display_name=payload.get("name"),
                    system_prompt=payload.get("system_prompt"),
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
                state, target = self._queue_independent_task_control(
                    current,
                    payload=payload,
                    command_id=command_id,
                )
                self._publish_command_state(state)
                self._publish_command_state(target)
                result = {
                    "task_id": state["task_id"],
                    "target_task_id": target["task_id"],
                    "control_id": target["controls"][-1]["control_id"],
                }
            elif kind == "independent_settings":
                if task_id is None:
                    raise ValueError("independent_settings requires task_id")
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.update_independent_agent(
                    current["manifest_path"],
                    enabled=payload.get("enabled") if "enabled" in payload else None,
                    display_name=payload.get("display_name"),
                    system_prompt=payload.get("system_prompt"),
                    trigger_settings=(
                        payload.get("trigger_settings")
                        if "trigger_settings" in payload
                        else None
                    ),
                    max_cycles=(
                        int(payload["max_cycles"])
                        if "max_cycles" in payload
                        else None
                    ),
                    new_chat_next_job=(
                        payload.get("new_chat_next_job")
                        if "new_chat_next_job" in payload
                        else None
                    ),
                    external_command_id=command_id,
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
                current = self._command_task_state(task_id)
                if current is None:
                    raise ValueError(f"task does not exist: {task_id}")
                state = self.store.request_control(
                    current["manifest_path"],
                    str(payload.get("action") or ""),
                    role=payload.get("role"),
                    reason=payload.get("reason"),
                    confirmed=bool(payload.get("confirmed")),
                    external_command_id=command_id,
                )
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
        if self.runtime_degraded or self.registry is None:
            recovery = self._apply_next_command(allowed_kinds=("reload_catalog",))
            if recovery is not None:
                return recovery
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
                    "independent_create_repair",
                    "independent_task_control",
                    "resume_team",
                    "task_control",
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
        browser_safe_commands = (
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
            "independent_create_repair",
            "independent_task_control",
            "resume_team",
            "task_control",
        )
        if self._browser_cycle_active:
            allowed = browser_safe_commands
        else:
            allowed = None
        return self._apply_next_command(allowed_kinds=allowed)

    async def run_command_loop(self) -> None:
        while True:
            command = self.dispatch_command_once()
            if command is None:
                await asyncio.sleep(self.config.command_poll_seconds)
            else:
                await asyncio.sleep(0)

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
                repository_allowed_roots=self.config.repository_allowed_roots,
            )
            for task_id in sorted(projection_ids)
            if task_id in self.registry.tasks_by_id
        ]
        if projections:
            self.runtime_db.upsert_task_projections(projections)
            self._publish_dashboard_actions()

    async def run_once(self, browser_context: Any) -> list[dict[str, Any] | None]:
        hydrated_now = False
        if self.registry is None and not self.runtime_degraded:
            self.hydrate_runtime()
            hydrated_now = True
        if self.runtime_degraded or self.registry is None:
            self._publish_heartbeat()
            return []
        self._browser_cycle_active = True
        try:
            await self._publish_browser_inventory(browser_context)
            await self._refresh_rate_limit_cooldown(browser_context)
            self._activate_independent_agents()
            await self._close_idle_independent_tabs(
                CDPATabActions(browser_context, self.config)
            )
            now = time.time()
            due_at = now + MINIMUM_DEADLINE_SECONDS if hydrated_now else now
            results: list[dict[str, Any] | None] = []
            disconnect: BaseException | None = None
            affected: set[str] = set()
            running: dict[asyncio.Task[Any], tuple[str, Path]] = {}
            scheduled_versions: dict[str, str] = {}

            def schedule_due(at: float, *, controls_only: bool = False) -> None:
                scheduling_tasks = list(self.registry.tasks_by_id.values())
                active_ids = {task_id for task_id, _path in running.values()}
                for task_id in self.registry.due_task_ids(at):
                    state = self.registry.tasks_by_id[task_id]
                    if controls_only and not any(
                        isinstance(item, Mapping) and item.get("status") == "requested"
                        for item in state.get("controls") or []
                    ):
                        continue
                    version = str(state.get("updated_at") or "")
                    if task_id in active_ids or scheduled_versions.get(task_id) == version:
                        continue
                    path = self.registry.paths_by_id[task_id]
                    task = asyncio.create_task(
                        self.advance(
                            path,
                            browser_context,
                            scheduling_tasks=scheduling_tasks,
                        )
                    )
                    running[task] = (task_id, path)
                    scheduled_versions[task_id] = version

            schedule_due(due_at)
            while running:
                done, _pending = await asyncio.wait(
                    tuple(running),
                    timeout=MINIMUM_DEADLINE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task_id, path = running.pop(task)
                    try:
                        result = task.result()
                    except BaseException as exc:
                        result = exc
                    if isinstance(result, BaseException):
                        if is_cdp_disconnect(result):
                            disconnect = disconnect or result
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
                schedule_due(time.time(), controls_only=True)
            if disconnect is not None:
                raise disconnect
            self._publish_affected(affected)
            self._activate_independent_agents()
            self._publish_heartbeat(browser_connected=True)
            return results
        finally:
            self._browser_cycle_active = False

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
    worker.hydrate_runtime()
    command_task = asyncio.create_task(worker.run_command_loop())
    heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
    reconnect_delay = max(0.5, min(2.0, config.worker_poll_seconds))
    last_reconnect_signature: str | None = None
    last_reconnect_log_at = 0.0
    try:
        while True:
            try:
                async with connected_browser(config.cdp_url) as browser:
                    if not browser.contexts:
                        raise RuntimeError("CDP browser has no persistent context")
                    reconnect_delay = max(0.5, min(2.0, config.worker_poll_seconds))
                    last_reconnect_signature = None
                    worker._publish_heartbeat(force=True, browser_connected=True)
                    await worker.run_forever(browser.contexts[0])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                signature = f"{type(exc).__name__}: {exc}"
                worker._publish_browser_disconnected()
                worker._publish_heartbeat(
                    force=True, browser_connected=False, browser_error=signature
                )
                now = time.monotonic()
                if (
                    signature != last_reconnect_signature
                    or now - last_reconnect_log_at >= 60.0
                ):
                    print(f"cdpa-worker: CDP reconnect after {signature}", file=sys.stderr)
                    last_reconnect_signature = signature
                    last_reconnect_log_at = now
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(10.0, reconnect_delay * 2)
    finally:
        command_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(command_task, heartbeat_task, return_exceptions=True)
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
