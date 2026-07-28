from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
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
        page_id = "reopened-page" if force_new_page_id else (self.page.snapshot_value.page_id or "assigned-page")
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
        return self.page.snapshot_value

    async def bind_task_identity(self, task_id, team):
        self.bind_calls.append((task_id, team))
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


class FakeContext:
    def __init__(self, pages=None, *, draft_on_new=""):
        self.pages = list(pages or [])
        self.draft_on_new = draft_on_new

    async def new_page(self):
        page = FakePage(page_id=None, role=None, team=None)
        page.snapshot_value.composer_text = self.draft_on_new
        self.pages.append(page)
        return page


class FakeWorkspace:
    async def open_role(self, context, role, *, timeout_ms=0):
        page = await context.new_page()
        page.snapshot_value.page_id = "fresh-page"
        page.snapshot_value.page_role = role
        client = FakeClient(page, timeout_ms=timeout_ms)
        client.binding = SimpleNamespace(page_id="fresh-page", role=role)
        return client


def manifest(
    *,
    team="new-team",
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
        "task_id": "task-1",
        "active_hop_id": active_hop_id,
        "hops": hops,
        "team": team,
        "reusable_teams": ["old-newest", "old-older"],
        "roles": {
            "PLAN": {
                "physical_role": "PLAN",
                "page_id": page_id,
                "page_url": page_url,
            }
        },
    }


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
    actions = CDPATabActions(SimpleNamespace(pages=pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(), "PLAN"))

    assert len(matches) == 1
    assert matches[0][1].page_id == "newest"
    assert matches[0][0].binding.page_id == "newest"


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
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)

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


@pytest.mark.parametrize(
    "message",
    (
        "accepted in-flight receipt cannot be transferred",
        "manual draft or attachment blocks task rebind",
    ),
)
def test_terminal_team_reuse_fails_before_identity_mutation_on_preflight(
    tmp_path, monkeypatch, message
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(
        page_id="terminal-page",
        role="PLAN",
        team="old-newest",
        task_id="task-old",
    )
    page.preflight_error = RuntimeError(message)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(actions.acquire(manifest(), "PLAN"))

    client = asyncio.run(actions._matching_clients(manifest(), "PLAN"))[0][0]
    assert client.bind_calls == []
    assert page.snapshot_value.page_task_id == "task-old"
    assert page.snapshot_value.page_team == "old-newest"


def test_terminal_team_reuse_with_fresh_flag_opens_new_chat(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(
        page_id="terminal-page",
        role="PLAN",
        team="old-newest",
        task_id="task-old",
    )
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)
    state = manifest()
    state["roles"]["PLAN"].update(
        reset_requested=True,
        reset_applied_generation=0,
        conversation_generation=1,
    )

    acquired = asyncio.run(actions.acquire(state, "PLAN"))

    assert acquired.page_id == "terminal-page"
    assert acquired.created is False
    assert acquired.new_chat is True
    assert acquired.client.preflight_calls == []
    assert acquired.client.prepare_calls == [("task-1", True)]
    assert acquired.client.bind_calls == [("task-1", "new-team")]
    assert acquired.url == "https://chatgpt.com/"
    assert page.snapshot_value.page_task_id == "task-1"
    assert page.snapshot_value.page_team == "new-team"


def test_matching_clients_does_not_claim_role_tab_without_matching_team(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [FakePage(page_id="unowned-role", role="PLAN", team=None)]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(), "PLAN"))

    assert matches == []


def test_exact_role_team_task_reconciles_despite_stale_recorded_page_id(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [
        FakePage(page_id="terminal", role="PLAN", team="old-newest"),
        FakePage(page_id="current", role="PLAN", team="new-team", task_id="task-1"),
    ]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(page_id="stale-page"), "PLAN"))

    assert [snapshot.page_id for _client, snapshot in matches] == ["current"]


def test_independent_successor_reuses_recorded_same_team_conversation(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(
        page_id="agent-page",
        role="agent-maintainers-agent",
        team="agent-maintainers",
        task_id="agent-old",
    )
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)
    state = {
        "task_mode": "independent",
        "task_id": "agent-new",
        "team": "agent-maintainers",
        "reusable_teams": ["agent-maintainers"],
        "active_hop_id": 1,
        "hops": [
            {
                "hop_id": 1,
                "target_role": "AGENT",
                "conversation_url": "https://chatgpt.com/c/test",
            }
        ],
        "roles": {
            "AGENT": {
                "physical_role": "agent-maintainers-agent",
                "page_id": "agent-page",
                "page_url": "https://chatgpt.com/c/test",
            }
        },
    }

    acquired = asyncio.run(actions.acquire(state, "AGENT"))

    assert acquired.page_id == "agent-page"
    assert acquired.new_chat is False
    assert acquired.client.preflight_calls == ["agent-new"]
    assert acquired.client.bind_calls == [("agent-new", "agent-maintainers")]
    assert page.snapshot_value.page_task_id == "agent-new"


def test_independent_preflight_matches_recorded_page_across_respawn(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(
        page_id="agent-page",
        role="agent-maintainers-agent",
        team="agent-maintainers",
        task_id="agent-old",
    )
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)
    state = {
        "task_mode": "independent",
        "task_id": "agent-new",
        "team": "agent-maintainers",
        "roles": {
            "AGENT": {
                "physical_role": "agent-maintainers-agent",
                "page_id": "agent-page",
            }
        },
    }

    assert asyncio.run(actions.preflight_team(state)) == [page]


def test_recorded_page_requires_matching_team_and_task_identity(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [
        FakePage(
            page_id="current",
            role="PLAN",
            team="new-team",
            task_id="different-task",
        )
    ]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=pages), config)

    matches = asyncio.run(actions._matching_clients(manifest(page_id="current"), "PLAN"))

    assert matches == []


def test_temporary_web_conversation_fails_closed_before_opening_page(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    context = FakeContext()
    actions = CDPATabActions(context, config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/",
        conversation_url="https://chatgpt.com/c/WEB:temporary-id",
    )

    with pytest.raises(RoleOwnershipError, match="temporary WEB conversation"):
        asyncio.run(actions.reopen(state, "PLAN"))

    assert context.pages == []


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
    actions = CDPATabActions(SimpleNamespace(pages=[owned, free, other]), config)
    state = manifest(page_id="stale-page")

    assert asyncio.run(actions.close_team(state)) == 1
    assert owned.closed is True
    assert free.closed is False
    assert other.closed is False


def test_duplicate_exact_role_tabs_fail_before_any_close(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    first = FakePage(page_id="one", role="PLAN", team="new-team", task_id="task-1")
    second = FakePage(page_id="two", role="PLAN", team="new-team", task_id="task-1")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "action_delay", lambda *_args, **_kwargs: asyncio.sleep(0))
    actions = CDPATabActions(SimpleNamespace(pages=[first, second]), config)

    with pytest.raises(RoleOwnershipError, match="multiple exact tabs"):
        asyncio.run(actions.close_team(manifest(page_id="stale")))
    assert first.closed is False
    assert second.closed is False


def test_explicit_restart_opens_fresh_role_tab_when_recorded_tab_is_offline(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(
        actions_module,
        "random_delay",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    context = FakeContext()
    actions = CDPATabActions(context, config)
    state = manifest(
        page_id="closed-page",
        conversation_url="https://chatgpt.com/c/WEB:temporary-id",
    )

    restarted = asyncio.run(actions.restart(state, "PLAN"))

    assert restarted.created is True
    assert restarted.new_chat is True
    assert restarted.page_id == "fresh-page"
    assert restarted.client.page.snapshot_value.page_role == "PLAN"
    assert restarted.client.page.snapshot_value.page_task_id == "task-1"
    assert restarted.client.page.snapshot_value.page_team == "new-team"


def test_team_preflight_fails_closed_when_supported_page_inspection_is_unknown(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    unreadable = FakePage(page_id="unknown", role="PLAN", team="new-team", task_id="task-1")
    unreadable.snapshot_error = RuntimeError("transient snapshot failure")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[unreadable]), config)

    with pytest.raises(RoleOwnershipError, match="cannot inspect.*snapshot failure"):
        asyncio.run(actions.preflight_team(manifest()))


def test_duplicate_detection_cannot_be_bypassed_by_one_unreadable_page(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    readable = FakePage(page_id="one", role="PLAN", team="new-team", task_id="task-1")
    unreadable = FakePage(page_id="two", role="PLAN", team="new-team", task_id="task-1")
    unreadable.snapshot_error = RuntimeError("second page unreadable")
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[readable, unreadable]), config)

    with pytest.raises(RoleOwnershipError, match="cannot inspect.*second page unreadable"):
        asyncio.run(actions.preflight_team(manifest()))
    assert readable.closed is False
    assert unreadable.closed is False


def test_close_team_counts_only_tabs_observed_closed(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    closed = FakePage(page_id="one", role="PLAN", team="new-team", task_id="task-1")
    stuck = FakePage(page_id="two", role="PLAN", team="new-team", task_id="task-1")

    async def close_without_closing():
        return None

    stuck.close = close_without_closing
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "action_delay", lambda *_args, **_kwargs: asyncio.sleep(0))
    actions = CDPATabActions(SimpleNamespace(pages=[closed, stuck]), config)

    with pytest.raises(TeamCloseError, match="did not report closed") as caught:
        asyncio.run(actions.close_team(manifest(), preflighted_pages=[closed, stuck]))

    assert caught.value.closed_tabs == 1
    assert closed.closed is True
    assert stuck.closed is False


def test_global_maintainer_reuses_exactly_one_role_only_tab(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    page = FakePage(page_id="maint-1", role="MAINTAINERS", team=None, task_id=None)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=[page]), config)

    acquired = asyncio.run(actions.acquire_global_role("MAINTAINERS"))

    assert acquired.page_id == "maint-1"
    assert acquired.created is False
    assert acquired.new_chat is False
    assert acquired.client.bind_calls == []
    assert page.snapshot_value.page_team is None
    assert page.snapshot_value.page_task_id is None


def test_global_maintainer_duplicate_tabs_fail_closed(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    pages = [
        FakePage(page_id="maint-1", role="MAINTAINERS", team=None, task_id=None),
        FakePage(page_id="maint-2", role="MAINTAINERS", team=None, task_id=None),
    ]
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(SimpleNamespace(pages=pages), config)

    with pytest.raises(RoleOwnershipError, match="multiple global tabs"):
        asyncio.run(actions.acquire_global_role("MAINTAINERS"))


def test_global_maintainer_opens_role_without_task_binding(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    context = FakeContext()
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(actions_module, "random_delay", lambda *_args: asyncio.sleep(0))
    actions = CDPATabActions(context, config)

    acquired = asyncio.run(actions.acquire_global_role("MAINTAINERS"))

    assert acquired.created is True
    assert acquired.page_id == "fresh-page"
    assert acquired.client.bind_calls == []
    assert acquired.client.page.snapshot_value.page_role == "MAINTAINERS"
    assert acquired.client.page.snapshot_value.page_team is None
    assert acquired.client.page.snapshot_value.page_task_id is None


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


def test_reopen_repairs_wrong_url_on_existing_exact_owned_tab(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    existing = FakePage(
        page_id="closed-page",
        role="PLAN",
        team="new-team",
        task_id="task-1",
    )
    existing.url = "https://chatgpt.com/c/wrong-conversation"
    existing.snapshot_value.url = existing.url
    context = FakeContext([existing])
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(context, config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    reopened = asyncio.run(
        actions.reopen(state, "PLAN", require_clean_ready=False)
    )

    assert reopened.created is False
    assert reopened.page_id == "closed-page"
    assert reopened.url == "https://chatgpt.com/c/exact-conversation"
    assert existing.url == "https://chatgpt.com/c/exact-conversation"
    assert existing.front is True


def test_reopen_uses_exact_role_url_when_active_hop_url_is_temporary(tmp_path, monkeypatch):
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
        page_url="https://chatgpt.com/c/exact-from-role",
        conversation_url="https://chatgpt.com/c/WEB:temporary-id",
    )

    reopened = asyncio.run(
        actions.reopen(state, "PLAN", require_clean_ready=False)
    )

    assert reopened.url == "https://chatgpt.com/c/exact-from-role"
    assert reopened.client.clean_ready_calls == []


def test_reopen_restores_recorded_page_id_on_existing_exact_conversation(
    tmp_path,
    monkeypatch,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    existing = FakePage(
        page_id="live-drifted-page",
        role="PLAN",
        team="new-team",
        task_id="task-1",
    )
    existing.url = "https://chatgpt.com/c/exact-conversation"
    existing.snapshot_value.url = existing.url
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(FakeContext([existing]), config)
    state = manifest(
        page_id="recorded-accepted-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    reopened = asyncio.run(
        actions.reopen(state, "PLAN", require_clean_ready=False)
    )

    assert reopened.created is False
    assert reopened.page_id == "recorded-accepted-page"
    assert reopened.url == "https://chatgpt.com/c/exact-conversation"
    assert reopened.client.binding.page_id == "recorded-accepted-page"
    assert existing.snapshot_value.page_id == "recorded-accepted-page"
    assert existing.front is True
