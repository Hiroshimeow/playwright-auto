"""One operational decision/execution path for waiting, Resume and pre-send recovery.

This controller performs at most one browser action per step, using the existing
worker's persistence and routing.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from ..cdpa_response import begin_refresh, finish_refresh, parse_time
from . import allow, response, timeout
from .model import Action, Context, Decision, Policy
from .signals import RoleSignals, SignalIdentity, record_cycle, record_signal_error


_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "rate-limit", "request limit")
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

        # After F5, wait for the page to settle before reading either response source.
        ready_at = parse_time(wait.get("refresh_ready_at"))
        if phase == "waiting" and ready_at is not None and ctx.now < ready_at:
            return self._remember(ctx, Decision(Action.WAIT, "reload_settle"))
        if ready_at is not None:
            wait.pop("refresh_ready_at", None)

        listen_pending = False
        dom_pending = False
        if phase == "waiting":
            # Priority #1: terminal LISTEN response.
            if network_response is not None:
                response.observe_candidate(ctx, network_response, validate, source="listen")
                listen_decision = response.handle(ctx)
                if listen_decision is not None:
                    return self._remember(ctx, listen_decision)
                listen_pending = bool(
                    ctx.candidate is not None
                    and ctx.validation_error is None
                    and not ctx.candidate_stable
                )

            # Priority #2: rendered DOM response.
            response.observe(ctx, getattr(receipt, "baseline", None), validate)
            dom_decision = response.handle(ctx)
            if dom_decision is not None:
                return self._remember(ctx, dom_decision)
            dom_pending = bool(
                ctx.candidate is not None
                and ctx.validation_error is None
                and not ctx.candidate_stable
            )

            # A complete-looking candidate must remain unchanged for five seconds.
            if listen_pending or dom_pending:
                return self._remember(ctx, Decision(Action.WAIT, "result_stability"))

        # Only after both response sources fail to produce a routable result do UI
        # interruption/recovery signals participate in the decision.
        dialog_text = " ".join(dialogs).lower()
        if getattr(snapshot, "requires_login", False) or "session has expired" in dialog_text:
            return self._remember(ctx, Decision(Action.BLOCK, "authentication_required"))
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
            timeout.observe(ctx)
            if getattr(snapshot, "retry_visible", False):
                retry_seen_at = parse_time(wait.get("retry_seen_at"))
                if retry_seen_at is None:
                    wait["retry_seen_at"] = ctx.now.isoformat()
                elif (ctx.now - retry_seen_at).total_seconds() >= 5:
                    return self._remember(ctx, Decision(Action.REPAIR, "retry_visible_stable"))
            else:
                wait.pop("retry_seen_at", None)

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
            return self._remember(ctx, Decision(
                Action.REPAIR, "malformed_result", ctx.candidate, ctx.validation_error,
            ))
        if ctx.candidate is not None and not ctx.candidate_stable:
            return self._remember(ctx, Decision(Action.WAIT, "result_stability"))

        permission_decision = allow.handle(ctx)
        if permission_decision is not None:
            return self._remember(ctx, permission_decision)

        if phase == "history":
            return self._remember(ctx, Decision(Action.WAIT, "history_observation"))

        if phase == "pre_send":
            # Streaming/Stop are recovery diagnostics, never authority to block a
            # new hop. Pre-send only requires an editable composer after the
            # response/UI interruption checks above.
            if error_texts:
                key = hashlib.sha256("|".join(error_texts).encode()).hexdigest()
                if wait.get("ui_error_refreshed_key") != key:
                    return self._remember(ctx, Decision(Action.REFRESH, "ui_error"))
                return self._remember(ctx, Decision(Action.WAIT, "ui_error_waiting_for_composer"))
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
        # LISTEN response and rendered DOM remain independent response sources.
        # Policy consumes one normalized signal frame; raw network chunks never leak in.
        listen_candidate = (
            MessageSnapshot.from_dict(signals.network_response)
            if phase == "waiting" and isinstance(signals.network_response, Mapping)
            else None
        )
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
            network_response=listen_candidate,
            signals=signals,
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
            result = await self.browser_action(client, snapshot, wait, permission, decision)
            if decision.action is Action.ALLOW:
                state["active_action"] = ("wait_mcp_allow_continuation" if result.action is Action.ALLOW
                                          else "wait_mcp_allow_handler_pending")
            return result
        if decision.action is Action.STATUS:
            wait["timeout_status_checked"] = True
            wait["timeout_status"] = await worker._one_shot_stream_status(actions, receipt) if receipt else None
            return decision
        if decision.action is Action.REPAIR and decision.reason != "malformed_result":
            retryable = decision.reason in {"retry_visible_stable", "stream_failure_recovery"}
            worker._block(
                state,
                ("Provider Retry or stream failure requires operator recovery without replaying the accepted Send."
                 if retryable else
                 "Accepted execution has no terminal result; exact response recovery is required before format repair."),
                code=decision.reason if retryable else "response_without_terminal_result",
                retryable=retryable,
            )
            return decision
        if decision.action is Action.REPAIR:
            worker._queue_format_repair(
                state, hop, decision.response,
                RouteContractError(decision.validation_error or decision.reason),
            )
            return decision
        if decision.action is Action.ACCEPT:
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

    async def browser_action(self, client, snapshot, wait, permission, decision) -> Decision:
        from ..connection import is_cdp_disconnect
        if decision.action is Action.ALLOW:
            try:
                dispatched = await client.auto_allow_mcp_permission(passive_action=permission)
            except Exception as exc:
                if is_cdp_disconnect(exc):
                    raise
                wait["mcp_allow_dispatch_error"] = type(exc).__name__
                dispatched = None
            now = datetime.now(timezone.utc)
            if not isinstance(dispatched, Mapping):
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
                    return Decision(Action.WAIT, "mcp_allow_stale_cleared")
                # A real permission node with no dispatchable handler gets one
                # bounded attempt. Reuse the normal post-Allow window so that
                # no UI progress becomes F5 after five seconds instead of an
                # unbounded five-second redispatch loop.
                for key in ("mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at"):
                    wait.pop(key, None)
                signature, length = allow.activity(snapshot)
                wait.update(
                    mcp_allow_clicked_at=now.isoformat(),
                    mcp_allow_clicked_target=str((permission or {}).get("target_message_id") or "__dom__"),
                    mcp_allow_activity_signature=signature,
                    mcp_allow_activity_length=length,
                )
                return Decision(Action.WAIT, "mcp_allow_handler_pending")
            for key in ("mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at", "mcp_allow_dispatch_error"):
                wait.pop(key, None)
            signature, length = allow.activity(snapshot)
            wait.update(
                mcp_allow_clicked_at=now.isoformat(),
                mcp_allow_clicked_target=str((permission or {}).get("target_message_id") or "__dom__"),
                mcp_allow_activity_signature=signature,
                mcp_allow_activity_length=length,
            )
            clear = getattr(client, "clear_permission_action", None)
            if callable(clear):
                clear()
            return decision
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
