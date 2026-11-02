from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.chatgpt_errors import ComposerConflictError
from playwright_auto.chatgpt_models import (
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    prompt_digest,
)
from test_cdpa_core import write_config


def _snapshot(*, messages=()):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/direct-chat",
        session_id="direct-chat",
        page_id="page-direct-chat",
        page_role="DIRECT_CHAT",
        state=ChatGPTState.WAITING_PROMPT,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=True,
        send_enabled=True,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=tuple(messages),
    )


class FakeDirectClient:
    def __init__(self, *, fail_wait_once=False, input_race=False):
        self.binding = PageBinding("page-direct-chat", "DIRECT_CHAT")
        self.send_calls = 0
        self.wait_calls = 0
        self.fail_wait_once = fail_wait_once
        self.input_race = input_race
        self.sent = False
        self.terminal = False

    async def snapshot(self):
        if self.terminal:
            return _snapshot(
                messages=(
                    MessageSnapshot("user", "user-new", "turn-user", "plain operator text", ()),
                    MessageSnapshot(
                        "assistant",
                        "assistant-new",
                        "turn-assistant",
                        "plain answer, deliberately not RouteContract JSON",
                        (),
                    ),
                )
            )
        if self.sent:
            return _snapshot(
                messages=(
                    MessageSnapshot("user", "user-new", "turn-user", "plain operator text", ()),
                )
            )
        return _snapshot()

    async def send(self, text, **kwargs):
        self.send_calls += 1
        if self.input_race:
            raise ComposerConflictError("operator typed after idle admission")
        callback = kwargs.get("after_send_click")
        if callback is not None:
            callback()
        self.sent = True
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=MessageBaseline(
                message_ids=frozenset(),
                turn_ids=frozenset(),
                assistant_turn_ids=frozenset(),
                user_message_ids=frozenset(),
            ),
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="direct-chat",
            user_message_id="user-new",
            user_turn_id="turn-user",
            conversation_id="direct-chat",
        )

    async def wait_for_response(self, _receipt, **_kwargs):
        self.wait_calls += 1
        if self.fail_wait_once and self.wait_calls == 1:
            raise TimeoutError("response remains active")
        self.terminal = True
        return MessageSnapshot(
            "assistant",
            "assistant-new",
            "turn-assistant",
            "plain answer, deliberately not RouteContract JSON",
            (),
        )


def _worker(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    writer = ConversationWriterLease(worker.runtime_db)
    binding = writer.bind(
        binding_id="direct-binding",
        provider_id="chatgpt_web",
        endpoint_id="ep-direct",
        endpoint_cdp_url="http://127.0.0.1:9222",
        physical_browser_id="browser-direct",
        page_target_identity="page-direct-chat",
        conversation_url="https://chatgpt.com/c/direct-chat",
        verified_new_chat=False,
    )
    return worker, writer, binding


def _enqueue(worker, *, command_id="direct-command"):
    payload = {
        "binding_id": "direct-binding",
        "text": "plain operator text",
        "task_id": None,
        "material_refs": [],
    }
    worker.runtime_db.enqueue_command(
        command_id=command_id,
        idempotency_key=f"api:{command_id}",
        kind="direct_chat_message",
        task_id=None,
        expected_task_version=None,
        payload=payload,
    )
    claimed = worker.runtime_db.claim_next_command(kinds=("direct_chat_message",))
    assert claimed and claimed["command_id"] == command_id
    return payload


def _observed():
    return {
        "endpoint_id": "ep-direct",
        "endpoint_cdp_url": "http://127.0.0.1:9222",
        "physical_browser_id": "browser-direct",
        "page_target_identity": "page-direct-chat",
        "conversation_url": "https://chatgpt.com/c/direct-chat",
    }


def test_t080_direct_chat_accepted_replay_never_clicks_twice(tmp_path, monkeypatch):
    worker, writer, _ = _worker(tmp_path)
    payload = _enqueue(worker)
    client = FakeDirectClient(fail_wait_once=True)

    async def exact_page(_endpoint_id, _url):
        return client, await client.snapshot(), _observed()

    monkeypatch.setattr(worker, "_direct_chat_page", exact_page)

    first = asyncio.run(worker._complete_direct_chat_message("direct-command", payload))
    assert first["status"] == "recovery_required"
    assert first["result"]["code"] == "DIRECT_CHAT_RESPONSE_PENDING"
    assert client.send_calls == 1
    attempt_row = worker.runtime_db.get_command_by_idempotency_key(
        "direct-chat-send:direct-command"
    )
    attempt = attempt_row["payload"]["send_attempt"]
    assert attempt["state"] == "accepted"
    assert attempt["receipt"]["user_message_id"] == "user-new"
    assert writer.get_binding("direct-binding")["binding_status"] == "active"

    second = asyncio.run(worker._complete_direct_chat_message("direct-command", payload))
    assert second["status"] == "applied"
    assert second["result"]["status"] == "completed"
    assert second["result"]["response"]["text"] == "plain answer, deliberately not RouteContract JSON"
    assert client.send_calls == 1
    assert client.wait_calls == 2
    assert writer.get_binding("direct-binding")["binding_status"] == "idle"


def test_t080_direct_chat_human_input_race_never_clears_or_submits(tmp_path, monkeypatch):
    worker, writer, _ = _worker(tmp_path)
    payload = _enqueue(worker, command_id="direct-race")
    client = FakeDirectClient(input_race=True)

    async def exact_page(_endpoint_id, _url):
        return client, await client.snapshot(), _observed()

    monkeypatch.setattr(worker, "_direct_chat_page", exact_page)

    result = asyncio.run(worker._complete_direct_chat_message("direct-race", payload))
    assert result["status"] == "recovery_required"
    assert result["result"]["code"] == "DIRECT_CHAT_INPUT_RACE"
    assert client.send_calls == 1
    assert client.wait_calls == 0
    attempt_row = worker.runtime_db.get_command_by_idempotency_key(
        "direct-chat-send:direct-race"
    )
    assert attempt_row["payload"]["send_attempt"]["state"] == "reconciled_absent"
    assert writer.get_binding("direct-binding")["binding_status"] == "idle"


def test_t080_direct_chat_accepted_active_response_resumes_wait_without_resend(tmp_path, monkeypatch):
    from dataclasses import replace

    worker, writer, _ = _worker(tmp_path)
    payload = _enqueue(worker, command_id="direct-active-resume")

    class ActiveDirectClient(FakeDirectClient):
        async def snapshot(self):
            snapshot = await super().snapshot()
            if self.sent and not self.terminal:
                return replace(snapshot, stop_visible=True, response_projection_active=True)
            return snapshot

    client = ActiveDirectClient(fail_wait_once=True)

    async def exact_page(_endpoint_id, _url):
        return client, await client.snapshot(), _observed()

    monkeypatch.setattr(worker, "_direct_chat_page", exact_page)
    first = asyncio.run(worker._complete_direct_chat_message("direct-active-resume", payload))
    assert first["result"]["code"] == "DIRECT_CHAT_RESPONSE_PENDING"
    assert client.send_calls == 1
    assert writer.get_binding("direct-binding")["binding_status"] == "active"

    resumed = asyncio.run(worker._complete_direct_chat_message("direct-active-resume", payload))
    assert resumed["status"] == "applied"
    assert resumed["result"]["status"] == "completed"
    assert resumed["result"]["response"]["text"] == "plain answer, deliberately not RouteContract JSON"
    assert client.send_calls == 1
    assert client.wait_calls == 2
    assert writer.get_binding("direct-binding")["binding_status"] == "idle"
