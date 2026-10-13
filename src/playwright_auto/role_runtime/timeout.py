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
    """A notice or inactive edge is evidence, never content progress or replay authority."""
    if ctx.phase != "waiting":
        return None
    active = bool(getattr(ctx.snapshot, "stop_visible", False)) or bool(
        getattr(ctx.snapshot, "response_projection_active", False)
    )
    previous_active = ctx.wait.get("last_stop_visible")
    ctx.wait["last_stop_visible"] = active
    if active:
        ctx.wait.pop("inactive_since", None)
    elif previous_active:
        ctx.wait.setdefault("inactive_since", ctx.now.isoformat())
    inactive_since = parse_time(ctx.wait.get("inactive_since"))
    progress = parse_time(ctx.wait.get("controller_progress_at"))
    inactive_anchor = max(inactive_since, progress) if inactive_since and progress else inactive_since
    if inactive_anchor and (ctx.now - inactive_anchor).total_seconds() >= ctx.policy.response_stable_seconds:
        return Decision(Action.REPAIR, "inactive_response")

    notice = " ".join((
        str(getattr(ctx.snapshot, "response_activity_text", "") or ""),
        *tuple(getattr(ctx.snapshot, "error_texts", ()) or ()),
    )).casefold()
    progress_at = parse_time(ctx.wait.get("controller_progress_at")) or parse_time(ctx.wait.get("started_at"))
    if progress_at is None:
        progress_at = ctx.now
        ctx.wait["controller_progress_at"] = progress_at.isoformat()
    if ("connection interrupted" in notice or "waiting for the complete answer" in notice) and (
        ctx.now - progress_at
    ).total_seconds() >= min(ctx.policy.timeout_seconds, 300):
        return Decision(Action.REPAIR, "interrupted_response")
    return None


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
