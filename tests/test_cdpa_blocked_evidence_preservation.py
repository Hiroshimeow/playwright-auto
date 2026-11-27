"""Preserve exact browser evidence when a persistent workflow needs reconciliation."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task


OWNED_URL = "https://chatgpt.com/c/6b081458-d3ec-83ec-93e5-afb8e8d35024"


def _handoff(tmp_path):
    store, state, worker, path, parent, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="preserve-exact-recovery-tab", conversation_mode="persistent",
    )
    parent["state"] = "routed"
    hop = worker._append_hop(state, source_role="PLAN", target_role="DEV", handoff="continue")
    hop.update(state="sending", browser_endpoint_id=state["browser_endpoint_id"],
               conversation_url=OWNED_URL)
    state["roles"]["DEV"].update(
        page_id="owned-evidence-page", binding_id="owned-evidence-binding",
        page_url=OWNED_URL, conversation_generation=1, online=True,
    )
    state.update(status="BLOCKED", block_code="CONVERSATION_IDENTITY_MISMATCH",
                 block_reason="binding evidence changed before browser send")
    assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None
    return store, state, worker, path, hop


@pytest.mark.parametrize("block_code,hop_state", [
    ("CONVERSATION_IDENTITY_MISMATCH", "sending"),
    ("CONVERSATION_BINDING_INVALID", "sending"),
    ("ASYNC_WAKE_IDENTITY_MISMATCH", "pre_send"),
    ("pre_send_request_recovery_required", "pre_send"),
    ("preboundary_acquisition_recovery_required", "pre_send"),
    ("durable_send_provenance_missing", "sending"),
    ("accepted_send_receipt_missing", "waiting"),
    ("accepted_send_provenance_unverified", "waiting"),
    ("accepted_conversation_identity_unresolved", "waiting"),
    ("preboundary_context_unrecoverable", "sending"),
    ("resume_recovery_failed", "pre_send"),
    ("resume_progress_unverified", "pre_send"),
])
def test_persistent_reconciliation_hold_keeps_exact_page_and_prior_send(
    tmp_path, block_code, hop_state,
):
    _store, state, worker, _path, hop = _handoff(tmp_path)
    state["block_code"] = block_code
    hop["state"] = hop_state
    before = copy.deepcopy(state)
    ledger = Path(hop["ledger_path"])
    ledger_before = ledger.read_bytes()
    actions = SimpleNamespace(
        preflight_team=AsyncMock(return_value=[object()]), close_team=AsyncMock(),
    )

    asyncio.run(worker._release_blocked_workflow_tabs(state, actions))

    actions.preflight_team.assert_not_awaited()
    actions.close_team.assert_not_awaited()
    assert state == before
    assert ledger.read_bytes() == ledger_before


@pytest.mark.parametrize("stage", ["acquisition", "fresh"])
def test_durable_acquisition_hold_survives_generic_legacy_block_code(tmp_path, stage):
    _store, state, worker, _path, hop = _handoff(tmp_path)
    state["block_code"] = "unexpected_error"
    hop["state"] = "pre_send"
    hop["preboundary_recovery"] = {
        "phase": "recovery_required", "stage": stage,
        "request_id": hop["request_id"], "attempts": 1,
    }
    before = copy.deepcopy(state)
    actions = SimpleNamespace(
        preflight_team=AsyncMock(return_value=[object()]), close_team=AsyncMock(),
    )

    asyncio.run(worker._release_blocked_workflow_tabs(state, actions))

    actions.preflight_team.assert_not_awaited()
    actions.close_team.assert_not_awaited()
    assert state == before


@pytest.mark.parametrize("block_code", ["role_offline", "route_target_invalid"])
def test_ordinary_persistent_block_still_releases_verified_team_tabs(tmp_path, block_code):
    _store, state, worker, _path, hop = _handoff(tmp_path)
    state["block_code"] = block_code
    hop["state"] = "pre_send"
    pages = [object()]
    actions = SimpleNamespace(
        preflight_team=AsyncMock(return_value=pages), close_team=AsyncMock(),
    )
    before = copy.deepcopy(state)
    ledger = Path(hop["ledger_path"])
    ledger_before = ledger.read_bytes()

    asyncio.run(worker._release_blocked_workflow_tabs(state, actions))

    actions.preflight_team.assert_awaited_once_with(state)
    actions.close_team.assert_awaited_once_with(state, preflighted_pages=pages)
    assert state == before
    assert ledger.read_bytes() == ledger_before


def test_pre_send_identity_failure_and_later_blocked_tick_never_close_evidence(
    tmp_path, monkeypatch,
):
    store, state, worker, path, hop = _handoff(tmp_path)
    state.update(status="RUNNING", block_code=None, block_reason=None)
    store.save(path, state)
    evidence_page = SimpleNamespace(closed=False)
    actions = SimpleNamespace(
        preflight_team=AsyncMock(return_value=[evidence_page]),
        close_team=AsyncMock(),
    )

    async def close_team(_state, *, preflighted_pages):
        for page in preflighted_pages:
            page.closed = True

    actions.close_team.side_effect = close_team
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args: actions)
    checked = []

    async def identity_failure(current, current_hop, _actions):
        checked.append(current_hop["request_id"])
        worker._block(
            current,
            "CONVERSATION_IDENTITY_MISMATCH: binding evidence changed before browser send",
            code="CONVERSATION_IDENTITY_MISMATCH",
            retryable=False,
        )

    monkeypatch.setattr(worker, "_sending", identity_failure)
    ledger = Path(hop["ledger_path"])
    ledger_before = ledger.read_bytes()

    async def run():
        first = await worker.advance(path, SimpleNamespace(pages=[evidence_page]),
                                     scheduling_tasks=[state])
        assert first["status"] == "BLOCKED"
        assert first["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
        await worker.advance(path, SimpleNamespace(pages=[evidence_page]),
                             scheduling_tasks=[first])

    asyncio.run(run())

    after = store.load(path)
    assert checked == [hop["request_id"]]
    assert evidence_page.closed is False
    actions.preflight_team.assert_not_awaited()
    actions.close_team.assert_not_awaited()
    assert after["roles"]["DEV"]["page_id"] == "owned-evidence-page"
    assert after["roles"]["DEV"]["binding_id"] == "owned-evidence-binding"
    assert _active_hop(after)["request_id"] == hop["request_id"]
    assert _active_hop(after)["state"] == "sending"
    assert RequestLedger(ledger).peek(hop["request_id"]) is None
    assert ledger.read_bytes() == ledger_before
