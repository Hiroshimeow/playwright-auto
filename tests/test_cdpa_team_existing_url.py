"""Team role URL attachment: durable identity and exact-tab adoption."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, BrowserChallengeError, CDPATabActions, RoleOwnershipError
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
    {"role_conversation_urls": {"REVIEW": "https://chatgpt.com/c/not-a-uuid"}},
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


def test_team_reservation_rejects_existing_solo_url(tmp_path):
    _config, store, _team, _worker = _task(tmp_path)
    solo_url = "https://chatgpt.com/c/33333333-3333-4333-8333-333333333333"
    store.create_task(
        "Solo owner", task_id="solo-owner", requested_team="solo-owner",
        workflow_mode="solo", roles=("REVIEW",),
        browser_endpoint_id="legacy", conversation_url=solo_url,
    )
    with pytest.raises(ValueError, match="already reserved"):
        store.create_task(
            "Team duplicate", task_id="team-duplicate", requested_team="team-duplicate",
            workflow_mode="team", roles=("PLAN", "REVIEW"),
            browser_endpoint_id="legacy",
            governing_objective="Team duplicate", governing_directives=[],
            role_conversation_urls={"PLAN": solo_url},
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


def test_later_team_role_adopts_its_own_preassigned_url(tmp_path):
    _config, _store, state, worker = _task(tmp_path)
    hop = worker._append_hop(
        state, source_role="REVIEW", target_role="PLAN",
        handoff="Continue via the PLAN conversation.",
    )
    acquired = AcquiredRole(
        client=SimpleNamespace(), page_id="plan-page", url=URL2,
        created=False, new_chat=False,
    )
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        adopt_conversation=AsyncMock(return_value=acquired),
        fresh_chat=AsyncMock(),
        acquire=AsyncMock(),
    )
    worker.role_controller.run = AsyncMock(
        return_value=SimpleNamespace(reason="response_in_progress")
    )
    asyncio.run(worker._pre_send(state, hop, actions))
    assert actions.adopt_conversation.await_args.kwargs["url"] == URL2
    assert state["roles"]["PLAN"]["page_url"] == URL2
    actions.fresh_chat.assert_not_awaited()


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


def test_manual_draft_prevents_existing_tab_takeover(tmp_path, monkeypatch):
    from playwright_auto.cdpa_actions import ComposerConflictError

    config, _store, state, _worker = _task(tmp_path)
    page = SimpleNamespace(url=URL1, is_closed=lambda: False)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)
    workspace = SimpleNamespace(bind=AsyncMock(), open_role=AsyncMock())
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", lambda: workspace)
    monkeypatch.setattr(
        actions_module, "inspect_page_metadata",
        AsyncMock(return_value={"team": None, "task_id": None}),
    )
    monkeypatch.setattr(
        actions_module, "ChatGPTPage",
        lambda *_a, **_kw: SimpleNamespace(
            snapshot=AsyncMock(return_value=SimpleNamespace(
                manual_input_pending=True, composer_text="Don't delete my draft", stop_visible=False,
            )),
        ),
    )
    with pytest.raises(ComposerConflictError, match="manual draft"):
        asyncio.run(actions.adopt_conversation(state, "REVIEW", url=URL1))
    workspace.bind.assert_not_awaited()
    workspace.open_role.assert_not_awaited()


def test_browser_adoption_reuses_same_unowned_tab_without_navigation(tmp_path):
    from playwright.async_api import async_playwright

    config, _store, state, _worker = _task(tmp_path)
    html = """
    <main>
      <form onsubmit="event.preventDefault()">
        <div id="prompt-textarea" role="textbox" contenteditable="true"></div>
        <button type="submit" data-testid="send-button">Send</button>
      </form>
    </main>
    """

    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()

                async def serve(route):
                    await route.fulfill(status=200, content_type="text/html", body=html)

                await page.route("https://chatgpt.com/**", serve)
                await page.goto(URL1)
                original_page_count = len(context.pages)
                actions = CDPATabActions(context, config)
                adopted = await actions.adopt_conversation(state, "REVIEW", url=URL1)
                assert adopted.created is False
                assert adopted.client.page is page
                assert len(context.pages) == original_page_count
                snapshot = await adopted.client.assert_ownership()
                assert snapshot.page_task_id == state["task_id"]
                assert snapshot.page_team == state["team"]
                assert snapshot.page_role == state["roles"]["REVIEW"]["physical_role"]
                assert snapshot.url == URL1
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_team_url_api_normalization_and_rejections(tmp_path):
    from playwright_auto.dashboard_api import APIError, DashboardAPI

    config, _store, _state, _worker = _task(tmp_path)
    api = DashboardAPI(config)
    base = {
        "workflow_mode": "team",
        "task": "Continue accepted conversation",
        "governing_objective": "Continue accepted conversation",
        "governing_directives": [],
        "roles": ["PLAN", "REVIEW"],
        "initial_role": "REVIEW",
        "browser_endpoint_id": "9222",
        "role_conversation_urls": {"REVIEW": URL1, "PLAN": URL2},
    }
    normalized = api.normalize_create(base)
    assert normalized["initial_role"] == "REVIEW"
    assert normalized["role_conversation_urls"] == base["role_conversation_urls"]
    for invalid in (
        {**base, "role_conversation_urls": {"REVIEW": "https://example.org/"}},
        {**base, "role_conversation_urls": {"REVIEW": "https://chatgpt.com/"}},
        {**base, "role_conversation_urls": {"REVIEW": "https://chatgpt.com/c/not-a-uuid"}},
        {**base, "browser_endpoint_id": None},
        {**base, "temporary": True},
        {**base, "workflow_mode": "solo", "role": "REVIEW"},
    ):
        with pytest.raises(APIError):
            api.normalize_create(invalid)


def test_team_role_urls_pass_through_cli_submission(tmp_path, monkeypatch):
    import playwright_auto.cdpa_cli as cli
    posted = []
    monkeypatch.setattr(
        cli, "_post",
        lambda _config, path, payload, **_kwargs:
            posted.append((path, payload)) or {"status": "queued"},
    )
    config = SimpleNamespace(repository_root=tmp_path.resolve())
    cli.submit_task(
        config,
        task="Continue role discussion", repository=tmp_path,
        team="existing-url-team", new_roles=(), new_all=False,
        browser_endpoint_id="9222", initial_role="REVIEW",
        role_conversation_urls={"REVIEW": URL1, "PLAN": URL2},
        governing_objective="Continue role discussion", governing_directives=[],
    )
    assert posted[0][0] == "/api/tasks"
    assert posted[0][1]["initial_role"] == "REVIEW"
    assert posted[0][1]["role_conversation_urls"]["PLAN"] == URL2


def test_team_role_url_cli_flags_forward_exact_mapping(tmp_path, monkeypatch):
    import playwright_auto.cdpa_cli as cli

    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    seen = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_cdpa_config", lambda *_a, **_kw: config)
    monkeypatch.setattr(
        cli, "submit_task",
        lambda _config, **kw: seen.append(kw) or {"status": "queued"},
    )
    assert cli.main([
        "Continue existing discussion",
        "--governing-objective", "Continue existing discussion",
        "--browser", "9222",
        "--start-role", "REVIEW",
        "--role-url", f"REVIEW={URL1}",
        "--role-url", f"PLAN={URL2}",
    ]) == 0
    assert seen[0]["initial_role"] == "REVIEW"
    assert seen[0]["role_conversation_urls"] == {"REVIEW": URL1, "PLAN": URL2}


def test_team_create_ui_exposes_initial_role_and_exact_role_url_fields():
    from pathlib import Path

    html = Path("src/playwright_auto/dashboard.html").read_text(encoding="utf-8")
    app = Path("src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")
    assert 'name="initial_role"' in html
    assert 'name="initial_role_url"' in html
    assert 'name="role_url_lines"' in html
    assert "body.role_conversation_urls = roleUrls" in app
    assert "body.initial_role = startRole" in app

@pytest.mark.parametrize('fault,expected', [
    (BrowserChallengeError('confirmed Cloudflare'), 'BROWSER_CHALLENGE'),
    (TimeoutError('saved canonical unavailable'), 'SESSION_UNAVAILABLE'),
    (RoleOwnershipError('saved canonical redirected', code='conversation_identity_unavailable'), 'SESSION_UNAVAILABLE'),
])
def test_team_preassigned_url_recovery_typed_pause(tmp_path, fault, expected):
    _config, _store, state, worker = _task(tmp_path)
    hop = _active_hop(state)
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        adopt_conversation=AsyncMock(side_effect=fault),
        reopen=AsyncMock(), fresh_chat=AsyncMock(), acquire=AsyncMock(),
    )
    asyncio.run(worker._pre_send(state, hop, actions))
    assert state['status'] == 'PAUSED'
    assert state['block_code'] == expected
    assert hop['conversation_url'] == URL1
    assert state['roles']['REVIEW']['page_url'] == URL1
    actions.fresh_chat.assert_not_awaited()


def test_team_saved_url_reopen_identity_unavailable_pauses_without_fresh(tmp_path):
    _config, _store, state, worker = _task(tmp_path)
    hop = _active_hop(state)
    state['roles']['REVIEW']['page_id'] = 'old-page'
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        reopen=AsyncMock(side_effect=RoleOwnershipError(
            'saved canonical redirected', code='conversation_identity_unavailable',
        )),
        adopt_conversation=AsyncMock(), fresh_chat=AsyncMock(), acquire=AsyncMock(),
    )
    asyncio.run(worker._pre_send(state, hop, actions))
    assert state['status'] == 'PAUSED'
    assert state['block_code'] == 'SESSION_UNAVAILABLE'
    assert hop['conversation_url'] == URL1
    assert state['roles']['REVIEW']['page_url'] == URL1
    actions.adopt_conversation.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()


def test_team_preassigned_foreign_owner_error_is_not_downgraded_to_session_unavailable(tmp_path):
    _config, _store, state, worker = _task(tmp_path)
    hop = _active_hop(state)
    actions = SimpleNamespace(
        preflight_role=AsyncMock(return_value=[]),
        adopt_conversation=AsyncMock(side_effect=RoleOwnershipError(
            'requested conversation tab is already task-owned',
            code='conversation_owned_by_other_task',
        )),
        reopen=AsyncMock(), fresh_chat=AsyncMock(), acquire=AsyncMock(),
    )
    with pytest.raises(RoleOwnershipError) as caught:
        asyncio.run(worker._pre_send(state, hop, actions))
    assert caught.value.code == 'conversation_owned_by_other_task'
    assert state['status'] != 'PAUSED'
    actions.fresh_chat.assert_not_awaited()
