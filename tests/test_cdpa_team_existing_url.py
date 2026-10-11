"""Team role URL attachment: durable identity and exact-tab adoption."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions, RoleOwnershipError
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import setup_task


CID1 = "11111111-1111-4111-8111-111111111111"
CID2 = "22222222-2222-4222-8222-222222222222"
URL1 = f"https://chatgpt.com/g/g-p-existing-project/c/{CID1}"
URL2 = f"https://chatgpt.com/c/{CID2}"


def _task(tmp_path, **overrides):
    config, store, _unused, worker = setup_task(tmp_path, task_id="unused-team-setup")
    params = {
        "task_id": "team-from-chat",
        "requested_team": "team-from-chat",
        "roles": ("PLAN", "REVIEW", "DEV"),
        "workflow_mode": "team",
        "governing_objective": "Review and implement work from existing conversation",
        "governing_directives": [],
        "browser_endpoint_id": "legacy",
        "initial_role": "REVIEW",
        "role_conversation_urls": {"REVIEW": URL1, "PLAN": URL2},
    }
    params.update(overrides)
    return config, store, store.create_task("Continue agreed work", **params), worker


def test_durable_team_start_can_use_any_selected_role_url(tmp_path):
    _config, store, state, _worker = _task(tmp_path)
    first = _active_hop(state)
    assert first["target_role"] == "REVIEW"
    assert first["conversation_url"] == URL1
    assert state["roles"]["REVIEW"]["page_url"] == URL1
    assert state["roles"]["PLAN"]["page_url"] == URL2
    assert state["roles"]["DEV"]["page_url"] is None
    assert state["roles"]["REVIEW"]["page_id"] is None
    loaded = store.load(state["manifest_path"])
    assert loaded["roles"]["PLAN"]["page_url"] == URL2
    assert loaded["hops"][0]["conversation_url"] == URL1


@pytest.mark.parametrize("extra", [
    {"role_conversation_urls": {"REVIEW": "https://chatgpt.com/"}},
    {"role_conversation_urls": {"REVIEW": URL1, "PLAN": URL1}},
    {"role_conversation_urls": {"AUDIT": URL1}},
    {"initial_role": "AUDIT"},
    {"browser_endpoint_id": None},
    {"conversation_mode": "temporary"},
    {"new_roles": ("REVIEW",)},
])
def test_reject_invalid_or_conflicting_team_url_mapping(tmp_path, extra):
    _config, store, _state, _worker = _task(tmp_path)
    kwargs = dict(
        task_id="invalid-mapping",
        requested_team="invalid-mapping",
        roles=("PLAN", "REVIEW"),
        governing_objective="Attach existing conversation",
        governing_directives=[],
        browser_endpoint_id="legacy",
        role_conversation_urls={"REVIEW": URL1},
    )
    kwargs.update(extra)
    with pytest.raises(ValueError):
        store.create_task("Attach existing conversation", **kwargs)


def test_durable_reservation_rejects_second_active_owner(tmp_path):
    _config, store, owner, _worker = _task(tmp_path)
    assert owner["roles"]["REVIEW"]["page_id"] is None
    with pytest.raises(ValueError, match="already reserved"):
        store.create_task(
            "Second task", task_id="url-conflict", requested_team="url-conflict",
            roles=("PLAN", "REVIEW"), browser_endpoint_id="legacy",
            role_conversation_urls={"PLAN": URL1},
            governing_objective="Second task", governing_directives=[],
        )


def test_team_adopts_url_on_first_role_without_fresh_chat(tmp_path):
    _config, _store, state, worker = _task(tmp_path)
    hop = _active_hop(state)
    acquired = AcquiredRole(
        client=SimpleNamespace(), page_id="existing-page", url=URL1,
        created=False, new_chat=False,
    )
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        adopt_conversation=AsyncMock(return_value=acquired),
        fresh_chat=AsyncMock(),
        acquire=AsyncMock(),
        locate_owned=AsyncMock(),
    )
    worker.role_controller.run = AsyncMock(
        return_value=SimpleNamespace(reason="response_in_progress")
    )
    asyncio.run(worker._pre_send(state, hop, actions))
    actions.adopt_conversation.assert_awaited_once()
    assert actions.adopt_conversation.await_args.kwargs["url"] == URL1
    actions.fresh_chat.assert_not_awaited()
    actions.acquire.assert_not_awaited()
    assert state["roles"]["REVIEW"]["page_url"] == URL1
    assert state["roles"]["REVIEW"]["page_id"] == "existing-page"
    assert state["roles"]["REVIEW"]["conversation_generation"] == 0
    assert hop["state"] == "waiting"


def test_existing_unowned_browser_tab_is_claimed_not_duplicated(tmp_path, monkeypatch):
    config, _store, state, _worker = _task(tmp_path)
    page = SimpleNamespace(url=URL1, is_closed=lambda: False)
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    actions = CDPATabActions(context, config)
    actions._set_page_active = AsyncMock()
    fake = SimpleNamespace(
        page=page,
        binding=SimpleNamespace(page_id="adopted-page"),
        assert_ownership=AsyncMock(return_value=SimpleNamespace(url=URL1)),
        bind_task_identity=AsyncMock(),
    )
    workspace = SimpleNamespace(
        bind=AsyncMock(return_value=fake),
        open_role=AsyncMock(),
    )
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", lambda: workspace)
    monkeypatch.setattr(
        actions_module, "inspect_page_metadata",
        AsyncMock(return_value={"team": None, "task_id": None, "role": None}),
    )
    monkeypatch.setattr(
        actions_module, "ChatGPTPage",
        lambda *_args, **_kwargs: SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(
                manual_input_pending=False, composer_text="", stop_visible=False,
            ))
        ),
    )
    acquired = asyncio.run(actions.adopt_conversation(state, "REVIEW", url=URL1))
    assert acquired.created is False
    assert acquired.url == URL1
    workspace.bind.assert_awaited_once()
    workspace.open_role.assert_not_awaited()
    context.new_page.assert_not_awaited()
    fake.bind_task_identity.assert_awaited_once()


def test_existing_foreign_owned_tab_is_never_rebound(tmp_path, monkeypatch):
    config, _store, state, _worker = _task(tmp_path)
    page = SimpleNamespace(url=URL1, is_closed=lambda: False)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)
    workspace = SimpleNamespace(bind=AsyncMock(), open_role=AsyncMock())
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", lambda: workspace)
    monkeypatch.setattr(
        actions_module, "inspect_page_metadata",
        AsyncMock(return_value={"team": "foreign", "task_id": "foreign-task"}),
    )
    with pytest.raises(RoleOwnershipError, match="already task-owned"):
        asyncio.run(actions.adopt_conversation(state, "REVIEW", url=URL1))
    workspace.bind.assert_not_awaited()
    workspace.open_role.assert_not_awaited()
