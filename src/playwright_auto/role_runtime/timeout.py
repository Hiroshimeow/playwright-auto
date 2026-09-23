"""Single unchanged-state timeout recovery path."""
from ..cdpa_response import parse_time
from .model import Action, Context, Decision


_ACTIVE_STREAM_STATUSES = {"IS_STREAMING", "IS_STOP_REQUESTED"}


def observe(ctx: Context) -> None:
    if ctx.permission_present or ctx.wait.get("mcp_allow_clicked_at"):
        state = "ALLOW"
    elif ctx.transport_active:
        state = "STOP"
    elif getattr(ctx.snapshot, "retry_visible", False):
        state = "RETRY"
    elif ctx.candidate is not None:
        state = "RESPONSE"
    else:
        state = "IDLE"
    if ctx.wait.get("controller_state") != state:
        ctx.wait["controller_state"] = state
        ctx.wait["controller_state_since"] = ctx.now.isoformat()
        if not ctx.wait.get("timeout_refreshed"):
            ctx.wait["controller_progress_at"] = ctx.now.isoformat()
            for key in ("timeout_status_checked", "timeout_status"):
                ctx.wait.pop(key, None)
    ctx.wait.setdefault("controller_progress_at", ctx.now.isoformat())


def _rearm_wait(ctx: Context) -> Decision:
    ctx.wait["controller_progress_at"] = ctx.now.isoformat()
    for key in ("timeout_refreshed", "timeout_status_checked", "timeout_status"):
        ctx.wait.pop(key, None)
    return Decision(Action.WAIT, "response_still_active")


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    since = parse_time(ctx.wait.get("controller_progress_at")) or ctx.now
    if (ctx.now - since).total_seconds() < ctx.policy.timeout_seconds + 5.0:
        return None
    if not ctx.wait.get("timeout_refreshed"):
        return Decision(Action.REFRESH, "response_timeout_recheck")
    if not ctx.wait.get("timeout_status_checked"):
        return Decision(Action.STATUS, "response_timeout_diagnostic")

    # Once the explicit post-reload diagnostic has run, do not fall back to
    # PageObserver's older cached network status.  A missing diagnostic is UNKNOWN,
    # not proof that an old IS_STREAMING marker is still live.
    status = str(ctx.wait.get("timeout_status") or "").upper()

    # After the timeout reload/status diagnostic, only authoritative transport
    # activity keeps waiting.  DOM response_activity_* is audit/progress evidence
    # and must never keep an old turn alive.
    if bool(getattr(ctx.snapshot, "stop_visible", False)):
        return _rearm_wait(ctx)
    if status in _ACTIVE_STREAM_STATUSES:
        return _rearm_wait(ctx)

    # No visible Stop, no MCP permission (handled earlier by the controller), and
    # no valid stable response. COMPLETE/empty means continue from the existing
    # work with a repair prompt. FAILURE is also continuation recovery: never
    # click ChatGPT Retry, which would rewind the prior work.
    if status == "FAILURE":
        return Decision(Action.REPAIR, "stream_failure_recovery")
    if status in {"", "COMPLETE", "COMPLETED"}:
        return Decision(Action.REPAIR, "state_repair")

    # Backend stream_status is schema-validated, so this is only a bounded
    # defensive fallback for an unexpected/unavailable status.
    return Decision(Action.REPAIR, "state_repair")
