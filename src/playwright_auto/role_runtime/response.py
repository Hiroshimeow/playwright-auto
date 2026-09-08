"""Current result selection. Original accepted-user identity is not an admission rule."""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Sequence

from ..cdpa_response import parse_time
from .model import Action, Context, Decision


def current_assistant(messages: Sequence[Any], baseline: Any = None) -> Any | None:
    """Use the newest assistant after the newest rendered user, including operator turns.

    A pre-send historical message is not new work. That tiny baseline check is not
    a requirement to find the original user, nor a blacklist of pre-refresh output.
    """
    last_user = -1
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index
    candidates = [message for message in messages[last_user + 1:] if message.role == "assistant"]
    if not candidates:
        return None
    latest = candidates[-1]
    if baseline is not None:
        old_ids = baseline.message_ids
        old_users = baseline.user_message_ids
        latest_user_is_old = last_user < 0 or messages[last_user].message_id in old_users
        if latest_user_is_old and latest.message_id in old_ids:
            return None
    return latest if str(latest.text or "").strip() or getattr(latest, "image_count", 0) else None


def fingerprint(message: Any) -> str:
    if message is None:
        return ""
    return hashlib.sha256(str(message.text or "").encode("utf-8")).hexdigest()


def observe(ctx: Context, baseline: Any, validate: Callable[[Any], None] | None) -> None:
    ctx.candidate = current_assistant(tuple(getattr(ctx.snapshot, "messages", ())), baseline)
    key = fingerprint(ctx.candidate)
    ctx.candidate_key = key
    if not key:
        ctx.wait.pop("result_seen_key", None)
        ctx.wait.pop("result_seen_at", None)
        ctx.wait.pop("result_samples", None)
        return
    if ctx.wait.get("result_seen_key") != key:
        ctx.wait["result_seen_key"] = key
        ctx.wait["result_seen_at"] = ctx.now.isoformat()
        ctx.wait["result_samples"] = 1
    else:
        ctx.wait["result_samples"] = int(ctx.wait.get("result_samples") or 0) + 1
    seen = parse_time(ctx.wait.get("result_seen_at")) or ctx.now
    ctx.candidate_stable = (
        (ctx.now - seen).total_seconds() >= ctx.policy.response_stable_seconds
        and int(ctx.wait["result_samples"]) >= ctx.policy.minimum_samples
    )
    if validate is not None:
        try:
            validate(ctx.candidate)
        except (ValueError, OSError) as exc:
            ctx.validation_error = str(exc)


def handle(ctx: Context) -> Decision | None:
    if ctx.candidate is not None and ctx.candidate_stable and ctx.validation_error is None:
        # A valid, stable route wins over a stuck Stop indicator. The response/report
        # contract, not the continued presence of UI decoration, defines the result.
        return Decision(Action.ACCEPT, "stable_current_result", ctx.candidate)
    return None
