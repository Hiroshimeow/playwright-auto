"""One refresh of stable malformed output, then guide-only continuation."""
from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    if ctx.candidate is None or not ctx.candidate_stable or ctx.validation_error is None:
        return None
    if ctx.active:
        return None
    if ctx.wait.get("invalid_refreshed_key") != ctx.candidate_key:
        return Decision(Action.REFRESH, "malformed_result")
    if ctx.composer_ready:
        return Decision(Action.REPAIR, ctx.validation_error, ctx.candidate)
    return Decision(Action.WAIT, "repair_composer_pending")
