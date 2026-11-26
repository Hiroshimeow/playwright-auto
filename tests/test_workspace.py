import asyncio
from contextlib import asynccontextmanager

import pytest

import playwright_auto.workspace as workspace_module
from playwright_auto.chatgpt import (
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
)
from playwright_auto.workflow import Workflow
from playwright_auto.chatgpt_errors import ProviderChallengeError
from playwright_auto.workspace import (
    ChatGPTWorkspace,
    RouteValidationError,
    WorkspaceBindingError,
    parse_route_map,
)
from playwright_auto.workspace_blocks import (
    DispatchRouteBlock,
    ParseRouteBlock,
    WaitRouteResponsesBlock,
)


def test_parse_route_map_accepts_one_exact_json_object():
    assert parse_route_map('{"PLAN":"review this"}', ["PLAN", "DEV"]) == {
        "PLAN": "review this"
    }
    assert parse_route_map('```json\n{"DEV":"implement"}\n```', ["PLAN", "DEV"]) == {
        "DEV": "implement"
    }


def test_parse_route_map_fails_closed_on_unknown_role_or_multiple_fences():
    with pytest.raises(RouteValidationError, match="unknown route target"):
        parse_route_map('{"AUDIT":"x"}', ["PLAN", "DEV"])
    with pytest.raises(RouteValidationError, match="exactly one"):
        parse_route_map('```json\n{"PLAN":"x"}\n```\n```json\n{"DEV":"y"}\n```', ["PLAN", "DEV"])


class FakeBoundClient:
    def __init__(self, page, timeout_ms=8000):
        self.page = page
        self.timeout_ms = timeout_ms
        self.binding = None

    async def snapshot(self):
        class Snapshot:
            page_id = self.page.page_id if self.page.assigned else None
        return Snapshot()

    async def set_role(self, role, allow_rebind=False, force_new_page_id=False):
        self.page.set_role_calls += 1
        if force_new_page_id:
            self.page.page_id = f"{self.page.page_id}-new"
        self.page.assigned = True
        self.binding = PageBinding(self.page.page_id, role)
        return {"page_id": self.page.page_id, "page_role": role}


class FakePage:
    def __init__(self, page_id):
        self.page_id = page_id
        self.assigned = False
        self.set_role_calls = 0


def test_workspace_rejects_role_and_physical_tab_collisions(monkeypatch):
    monkeypatch.setattr(workspace_module, "ChatGPTPage", FakeBoundClient)
    ws = ChatGPTWorkspace()
    page1 = FakePage("p1")
    page2 = FakePage("p2")

    asyncio.run(ws.bind("DEV", page1))

    with pytest.raises(WorkspaceBindingError, match="another physical tab"):
        asyncio.run(ws.bind("DEV", page2))
    with pytest.raises(WorkspaceBindingError, match="already bound to role"):
        asyncio.run(ws.bind("PLAN", page1))
    assert page1.set_role_calls == 1


class FakeRoleClient:
    def __init__(self, role):
        self.role = role
        self.sent = []
        self.binding = PageBinding(f"page-{role}", role)

    @asynccontextmanager
    async def workflow_guard(self):
        yield

    async def send(self, prompt, wait_for_stop=True, max_attempts=2, recovery_reload=True):
        self.sent.append((prompt, wait_for_stop, max_attempts, recovery_reload))
        return SendReceipt(
            prompt=prompt,
            prompt_sha256=f"digest-{self.role}",
            binding=self.binding,
            baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
            attempts=1,
            accepted_via="stop_button",
            session_id_before=None,
        )

    async def wait_for_response(self, receipt, **_options):
        return MessageSnapshot(
            "assistant",
            f"message-{self.role}",
            f"turn-{self.role}",
            f"response-{self.role}",
            (),
        )


class FakeWorkspace:
    def __init__(self):
        self.clients = {role: FakeRoleClient(role) for role in ("PLAN", "DEV")}

    @property
    def active_roles(self):
        return tuple(self.clients)

    def get(self, role):
        return self.clients[role]


def test_route_repair_is_one_shot_then_dispatches_exact_roles():
    repairs = []

    async def repair(text, error):
        repairs.append((text, error))
        return '{"PLAN":"review","DEV":"implement"}'

    ws = FakeWorkspace()
    workflow = Workflow(
        "route",
        [
            ParseRouteBlock("not-json", repair=repair),
            DispatchRouteBlock(wait_for_stop=False),
            WaitRouteResponsesBlock(stable_ms=0),
        ],
    )

    result = asyncio.run(workflow.run(ws))

    assert len(repairs) == 1
    assert result.context.variables["route_map"] == {
        "PLAN": "review",
        "DEV": "implement",
    }
    assert ws.clients["PLAN"].sent == [("review", False, 2, True)]
    assert ws.clients["DEV"].sent == [("implement", False, 2, True)]
    assert result.context.variables["route_responses"]["PLAN"].text == "response-PLAN"
    assert result.context.variables["route_responses"]["DEV"].text == "response-DEV"


def test_route_repair_failure_does_not_fallback_to_another_role():
    async def bad_repair(_text, _error):
        return '{"UNKNOWN":"fallback"}'

    workflow = Workflow(
        "bad-route",
        [ParseRouteBlock("bad", repair=bad_repair)],
    )

    with pytest.raises(Exception, match="repair failed"):
        asyncio.run(workflow.run(FakeWorkspace()))


def test_route_parser_rejects_surrounding_prose_and_duplicate_targets():
    with pytest.raises(RouteValidationError, match="surrounding prose"):
        parse_route_map('route this:\n```json\n{"PLAN":"x"}\n```', ["PLAN"])
    with pytest.raises(RouteValidationError, match="duplicate route target"):
        parse_route_map('{"PLAN":"x","PLAN":"y"}', ["PLAN"])


def test_route_parser_allows_braces_inside_prompt_text():
    assert parse_route_map(
        '```json\n{"DEV":"implement object {\\"x\\": 1}"}\n```',
        ["DEV"],
    ) == {"DEV": 'implement object {"x": 1}'}


def test_cloned_session_storage_gets_new_physical_page_id(monkeypatch):
    monkeypatch.setattr(workspace_module, "ChatGPTPage", FakeBoundClient)
    ws = ChatGPTWorkspace()
    original = FakePage("shared")
    clone = FakePage("shared")
    clone.assigned = True

    first = asyncio.run(ws.bind("DEV", original))
    second = asyncio.run(ws.bind("PLAN", clone))

    assert first.binding.page_id == "shared"
    assert second.binding.page_id == "shared-new"
    assert ws.active_roles == ("DEV", "PLAN")


def test_parallel_dispatch_collects_all_role_outcomes_before_failing():
    from playwright_auto.workspace_blocks import RoleDispatchError

    class PartiallyFailingClient(FakeRoleClient):
        async def send(self, prompt, **options):
            self.sent.append((prompt, options))
            if self.role == "PLAN":
                raise RuntimeError("plan unavailable")
            return await super().send(prompt, **options)

    class PartialWorkspace:
        def __init__(self):
            self.clients = {
                role: PartiallyFailingClient(role) for role in ("PLAN", "DEV")
            }

        def get(self, role):
            return self.clients[role]

    ws = PartialWorkspace()
    workflow = Workflow(
        "parallel-errors",
        [DispatchRouteBlock(parallel=True, fail_on_error=True)],
    )

    with pytest.raises(Exception) as captured:
        asyncio.run(
            workflow.run(
                ws,
                {"route_map": {"PLAN": "review", "DEV": "implement"}},
            )
        )

    cause = captured.value.cause
    assert isinstance(cause, RoleDispatchError)
    assert "PLAN" in cause.errors
    assert "DEV" not in cause.errors
    assert "DEV" in captured.value.context.variables["route_receipts"]
    assert ws.clients["PLAN"].sent
    assert ws.clients["DEV"].sent


def test_parallel_dispatch_can_return_partial_results_without_raising():
    class PartiallyFailingClient(FakeRoleClient):
        async def send(self, prompt, **options):
            if self.role == "PLAN":
                raise RuntimeError("plan unavailable")
            return await super().send(prompt, **options)

    class PartialWorkspace:
        def __init__(self):
            self.clients = {
                role: PartiallyFailingClient(role) for role in ("PLAN", "DEV")
            }

        def get(self, role):
            return self.clients[role]

    result = asyncio.run(
        Workflow(
            "partial",
            [DispatchRouteBlock(parallel=True, fail_on_error=False)],
        ).run(
            PartialWorkspace(),
            {"route_map": {"PLAN": "review", "DEV": "implement"}},
        )
    )

    output = result.context.results["dispatch_route"]
    assert set(output["receipts"]) == {"DEV"}
    assert set(output["errors"]) == {"PLAN"}


class ChallengeLocator:
    @property
    def first(self):
        return self

    async def wait_for(self, **_options):
        raise TimeoutError("composer absent")


class ChallengePage:
    url = "about:blank"
    closed = False

    async def goto(self, _url, **_options):
        self.url = "https://chatgpt.com/?__cf_chl_rt_tk=challenge-token"

    def locator(self, _selector):
        return ChallengeLocator()

    async def close(self):
        self.closed = True


class ChallengeContext:
    def __init__(self):
        self.page = ChallengePage()

    async def new_page(self):
        return self.page


def test_open_role_classifies_cloudflare_verification_as_provider_challenge():
    context = ChallengeContext()

    with pytest.raises(ProviderChallengeError, match="Cloudflare challenge"):
        asyncio.run(ChatGPTWorkspace().open_role(context, "PLAN"))

    assert context.page.closed is True


def test_cloudflare_detection_requires_a_challenge_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(
                '<script type="application/json" src="/cdn-cgi/challenge-platform/cdn.js"></script>'
                '<main><p>How to tell if a site says verifying you are human</p>'
                '<div contenteditable="true" role="textbox"></div></main>'
            )
            assert page.evaluate(workspace_module.CLOUDFLARE_CHALLENGE_DOM) is False
            page.set_content('<main id="challenge-stage">Verifying you are human</main>')
            assert page.evaluate(workspace_module.CLOUDFLARE_CHALLENGE_DOM) is True
        finally:
            browser.close()


def test_open_role_waits_for_composer_after_navigation_commits(monkeypatch):
    context = ChallengeContext()
    steps = []

    async def goto(url, *, wait_until, timeout):
        if wait_until != "commit":
            raise TimeoutError("document committed but DOMContentLoaded is delayed")
        context.page.url = url
        steps.append("commit")

    async def ready(self, **_options):
        steps.append("composer")

    async def bind(self, role, page, **_options):
        assert page is context.page and role == "PLAN"
        steps.append("bind")
        return page

    monkeypatch.setattr(context.page, "goto", goto)
    monkeypatch.setattr(ChallengeLocator, "wait_for", ready)
    monkeypatch.setattr(ChatGPTWorkspace, "bind", bind)
    assert asyncio.run(ChatGPTWorkspace().open_role(context, "PLAN")) is context.page
    assert steps == ["commit", "composer", "bind"]


@pytest.mark.parametrize("commits", [True, False])
def test_open_role_observes_expected_document_after_aborted_navigation(monkeypatch, commits):
    from playwright.async_api import Error

    expected = "https://chatgpt.com/?temporary-chat=true"
    steps = []

    class AbortedPage(ChallengePage):
        async def goto(self, url, *, wait_until, timeout):
            assert url == expected and wait_until == "commit"
            steps.append("goto")
            raise Error("Page.goto: net::ERR_ABORTED")

        async def wait_for_url(self, url, *, wait_until, timeout):
            assert url == expected and wait_until == "commit"
            steps.append("observe")
            if not commits:
                raise TimeoutError("expected document never committed")
            self.url = url

    async def ready(self, **_options):
        steps.append("composer")

    async def bind(self, role, page, **_options):
        assert page is context.page and role == "PLAN"
        steps.append("bind")
        return page

    context = ChallengeContext()
    context.page = AbortedPage()
    monkeypatch.setattr(ChallengeLocator, "wait_for", ready)
    monkeypatch.setattr(ChatGPTWorkspace, "bind", bind)
    operation = ChatGPTWorkspace().open_role(context, "PLAN", url=expected)
    if commits:
        assert asyncio.run(operation) is context.page
        assert steps == ["goto", "observe", "composer", "bind"]
        assert not context.page.closed
    else:
        with pytest.raises(workspace_module.FreshPageReadinessTimeout):
            asyncio.run(operation)
        assert steps == ["goto", "observe"]
        assert context.page.closed


@pytest.mark.parametrize("evidence", [True, False, "diagnostic_error", "diagnostic_timeout"])
def test_open_role_unchanged_url_challenge_evidence(monkeypatch, evidence):
    class RootPage(ChallengePage):
        async def goto(self, url, **_options):
            self.url = url

        async def evaluate(self, script):
            assert "_cf_chl_opt" in script
            if evidence == "diagnostic_error":
                raise RuntimeError("renderer unavailable")
            if evidence == "diagnostic_timeout":
                await asyncio.sleep(10)
            return evidence

    context = ChallengeContext()
    context.page = RootPage()
    ws = ChatGPTWorkspace()
    if evidence == "diagnostic_timeout":
        original_wait_for = asyncio.wait_for

        async def bounded_wait_for(awaitable, timeout):
            assert timeout <= 2
            return await original_wait_for(awaitable, timeout=0.001)

        monkeypatch.setattr(asyncio, "wait_for", bounded_wait_for)
    expected = ProviderChallengeError if evidence is True else TimeoutError
    with pytest.raises(expected, match="Cloudflare challenge" if evidence is True else "composer absent") as failure:
        asyncio.run(ws.open_role(context, "PLAN"))
    assert context.page.closed is True
    assert ws.active_roles == ()
    if evidence is not True:
        assert type(failure.value).__name__ == "FreshPageReadinessTimeout"


def test_open_role_binding_failure_is_not_reclassified_as_cloudflare(monkeypatch):
    class ReadyLocator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_options):
            pass

    class ReadyPage(ChallengePage):
        def locator(self, _selector):
            return ReadyLocator()

        async def evaluate(self, _script):
            pytest.fail("diagnostics must not override a binding failure")

    async def fail_binding(*_args, **_kwargs):
        raise WorkspaceBindingError("ownership conflict")

    ws = ChatGPTWorkspace()
    monkeypatch.setattr(ws, "bind", fail_binding)
    context = ChallengeContext()
    context.page = ReadyPage()
    with pytest.raises(WorkspaceBindingError, match="ownership conflict"):
        asyncio.run(ws.open_role(context, "PLAN"))
    assert context.page.closed is True


@pytest.mark.parametrize("close_verified", [True, False])
def test_direct_open_only_retries_after_the_exact_unattached_target_is_gone(close_verified):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from playwright.async_api import Error

    calls = []

    async def send(method, params=None):
        calls.append((method, params))
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": []}
        if method == "Target.createTarget":
            return {"targetId": "created-here"}
        assert params == {"targetId": "created-here"}
        if method == "Target.closeTarget":
            return {"success": True}
        assert method == "Target.getTargetInfo"
        if close_verified:
            raise Error("No target with given id found")
        return {"targetInfo": {"targetId": "created-here"}}

    session = SimpleNamespace(send=send, detach=AsyncMock())
    browser = SimpleNamespace(
        browser_type=SimpleNamespace(name="chromium"),
        new_browser_cdp_session=AsyncMock(return_value=session),
    )
    context = SimpleNamespace(browser=browser, on=Mock(), remove_listener=Mock(), new_page=AsyncMock())
    with pytest.raises(TimeoutError) as exc:
        asyncio.run(ChatGPTWorkspace().open_role(context, "PLAN", timeout_ms=25))
    assert isinstance(exc.value, workspace_module.FreshPageReadinessTimeout) == close_verified
    assert [method for method, _ in calls] == [
        "Target.getBrowserContexts", "Target.createTarget", "Target.closeTarget", "Target.getTargetInfo"
    ]
    assert context.remove_listener.call_args == context.on.call_args
    context.new_page.assert_not_awaited()
    session.detach.assert_awaited_once()


def test_open_role_creates_the_destination_directly_and_matches_exact_target(tmp_path, monkeypatch):
    from playwright.async_api import Page, async_playwright

    async def refuse_goto(*_args, **_kwargs):
        raise TimeoutError("blank-tab navigation deferred")

    async def bind(self, _role, page, **_options):
        return page

    monkeypatch.setattr(Page, "goto", refuse_goto)
    monkeypatch.setattr(ChatGPTWorkspace, "bind", bind)

    async def run():
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path / "chromium"))
            try:
                existing = context.pages[0]
                url = 'data:text/html,<div contenteditable="true" role="textbox">ready</div>'
                workspace = ChatGPTWorkspace()
                plan, dev = await asyncio.gather(
                    workspace.open_role(context, "PLAN", url=url, timeout_ms=3000),
                    workspace.open_role(context, "DEV", url=url, timeout_ms=3000),
                )
                assert len({plan, dev, existing}) == 3
                assert existing.url == "about:blank"
                assert await plan.locator('[role="textbox"]').inner_text() == "ready"
                assert await dev.locator('[role="textbox"]').inner_text() == "ready"
            finally:
                await context.close()

    asyncio.run(run())
