import asyncio
from types import SimpleNamespace

import pytest

from playwright_auto import observability
from playwright_auto.workspace import ChatGPTWorkspace


class Page:
    url = "about:blank"

    def __init__(self, failure=None):
        self.failure = failure
        self.closed = False

    async def evaluate(self, *args):
        return {}

    async def goto(self, url, **kwargs):
        if self.failure:
            raise self.failure
        self.url = url

    def locator(self, *args):
        return self

    @property
    def first(self):
        return self

    async def wait_for(self, **kwargs):
        pass

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("failure", [None, RuntimeError("disconnected")])
def test_workspace_logs_allocation_navigation_and_failed_acquisition_cleanup(tmp_path, monkeypatch, failure):
    from playwright_auto import workspace as module
    observability.configure_action_event_log(tmp_path / "actions.jsonl")
    page = Page(failure)
    workspace = ChatGPTWorkspace()

    class Context:
        async def new_page(self):
            return page

    async def bind(*args, **kwargs):
        return SimpleNamespace(page=page)

    async def challenge(*args):
        return False

    monkeypatch.setattr(workspace, "bind", bind)
    monkeypatch.setattr(module, "cloudflare_challenge_visible", challenge)
    if failure:
        with pytest.raises(RuntimeError, match="disconnected"):
            asyncio.run(workspace.open_role(Context(), "team-dev"))
    else:
        asyncio.run(workspace.open_role(Context(), "team-dev"))
    events = observability.read_recent_action_events()
    expected = [("open_tab", "start"), ("open_tab", "complete"), ("goto", "start"), ("goto", "error" if failure else "complete")]
    if failure:
        expected += [("close_tab", "start"), ("close_tab", "complete")]
    assert [(e["action"], e["phase"]) for e in events] == expected
    for start, end in zip(events[::2], events[1::2]):
        assert start["action_id"] == end["action_id"]
        assert start["reason"] == end["reason"]
        assert start["reason"]
        assert start["page_id"] is None
        assert start["role"] is None  # Not bound yet: requested role is not ownership.
    assert page.closed == bool(failure)


@pytest.mark.parametrize("failure", [False, True])
def test_saved_conversation_reopen_logs_unbound_allocation_and_navigation(tmp_path, monkeypatch, failure):
    from playwright_auto import cdpa_actions as module
    from playwright_auto.cdpa_actions import CDPATabActions
    from playwright_auto.cdpa_config import load_cdpa_config
    from test_cdpa_actions import FakeClient, FakeContext, manifest
    from test_cdpa_core import write_config
    observability.configure_action_event_log(tmp_path / "actions.jsonl")
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(module, "random_delay", lambda *a, **kw: asyncio.sleep(0))
    context = FakeContext()
    if failure:
        original = context.new_page

        async def new_page():
            page = await original()

            async def goto(*args, **kwargs):
                raise RuntimeError("disconnected")

            page.goto = goto
            return page

        context.new_page = new_page
    state = manifest(page_id="closed-page", conversation_url="https://chatgpt.com/c/exact-conversation")
    actions = CDPATabActions(context, config)
    if failure:
        with pytest.raises(RuntimeError, match="disconnected"):
            asyncio.run(actions.reopen(state, "PLAN", foreground=False))
    else:
        asyncio.run(actions.reopen(state, "PLAN", foreground=False))
    events = observability.read_recent_action_events()
    assert [(e["action"], e["phase"]) for e in events] == [
        ("open_tab", "start"), ("open_tab", "complete"),
        ("goto", "start"), ("goto", "error" if failure else "complete"),
    ] + ([("close_tab", "start"), ("close_tab", "complete")] if failure else [])
    assert all(e["reason"] for e in events)
    assert all(e["page_id"] is None for e in events)  # Saved identity is not yet restored.
