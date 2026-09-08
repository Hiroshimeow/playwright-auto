"""One deadline boundary; no competing terminal/settle/graph recovery modes."""
from ..cdpa_response import parse_time
from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    deadline = parse_time(ctx.wait.get("deadline_at"))
    if deadline is None or ctx.now < deadline:
        return None
    if not ctx.wait.get("timeout_refreshed"):
        return Decision(Action.REFRESH, "response_timeout_recheck")
    if not ctx.dom_only and not ctx.active and not ctx.wait.get("timeout_status_checked"):
        return Decision(Action.STATUS, "response_timeout_diagnostic")
    status = str(ctx.wait.get("timeout_status") or ctx.network_status or "").upper()
    if not ctx.active and status != "IS_STREAMING" and ctx.composer_ready:
        return Decision(Action.REPAIR, "response timeout; continue from this state and return the route JSON")
    return Decision(Action.BLOCK, "response_timeout")
