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


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    if ctx.now < _deadline(ctx):
        return None
    if not ctx.wait.get("timeout_refreshed"):
        return Decision(Action.REFRESH, "response_timeout_recheck")
    if not ctx.wait.get("timeout_status_checked"):
        return Decision(Action.STATUS, "response_timeout_diagnostic")

    # Only after the ten-minute reload/recheck do Stop and stream_status matter.
    status = str(ctx.wait.get("timeout_status") or "").upper()
    if bool(getattr(ctx.snapshot, "stop_visible", False)):
        return _rearm_wait(ctx)
    if status in _ACTIVE_STREAM_STATUSES:
        return _rearm_wait(ctx)
    if status == "FAILURE":
        return Decision(Action.REPAIR, "stream_failure_recovery")
    return Decision(Action.REPAIR, "state_repair")
