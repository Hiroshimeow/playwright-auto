"""Ten minutes without an operational state transition triggers F5, never Send."""
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
        ctx.wait["controller_progress_at"] = ctx.now.isoformat()
    ctx.wait.setdefault("controller_progress_at", ctx.now.isoformat())


def handle(ctx: Context) -> Decision | None:
    since = parse_time(ctx.wait.get("controller_progress_at")) or ctx.now
    if (ctx.now - since).total_seconds() >= ctx.policy.stalled_seconds:
        return Decision(Action.REFRESH, "state_stalled")
    return None
