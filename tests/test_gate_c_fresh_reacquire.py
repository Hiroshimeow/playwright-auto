from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
