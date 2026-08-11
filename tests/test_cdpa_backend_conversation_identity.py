from __future__ import annotations

from types import SimpleNamespace

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.chatgpt import MessageBaseline, PageBinding, SendReceipt, prompt_digest


def _receipt(conversation_id: str | None) -> dict[str, object]:
    prompt = "accepted prompt"
    return SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=PageBinding("page-plan", "alpha-plan"),
        baseline=MessageBaseline(
            message_ids=frozenset(),
            turn_ids=frozenset(),
            assistant_turn_ids=frozenset(),
            user_message_ids=frozenset(),
        ),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="user-1",
        user_turn_id="user-1",
        conversation_id=conversation_id,
    ).to_dict()


def test_late_backend_conversation_id_replaces_temporary_web_url(monkeypatch):
    exact_id = "6a737048-f414-83ee-a6e2-c33b7bdb9904"
    exact_url = f"https://chatgpt.com/c/{exact_id}"
    temporary_url = "https://chatgpt.com/c/WEB:temporary"
    state = {"roles": {"PLAN": {"page_url": temporary_url}}}
    hop = {
        "target_role": "PLAN",
        "ledger_path": "/tmp/ledger.json",
        "request_id": "request-1",
        "conversation_url": temporary_url,
        "receipt": _receipt(None),
    }

    class FakeLedger:
        def __init__(self, _path: str):
            pass

        def get(self, _request_id: str):
            return SimpleNamespace(receipt=_receipt(exact_id))

    monkeypatch.setattr(worker_module, "RequestLedger", FakeLedger)

    worker = object.__new__(CDPAWorker)
    worker._reconcile_hop_conversation_identity(state, hop)

    assert hop["receipt"]["conversation_id"] == exact_id
    assert hop["conversation_url"] == exact_url
    assert state["roles"]["PLAN"]["page_url"] == exact_url
