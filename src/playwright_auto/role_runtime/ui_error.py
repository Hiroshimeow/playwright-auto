"""A generation/UI failure is recoverable; do not replay the original task."""
from __future__ import annotations

import hashlib

from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    errors = tuple(getattr(ctx.snapshot, "error_texts", ()) or ())
    if not errors or ctx.active:
        return None
    key = hashlib.sha256("|".join(errors).encode()).hexdigest()
    if ctx.wait.get("ui_error_refreshed_key") != key:
        return Decision(Action.REFRESH, "ui_error")
    if ctx.composer_ready and ctx.phase != "pre_send":
        return Decision(Action.REPAIR, "UI generation error; continue from this state")
    return Decision(Action.WAIT, "ui_error_waiting_for_composer")
