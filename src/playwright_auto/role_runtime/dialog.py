"""Only real blocking dialogs stop the role; permission cards have their own handler."""
from .model import Action, Context, Decision


def handle(ctx: Context) -> Decision | None:
    dialogs = tuple(getattr(ctx.snapshot, "blocking_dialogs", ()) or ())
    if dialogs and not ctx.permission_present:
        return Decision(Action.BLOCK, "blocking_dialog")
    if getattr(ctx.snapshot, "choice_prompt_labels", ()) and not ctx.permission_present:
        return Decision(Action.BLOCK, "choice_prompt_blocked")
    return None
