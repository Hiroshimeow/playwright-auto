"""Reuse the existing profile cooldown; no independent polling or retry loop."""
from .model import Action, Context, Decision


MARKERS = ("too many requests", "rate limit", "rate-limit", "request limit")


def handle(ctx: Context) -> Decision | None:
    texts = tuple(getattr(ctx.snapshot, "blocking_dialogs", ()) or ()) + tuple(
        getattr(ctx.snapshot, "error_texts", ()) or ()
    )
    if any(marker in str(text).lower() for text in texts for marker in MARKERS):
        return Decision(Action.COOLDOWN, "profile_rate_limit")
    return None
