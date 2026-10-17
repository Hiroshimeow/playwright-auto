"""Ten-minute recovery path after neither LISTEN nor DOM produced a valid response."""
from datetime import timedelta

from ..cdpa_response import parse_time
from .model import Action, Context, Decision


_ACTIVE_STREAM_STATUSES = {"IS_STREAMING", "IS_STOP_REQUESTED"}


def observe(ctx: Context) -> None:
    """Record diagnostics without extending the fixed ten-minute recovery budget."""
    if ctx.permission_present:
        state = "ALLOW"
    elif getattr(ctx.snapshot, "retry_visible", False):
        state = "RETRY"
    elif ctx.candidate is not None:
        state = "RESPONSE"
    else:
        state = "IDLE"
    ctx.wait["controller_state"] = state
    started = parse_time(ctx.wait.get("started_at")) or ctx.now
    ctx.wait.setdefault("controller_progress_at", started.isoformat())


def _deadline(ctx: Context):
    started = parse_time(ctx.wait.get("controller_progress_at")) or ctx.now
    return started + timedelta(seconds=ctx.policy.timeout_seconds)


def _rearm_wait(ctx: Context) -> Decision:
    ctx.wait["controller_progress_at"] = ctx.now.isoformat()
    for key in ("timeout_refreshed", "timeout_status_checked", "timeout_status"):
        ctx.wait.pop(key, None)
    return Decision(Action.WAIT, "response_still_active")


def recovery(ctx: Context) -> Decision | None:
    """Only an explicit Stop attempt can trigger early continuation recovery."""
    if ctx.phase != "waiting":
        return None
    stop_visible = bool(getattr(ctx.snapshot, "stop_visible", False))
    ctx.wait["last_stop_visible"] = stop_visible
    if ctx.permission_present:
        return None

    requested_at = parse_time(ctx.wait.get("stop_requested_at"))
    if requested_at is None:
        return None
    if stop_visible or not ctx.composer_ready:
        return None
    if (ctx.now - requested_at).total_seconds() < ctx.policy.response_stable_seconds:
        return None
    return Decision(Action.REPAIR, "stopped_response_continue")


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    if ctx.now < _deadline(ctx):
        return None

    refresh_count = int(ctx.wait.get("refresh_count") or 0)
    if not ctx.wait.get("timeout_refreshed"):
        if refresh_count < 2:
            return Decision(Action.REFRESH, "response_timeout_recheck")
        if bool(getattr(ctx.snapshot, "stop_visible", False)):
            return Decision(Action.REPAIR, "response_timeout_stop")
        if str(ctx.network_status or "").upper() in _ACTIVE_STREAM_STATUSES:
            return _rearm_wait(ctx)

    # After each of the two F5 cycles, visible Stop or a live stream means the
    # accepted execution is still running. Rearm the same ten-minute gate.
    if bool(getattr(ctx.snapshot, "stop_visible", False)):
        return _rearm_wait(ctx)
    if str(ctx.network_status or "").upper() in _ACTIVE_STREAM_STATUSES:
        return _rearm_wait(ctx)

    if not ctx.wait.get("timeout_status_checked"):
        return Decision(Action.STATUS, "response_timeout_diagnostic")

    status = str(ctx.wait.get("timeout_status") or "").upper()
    if status in _ACTIVE_STREAM_STATUSES:
        return _rearm_wait(ctx)
    if status == "FAILURE":
        return Decision(Action.REPAIR, "stream_failure_recovery")
    return Decision(Action.REPAIR, "state_repair")
