import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playwright_auto import chatgpt, observability
from playwright_auto.cdpa_actions import CDPATabActions
from playwright_auto.role_runtime import controller


@pytest.fixture
def events(tmp_path, monkeypatch):
    observability.configure_action_event_log(tmp_path / "actions.jsonl")
    monkeypatch.setattr(chatgpt, "action_delay", _no_delay)
    async def clean_probe(*args, **kwargs):
        return SimpleNamespace(composer_text="", attachment_count=0, composer_ambiguous=False)
    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", clean_probe)
    return lambda: observability.read_recent_action_events()


async def _no_delay(*args, **kwargs):
    pass


class Page:
    url = "https://chatgpt.com/c/owned"

    def __init__(self, *, failure=None):
        self.failure = failure
        self.lost = False
        self.reloads = 0
        self.gotos = 0
        self.closed = False

    async def evaluate(self, script, *args):
        if self.lost:
            raise RuntimeError("renderer unavailable")
        return {"page_id": "page-1", "role": "team-dev", "task_id": "task-1", "team": "team-1"}

    async def reload(self, **kwargs):
        self.reloads += 1
        self.lost = True
        if self.failure:
            raise self.failure

    async def close(self):
        self.lost = True
        if self.failure:
            raise self.failure
        self.closed = True

    def is_closed(self):
        return self.closed


@pytest.mark.parametrize("failure", [None, RuntimeError("disconnected")])
def test_reload_events_keep_identity_and_correlation_after_renderer_loss(events, failure):
    page = Page(failure=failure)
    if failure:
        with pytest.raises(RuntimeError, match="disconnected"):
            asyncio.run(chatgpt.refresh_page(page, reason="response_timeout_recheck"))
    else:
        asyncio.run(chatgpt.refresh_page(page, reason="response_timeout_recheck"))
    records = events()
    assert [e["phase"] for e in records] == ["start", "error" if failure else "complete"]
    assert page.reloads == 1
    assert len({e["action_id"] for e in records}) == 1
    assert records[0]["action_id"]
    assert len({e["event_id"] for e in records}) == 2
    for e in records:
        assert e["action"] == "reload"
        assert e["reason"] == "response_timeout_recheck"
        assert (e["page_id"], e["role"], e["task_id"], e["team"]) == ("page-1", "team-dev", "task-1", "team-1")
        assert e["page_url"] == Page.url
    assert observability.mcp_allow_click_summary()["clicks"] == 0


@pytest.mark.parametrize("failure", [None, RuntimeError("close failed")])
def test_close_team_records_actual_mutation_outcome(events, monkeypatch, failure):
    from playwright_auto import cdpa_actions
    monkeypatch.setattr(cdpa_actions, "action_delay", _no_delay)
    actions = object.__new__(CDPATabActions)
    page = Page(failure=failure)
    manifest = {"task_id": "task-1", "team": "team-1"}
    if failure:
        with pytest.raises(cdpa_actions.TeamCloseError):
            asyncio.run(actions.close_team(manifest, preflighted_pages=[page]))
    else:
        assert asyncio.run(actions.close_team(manifest, preflighted_pages=[page])) == 1
    records = events()
    assert [e["phase"] for e in records] == ["start", "error" if failure else "complete"]
    assert all(e["action"] == "close_tab" for e in records)
    assert all(e["reason"] == "owned_team_cleanup" for e in records)
    assert all(e["page_id"] == "page-1" and e["team"] == "team-1" for e in records)
    assert records[0]["action_id"] == records[1]["action_id"]


class NewChatPage(Page):
    def __init__(self, *, clicked=True, wait_error=None, achieved=False):
        super().__init__()
        self.clicked = clicked
        self.wait_error = wait_error
        self.achieved = achieved
        self.waits = 0

    async def evaluate(self, script, *args):
        if "target.click()" in script:
            return self.clicked
        if "location.pathname" in script:
            return self.achieved
        return await super().evaluate(script, *args)

    async def wait_for_function(self, *args, **kwargs):
        self.waits += 1
        if self.waits == 1 and self.wait_error:
            raise self.wait_error

    async def goto(self, url, **kwargs):
        self.gotos += 1
        self.url = url


@pytest.mark.parametrize("clicked,wait_error,achieved,expected", [
    (True, None, False, "dom"),
    (False, None, False, "navigate"),
    (True, PlaywrightTimeoutError("readiness timeout"), True, "dom"),
    (True, PlaywrightTimeoutError("readiness timeout"), False, "navigate"),
])
def test_new_chat_uses_only_supported_fallback(events, clicked, wait_error, achieved, expected):
    page = NewChatPage(clicked=clicked, wait_error=wait_error, achieved=achieved)
    assert asyncio.run(chatgpt.open_new_chat(page)) == expected
    assert page.gotos == (1 if expected == "navigate" else 0)
    navigations = [e for e in events() if e["action"] == "goto"]
    assert [e["phase"] for e in navigations] == (["start", "complete"] if expected == "navigate" else [])
    if navigations:
        assert navigations[0]["page_url"] == "https://chatgpt.com/c/owned"
        assert navigations[-1]["page_url"] == "https://chatgpt.com/"
        assert all(e["reason"] == ("new_chat_dom_timeout" if clicked else "new_chat_dom_missing") for e in navigations)
        assert navigations[0]["action_id"] == navigations[1]["action_id"]


@pytest.mark.parametrize("clicked", [True, False])
@pytest.mark.parametrize("input_kind", ["draft", "attachment", "ambiguous"])
def test_new_chat_fallback_preserves_input_arriving_after_click(events, monkeypatch, clicked, input_kind):
    page = NewChatPage(clicked=clicked, wait_error=PlaywrightTimeoutError("readiness timeout") if clicked else None)

    async def probe(*args, **kwargs):
        return SimpleNamespace(
            composer_text="operator draft arrived" if input_kind == "draft" else "",
            attachment_count=1 if input_kind == "attachment" else 0,
            composer_ambiguous=input_kind == "ambiguous",
        )

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", probe)
    with pytest.raises(chatgpt.ComposerConflictError):
        asyncio.run(chatgpt.open_new_chat(page))
    assert page.gotos == 0
    assert not [e for e in events() if e["action"] == "goto"]


def test_new_chat_disconnect_propagates_without_navigation(events):
    page = NewChatPage(wait_error=RuntimeError("disconnected"))
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(chatgpt.open_new_chat(page))
    assert page.gotos == 0


def test_refresh_timeout_logs_attempt_error_and_verified_postcondition(events):
    from unittest.mock import AsyncMock
    from test_cdpa_action_refresh_rootfix import _snapshot, RefreshPage, _acquired
    before = _snapshot()
    page = RefreshPage(
        reload=AsyncMock(side_effect=TimeoutError("committed")),
        evaluate=AsyncMock(side_effect=[
            {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
            {"url": before.url, "time_origin": 200.0, "ready_state": "interactive"},
        ]),
    )
    acquired = _acquired(page, [before, before])
    asyncio.run(acquired.client.refresh(reason="ui_error"))
    records = events()
    assert [(e["action"], e["phase"]) for e in records] == [
        ("refresh", "start"), ("reload", "start"), ("reload", "error"), ("refresh", "complete")
    ]
    assert records[-1]["detail"] == "verified_committed_reload"
    assert all(e["reason"] == "ui_error" for e in records)
    assert page.evaluate.await_count == 2  # Logging uses existing snapshot, not renderer reads.
    assert page.reload.await_count == 1
    acquired.client.send.assert_not_awaited()


def test_action_log_io_failure_does_not_mask_reload_outcome(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(observability, "append_action_event", fail)
    page = Page()
    asyncio.run(chatgpt.refresh_page(page, reason="ui_error"))
    assert page.reloads == 1
    page = Page(failure=RuntimeError("original browser error"))
    with pytest.raises(RuntimeError, match="original browser error"):
        asyncio.run(chatgpt.refresh_page(page, reason="ui_error"))
    assert page.reloads == 1


def test_controller_passes_semantic_refresh_reason():
    reasons = []

    class Client:
        async def refresh(self, *, reason=None):
            reasons.append(reason)

    decision = controller.Decision(controller.Action.REFRESH, "response_timeout_recheck")
    asyncio.run(controller.RoleController().browser_action(Client(), SimpleNamespace(), {}, None, decision))
    assert reasons == ["response_timeout_recheck"]
