"""Authentication is an external prerequisite, not a generation retry."""
from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    dialogs = " ".join(getattr(ctx.snapshot, "blocking_dialogs", ()) or ()).lower()
    if getattr(ctx.snapshot, "requires_login", False) or "session has expired" in dialogs:
        return Decision(Action.BLOCK, "authentication_required")
    return None
