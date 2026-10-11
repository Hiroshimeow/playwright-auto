"""Operator-approved Cloudflare exception: durable, consumed once, never an implicit retry."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task

UUID = "6ac48020-3fc8-83ec-9a6f-017047b76b0a"
REGULAR = f"https://chatgpt.com/c/{UUID}"


def challenge_case(tmp_path):
    store, state, worker, path, hop, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="p0012-once-only",
    )
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['conversation_mode'] = 'temporary'
    raw['context_policy'] = 'fresh'
    path.write_text(json.dumps(raw, indent=2) + '\n', encoding='utf-8')
    state = store.load(path)
    role = state["roles"]["PLAN"]
    role["page_url"] = "https://chatgpt.com/?temporary-chat=true"
    role["temporary_document_id"] = "lost-document"
    hop["conversation_url"] = "https://chatgpt.com/c/local-chatgpt%3Atemporary"
    worker._pause_recovery(
        state, code="BROWSER_CHALLENGE", reason="Cloudflare challenge positively observed",
    )
    store.save(path, state)
    original = RequestLedger(hop["ledger_path"]).peek(hop["request_id"]).to_dict()
    return store, worker, path, hop["request_id"], original


def apply_resume(store, worker, path, *, confirmed, actions=None, reason="operator recovery"):
    pending = store.request_control(
        path, "resume", confirmed=confirmed, reason=reason,
        external_command_id=f"resume-{len(store.load(path)['controls'])}-{confirmed}",
    )
    actions = actions or SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        fresh_chat=AsyncMock(),
        reopen=AsyncMock(),
    )
    assert asyncio.run(worker._apply_control(pending, actions, path)) is True
    return store.load(path), actions


def test_explicit_operator_confirmation_preserved_in_queued_resume(tmp_path):
    store, worker, path, request_id, original = challenge_case(tmp_path)

    queued = store.request_control(path, "resume", confirmed=True, reason="approve one fresh send")

    control = queued["controls"][-1]
    assert control["action"] == "resume"
    assert control["confirmed"] is True
    assert control["origin"] == "operator"


def test_one_confirmed_resume_consumes_single_new_generation_preserving_receipt(tmp_path):
    store, worker, path, request_id, original = challenge_case(tmp_path)
    result, actions = apply_resume(store, worker, path, confirmed=True, reason="I acknowledge duplicate Send risk")
    original_hop, child = result["hops"][-2:]
    assert original_hop["request_id"] == request_id
    assert original_hop["state"] == "abandoned"
    assert original_hop["receipt"] == original["receipt"]
    assert original_hop["challenge_recovery"]["retry_count"] == 1
    assert original_hop["challenge_recovery"]["original_request_id"] == request_id
    assert "may have" in child["duplicate_risk_warning"].lower()
    assert child["request_id"] != request_id
    assert child["state"] == "pre_send"
    assert result["browser_endpoint_id"] == result["roles"]["PLAN"]["browser_endpoint_id"]
    assert RequestLedger(original_hop["ledger_path"]).peek(request_id).to_dict() == original
    assert len(result["hops"]) == 2
    actions.fresh_chat.assert_not_awaited()
    actions.reopen.assert_not_awaited()


def test_second_operator_approval_after_restart_never_replays_again(tmp_path):
    store, worker, path, request_id, original = challenge_case(tmp_path)
    first, _ = apply_resume(store, worker, path, confirmed=True)
    worker._pause_recovery(first, code="BROWSER_CHALLENGE", reason="Cloudflare again")
    store.save(path, first)

    second, actions = apply_resume(store, worker, path, confirmed=True)
    assert len(second["hops"]) == 2
    assert second["hops"][0]["challenge_recovery"]["retry_count"] == 1
    assert second["hops"][0]["receipt"] == original["receipt"]
    assert second["status"] in {"PAUSED", "BLOCKED"}
    actions.fresh_chat.assert_not_awaited()


def test_unconfirmed_resume_does_not_consume_permission_or_create_generation(tmp_path):
    store, worker, path, request_id, original = challenge_case(tmp_path)
    state, actions = apply_resume(store, worker, path, confirmed=False)
    assert len(state["hops"]) == 1
    assert not state["hops"][0].get("challenge_recovery")
    assert RequestLedger(state["hops"][0]["ledger_path"]).peek(request_id).to_dict() == original
    actions.fresh_chat.assert_not_awaited()


@pytest.mark.parametrize("historical_source", [
    "hop_url", "role_url", "receipt", "ledger_receipt", "url_revision",
])
def test_canonical_identity_anywhere_in_history_forbids_fresh_exception(
    tmp_path, historical_source,
):
    store, worker, path, request_id, original = challenge_case(tmp_path)
    state = store.load(path)
    hop = state["hops"][0]
    if historical_source == "hop_url":
        hop["conversation_url"] = REGULAR
    elif historical_source == "role_url":
        state["roles"]["PLAN"]["page_url"] = REGULAR
    elif historical_source == "receipt":
        hop["receipt"]["conversation_id"] = UUID
    elif historical_source == "ledger_receipt":
        RequestLedger(hop["ledger_path"]).update(
            request_id, receipt={**original["receipt"], "conversation_id": UUID},
        )
    else:
        state["role_url_revisions"] = [{"role": "PLAN", "url": REGULAR}]
    store.save(path, state)

    changed, actions = apply_resume(store, worker, path, confirmed=True)
    assert len(changed["hops"]) == 1
    assert changed["hops"][0]["state"] != "abandoned"
    assert not changed["hops"][0].get("challenge_recovery")
    actions.fresh_chat.assert_not_awaited()


@pytest.mark.parametrize("safety_signal", ["attachments", "activity", "live_page"])
def test_manual_assets_or_active_response_never_approves_replay(tmp_path, safety_signal):
    store, worker, path, request_id, original = challenge_case(tmp_path)
    state = store.load(path)
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        fresh_chat=AsyncMock(),
        reopen=AsyncMock(),
    )
    if safety_signal == "attachments":
        state["roles"]["PLAN"]["attachments_uploaded_generation"] = 0
    elif safety_signal == "activity":
        state["hops"][0]["wait"]["transport_ui_active"] = True
    else:
        actions.locate_owned.return_value = SimpleNamespace()
    store.save(path, state)

    changed, _ = apply_resume(store, worker, path, confirmed=True, actions=actions)
    assert len(changed["hops"]) == 1
    assert not changed["hops"][0].get("challenge_recovery")
    actions.fresh_chat.assert_not_awaited()
