from __future__ import annotations

import json
from pathlib import Path

from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import setup_task


def _accept_route(worker, state, route: str):
    hop = _active_hop(state)
    state["roles"][str(hop["target_role"])]["turn"] = int(hop["turn"])
    handoff = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        f"{state['task_id']}.md"
    )
    hop["expected_report_path"] = handoff
    report = Path(str(state["repository"])) / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"# {hop['physical_role']} turn {hop['turn']} report\n", encoding="utf-8")
    hop["receipt"] = {"accepted_via": "test", "attempts": 1}
    hop["response"] = json.dumps({"route": route, "handoff": handoff})
    hop["state"] = "responded"
    worker._responded(state, hop)
    return hop


def test_alternating_routes_do_not_trigger_context_rotation(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-alternating-budget",
        roles=("PLAN", "DEV", "REVIEW"),
    )

    for route in ("DEV", "REVIEW", "DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_route(worker, state, route)

    current = _active_hop(state)
    assert state["status"] == "RUNNING"
    assert state["active_role"] == "DEV"
    assert state["roles"]["DEV"]["reset_requested"] is False
    assert state["block_code"] is None
    assert current["hop_id"] == 8
    assert state["execution_budget"]["charged_hop_ids"] == list(range(1, 9))
    assert not any(
        event.get("kind") == "alternating_route_context_rotation"
        for event in state["route_timeline"]
    )


def test_route_churn_is_bounded_by_gate_a_route_budget_before_execution_budget(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-route-churn-budget",
        roles=("PLAN", "DEV", "REVIEW"),
    )

    routes = ["DEV", "REVIEW"] * 6
    for route in routes:
        source = _accept_route(worker, state, route)
        if state["status"] == "BLOCKED":
            break

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "ROUTE_BUDGET_EXHAUSTED"
    assert state["block_retryable"] is False
    assert state["route_seq"] == state["max_routes"] == 8
    assert source["state"] == "responded"
    assert state["active_hop_id"] == 9
    assert len(state["hops"]) == 9
    assert len(state["execution_budget"]["charged_hop_ids"]) < 12
    assert RequestLedger(source["ledger_path"]).peek(f"{state['task_id']}-hop10") is None


def test_route_budget_survives_manifest_reload_without_double_charge(tmp_path):
    config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-restart-stable",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    path = Path(state["manifest_path"])

    for route in ("DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_route(worker, state, route)
    assert state["route_seq"] == 5
    store.save(path, state)
    reloaded = store.load(path)
    restarted = CDPAWorker(config, store=store)

    assert reloaded["route_seq"] == 5
    assert reloaded["execution_budget"]["charged_hop_ids"] == list(range(1, 7))
    for route in ("REVIEW", "DEV", "REVIEW", "DEV"):
        final = _accept_route(restarted, reloaded, route)
        if reloaded["status"] == "BLOCKED":
            break

    assert reloaded["status"] == "BLOCKED"
    assert reloaded["block_code"] == "ROUTE_BUDGET_EXHAUSTED"
    assert reloaded["route_seq"] == reloaded["max_routes"] == 8
    assert reloaded["active_hop_id"] == 9
    assert final["state"] == "responded"
    assert len(reloaded["execution_budget"]["charged_hop_ids"]) == 9
    assert RequestLedger(final["ledger_path"]).peek(f"{reloaded['task_id']}-hop10") is None
