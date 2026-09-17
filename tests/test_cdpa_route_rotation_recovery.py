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


def test_route_churn_is_bounded_only_by_durable_execution_budget(tmp_path):
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
    assert state["block_code"] == "autonomous_execution_budget_exhausted"
    assert state["block_retryable"] is False
    assert source["state"] == "routed"
    assert source["route"] == routes[-1]
    assert state["active_hop_id"] == 12
    assert state["execution_budget"]["charged_hop_ids"] == list(range(1, 13))
    assert len(state["hops"]) == 12
    assert RequestLedger(source["ledger_path"]).peek(f"{state['task_id']}-hop13") is None


def test_execution_budget_survives_manifest_reload_without_double_charge(tmp_path):
    config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-restart-stable",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    path = Path(state["manifest_path"])

    for route in ("DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_route(worker, state, route)
    store.save(path, state)
    reloaded = store.load(path)
    restarted = CDPAWorker(config, store=store)

    assert reloaded["execution_budget"]["charged_hop_ids"] == list(range(1, 7))
    for route in ("REVIEW", "DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_route(restarted, reloaded, route)

    assert reloaded["status"] == "RUNNING"
    assert reloaded["active_hop_id"] == 12
    assert reloaded["execution_budget"]["charged_hop_ids"] == list(range(1, 13))

    final = _accept_route(restarted, reloaded, "REVIEW")
    assert reloaded["status"] == "BLOCKED"
    assert reloaded["active_hop_id"] == 12
    assert final["state"] == "routed"
    assert len(reloaded["execution_budget"]["charged_hop_ids"]) == 12
    assert RequestLedger(final["ledger_path"]).peek(f"{reloaded['task_id']}-hop13") is None
