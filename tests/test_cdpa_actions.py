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
