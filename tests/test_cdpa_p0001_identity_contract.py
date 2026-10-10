"""P0.001.1: Send acceptance and URL settlement must not strand a live response."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright_auto.cdpa_actions import (
    AcquiredRole, _reopenable_conversation_identity,
)
from playwright_auto.cdpa_worker import (
    _official_saved_url, _preferred_saved_url,
)
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task


ID = "6ac48020-3fc8-83ec-9a6f-017047b76b0a"
REGULAR = f"https://chatgpt.com/c/{ID}"
PROJECT = f"https://chatgpt.com/g/g-p-project-cdpa/c/{ID}"
PROVISIONAL = "https://chatgpt.com/c/local-chatgpt%3Atemporary"


def _scope(tmp_path, *, stop=False, messages=()):
    _store, state, worker, _path, hop, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="p0001-identity"
    )
    hop["conversation_url"] = PROVISIONAL
    state["roles"]["PLAN"]["page_url"] = PROVISIONAL
    receipt = replace(
        receipt,
        accepted_via="stop_button",
        user_message_id=None,
        user_turn_id=None,
        conversation_id=None,
    )
    hop["receipt"] = receipt.to_dict()
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(hop["request_id"], receipt=receipt.to_dict())
    snapshot = SimpleNamespace(
        page_id=receipt.binding.page_id,
        session_id="local-chatgpt:temporary",
        url=PROVISIONAL,
        messages=tuple(messages),
        stop_visible=stop,
        response_projection_active=False,
        response_activity_turn_id=None,
        response_activity_text="",
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(assert_ownership=AsyncMock(return_value=snapshot)),
        page_id=receipt.binding.page_id,
        url=PROVISIONAL,
        created=False,
        new_chat=False,
    )
    actions = SimpleNamespace(
        locate_owned=AsyncMock(return_value=acquired),
        reopen=AsyncMock(),
        fresh_chat=AsyncMock(),
    )
    return state, worker, hop, receipt, ledger, snapshot, acquired, actions


def test_saved_url_validation_and_project_preference():
    assert _official_saved_url(REGULAR) == (ID, False)
    assert _official_saved_url(PROJECT) == (ID, True)
    assert _preferred_saved_url(REGULAR, PROJECT) == PROJECT
    assert _preferred_saved_url(PROJECT, REGULAR) == PROJECT
    assert _reopenable_conversation_identity(REGULAR) == f"/c/{ID}"
    assert _reopenable_conversation_identity(PROJECT) == f"/c/{ID}"
    assert _preferred_saved_url(PROVISIONAL, REGULAR) == REGULAR
    for invalid in (
        PROVISIONAL,
        "https://chatgpt.com/c/WEB:local",
        "https://chatgpt.com/",
        f"https://other.example/c/{ID}",
        f"https://chatgpt.com/c/{ID}?temporary-chat=true",
    ):
        assert _official_saved_url(invalid) is None
        assert _reopenable_conversation_identity(invalid) is None


def test_project_redirect_does_not_overwrite_good_recovery_url(tmp_path):
    state, worker, hop, _receipt, _ledger, snapshot, acquired, actions = _scope(tmp_path)
    state["roles"]["PLAN"]["page_url"] = REGULAR
    hop["conversation_url"] = REGULAR
    snapshot.url = PROJECT
    snapshot.session_id = ID
    acquired = replace(acquired, url=PROJECT)
    actions.locate_owned.return_value = acquired

    result = asyncio.run(worker._owned_or_block(state, "PLAN", actions))

    assert result is acquired
    assert state["block_code"] is None
    assert hop["conversation_url"] == PROJECT
    assert state["roles"]["PLAN"]["page_url"] == PROJECT
    actions.reopen.assert_not_awaited()

    # A later noncanonical browser URL must not replace Project navigation.
    worker._record_acquired(state, "PLAN", replace(acquired, url=PROVISIONAL))
    assert state["roles"]["PLAN"]["page_url"] == PROJECT


def test_immediate_canonical_url_after_stop_enriches_ledger(tmp_path):
    state, worker, hop, _receipt, ledger, snapshot, acquired, actions = _scope(
        tmp_path, stop=True,
    )
    hop["conversation_url"] = REGULAR
    state["roles"]["PLAN"]["page_url"] = REGULAR
    snapshot.url = REGULAR
    snapshot.session_id = ID
    acquired = replace(acquired, url=REGULAR)
    actions.locate_owned.return_value = acquired

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is acquired
    assert ledger.get(hop["request_id"]).receipt["conversation_id"] == ID
    assert ledger.get(hop["request_id"]).attempts == 1
    assert hop["conversation_url"] == REGULAR
    actions.reopen.assert_not_awaited()


def test_different_canonical_uuid_cannot_replace_durable_saved_owner(tmp_path):
    state, worker, hop, receipt, ledger, snapshot, acquired, actions = _scope(
        tmp_path, stop=True,
    )
    state["roles"]["PLAN"]["page_url"] = REGULAR
    hop["conversation_url"] = REGULAR
    wrong = "22222222-2222-4222-8222-222222222222"
    snapshot.url = f"https://chatgpt.com/c/{wrong}"
    snapshot.session_id = wrong
    actions.locate_owned.return_value = replace(acquired, url=snapshot.url)

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "accepted_conversation_identity_unresolved"
    assert hop["conversation_url"] == REGULAR
    assert ledger.get(hop["request_id"]).receipt["conversation_id"] is None
    assert ledger.get(hop["request_id"]).attempts == 1
    actions.reopen.assert_not_awaited()


def test_stop_acceptance_without_rendered_user_can_canonicalize_project(tmp_path):
    state, worker, hop, receipt, ledger, snapshot, acquired, actions = _scope(
        tmp_path, stop=True,
    )
    snapshot.url = PROJECT
    snapshot.session_id = ID
    acquired = replace(acquired, url=PROJECT)
    actions.locate_owned.return_value = acquired

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is acquired
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["conversation_url"] == PROJECT
    assert state["roles"]["PLAN"]["page_url"] == PROJECT
    assert ledger.get(hop["request_id"]).receipt["conversation_id"] == ID
    assert ledger.get(hop["request_id"]).attempts == 1
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_transient_chatgpt_root_after_local_send_is_wait_not_identity_block(tmp_path):
    state, worker, hop, _receipt, ledger, snapshot, acquired, actions = _scope(
        tmp_path, stop=True,
    )
    snapshot.url = "https://chatgpt.com/"
    snapshot.session_id = None
    acquired = replace(acquired, url=snapshot.url)
    actions.locate_owned.return_value = acquired

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_accepted_conversation_identity"
    assert state["block_code"] is None
    hop["wait"]["accepted_identity_deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is acquired
    assert state["status"] == "RUNNING"
    assert ledger.get(hop["request_id"]).attempts == 1


def test_still_provisional_after_ten_seconds_can_observe_response(tmp_path):
    state, worker, hop, _receipt, ledger, _snap, acquired, actions = _scope(
        tmp_path, stop=True,
    )
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    now = datetime.now(timezone.utc)
    hop["wait"]["accepted_identity_deadline_at"] = (now - timedelta(seconds=1)).isoformat()

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is acquired
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert hop["wait"]["accepted_identity_outcome"] == "observe_response_without_url"
    assert ledger.get(hop["request_id"]).attempts == 1
    actions.reopen.assert_not_awaited()


def test_provisional_without_response_after_ten_seconds_pauses_no_replay(tmp_path):
    state, worker, hop, _receipt, ledger, _snap, acquired, actions = _scope(tmp_path)
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    hop["wait"]["accepted_identity_deadline_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] == "PAUSED"
    assert state["block_code"] is None
    assert "no canonical conversation URL" in state["pause_reason"]
    assert hop["wait"]["accepted_identity_outcome"] == "paused_unresolved"
    assert ledger.get(hop["request_id"]).attempts == 1
    actions.reopen.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_receipt_canonicalization_keeps_project_url_when_plain_url_observed(tmp_path):
    state, worker, hop, receipt, _ledger, *_ = _scope(tmp_path)
    state["roles"]["PLAN"]["page_url"] = PROJECT
    hop["conversation_url"] = REGULAR
    hop["receipt"] = replace(receipt, conversation_id=ID).to_dict()

    worker._canonicalize_receipt_conversation_url(state, hop)

    assert hop["conversation_url"] == PROJECT
    assert state["roles"]["PLAN"]["page_url"] == PROJECT


def test_atomic_browser_send_allows_project_redirect_same_id_not_foreign_id(monkeypatch):
    import pytest
    from playwright.async_api import async_playwright
    import playwright_auto.chatgpt as chatgpt

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", noop)
    monkeypatch.setattr(chatgpt, "record_page_action", noop)
    html = """
        <main>
          <form onsubmit="event.preventDefault();window.__sends=(window.__sends||0)+1">
            <div id="prompt-textarea" role="textbox" contenteditable="true">test prompt</div>
            <button type="submit" data-testid="send-button">Send</button>
          </form>
        </main>
    """

    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def serve(route):
                    await route.fulfill(status=200, content_type="text/html", body=html)

                await page.route("https://chatgpt.com/**", serve)
                await page.goto(REGULAR)
                await page.evaluate("""() => {
                    window.name = '__PLAYWRIGHT_AUTO_BINDING__:' + JSON.stringify({
                        role: 'DEV', pageId: 'page-1',
                        taskId: null, team: null
                    });
                }""")
                await page.evaluate(
                    "(url) => history.replaceState(null, '', url)", PROJECT
                )
                assert await chatgpt.click_send_button(
                    page, timeout_ms=1000, expected_url=REGULAR,
                    expected_page_id="page-1", expected_role="DEV",
                    expected_prompt="test prompt",
                ) == "dom_click"
                assert await page.evaluate("window.__sends") == 1

                other = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222"
                await page.evaluate("(url) => history.replaceState(null, '', url)", other)
                with pytest.raises(chatgpt.PageOwnershipError, match="conversation_changed"):
                    await chatgpt.click_send_button(
                        page, timeout_ms=1000, expected_url=REGULAR,
                        expected_page_id="page-1", expected_role="DEV",
                        expected_prompt="test prompt",
                    )
                assert await page.evaluate("window.__sends") == 1
            finally:
                await browser.close()

    asyncio.run(exercise())
