from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_routes import RouteContractError, parse_route_response
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.cdpa_workflow_agents import WorkflowAgentCatalog
from playwright_auto.dashboard import ASSET_ROOT, DASHBOARD_HTML_PATH
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions


def setup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    return config, store


def test_catalog_system_overrides_custom_identity_and_tombstone(tmp_path: Path):
    config, _store = setup(tmp_path)
    catalog = WorkflowAgentCatalog(config)

    initial = {item["route_key"]: item for item in catalog.list_agents()}
    assert list(initial) == ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT"]
    assert initial["PLAN"] == {
        "route_key": "PLAN",
        "display_name": "PLAN",
        "system_prompt": "# PLAN\nConstructor for PLAN.",
        "is_system": True,
        "deleted_at": None,
    }

    edited = catalog.update(
        "PLAN",
        display_name="Planner",
        system_prompt="PLAN override marker",
        external_command_id="cmd-system-edit",
    )
    assert edited["route_key"] == "PLAN"
    assert WorkflowAgentCatalog(config).get("PLAN")["display_name"] == "Planner"
    assert config.constructor_paths["PLAN"].read_text(encoding="utf-8") == (
        "# PLAN\nConstructor for PLAN.\n"
    )

    created = catalog.create(
        display_name="Researcher",
        system_prompt="CUSTOM_WORKFLOW_MARKER",
        external_command_id="cmd-custom-create",
    )
    assert created["route_key"].startswith("WF_")
    assert created["is_system"] is False
    route_key = created["route_key"]

    renamed = catalog.update(
        route_key,
        display_name="Renamed researcher",
        system_prompt="CUSTOM_WORKFLOW_MARKER_V2",
        external_command_id="cmd-custom-update",
    )
    assert renamed["route_key"] == route_key
    assert WorkflowAgentCatalog(config).get(route_key)["display_name"] == "Renamed researcher"

    with pytest.raises(ValueError, match="system workflow agents cannot be deleted"):
        catalog.delete("PLAN", external_command_id="cmd-delete-plan")

    deleted = catalog.delete(route_key, external_command_id="cmd-custom-delete")
    assert deleted["deleted_at"]
    assert route_key not in {item["route_key"] for item in catalog.list_agents()}
    assert catalog.get(route_key, include_deleted=True)["route_key"] == route_key


def test_task_snapshots_custom_agent_and_exact_team_reuse_fails_after_delete(tmp_path: Path):
    _config, store = setup(tmp_path)
    custom = store.create_workflow_agent(
        display_name="Researcher",
        system_prompt="TASK_SNAPSHOT_PROMPT",
        external_command_id="cmd-create-researcher",
    )
    route_key = custom["route_key"]

    first = store.create_task(
        "Use custom workflow",
        requested_team="catalog-team",
        task_id="task-catalog-one",
        roles=("PLAN", "DEV", route_key, "REVIEW"),
    )
    assert list(first["roles"]) == ["PLAN", "DEV", "REVIEW", route_key]
    assert list(first["workflow_agents"]) == ["PLAN", "DEV", "REVIEW", route_key]
    assert first["workflow_agents"][route_key]["system_prompt"] == "TASK_SNAPSHOT_PROMPT"

    store.update_workflow_agent(
        route_key,
        display_name="Researcher edited",
        system_prompt="CATALOG_CHANGED_PROMPT",
        external_command_id="cmd-edit-researcher",
    )
    persisted = store.load(first["manifest_path"])
    assert persisted["workflow_agents"][route_key]["system_prompt"] == "TASK_SNAPSHOT_PROMPT"

    with pytest.raises(ValueError, match="nonterminal task"):
        store.delete_workflow_agent(route_key, external_command_id="cmd-delete-active")

    def stop(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "DONE_STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["stopped_at"] = current["updated_at"]
        return current

    store.update(first["manifest_path"], stop)
    store.delete_workflow_agent(route_key, external_command_id="cmd-delete-terminal")

    with pytest.raises(ValueError, match="deleted or missing workflow agent"):
        store.create_task(
            "Reuse deleted composition",
            reuse_team="catalog-team",
            task_id="task-catalog-two",
        )


def test_worker_custom_hop_uses_snapshotted_prompt_and_dynamic_routes(tmp_path: Path):
    _config, store = setup(tmp_path)
    custom = store.create_workflow_agent(
        display_name="Researcher",
        system_prompt="CUSTOM_WORKER_PROMPT_MARKER",
        external_command_id="cmd-create-worker-custom",
    )
    route_key = custom["route_key"]
    state = store.create_task(
        "Custom prompt task",
        requested_team="alpha",
        task_id="task-custom-worker",
        roles=("PLAN", route_key, "REVIEW"),
    )
    hop = _active_hop(state)
    hop["target_role"] = route_key
    hop["physical_role"] = state["roles"][route_key]["physical_role"]
    state["active_role"] = route_key
    worker = CDPAWorker(store.config, store=store)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert "CUSTOM_WORKER_PROMPT_MARKER" in hop["prompt"]
    prefix = f"alpha · role: {route_key.lower()}\n"
    envelope = json.loads(hop["prompt"].split("\n\nCUSTOM_WORKER_PROMPT_MARKER", 1)[0].removeprefix(prefix))
    assert envelope["allowed-routes"] == ["PLAN", "REVIEW", route_key, "DONE"]

    decision = parse_route_response(
        json.dumps({"route": route_key, "handoff": "x"}),
        source_role="PLAN",
        allowed_routes=("PLAN", route_key, "REVIEW", "DONE"),
    )
    assert decision.route == route_key
    with pytest.raises(RouteContractError, match="not selected or unavailable"):
        parse_route_response(
            '{"route":"DEV","handoff":"x"}',
            source_role="PLAN",
            allowed_routes=("PLAN", route_key, "REVIEW", "DONE"),
        )


def test_custom_agent_receives_real_hop_and_routes_onward(tmp_path: Path):
    _config, store = setup(tmp_path)
    custom = store.create_workflow_agent(
        display_name="Researcher",
        system_prompt="REAL_CUSTOM_HOP_PROMPT",
        external_command_id="cmd-real-custom-hop",
    )
    route_key = custom["route_key"]
    state = store.create_task(
        "Route through custom agent",
        requested_team="custom-route",
        task_id="task-custom-route",
        roles=("PLAN", route_key, "REVIEW"),
    )
    worker = CDPAWorker(store.config, store=store)
    actions = FakeActions()

    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, actions))
    plan_report = tmp_path / plan_hop["expected_report_path"]
    plan_report.parent.mkdir(parents=True, exist_ok=True)
    plan_report.write_text("Verified custom workflow routing.", encoding="utf-8")
    plan_hop["response"] = json.dumps(
        {"route": route_key, "handoff": plan_hop["expected_report_path"]}
    )
    plan_hop["state"] = "responded"
    worker._responded(state, plan_hop)

    custom_hop = _active_hop(state)
    assert custom_hop["target_role"] == route_key
    asyncio.run(worker._pre_send(state, custom_hop, actions))
    assert "REAL_CUSTOM_HOP_PROMPT" in custom_hop["prompt"]
    custom_report = tmp_path / custom_hop["expected_report_path"]
    custom_report.parent.mkdir(parents=True, exist_ok=True)
    custom_report.write_text("Custom agent evidence.", encoding="utf-8")
    custom_hop["response"] = json.dumps(
        {"route": "REVIEW", "handoff": custom_hop["expected_report_path"]}
    )
    custom_hop["state"] = "responded"
    worker._responded(state, custom_hop)

    review_hop = _active_hop(state)
    assert review_hop["target_role"] == "REVIEW"
    assert [item["target_role"] for item in state["hops"]] == [
        "PLAN",
        route_key,
        "REVIEW",
    ]


def test_custom_role_controls_and_replacement_keep_snapshot(tmp_path: Path):
    _config, store = setup(tmp_path)
    custom = store.create_workflow_agent(
        display_name="Researcher",
        system_prompt="REPLACEMENT_SNAPSHOT_PROMPT",
        external_command_id="cmd-replacement-custom",
    )
    route_key = custom["route_key"]
    state = store.create_task(
        "Replace custom workflow",
        requested_team="replace-custom",
        task_id="task-replace-custom",
        roles=("PLAN", route_key, "REVIEW"),
    )

    controlled = store.request_control(
        state["manifest_path"],
        "open_tab",
        role=route_key,
        external_command_id="cmd-open-custom-role",
    )
    assert controlled["controls"][-1]["role"] == route_key
    with pytest.raises(ValueError, match="unsupported control role"):
        store.request_control(
            state["manifest_path"],
            "open_tab",
            role="DEV",
            external_command_id="cmd-open-unselected-role",
        )

    def stop(current: dict) -> dict:
        current["controls"] = []
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "DONE_STOPPED"
        current["stopped_at"] = current["updated_at"]
        current["stop_reason"] = "replacement fixture"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "stopped"
        return current

    store.update(state["manifest_path"], stop)
    result = store.replace_task_and_rewire(
        state["task_id"],
        "Continue with the same custom workflow",
        reuse_team=True,
        rewire_children=False,
        incident_id="incident-custom-replacement",
    )
    replacement = result["replacement"]
    assert list(replacement["roles"]) == ["PLAN", "REVIEW", route_key]
    assert replacement["workflow_agents"][route_key]["system_prompt"] == (
        "REPLACEMENT_SNAPSHOT_PROMPT"
    )


def test_delete_idle_independent_agent_and_reject_builtin(tmp_path: Path):
    _config, store = setup(tmp_path)
    custom = store.create_independent_agent(
        "Disposable Independent",
        system_prompt="DISPOSABLE_INDEPENDENT_PROMPT",
        trigger_settings={},
        task_id="task-disposable-independent",
        external_command_id="cmd-create-disposable-independent",
    )
    deleted = store.delete_independent_agent(
        custom["manifest_path"],
        external_command_id="cmd-delete-disposable-independent",
    )
    assert deleted["status"] == "STOPPED"
    assert deleted["independent"]["enabled"] is False
    assert deleted["independent"]["deleted_at"]
    repeated = store.delete_independent_agent(
        custom["manifest_path"],
        external_command_id="cmd-delete-disposable-independent-again",
    )
    assert repeated["independent"]["deleted_at"] == deleted["independent"]["deleted_at"]
    assert "cmd-delete-disposable-independent-again" in repeated["applied_command_ids"]

    builtin = store.create_independent_agent(
        "Maintainers",
        system_prompt="BUILTIN_PROMPT",
        trigger_settings={},
        task_id="task-maintainers-delete-reject",
        external_command_id="cmd-create-maintainers-delete-reject",
    )
    with pytest.raises(ValueError, match="built-in independent agents cannot be deleted"):
        store.delete_independent_agent(
            builtin["manifest_path"],
            external_command_id="cmd-delete-maintainers-reject",
        )


def test_agents_snapshot_api_and_workflow_command_dispatch(tmp_path: Path):
    config, store = setup(tmp_path)
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    api = DashboardAPI(config, db=worker.runtime_db)

    snapshot = worker.runtime_db.get_snapshot("agents")
    assert snapshot is not None
    assert [item["route_key"] for item in snapshot["payload"]["workflow"]][:5] == [
        "PLAN",
        "DEV",
        "TEST",
        "REVIEW",
        "AUDIT",
    ]

    payload = api.normalize_workflow_agent_create(
        {"name": "Researcher", "system_prompt": "API_CUSTOM_PROMPT"}
    )
    command = api.enqueue(
        idempotency_key="api-create-workflow-agent",
        kind="create_workflow_agent",
        task_id=None,
        payload=payload,
    )
    applied = worker.dispatch_command_once()
    assert applied["command_id"] == command["command_id"]
    assert applied["status"] == "applied"
    result = applied["result"]
    route_key = result["route_key"]
    assert store.workflow_agents.get(route_key)["system_prompt"] == "API_CUSTOM_PROMPT"
    assert any(
        item["route_key"] == route_key
        for item in worker.runtime_db.get_snapshot("agents")["payload"]["workflow"]
    )


def test_dashboard_add_agents_panel_and_dynamic_create_task_contract():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    actions = (ASSET_ROOT / "views" / "dashboard_actions.js").read_text(encoding="utf-8")

    assert "data-open-agent>Add Agents</button>" in html
    assert 'id="agents-panel"' in html
    assert 'name="agent_select"' in html
    assert ">New agent</button>" in html
    assert 'name="independent"' in html
    assert 'name="trigger_type"' in html
    assert 'value="manual"' in html
    assert 'value="interval"' in html
    assert 'value="task_done"' in html
    assert 'value="role_completed"' in html
    assert 'value="task_state"' in html
    assert 'value="check_all"' in html
    assert 'value="recovery"' in html
    assert 'id="workflow-agent-options"' in html
    assert "fetchAgents" in app
    assert "/api/agents" in app
    assert "renderWorkflowAgentOptions" in actions
    assert "Independent agents must not appear" in actions
