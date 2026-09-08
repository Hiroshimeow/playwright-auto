"""Transient browser lifecycle interruptions wait/recover without task replay."""
from datetime import datetime, timedelta, timezone

from .model import Action, Decision


def handle(error: BaseException, wait: dict) -> Decision | None:
    from ..cdpa_actions import is_transient_page_lifecycle_error
    from ..chatgpt import AuthenticationRequiredError, ComposerConflictError
    if isinstance(error, AuthenticationRequiredError):
        return Decision(Action.BLOCK, "authentication_required")
    if isinstance(error, ComposerConflictError):
        return Decision(Action.WAIT, "manual_draft_preserved")
    if isinstance(error, TimeoutError) or is_transient_page_lifecycle_error(error):
        wait["observation_retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        wait["observation_error"] = type(error).__name__
        return Decision(Action.WAIT, "page_lifecycle_recovery")
    return None
