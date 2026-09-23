from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playwright_auto.cdpa_bootstraps import BootstrapCatalog
from playwright_auto.chatgpt import PageBinding
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.cdpa_routes import RouteDecision
from test_cdpa_worker import FakeActions, RecordingCDPASendClient, bootstrap_record, setup_task


class TransientComposerClient(RecordingCDPASendClient):
    def __init__(self, state, *, failures: int):
        super().__init__(task_id=state["task_id"], team=state["team"])
        self.binding = PageBinding("page-alpha-plan", "alpha-plan")
        self.failures = failures

    async def set_text(self, text):
        if self.failures:
            self.failures -= 1
            raise TimeoutError("composer mutation timed out")
        return await super().set_text(text)


class ExactSendActions(FakeActions):
    def __init__(self, client):
        super().__init__()
        self.client = client

    async def locate_owned(self, state, role):
        record = state["roles"][role]
        return SimpleNamespace(
            client=self.client,
            page_id=record.get("page_id") or f"page-{record['physical_role']}",
            url=record.get("page_url") or "https://chatgpt.com/",
            created=False,
            new_chat=False,
        )


def configured_endpoint(store, state):
    endpoint = store.endpoints.resolve()
    state["roles"]["PLAN"]["browser_endpoint_id"] = endpoint["endpoint_id"]
    return endpoint


def test_preboundary_recovery_crash_after_consumption_fails_closed_without_reallocation(
    tmp_path: Path,
):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-preboundary-crash-window")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = TransientComposerClient(state, failures=1)
    actions = ExactSendActions(client)

    asyncio.run(worker._sending(state, hop, actions))
    assert hop["preboundary_recovery"]["phase"] == "pending"
    store.save(Path(state["manifest_path"]), state)

    worker = type(worker)(worker.config, store=store)
    state = store.load(state["manifest_path"])
    asyncio.run(worker._sending(state, _active_hop(state), actions))
    assert _active_hop(state)["preboundary_recovery"]["phase"] == "reserved"
    assert actions.client.send_calls == []
    store.save(Path(state["manifest_path"]), state)

    worker = type(worker)(worker.config, store=store)
    state = store.load(state["manifest_path"])
    asyncio.run(worker._sending(state, _active_hop(state), actions))
    recovery = _active_hop(state)["preboundary_recovery"]
    assert recovery["phase"] == "in_progress"
    assert actions.client.send_calls == []
    store.save(Path(state["manifest_path"]), state)

    crashed_worker = type(worker)(worker.config, store=store)
    state = store.load(state["manifest_path"])
    asyncio.run(crashed_worker._sending(state, _active_hop(state), actions))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "preboundary_recovery_interrupted"
    assert actions.client.send_calls == []


def test_preboundary_ready_survives_worker_restart_and_continues_normal_send(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-preboundary-ready-restart")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = TransientComposerClient(state, failures=1)
    actions = ExactSendActions(client)
    asyncio.run(worker._sending(state, hop, actions))
    asyncio.run(worker._sending(state, hop, actions))
    asyncio.run(worker._sending(state, hop, actions))
    asyncio.run(worker._sending(state, hop, actions))
    assert hop["preboundary_recovery"]["phase"] == "ready"
    store.save(Path(state["manifest_path"]), state)

    worker = type(worker)(worker.config, store=store)
    state = store.load(state["manifest_path"])
    asyncio.run(worker._sending(state, _active_hop(state), actions))

    assert _active_hop(state)["state"] == "sent"
    assert client.send_calls == [_active_hop(state)["prompt"]]


def test_preboundary_mutation_timeout_does_not_reauthorize_allocation(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-preboundary-mutation-timeout")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = TransientComposerClient(state, failures=1)
    asyncio.run(worker._sending(state, hop, ExactSendActions(client)))
    allocations = []

    class Actions(ExactSendActions):
        async def locate_owned(self, state, role):
            allocations.append((state["task_id"], role))
            raise PlaywrightTimeoutError("allocation timed out after page creation")

    actions = Actions(client)
    asyncio.run(worker._sending(state, hop, actions))
    asyncio.run(worker._sending(state, hop, actions))
    asyncio.run(worker._sending(state, hop, actions))

    assert allocations == [(state["task_id"], "PLAN")]
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "preboundary_recovery_interrupted"
    store.save(Path(state["manifest_path"]), state)
    restarted = type(worker)(worker.config, store=store)
    state = store.load(state["manifest_path"])
    asyncio.run(restarted._sending(state, _active_hop(state), actions))
    assert allocations == [(state["task_id"], "PLAN")]


def test_deleted_bootstrap_cannot_regenerate_from_stale_snapshot(tmp_path: Path):
    _config, _store, state, worker = setup_task(tmp_path, task_id="task-deleted-regeneration")
    catalog = BootstrapCatalog(tmp_path)
    selected = catalog.upsert(bootstrap_record())
    state["bootstrap"] = selected
    catalog.delete(selected["bootstrap_id"])
    actions = SimpleNamespace(
        acquire_global_role=AsyncMock(side_effect=AssertionError("deleted bootstrap must not prewarm"))
    )

    regenerated = asyncio.run(worker._regenerate_bootstrap_donor(state, selected, actions))

    assert regenerated is None
    actions.acquire_global_role.assert_not_awaited()
    assert state["block_code"] == "bootstrap_unavailable"


def test_admission_preflight_is_exact_role_scoped_and_cleanup_remains_team_strict(tmp_path: Path):
    _config, _store, state, worker = setup_task(tmp_path, task_id="task-preflight-caller-scope")
    calls = []

    class Actions:
        async def preflight_role(self, manifest, role):
            assert manifest is state
            calls.append(("role", role))
            return []

        async def preflight_team(self, manifest, **kwargs):
            assert manifest is state
            calls.append(("team", kwargs))
            return []

    actions = Actions()
    asyncio.run(worker._admission_preflight(state, actions))
    asyncio.run(worker._cleanup_preflight(state, actions))

    assert calls == [("role", "PLAN"), ("team", {})]


def test_tool_failure_fingerprint_does_not_match_quoted_history_or_valid_report():
    from playwright_auto.cdpa_worker import CDPAWorker

    quoted = (
        "I previously saw `Script error: The tool failed internally`, then recovered.\n"
        "ROUTE: PAUSE\nHANDOFF: .plan/team/report.md"
    )
    ordinary = "ROUTE: PAUSE\nHANDOFF: .plan/team/report.md"

    quoted_failures = (
        quoted,
        "Historical note: permission denied yesterday.\nROUTE: PAUSE\nHANDOFF: .plan/team/report.md",
        "Prior output said no devices available, but the report is now valid.\nROUTE: PAUSE\nHANDOFF: .plan/team/report.md",
        "Quoted connector unavailable incident was resolved.\nROUTE: PAUSE\nHANDOFF: .plan/team/report.md",
    )
    assert all(CDPAWorker._tool_failure_from_response(item) is None for item in quoted_failures)
    operation_failures = (
        "write_file: internal failure",
        "shell_execute: failed internally",
        "get_skill: internal failure.",
    )
    narrative = (
        "The write_file internal failure was investigated and resolved.\n"
        "ROUTE: PAUSE\nHANDOFF: .plan/team/report.md"
    )
    assert all(
        CDPAWorker._tool_failure_from_response(item) == "session_tool_unavailable"
        for item in operation_failures
    )
    assert CDPAWorker._tool_failure_from_response(narrative) is None
    assert CDPAWorker._tool_failure_from_response(ordinary) is None
    assert CDPAWorker._tool_failure_from_response("no devices available") == "tool_target_unreachable"


def test_current_error_parser_requires_bounded_explicit_error_envelope():
    import json
    from playwright_auto.cdpa_worker import CDPAWorker

    route = json.dumps({"route": "PAUSE", "handoff": ".plan/team/report.md"})
    for suffix in (route, "```json\n" + route + "\n```"):
        assert CDPAWorker._tool_failure_from_response("No devices available\n" + suffix) == "tool_target_unreachable"
    for text in (
        "No devices available yesterday; now fixed.\n" + route,
        '> No devices available\n' + route,
        'No devices available\nHistorical quotation, resolved.\n' + route,
        json.dumps({"route": "PAUSE", "handoff": "Historical No devices available"}),
    ):
        assert CDPAWorker._tool_failure_from_response(text) is None


def test_ordinary_missing_report_still_uses_report_repair(tmp_path: Path):
    _config, _store, state, worker = setup_task(tmp_path, task_id="task-ordinary-report-omission")
    hop = _active_hop(state)
    hop.update(state="responded", response="ROUTE: PAUSE", expected_report_path=".plan/alpha/missing.md")
    decision = RouteDecision(route="PAUSE", handoff=hop["expected_report_path"])

    worker._repair_report(state, hop, FileNotFoundError("missing report"), decision=decision)

    assert state["hops"][-1]["kind"] == "report_repair"
    assert state["hops"][-1]["locked_route"] == "PAUSE"
    assert state["hops"][-1]["locked_handoff"] == hop["expected_report_path"]


def test_disabled_bootstrap_cannot_regenerate_from_stale_snapshot(tmp_path: Path):
    _config, _store, state, worker = setup_task(tmp_path, task_id="task-disabled-regeneration")
    catalog = BootstrapCatalog(tmp_path)
    selected = catalog.upsert(bootstrap_record())
    state["bootstrap"] = selected
    catalog.disable(selected["bootstrap_id"])
    actions = SimpleNamespace(
        acquire_global_role=AsyncMock(side_effect=AssertionError("disabled bootstrap must not prewarm"))
    )

    regenerated = asyncio.run(worker._regenerate_bootstrap_donor(state, selected, actions))

    assert regenerated is None
    actions.acquire_global_role.assert_not_awaited()
    assert state["block_code"] == "bootstrap_unavailable"
