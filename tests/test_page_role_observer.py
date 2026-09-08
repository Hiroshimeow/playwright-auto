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


def test_dom_only_ignores_network_wake_without_detaching_listener():
    async def run():
        page = Page()
        observer = observer_for(page)
        observer.attach()
        observer.network_wake.set()
        assert await observer.wait(0.005, dom_only=True) is False
        assert observer.attached
        assert await observer.wait(0.005, dom_only=False) is True
    asyncio.run(run())
