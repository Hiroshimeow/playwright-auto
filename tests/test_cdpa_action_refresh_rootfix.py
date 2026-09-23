from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageSnapshot,
    PageBinding,
)

from test_cdpa_core import write_config


class RefreshPage(SimpleNamespace):
    __hash__ = object.__hash__


def _snapshot(*, task_id: str = "task-1", composer_text: str = "") -> ChatGPTSnapshot:
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/conversation-1",
        session_id="conversation-1",
        page_id="page-plan",
        page_role="PLAN",
        page_task_id=task_id,
        page_team="team-1",
        state=ChatGPTState.NEW_CHAT,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=composer_text,
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=(MessageSnapshot("assistant", "a-1", "t-1", "ready", ()),),
    )


def _manifest() -> dict[str, object]:
    return {
        "task_id": "task-1",
        "team": "team-1",
        "roles": {
            "PLAN": {
                "physical_role": "PLAN",
                "page_id": "page-plan",
                "page_url": "https://chatgpt.com/c/conversation-1",
            }
        },
    }


def _actions(tmp_path, page) -> CDPATabActions:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    config = replace(config, workspace_timeout_seconds=0.1)
    return CDPATabActions(SimpleNamespace(pages=[page]), config)


def _acquired(page, snapshots) -> AcquiredRole:
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = PageBinding("page-plan", "PLAN")
    client.assert_ownership = AsyncMock(side_effect=snapshots)
    client.send = AsyncMock()
    return AcquiredRole(
        client=client,
        page_id="page-plan",
        url="https://chatgpt.com/c/conversation-1",
        created=False,
        new_chat=False,
    )


async def _no_delay(*_args, **_kwargs):
    return None


def test_action_refresh_accepts_one_timed_out_reload_only_after_exact_new_document_commit(
    tmp_path, monkeypatch
):
    before = _snapshot()
    after = _snapshot()
    reload_error = TimeoutError("reload timed out after commit")
    page = RefreshPage(
        reload=AsyncMock(side_effect=reload_error),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
                {"url": after.url, "time_origin": 200.0, "ready_state": "interactive"},
            ]
        ),
    )
    acquired = _acquired(page, [before, before, after, after])
    monkeypatch.setattr(actions_module, "action_delay", _no_delay)

    result = asyncio.run(
        _actions(tmp_path, page).refresh(
            acquired,
            manifest=_manifest(),
            logical_role="PLAN",
            recover=True,
        )
    )

    assert result is acquired
    assert page.reload.await_count == 1
    acquired.client.send.assert_not_awaited()


@pytest.mark.parametrize("failure", ["unchanged_document", "identity_mismatch"])
def test_action_refresh_rethrows_original_timeout_without_second_reload_or_send(
    tmp_path, monkeypatch, failure
):
    before = _snapshot()
    after = _snapshot(task_id="other-task") if failure == "identity_mismatch" else before
    after_origin = 200.0 if failure == "identity_mismatch" else 100.0
    reload_error = TimeoutError("original reload timeout")
    page = RefreshPage(
        reload=AsyncMock(side_effect=reload_error),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
                {"url": after.url, "time_origin": after_origin, "ready_state": "complete"},
            ]
        ),
    )
    acquired = _acquired(page, [before, before, after])
    monkeypatch.setattr(actions_module, "action_delay", _no_delay)

    with pytest.raises(TimeoutError, match="original reload timeout") as caught:
        asyncio.run(
            _actions(tmp_path, page).refresh(
                acquired,
                manifest=_manifest(),
                logical_role="PLAN",
                recover=True,
            )
        )

    assert caught.value is reload_error
    assert page.reload.await_count == 1
    acquired.client.send.assert_not_awaited()


def test_action_refresh_manual_draft_prevents_reload(tmp_path, monkeypatch):
    draft = _snapshot(composer_text="operator draft")
    page = RefreshPage(reload=AsyncMock(), evaluate=AsyncMock())
    acquired = _acquired(page, [draft, draft])
    monkeypatch.setattr(actions_module, "action_delay", _no_delay)

    with pytest.raises(ComposerConflictError, match="manual draft"):
        asyncio.run(_actions(tmp_path, page).refresh(acquired))

    page.reload.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    acquired.client.send.assert_not_awaited()
