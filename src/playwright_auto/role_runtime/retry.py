"""Retry UI requests a continuation, never a Regenerate/Retry button click."""
from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    if not getattr(ctx.snapshot, "retry_visible", False):
        return None
    if ctx.phase == "pre_send":
        # A queued FORMAT-REPAIR is allowed to use the same editable composer.
        return None
    if ctx.composer_ready:
        return Decision(Action.REPAIR, "ChatGPT Retry UI is visible; continue from the existing state")
    return Decision(Action.WAIT, "retry_composer_pending")
