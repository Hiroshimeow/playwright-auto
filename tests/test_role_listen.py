"""Wire parsing is independent of request user IDs and graph completeness."""
import json

from playwright_auto.role_runtime.listen import ConversationDecoder, source_kind


def permission(target="tool-call"):
    return {"id": "permission-message", "author": {"role": "tool"}, "metadata": {
        "jit_plugin_data": {"from_server": {"body": {"actions": [{
            "type": "allow", "allow": {"target_message_id": target},
            "split_action_options": [{"label": "Allow mcp-g8 for this conversation", "action": {
                "type": "allow", "target_message_id": target, "remember_answer": True,
            }}],
        }]}}},
    }}


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def test_permission_is_delivered_before_stream_finishes_without_user_or_graph():
    decoder = ConversationDecoder()
    results = decoder.feed(event({"message": permission()}))
    assert results[0]["permission_action"]["target_message_id"] == "tool-call"
    assert results[0]["permission_action"]["remember_answer"] is True


def test_chunk_boundaries_utf8_and_sse_line_fragments():
    decoder = ConversationDecoder()
    data = event({"message": permission(), "label": "Tiếng Việt"})
    results = []
    for value in data:
        results.extend(decoder.feed(bytes([value])))
    assert len(results) == 1
    assert results[0]["permission_action"]["type"] == "allow"


def test_direct_action_and_nested_delta_do_not_require_message_envelope():
    decoder = ConversationDecoder()
    value = {"v": [{"p": "/message/metadata", "o": "replace", "v": {
        "action": {"type": "allow", "target_message_id": "nested", "remember_answer": True}
    }}]}
    assert decoder.feed(event(value))[0]["permission_action"]["target_message_id"] == "nested"


def test_only_terminal_assistant_is_a_result_not_a_partial_message():
    decoder = ConversationDecoder()
    value = {"conversation_id": "chat", "message": {
        "id": "answer", "author": {"role": "assistant"}, "recipient": "all",
        "status": "in_progress", "content": {"content_type": "text", "parts": ['{"route":']},
    }}
    results = decoder.feed(event(value))
    assert not results[0].get("response")
    value["message"]["status"] = "finished_successfully"
    value["message"]["end_turn"] = True
    value["message"]["content"]["parts"] = ['{"route":"PLAN"}']
    result = decoder.feed(event(value))[0]
    assert result["response"]["text"] == '{"route":"PLAN"}'
    assert result["conversation_id"] == "chat"


def test_history_is_information_only_and_never_requires_original_user():
    assert source_kind("GET", "https://chatgpt.com/backend-api/conversations/chat") == "history"
    assert source_kind("GET", "https://chatgpt.com/backend-api/conversation/chat") == "history"
    assert source_kind("POST", "https://chatgpt.com/backend-api/f/conversation") == "live"
    assert source_kind("GET", "https://chatgpt.com/assets/app.js") is None
    assert source_kind("POST", "https://other.example/backend-api/f/conversation") is None


def test_bad_packet_does_not_discard_following_permission():
    decoder = ConversationDecoder()
    results = decoder.feed(b'data: {bad}\n\n' + event({"message": permission()}))
    assert decoder.errors == 1
    assert results[-1]["permission_action"]["target_message_id"] == "tool-call"


def test_decoder_limits_incomplete_buffer_growth():
    decoder = ConversationDecoder(max_buffer=128)
    assert decoder.feed(b"data: " + b"x" * 200) == []
    assert decoder.buffered_bytes <= 128
    assert decoder.errors == 1
