"""Regression for the observed Solo PAUSE -> Resume lost-URL incident."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import setup_task


CID = "6acb02f9-751c-83ee-8b7b-b46fd4ea6235"
PLAIN = f"https://chatgpt.com/c/{CID}"
PROJECT = f"https://chatgpt.com/g/g-p-project/c/{CID}"


def paused_solo(tmp_path, *, parent_url=PLAIN, role_url=PROJECT, receipt_id=CID, page_id="owned-page"):
    _config, _store, state, worker = setup_task(tmp_path, task_id="solo-url-resume")
    state["workflow_mode"] = "solo"
    state["conversation_mode"] = "persistent"
    state["context_policy"] = "fresh"
    state["status"] = "PAUSED"
    state["kanban_column"] = "PAUSED"
    role = state["roles"]["PLAN"]
    role.update(page_id=page_id, page_url=role_url, conversation_generation=1)
    parent = _active_hop(state)
    parent.update(
        state="routed", route="PAUSE", routed_handoff="Continue on the same conversation.",
        conversation_url=parent_url,
        receipt={"conversation_id": receipt_id} if receipt_id else None,
    )
    state["controls"] = [
        {"control_id": 1, "action": "resume", "role": "PLAN",
         "origin": "operator", "reason": "continue", "status": "requested"}
    ]
    return state, worker, parent


@pytest.mark.parametrize("existing_page", [True, False])
def test_operator_resume_reuses_project_url_even_with_fresh_context(tmp_path, existing_page):
    state, worker, parent = paused_solo(
        tmp_path, parent_url=PLAIN, role_url=PROJECT,
        page_id="owned-page" if existing_page else None,
    )
    old_generation = state["roles"]["PLAN"]["conversation_generation"]
    assert asyncio.run(worker._apply_control(state, SimpleNamespace())) is True
    child = _active_hop(state)
    assert child["hop_id"] == parent["hop_id"] + 1
    assert child["kind"] == "solo_resume"
    assert child["conversation_url"] == PROJECT
    assert state["roles"]["PLAN"]["page_url"] == PROJECT
    assert state["roles"]["PLAN"]["conversation_generation"] == old_generation
    assert state["controls"][0]["status"] == "applied"

    acquired = AcquiredRole(
        client=SimpleNamespace(), page_id="owned-page", url=PROJECT,
        created=not existing_page, new_chat=False,
    )
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        reopen=AsyncMock(return_value=acquired),
        adopt_conversation=AsyncMock(return_value=acquired),
        fresh_chat=AsyncMock(),
        new_chat=AsyncMock(),
        acquire=AsyncMock(),
    )
    worker.role_controller.run = AsyncMock(
        return_value=SimpleNamespace(reason="response_in_progress")
    )
    asyncio.run(worker._pre_send(state, child, actions))
    assert child["conversation_url"] == PROJECT
    assert state["roles"]["PLAN"]["conversation_generation"] == old_generation
    assert child["state"] == "pre_send"  # no new Send while response still active
    actions.fresh_chat.assert_not_awaited()
    actions.new_chat.assert_not_awaited()
    actions.acquire.assert_not_awaited()
    if existing_page:
        actions.reopen.assert_awaited_once()
        actions.adopt_conversation.assert_not_awaited()
    else:
        actions.adopt_conversation.assert_awaited_once()
        assert actions.adopt_conversation.await_args.kwargs["url"] == PROJECT
        actions.reopen.assert_not_awaited()


def test_operator_resume_uses_receipt_id_when_page_urls_are_not_canonical(tmp_path):
    state, worker, _parent = paused_solo(
        tmp_path, parent_url="https://chatgpt.com/",
        role_url="https://chatgpt.com/", receipt_id=CID,
    )
    assert asyncio.run(worker._apply_control(state, SimpleNamespace())) is True
    child = _active_hop(state)
    assert child["conversation_url"] == PLAIN
    assert state["roles"]["PLAN"]["page_url"] == PLAIN
    assert child["kind"] == "solo_resume"


def test_operator_resume_never_allocates_a_new_chat_without_saved_id(tmp_path):
    state, worker, _parent = paused_solo(
        tmp_path, parent_url=None, role_url="https://chatgpt.com/", receipt_id=None,
    )
    assert asyncio.run(worker._apply_control(state, SimpleNamespace())) is True
    assert len(state["hops"]) == 1
    assert state["status"] == "PAUSED"
    assert state["controls"][0]["status"] in {"rejected", "ineffective"}


def test_real_solo_resume_keeps_url_through_manifest_reload(tmp_path):
    from pathlib import Path

    _config, store, _unrelated, worker = setup_task(
        tmp_path, task_id="unrelated-solo-hotfix-setup"
    )
    state = store.create_task(
        "Inspect current code", task_id="real-solo-pause-url",
        requested_team="real-solo-pause-url",
        roles=("REVIEW",), workflow_mode="solo", goal="Inspect current code",
        browser_endpoint_id="legacy",
    )
    parent = _active_hop(state)
    parent.update(
        state="routed", route="PAUSE",
        routed_handoff="Continue review without a new conversation.",
        conversation_url=PLAIN,
    )
    state["roles"]["REVIEW"].update(
        page_id="prior-page", page_url=PROJECT, conversation_generation=1,
    )
    state["status"] = "PAUSED"
    state["kanban_column"] = "PAUSED"
    path = Path(state["manifest_path"])
    store.save(path, state)
    resumed = store.request_resume(
        path, reason="continue after review pause",
        external_command_id="real-solo-hotfix-resume",
    )
    assert asyncio.run(worker._apply_control(resumed, SimpleNamespace(), path)) is True
    persisted = store.load(path)
    child = _active_hop(persisted)
    assert child["kind"] == "solo_resume"
    assert child["conversation_url"] == PROJECT
    assert persisted["roles"]["REVIEW"]["page_url"] == PROJECT
    assert persisted["roles"]["REVIEW"]["conversation_generation"] == 1
    assert persisted["controls"][-1]["status"] == "applied"


def test_team_role_pause_is_not_reinterpreted_as_solo_resume(tmp_path):
    state, worker, _parent = paused_solo(tmp_path)
    state["workflow_mode"] = "team"
    assert asyncio.run(worker._apply_control(state, SimpleNamespace())) is True
    assert _active_hop(state)["kind"] == "handoff"
