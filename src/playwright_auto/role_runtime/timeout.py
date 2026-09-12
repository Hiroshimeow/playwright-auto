"""Single unchanged-state timeout recovery path."""
from ..cdpa_response import parse_time
from .model import Action, Context, Decision


def observe(ctx: Context) -> None:
    if ctx.permission_present or ctx.wait.get("mcp_allow_clicked_at"):
        state = "ALLOW"
    elif ctx.active:
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


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    since = parse_time(ctx.wait.get("controller_progress_at")) or ctx.now
    if (ctx.now - since).total_seconds() < ctx.policy.timeout_seconds + 5.0:
        return None
    if not ctx.wait.get("timeout_refreshed"):
        return Decision(Action.REFRESH, "response_timeout_recheck")
    if not ctx.dom_only and not ctx.wait.get("timeout_status_checked"):
        return Decision(Action.STATUS, "response_timeout_diagnostic")
    status = str(ctx.wait.get("timeout_status") or ctx.network_status or "").upper()
    if not ctx.dom_only and status in {"", "COMPLETE", "COMPLETED"}:
        return Decision(Action.REPAIR, "state_repair")
    if not ctx.active:
        return Decision(Action.REPAIR, "state_repair")
    ctx.wait["controller_progress_at"] = ctx.now.isoformat()
    for key in ("timeout_refreshed", "timeout_status_checked", "timeout_status"):
        ctx.wait.pop(key, None)
    return Decision(Action.WAIT, "response_still_active")
