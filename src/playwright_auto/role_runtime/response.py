"""Current result selection. LISTEN is priority #1; rendered DOM is priority #2."""
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


def _keys(source: str) -> tuple[str, str, str]:
    prefix = "" if source == "dom" else f"{source}_"
    return (
        f"{prefix}result_seen_key",
        f"{prefix}result_seen_at",
        f"{prefix}result_samples",
    )


def observe_candidate(
    ctx: Context,
    candidate: Any,
    validate: Callable[[Any], None] | None,
    *,
    source: str,
) -> None:
    """Observe one source independently so LISTEN cannot poison DOM stability."""
    ctx.candidate = candidate
    ctx.validation_error = None
    ctx.candidate_stable = False
    key = fingerprint(candidate)
    ctx.candidate_key = key
    key_name, at_name, samples_name = _keys(source)

    if not key:
        ctx.wait.pop(key_name, None)
        ctx.wait.pop(at_name, None)
        ctx.wait.pop(samples_name, None)
        return

    if ctx.wait.get(key_name) != key:
        if getattr(ctx, "phase", "wait") != "pre_send":
            ctx.wait["last_assistant_progress"] = {
                "source": source,
                "text": str(candidate.text or "")[:8000],
                "message_id": getattr(candidate, "message_id", None),
                "turn_id": getattr(candidate, "turn_id", None),
                "observed_at": ctx.now.isoformat(),
            }
        ctx.wait[key_name] = key
        ctx.wait[at_name] = ctx.now.isoformat()
        ctx.wait[samples_name] = 1
    else:
        ctx.wait[samples_name] = int(ctx.wait.get(samples_name) or 0) + 1

    if validate is not None:
        try:
            validate(candidate)
        except (ValueError, OSError) as exc:
            ctx.validation_error = str(exc)

    seen_at = parse_time(ctx.wait.get(at_name)) or ctx.now
    stable_for = max(0.0, (ctx.now - seen_at).total_seconds())
    ctx.candidate_stable = stable_for >= ctx.policy.response_stable_seconds


def observe(
    ctx: Context,
    baseline: Any,
    validate: Callable[[Any], None] | None,
) -> None:
    observe_candidate(
        ctx,
        current_assistant(tuple(getattr(ctx.snapshot, "messages", ())), baseline),
        validate,
        source="dom",
    )


def handle(ctx: Context) -> Decision | None:
    if ctx.candidate is not None and ctx.candidate_stable and ctx.validation_error is None:
        return Decision(Action.ACCEPT, "valid_current_result", ctx.candidate)
    return None
