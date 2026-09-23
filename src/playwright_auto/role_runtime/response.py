"""Current result selection. Original accepted-user identity is not an admission rule."""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Sequence

from ..cdpa_response import parse_time
from .model import Action, Context, Decision


def current_assistant(messages: Sequence[Any], baseline: Any = None) -> Any | None:
    """Use the newest non-empty assistant after the newest rendered user.

    ChatGPT can leave an empty assistant placeholder after a completed answer. That
    placeholder is UI scaffolding, not a newer response and must not hide the route.
    """
    last_user = -1
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index
    candidates = [message for message in messages[last_user + 1:] if message.role == "assistant"]
    latest = next(
        (
            message
            for message in reversed(candidates)
            if str(message.text or "").strip() or getattr(message, "image_count", 0)
        ),
        None,
    )
    if latest is None:
        return None
    if baseline is not None:
        old_ids = baseline.message_ids
        old_users = baseline.user_message_ids
        latest_user_is_old = last_user < 0 or messages[last_user].message_id in old_users
        if latest_user_is_old and latest.message_id in old_ids:
            return None
    return latest


def fingerprint(message: Any) -> str:
    if message is None:
        return ""
    return hashlib.sha256(str(message.text or "").encode("utf-8")).hexdigest()


def observe(
    ctx: Context,
    baseline: Any,
    validate: Callable[[Any], None] | None,
    *,
    terminal_message_id: str | None = None,
) -> None:
    ctx.candidate = current_assistant(tuple(getattr(ctx.snapshot, "messages", ())), baseline)
    key = fingerprint(ctx.candidate)
    ctx.candidate_key = key
    if not key:
        ctx.wait.pop("result_seen_key", None)
        ctx.wait.pop("result_seen_at", None)
        ctx.wait.pop("result_samples", None)
        return
    if ctx.wait.get("result_seen_key") != key:
        if getattr(ctx, "phase", "wait") != "pre_send":
            ctx.wait["last_assistant_progress"] = {
                "text": str(ctx.candidate.text or "")[:8000],
                "message_id": getattr(ctx.candidate, "message_id", None),
                "turn_id": getattr(ctx.candidate, "turn_id", None),
                "observed_at": ctx.now.isoformat(),
            }
        ctx.wait["result_seen_key"] = key
        ctx.wait["result_seen_at"] = ctx.now.isoformat()
        ctx.wait["result_samples"] = 1
    else:
        ctx.wait["result_samples"] = int(ctx.wait.get("result_samples") or 0) + 1
    if validate is not None:
        try:
            validate(ctx.candidate)
        except (ValueError, OSError) as exc:
            ctx.validation_error = str(exc)
    terminal_match = bool(
        terminal_message_id
        and getattr(ctx.candidate, "message_id", None) == terminal_message_id
    )
    # Both LISTEN and DOM candidates must remain unchanged for the same five-second
    # stability window before they can be accepted or classified malformed.  This
    # protects against a partially rendered DOM result and against a terminal
    # transport frame whose visible/rendered state is still settling.
    seen_at = parse_time(ctx.wait.get("result_seen_at")) or ctx.now
    stable_for = max(0.0, (ctx.now - seen_at).total_seconds())
    ctx.candidate_stable = stable_for >= ctx.policy.response_stable_seconds
    if terminal_match:
        ctx.wait["terminal_result_seen"] = True


def handle(ctx: Context) -> Decision | None:
    if ctx.candidate is not None and ctx.candidate_stable and ctx.validation_error is None:
        # A valid route wins after the shared five-second stability window.  This
        # keeps LISTEN and DOM on the same completion contract.
        return Decision(Action.ACCEPT, "valid_current_result", ctx.candidate)
    return None
