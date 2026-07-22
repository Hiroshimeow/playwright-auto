from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cdpa_actions import AcquiredRole, CDPATabActions, RoleOwnershipError, TeamCloseError
from .cdpa_config import CDPAConfig, load_cdpa_config
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
from .cdpa_routes import (
    RouteContractError,
    expected_report_relative,
    parse_route_response,
    validate_report,
)
from .cdpa_store import TaskStore, utc_now
from .cdpa_team import cleanup_eligible
from .chatgpt import (
    ChoicePromptBlockedError,
    capture_response_recovery_baseline,
    configure_action_delays,
    IncompleteResponseTimeoutError,
    ManualInputPendingError,
    merge_response_recovery_baselines,
    MessageSnapshot,
    response_activity_signature,
    response_transport_ui_active,
    SendReceipt,
    StableMalformedResponseError,
    unique_new_user_message,
)
from .connection import connected_browser
from .durable import RequestLedger, RequestStatus
from .durable_blocks import DurableSendBlock
from .workflow import WorkflowContext

TERMINAL = frozenset({"DONE", "STOPPED"})
IN_FLIGHT = frozenset({"sending", "sent", "waiting"})


def _active_hop(state: Mapping[str, Any]) -> dict[str, Any]:
    active = state.get("active_hop_id")
    for hop in state.get("hops") or []:
        if isinstance(hop, dict) and hop.get("hop_id") == active:
            return hop
    raise RuntimeError(f"active hop {active!r} does not exist")


def _column_for(role: str) -> str:
    if role == "PLAN":
        return "PLANNING"
    if role == "DEV":
        return "WORKING"
    return "VERIFYING"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_cdp_disconnect(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    needles = (
        "browser has been closed",
        "browser closed",
        "connection closed",
        "connection is closed",
        "target page, context or browser has been closed",
        "browser context has been closed",
        "websocket is not open",
    )
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {"TargetClosedError", "BrowserDisconnectedError"}:
            return True
        message = str(current).lower()
        if any(needle in message for needle in needles):
            return True
        current = current.__cause__ or current.__context__
    return False


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

    def _block(
        self,
        state: dict[str, Any],
        error: BaseException | str,
        *,
        code: str = "unexpected_error",
        retryable: bool = False,
    ) -> dict[str, Any]:
        message = str(error)
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

    def _start_wait_budget_from_sent(self, hop: dict[str, Any]) -> None:
        sent_at = parse_time((hop.get("timestamps") or {}).get("sent_at"))
        if sent_at is None:
            raise RuntimeError("sent hop is missing its accepted send timestamp")
        start_wait_budget(
            hop["wait"],
            timeout_seconds=self.config.response_timeout_seconds,
            now=sent_at,
        )

    def _record_acquired(
        self,
        state: dict[str, Any],
        logical_role: str,
        acquired: AcquiredRole,
    ) -> None:
        record = state["roles"][logical_role]
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
                "last_activity_at": utc_now(),
                "last_error": None,
            }
        )
        state["last_role_activity_at"] = record["last_activity_at"]

    async def _owned_or_block(
        self,
        state: dict[str, Any],
        role: str,
        actions: CDPATabActions,
    ) -> AcquiredRole | None:
        try:
            acquired = await actions.locate_owned(state, role)
        except Exception as exc:
            self._block(
                state,
                exc,
                code="role_ownership_ambiguous",
                retryable=False,
            )
            return None
        if acquired is None:
            state["roles"][role]["online"] = False
            self._block(
                state,
                f"owned {state['roles'][role]['physical_role']} tab is offline",
                code="role_offline",
                retryable=False,
            )
            return None
        try:
            hop = _active_hop(state)
        except RuntimeError:
            hop = None
        if hop is not None and str(hop.get("state") or "") in IN_FLIGHT:
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
    ) -> None:
        cleanup = state.setdefault("cleanup", {})
        if cleanup.get("state") == "CLEARING":
            return
        status_before = str(state.get("status") or "").upper()
        now = utc_now()
        active_role = str(state.get("active_role") or "").upper() or None
        active_hop_id = state.get("active_hop_id")
        cleanup.update(
            {
                "state": "CLEARING",
                "phase": "stop_pending",
                "clear_requested_at": cleanup.get("clear_requested_at") or now,
                "cleared_at": None,
                "status_before": status_before,
                "terminal_state_before": state.get("terminal_state"),
                "active_role": active_role,
                "active_hop_id": active_hop_id,
                "control_id": control.get("control_id") if control is not None else None,
                "target_tabs": max(0, int(target_tabs)),
                "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                "retry_count": 0,
                "last_error": None,
                "last_error_at": None,
            }
        )
        if control is not None:
            control["status"] = "cleanup_pending"
            control["result"] = {"phase": "stop_pending", "status_before": status_before}
            control["applied_at"] = now
        if status_before not in TERMINAL:
            if active_hop_id is not None:
                try:
                    hop = _active_hop(state)
                except RuntimeError:
                    hop = None
                if hop is not None and str(hop.get("state") or "") not in {"routed", "abandoned"}:
                    hop["state"] = "abandoned"
                    hop["abandon_reason"] = "team cleanup started"
                    hop.setdefault("timestamps", {})["abandoned_at"] = now
            state["status"] = "STOPPED"
            state["terminal_state"] = "STOPPED"
            state["kanban_column"] = "DONE_STOPPED"
            state["stopped_at"] = state.get("stopped_at") or now
            state["stop_reason"] = "team cleared"
            state["last_role_activity_at"] = now
            state["active_role"] = None
            state["active_hop_id"] = None
        state["active_action"] = "cleanup_stop_pending"
        state["pause_reason"] = None
        state["block_code"] = None
        state["block_retryable"] = False
        state["block_reason"] = None
        self.store.save(manifest_path, state)

    def _record_cleanup_failure(
        self,
        state: dict[str, Any],
        error: BaseException,
    ) -> None:
        cleanup = state.setdefault("cleanup", {})
        if cleanup.get("phase") == "closing":
            cleanup["phase"] = "close_pending"
        now = utc_now()
        detail = f"{type(error).__name__}: {error}"
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
        cleanup = state.setdefault("cleanup", {})
        if cleanup.get("state") == "CLEARED":
            return True
        if cleanup.get("state") != "CLEARING":
            raise RuntimeError("cleanup continuation requires CLEARING state")
        status = str(state.get("status") or "").upper()
        if status not in TERMINAL:
            now = utc_now()
            active_role = str(state.get("active_role") or "").upper() or None
            active_hop_id = state.get("active_hop_id")
            cleanup["status_before"] = cleanup.get("status_before") or status
            cleanup["active_role"] = cleanup.get("active_role") or active_role
            cleanup["active_hop_id"] = cleanup.get("active_hop_id") or active_hop_id
            if active_hop_id is not None:
                try:
                    hop = _active_hop(state)
                except RuntimeError:
                    hop = None
                if hop is not None and str(hop.get("state") or "") not in {"routed", "abandoned"}:
                    hop["state"] = "abandoned"
                    hop["abandon_reason"] = "team cleanup resumed"
                    hop.setdefault("timestamps", {})["abandoned_at"] = now
            state["status"] = "STOPPED"
            state["terminal_state"] = "STOPPED"
            state["kanban_column"] = "DONE_STOPPED"
            state["stopped_at"] = state.get("stopped_at") or now
            state["stop_reason"] = state.get("stop_reason") or "team cleared"
            state["last_role_activity_at"] = now
            state["active_role"] = None
            state["active_hop_id"] = None
            self.store.save(manifest_path, state)
        try:
            phase = str(cleanup.get("phase") or "stop_pending")
            if phase == "stop_pending":
                active_role = str(cleanup.get("active_role") or "").upper()
                stopped = False
                if active_role and active_role in state.get("roles", {}):
                    acquired = await actions.locate_owned(state, active_role)
                    if acquired is not None:
                        stopped = await actions.stop_if_active(acquired)
                cleanup["stopped_response"] = bool(stopped)
                cleanup["phase"] = "close_pending"
                cleanup["last_error"] = None
                cleanup["last_error_at"] = None
                state["active_action"] = "cleanup_close_pending"
                self.store.save(manifest_path, state)
                phase = "close_pending"
            if phase in {"close_pending", "closing"}:
                selected = (
                    list(preflighted_pages)
                    if preflighted_pages is not None
                    else await actions.preflight_team(state)
                )
                cleanup["target_tabs"] = max(
                    int(cleanup.get("target_tabs") or 0),
                    int(cleanup.get("closed_tabs") or 0) + len(selected),
                )
                cleanup["phase"] = "closing"
                state["active_action"] = "cleanup_closing"
                self.store.save(manifest_path, state)
                try:
                    closed = await actions.close_team(state, preflighted_pages=selected)
                except TeamCloseError as exc:
                    cleanup["closed_tabs"] = int(cleanup.get("closed_tabs") or 0) + exc.closed_tabs
                    raise
                cleanup["closed_tabs"] = int(cleanup.get("closed_tabs") or 0) + int(closed)
                cleanup["phase"] = "verify_pending"
                state["active_action"] = "cleanup_verify_pending"
                self.store.save(manifest_path, state)
                phase = "verify_pending"
            if phase == "verify_pending":
                remaining = await actions.preflight_team(state)
                if remaining:
                    cleanup["phase"] = "close_pending"
                    raise RoleOwnershipError(
                        f"post-close verification found {len(remaining)} assigned team tab(s) still open"
                    )
                self._finish_cleanup(state)
                self.store.save(manifest_path, state)
                return True
            if phase == "cleared":
                self._finish_cleanup(state)
                self.store.save(manifest_path, state)
                return True
            raise RuntimeError(f"unknown cleanup phase {phase!r}")
        except Exception as exc:
            self._record_cleanup_failure(state, exc)
            self.store.save(manifest_path, state)
            return False

    async def _apply_control(
        self,
        state: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path | None = None,
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
        role = str(control.get("role") or state.get("active_role") or "PLAN").upper()
        result: Any = None
        try:
            hop = _active_hop(state) if state.get("active_hop_id") is not None else None
            hop_state = str(hop.get("state") or "") if hop else ""
            if action == "pause":
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
                    "status_before": status,
                    "hop_id": hop.get("hop_id") if hop else None,
                    "request_id": hop.get("request_id") if hop else None,
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
                acquired = await actions.locate_owned(state, role)
                recovered = acquired is None
                if acquired is None:
                    acquired = await actions.reopen(state, role)
                    self._record_acquired(state, role, acquired)
                else:
                    await actions.open_tab(acquired)
                result = {"page_id": acquired.page_id, "recovered": recovered}
            elif action == "route_plan":
                if state.get("status") in TERMINAL:
                    raise RuntimeError("cannot route a terminal task to PLAN")
                if hop_state in IN_FLIGHT:
                    raise RuntimeError("cannot route to PLAN across an in-flight send boundary")
                if state.get("active_role") == "PLAN":
                    raise RuntimeError("PLAN is already the active role")
                assert hop is not None
                source_role = str(state.get("active_role") or role)
                old_hop_id = int(hop["hop_id"])
                reason = str(control.get("reason") or "").strip() or "manual route to PLAN"
                hop["state"] = "abandoned"
                hop["abandon_reason"] = reason
                hop["timestamps"]["abandoned_at"] = utc_now()
                new_hop = self._append_hop(
                    state,
                    source_role=source_role,
                    target_role="PLAN",
                    handoff=(state.get("reports") or [{}])[-1].get("path")
                    if state.get("reports")
                    else str(state["task_text"]),
                    kind="control",
                )
                state.setdefault("route_timeline", []).append(
                    {
                        "at": utc_now(),
                        "hop_id": old_hop_id,
                        "source_role": source_role,
                        "route": "PLAN",
                        "kind": "control",
                        "new_hop_id": new_hop["hop_id"],
                        "reason": reason,
                    }
                )
                result = {"old_hop_id": old_hop_id, "new_hop_id": new_hop["hop_id"]}
            elif action == "clear_team":
                cleanup = state.setdefault("cleanup", {})
                if cleanup.get("state") == "CLEARED":
                    if manifest_path is None:
                        raise RuntimeError("Clear Team requires a manifest path")
                    selected_pages = await actions.preflight_team(state)
                    if selected_pages:
                        self._start_cleanup(
                            state,
                            control=control,
                            target_tabs=len(selected_pages),
                            manifest_path=manifest_path,
                        )
                        completed = await self._continue_cleanup(
                            state,
                            actions,
                            manifest_path,
                            preflighted_pages=selected_pages,
                        )
                        if not completed:
                            return True
                        result = {
                            "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                            "status_before": cleanup.get("status_before"),
                            "retry_count": int(cleanup.get("retry_count") or 0),
                            "reverified": True,
                        }
                    else:
                        result = {
                            "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                            "idempotent": True,
                        }
                elif cleanup.get("state") == "CLEARING":
                    if manifest_path is None:
                        raise RuntimeError("cleanup continuation requires a manifest path")
                    completed = await self._continue_cleanup(state, actions, manifest_path)
                    if not completed:
                        return True
                    result = {
                        "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                        "status_before": cleanup.get("status_before"),
                        "retry_count": int(cleanup.get("retry_count") or 0),
                    }
                else:
                    status_before = str(state.get("status") or "").upper()
                    if status_before not in TERMINAL and not bool(control.get("confirmed")):
                        raise RuntimeError("Clear Team requires confirmation for a nonterminal task")
                    if manifest_path is None:
                        raise RuntimeError("Clear Team requires a manifest path")
                    selected_pages = await actions.preflight_team(state)
                    self._start_cleanup(
                        state,
                        control=control,
                        target_tabs=len(selected_pages),
                        manifest_path=manifest_path,
                    )
                    completed = await self._continue_cleanup(
                        state,
                        actions,
                        manifest_path,
                        preflighted_pages=selected_pages,
                    )
                    if not completed:
                        return True
                    result = {
                        "closed_tabs": int(cleanup.get("closed_tabs") or 0),
                        "status_before": cleanup.get("status_before"),
                        "retry_count": int(cleanup.get("retry_count") or 0),
                    }
            else:
                raise RuntimeError(f"unsupported control action {action!r}")
        except Exception as exc:
            control["status"] = "rejected"
            control["result"] = f"{type(exc).__name__}: {exc}"
            control["applied_at"] = utc_now()
            return True
        control["status"] = "applied"
        control["result"] = result
        control["applied_at"] = utc_now()
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
            "validation_error": validation_error,
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
        acquired = await actions.acquire(state, role)
        self._record_acquired(state, role, acquired)
        role_record = state["roles"][role]
        generation = int(role_record.get("conversation_generation") or 0)
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
        acquired = await self._owned_or_block(state, role, actions)
        if acquired is None:
            return
        constructor = self.config.constructor_paths[role].read_text(encoding="utf-8")
        block = DurableSendBlock(
            str(hop["prompt"]),
            ledger_path=hop["ledger_path"],
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
        output = await block.run(WorkflowContext(acquired.client))
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
        ledger.update(
            record.request_id,
            receipt=upgraded.to_dict(),
            error=None,
        )
        hop["receipt"] = upgraded.to_dict()
        self.store.save(manifest_path, state)
        return upgraded

    def _validate_response_candidate(
        self,
        state: Mapping[str, Any],
        hop: Mapping[str, Any],
        response: MessageSnapshot,
    ) -> None:
        role = str(hop["target_role"])
        decision = parse_route_response(response.text, source_role=role)
        validate_report(
            decision.handoff,
            repository_root=state["repository"],
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
        hop["validation_error"] = validation_error
        hop["state"] = "responded"
        hop.setdefault("timestamps", {})["responded_at"] = utc_now()
        state["active_action"] = "validate_route"

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
            state["pause_reason"] = str(exc)
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

    async def _waiting(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        actions: CDPATabActions,
        manifest_path: Path,
    ) -> None:
        role = str(hop["target_role"])
        acquired = await self._owned_or_block(state, role, actions)
        if acquired is None:
            return
        wait = hop["wait"]
        recover_incomplete_refresh(wait)
        self._start_wait_budget_from_sent(hop)
        receipt = SendReceipt.from_dict(hop["receipt"])
        snapshot = await acquired.client.assert_ownership()
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
            snapshot = await acquired.client.assert_ownership()
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
            wait["recovery_baseline"] = merge_response_recovery_baselines(
                wait.get("recovery_baseline"),
                capture_response_recovery_baseline(
                    snapshot.messages,
                    receipt.baseline,
                ),
            )
            begin_refresh(wait)
            self.store.save(manifest_path, state)
            try:
                await actions.refresh(acquired)
            except Exception as exc:
                finish_refresh(wait, error=f"{type(exc).__name__}: {exc}")
                self.store.save(manifest_path, state)
                raise
            finish_refresh(wait)
            self.store.save(manifest_path, state)
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
            state["pause_reason"] = str(exc)
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

    def _repair_route(
        self,
        state: dict[str, Any],
        hop: dict[str, Any],
        error: Exception,
    ) -> None:
        self._complete_request_response(hop)
        attempt = int(hop.get("repair_attempt") or 0) + 1
        hop["validation_error"] = str(error)
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
                "error": str(error),
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
            validation_error=str(error),
        )

    def _responded(self, state: dict[str, Any], hop: dict[str, Any]) -> None:
        role = str(hop["target_role"])
        try:
            decision = parse_route_response(str(hop.get("response") or ""), source_role=role)
            evidence = validate_report(
                decision.handoff,
                repository_root=state["repository"],
                plans_root=self.config.plans_root,
                team=str(state["team"]),
                physical_role=str(hop["physical_role"]),
                turn=int(hop["turn"]),
                task_id=str(state["task_id"]),
            )
        except (RouteContractError, ValueError) as exc:
            self._repair_route(state, hop, exc)
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
            handoff=decision.handoff,
        )

    def _finalize_resume_recheck(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
    ) -> None:
        if state.get("status") == "BLOCKED":
            control["status"] = "reblocked"
            control["result"] = {
                "block_code": state.get("block_code"),
                "block_retryable": bool(state.get("block_retryable")),
                "block_reason": state.get("block_reason"),
            }
        else:
            prior = control.get("result")
            result = dict(prior) if isinstance(prior, Mapping) else {}
            result["rechecked"] = True
            result["status_after"] = state.get("status")
            control["result"] = result
        control["applied_at"] = utc_now()

    async def advance(
        self,
        manifest_path: str | Path,
        browser_context: Any,
    ) -> dict[str, Any] | None:
        path = Path(manifest_path).resolve()
        try:
            with self.store.task_run_lock(path, blocking=False):
                state = self.store.load(path)
                actions = CDPATabActions(browser_context, self.config)
                if state.get("cleanup", {}).get("state") == "CLEARING":
                    await self._continue_cleanup(state, actions, path)
                    return self.store.save(path, state)
                pending_control = next(
                    (
                        item for item in state.get("controls") or []
                        if isinstance(item, dict) and item.get("status") == "requested"
                    ),
                    None,
                )
                resumed_control = None
                if await self._apply_control(state, actions, path):
                    if (
                        pending_control is not None
                        and pending_control.get("action") == "resume"
                        and pending_control.get("status") == "applied"
                        and state.get("status") not in TERMINAL
                    ):
                        resumed_control = pending_control
                    else:
                        return self.store.save(path, state)
                if state.get("status") in TERMINAL:
                    if (
                        not state.get("cleanup", {}).get("cleared_at")
                        and cleanup_eligible(
                            state,
                            idle_seconds=self.config.cleanup_terminal_idle_seconds,
                        )
                    ):
                        try:
                            selected_pages = await actions.preflight_team(state)
                        except Exception as exc:
                            self._record_cleanup_failure(state, exc)
                            return self.store.save(path, state)
                        self._start_cleanup(
                            state,
                            control=None,
                            target_tabs=len(selected_pages),
                            manifest_path=path,
                        )
                        await self._continue_cleanup(
                            state,
                            actions,
                            path,
                            preflighted_pages=selected_pages,
                        )
                        return self.store.save(path, state)
                    return state
                if state.get("status") in {"PAUSED", "BLOCKED"}:
                    return state
                try:
                    hop = _active_hop(state)
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
                        await self._waiting(state, hop, actions, path)
                    elif hop["state"] == "responded":
                        self._responded(state, hop)
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
                except Exception as exc:
                    if resumed_control is None or _is_cdp_disconnect(exc):
                        raise
                    code = (
                        "role_ownership_ambiguous"
                        if isinstance(exc, RoleOwnershipError)
                        else "unexpected_error"
                    )
                    self._block(
                        state,
                        f"{type(exc).__name__}: {exc}",
                        code=code,
                        retryable=False,
                    )
                    self._finalize_resume_recheck(state, resumed_control)
                    return self.store.save(path, state)
                if resumed_control is not None:
                    self._finalize_resume_recheck(state, resumed_control)
                return self.store.save(path, state)
        except BlockingIOError:
            return None
        except Exception as exc:
            if _is_cdp_disconnect(exc):
                raise
            try:
                state = self.store.load(path)
                self._block(
                    state,
                    f"{type(exc).__name__}: {exc}",
                    code="unexpected_error",
                    retryable=False,
                )
                return self.store.save(path, state)
            except Exception:
                raise

    async def run_once(self, browser_context: Any) -> list[dict[str, Any] | None]:
        paths = self.store.discover_paths()
        raw_results = await asyncio.gather(
            *(self.advance(path, browser_context) for path in paths),
            return_exceptions=True,
        )
        results: list[dict[str, Any] | None] = []
        disconnect: BaseException | None = None
        for path, result in zip(paths, raw_results, strict=True):
            if isinstance(result, BaseException):
                if _is_cdp_disconnect(result):
                    disconnect = disconnect or result
                else:
                    print(
                        f"cdpa-worker: manifest {path}: {type(result).__name__}: {result}",
                        file=sys.stderr,
                    )
                results.append(None)
            else:
                results.append(result)
        if disconnect is not None:
            raise disconnect
        return results

    async def run_forever(self, browser_context: Any) -> None:
        browser = getattr(browser_context, "browser", None)
        while True:
            if browser is not None and not browser.is_connected():
                raise ConnectionError("CDP browser disconnected")
            await self.run_once(browser_context)
            if browser is not None and not browser.is_connected():
                raise ConnectionError("CDP browser disconnected")
            await asyncio.sleep(self.config.worker_poll_seconds)


async def _run(config: CDPAConfig) -> None:
    worker = CDPAWorker(config)
    reconnect_delay = max(0.25, min(2.0, config.worker_poll_seconds))
    while True:
        try:
            async with connected_browser(config.cdp_url) as browser:
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no persistent context")
                reconnect_delay = max(0.25, min(2.0, config.worker_poll_seconds))
                await worker.run_forever(browser.contexts[0])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"cdpa-worker: CDP reconnect after {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(10.0, reconnect_delay * 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the persistent CDPA task worker")
    parser.add_argument("--config", default="cdpa.yaml")
    parser.add_argument("--repository", default=".")
    args = parser.parse_args(argv)
    try:
        config = load_cdpa_config(
            args.config,
            repository_root=Path(args.repository).expanduser().resolve(),
        )
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"cdpa-worker: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
