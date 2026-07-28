from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cdpa_actions import AcquiredRole, CDPATabActions, RoleOwnershipError, TeamCloseError
from .cdpa_commands import (
    RepairRequest,
    WorkerCommand,
    command_snapshot,
    conversation_identity,
    validate_worker_command,
)
from .cdpa_config import CDPAConfig, load_cdpa_config
from .cdpa_browser_projection import build_browser_projection
from .cdpa_independent import (
    BUILTIN_MAINTAINERS_PROMPT,
    BUILTIN_MONITOR_PROMPT,
    INDEPENDENT_COLUMN,
    INDEPENDENT_ROLE,
    canonical_independent_events,
    claim_oldest_event,
    is_independent_task,
    normalize_agent_name,
    record_consumed_event,
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
    validate_report,
)
from .cdpa_store import (
    TaskStore,
    is_replaced_immutable_history,
    report_mode_from_options,
    utc_now,
)
from .cdpa_team import cleanup_eligible, has_other_nonterminal_team_work
from .chatgpt import (
    ChoicePromptBlockedError,
    ComposerConflictError,
    PageOwnershipError,
    SendRecoveryError,
    TaskBindingError,
    attachment_names_match,
    capture_message_baseline,
    capture_response_recovery_baseline,
    configure_action_delays,
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
from .connection import connected_browser, is_cdp_disconnect
from .durable import RequestLedger, RequestStatus
from .durable_blocks import DurableSendBlock
from .upload import UploadIdentityChangedError, UploadReceipt, collect_file_identities
from .workflow import WorkflowContext

TERMINAL = frozenset({"DONE", "STOPPED"})
IN_FLIGHT = frozenset({"sending", "sent", "waiting"})
_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_RATE_LIMIT_BLOCK_MESSAGE = (
    "Too many requests; shared browser-profile cooldown is active"
)
_RATE_LIMIT_DEFERRED_CONTROLS = frozenset(
    {"resume", "retry", "restart_role", "new_chat", "route_plan"}
)
_RATE_LIMIT_DEFERRED_COMMANDS = frozenset(
    {"independent_run_now", "independent_activate_agent"}
)


class IneffectiveControlError(RuntimeError):
    """The primitive ran, but its required operational postcondition did not hold."""


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
        self._browser_connected = False
        self._browser_error: str | None = None
        self._browser_cycle_active = False
        self._rate_limit_cooldown: dict[str, Any] | None = None
        self._rate_limit_lock = asyncio.Lock()
        self._send_gate_lock = asyncio.Lock()
        self.rate_limit_cooldown_seconds = _RATE_LIMIT_COOLDOWN_SECONDS
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

    def _rate_limit_gate_active(self) -> bool:
        return (
            isinstance(self._rate_limit_cooldown, Mapping)
            and self._rate_limit_cooldown.get("state") == "active"
        )

    def _cooldown_allows_hop(self, hop: Mapping[str, Any]) -> bool:
        hop_state = str(hop.get("state") or "")
        if hop_state in {"sent", "waiting", "responded", "routed"}:
            return True
        receipt = hop.get("receipt")
        if isinstance(receipt, Mapping) and (
            receipt.get("user_message_id") or receipt.get("user_turn_id")
        ):
            return True
        ledger_path = hop.get("ledger_path")
        request_id = hop.get("request_id")
        if not ledger_path or not request_id:
            return False
        try:
            record = RequestLedger(str(ledger_path)).get(str(request_id))
        except Exception:
            return False
        return bool(
            record is not None
            and record.status
            in {RequestStatus.SENDING, RequestStatus.SENT, RequestStatus.COMPLETED}
        )

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
            self._rate_limit_cooldown = dict(cooldown)

    async def _enter_rate_limit_cooldown(
        self,
        state: Mapping[str, Any],
        actions: CDPATabActions,
        error: BaseException | str,
    ) -> dict[str, Any]:
        async with self._rate_limit_lock:
            if self._rate_limit_gate_active():
                assert self._rate_limit_cooldown is not None
                return self._rate_limit_cooldown
            now = datetime.now(timezone.utc)
            role = str(state.get("active_role") or "").upper()
            if not role and state.get("active_hop_id") is not None:
                role = str(_active_hop(state).get("target_role") or "").upper()
            self._rate_limit_cooldown = {
                "state": "active",
                "detected_at": now.isoformat(),
                "release_not_before": (
                    now + timedelta(seconds=float(self.rate_limit_cooldown_seconds))
                ).isoformat(),
                "reason": sanitize_text(error, max_chars=500),
                "profile": str(self.config.cdp_url),
                "detector_task_id": str(state.get("task_id") or "") or None,
                "detector_role": role or None,
                "dismiss_attempted_at": None,
                "dismiss_result": None,
                "last_checked_at": None,
                "banner_visible": None,
            }
            self._publish_heartbeat(force=True)
            acquired = None
            if role:
                try:
                    acquired = await actions.locate_owned(state, role)
                except Exception as exc:
                    self._rate_limit_cooldown["dismiss_result"] = (
                        f"locate_failed:{type(exc).__name__}"
                    )
            if acquired is not None:
                self._rate_limit_cooldown["dismiss_attempted_at"] = utc_now()
                try:
                    self._rate_limit_cooldown["dismiss_result"] = (
                        await acquired.client.dismiss_known_rate_limit()
                    )
                except Exception as exc:
                    self._rate_limit_cooldown["dismiss_result"] = sanitize_text(
                        f"{type(exc).__name__}: {exc}", max_chars=500
                    )
            self._publish_heartbeat(force=True)
            return self._rate_limit_cooldown

    def _apply_rate_limit_to_state(
        self,
        state: dict[str, Any],
        hop: Mapping[str, Any],
    ) -> None:
        if self._cooldown_allows_hop(hop):
            state["active_action"] = "rate_limit_cooldown_reconcile"
            state["block_code"] = None
            state["block_retryable"] = False
            state["block_reason"] = None
            return
        self._block(
            state,
            _RATE_LIMIT_BLOCK_MESSAGE,
            code="rate_limit_cooldown",
            retryable=True,
        )

    def _release_rate_limit_blocked_tasks(self) -> set[str]:
        if self.registry is None:
            return set()
        changed: set[str] = set()
        for snapshot in list(self.registry.tasks_by_id.values()):
            if (
                str(snapshot.get("status") or "").upper() != "BLOCKED"
                or snapshot.get("block_code") != "rate_limit_cooldown"
            ):
                continue
            task_id = str(snapshot.get("task_id") or "")
            path = self.registry.paths_by_id.get(task_id)
            if path is None:
                continue

            def release(current: dict[str, Any]) -> dict[str, Any]:
                if (
                    str(current.get("status") or "").upper() != "BLOCKED"
                    or current.get("block_code") != "rate_limit_cooldown"
                ):
                    return current
                role = str(current.get("active_role") or "PLAN").upper()
                current["status"] = "RUNNING"
                current["kanban_column"] = (
                    INDEPENDENT_COLUMN
                    if is_independent_task(current)
                    else _column_for(role)
                )
                current["block_code"] = None
                current["block_retryable"] = False
                current["block_reason"] = None
                try:
                    hop_state = str(_active_hop(current).get("state") or "")
                except RuntimeError:
                    hop_state = ""
                current["active_action"] = (
                    "wait_response"
                    if hop_state in {"sent", "waiting"}
                    else "send"
                    if hop_state == "sending"
                    else "queued"
                )
                return current

            saved = self.store.update(path, release)
            if saved != snapshot:
                changed.update(self.registry.update_task(saved, now=time.time()))
        self._publish_affected(changed)
        return changed

    async def _refresh_rate_limit_cooldown(
        self, browser_context: Any
    ) -> bool:
        if not self._rate_limit_gate_active():
            return False
        assert self._rate_limit_cooldown is not None
        release_at = parse_time(self._rate_limit_cooldown.get("release_not_before"))
        if release_at is None or datetime.now(timezone.utc) < release_at:
            return False
        if self.registry is None:
            return False
        actions = CDPATabActions(browser_context, self.config)
        detector_task_id = str(
            self._rate_limit_cooldown.get("detector_task_id") or ""
        )
        detector_role = str(
            self._rate_limit_cooldown.get("detector_role") or ""
        ).upper()
        tasks = list(self.registry.tasks_by_id.values())
        tasks.sort(
            key=lambda item: (
                str(item.get("task_id") or "") != detector_task_id,
                str(item.get("task_id") or ""),
            )
        )
        probed = False
        for snapshot in tasks:
            roles = list((snapshot.get("roles") or {}).keys())
            roles.sort(
                key=lambda role: (
                    not (
                        str(snapshot.get("task_id") or "") == detector_task_id
                        and str(role).upper() == detector_role
                    ),
                    str(role),
                )
            )
            for role in roles:
                record = (snapshot.get("roles") or {}).get(role)
                if not isinstance(record, Mapping) or not record.get("page_id"):
                    continue
                try:
                    acquired = await actions.locate_owned(snapshot, str(role))
                except Exception:
                    continue
                if acquired is None:
                    continue
                probed = True
                try:
                    visible = bool(
                        await acquired.client.known_rate_limit_visible()
                    )
                except Exception as exc:
                    self._rate_limit_cooldown["last_probe_error"] = sanitize_text(
                        f"{type(exc).__name__}: {exc}", max_chars=500
                    )
                    continue
                self._rate_limit_cooldown["last_checked_at"] = utc_now()
                self._rate_limit_cooldown["banner_visible"] = visible
                self._publish_heartbeat(force=True)
                if visible:
                    return False
                self._rate_limit_cooldown.update(
                    state="released",
                    released_at=utc_now(),
                    banner_visible=False,
                    last_probe_error=None,
                )
                self._release_rate_limit_blocked_tasks()
                self._publish_heartbeat(force=True)
                return True
        if not probed:
            self._rate_limit_cooldown["last_probe_error"] = (
                "no exact owned ChatGPT tab was available for cooldown recheck"
            )
            self._publish_heartbeat(force=True)
        return False

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
            state, role, require_clean_ready=require_clean_ready
        )
        if _recoverable_conversation_identity(acquired.url) != expected_conversation:
            raise RoleOwnershipError(
                "automatic role recovery reopened a different conversation"
            )
        if expected_page_id is not None and str(acquired.page_id) != expected_page_id:
            raise RoleOwnershipError(
                "automatic role recovery did not restore the recorded page identity"
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
            live_conversation = (
                _recoverable_conversation_identity(acquired.url) if acquired is not None else None
            )
            if (
                acquired is not None
                and live_conversation is not None
                and expected_conversation is None
            ):
                expected_conversation = live_conversation
                hop["conversation_url"] = acquired.url
            if acquired is None and expected_conversation is None:
                physical_role = state["roles"][role]["physical_role"]
                raise RoleOwnershipError(
                    f"owned {physical_role} tab is offline", code="role_offline"
                )
            if (
                acquired is None
                or (
                    expected_conversation is not None
                    and _recoverable_conversation_identity(acquired.url) != expected_conversation
                )
                or (
                    expected_page_id is not None
                    and str(acquired.page_id) != expected_page_id
                )
            ):
                acquired = await self._recover_active_role(state, role, actions)
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
            independent["enabled"] = True
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
                at = utc_now()
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
        if action == "stop":
            acquired = await actions.locate_owned(state, role)
            stopped_response = (
                await actions.stop_if_active(acquired) if acquired is not None else False
            )
            event = independent.get("active_event")
            event_key = (
                str(event.get("event_key") or "")
                if isinstance(event, Mapping)
                else ""
            )
            if event_key:
                record_consumed_event(independent, event)
            independent["enabled"] = False
            independent["active_event"] = None
            state["status"] = "STOPPED"
            state["terminal_state"] = "STOPPED"
            state["kanban_column"] = "STOPPED"
            state["stopped_at"] = utc_now()
            state["stop_reason"] = control.get("reason") or "independent job stopped"
            state["active_role"] = None
            state["active_hop_id"] = None
            state["active_action"] = "stopped"
            return {"stopped_response": stopped_response, "enabled": False}
        raise RuntimeError(f"unsupported independent control action {action!r}")

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
        if (
            self._rate_limit_gate_active()
            and action in _RATE_LIMIT_DEFERRED_CONTROLS
        ):
            return False
        control_id = control.get("control_id")
        baseline = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        baseline_control_id = control.get("control_id")
        role = str(control.get("role") or state.get("active_role") or "PLAN").upper()
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
                acquired = await actions.locate_owned(state, stop_role)
                if acquired is not None:
                    result = {"stopped_response": await actions.stop_if_active(acquired)}
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
            elif action == "restart_role":
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
                if hop_state in IN_FLIGHT:
                    raise RuntimeError(
                        "cannot restart a role across an in-flight send boundary"
                    )
                old_page_id = state["roles"][role].get("page_id")
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
                if hop_state in IN_FLIGHT:
                    raise RuntimeError("cannot reset a role across an in-flight send boundary")
                acquired = await actions.new_chat(state, role)
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
                if blocked_role_recovery:
                    try:
                        acquired = await self._recover_active_role(state, role, actions)
                    except RoleOwnershipError as exc:
                        raise IneffectiveControlError(str(exc)) from exc
                    recovered = True
                else:
                    acquired = await actions.locate_owned(state, role)
                    recovered = acquired is None
                    if acquired is None:
                        acquired = await actions.reopen(state, role)
                    else:
                        await actions.open_tab(acquired)
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
                assert hop is not None
                result = self._route_to_plan(
                    state,
                    hop,
                    reason=(
                        str(control.get("reason") or "").strip()
                        or "manual route to PLAN"
                    ),
                    kind="control",
                    allow_active_plan=False,
                    require_retained_report=False,
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
            state.update(baseline)
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
        if action == "resume" and not is_independent_task(state):
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

    async def _pre_send(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        role = str(hop["target_role"])
        if self._attachment_files_for_generation(state, role) is None:
            return
        independent = state.get("independent") if is_independent_task(state) else None
        if (
            isinstance(independent, dict)
            and independent.get("new_chat_next_job") is True
            and independent.get("new_chat_deferred_task_id")
            != str(state.get("task_id") or "")
        ):
            acquired = await actions.new_chat(state, role)
            independent["new_chat_next_job"] = False
            independent["new_chat_deferred_task_id"] = None
        else:
            try:
                acquired = await actions.acquire(state, role)
            except RoleOwnershipError as exc:
                if _role_ownership_block_code(exc) != "role_offline":
                    raise
                _, expected_conversation, _, _ = self._active_recovery_context(
                    state, role
                )
                if expected_conversation is None:
                    raise
                acquired = await self._recover_active_role(state, role, actions)
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
                    return
                try:
                    acquired = await actions.acquire(state, role)
                except UnsafePageStateError as retry_exc:
                    if (
                        str(retry_exc).strip().casefold()
                        == "page is already responding"
                    ):
                        state["active_action"] = "reconcile_page_response"
                        return
                    raise
        self._record_acquired(state, role, acquired)
        role_record = state["roles"][role]
        generation = int(role_record.get("conversation_generation") or 0)
        if isinstance(independent, Mapping):
            active_event = independent.get("active_event")
            if not isinstance(active_event, Mapping):
                raise ValueError("independent job has no active trigger event")
            built = self.prompts.build_independent(
                agent_name=str(independent["agent_name"]),
                system_prompt=str(independent["system_prompt"]),
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                workspace=str(state["repository"]),
                event=active_event,
                cycle=int(independent.get("cycle") or 1),
                max_cycles=int(independent.get("max_cycles") or 1),
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
        expected = expected_report_relative(
            plans_root=self.config.plans_root,
            repository_root=self.config.repository_root,
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
        if hop.get("kind") == "route_repair":
            prompt = self.prompts.repair(
                task_id=str(state["task_id"]),
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                validation_error=str(hop["validation_error"]),
                report_mode=_report_mode(state),
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
                allowed_routes=("PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"),
                workspace=str(state["repository"]),
                source_physical_role=source_physical,
                handoff=str(hop["handoff"]),
                goal=str(state["task_text"]),
                constructor_sent_generation=role_record.get(
                    "constructor_sent_generation"
                ),
                conversation_generation=generation,
                report_mode=_report_mode(state),
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
            constructor = self.config.constructor_paths[role].read_text(encoding="utf-8")
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
            response_timeout_ms=None,
        )
        try:
            async with self._send_gate_lock:
                current_record = (
                    ledger.get(str(hop["request_id"]))
                    if ledger.path.exists()
                    else None
                )
                crossed_send_boundary = (
                    current_record is not None
                    and current_record.status
                    in {
                        RequestStatus.SENDING,
                        RequestStatus.SENT,
                        RequestStatus.COMPLETED,
                    }
                )
                if self._rate_limit_gate_active() and not crossed_send_boundary:
                    self._apply_rate_limit_to_state(state, hop)
                    return
                try:
                    output = await block.run(WorkflowContext(acquired.client))
                except RateLimitBlockedError as exc:
                    await self._enter_rate_limit_cooldown(state, actions, exc)
                    self._apply_rate_limit_to_state(state, hop)
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
        baseline = json.loads(json.dumps(state, ensure_ascii=False, default=str))
        ledger.update(
            record.request_id,
            receipt=upgraded.to_dict(),
            error=None,
        )
        hop["receipt"] = upgraded.to_dict()
        self._persist_transport_result(manifest_path, baseline, state)
        return upgraded

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
        parsed = parse_role_response(
            response.text,
            source_role=role,
            report_mode=_report_mode(state),
        )
        if parsed.inline_report is None:
            validate_report(
                parsed.decision.handoff,
                repository_root=self.config.repository_root,
                plans_root=self.config.plans_root,
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                task_id=str(state["task_id"]),
            )

    def _record_response(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        response: MessageSnapshot,
        *,
        validation_error: str | None = None,
    ) -> None:
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
        self._record_response(state, hop, response)
        return True

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

    async def _waiting(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
        transport_baseline: dict[str, Any] | None = None,
    ) -> None:
        role = str(hop["target_role"])
        acquired = await self._owned_or_block(state, role, actions)
        if acquired is None:
            return
        wait = hop["wait"]
        recover_incomplete_refresh(wait)
        self._start_wait_budget_from_sent(hop)
        receipt = SendReceipt.from_dict(hop["receipt"])
        snapshot = await self._waiting_snapshot(
            acquired.client,
            receipt,
            probe_wait_ms=12_000,
        )
        receipt = self._upgrade_legacy_receipt(
            state,
            hop,
            receipt,
            snapshot,
            manifest_path,
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
        remaining = remaining_timeout_ms(wait)
        refreshed_this_cycle = False
        should_refresh = refresh_due(
            wait,
            refresh_after_seconds=self.config.response_refresh_after_seconds,
            composer_empty=snapshot.composer_empty,
            manual_input_pending=snapshot.manual_input_pending,
        )
        if remaining <= 0 or should_refresh:
            if await self._final_response_reconciliation(
                state, hop, acquired, receipt, wait
            ):
                return
            snapshot = await self._waiting_snapshot(
                acquired.client,
                receipt,
                force_full=True,
            )
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
                json.dumps(state, ensure_ascii=False, default=str)
            )
            wait["recovery_baseline"] = merge_response_recovery_baselines(
                wait.get("recovery_baseline"),
                capture_response_recovery_baseline(
                    snapshot.messages,
                    receipt.baseline,
                ),
            )
            begin_refresh(wait)
            self._persist_transport_result(manifest_path, refresh_baseline, state)
            refresh_baseline = json.loads(
                json.dumps(state, ensure_ascii=False, default=str)
            )
            try:
                await actions.refresh(acquired)
            except Exception as exc:
                finish_refresh(wait, error=sanitize_exception(exc))
                self._persist_transport_result(manifest_path, refresh_baseline, state)
                raise
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
            if await self._final_response_reconciliation(
                state, hop, acquired, receipt, wait
            ):
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
                timeout_ms=min(3_000, remaining),
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
            self._record_response(
                state,
                hop,
                exc.candidate,
                validation_error=str(exc.validation_error),
            )
            return
        except (TimeoutError, IncompleteResponseTimeoutError):
            if remaining_timeout_ms(wait) <= 0:
                if await self._final_response_reconciliation(
                    state, hop, acquired, receipt, wait
                ):
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
        self._record_response(state, hop, response)

    def _route_to_plan(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        *,
        reason: str,
        kind: str,
        allow_active_plan: bool,
        require_retained_report: bool,
    ) -> dict[str, int]:
        if state.get("status") in TERMINAL:
            raise RuntimeError("cannot route a terminal task to PLAN")
        if str(hop.get("state") or "") in IN_FLIGHT:
            raise RuntimeError("cannot route to PLAN across an in-flight send boundary")
        source_role = str(state.get("active_role") or hop.get("target_role") or "PLAN")
        if source_role == "PLAN" and not allow_active_plan:
            raise RuntimeError("PLAN is already the active role")
        retained_reports = [
            item
            for item in state.get("reports") or []
            if isinstance(item, Mapping) and str(item.get("path") or "").strip()
        ]
        if require_retained_report and not retained_reports:
            raise RuntimeError("no retained report is available for PLAN continuation")
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
    def _operator_lifecycle_recovery_pending(state: Mapping[str, Any]) -> bool:
        if str(state.get("status") or "").upper() != "RUNNING":
            return True
        if str((state.get("cleanup") or {}).get("state") or "ACTIVE") != "ACTIVE":
            return True
        return any(
            isinstance(item, Mapping)
            and item.get("origin") == "operator"
            and item.get("status") == "requested"
            and item.get("action")
            in {"pause", "stop", "clear_team", "restart_role", "new_chat"}
            for item in state.get("controls") or []
        )

    def _is_identical_missing_file_repair(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        error: Exception,
        decision: Any,
    ) -> bool:
        if (
            _report_mode(state) != "file"
            or str(error) != "report file does not exist"
            or decision is None
            or hop.get("kind") != "route_repair"
            or str(decision.handoff) != str(hop.get("expected_report_path") or "")
        ):
            return False
        parent_id = hop.get("parent_hop_id")
        parent = next(
            (
                item
                for item in state.get("hops") or []
                if isinstance(item, Mapping) and item.get("hop_id") == parent_id
            ),
            None,
        )
        return bool(
            isinstance(parent, Mapping)
            and parent.get("target_role") == hop.get("target_role")
            and parent.get("physical_role") == hop.get("physical_role")
            and parent.get("turn") == hop.get("turn")
            and parent.get("expected_report_path") == hop.get("expected_report_path")
            and conversation_identity(parent.get("conversation_url"))
            == conversation_identity(hop.get("conversation_url"))
            and parent.get("response_sha256") == hop.get("response_sha256")
            and parent.get("validation_error") == "report file does not exist"
            and parent.get("validation_route") == decision.route
            and parent.get("validation_handoff") == decision.handoff
        )

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
        if self._is_identical_missing_file_repair(state, hop, error, decision):
            if self._operator_lifecycle_recovery_pending(state):
                return
            hops_by_id = {
                item.get("hop_id"): item
                for item in state.get("hops") or []
                if isinstance(item, Mapping) and isinstance(item.get("hop_id"), int)
            }
            ancestor_id = hop.get("parent_hop_id")
            visited: set[int] = set()
            fallback_exhausted = False
            while isinstance(ancestor_id, int) and ancestor_id not in visited:
                visited.add(ancestor_id)
                ancestor = hops_by_id.get(ancestor_id)
                if not isinstance(ancestor, Mapping):
                    break
                if ancestor.get("kind") == "missing_file_fallback":
                    fallback_exhausted = True
                    break
                ancestor_id = ancestor.get("parent_hop_id")
            retained_report = any(
                isinstance(item, Mapping) and str(item.get("path") or "").strip()
                for item in state.get("reports") or []
            )
            if fallback_exhausted or not retained_report:
                self._block(
                    state,
                    (
                        "valid route report could not be materialized after bounded PLAN recovery"
                        if fallback_exhausted
                        else "valid route report could not be materialized and no retained report exists"
                    ),
                    code="report_materialization_unavailable",
                    retryable=False,
                )
                return
            self._route_to_plan(
                state,
                hop,
                reason="identical missing file report repeated after one route repair",
                kind="missing_file_fallback",
                allow_active_plan=True,
                require_retained_report=True,
            )
            return
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
        decision = None
        try:
            parsed = parse_role_response(
                str(hop.get("response") or ""),
                source_role=role,
                report_mode=_report_mode(state),
            )
            decision = parsed.decision
            if parsed.inline_report is None:
                evidence = validate_report(
                    decision.handoff,
                    repository_root=self.config.repository_root,
                    plans_root=self.config.plans_root,
                    team=str(state["team"]),
                    physical_role=str(hop["physical_role"]),
                    turn=int(hop["turn"]),
                    task_id=str(state["task_id"]),
                )
                routed_handoff = decision.handoff
            else:
                try:
                    evidence = materialize_inline_report(
                        parsed.inline_report,
                        expected_report_path=str(hop.get("expected_report_path") or ""),
                        repository_root=self.config.repository_root,
                        plans_root=self.config.plans_root,
                        team=str(state["team"]),
                        physical_role=str(hop["physical_role"]),
                        turn=int(hop["turn"]),
                        task_id=str(state["task_id"]),
                    )
                except (RouteContractError, OSError) as exc:
                    raise InlineReportMaterializationError(
                        "inline report materialization failed"
                    ) from exc
                routed_handoff = str(hop["expected_report_path"])
        except InlineReportMaterializationError:
            self._block(
                state,
                "inline report materialization failed",
                code="inline_report_materialization_failed",
                retryable=False,
            )
            return
        except (RouteContractError, ValueError, OSError) as exc:
            self._repair_route(state, hop, exc, decision=decision)
            return
        self._complete_request_response(hop)
        hop.update(
            {
                "report_path": evidence.path,
                "report_sha256": evidence.sha256,
                "report_size": evidence.size,
                "route": decision.route,
                "state": "routed",
            }
        )
        hop["timestamps"]["routed_at"] = utc_now()
        if not any(
            item.get("path") == evidence.path and item.get("sha256") == evidence.sha256
            for item in state.get("reports") or []
            if isinstance(item, Mapping)
        ):
            state.setdefault("reports", []).append(
                {
                    "report_id": len(state.get("reports") or []) + 1,
                    "role": role,
                    "physical_role": hop["physical_role"],
                    "turn": hop["turn"],
                    "path": evidence.path,
                    "sha256": evidence.sha256,
                    "size": evidence.size,
                    "created_at": utc_now(),
                }
            )
        state.setdefault("route_timeline", []).append(
            {
                "at": utc_now(),
                "hop_id": hop["hop_id"],
                "source_role": role,
                "route": decision.route,
                "report_path": evidence.path,
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
    ) -> tuple[AcquiredRole, bool]:
        role = str(hop["target_role"])
        acquired = await actions.locate_owned(state, role)
        reopened = acquired is None
        if acquired is None:
            acquired = await actions.reopen(state, role)
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
                "recovered tab does not match the durable conversation identity"
            )
        self._record_acquired(state, role, acquired)
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
        receipt = SendReceipt.from_dict(receipt_value)
        try:
            acquired, reopened = await self._resume_exact_owned_role(
                state,
                hop,
                actions,
                expected_page_id=receipt.binding.page_id,
            )
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
        if str(getattr(snapshot, "composer_text", "") or "").strip() or tuple(
            getattr(snapshot, "attachment_markers", ()) or ()
        ):
            self._require_resume_recovery(
                state,
                control,
                action="none",
                reason_code="manual_composer_conflict",
                reason="Resume found manual composer input or attachments on the accepted request tab.",
                next_safe_action="Resolve the manual draft or attachments without overwriting them, then Resume again.",
            )
            return
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
        except (TimeoutError, IncompleteResponseTimeoutError):
            response = None
        if response is not None:
            old_hop_id = state.get("active_hop_id")
            self._record_response(state, hop, response)
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

        if bool(getattr(snapshot, "retry_visible", False)):
            try:
                progress = await acquired.client.retry_generation(
                    receipt,
                    expected_task_id=str(state["task_id"]),
                    expected_team=str(state["team"]),
                    timeout_ms=max(
                        250, min(2_000, int(self.config.worker_poll_seconds * 2000))
                    ),
                )
            except (ComposerConflictError, PageOwnershipError, TaskBindingError) as exc:
                self._require_resume_recovery(
                    state,
                    control,
                    action="retry_generation",
                    reason_code="manual_composer_conflict"
                    if isinstance(exc, ComposerConflictError)
                    else "ownership_conflict",
                    reason=sanitize_exception(exc),
                    next_safe_action="Restore exact ownership and resolve manual input before retrying.",
                )
                return
            if isinstance(progress, Mapping) and progress.get("progress"):
                sent_at = datetime.now(timezone.utc)
                hop.setdefault("timestamps", {})["sent_at"] = sent_at.isoformat()
                start_wait_budget(
                    hop["wait"],
                    timeout_seconds=self.config.response_timeout_seconds,
                    now=sent_at,
                )
                state["status"] = "RUNNING"
                state["kanban_column"] = _column_for(str(hop["target_role"]))
                state["active_action"] = "wait_response"
                self._finish_resume_control(
                    state,
                    control,
                    outcome="continued",
                    action="retry_generation",
                    reason_code=None,
                    reason="Retry generation produced verified progress for the accepted user turn.",
                    postcondition="generation_progress",
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
            reason="Resume found no stable response, Retry control, or verified generation progress.",
            next_safe_action="Inspect the exact conversation and choose Retry generation only if the accepted turn is visible.",
        )

    async def _recover_resume_sending(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        control: dict[str, Any],
        actions: CDPATabActions,
    ) -> None:
        ledger = RequestLedger(str(hop.get("ledger_path") or ""))
        record = ledger.get(str(hop.get("request_id") or ""))
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
        try:
            acquired, _reopened = await self._resume_exact_owned_role(
                state,
                hop,
                actions,
                expected_page_id=record.binding.page_id,
            )
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
            receipt = await acquired.client.send(
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

    def _sync_resume_commands(self, state: Mapping[str, Any]) -> None:
        for control in state.get("controls") or []:
            if not isinstance(control, Mapping) or control.get("action") != "resume":
                continue
            status = str(control.get("status") or "")
            if status not in {"applied", "recovery_required", "failed", "rejected"}:
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
                if scheduling_changed:
                    return state
                if state.get("status") == "WAITING":
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
                    return self.store.load(path)
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
                        return self._load_manifest_cached(path)
                    else:
                        raise RuntimeError("control result was not persisted atomically")
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
                    self._sync_resume_commands(saved)
                    return saved
                if state.get("status") in {"PAUSED", "BLOCKED"}:
                    return state
                hop = _active_hop(state)
                if (
                    self._rate_limit_gate_active()
                    and not self._cooldown_allows_hop(hop)
                ):
                    self._apply_rate_limit_to_state(state, hop)
                    saved = self._persist_transport_result(
                        path, transport_baseline, state
                    )
                    state.clear()
                    state.update(saved)
                    return saved
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
                        self._responded(state, hop)
                        if is_independent_task(state):
                            pending = state["independent"]
                            completion = pending.get("completion_request")
                            continuation = pending.get("continuation_request")
                            if completion is not None:
                                completed, successor = self.store.complete_independent_task(
                                    path,
                                    outcome=str(completion.get("outcome") or ""),
                                    summary=str(completion.get("summary") or ""),
                                    target_task_id=completion.get("target_task_id"),
                                    repair_task_id=completion.get("repair_task_id"),
                                )
                                self._publish_command_state(completed)
                                self._publish_command_state(successor)
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
            max_cycles=5,
        )
        self.store.seed_independent_agent(
            "Monitor",
            system_prompt=BUILTIN_MONITOR_PROMPT,
            trigger_settings={"interval_minutes": 30, "check_all": True},
            max_cycles=1,
        )

    def _activate_independent_agents(self) -> set[str]:
        if self.registry is None or self._rate_limit_gate_active():
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
            if str(snapshot.get("status") or "").upper() != "WAITING":
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
            if (
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
                    str(state.get("status") or "").upper() != "WAITING"
                    or current.get("active_event") is not None
                    or current.get("idle_since") != independent.get("idle_since")
                ):
                    return state
                current["idle_tab_closed_at"] = utc_now()
                current["close_tab_when_idle"] = False
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
            self._ensure_builtin_independent_agents()
        tasks, errors = self.store.discover_with_errors()
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
        self._manifest_cache.clear()
        for path, task in records:
            self._remember_manifest(path, task)
        self.registry = CDPARuntimeRegistry.hydrate(
            tasks,
            now=time.time(),
            cleanup_idle_seconds=self.config.cleanup_terminal_idle_seconds,
        )
        waiting_order = build_waiting_order(tasks)
        projections = [
            build_task_projection(task, tasks=tasks, waiting_order=waiting_order)
            for task in tasks
        ]
        self.runtime_db.replace_task_projections(projections, catalog=catalog)
        self.runtime_degraded = False
        self._publish_dashboard_actions()
        self._publish_heartbeat(force=True)
        return catalog

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
            "independent_complete",
            "independent_continue",
            "independent_run_now",
            "independent_settings",
            "independent_activate_agent",
            "independent_create_repair",
            "independent_task_control",
        }:
            if not is_independent_task(state):
                raise RuntimeError("independent command provenance belongs to a workflow task")
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
                if str(command.get("kind") or "") == "resume_team":
                    self._sync_resume_commands(replay_state)
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
                    state = self.store.create_task(
                        str(payload.get("task") or ""),
                        requested_team=payload.get("requested_team"),
                        reuse_team=payload.get("reuse_team") or None,
                        new_roles=tuple(payload.get("new_roles") or ()),
                        new_all=bool(payload.get("new_all")),
                        repository=self._repository_allowed(payload.get("repository")),
                        task_id=task_id,
                        report_mode=str(payload.get("report_mode") or "file"),
                        depends_on_task_ids=tuple(payload.get("depends_on_task_ids") or ()),
                        upload_paths=tuple(payload.get("upload_paths") or ()),
                        external_command_id=command_id,
                    )
                self._publish_command_state(state)
                result = {"task_id": task_id, "status": state.get("status")}
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
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                result = {"task_id": state["task_id"], "status": state["status"]}
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
                    completed, successor = self.store.complete_independent_task(
                        requested["manifest_path"],
                        outcome=str(payload.get("outcome") or ""),
                        summary=str(payload.get("summary") or ""),
                        target_task_id=payload.get("target_task_id"),
                        repair_task_id=payload.get("repair_task_id"),
                    )
                    self._publish_command_state(completed)
                    self._publish_command_state(successor)
                    state = completed
                    result = {
                        "task_id": completed["task_id"],
                        "status": completed["status"],
                        "successor_task_id": successor["task_id"],
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
                    system_prompt=payload.get("system_prompt"),
                    trigger_settings=(
                        payload.get("trigger_settings")
                        if "trigger_settings" in payload
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
            elif kind == "resume_team":
                state = self.store.resume_team(
                    str(payload.get("team") or ""),
                    reason=payload.get("reason"),
                    external_command_id=command_id,
                )
                self._publish_command_state(state)
                self._sync_resume_commands(state)
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
                result = {"task_id": task_id, "status": state.get("status")}
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
                    "create_independent_agent",
                    "independent_complete",
                    "independent_continue",
                    "independent_run_now",
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
            "create_independent_agent",
            "independent_complete",
            "independent_continue",
            "independent_run_now",
            "independent_settings",
            "independent_activate_agent",
            "independent_create_repair",
            "independent_task_control",
            "resume_team",
            "task_control",
        )
        if self._rate_limit_gate_active():
            allowed = tuple(
                kind
                for kind in browser_safe_commands
                if kind not in _RATE_LIMIT_DEFERRED_COMMANDS
            )
        elif self._browser_cycle_active:
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
        projection = await build_browser_projection(
            browser_context, previous=self._browser_projection
        )
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
            due_ids = list(self.registry.due_task_ids(due_at))
            paths = [self.registry.paths_by_id[task_id] for task_id in due_ids]
            scheduling_tasks = list(self.registry.tasks_by_id.values())
            raw_results = await asyncio.gather(
                *(
                    self.advance(
                        path,
                        browser_context,
                        scheduling_tasks=scheduling_tasks,
                    )
                    for path in paths
                ),
                return_exceptions=True,
            )
            results: list[dict[str, Any] | None] = []
            disconnect: BaseException | None = None
            affected: set[str] = set()
            for task_id, path, result in zip(due_ids, paths, raw_results, strict=True):
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
                        if previous != result:
                            affected.update(
                                self.registry.update_task(result, now=time.time())
                            )
                    else:
                        affected.add(task_id)
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
        await asyncio.gather(command_task, return_exceptions=True)
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
