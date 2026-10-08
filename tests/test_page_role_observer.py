import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace

from playwright_auto.role_runtime.page_observer import observer_for
from test_role_listen import permission


class Page:
    def __init__(self):
        self.url = "https://chatgpt.com/c/chat"
        self.listeners = defaultdict(list)
        self.main_frame = object()
    def on(self, name, callback):
        self.listeners[name].append(callback)
    def remove_listener(self, name, callback):
        if callback in self.listeners[name]:
            self.listeners[name].remove(callback)


def test_page_observer_is_shared_without_active_request_scope():
    page = Page()
    first = observer_for(page)
    second = observer_for(page)
    assert first is second
    first.attach()
    second.attach()
    assert len(page.listeners["requestfinished"]) == 1
    first.publish({"permission_action": {"type": "allow", "target_message_id": "call"}}, epoch=0, source="test")
    assert second.observation()["permission_action"]["target_message_id"] == "call"
    first.close()
    assert page.listeners["requestfinished"] == []


def test_late_requestless_permission_does_not_depend_on_original_user():
    page = Page()
    observer = observer_for(page)
    observer._on_request(SimpleNamespace(method="POST", url="https://chatgpt.com/backend-api/f/conversation"))
    observer.publish({"conversation_id": "chat", "permission_action": {"type": "allow", "target_message_id": "tool-continuation"}}, epoch=0, source="test")
    assert observer.observation()["permission_action"]["target_message_id"] == "tool-continuation"


def test_other_conversation_and_old_document_are_not_applied():
    page = Page()
    observer = observer_for(page)
    observer.publish({"conversation_id": "foreign", "permission_action": {"type": "allow", "target_message_id": "other"}}, epoch=0, source="test")
    assert observer.observation()["permission_action"] is None
    page.url = "https://chatgpt.com/c/new"
    observer._on_navigation(page.main_frame)
    observer.publish({"permission_action": {"type": "allow", "target_message_id": "late"}}, epoch=0, source="test")
    assert observer.observation()["permission_action"] is None


def test_provisional_url_resolution_does_not_discard_first_live_allow():
    page = Page()
    page.url = "https://chatgpt.com/c/WEB:temporary"
    observer = observer_for(page)
    observer.publish({"conversation_id": "canonical", "permission_action": {"type": "allow", "target_message_id": "first"}}, epoch=0, source="test")
    page.url = "https://chatgpt.com/c/canonical"
    observer._on_navigation(page.main_frame)
    assert observer.observation()["permission_action"]["target_message_id"] == "first"


def test_history_is_saved_as_info_not_an_auto_action():
    async def run():
        page = Page()
        observer = observer_for(page)
        class Response:
            status = 200
            async def body(self):
                return json.dumps({"messages": [permission()]}).encode()
        class Request:
            async def response(self):
                return Response()
        await observer._read_finished(Request(), "history", 0)
        assert observer.info["messages"]
        assert observer.observation()["permission_action"] is None
        assert observer.observation()["response"] is None
    asyncio.run(run())


def test_cleared_permission_is_not_replayed_from_whole_body_fallback():
    page = Page()
    observer = observer_for(page)
    value = {"permission_action": {"type": "allow", "target_message_id": "a", "remember_answer": True}}
    observer.publish(value, epoch=0, source="network_chunk")
    observer.clear_permission()
    observer.publish(value, epoch=0, source="response_body")
    assert observer.observation()["permission_action"] is None


def test_hybrid_wait_wakes_on_network_without_detaching_listener():
    async def run():
        page = Page()
        observer = observer_for(page)
        observer.attach()
        observer.network_wake.set()
        assert await observer.wait(0.005) is True
        assert observer.attached
    asyncio.run(run())


def test_terminal_response_closes_stale_streaming_status():
    page = Page()
    observer = observer_for(page)
    observer._on_request(
        SimpleNamespace(method="POST", url="https://chatgpt.com/backend-api/f/conversation")
    )
    assert observer.observation()["status"] == "IS_STREAMING"

    observer.publish(
        {
            "conversation_id": "chat",
            "response": {
                "role": "assistant",
                "message_id": "answer",
                "turn_id": "turn",
                "text": '{"route":"REVIEW"}',
                "actions": [],
                "image_count": 0,
            },
        },
        epoch=0,
        source="network_chunk",
    )

    evidence = observer.observation()
    assert evidence["status"] == "COMPLETE"
    assert evidence["response"]["text"] == '{"route":"REVIEW"}'


def test_accept_boundary_clears_stale_response_status_and_old_epoch():
    page = Page()
    observer = observer_for(page)
    observer._on_request(
        SimpleNamespace(method="POST", url="https://chatgpt.com/backend-api/f/conversation")
    )
    observer.publish(
        {
            "conversation_id": "chat",
            "response": {
                "role": "assistant",
                "message_id": "answer",
                "turn_id": "turn",
                "text": '{"route":"REVIEW"}',
                "actions": [],
                "image_count": 0,
            },
        },
        epoch=0,
        source="network_chunk",
    )
    assert observer.observation()["status"] == "COMPLETE"
    observer.clear_response_state()
    evidence = observer.observation()
    assert evidence["status"] is None
    assert evidence["response"] is None
    assert evidence["messages"] == []
    observer.publish({"status": "IS_STREAMING"}, epoch=0, source="network_chunk")
    assert observer.observation()["status"] is None


def test_observer_emits_instance_epoch_monotonic_signal_sequence_and_time():
    page = Page()
    observer = observer_for(page)
    first = observer.observation()
    assert first["observer_instance_id"]
    assert first["observer_epoch"] == 0
    assert first["signal_seq"] == 0
    assert first["observed_at"] is None

    observer.publish({"status": "IS_STREAMING"}, epoch=0, source="test")
    second = observer.observation()
    assert second["observer_instance_id"] == first["observer_instance_id"]
    assert second["observer_epoch"] == 0
    assert second["signal_seq"] == 1
    assert second["observed_at"]

    observer.publish({"status": "COMPLETE"}, epoch=0, source="test")
    third = observer.observation()
    assert third["signal_seq"] == 2

    observer.clear_response_state()
    fourth = observer.observation()
    assert fourth["observer_epoch"] == 1
    assert fourth["signal_seq"] == 2


def test_explicit_failure_is_not_overwritten_by_terminal_response():
    page = Page()
    observer = observer_for(page)
    observer.publish(
        {
            "status": "FAILURE",
            "response": {
                "role": "assistant",
                "message_id": "answer",
                "turn_id": "turn",
                "text": '{"route":"REVIEW"}',
                "actions": [],
                "image_count": 0,
            },
        },
        epoch=0,
        source="test",
    )
    assert observer.observation()["status"] == "FAILURE"
