from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.cdpa_actions import AcquiredRole
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


def test_accepted_provisional_url_canonicalizes_only_on_same_owned_page(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="provisional-owned"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    canonical = "https://chatgpt.com/c/11111111-1111-4111-8111-111111111111"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    user = MessageSnapshot(role="user", message_id="u1", turn_id="t1", text=receipt.prompt, actions=())
    snapshot = SimpleNamespace(page_id=receipt.binding.page_id, session_id=canonical.rsplit("/", 1)[-1],
                               url=canonical, messages=(user,))
    acquired = AcquiredRole(client=SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot)),
                            page_id=receipt.binding.page_id, url=canonical, created=False, new_chat=False)
    actions = SimpleNamespace(locate_owned=AsyncMock(return_value=acquired), reopen=AsyncMock())

    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result is acquired
    assert hop["conversation_url"] == canonical
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).receipt["conversation_id"] == snapshot.session_id
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1
    actions.reopen.assert_not_awaited()


def test_accepted_provisional_url_same_page_without_canonical_url_enters_read_only_settle(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="provisional-still-local"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    user = MessageSnapshot(role="user", message_id="u1", turn_id="t1", text=receipt.prompt, actions=())
    snapshot = SimpleNamespace(page_id=receipt.binding.page_id, session_id="local-chatgpt%3Atemporary",
                               url=provisional, messages=(user,))
    acquired = AcquiredRole(client=SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot)),
                            page_id=receipt.binding.page_id, url=provisional, created=False, new_chat=False)
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=acquired),
        reopen=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts
    original_request_id = hop["request_id"]
    original_deadline = hop["wait"]["deadline_at"]
    original_hop_count = len(state["hops"])

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None

    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["active_action"] == "wait_accepted_conversation_identity"
    assert hop["request_id"] == original_request_id
    assert hop["wait"]["deadline_at"] == original_deadline
    assert hop["wait"]["accepted_identity_page_id"] == receipt.binding.page_id
    assert "accepted_identity_started_at" in hop["wait"]
    assert "accepted_identity_deadline_at" in hop["wait"]
    assert len(state["hops"]) == original_hop_count
    durable = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert durable.receipt["conversation_id"] is None
    assert durable.attempts == original_attempts
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_accepted_provisional_url_other_page_cannot_enrich_or_rebind(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="provisional-other-page"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    acquired = AcquiredRole(client=SimpleNamespace(assert_ownership=AsyncMock()),
                            page_id="different-page", url="https://chatgpt.com/c/other", created=False, new_chat=False)
    actions = SimpleNamespace(locate_owned=AsyncMock(return_value=acquired), reopen=AsyncMock())

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["block_code"] == "accepted_conversation_identity_unresolved"
    assert hop["receipt"] == receipt.to_dict()
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).receipt["conversation_id"] is None
    acquired.client.assert_ownership.assert_not_awaited()
    actions.reopen.assert_not_awaited()


def test_accepted_provisional_url_lost_page_blocks_without_reopen_or_rebind(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="provisional-lost"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    original_hop_count = len(state["hops"])
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "accepted_conversation_identity_unresolved"
    assert state["block_retryable"] is False
    assert hop["receipt"] == receipt.to_dict()
    assert len(state["hops"]) == original_hop_count
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_accepted_provisional_url_lost_user_provenance_blocks_without_replay(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="provisional-lost-user"
    )
    provisional = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    hop["conversation_url"] = provisional
    state["roles"]["PLAN"]["page_url"] = provisional
    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id="local-chatgpt%3Atemporary",
        url=provisional,
        messages=(),
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot)),
        page_id=receipt.binding.page_id,
        url=provisional,
        created=False,
        new_chat=False,
    )
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=acquired),
        reopen=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    original_hop_count = len(state["hops"])
    original_attempts = RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "accepted_conversation_identity_unresolved"
    assert state["block_retryable"] is False
    assert hop["receipt"] == receipt.to_dict()
    assert len(state["hops"]) == original_hop_count
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == original_attempts
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


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
