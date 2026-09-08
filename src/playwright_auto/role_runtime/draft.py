"""Manual input gates destructive actions, never read-only result observation."""
from .model import Action, Context, Decision


def protect(ctx: Context, decision: Decision) -> Decision:
    if ctx.draft_present and decision.action in {Action.REFRESH, Action.REPAIR}:
        return Decision(Action.WAIT, "manual_draft_preserved")
    return decision
