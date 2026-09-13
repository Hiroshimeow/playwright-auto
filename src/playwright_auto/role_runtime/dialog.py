"""Only real blocking dialogs stop the role; permission cards have their own handler."""
from urllib.parse import parse_qs, urlsplit

from .model import Action, Context, Decision

_TEMPORARY_SAVE_OFFER = "Want to keep this chat? You can now save it to your history."


def handle(ctx: Context) -> Decision | None:
    dialogs = tuple(getattr(ctx.snapshot, "blocking_dialogs", ()) or ())
    if ctx.phase == "waiting" and parse_qs(urlsplit(str(getattr(ctx.snapshot, "url", ""))).query).get("temporary-chat") == ["true"]:
        # Live Temporary onboarding is an offer, not permission to save. Reading
        # an already accepted result is safe; pre-send and unknown dialogs still block.
        dialogs = tuple(value for value in dialogs if " ".join(value.split()) != _TEMPORARY_SAVE_OFFER)
    if dialogs and not ctx.permission_present:
        return Decision(Action.BLOCK, "blocking_dialog")
    if getattr(ctx.snapshot, "choice_prompt_labels", ()) and not ctx.permission_present:
        return Decision(Action.BLOCK, "choice_prompt_blocked")
    return None
