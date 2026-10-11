"""P0.001.2: recovery is identity-preserving, non-replaying, and fail-closed."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import CDPATabActions, RoleOwnershipError, AcquiredRole
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.durable import RequestLedger
from test_cdpa_actions import FakeClient, FakeContext, FakePage, manifest
from test_cdpa_core import write_config
from test_cdpa_worker import _prepare_sent_waiting_task


UUID = "6ac48020-3fc8-83ec-9a6f-017047b76b0a"
OTHER = "22222222-2222-4222-8222-222222222222"
REGULAR = f"https://chatgpt.com/c/{UUID}"
PROJECT = f"https://chatgpt.com/g/g-p-project-cdpa/c/{UUID}"


def _actions(tmp_path, monkeypatch, pages=(), **kwargs):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "random_delay", lambda *_a, **_k: asyncio.sleep(0))
    return CDPATabActions(FakeContext(list(pages), **kwargs), config)


def test_reopen_preassigned_team_project_url_without_saved_page_id(tmp_path, monkeypatch):
    actions = _actions(tmp_path, monkeypatch)
    state = manifest(page_id=None, page_url=PROJECT, conversation_url=REGULAR)

    acquired = asyncio.run(actions.reopen(state, "PLAN", require_clean_ready=False))

    assert acquired.url == PROJECT
    assert acquired.page_id
    assert acquired.new_chat is False
    assert actions.browser_context.new_page_calls == 1
    assert acquired.client.page.goto_calls == [PROJECT]


def test_reopen_reuses_changed_runtime_page_id_on_same_canonical_owner(tmp_path, monkeypatch):
    page = FakePage(page_id="new-runtime-page", role="PLAN", team="new-team", task_id="task-1")
    page.url = PROJECT
    page.snapshot_value.url = PROJECT
    actions = _actions(tmp_path, monkeypatch, [page])
    state = manifest(page_id="old-runtime-page", page_url=PROJECT, conversation_url=REGULAR)

    acquired = asyncio.run(actions.reopen(state, "PLAN", require_clean_ready=False))

    assert acquired.page_id == "new-runtime-page"
    assert acquired.url == PROJECT
    assert acquired.created is False
    assert actions.browser_context.new_page_calls == 0
    assert page.goto_calls == []


def test_reopen_rejects_conflicting_saved_uuids_before_browser_mutation(tmp_path, monkeypatch):
    actions = _actions(tmp_path, monkeypatch)
    state = manifest(page_id="old", page_url=PROJECT, conversation_url=f"https://chatgpt.com/c/{OTHER}")

    with pytest.raises(RoleOwnershipError, match="conflict|different"):
        asyncio.run(actions.reopen(state, "PLAN"))
    assert actions.browser_context.new_page_calls == 0


def test_reopen_rejects_foreign_owner_on_same_uuid(tmp_path, monkeypatch):
    foreign = FakePage(page_id="foreign", role="PLAN", team="other-team", task_id="task-other")
    foreign.url = PROJECT
    foreign.snapshot_value.url = PROJECT
    actions = _actions(tmp_path, monkeypatch, [foreign])
    with pytest.raises(RoleOwnershipError, match="owned|owner"):
        asyncio.run(actions.reopen(manifest(page_id="lost", page_url=PROJECT), "PLAN"))
    assert actions.browser_context.new_page_calls == 0
    assert foreign.goto_calls == []


def test_reopen_refuses_ambiguous_double_owned_new_page_ids(tmp_path, monkeypatch):
    pages = [
        FakePage(page_id=f"runtime-{i}", role="PLAN", team="new-team", task_id="task-1")
        for i in (1, 2)
    ]
    for page in pages:
        page.url = REGULAR
        page.snapshot_value.url = REGULAR
    actions = _actions(tmp_path, monkeypatch, pages)
    with pytest.raises(RoleOwnershipError, match="multiple"):
        asyncio.run(actions.reopen(manifest(page_id="lost", page_url=REGULAR), "PLAN"))
    assert actions.browser_context.new_page_calls == 0


def test_confirmed_challenge_causes_typed_reopen_error_not_picker_only(tmp_path, monkeypatch):
    actions = _actions(tmp_path, monkeypatch)
    monkeypatch.setattr(actions_module, "cloudflare_challenge_visible", AsyncMock(return_value=True))
    with pytest.raises(actions_module.BrowserChallengeError):
        asyncio.run(actions.reopen(manifest(page_id="old", page_url=REGULAR), "PLAN"))
    assert actions.browser_context.pages[0].closed is True

    monkeypatch.setattr(actions_module, "cloudflare_challenge_visible", AsyncMock(return_value=False))
    clean = _actions(tmp_path, monkeypatch)
    reopened = asyncio.run(clean.reopen(manifest(page_id="old", page_url=REGULAR), "PLAN"))
    assert reopened.url == REGULAR


def test_reopen_ready_uses_bounded_thirty_second_budget(tmp_path, monkeypatch):
    actions = _actions(tmp_path, monkeypatch)
    reopened = asyncio.run(actions.reopen(manifest(page_id="lost", page_url=REGULAR), "PLAN"))
    ready_budget = reopened.client.clean_ready_calls
    assert len(ready_budget) == 1
    assert 15000 < ready_budget[0] <= 30000


def _sent(tmp_path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="p0012-recovery"
    )
    hop["conversation_url"] = REGULAR
    state["roles"]["PLAN"]["page_url"] = PROJECT
    return state, worker, hop, receipt


def test_accepted_conversation_reopens_changed_runtime_page_id_without_replay(tmp_path):
    state, worker, hop, receipt = _sent(tmp_path)
    original = dict(hop["receipt"])
    recovered = AcquiredRole(
        client=SimpleNamespace(
            wait_until_clean_ready=AsyncMock(),
            assert_ownership=AsyncMock(),
        ),
        page_id="new-runtime-page",
        url=PROJECT,
        created=True,
        new_chat=False,
    )
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(return_value=recovered),
        wake=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result is recovered
    assert state["status"] != "BLOCKED"
    assert hop["receipt"] == original
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1
    assert state["roles"]["PLAN"]["page_url"] == PROJECT
    actions.fresh_chat.assert_not_awaited()


def test_unavailable_saved_conversation_pauses_without_new_chat_or_reload(tmp_path):
    state, worker, hop, receipt = _sent(tmp_path)
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(side_effect=TimeoutError("readiness did not settle")),
        fresh_chat=AsyncMock(),
        wake=AsyncMock(),
    )
    original = dict(hop["receipt"])
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "PAUSED"
    assert state["block_code"] == "SESSION_UNAVAILABLE"
    assert hop["receipt"] == original
    actions.fresh_chat.assert_not_awaited()


def test_confirmed_reopen_challenge_pauses_not_rate_limit(tmp_path):
    state, worker, hop, receipt = _sent(tmp_path)
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(side_effect=actions_module.BrowserChallengeError("Cloudflare challenge")),
        fresh_chat=AsyncMock(),
        wake=AsyncMock(),
    )
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "PAUSED"
    assert state["block_code"] == "BROWSER_CHALLENGE"
    actions.fresh_chat.assert_not_awaited()


def test_lost_temporary_document_preserves_accepted_receipt_and_pauses(tmp_path):
    state, worker, hop, receipt = _sent(tmp_path)
    state["conversation_mode"] = "temporary"
    state["context_policy"] = "fresh"
    hop["conversation_url"] = f"{REGULAR}?temporary-chat=true"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    original = dict(hop["receipt"])
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "PAUSED"
    assert state["block_code"] == "SESSION_UNAVAILABLE"
    assert hop["receipt"] == original
    assert RequestLedger(hop['ledger_path']).get(hop['request_id']).attempts == 1
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_ledger_only_accepted_canonical_id_is_reconciled_before_reopen(tmp_path):
    state, worker, hop, receipt = _sent(tmp_path)
    hop["conversation_url"] = None
    state["roles"]["PLAN"]["page_url"] = None
    ledger = RequestLedger(hop["ledger_path"])
    canonical = replace(receipt, conversation_id=UUID).to_dict()
    ledger.update(hop["request_id"], receipt=canonical)
    original_ledger = ledger.peek(hop["request_id"]).to_dict()
    acquired = AcquiredRole(
        client=SimpleNamespace(wait_until_clean_ready=AsyncMock()),
        page_id="new-runtime-page", url=REGULAR,
        created=True, new_chat=False,
    )
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=None),
        reopen=AsyncMock(return_value=acquired),
        wake=AsyncMock(), fresh_chat=AsyncMock(),
    )
    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))
    assert result is acquired
    assert actions.reopen.await_count == 1
    assert hop["conversation_url"] == REGULAR
    assert hop["receipt"] == canonical
    assert hop["receipt"]["binding"] == receipt.to_dict()["binding"]
    assert ledger.peek(hop["request_id"]).to_dict() == original_ledger
    actions.fresh_chat.assert_not_awaited()
