from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
import playwright_auto.workspace as workspace_module
from playwright_auto.cdpa_actions import (
    AcquiredRole,
    CDPATabActions,
    RoleOwnershipError,
    TeamCloseError,
)
from playwright_auto.cdpa_config import load_cdpa_config

from test_cdpa_core import write_config


class FakeLocator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None


class FakePage:
    def __init__(self, *, page_id, role, team, task_id=None):
        self.url = "https://chatgpt.com/c/test"
        self.closed = False
        self.front = False
        self.goto_calls = []
        self.snapshot_value = SimpleNamespace(
            page_id=page_id,
            page_role=role,
            page_team=team,
            page_task_id=task_id,
            composer_text="",
            url=self.url,
        )

    def is_closed(self):
        return self.closed

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        error = getattr(self, "goto_error", None)
        if error is not None:
            raise error
        self.url = url
        self.snapshot_value.url = url

    def locator(self, _selector):
        return FakeLocator()

    async def bring_to_front(self):
        self.front = True

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, page, *, timeout_ms=0):
        self.page = page
        self.timeout_ms = timeout_ms
        self.binding = None
        self.preflight_calls = []
        self.prepare_calls = []
        self.bind_calls = []
        self.clean_ready_calls = []
        self.set_role_calls = []
        self.events = []
        self.send_calls = []

    async def snapshot(self):
        error = getattr(self.page, "snapshot_error", None)
        if error is not None:
            raise error
        return self.page.snapshot_value

    async def assert_ownership(self):
        return self.page.snapshot_value

    async def restore_identity(self, *, page_id, role, task_id, team):
        self.binding = SimpleNamespace(page_id=page_id, role=role)
        self.page.snapshot_value.page_id = page_id
        self.page.snapshot_value.page_role = role
        self.page.snapshot_value.page_task_id = task_id
        self.page.snapshot_value.page_team = team
        return self.page.snapshot_value

    async def set_role(self, role, *, allow_rebind=False, force_new_page_id=False):
        self.set_role_calls.append((role, allow_rebind, force_new_page_id))
        page_id = (
            f"fresh-page-{getattr(self.page, 'generated_index', 0)}"
            if force_new_page_id
            else (self.page.snapshot_value.page_id or "assigned-page")
        )
        self.binding = SimpleNamespace(page_id=page_id, role=role)
        self.page.snapshot_value.page_id = page_id
        self.page.snapshot_value.page_role = role
        return {"page_id": page_id, "page_role": role}

    async def task_preflight(self, task_id):
        self.preflight_calls.append(task_id)
        error = getattr(self.page, "preflight_error", None)
        if error is not None:
            raise error
        return {
            "task_id": task_id,
            "previous_task_id": self.page.snapshot_value.page_task_id,
            "requires_new_chat": self.page.snapshot_value.page_task_id != task_id,
        }

    async def wait_until_clean_ready(self, *, timeout_ms=None, **_kwargs):
        self.clean_ready_calls.append(timeout_ms)
        self.events.append("ready")
        error = getattr(self.page, "clean_ready_error", None)
        if error is not None:
            raise error
        return self.page.snapshot_value

    async def bind_task_identity(self, task_id, team):
        self.bind_calls.append((task_id, team))
        self.events.append("bind")
        error = getattr(self.page, "bind_error", None)
        if error is not None:
            raise error
        self.page.snapshot_value.page_task_id = task_id
        self.page.snapshot_value.page_team = team
        return {"task_id": task_id, "team": team}

    async def prepare_task(self, task_id, *, force_new_chat=False):
        self.prepare_calls.append((task_id, force_new_chat))
        reused = self.page.snapshot_value.page_task_id == task_id and not force_new_chat
        if not reused:
            self.page.url = "https://chatgpt.com/"
            self.page.snapshot_value.url = self.page.url
        return {"reused": reused}

    async def new_chat(self, *, expected_draft_text=None, **_kwargs):
        if expected_draft_text != self.page.snapshot_value.composer_text:
            raise RuntimeError("draft provenance mismatch")
        self.page.snapshot_value.composer_text = ""
        return "fake"

    async def send(self, *_args, **_kwargs):
        self.send_calls.append((_args, _kwargs))
        raise AssertionError("branch primitive must not Send")


class FakeCDPSession:
    def __init__(self, context, page):
        self.context = context
        self.page = page

    async def send(self, method, params):
        self.context.lifecycle_calls.append(
            (
                self.page,
                method,
                dict(params),
                self.page.snapshot_value.page_task_id,
                self.page.snapshot_value.page_team,
            )
        )
        if self.context.cdp_error is not None:
            raise self.context.cdp_error
        return {}

    async def detach(self):
        self.context.detached_sessions += 1


class FakeContext:
    def __init__(
        self,
        pages=None,
        *,
        draft_on_new="",
        cdp_error=None,
        goto_error=None,
        clean_ready_error=None,
        bind_error=None,
    ):
        self.pages = list(pages or [])
        self.draft_on_new = draft_on_new
        self.cdp_error = cdp_error
        self.goto_error = goto_error
        self.clean_ready_error = clean_ready_error
        self.bind_error = bind_error
        self.lifecycle_calls = []
        self.detached_sessions = 0
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        page = FakePage(page_id=None, role=None, team=None)
        page.generated_index = self.new_page_calls
        page.snapshot_value.composer_text = self.draft_on_new
        page.goto_error = self.goto_error
        page.clean_ready_error = self.clean_ready_error
        page.bind_error = self.bind_error
        self.pages.append(page)
        return page

    async def new_cdp_session(self, page):
        return FakeCDPSession(self, page)


class FakeWorkspace:
    async def open_role(
        self,
        context,
        role,
        *,
        url="https://chatgpt.com/",
        timeout_ms=0,
        force_new_page_id=False,
    ):
        page = await context.new_page()
        await page.goto(url)
        page_id = (
            f"fresh-page-{page.generated_index}"
            if force_new_page_id
            else "fresh-page"
        )
        page.snapshot_value.page_id = page_id
        page.snapshot_value.page_role = role
        client = FakeClient(page, timeout_ms=timeout_ms)
        client.binding = SimpleNamespace(page_id=page_id, role=role)
        return client


def manifest(
    *,
    team="new-team",
    task_id="task-1",
    physical_role="PLAN",
    page_id=None,
    page_url="https://chatgpt.com/c/test",
    conversation_url=None,
):
    hops = []
    active_hop_id = None
    if conversation_url:
        active_hop_id = 1
        hops = [
            {
                "hop_id": 1,
                "target_role": "PLAN",
                "conversation_url": conversation_url,
            }
        ]
    return {
        "task_id": task_id,
        "active_hop_id": active_hop_id,
        "hops": hops,
        "team": team,
        "reusable_teams": ["old-newest", "old-older"],
        "roles": {
            "PLAN": {
                "physical_role": physical_role,
                "page_id": page_id,
                "page_url": page_url,
            }
        },
    }


SOURCE_CONVERSATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ASSISTANT_MESSAGE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BRANCH_URL = (
    "https://chatgpt.com/branch/"
    f"{SOURCE_CONVERSATION_ID}/{ASSISTANT_MESSAGE_ID}"
)


def test_workspace_open_role_can_force_fresh_page_identity(monkeypatch):
    monkeypatch.setattr(workspace_module, "ChatGPTPage", FakeClient)
    context = FakeContext()
    workspace = workspace_module.ChatGPTWorkspace()

    client = asyncio.run(
        workspace.open_role(
            context,
            "PLAN",
            url=BRANCH_URL,
            timeout_ms=1234,
            force_new_page_id=True,
        )
    )

    assert context.new_page_calls == 1
    assert client.page.goto_calls == [BRANCH_URL]
    assert client.set_role_calls == [("PLAN", False, True)]
    assert client.binding.page_id == "fresh-page-1"


def test_branch_from_anchor_opens_native_route_and_binds_clean_fresh_target(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    context = FakeContext()
    actions = CDPATabActions(context, config)

    acquired = asyncio.run(
        actions.branch_from_anchor(
            manifest(physical_role="multi-bootstrap-branch-core-dev"),
            "PLAN",
            source_conversation_id=SOURCE_CONVERSATION_ID,
            assistant_message_id=ASSISTANT_MESSAGE_ID,
        )
    )

    assert context.new_page_calls == 1
    assert acquired.client.page.goto_calls == [BRANCH_URL]
    assert acquired.page_id == "fresh-page-1"
    assert acquired.created is True
    assert acquired.new_chat is True
    assert acquired.client.events == ["ready", "bind"]
    assert acquired.client.clean_ready_calls == [
        round(config.workspace_timeout_seconds * 1000)
    ]
    assert acquired.client.bind_calls == [("task-1", "new-team")]
    assert acquired.client.page.snapshot_value.page_role == "multi-bootstrap-branch-core-dev"
    assert acquired.client.page.snapshot_value.page_task_id == "task-1"
    assert acquired.client.page.snapshot_value.page_team == "new-team"
    assert acquired.client.send_calls == []


def test_branch_from_same_anchor_is_independent_of_closed_source_and_repeats_fresh(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    source = FakePage(
        page_id="source-page",
        role="bootstrap",
        team="bootstrap-team",
        task_id="bootstrap-task",
    )
    source.closed = True
    source_before = vars(source.snapshot_value).copy()
    context = FakeContext([source])
    actions = CDPATabActions(context, config)

    first = asyncio.run(
        actions.branch_from_anchor(
            manifest(team="team-a", task_id="task-a", physical_role="team-a-dev"),
            "PLAN",
            source_conversation_id=SOURCE_CONVERSATION_ID,
            assistant_message_id=ASSISTANT_MESSAGE_ID,
        )
    )
    second = asyncio.run(
        actions.branch_from_anchor(
            manifest(team="team-b", task_id="task-b", physical_role="team-b-review"),
            "PLAN",
            source_conversation_id=SOURCE_CONVERSATION_ID,
            assistant_message_id=ASSISTANT_MESSAGE_ID,
        )
    )

    assert source.closed is True
    assert vars(source.snapshot_value) == source_before
    assert context.new_page_calls == 2
    assert first.client.page is not second.client.page
    assert first.page_id != second.page_id
    assert first.client.page.goto_calls == second.client.page.goto_calls == [BRANCH_URL]
    assert first.client.page.snapshot_value.page_team == "team-a"
    assert second.client.page.snapshot_value.page_team == "team-b"


@pytest.mark.parametrize(
    ("source_conversation_id", "assistant_message_id"),
    [
        ("", ASSISTANT_MESSAGE_ID),
        ("WEB:temporary", ASSISTANT_MESSAGE_ID),
        (SOURCE_CONVERSATION_ID.upper(), ASSISTANT_MESSAGE_ID),
        (SOURCE_CONVERSATION_ID, "not-a-uuid"),
    ],
)
def test_branch_from_anchor_rejects_noncanonical_ids_before_new_page(
    tmp_path,
    monkeypatch,
    source_conversation_id,
    assistant_message_id,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    context = FakeContext()
    actions = CDPATabActions(context, config)

    with pytest.raises(actions_module.BranchBootstrapError, match="canonical UUID"):
        asyncio.run(
            actions.branch_from_anchor(
                manifest(),
                "PLAN",
                source_conversation_id=source_conversation_id,
                assistant_message_id=assistant_message_id,
            )
        )

    assert context.new_page_calls == 0


@pytest.mark.parametrize(
    "context_kwargs",
    [
        {"clean_ready_error": RuntimeError("not ready")},
        {"bind_error": RuntimeError("bind failed")},
    ],
)
def test_branch_from_anchor_wraps_post_navigation_failure_and_closes_only_new_branch(
    tmp_path, monkeypatch, context_kwargs
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    source = FakePage(
        page_id="source-page",
        role="bootstrap",
        team="bootstrap-team",
        task_id="bootstrap-task",
    )
    context = FakeContext([source], **context_kwargs)
    actions = CDPATabActions(context, config)

    with pytest.raises(actions_module.BranchBootstrapError, match="branch bootstrap"):
        asyncio.run(
            actions.branch_from_anchor(
                manifest(),
                "PLAN",
                source_conversation_id=SOURCE_CONVERSATION_ID,
                assistant_message_id=ASSISTANT_MESSAGE_ID,
            )
        )

    assert source.closed is False
    assert context.new_page_calls == 1
    assert context.pages[-1].closed is True


def test_branch_from_anchor_wraps_navigation_failure_after_workspace_closes_page(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(workspace_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    context = FakeContext(goto_error=RuntimeError("navigation failed"))
    actions = CDPATabActions(context, config)

    with pytest.raises(actions_module.BranchBootstrapError, match="navigation failed"):
        asyncio.run(
            actions.branch_from_anchor(
                manifest(),
                "PLAN",
                source_conversation_id=SOURCE_CONVERSATION_ID,
                assistant_message_id=ASSISTANT_MESSAGE_ID,
            )
        )

    assert context.new_page_calls == 1
    assert context.pages[-1].closed is True


def test_temporary_web_conversation_remains_non_reopenable():
    assert (
        actions_module._reopenable_conversation_identity(
            "https://chatgpt.com/c/WEB:12345678-1234-4234-8234-123456789abc"
        )
        is None
    )


def test_automatic_refresh_uses_reload_only_and_never_retry_control(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    events = []
    page = SimpleNamespace()

    class Client:
        def __init__(self):
            self.page = page

        async def assert_ownership(self):
            events.append("ownership")
            return SimpleNamespace()

    async def fake_delay(target, action, multiplier):
        assert target is page
        assert action == "refresh"
        assert multiplier > 0
        events.append("delay")

    async def fake_refresh(target, *, timeout_ms):
        assert target is page
        assert timeout_ms > 0
        events.append("reload")

    monkeypatch.setattr(actions_module, "action_delay", fake_delay)
    monkeypatch.setattr(actions_module, "refresh_page", fake_refresh)
    actions = CDPATabActions(FakeContext(), config)
    acquired = AcquiredRole(
        client=Client(),
        page_id="page-plan",
        url="https://chatgpt.com/c/test",
        created=False,
        new_chat=False,
    )

    asyncio.run(actions.refresh(acquired))

    assert events == ["ownership", "delay", "reload", "ownership"]


def test_matching_clients_skips_free_tabs_and_prefers_latest_terminal_team(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [
        FakePage(page_id="free", role=None, team=None),
        FakePage(page_id="older", role="PLAN", team="old-older"),
        FakePage(page_id="newest", role="PLAN", team="old-newest"),
    ]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(FakeContext(pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(), "PLAN"))

    assert len(matches) == 1
    assert matches[0][1].page_id == "newest"
    assert matches[0][0].binding.page_id == "newest"


def test_locate_owned_metadata_never_falls_back_to_dom_snapshot(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    owned = FakePage(page_id="owned", role="PLAN", team="new-team", task_id="task-1")

    class NoSnapshotClient(FakeClient):
        async def snapshot(self):
            raise AssertionError("metadata-only locator must not take a DOM snapshot")

    async def fake_metadata(page):
        snap = page.snapshot_value
        return {
            "page_id": snap.page_id,
            "role": snap.page_role,
            "team": snap.page_team,
            "task_id": snap.page_task_id,
            "url": snap.url,
        }

    monkeypatch.setattr(actions_module, "ChatGPTPage", NoSnapshotClient)
    monkeypatch.setattr(actions_module, "inspect_page_metadata", fake_metadata)
    context = FakeContext([owned])
    actions = CDPATabActions(context, config)

    acquired = asyncio.run(actions.locate_owned_metadata(manifest(page_id="owned"), "PLAN"))

    assert acquired is not None
    assert acquired.page_id == "owned"
    assert acquired.url == owned.url
    assert context.lifecycle_calls == []


def test_locate_owned_sets_active_lifecycle_on_only_the_selected_page(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    other = FakePage(page_id="other", role="PLAN", team="other-team", task_id="task-1")
    owned = FakePage(page_id="owned", role="PLAN", team="new-team", task_id="task-1")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    context = FakeContext([other, owned])
    actions = CDPATabActions(context, config)

    acquired = asyncio.run(actions.locate_owned(manifest(page_id="owned"), "PLAN"))

    assert acquired is not None
    assert acquired.page_id == "owned"
    assert owned.front is False
    assert other.front is False
    assert context.lifecycle_calls == [
        (owned, "Page.setWebLifecycleState", {"state": "active"}, "task-1", "new-team"),
        (owned, "Emulation.setFocusEmulationEnabled", {"enabled": True}, "task-1", "new-team"),
    ]
    assert context.detached_sessions == 1


def test_lifecycle_command_failure_is_soft_and_warns_once(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    owned = FakePage(page_id="owned", role="PLAN", team="new-team", task_id="task-1")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    context = FakeContext([owned], cdp_error=RuntimeError("unsupported command"))
    actions = CDPATabActions(context, config)

    with pytest.warns(RuntimeWarning, match="lifecycle") as warnings:
        first = asyncio.run(actions.locate_owned(manifest(page_id="owned"), "PLAN"))
        second = asyncio.run(actions.locate_owned(manifest(page_id="owned"), "PLAN"))

    assert first is not None and second is not None
    assert first.page_id == second.page_id == "owned"
    assert len(warnings) == 1
    assert len(context.lifecycle_calls) == 2
    assert context.detached_sessions == 2
    assert owned.front is False


def test_terminal_team_reuse_rebinds_without_new_chat(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(
        page_id="terminal-page",
        role="PLAN",
        team="old-newest",
        task_id="task-old",
    )
    original_url = page.url
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    context = FakeContext([page])
    actions = CDPATabActions(context, config)

    acquired = asyncio.run(actions.acquire(manifest(), "PLAN"))

    assert acquired.page_id == "terminal-page"
    assert acquired.created is False
    assert acquired.new_chat is False
    assert acquired.url == original_url
    assert acquired.client.preflight_calls == ["task-1"]
    assert acquired.client.prepare_calls == []
    assert acquired.client.bind_calls == [("task-1", "new-team")]
    assert page.snapshot_value.page_task_id == "task-1"
    assert page.snapshot_value.page_team == "new-team"
    assert page.front is False
    assert context.lifecycle_calls == [
        (page, "Page.setWebLifecycleState", {"state": "active"}, "task-1", "new-team"),
        (page, "Emulation.setFocusEmulationEnabled", {"enabled": True}, "task-1", "new-team"),
    ]
    assert context.detached_sessions == 1








def test_exact_role_team_task_reconciles_despite_stale_recorded_page_id(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [
        FakePage(page_id="terminal", role="PLAN", team="old-newest"),
        FakePage(page_id="current", role="PLAN", team="new-team", task_id="task-1"),
    ]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(FakeContext(pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(page_id="stale-page"), "PLAN"))

    assert [snapshot.page_id for _client, snapshot in matches] == ["current"]










def test_recorded_offline_role_requires_controlled_reopen(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "random_delay", lambda *_args, **_kwargs: asyncio.sleep(0))
    actions = CDPATabActions(FakeContext(), config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    with pytest.raises(RoleOwnershipError, match="Open tab"):
        asyncio.run(actions.acquire(state, "PLAN"))

    reopened = asyncio.run(actions.reopen(state, "PLAN"))

    assert reopened.page_id == "closed-page"
    assert reopened.created is True
    assert reopened.client.binding.page_id == "closed-page"
    assert reopened.url == "https://chatgpt.com/c/exact-conversation"
    assert reopened.client.clean_ready_calls == [
        round(config.workspace_timeout_seconds * 1000)
    ]
    assert reopened.client.page.snapshot_value.page_task_id == "task-1"
    assert reopened.client.page.snapshot_value.page_team == "new-team"
    assert reopened.client.page.front is True
    assert actions.browser_context.lifecycle_calls == [
        (
            reopened.client.page,
            "Page.setWebLifecycleState",
            {"state": "active"},
            "task-1",
            "new-team",
        ),
        (
            reopened.client.page,
            "Emulation.setFocusEmulationEnabled",
            {"enabled": True},
            "task-1",
            "new-team",
        ),
    ]
    assert actions.browser_context.detached_sessions == 1


def test_explicit_restart_discards_only_exact_known_automated_draft(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    state = manifest(
        page_id="closed-page",
        conversation_url="https://chatgpt.com/c/WEB:temporary-id",
    )

    matching = FakeContext(draft_on_new="exact automated prompt")
    restarted = asyncio.run(
        CDPATabActions(matching, config).restart(
            state,
            "PLAN",
            known_automated_draft="exact automated prompt",
        )
    )
    assert restarted.client.page.snapshot_value.composer_text == ""
    assert restarted.client.page.closed is False

    mismatched = FakeContext(draft_on_new="manual user draft")
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        asyncio.run(
            CDPATabActions(mismatched, config).restart(
                state,
                "PLAN",
                known_automated_draft="exact automated prompt",
            )
        )
    assert mismatched.pages[0].closed is True


def test_close_team_uses_role_team_task_and_ignores_stale_page_id(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    owned = FakePage(page_id="runtime-page", role="PLAN", team="new-team", task_id="task-1")
    free = FakePage(page_id="free", role=None, team=None, task_id=None)
    other = FakePage(page_id="other", role="PLAN", team="other-team", task_id="task-1")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "action_delay", lambda *_args, **_kwargs: asyncio.sleep(0))
    actions = CDPATabActions(FakeContext([owned, free, other]), config)
    state = manifest(page_id="stale-page")

    assert asyncio.run(actions.close_team(state)) == 1
    assert owned.closed is True
    assert free.closed is False
    assert other.closed is False


















def test_reopen_accepted_waiting_skips_clean_composer_requirement(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    actions = CDPATabActions(FakeContext(), config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    reopened = asyncio.run(
        actions.reopen(state, "PLAN", require_clean_ready=False)
    )

    assert reopened.url == "https://chatgpt.com/c/exact-conversation"
    assert reopened.client.clean_ready_calls == []


def test_automatic_reopen_stays_background_until_explicit_wake(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    actions = CDPATabActions(FakeContext(), config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    reopened = asyncio.run(
        actions.reopen(
            state,
            "PLAN",
            require_clean_ready=False,
            foreground=False,
        )
    )

    assert reopened.client.page.front is False
    assert actions.browser_context.lifecycle_calls == []

    asyncio.run(actions.wake(reopened))
    assert actions.browser_context.lifecycle_calls == [
        (
            reopened.client.page,
            "Page.setWebLifecycleState",
            {"state": "active"},
            "task-1",
            "new-team",
        ),
        (
            reopened.client.page,
            "Emulation.setFocusEmulationEnabled",
            {"enabled": True},
            "task-1",
            "new-team",
        ),
    ]
