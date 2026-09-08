"""MCP permission interruption, with intentional five-second pre/post windows."""
from __future__ import annotations

import hashlib
from typing import Any

from ..cdpa_response import parse_time
from .model import Action, Context, Decision
from .response import current_assistant, fingerprint


def activity(snapshot: Any) -> tuple[str, int]:
    text = str(getattr(snapshot, "response_activity_text", "") or getattr(snapshot, "response_activity_tail", "") or "")
    length = max(len(text), int(getattr(snapshot, "response_activity_length", 0) or 0))
    candidate = current_assistant(tuple(getattr(snapshot, "messages", ())))
    result_key = fingerprint(candidate) or str(getattr(snapshot, "last_assistant_message_id", "") or "")
    signature = hashlib.sha256((text[-160:] + "|" + result_key).encode()).hexdigest()
    return signature, length


def handle(ctx: Context) -> Decision | None:
    wait = ctx.wait
    clicked = parse_time(wait.get("mcp_allow_clicked_at"))
    if clicked is not None:
        signature, length = activity(ctx.snapshot)
        previous = wait.get("mcp_allow_activity_signature")
        progressed = ctx.active or length > int(wait.get("mcp_allow_activity_length") or 0)
        progressed = progressed or bool(previous and signature != previous)
        if progressed:
            for key in ("mcp_allow_clicked_at", "mcp_allow_post_click_refreshed",
                        "mcp_allow_activity_signature", "mcp_allow_activity_length"):
                wait.pop(key, None)
            wait["controller_progress_at"] = ctx.now.isoformat()
            return None
        if (ctx.now - clicked).total_seconds() < ctx.policy.post_allow_seconds:
            return Decision(Action.WAIT, "mcp_allow_continuation")
        # Disappearance alone is NOT progress: a hidden permission can be approved
        # while the UI remains stuck. Refresh instead of resending the task.
        return Decision(Action.REFRESH, "mcp_allow_no_ui_progress")

    if not ctx.permission_present:
        wait.pop("mcp_allow_seen_at", None)
        wait.pop("mcp_allow_seen_target", None)
        return None
    target = str((ctx.permission or {}).get("target_message_id") or "")
    seen = parse_time(wait.get("mcp_allow_seen_at"))
    if seen is None or (target and target != wait.get("mcp_allow_seen_target")):
        wait["mcp_allow_seen_at"] = ctx.now.isoformat()
        wait["mcp_allow_seen_target"] = target
        return Decision(Action.WAIT, "mcp_allow_stable")
    if (ctx.now - seen).total_seconds() < ctx.policy.allow_stable_seconds:
        return Decision(Action.WAIT, "mcp_allow_stable")
    retry_at = parse_time(wait.get("mcp_allow_retry_at"))
    if retry_at is not None and ctx.now < retry_at:
        return Decision(Action.WAIT, "mcp_allow_handler_pending")
    return Decision(Action.ALLOW, "mcp_allow_ready")
