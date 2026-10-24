"""MCP permission interruption."""
from __future__ import annotations

import hashlib
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
        wait.pop("mcp_allow_pending_target", None)
        wait.pop("mcp_allow_pending_at", None)
        return None

    if (int(getattr(ctx.snapshot, "mcp_permission_allow_count", 0) or 0) > 1
            and not getattr(ctx.snapshot, "mcp_permission_surface_key", None)):
        return Decision(Action.BLOCK, "permission_surface_ambiguous")
    target = str(getattr(ctx.snapshot, "mcp_permission_surface_key", None) or "__dom__")
    # LISTEN target IDs are never authority for DOM approval identity.
    if str(wait.get("mcp_allow_pending_target") or "") == target:
        # The DOM still shows a permission whose previous dispatch produced no
        # click receipt. Do not repeatedly dispatch a potentially ambiguous action.
        return None
    clicked_target = str(wait.get("mcp_allow_clicked_target") or "")
    if clicked_target == target:
        # Same permission was already dispatched. Do not click-loop; ordinary
        # response timeout/recovery continues to run.
        return None
    return Decision(Action.ALLOW, "mcp_allow_ready")
