"""MCP permission interruption."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

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
    if not ctx.permission_present:
        wait.pop("mcp_allow_clicked_target", None)
        wait.pop("mcp_allow_clicked_at", None)
        return None

    target = str((ctx.permission or {}).get("target_message_id") or "__dom__")
    retry_at_raw = str(wait.get("mcp_allow_retry_at") or "").strip()
    if retry_at_raw:
        try:
            retry_at = datetime.fromisoformat(retry_at_raw)
        except ValueError:
            retry_at = None
        if retry_at is not None and ctx.now < retry_at:
            return Decision(Action.WAIT, "mcp_allow_retry_backoff")
        wait.pop("mcp_allow_retry_at", None)

    clicked_target = str(wait.get("mcp_allow_clicked_target") or "")
    if clicked_target == target:
        clicked_at_raw = str(wait.get("mcp_allow_clicked_at") or "").strip()
        try:
            clicked_at = datetime.fromisoformat(clicked_at_raw) if clicked_at_raw else None
        except ValueError:
            clicked_at = None
        if (
            clicked_at is not None
            and (ctx.now - clicked_at).total_seconds() < ctx.policy.post_allow_seconds
        ):
            return Decision(Action.WAIT, "mcp_allow_post_click")
        # Permission is still visibly present after the post-click window. A
        # successful click should have removed it, so retry instead of waiting
        # until the generic timeout. This also recovers durable wait state
        # written by the pre-hotfix false "clicked" path.
        wait.pop("mcp_allow_clicked_at", None)
        wait.pop("mcp_allow_clicked_target", None)
    return Decision(Action.ALLOW, "mcp_allow_ready")
