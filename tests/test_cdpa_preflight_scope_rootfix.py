from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import CDPATabActions, RoleOwnershipError


class FakePage:
    def __init__(self, *, metadata, snapshot=None, snapshot_error=None):
        self.url = "https://chatgpt.com/c/test"
        self.metadata = metadata
        self.snapshot = snapshot
        self.snapshot_error = snapshot_error

    def is_closed(self):
        return False

    async def evaluate(self, _script):
        if isinstance(self.metadata, BaseException):
            raise self.metadata
        return {"url": self.url, "title": "", **self.metadata}


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeClient:
    snapshot_calls = []

    def __init__(self, page, **_kwargs):
        self.page = page

    async def snapshot(self):
        self.snapshot_calls.append(self.page)
        if self.page.snapshot_error is not None:
            raise self.page.snapshot_error
        return self.page.snapshot


def manifest(*, page_id="target-page", binding_id="binding-target"):
    return {
        "team": "target-team",
        "task_id": "target-task",
        "active_role": "PLAN",
        "roles": {
            "PLAN": {
                "physical_role": "target-plan",
                "page_id": page_id,
                "binding_id": binding_id,
            }
        },
    }


def page_metadata(
    *,
    page_id,
    binding_id=None,
    role="target-plan",
    team="target-team",
    task_id="target-task",
):
    return {
        "page_id": page_id,
        "binding_id": binding_id,
        "role": role,
        "team": team,
        "task_id": task_id,
    }


def page_snapshot(*, page_id, role="target-plan", team="target-team", task_id="target-task"):
    return SimpleNamespace(
        page_id=page_id,
        page_role=role,
        page_team=team,
        page_task_id=task_id,
    )


def make_actions(monkeypatch, pages):
    FakeClient.snapshot_calls = []
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    config = SimpleNamespace(workspace_timeout_seconds=0.02)
    return CDPATabActions(FakeContext(pages), config)


def test_normal_role_preflight_ignores_unrelated_snapshot_failure(monkeypatch):
    unrelated = FakePage(
        metadata=page_metadata(
            page_id="other-page",
            binding_id="other-binding",
            role=None,
            team=None,
            task_id=None,
        ),
        snapshot_error=TimeoutError("unrelated renderer stalled"),
    )
    owned = FakePage(
        metadata=page_metadata(page_id="target-page", binding_id="binding-target"),
        snapshot=page_snapshot(page_id="target-page"),
    )
    actions = make_actions(monkeypatch, [unrelated, owned])

    selected = asyncio.run(actions.preflight_role(manifest(), "PLAN"))

    assert selected == [owned]
    assert FakeClient.snapshot_calls == []


def test_normal_role_preflight_ignores_unbound_chatgpt_page(monkeypatch):
    unrelated = FakePage(
        metadata={},
        snapshot_error=TimeoutError("unbound page is unreadable"),
    )
    actions = make_actions(monkeypatch, [unrelated])

    selected = asyncio.run(actions.preflight_role(manifest(), "PLAN"))

    assert selected == []
    assert FakeClient.snapshot_calls == []


def test_normal_role_preflight_fails_on_exact_recorded_owner_conflict(monkeypatch):
    conflicting = FakePage(
        metadata=page_metadata(
            page_id="target-page",
            binding_id="binding-target",
            team="other-team",
        )
    )
    actions = make_actions(monkeypatch, [conflicting])

    with pytest.raises(RoleOwnershipError) as captured:
        asyncio.run(actions.preflight_role(manifest(), "PLAN"))

    assert captured.value.code == "role_ownership_conflict"


def test_normal_role_preflight_rejects_duplicate_binding(monkeypatch):
    pages = [
        FakePage(
            metadata=page_metadata(
                page_id=page_id,
                binding_id="binding-target",
            )
        )
        for page_id in ("page-a", "page-b")
    ]
    actions = make_actions(monkeypatch, pages)

    with pytest.raises(RoleOwnershipError, match="multiple exact bindings"):
        asyncio.run(
            actions.preflight_role(
                manifest(page_id="", binding_id="binding-target"),
                "PLAN",
            )
        )


def test_destructive_team_preflight_remains_strict(monkeypatch):
    unrelated = FakePage(
        metadata=page_metadata(
            page_id="other-page",
            binding_id="other-binding",
            role=None,
            team=None,
            task_id=None,
        ),
        snapshot_error=TimeoutError("unrelated renderer stalled"),
    )
    actions = make_actions(monkeypatch, [unrelated])

    with pytest.raises(RoleOwnershipError, match="exact-team preflight"):
        asyncio.run(actions.preflight_team(manifest()))

    assert FakeClient.snapshot_calls == [unrelated]


def test_fresh_chat_preflight_is_exact_role_scoped(monkeypatch):
    actions = make_actions(monkeypatch, [])
    owned = object()
    calls = []

    async def role_preflight(scope, role):
        calls.append(("role", role, scope["roles"]))
        return [owned] if len(calls) == 1 else []

    async def strict_team(*_args, **_kwargs):
        raise AssertionError("fresh_chat must not use strict team-wide preflight")

    async def close_team(scope, *, preflighted_pages=None):
        assert preflighted_pages == [owned]
        assert set(scope["roles"]) == {"PLAN"}
        return 1

    monkeypatch.setattr(actions, "preflight_role", role_preflight)
    monkeypatch.setattr(actions, "preflight_team", strict_team)
    monkeypatch.setattr(actions, "close_team", close_team)

    class Workspace:
        async def open_role(self, *_args, **_kwargs):
            raise RuntimeError("stop after preflight")

    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", lambda: Workspace())

    with pytest.raises(RuntimeError, match="stop after preflight"):
        asyncio.run(
            actions.fresh_chat(
                manifest(),
                "PLAN",
                url="https://chatgpt.com/",
            )
        )

    assert len(calls) == 2
