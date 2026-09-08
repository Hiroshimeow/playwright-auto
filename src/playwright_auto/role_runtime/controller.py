"""One operational decision/execution path for waiting, Resume and pre-send recovery.

Handlers do not send, click or create tasks. This controller performs at most one
browser action per step, using the existing worker's persistence and routing.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..cdpa_response import begin_refresh, finish_refresh, parse_time
from . import allow, auth, dialog, draft, malformed, rate_limit, response, retry, stalled, timeout, ui_error
from .model import Action, Context, Decision, Policy


class RoleController:
    def __init__(self, policy: Policy | None = None):
        self.policy = policy or Policy()

    def decide(
        self, snapshot: Any, wait: dict[str, Any], *, receipt: Any = None,
        permission: Mapping[str, Any] | None = None,
        validate: Callable[[Any], None] | None = None, phase: str = "waiting",
        dom_only: bool = False, network_status: str | None = None,
        now: datetime | None = None,
    ) -> Decision:
        ctx = Context(snapshot, wait, self.policy, now or datetime.now(timezone.utc),
                      permission, phase, dom_only, network_status=network_status)
        wait["manual_draft_present"] = ctx.draft_present
        for handler in (auth.handle, rate_limit.handle, dialog.handle):
            decision = handler(ctx)
            if decision is not None:
                return self._remember(ctx, decision)
        if phase == "waiting":
            response.observe(ctx, getattr(receipt, "baseline", None), validate)
        stalled.observe(ctx)
        ready_at = parse_time(wait.get("refresh_ready_at"))
        if ready_at is not None and ctx.now < ready_at:
            return self._remember(ctx, Decision(Action.WAIT, "reload_settle"))
        wait.pop("refresh_ready_at", None)

        # A stable validated role result is terminal for this hop. It must not be
        # masked by stale permission evidence left behind by an earlier tool turn.
        if phase == "waiting":
            result_decision = response.handle(ctx)
            if result_decision is not None:
                return self._remember(ctx, result_decision)

        permission_decision = allow.handle(ctx)
        if permission_decision is not None:
            # A detected-but-undispatchable permission must not bypass stall/timeout
            # recovery forever. First detection and post-click windows still get 5s.
            recovery = stalled.handle(ctx)
            if wait.get("mcp_allow_retry_at"):
                recovery = timeout.handle(ctx) or recovery
            return self._remember(ctx, draft.protect(ctx, recovery or permission_decision))

        if phase == "history":
            return self._remember(ctx, Decision(Action.WAIT, "history_observation"))

        if phase == "pre_send":
            if ctx.active:
                return self._remember(ctx, draft.protect(ctx, stalled.handle(ctx) or Decision(Action.WAIT, "response_in_progress")))
            if ctx.draft_present:
                return self._remember(ctx, Decision(Action.WAIT, "manual_draft_preserved"))
            if getattr(snapshot, "error_texts", ()):
                decision = ui_error.handle(ctx)
                return self._remember(ctx, decision or Decision(Action.WAIT, "ui_error"))
            reason = "ready" if ctx.composer_ready else "composer_pending"
            return self._remember(ctx, Decision(Action.WAIT, reason))

        for handler in (retry.handle, malformed.handle, ui_error.handle):
            decision = handler(ctx)
            if decision is not None and decision.action is not Action.WAIT:
                return self._remember(ctx, draft.protect(ctx, decision))
        # Give a current candidate its normal two samples even at the deadline.
        if ctx.candidate is not None and not ctx.candidate_stable and not ctx.active:
            return self._remember(ctx, Decision(Action.WAIT, "result_stability"))
        decision = timeout.handle(ctx) or stalled.handle(ctx)
        if decision is not None:
            return self._remember(ctx, draft.protect(ctx, decision))
        return self._remember(ctx, Decision(Action.WAIT, "response_in_progress" if ctx.active else "wait_response"))

    @staticmethod
    def _remember(ctx: Context, decision: Decision) -> Decision:
        ctx.wait["controller_reason"] = decision.reason
        ctx.wait["controller_decision"] = decision.action.value
        return decision

    async def run(self, worker, state, hop, acquired, actions, **kwargs) -> Decision:
        from ..connection import is_cdp_disconnect
        from . import lifecycle
        wait = hop.setdefault("wait", {})
        retry_at = parse_time(wait.get("observation_retry_at"))
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            state["active_action"] = "wait_response"
            return Decision(Action.WAIT, "page_lifecycle_recovery")
        try:
            result = await self._run(worker, state, hop, acquired, actions, **kwargs)
        except Exception as exc:
            if is_cdp_disconnect(exc):
                raise
            result = lifecycle.handle(exc, wait)
            if result is None:
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
        from ..observability import append_live_event

        client = acquired.client
        wait = hop.setdefault("wait", {})
        receipt = SendReceipt.from_dict(hop["receipt"]) if hop.get("receipt") else None
        dom_only = worker._dom_only_enabled()
        install = getattr(client, "install_page_observer", None)
        if callable(install):
            await install()
        if snapshot is None:
            wait_for_signal = getattr(client, "wait_for_page_observation", None)
            if wait_ms > 0 and callable(wait_for_signal):
                await wait_for_signal(timeout_ms=wait_ms, dom_only=dom_only)
                wait_ms = 0
            if receipt is not None:
                snapshot = await client.wait_snapshot(receipt, probe_wait_ms=wait_ms)
            else:
                snapshot = await client.assert_ownership()
        read = getattr(client, "page_observation", None)
        evidence = read() if not dom_only and callable(read) else {}
        evidence = evidence if isinstance(evidence, Mapping) else {}
        permission = evidence.get("permission_action")
        permission = permission if isinstance(permission, Mapping) else None
        # Live network final messages may precede DOM rendering. History/G2 never
        # supplies this slot, and missing network evidence never gates DOM.
        network_response = evidence.get("response")
        if phase == "waiting" and network_response and not response.current_assistant(snapshot.messages, getattr(receipt, "baseline", None)):
            from dataclasses import replace
            candidate = MessageSnapshot.from_dict(network_response)
            snapshot = replace(snapshot, messages=(*snapshot.messages, candidate))
        previous = (wait.get("controller_decision"), wait.get("controller_reason"))
        decision = self.decide(
            snapshot, wait, receipt=receipt, permission=permission,
            validate=(lambda candidate: worker._validate_response_candidate(state, hop, candidate)),
            phase=phase, dom_only=dom_only, network_status=evidence.get("status"),
        )
        if previous != (decision.action.value, decision.reason):
            append_live_event(
                "CTRL", "role_decision", page_url=str(snapshot.url), page_id=acquired.page_id,
                role=str(hop.get("physical_role") or hop.get("target_role")),
                task_id=str(state["task_id"]), team=str(state["team"]),
                request_id=str(hop.get("request_id") or ""),
                values={"action": decision.action.value, "reason": decision.reason,
                        "mcp_permission_node_count": int(getattr(snapshot, "mcp_permission_node_count", 0)),
                        "retry_visible": bool(getattr(snapshot, "retry_visible", False))},
            )
        state["active_action"] = "wait_response" if phase == "waiting" else "queued"
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
        if decision.action is Action.REPAIR:
            worker._queue_format_repair(state, hop, decision.response, RouteContractError(decision.reason))
            return decision
        if decision.action is Action.ACCEPT:
            if receipt is not None:
                worker._capture_passive_remote_report_if_available(state, hop, acquired, receipt, decision.response)
            worker._record_response(state, hop, decision.response)
            if receipt is not None:
                worker._release_stream_status_slot(client, receipt)
            return decision
        raise AssertionError(f"unhandled operational action: {decision.action}")

    async def maintain_history(self, client, wait: dict, *, dom_only: bool) -> Decision:
        # History tabs only auto-Allow; they cannot enqueue work or route old results.
        probe = await client.read_wait_probe()
        permission = None if dom_only else client.page_observation().get("permission_action")
        decision = self.decide(probe, wait, permission=permission, phase="history", dom_only=dom_only)
        if decision.action is Action.ALLOW:
            return await self.browser_action(client, probe, wait, permission, decision)
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
                wait["mcp_allow_retry_at"] = (now + timedelta(seconds=5)).isoformat()
                return Decision(Action.WAIT, "mcp_allow_handler_pending")
            for key in ("mcp_allow_seen_at", "mcp_allow_seen_target", "mcp_allow_retry_at", "mcp_allow_dispatch_error"):
                wait.pop(key, None)
            signature, length = allow.activity(snapshot)
            wait.update(mcp_allow_clicked_at=now.isoformat(), mcp_allow_activity_signature=signature,
                        mcp_allow_activity_length=length, controller_progress_at=now.isoformat())
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
            wait["controller_progress_at"] = now.isoformat()
            wait["controller_last_refresh_at"] = now.isoformat()
            for key in ("mcp_allow_clicked_at", "mcp_allow_seen_at", "mcp_allow_retry_at"):
                wait.pop(key, None)
            return decision
        raise ValueError(f"not a browser action: {decision.action}")
