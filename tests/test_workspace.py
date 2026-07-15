import asyncio
from contextlib import asynccontextmanager

import pytest

import playwright_auto.workspace as workspace_module
from playwright_auto.chatgpt import MessageBaseline, MessageSnapshot, PageBinding, SendReceipt
from playwright_auto.workflow import Workflow
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
