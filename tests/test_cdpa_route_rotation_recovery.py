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


def test_team_handoffs_continue_past_both_historical_counts(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-route-churn-budget",
        roles=("PLAN", "DEV", "REVIEW"),
        max_routes=8,
    )

    routes = ["DEV", "REVIEW"] * 7
    for route in routes:
        source = _accept_route(worker, state, route)
        assert state["status"] == "RUNNING"

    assert state["block_code"] is None
    assert state["max_routes"] == 8
    assert state["route_seq"] == len(routes)
    assert source["state"] == "routed"
    assert state["active_role"] == "REVIEW"
    assert state["active_hop_id"] == len(routes) + 1
    assert len(state["hops"]) == len(routes) + 1
    assert RequestLedger(source["ledger_path"]).peek(_active_hop(state)["request_id"]) is None


def test_team_handoffs_continue_after_reload_preserving_counts(tmp_path):
    config, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-restart-stable",
        roles=("PLAN", "DEV", "REVIEW"),
        max_routes=8,
    )
    path = Path(state["manifest_path"])

    for route in ("DEV", "REVIEW", "DEV", "REVIEW", "DEV"):
        _accept_route(worker, state, route)
    assert state["route_seq"] == 5
    state["route_budget_start_seq"] = 2
    store.save(path, state)
    reloaded = store.load(path)
    restarted = CDPAWorker(config, store=store)

    assert reloaded["route_seq"] == 5
    assert reloaded["execution_budget"]["charged_hop_ids"] == list(range(1, 7))
    for route in ("REVIEW", "DEV") * 4:
        final = _accept_route(restarted, reloaded, route)
        assert reloaded["status"] == "RUNNING"

    assert reloaded["block_code"] is None
    assert reloaded["max_routes"] == 8
    assert reloaded["route_budget_start_seq"] == 2
    assert reloaded["route_seq"] == 13
    assert reloaded["active_hop_id"] == 14
    assert final["state"] == "routed"
    assert reloaded["execution_budget"]["charged_hop_ids"] == list(range(1, 15))
