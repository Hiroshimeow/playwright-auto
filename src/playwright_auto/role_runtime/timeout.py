"""Bounded DOM-only fallback and interrupted-stream observation."""
from datetime import timedelta
import hashlib

from ..cdpa_response import parse_time
from .model import Action, Context, Decision


def observe(ctx: Context) -> None:
    if ctx.permission_present:
        state = "ALLOW"
    elif getattr(ctx.snapshot, "retry_visible", False):
        state = "RETRY"
    elif ctx.candidate is not None:
        state = "RESPONSE"
    else:
        state = "IDLE"
    ctx.wait["controller_state"] = state
    started = parse_time(ctx.wait.get("started_at")) or ctx.now
    ctx.wait.setdefault("controller_progress_at", started.isoformat())
    if ctx.transport_active:
        # A stuck Stop is not proof of progress. Only changed fresh DOM content
        # may reset this longer, one-owner inactivity budget.
        activity = (
            str(getattr(ctx.snapshot, "response_activity_length", 0) or 0)
            + ":" + str(getattr(ctx.snapshot, "response_activity_tail", "") or "")
            + ":" + str(ctx.candidate_key or "")
        )
        signature = hashlib.sha256(activity.encode("utf-8")).hexdigest()
        if ctx.wait.get("active_stall_signature") != signature:
            ctx.wait["active_stall_signature"] = signature
            ctx.wait["active_stall_since"] = ctx.now.isoformat()
    else:
        ctx.wait.pop("active_stall_signature", None)
        ctx.wait.pop("active_stall_since", None)


def handle(ctx: Context) -> Decision | None:
    if ctx.phase != "waiting":
        return None
    started = parse_time(ctx.wait.get("controller_progress_at")) or ctx.now
    if ctx.now < started + timedelta(seconds=ctx.policy.timeout_seconds):
        return None
    if ctx.transport_active:
        # DOM may still display Stop after generation has stopped. Bound waiting
        # without inventing Retry/refresh and without trusting LISTEN.
        last_changed = parse_time(ctx.wait.get("active_stall_since")) or ctx.now
        if (ctx.now - last_changed).total_seconds() > 3 * ctx.policy.timeout_seconds:
            return Decision(Action.BLOCK, "response_stuck_active")
        ctx.wait["controller_progress_at"] = ctx.now.isoformat()
        return Decision(Action.WAIT, "response_still_active")
    # Missing output is not malformed output. Never invent a repair candidate,
    # reload a Temporary document, or replay an accepted Send.
    return Decision(Action.BLOCK, "response_terminal_missing")


def observe_connection_interruption(ctx: Context) -> None:
    """Record UI stall evidence on the existing hop; do not infer server completion."""
    if (ctx.phase != "waiting" or not getattr(ctx.snapshot, "connection_interrupted_visible", False)
            or getattr(ctx.snapshot, "retry_visible", False)):
        ctx.wait.pop("connection_interruption", None)
        return
    snap = ctx.snapshot
    # Hash bounded assistant evidence, not raw user/tool text. A different
    # accepted turn or page resets the same observation window.
    activity = "|".join((
        str(getattr(snap, "page_id", "") or ""),
        str(getattr(snap, "last_user_turn_id", "") or ""),
        str(ctx.candidate_key or ""),
        str(getattr(snap, "response_activity_length", 0) or 0),
        str(getattr(snap, "response_activity_tail", "") or getattr(snap, "response_activity_text", "") or "")[-160:],
        str(getattr(snap, "assistant_text_length", 0) or 0),
        str(getattr(snap, "assistant_text_tail", "") or "")[-160:],
    ))
    signature = hashlib.sha256(activity.encode("utf-8")).hexdigest()
    identity = hashlib.sha256("|".join((
        str(getattr(snap, "page_id", "") or ""),
        str(getattr(snap, "url", "") or ""),
        str(getattr(snap, "last_user_turn_id", "") or ""),
    )).encode("utf-8")).hexdigest()
    record = ctx.wait.get("connection_interruption")
    if not isinstance(record, dict) or record.get("identity") != identity:
        record = {"identity": identity, "since": ctx.now.isoformat(), "progress_at": ctx.now.isoformat()}
    if record.get("progress_hash") != signature:
        record["progress_hash"] = signature
        record["progress_at"] = ctx.now.isoformat()
    ctx.wait["connection_interruption"] = record


def connection_interruption_decision(ctx: Context) -> Decision | None:
    """Flag uncertain UI after 10 stagnant minutes; never Stop or resend."""
    record = ctx.wait.get("connection_interruption")
    if ctx.phase != "waiting" or not isinstance(record, dict):
        return None
    last_progress = parse_time(record.get("progress_at")) or ctx.now
    if (ctx.now - last_progress).total_seconds() >= ctx.policy.connection_interruption_window_seconds:
        return Decision(Action.BLOCK, "connection_interrupted_unconfirmed")
    return Decision(Action.WAIT, "connection_interrupted_observing")
