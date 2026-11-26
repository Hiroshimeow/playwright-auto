from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_worker import CDPAWorker


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


@pytest.mark.parametrize(
    "provisional_url",
    ["https://chatgpt.com/", "https://chatgpt.com/c/local-chatgpt%3Ashared-placeholder"],
)
def test_gate_c_accepted_fresh_chat_keeps_page_writer_until_canonicalized(tmp_path, provisional_url):
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

    state = {
        "task_id": "fresh-accepted-task",
        "conversation_mode": "persistent",
        "roles": {
            "PLAN": {
                "conversation_generation": 1,
                "page_id": "page-fresh-accepted",
                "page_url": provisional_url,
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
        url=provisional_url,
        new_chat=False,
        client=Client(),
    )

    try:
        _writer, lease, attempt = asyncio.run(
            worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired)
        )
        worker._gate_c_mark_send_accepted(state, hop, "PLAN", provisional_url)

        binding = writer.get_binding(state["roles"]["PLAN"]["conversation_binding_id"])
        command = db.get_command(attempt["attempt_id"])
        assert binding["canonical_conversation_id"] is None
        assert binding["writer_identity_key"] == lease["writer_identity_key"]
        assert binding["provisional_writer_identity_key"] == lease["writer_identity_key"]
        assert command["payload"]["send_attempt"]["state"] == "accepted"

        worker._gate_c_mark_send_accepted(
            state, hop, "PLAN", "https://chatgpt.com/c/canonical-fresh-chat"
        )
        assert writer.get_binding(binding["binding_id"])["canonical_conversation_id"] == "canonical-fresh-chat"
    finally:
        db.close()


@pytest.mark.parametrize("hop_kind", ["role_restart", "accepted_send_reconciliation"])
def test_fresh_recovery_hop_binds_its_new_page_without_mutating_the_old_writer(tmp_path, hop_kind):
    from test_cdpa_stop_writer_cleanup import owned_binding
    from test_cdpa_worker import ready_client, setup_task

    _, _, state, worker = setup_task(tmp_path)
    role = state["roles"]["PLAN"]
    role["conversation_generation"] = 2
    old_id = worker._gate_c_task_binding_id(state, "PLAN")
    writer, _ = owned_binding(worker, state, "PLAN", identity=old_id)
    before = writer.get_binding(old_id)
    role.update(page_id="fresh-page-after-restart", page_url="https://chatgpt.com/")
    hop = {
        "kind": hop_kind, "request_id": "new-role-recovery-request", "prompt": "canonical prompt",
        "prompt_sha256": hashlib.sha256(b"canonical prompt").hexdigest(),
        "target_role": "PLAN",
        "browser_endpoint_id": state["browser_endpoint_id"], "conversation_generation": 2,
        "generation_started_here": True,
    }
    acquired = SimpleNamespace(page_id=role["page_id"], url=role["page_url"], new_chat=False, client=ready_client(state, "PLAN"))
    _, _, attempt = asyncio.run(worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired))
    assert attempt["owner_binding_id"] != old_id
    assert writer.get_binding(attempt["owner_binding_id"])["page_target_identity"] == acquired.page_id
    assert writer.get_binding(old_id) == before
    _, _, same_attempt = asyncio.run(worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired))
    assert same_attempt["attempt_id"] == attempt["attempt_id"]

    # A model gate can leave an intent on the old writer before any Send.
    # Reconcile that durable intent before the exact pristine request moves.
    old_key = "browser-send:other-pending-role-request"
    old_attempt, _ = writer.record_send_intent(
        writer.acquire(old_id), idempotency_key=old_key, text=hop["prompt"],
        baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    new_hop = {**hop, "request_id": "other-pending-role-request"}
    worker._reconcile_pristine_presend_writer(state, new_hop, page_missing=True)
    assert worker.runtime_db.get_command(old_attempt["attempt_id"])["payload"]["send_attempt"]["state"] == "reconciled_absent"
    assert writer.get_binding(old_id)["binding_status"] == "sealed"
