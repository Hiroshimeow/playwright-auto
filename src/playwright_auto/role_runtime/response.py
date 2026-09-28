"""Exact accepted-user to assistant binding for current response selection."""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Sequence

from ..cdpa_response import parse_time
from ..chatgpt_models import bound_assistant_for_receipt
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
            progress = {
                "source": source,
                "text": str(candidate.text or "")[:8000],
                "message_id": getattr(candidate, "message_id", None),
                "turn_id": getattr(candidate, "turn_id", None),
                "observed_at": ctx.now.isoformat(),
            }
            ctx.wait["last_assistant_progress"] = progress
            if source == "dom" and (
                (ctx.wait.get("bound_assistant_message_id")
                 and progress["message_id"] == ctx.wait.get("bound_assistant_message_id"))
                or (ctx.wait.get("bound_assistant_turn_id")
                    and progress["turn_id"] == ctx.wait.get("bound_assistant_turn_id"))
            ):
                ctx.wait["bound_assistant_progress"] = progress
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


def _bound_assistant(ctx: Context, receipt: Any) -> Any | None:
    messages = tuple(getattr(ctx.snapshot, "messages", ()) or ())
    if receipt is None:
        return current_assistant(messages)
    if not getattr(receipt, "user_message_id", None) and not getattr(receipt, "user_turn_id", None):
        return current_assistant(messages, getattr(receipt, "baseline", None))

    candidate, error = bound_assistant_for_receipt(
        messages,
        receipt,
        assistant_message_id=ctx.wait.get("bound_assistant_message_id"),
        assistant_turn_id=ctx.wait.get("bound_assistant_turn_id"),
    )
    if error is not None:
        ctx.wait["response_binding_error"] = error
        return None
    ctx.wait.pop("response_binding_error", None)
    if candidate is None:
        return None
    if not ctx.wait.get("bound_assistant_message_id") and not ctx.wait.get("bound_assistant_turn_id"):
        ctx.wait["bound_assistant_message_id"] = getattr(candidate, "message_id", None)
        ctx.wait["bound_assistant_turn_id"] = getattr(candidate, "turn_id", None)
    return candidate


def observe(
    ctx: Context,
    receipt: Any,
    validate: Callable[[Any], None] | None,
) -> None:
    observe_candidate(
        ctx,
        _bound_assistant(ctx, receipt),
        validate,
        source="dom",
    )


def handle(ctx: Context) -> Decision | None:
    candidate = ctx.candidate
    if candidate is None or not ctx.candidate_stable or ctx.validation_error is not None:
        return None
    if not str(getattr(candidate, "text", "") or "").strip() and not getattr(candidate, "image_count", 0):
        return None

    actions = tuple(getattr(candidate, "actions", ()) or ())
    completed_action = any(
        "copy" in str(action).lower()
        or "feedback" in str(action).lower()
        or "regenerate" in str(action).lower()
        for action in actions
    )
    bound_turn_id = str(ctx.wait.get("bound_assistant_turn_id") or "").strip()
    activity_turn_id = str(getattr(ctx.snapshot, "response_activity_turn_id", "") or "").strip()
    bound_generation_active = bool(
        bound_turn_id and activity_turn_id and bound_turn_id == activity_turn_id
    )
    resume_terminal = bool(ctx.wait.get("resume_allow_missing_completion_action"))
    if bound_generation_active:
        return None
    if not completed_action and not resume_terminal:
        return None
    if resume_terminal and (
        bool(getattr(ctx.snapshot, "response_projection_active", False))
        or bool(getattr(ctx.snapshot, "retry_visible", False))
        or bool(getattr(ctx.snapshot, "blocking_dialogs", ()))
        or bool(getattr(ctx.snapshot, "error_texts", ()))
    ):
        return None
    return Decision(Action.ACCEPT, "valid_bound_result", candidate)
