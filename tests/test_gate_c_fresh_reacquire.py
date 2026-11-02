from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.durable import RequestStatus


def test_gate_c_preserves_fresh_chat_provenance_across_sending_reacquire(tmp_path):
    db = RuntimeDB(tmp_path / "fresh-reacquire.sqlite3")
    db.ensure_schema()
    writer = ConversationWriterLease(db)

    class Endpoints:
        @staticmethod
        def get(_endpoint_id):
            return {"endpoint_id": "ep-fresh", "cdp_url": "http://127.0.0.1:9222"}

    class Client:
        @staticmethod
        async def assert_ownership():
            return SimpleNamespace(messages=[])

    worker = object.__new__(CDPAWorker)
    worker.runtime_db = db
    worker.store = SimpleNamespace(endpoints=Endpoints())
    worker._endpoint_pool = None

    state = {
        "task_id": "fresh-task",
        "conversation_mode": "persistent",
        "roles": {
            "PLAN": {
                "conversation_generation": 1,
                "page_id": "page-fresh",
                "page_url": "https://chatgpt.com/",
            }
        },
    }
    hop = {
        "browser_endpoint_id": "ep-fresh",
        "request_id": "fresh-request-1",
        "prompt": "research prompt",
        "conversation_generation": 1,
        "generation_started_here": True,
    }
    acquired = SimpleNamespace(
        page_id="page-fresh",
        url="https://chatgpt.com/",
        new_chat=False,
        client=Client(),
    )

    try:
        _writer, lease, attempt = asyncio.run(
            worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired)
        )
        binding = writer.get_binding(state["roles"]["PLAN"]["conversation_binding_id"])
        assert binding["verified_new_chat"] is True
        assert binding["canonical_conversation_id"] is None
        assert binding["provisional_writer_identity_key"] == lease["writer_identity_key"]
        assert attempt["state"] == "intent_recorded"
    finally:
        db.close()


def test_gate_c_accepted_local_placeholder_keeps_page_writer_until_canonicalized(tmp_path):
    db = RuntimeDB(tmp_path / "fresh-accepted-local.sqlite3")
    db.ensure_schema()
    writer = ConversationWriterLease(db)

    class Endpoints:
        @staticmethod
        def get(_endpoint_id):
            return {"endpoint_id": "ep-fresh", "cdp_url": "http://127.0.0.1:9222"}

    class Client:
        @staticmethod
        async def assert_ownership():
            return SimpleNamespace(messages=[])

    worker = object.__new__(CDPAWorker)
    worker.runtime_db = db
    worker.store = SimpleNamespace(endpoints=Endpoints())
    worker._endpoint_pool = None

    local_url = "https://chatgpt.com/c/local-chatgpt%3Ashared-placeholder"
    state = {
        "task_id": "fresh-accepted-task",
        "conversation_mode": "persistent",
        "roles": {
            "PLAN": {
                "conversation_generation": 1,
                "page_id": "page-fresh-accepted",
                "page_url": local_url,
            }
        },
    }
    hop = {
        "browser_endpoint_id": "ep-fresh",
        "request_id": "fresh-accepted-request",
        "prompt": "research prompt",
        "conversation_generation": 1,
        "generation_started_here": True,
    }
    acquired = SimpleNamespace(
        page_id="page-fresh-accepted",
        url=local_url,
        new_chat=False,
        client=Client(),
    )

    try:
        _writer, lease, attempt = asyncio.run(
            worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired)
        )
        worker._gate_c_mark_send_accepted(state, hop, "PLAN", local_url)

        binding = writer.get_binding(state["roles"]["PLAN"]["conversation_binding_id"])
        command = db.get_command(attempt["attempt_id"])
        assert binding["canonical_conversation_id"] is None
        assert binding["writer_identity_key"] == lease["writer_identity_key"]
        assert binding["provisional_writer_identity_key"] == lease["writer_identity_key"]
        assert command["payload"]["send_attempt"]["state"] == "accepted"
    finally:
        db.close()

def test_gate_c_pristine_resume_rotates_stale_page_binding_without_replay(tmp_path):
    db = RuntimeDB(tmp_path / "fresh-rotate.sqlite3")
    db.ensure_schema()
    writer = ConversationWriterLease(db)

    class Endpoints:
        @staticmethod
        def get(_endpoint_id):
            return {"endpoint_id": "ep-fresh", "cdp_url": "http://127.0.0.1:9222"}

    class Client:
        @staticmethod
        async def assert_ownership():
            return SimpleNamespace(messages=[])

    worker = object.__new__(CDPAWorker)
    worker.runtime_db = db
    worker.store = SimpleNamespace(endpoints=Endpoints())
    worker._endpoint_pool = None

    state = {
        "task_id": "fresh-rotate-task",
        "conversation_mode": "persistent",
        "roles": {
            "PLAN": {
                "conversation_generation": 1,
                "page_id": "page-old",
                "page_url": "https://chatgpt.com/",
            }
        },
    }
    hop = {
        "browser_endpoint_id": "ep-fresh",
        "request_id": "fresh-rotate-request",
        "prompt": "research prompt",
        "conversation_generation": 1,
        "generation_started_here": True,
    }
    old_binding_id = worker._gate_c_task_binding_id(state, "PLAN")
    writer.bind(
        binding_id=old_binding_id,
        provider_id="chatgpt_web",
        endpoint_id="ep-fresh",
        endpoint_cdp_url="http://127.0.0.1:9222",
        physical_browser_id="endpoint:ep-fresh:http://127.0.0.1:9222",
        page_target_identity="page-old",
        conversation_url="https://chatgpt.com/",
        verified_new_chat=True,
        task_id=state["task_id"],
        role_key="PLAN",
    )
    lease = writer.acquire(old_binding_id)
    attempt, _ = writer.record_send_intent(
        lease,
        idempotency_key="browser-send:fresh-rotate-request",
        text=hop["prompt"],
        baseline_last_user_message_id=None,
        baseline_user_turn_count=0,
    )
    state["roles"]["PLAN"]["conversation_binding_id"] = old_binding_id
    hop["conversation_binding_id"] = old_binding_id
    hop["send_attempt_id"] = attempt["attempt_id"]

    durable = SimpleNamespace(
        status=RequestStatus.NEW,
        attempts=0,
        receipt=None,
        binding=None,
    )
    acquired = SimpleNamespace(
        page_id="page-new",
        url="https://chatgpt.com/",
        new_chat=True,
        created=True,
        client=Client(),
    )
    control = {"control_id": 7, "action": "resume"}

    worker._gate_c_rotate_pristine_recovery_binding(
        state, hop, "PLAN", acquired, durable, control
    )

    old_after = writer.get_binding(old_binding_id)
    command = db.get_command(attempt["attempt_id"])
    old_attempt = command["payload"]["send_attempt"]
    new_binding_id = state["roles"]["PLAN"]["conversation_binding_id"]
    assert old_after["binding_status"] == "sealed"
    assert old_attempt["state"] == "reconciled_absent"
    assert new_binding_id != old_binding_id
    assert hop["conversation_binding_id"] == new_binding_id
    assert "send_attempt_id" not in hop
    assert hop["binding_recovery"]["control_id"] == 7

    state["roles"]["PLAN"]["page_id"] = "page-new"
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/"
    hop["generation_started_here"] = True
    reusable = SimpleNamespace(
        page_id="page-new",
        url="https://chatgpt.com/",
        new_chat=False,
        client=Client(),
    )
    _writer, _lease, new_attempt = asyncio.run(
        worker._gate_c_prepare_browser_send(state, hop, "PLAN", reusable)
    )
    new_binding = writer.get_binding(new_binding_id)
    assert new_binding["page_target_identity"] == "page-new"
    assert new_binding["verified_new_chat"] is True
    assert new_attempt["state"] == "intent_recorded"
    db.close()
