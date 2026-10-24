"""One operational decision/execution path for waiting, Resume and pre-send recovery.

This controller performs at most one browser action per step, using the existing
worker's persistence and routing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from ..cdpa_response import begin_refresh, finish_refresh, parse_time
from . import allow, response, timeout
from .model import Action, Context, Decision, Policy
from .signals import RoleSignals, SignalIdentity, record_cycle, record_signal_error


_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "rate-limit", "request limit")
_PROVIDER_CHALLENGE_MARKERS = ("cloudflare_challenge", "could not load chatgpt models")
_TEMPORARY_SAVE_OFFER = "Want to keep this chat? You can now save it to your history."


class RoleController:
    def __init__(self, policy: Policy | None = None):
        self.policy = policy or Policy()

    def decide(
        self, snapshot: Any, wait: dict[str, Any], *, receipt: Any = None,
        permission: Mapping[str, Any] | None = None,
        validate: Callable[[Any], None] | None = None, phase: str = "waiting",
        network_status: str | None = None, network_response: Any = None,
        now: datetime | None = None, signals: RoleSignals | None = None,
        reconcile: Callable[[Any], dict[str, Any]] | None = None,
    ) -> Decision:
        if signals is None:
            signals = RoleSignals.from_sources(
                snapshot,
                {
                    "permission_action": permission,
                    "status": network_status,
                },
            )
        snapshot = signals.snapshot
        ctx = Context(
            signals,
            wait,
            self.policy,
            now or datetime.now(timezone.utc),
            phase,
        )
        wait["manual_draft_present"] = ctx.draft_present
        dialogs = tuple(getattr(snapshot, "blocking_dialogs", ()) or ())
        error_texts = tuple(getattr(snapshot, "error_texts", ()) or ())
        stop_visible = bool(getattr(snapshot, "stop_visible", False))
        retry_visible = bool(getattr(snapshot, "retry_visible", False))
        # Bounded projection of the same DOM snapshot; no timestamps so idle ticks stay unchanged.
        wait["browser_control"] = {
            "stop_visible": stop_visible,
            "retry_visible": retry_visible,
            "composer_nonempty": bool(str(getattr(snapshot, "composer_text", "") or "").strip()),
            "permission_node_count": int(getattr(snapshot, "mcp_permission_node_count", 0) or 0),
            "permission_allow_count": int(getattr(snapshot, "mcp_permission_allow_count", 0) or 0),
            "blocking_dialog_count": len(dialogs),
            "error_count": len(error_texts),
        }

        # Passive history has no execution lease and must never mutate the browser.
        if phase == "history":
            return self._remember(ctx, Decision(Action.WAIT, "history_observation"))

        # After F5, wait for the page to settle before reading either response source.
        ready_at = parse_time(wait.get("refresh_ready_at"))
        if phase == "waiting" and ready_at is not None and ctx.now < ready_at:
            return self._remember(ctx, Decision(Action.WAIT, "reload_settle"))
        if ready_at is not None:
            wait.pop("refresh_ready_at", None)

        if phase == "waiting":
            # Exact rendered U->A binding owns the operation. LISTEN is
            # supplemental and may only update the already-bound assistant.
            response.observe(ctx, receipt, validate)
            timeout.observe_connection_interruption(ctx)
            binding_error = str(wait.get("response_binding_error") or "")
            if binding_error:
                return self._remember(ctx, Decision(Action.BLOCK, binding_error))
            dom_decision = response.handle(ctx)
            if dom_decision is not None:
                return self._remember(ctx, dom_decision)
            # Terminal-route reconciliation precedes Stop/Retry/repair for the accepted turn.
            terminal = response.reconcile_terminal(ctx, receipt, reconcile)
            if terminal is not None:
                return self._remember(ctx, terminal)
            # The page DOM is the only authority for the accepted assistant turn.
            # LISTEN/network observations are diagnostics, never another result owner.

        # A permission from an earlier turn cannot be owned by a pristine
        # pre-send request. Do not approve it without the exact accepted receipt.
        if phase != "waiting" and ctx.permission_present:
            # No accepted-turn owner is available to approve this old surface.
            # Explicitly pause instead of waiting forever or clicking blindly.
            return self._remember(ctx, Decision(Action.BLOCK, "pre_send_permission_unowned"))
        # Provider risk UI can only tighten an otherwise eligible approval.
        # File/tool content never supplies a route or permission decision.
        if ctx.permission_present and bool(getattr(snapshot, "mcp_provider_risk", False)):
            return self._remember(ctx, Decision(Action.BLOCK, "permission_review"))
        if ctx.permission_present and (
            int(getattr(snapshot, "mcp_permission_allow_count", 0) or 0) > 1
            or int(getattr(snapshot, "mcp_permission_node_count", 0) or 0) > 1
        ):
            return self._remember(ctx, Decision(Action.BLOCK, "permission_surface_ambiguous"))
        if ctx.permission_present and not getattr(snapshot, "mcp_permission_surface_key", None):
            return self._remember(ctx, Decision(Action.BLOCK, "permission_surface_unidentified"))
        if ctx.permission_present and not int(getattr(snapshot, "mcp_permission_allow_count", 0) or 0):
            return self._remember(ctx, Decision(Action.BLOCK, "permission_surface_unactionable"))
        # A visible MCP Allow interruption belongs to this exact DOM page and
        # must be handled before Retry, dialogs, or invalid-format repair.
        permission_decision = allow.handle(ctx)
        if permission_decision is not None:
            return self._remember(ctx, permission_decision)
        if ctx.permission_present:
            # A permission still on the page is never another click for the
            # same surface. Give acknowledgement a bounded window; do not
            # misclassify a stuck tool approval as missing assistant output.
            pending_at = parse_time(wait.get("mcp_allow_pending_at") or wait.get("mcp_allow_clicked_at"))
            if pending_at and (ctx.now - pending_at).total_seconds() >= ctx.policy.timeout_seconds:
                return self._remember(ctx, Decision(Action.BLOCK, "mcp_allow_unresolved"))
            timeout.observe(ctx)
            terminal_timeout = timeout.handle(ctx)
            if terminal_timeout is not None and terminal_timeout.action is Action.BLOCK:
                return self._remember(ctx, Decision(Action.BLOCK, "mcp_allow_unresolved"))
            return self._remember(ctx, Decision(Action.WAIT, "mcp_allow_pending"))

        # A bound assistant may be writing its last route bytes. Do not treat
        # an old Retry banner as stronger evidence until DOM text is stable.
        if phase == "waiting" and ctx.candidate is not None and not ctx.candidate_stable:
            return self._remember(ctx, Decision(Action.WAIT, "result_stability"))

        # Only after the DOM has no valid terminal result do UI interruption
        # and recovery signals participate in the decision.
        dialog_text = " ".join(dialogs).lower()
        if getattr(snapshot, "requires_login", False) or "session has expired" in dialog_text:
            return self._remember(ctx, Decision(Action.BLOCK, "authentication_required"))
        if any(
            marker in str(text).lower()
            for text in error_texts
            for marker in _PROVIDER_CHALLENGE_MARKERS
        ):
            return self._remember(ctx, Decision(Action.BLOCK, "provider_challenge"))
        if any(marker in str(text).lower() for text in (*dialogs, *error_texts) for marker in _RATE_LIMIT_MARKERS):
            return self._remember(ctx, Decision(Action.COOLDOWN, "profile_rate_limit"))

        blocking_dialogs = dialogs
        if phase == "waiting" and parse_qs(urlsplit(str(getattr(snapshot, "url", ""))).query).get("temporary-chat") == ["true"]:
            blocking_dialogs = tuple(
                value for value in dialogs if " ".join(value.split()) != _TEMPORARY_SAVE_OFFER
            )
        if blocking_dialogs and not ctx.permission_present:
            return self._remember(ctx, Decision(Action.WAIT, "blocking_dialog_wait"))
        if getattr(snapshot, "choice_prompt_labels", ()) and not ctx.permission_present:
            return self._remember(ctx, Decision(Action.WAIT, "choice_prompt_wait"))
        if phase == "waiting":
            retry_record = wait.get("provider_retry")
            retry_turn = response.accepted_turn_key(receipt)
            retry_reconciliation_pending = (
                isinstance(retry_record, Mapping)
                and str(retry_record.get("accepted_user_turn") or "") == retry_turn
                and str(retry_record.get("intent_status") or "") == "recovery_required"
            )
            # A durable ambiguous Retry action is reconciliation-only. Normal
            # response/terminal handling above still wins, and permission handling
            # below remains available when the retried response asks for Allow.
            if retry_reconciliation_pending and not ctx.permission_present:
                return self._remember(
                    ctx, Decision(Action.WAIT, "provider_retry_reconciliation_required")
                )
            timeout.observe(ctx)
            # Stop means the response is still active; only Stop-free Retry is eligible.
            if retry_visible and not stop_visible:
                retry_seen_at = parse_time(wait.get("retry_seen_at"))
                turn_key = response.accepted_turn_key(receipt)
                already_retried = (
                    str((wait.get("provider_retry") or {}).get("accepted_user_turn") or "")
                    == turn_key
                )
                if retry_seen_at is None:
                    wait["retry_seen_at"] = ctx.now.isoformat()
                    # A newly visible Retry supersedes any old banner stall.
                    # Give the current DOM a fresh stability window rather
                    # than immediately treating the old timeout as terminal.
                    wait["controller_progress_at"] = ctx.now.isoformat()
                elif (
                    (ctx.now - retry_seen_at).total_seconds() >= 5
                    and turn_key
                    and not already_retried
                ):
                    return self._remember(ctx, Decision(Action.RETRY, "retry_visible_stable"))
            else:
                wait.pop("retry_seen_at", None)

        # The rendered response can be truncated while the web stream is
        # interrupted. Never repair a partial route just because Stop vanished.
        interrupted = timeout.connection_interruption_decision(ctx)
        if interrupted is not None:
            return self._remember(ctx, interrupted)

        if ctx.candidate is not None and ctx.validation_error is not None:
            # A cached LISTEN stream status can outlive the rendered response.
            # Only current-page activity prevents repairing a stable DOM candidate.
            page_state = getattr(snapshot, "state", None)
            if (
                bool(getattr(snapshot, "stop_visible", False))
                or str(getattr(page_state, "value", page_state) or "").lower() == "responding"
                or not ctx.candidate_stable
            ):
                return self._remember(ctx, Decision(Action.WAIT, "malformed_result_stability"))
            if phase == "waiting" and retry_reconciliation_pending:
                return self._remember(
                    ctx, Decision(Action.WAIT, "provider_retry_reconciliation_required")
                )
            return self._remember(ctx, Decision(
                Action.REPAIR, "malformed_result", ctx.candidate, ctx.validation_error,
            ))
        if ctx.candidate is not None and not ctx.candidate_stable:
            return self._remember(ctx, Decision(Action.WAIT, "result_stability"))

        if phase == "waiting":
            retry_record = wait.get("provider_retry")
            if (
                isinstance(retry_record, Mapping)
                and str(retry_record.get("accepted_user_turn") or "")
                == response.accepted_turn_key(receipt)
                and str(retry_record.get("intent_status") or "") == "recovery_required"
            ):
                return self._remember(
                    ctx, Decision(Action.WAIT, "provider_retry_reconciliation_required")
                )

        if phase == "history":
            return self._remember(ctx, Decision(Action.WAIT, "history_observation"))

        if phase == "pre_send":
            # Streaming/Stop are recovery diagnostics, never authority to block a
            # new hop. Pre-send only requires an editable composer after the
            # response/UI interruption checks above.
            # Errors from an older attempt are not pre-send authority. Login,
            # rate limits, and blocking dialogs were classified above; the
            # send owner independently verifies manual composer/attachment safety.
            if ctx.signals.composer_ambiguous:
                return self._remember(ctx, Decision(Action.WAIT, "composer_ambiguous"))
            reason = "ready" if ctx.composer_ready else "composer_pending"
            return self._remember(ctx, Decision(Action.WAIT, reason))

        decision = timeout.handle(ctx)
        if decision is not None:
            return self._remember(ctx, decision)
        return self._remember(ctx, Decision(Action.WAIT, "wait_response"))

    @staticmethod
    def _remember(ctx: Context, decision: Decision) -> Decision:
        ctx.wait["controller_reason"] = decision.reason
        ctx.wait["controller_decision"] = decision.action.value
        return decision

    @staticmethod
    def _hydrate_provider_retry_intent(worker, state, hop, receipt) -> None:
        """Project the deterministic Retry ledger row into controller wait state."""
        turn_key = response.accepted_turn_key(receipt)
        runtime_db = getattr(worker, "runtime_db", None)
        if (
            not turn_key
            or runtime_db is None
            or not getattr(runtime_db, "path", None)
            or not runtime_db.path.exists()
        ):
            return
        from ..cdpa_commands import ProviderRetryIntent

        intent = ProviderRetryIntent.create(
            state=state,
            hop=hop,
            receipt=receipt,
            accepted_user_turn=turn_key,
        )
        command = runtime_db.get_command_by_idempotency_key(intent.idempotency_key)
        if command is None:
            return
        if (
            str(command.get("kind") or "") != "provider_retry"
            or str(command.get("task_id") or "") != intent.task_id
            or dict(command.get("payload") or {}) != intent.payload
        ):
            raise RuntimeError("provider Retry command-ledger identity mismatch")
        result = command.get("result") if isinstance(command.get("result"), Mapping) else {}
        existing = hop.setdefault("wait", {}).get("provider_retry")
        projected = {
            **(dict(existing) if isinstance(existing, Mapping) else {}),
            "accepted_user_turn": turn_key,
            "command_id": intent.command_id,
            "intent_status": str(command.get("status") or ""),
            "result": str(result.get("result") or command.get("status") or ""),
            "original_prompt_resent": False,
        }
        if command.get("error"):
            projected["error"] = str(command["error"])
        hop["wait"]["provider_retry"] = projected
        if projected["intent_status"] == "recovery_required":
            hop["wait"].pop("retry_seen_at", None)

    async def run(self, worker, state, hop, acquired, actions, **kwargs) -> Decision:
        from ..cdpa_actions import is_transient_page_lifecycle_error
        from ..chatgpt import AuthenticationRequiredError, ComposerConflictError
        from ..connection import is_cdp_disconnect
        wait = hop.setdefault("wait", {})
        retry_at = parse_time(wait.get("observation_retry_at"))
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            state["active_action"] = "wait_response"
            return Decision(Action.WAIT, "page_lifecycle_recovery")
        try:
            result = await self._run(worker, state, hop, acquired, actions, **kwargs)
        except Exception as exc:
            record_signal_error(
                SignalIdentity(
                    page_url=str(getattr(acquired, "url", "") or ""),
                    page_id=getattr(acquired, "page_id", None),
                    role=str(hop.get("physical_role") or hop.get("target_role") or "") or None,
                    task_id=str(state.get("task_id") or "") or None,
                    team=str(state.get("team") or "") or None,
                    request_id=str(hop.get("request_id") or "") or None,
                ),
                component="role_controller",
                stage=str(kwargs.get("phase") or "waiting"),
                error=exc,
            )
            if is_cdp_disconnect(exc):
                raise
            if isinstance(exc, AuthenticationRequiredError):
                result = Decision(Action.BLOCK, "authentication_required")
            elif isinstance(exc, ComposerConflictError):
                result = Decision(Action.WAIT, "manual_draft_preserved")
            elif isinstance(exc, TimeoutError) or is_transient_page_lifecycle_error(exc):
                wait["observation_retry_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=5)
                ).isoformat()
                wait["observation_error"] = type(exc).__name__
                result = Decision(Action.WAIT, "page_lifecycle_recovery")
            else:
                raise
            if result.action is Action.BLOCK:
                worker._block(state, result.reason, code=result.reason, retryable=True)
            else:
                state["active_action"] = "wait_response"
            return result
        wait.pop("observation_retry_at", None)
        wait.pop("observation_error", None)
        return result

    async def _run(
        self, worker: Any, state: dict[str, Any], hop: dict[str, Any],
        acquired: Any, actions: Any, *, snapshot: Any = None,
        phase: str = "waiting", wait_ms: int = 5_000,
    ) -> Decision:
        from ..chatgpt import MessageSnapshot, RateLimitBlockedError, SendReceipt
        from ..cdpa_routes import RouteContractError

        client = acquired.client
        wait = hop.setdefault("wait", {})
        receipt = SendReceipt.from_dict(hop["receipt"]) if hop.get("receipt") else None
        if phase == "waiting" and receipt is not None:
            self._hydrate_provider_retry_intent(worker, state, hop, receipt)
        install = getattr(client, "install_page_observer", None)
        if callable(install):
            await install()
        if snapshot is None:
            wait_for_signal = getattr(client, "wait_for_page_observation", None)
            if wait_ms > 0 and callable(wait_for_signal):
                await wait_for_signal(timeout_ms=wait_ms)
                wait_ms = 0
            if receipt is not None:
                snapshot = await client.wait_snapshot(receipt, probe_wait_ms=wait_ms)
            else:
                snapshot = await client.assert_ownership()
        read = getattr(client, "page_observation", None)
        evidence = read() if callable(read) else {}
        signals = RoleSignals.from_sources(
            snapshot, evidence if isinstance(evidence, Mapping) else {}
        )
        permission = signals.permission
        previous = (wait.get("controller_decision"), wait.get("controller_reason"))
        decision = self.decide(
            snapshot,
            wait,
            receipt=receipt,
            validate=(
                lambda candidate: worker._validate_response_candidate(
                    state, hop, candidate
                )
            ),
            phase=phase,
            signals=signals,
            reconcile=(
                (lambda candidate: worker._terminal_route_evidence(state, hop, candidate))
                if phase == "waiting"
                else None
            ),
        )
        identity = SignalIdentity.from_snapshot(
            snapshot,
            request_id=str(hop.get("request_id") or "") or None,
            page_id=acquired.page_id,
            role=str(hop.get("physical_role") or hop.get("target_role")),
            task_id=str(state["task_id"]),
            team=str(state["team"]),
        )
        record_cycle(
            signals,
            wait,
            identity=identity,
            phase=phase,
            previous_decision=previous,
            decision=decision,
        )
        state["active_action"] = "wait_response" if phase == "waiting" else "queued"
        if decision.action is Action.REFRESH and state.get("conversation_mode") == "temporary":
            # Temporary Chat documents are ephemeral. Reload can destroy the live
            # document identity, so automatic F5 is never an allowed recovery.
            if decision.reason == "malformed_result":
                wait["invalid_refreshed_key"] = wait.get("result_seen_key")
            elif decision.reason == "ui_error":
                wait["ui_error_refreshed_key"] = hashlib.sha256(
                    "|".join(getattr(snapshot, "error_texts", ()) or ()).encode()
                ).hexdigest()
            elif decision.reason == "response_timeout_recheck":
                wait["timeout_refreshed"] = True
            wait["temporary_refresh_suppressed_at"] = datetime.now(timezone.utc).isoformat()
            wait["controller_decision"] = Action.WAIT.value
            wait["controller_reason"] = f"temporary_no_refresh:{decision.reason}"
            return Decision(Action.WAIT, f"temporary_no_refresh:{decision.reason}")
        if decision.action is Action.WAIT:
            if decision.reason.startswith("mcp_allow_"):
                state["active_action"] = "wait_" + decision.reason
            return decision
        if decision.action is Action.BLOCK:
            worker._block(state, decision.reason, code=decision.reason, retryable=True)
            return decision
        if decision.action is Action.COOLDOWN:
            await worker._enter_rate_limit_cooldown(state, actions, RateLimitBlockedError("Too many requests"))
            state["active_action"] = "rate_limit_cooldown"
            return decision
        if decision.action in {Action.ALLOW, Action.REFRESH}:
            allow_before = wait.get("mcp_allow_last")
            result = await self.browser_action(
                client, snapshot, wait, permission, decision,
                expected_task_id=str(state["task_id"]), expected_team=str(state["team"]),
                worker=worker, state=state, hop=hop, acquired=acquired, receipt=receipt,
            )
            if decision.action is Action.ALLOW:
                state["active_action"] = (
                    "wait_mcp_allow_continuation" if result.action is Action.ALLOW
                    else "wait_mcp_allow_composer_conflict" if result.reason == "mcp_allow_composer_conflict"
                    else "wait_mcp_allow_handler_pending"
                )
                if wait.get("mcp_allow_last") != allow_before:
                    record_control_event(state, "mcp_allow", wait.get("mcp_allow_last"))
            return result
        if decision.action is Action.RETRY:
            return await self._provider_retry(worker, client, state, hop, receipt, snapshot)
        if decision.action is Action.STATUS:
            wait["timeout_status_checked"] = True
            wait["timeout_status"] = await worker._one_shot_stream_status(actions, receipt) if receipt else None
            return decision
        if decision.action is Action.REPAIR and decision.reason != "malformed_result":
            if decision.reason == "state_repair":
                worker._queue_format_repair(
                    state,
                    hop,
                    None,
                    RouteContractError(
                        "accepted execution produced no terminal result; recover the route "
                        "from the existing workspace/report state without replaying the Send"
                    ),
                )
                return decision
            worker._block(
                state,
                "Provider stream failure requires operator recovery without replaying the accepted Send.",
                code=decision.reason,
                retryable=True,
            )
            return decision
        if decision.action is Action.REPAIR:
            worker._queue_format_repair(
                state, hop, decision.response,
                RouteContractError(decision.validation_error or decision.reason),
            )
            return decision
        if decision.action is Action.ACCEPT:
            if decision.reason == "terminal_route_reconciled":
                record_control_event(state, "terminal_route_reconciled", wait.get("terminal_reconciliation"))
            if receipt is not None:
                worker._capture_passive_remote_report_if_available(state, hop, acquired, receipt, decision.response)
            worker._record_response(state, hop, decision.response)
            if receipt is not None:
                worker._release_stream_status_slot(client, receipt)
            clear_page_response = getattr(client, "clear_page_response_state", None)
            if callable(clear_page_response):
                clear_page_response()
            return decision
        raise AssertionError(f"unhandled operational action: {decision.action}")

    async def maintain_history(self, client, wait: dict) -> Decision:
        # History tabs only auto-Allow; they cannot enqueue work or route old results.
        probe = await client.read_wait_probe()
        observation = client.page_observation()
        signals = RoleSignals.from_sources(
            probe, observation if isinstance(observation, Mapping) else {}
        )
        previous = (wait.get("controller_decision"), wait.get("controller_reason"))
        decision = self.decide(probe, wait, phase="history", signals=signals)
        record_cycle(
            signals,
            wait,
            identity=SignalIdentity.from_snapshot(probe),
            phase="history",
            previous_decision=previous,
            decision=decision,
        )
        if decision.action is Action.ALLOW:
            return await self.browser_action(
                client, probe, wait, signals.permission, decision
            )
        if decision.action is Action.REFRESH:
            return Decision(Action.WAIT, "history_observation")
        return decision

    async def browser_action(
        self, client, snapshot, wait, permission, decision, *,
        expected_task_id: str | None = None, expected_team: str | None = None,
        worker: Any = None, state: Mapping[str, Any] | None = None,
        hop: Mapping[str, Any] | None = None, acquired: Any = None, receipt: Any = None,
    ) -> Decision:
        if decision.action is Action.ALLOW:
            return await self._allow_permission(
                client, snapshot, wait, permission,
                expected_task_id=expected_task_id, expected_team=expected_team,
                worker=worker, state=state, hop=hop, acquired=acquired, receipt=receipt,
            )
        if decision.action is Action.REFRESH:
            if decision.reason == "malformed_result":
                wait["invalid_refreshed_key"] = wait.get("result_seen_key")
            elif decision.reason == "ui_error":
                wait["ui_error_refreshed_key"] = hashlib.sha256("|".join(snapshot.error_texts).encode()).hexdigest()
            elif decision.reason == "response_timeout_recheck":
                wait["timeout_refreshed"] = True
            begin_refresh(wait)
            try:
                await client.refresh()
            except Exception as exc:
                finish_refresh(wait, error=type(exc).__name__)
                raise
            finish_refresh(wait)
            now = datetime.now(timezone.utc)
            wait["refresh_ready_at"] = (now + timedelta(seconds=self.policy.reload_settle_seconds)).isoformat()
            if decision.reason != "response_timeout_recheck":
                wait["controller_progress_at"] = now.isoformat()
            wait["controller_last_refresh_at"] = now.isoformat()
            for key in ("mcp_allow_clicked_at", "mcp_allow_seen_at", "mcp_allow_retry_at"):
                wait.pop(key, None)
            return decision
        raise ValueError(f"not a browser action: {decision.action}")

    async def _allow_permission(
        self, client, snapshot, wait, permission, *,
        expected_task_id: str | None, expected_team: str | None,
        worker: Any = None, state: Mapping[str, Any] | None = None,
        hop: Mapping[str, Any] | None = None, acquired: Any = None, receipt: Any = None,
    ) -> Decision:
        """Click one DOM-owned Allow via one durable, non-replayable intent."""
        from ..chatgpt import ComposerConflictError, PageOwnershipError, TaskBindingError
        from ..connection import is_cdp_disconnect
        target = str(getattr(snapshot, "mcp_permission_surface_key", None) or "__dom__")
        now = datetime.now(timezone.utc)
        # A manual draft is operator-owned; nothing must be reserved or clicked.
        if bool(str(getattr(snapshot, "composer_text", "") or "").strip() or
                tuple(getattr(snapshot, "attachment_markers", ()) or ())):
            _remember_allow(wait, now, "composer_conflict", target)
            return Decision(Action.WAIT, "mcp_allow_composer_conflict")
        runtime_db = getattr(worker, "runtime_db", None) if worker is not None else None
        intent_id: str | None = None
        if worker is not None:
            if not (runtime_db is not None and state is not None and hop is not None and
                    receipt is not None and expected_task_id and expected_team):
                raise RuntimeError("MCP Allow requires the accepted role and durable action ledger")
            turn = response.accepted_turn_key(receipt)
            if not turn:
                raise RuntimeError("MCP Allow requires exact accepted user turn")
            payload = {
                "task_id": str(state["task_id"]),
                "team": str(state["team"]),
                "hop_id": str(hop["hop_id"]),
                "request_id": str(hop["request_id"]),
                "page_id": str(getattr(acquired, "page_id", "") or ""),
                "accepted_turn": turn,
                "surface": target,
            }
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            intent_id = "mcp-allow-" + digest[:32]
            command, created = runtime_db.reserve_action_intent(
                command_id=intent_id,
                idempotency_key="mcp-allow:" + digest,
                kind="mcp_allow",
                task_id=str(state["task_id"]),
                payload=payload,
            )
            if not created:
                wait["mcp_allow_pending_target"] = target
                wait.setdefault("mcp_allow_pending_at", now.isoformat())
                return Decision(Action.WAIT, "mcp_allow_reconciliation_required"
                                if command.get("status") == "recovery_required"
                                else "mcp_allow_already_recorded")
        try:
            dispatched = await client.auto_allow_mcp_permission(
                passive_action=permission,
                expected_task_id=expected_task_id,
                expected_team=expected_team,
                expected_surface_key=target,
            )
        except ComposerConflictError as exc:
            # Ownership/draft can change after the durable pre-click reserve.
            # The exception alone does not prove no click crossed the boundary.
            _remember_allow(wait, now, "composer_conflict", target)
            if intent_id is None:
                return Decision(Action.WAIT, "mcp_allow_composer_conflict")
            runtime_db.require_command_recovery(intent_id, error=_bounded_error(exc))
            wait["mcp_allow_pending_target"] = target
            wait["mcp_allow_pending_at"] = now.isoformat()
            return Decision(Action.WAIT, "mcp_allow_reconciliation_required")
        except (PageOwnershipError, TaskBindingError) as exc:
            _remember_allow(wait, now, "ownership_mismatch", target)
            if intent_id is not None:
                runtime_db.require_command_recovery(intent_id, error=_bounded_error(exc))
            raise
        except Exception as exc:
            # Even a CDP disconnect may occur after click. Preserve the intent
            # and its uncertainty; the same fingerprint cannot be replayed.
            wait["mcp_allow_dispatch_error"] = type(exc).__name__
            if intent_id is not None:
                runtime_db.require_command_recovery(intent_id, error=_bounded_error(exc))
            if is_cdp_disconnect(exc):
                raise
            dispatched = None
        if not isinstance(dispatched, Mapping):
            # The browser action may have clicked before returning null/error.
            # Without positive pre-dispatch evidence this is ambiguous; never retry.
            if intent_id is not None:
                runtime_db.require_command_recovery(
                    intent_id, error="unconfirmed_mcp_allow_dispatch"
                )
            # Network/listen evidence can outlive the actual permission node.
            # If the DOM no longer contains any permission affordance, discard
            # that stale observation instead of retrying it forever and masking
            # a completed response or Retry state.
            dom_permission_present = bool(
                int(getattr(snapshot, "mcp_permission_node_count", 0) or 0)
                or int(getattr(snapshot, "mcp_permission_allow_count", 0) or 0)
            )
            if not dom_permission_present:
                clear = getattr(client, "clear_permission_action", None)
                if callable(clear):
                    clear()
                for key in ("mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at", "mcp_allow_dispatch_error"):
                    wait.pop(key, None)
                _remember_allow(wait, now, "stale_cleared", target)
                return Decision(Action.WAIT, "mcp_allow_stale_cleared")
            # A real permission node with no dispatchable handler gets one
            # bounded attempt. Reuse the normal post-Allow window so that
            # no UI progress becomes F5 after five seconds instead of an
            # unbounded five-second redispatch loop.
            for key in ("mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at"):
                wait.pop(key, None)
            # The handler did not acknowledge a click. Preserve ambiguity
            # without falsely claiming the permission was dispatched.
            wait["mcp_allow_pending_at"] = now.isoformat()
            wait["mcp_allow_pending_target"] = target
            _remember_allow(wait, now, "handler_pending", target)
            return Decision(Action.WAIT, "mcp_allow_handler_pending")
        for key in (
            "mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at",
            "mcp_allow_dispatch_error", "mcp_allow_pending_at", "mcp_allow_pending_target",
        ):
            wait.pop(key, None)
        # A click receipt is not permission acceptance. Persist that exactly
        # this surface has already been dispatched, then await DOM acknowledgement.
        if intent_id is not None:
            runtime_db.finish_action_intent(
                intent_id,
                result={"result": "clicked", "surface": target,
                        "method": str(dispatched.get("method") or "")},
            )
        signature, length = allow.activity(snapshot)
        wait["controller_progress_at"] = now.isoformat()
        wait.update(
            mcp_allow_clicked_at=now.isoformat(),
            mcp_allow_clicked_target=target,
            mcp_allow_activity_signature=signature,
            mcp_allow_activity_length=length,
            mcp_allow_click_count=int(wait.get("mcp_allow_click_count") or 0) + 1,
        )
        _remember_allow(wait, now, "clicked", target, dispatched)
        clear = getattr(client, "clear_permission_action", None)
        if callable(clear):
            clear()
        return Decision(Action.ALLOW, "mcp_allow_ready")

    async def _provider_retry(
        self, worker, client, state: dict[str, Any], hop: dict[str, Any], receipt: Any, snapshot: Any,
    ) -> Decision:
        """Invoke provider Retry once for the exact accepted turn; never resend the prompt."""
        from ..chatgpt import (
            ComposerConflictError, PageOwnershipError, SendRecoveryError,
            TaskBindingError, UnsafePageStateError,
        )
        from ..connection import is_cdp_disconnect
        wait = hop.setdefault("wait", {})
        state["active_action"] = "wait_response"
        turn_key = response.accepted_turn_key(receipt)
        now = datetime.now(timezone.utc).isoformat()
        if (
            str(getattr(snapshot, "composer_text", "") or "").strip()
            or tuple(getattr(snapshot, "attachment_markers", ()) or ())
        ):
            # Operator draft/attachments: do not consume the single Retry; nothing was clicked.
            wait["provider_retry_deferred"] = {"accepted_user_turn": turn_key, "reason": "composer_conflict"}
            return Decision(Action.WAIT, "provider_retry_composer_conflict")
        wait.pop("provider_retry_deferred", None)
        # The existing runtime command ledger is the sole once-only authority for
        # this irreversible browser action. Persist/consume the exact-turn intent
        # before entering ChatGPTPage.retry_generation(), whose mutation guard may
        # click Retry. An existing intent is reconciliation-only and is never replayed.
        from ..cdpa_commands import ProviderRetryIntent

        intent = ProviderRetryIntent.create(
            state=state,
            hop=hop,
            receipt=receipt,
            accepted_user_turn=turn_key,
        )
        command, intent_created = worker.runtime_db.reserve_action_intent(
            command_id=intent.command_id,
            idempotency_key=intent.idempotency_key,
            kind="provider_retry",
            task_id=intent.task_id,
            payload=intent.payload,
        )
        record: dict[str, Any] = {
            "accepted_user_turn": turn_key,
            "attempted_at": now,
            "command_id": intent.command_id,
            "intent_status": str(command.get("status") or ""),
            "result": "pending" if intent_created else str(command.get("status") or "recovery_required"),
            "original_prompt_resent": False,
        }
        wait["provider_retry"] = record
        wait.pop("retry_seen_at", None)
        if not intent_created:
            decision = Decision(
                Action.WAIT,
                "provider_retry_reconciliation_required"
                if command.get("status") == "recovery_required"
                else "provider_retry_already_recorded",
            )
            record_control_event(state, "provider_retry", record)
            wait["controller_decision"] = decision.action.value
            wait["controller_reason"] = decision.reason
            return decision

        decision = Decision(Action.WAIT, "provider_retry_clicked")
        try:
            outcome = await client.retry_generation(
                receipt,
                expected_task_id=str(state["task_id"]),
                expected_team=str(state["team"]),
            )
        except (PageOwnershipError, TaskBindingError) as exc:
            # These exception types can occur on either side of retry.click().
            # Once the durable intent exists, absence of explicit pre-click proof
            # means the irreversible action is ambiguous and must not terminalize.
            record.update(
                result="ambiguous",
                error=_bounded_error(exc),
                intent_status="recovery_required",
            )
            worker.runtime_db.require_command_recovery(
                intent.command_id, result=record, error=record["error"]
            )
            record_control_event(state, "provider_retry", record)
            raise
        except (ComposerConflictError, UnsafePageStateError, SendRecoveryError, TimeoutError) as exc:
            if is_cdp_disconnect(exc):
                raise
            # retry_generation() can raise all of these after retry.click().  Do
            # not infer non-application from the exception class. Keep the single
            # command-ledger intent ambiguous until response/terminal evidence
            # reconciles the accepted turn.
            record.update(
                result="ambiguous",
                error=_bounded_error(exc),
                intent_status="recovery_required",
            )
            worker.runtime_db.require_command_recovery(
                intent.command_id, result=record, error=record["error"]
            )
            decision = Decision(Action.WAIT, "provider_retry_reconciliation_required")
        else:
            record.update(
                result="progress",
                transport_active=bool((outcome or {}).get("transport_active")),
            )
            completed = worker.runtime_db.finish_action_intent(intent.command_id, result=record)
            record["intent_status"] = str(completed.get("status") or "applied")
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        record_control_event(state, "provider_retry", record)
        wait["controller_decision"] = decision.action.value
        wait["controller_reason"] = decision.reason
        return decision


_CONTROL_EVENT_LIMIT = 50


def record_control_event(
    state: dict[str, Any], kind: str, values: Mapping[str, Any] | None
) -> None:
    """Append one bounded Phase 1.5 control diagnostic to the task timeline."""
    events = state.setdefault("control_events", [])
    events.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "hop_id": state.get("active_hop_id"),
        **{str(key): value for key, value in dict(values or {}).items()},
    })
    del events[:-_CONTROL_EVENT_LIMIT]


def _remember_allow(
    wait: dict[str, Any], now: datetime, result: str, target: str,
    dispatched: Mapping[str, Any] | None = None,
) -> None:
    current = {
        "result": result,
        "target": target,
        "method": str((dispatched or {}).get("method") or "") or None,
        "remember_answer": (dispatched or {}).get("remember_answer"),
    }
    previous = dict(wait.get("mcp_allow_last") or {})
    previous.pop("at", None)
    if previous != current or result == "clicked":
        wait["mcp_allow_last"] = {**current, "at": now.isoformat()}


def _bounded_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]
