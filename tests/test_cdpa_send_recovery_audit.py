from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import (
    _prepare_sent_waiting_task, RecordingCDPASendActions, RecordingCDPASendClient,
)
from test_cdpa_temporary_workflow import TemporaryWorkflowActions
from test_operator_resume_recovery import _prepare_pristine_new_sending_record


@pytest.mark.parametrize("temporary", [False, True])
def test_recovery_send_does_not_require_old_account_conversation_baseline(tmp_path, monkeypatch, temporary):
    store, state, worker, _, old_hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="new-generation-send")
    state["conversation_mode"] = "temporary" if temporary else "persistent"
    first = store.endpoints.resolve()["endpoint_id"]
    second = store.endpoints.add("9202")["endpoint_id"]
    state["roles"]["PLAN"]["browser_endpoint_id"] = first
    old_request = old_hop["request_id"]
    assert worker._prepare_cross_endpoint_recovery(state, second, reason="endpoint offline")
    hop = _active_hop(state)
    actions = TemporaryWorkflowActions()
    asyncio.run(worker._pre_send(state, hop, actions))
    observed = []
    class Client(RecordingCDPASendClient):
        async def send(self, text, *, require_existing_conversation_baseline=False, **kwargs):
            observed.append(require_existing_conversation_baseline)
            return await super().send(text, **kwargs)
    client = Client(task_id=state["task_id"], team=state["team"])
    # Real DurableSendBlock remains in this test; only browser I/O is deterministic.
    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))
    assert observed == [False], (state.get("block_code"), state.get("block_reason"))
    assert len(client.send_calls) == 1
    assert old_hop["prompt"] != client.send_calls[0]
    assert "RECOVERY_SNAPSHOT" in client.send_calls[0]
    assert RequestLedger(hop["ledger_path"]).get(old_request).attempts == 1
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_fresh_pristine_relocation_reacquires_surface_with_same_request(tmp_path):
    store, state, worker, _, hop, ledger, _ = _prepare_pristine_new_sending_record(tmp_path, task_id="pristine-surface")
    state["bootstrap"] = None
    state["context_policy"] = "fresh"
    state["roles"]["PLAN"].update(context_source="fresh", bootstrap_source_donor=None,
                                      browser_endpoint_id=store.endpoints.resolve()["endpoint_id"])
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    request = hop["request_id"]
    prompt = hop["prompt"]
    assert worker._prepare_execution_endpoint(state, endpoint)
    actions = TemporaryWorkflowActions()
    acquired = asyncio.run(worker._owned_or_block(state, "PLAN", actions))
    assert acquired is not None
    assert actions.fresh_urls == ["https://chatgpt.com/"]
    assert hop["request_id"] == request and hop["prompt"] == prompt
    assert ledger.get(request).attempts == 0


def test_temporary_f5_same_page_and_url_recovery_preserves_accepted_receipt(tmp_path, monkeypatch):
    store, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="f5-same-url")
    state["conversation_mode"] = "temporary"
    role = state["roles"]["PLAN"]
    role.update(browser_endpoint_id=store.endpoints.resolve()["endpoint_id"], temporary_document_id="old-document")
    original = hop["request_id"]
    actions = TemporaryWorkflowActions(owned=True)
    monkeypatch.setattr(worker, "_temporary_document_token", AsyncMock(return_value=None))
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert _active_hop(state)["recovery_of_request_id"] == original
    assert role["page_id"] is None
    assert RequestLedger(hop["ledger_path"]).get(original).attempts == 1


def test_unreadable_ledger_is_semantic_safety_block_not_silent_resource_wait(tmp_path):
    store, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="broken-provenance")
    state["roles"]["PLAN"]["browser_endpoint_id"] = store.endpoints.resolve()["endpoint_id"]
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    from pathlib import Path
    Path(hop["ledger_path"]).write_text("{broken", encoding="utf-8")
    original_hop_count = len(state["hops"])
    assert not worker._prepare_cross_endpoint_recovery(state, endpoint, reason="lost endpoint")
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "execution_recovery_provenance_invalid"
    assert len(state["hops"]) == original_hop_count
