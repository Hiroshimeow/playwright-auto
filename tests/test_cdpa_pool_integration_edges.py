from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task


@pytest.mark.parametrize("temporary", [False, True])
def test_disconnect_during_owned_lookup_escapes_to_pool_without_block(tmp_path, temporary):
    _store, state, worker, _path, hop, _receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="owned-disconnect"
    )
    state["conversation_mode"] = "temporary" if temporary else "persistent"
    actions = SimpleNamespace(locate_owned=AsyncMock(side_effect=ConnectionError("browser has been closed")))
    original_request = hop["request_id"]
    with pytest.raises(ConnectionError, match="browser has been closed"):
        asyncio.run(worker._owned_or_block(state, "PLAN", actions))
    assert state["status"] != "BLOCKED"
    assert _active_hop(state)["request_id"] == original_request
    assert RequestLedger(hop["ledger_path"]).get(original_request).attempts == 1


def test_resume_preserves_a_created_recovery_child_instead_of_reblocking_saved_chat(tmp_path):
    from test_cdpa_temporary_workflow import TemporaryWorkflowActions
    _store, state, worker, _path, hop, _receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="resume-lost-temporary"
    )
    state["conversation_mode"] = "temporary"
    original = hop["request_id"]
    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering"}
    state["controls"] = [control]
    asyncio.run(worker._recover_resume_waiting(state, hop, control, TemporaryWorkflowActions()))
    assert state["status"] != "BLOCKED"
    assert _active_hop(state)["recovery_of_request_id"] == original
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "recovery_continuation_queued"
    assert RequestLedger(hop["ledger_path"]).get(original).attempts == 1


def test_pool_resume_lost_ambiguous_send_queues_continuation_without_replaying(tmp_path):
    from test_operator_resume_recovery import _prepare_sending_record
    _store, state, worker, _path, hop, ledger, _binding, _baseline = _prepare_sending_record(
        tmp_path, task_id="pool-lost-ambiguous"
    )
    worker._endpoint_pool = SimpleNamespace(contexts={})
    original = hop["request_id"]
    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering"}
    state["controls"] = [control]
    actions = SimpleNamespace(locate_owned=AsyncMock(return_value=None))
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    assert _active_hop(state)["kind"] == "execution_recovery"
    assert _active_hop(state)["recovery_of_request_id"] == original
    assert ledger.get(original).attempts == 1
    assert ledger.get(original).receipt is None
    assert control["status"] == "applied"


def test_temporary_receipt_identity_is_provenance_not_a_saved_reopen_url(tmp_path):
    _store, state, worker, _path, hop, _receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="temporary-canonical-url"
    )
    state["conversation_mode"] = "temporary"
    url = "https://chatgpt.com/?temporary-chat=true"
    state["roles"]["PLAN"]["page_url"] = url
    hop["conversation_url"] = url
    hop["receipt"]["conversation_id"] = "11111111-1111-4111-8111-111111111111"
    worker._canonicalize_receipt_conversation_url(state, hop)
    assert hop["conversation_url"] == url
    assert state["roles"]["PLAN"]["page_url"] == url
    assert hop["receipt"]["conversation_id"] == "11111111-1111-4111-8111-111111111111"
