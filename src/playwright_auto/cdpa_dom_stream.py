from __future__ import annotations

from typing import Any

from .chatgpt_models import ChatGPTSnapshot, MessageSnapshot

ACTIVITY_SELECTORS = (
    ".group/activity-header",
    '[class*="agent-activity"]',
    '[class*="activity-header"]',
    ".summary-lK7Lpm",
    ".cadencedShimmerSweep-ICUAVH",
    ".cadencedShimmerHighlight-VcX29h",
)


def _node(kind: str, key: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "text": str(text or ""),
        **{name: value for name, value in extra.items() if value not in {None, ""}},
    }


def _message_node(message: MessageSnapshot, index: int) -> dict[str, Any] | None:
    if message.role == "user":
        kind = "user_message"
    elif message.role == "assistant":
        kind = "assistant_text"
    else:
        return None
    identity = str(message.message_id or message.turn_id or index)
    return _node(
        kind,
        f"{kind}:{identity}",
        message.text,
        message_id=message.message_id,
        turn_id=message.turn_id,
    )


def project_dom_snapshot(snapshot: ChatGPTSnapshot) -> dict[str, Any]:
    """Serialize one authoritative ChatGPTSnapshot without reading the page again."""
    nodes: list[dict[str, Any]] = []
    for index, message in enumerate(snapshot.messages):
        projected = _message_node(message, index)
        if projected is not None:
            nodes.append(projected)

    activity = str(snapshot.response_activity_text or "").strip()
    if activity:
        structure = str(snapshot.response_activity_structure or "").lower()
        if any(token in structure for token in ("tool", "agent-activity", "activity-header")):
            kind = "tool_activity"
        elif any(token in structure for token in ("think", "shimmer", "summary-")):
            kind = "visible_thinking"
        else:
            kind = "progress"
        nodes.append(
            _node(
                kind,
                f"{kind}:{snapshot.response_activity_turn_id or 'active'}",
                activity,
                turn_id=snapshot.response_activity_turn_id,
            )
        )

    for index, error in enumerate(snapshot.error_texts):
        if str(error or "").strip():
            nodes.append(_node("error_banner", f"error:{index}", str(error)))

    last_assistant = next(
        (message for message in reversed(snapshot.messages) if message.role == "assistant"),
        None,
    )
    active = bool(
        snapshot.stop_visible
        or snapshot.response_projection_active
        or snapshot.response_activity_turn_id
    )
    if last_assistant is not None and not active and str(last_assistant.text or "").strip():
        identity = str(last_assistant.message_id or last_assistant.turn_id or "assistant")
        nodes.append(
            _node(
                "terminal",
                f"terminal:{identity}",
                last_assistant.text,
                message_id=last_assistant.message_id,
                turn_id=last_assistant.turn_id,
            )
        )

    return {
        "page_id": snapshot.page_id,
        "page_role": snapshot.page_role,
        "url": snapshot.url,
        "active": active,
        "nodes": nodes,
        "activity_selectors": list(ACTIVITY_SELECTORS),
    }
