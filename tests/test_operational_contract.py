"""Operator contract regressions, using the actual production snapshot types."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from playwright_auto.chatgpt import (
    ChatGPTPage, ChatGPTSnapshot, ChatGPTState, MessageBaseline,
    MessageSnapshot, PageBinding, SendReceipt, WaitProbe, classify_chatgpt_state,
)


def snapshot(**changes):
    values = dict(
        url="https://chatgpt.com/c/current", session_id="current", page_id="page",
        page_role="team-dev", page_task_id="task", page_team="team",
        state=ChatGPTState.WAITING_PROMPT, requires_login=False, composer_present=True,
        composer_editable=True, composer_text="", send_visible=True, send_enabled=True,
        stop_visible=False, blocking_dialogs=(), attachment_markers=(), error_texts=(),
        messages=(),
    )
    values.update(changes)
    return ChatGPTSnapshot(**values)


def probe(**changes):
    values = dict(
        url="https://chatgpt.com/c/current", session_id="current", page_id="page",
        page_role="team-dev", page_task_id="task", page_team="team",
        requires_login=False, composer_present=True, composer_text="", attachment_count=0,
        stop_visible=False, transport_active=False, error_texts=(), blocking_dialogs=(),
        choice_prompt_labels=(), mcp_permission_allow_count=0, mcp_permission_node_count=0,
        last_user_message_id=None, last_user_turn_id=None, last_assistant_message_id=None,
        last_assistant_turn_id=None, assistant_text_length=0, assistant_text_tail="",
        response_activity_length=0, response_activity_tail="", response_activity_turn_id=None,
    )
    values.update(changes)
    return WaitProbe(**values)


def receipt():
    return SendReceipt(
        prompt="original task", prompt_sha256="digest", binding=PageBinding("page", "team-dev"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1, accepted_via="user_message_identity", session_id_before="current",
        conversation_id="current", user_message_id="original-user", user_turn_id="original-turn",
    )


def test_hidden_permission_survives_real_probe_to_full_snapshot_conversion():
    converted = ChatGPTPage._snapshot_from_probe(
        snapshot(), probe(mcp_permission_node_count=1, mcp_permission_allow_count=0),
    )
    assert getattr(converted, "mcp_permission_node_count", None) == 1
    assert getattr(converted, "mcp_permission_allow_count", None) == 0


def test_retry_is_recoverable_not_a_fatal_page_state():
    state = classify_chatgpt_state({
        "error_present": True, "retry_visible": True, "error_texts": [],
        "composer_present": True, "composer_text": "", "stop_visible": False,
    })
    assert state is not ChatGPTState.ERROR


def test_current_operator_continuation_does_not_require_original_user_id():
    response = MessageSnapshot("assistant", "current-answer", "new-turn", "Current result", ())
    current = snapshot(messages=(
        MessageSnapshot("user", "operator-injection", "operator-turn", "continue", ()),
        response,
    ))
    client = ChatGPTPage(SimpleNamespace(url=current.url))
    client.binding = receipt().binding

    async def read(*_args, **_kwargs):
        return current

    client.wait_snapshot = read
    result = asyncio.run(client.wait_for_response(
        receipt(), timeout_ms=80, stable_ms=0, poll_ms=1, minimum_samples=1,
    ))
    assert result == response


def test_retry_snapshot_allows_continuation_but_auth_does_not():
    # The Retry flag must never become permission to send through a real login failure.
    current = snapshot(state=ChatGPTState.ERROR, retry_visible=True)
    ChatGPTPage._assert_interaction_safe(current)
    with pytest.raises(Exception):
        ChatGPTPage._assert_interaction_safe(replace(current, requires_login=True))
