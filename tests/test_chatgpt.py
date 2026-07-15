import pytest

from playwright_auto.chatgpt import (
    ChatGPTState,
    MessageSnapshot,
    classify_chatgpt_state,
    extract_session_id,
    recent_assistant_messages,
    recent_assistant_turns,
    validate_page_role,
)


def test_extract_session_id_from_supported_routes():
    assert extract_session_id("https://chatgpt.com/c/abc-123") == "abc-123"
    assert extract_session_id("https://chatgpt.com/g/g-demo/c/session-9") == "session-9"
    assert extract_session_id("https://chatgpt.com/") is None
    assert extract_session_id("https://auth.openai.com/c/not-a-chat") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"error_present": True}, ChatGPTState.ERROR),
        ({"requires_login": True}, ChatGPTState.AUTH_REQUIRED),
        ({"requires_login": True, "stop_visible": True}, ChatGPTState.RESPONDING),
        ({"requires_login": True, "composer_text": "draft"}, ChatGPTState.DRAFT),
        ({"messages": [{"role": "user"}]}, ChatGPTState.SUBMITTING),
        ({"messages": [{"role": "assistant"}]}, ChatGPTState.WAITING_PROMPT),
        ({"composer_present": True}, ChatGPTState.NEW_CHAT),
        ({}, ChatGPTState.UNKNOWN),
    ],
)
def test_classify_chatgpt_state(raw, expected):
    assert classify_chatgpt_state(raw) is expected


def test_recent_assistant_messages_returns_last_n():
    messages = [
        MessageSnapshot("user", "u1", "t1", "one", ()),
        MessageSnapshot("assistant", "a1", "t1", "first", ("copy-turn-action-button",)),
        MessageSnapshot("user", "u2", "t2", "two", ()),
        MessageSnapshot("assistant", "a2", "t2", "second", ("copy-turn-action-button",)),
    ]
    assert [item.message_id for item in recent_assistant_messages(messages, 1)] == ["a2"]
    assert [item.message_id for item in recent_assistant_messages(messages, 2)] == ["a1", "a2"]
    with pytest.raises(ValueError):
        recent_assistant_messages(messages, 0)


def test_recent_assistant_turns_deduplicates_message_nodes():
    messages = [
        MessageSnapshot("assistant", "a1-analysis", "t1", "analysis", ()),
        MessageSnapshot("assistant", "a1-final", "t1", "final", ()),
        MessageSnapshot("user", "u2", "t2", "next", ()),
        MessageSnapshot("assistant", "a2", "t2", "second", ()),
    ]
    assert [item.message_id for item in recent_assistant_turns(messages, 2)] == [
        "a1-final",
        "a2",
    ]


def test_validate_page_role():
    assert validate_page_role("PLAN") == "PLAN"
    assert validate_page_role("review-2") == "review-2"
    with pytest.raises(ValueError):
        validate_page_role("2bad role")
