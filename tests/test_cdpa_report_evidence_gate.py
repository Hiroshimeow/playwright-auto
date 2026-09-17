from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import FakeActions, setup_task


def _prepare_report(state, hop, *, route: str, body: str = "# role report\n") -> str:
    expected = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_"
        f"{state['task_id']}.md"
    )
    hop["expected_report_path"] = expected
    report = Path(state["repository"]) / expected
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(body, encoding="utf-8")
    hop["response"] = json.dumps({"route": route, "handoff": expected})
    hop["state"] = "responded"
    return expected


def test_pre_send_does_not_persist_legacy_substantive_evidence_flag(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-no-evidence-flag",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert "substantive_evidence_required" not in hop


def test_report_content_is_not_semantically_policed(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-minimal-report-contract",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    hop["target_role"] = "DEV"
    hop["physical_role"] = state["roles"]["DEV"]["physical_role"]
    state["active_role"] = "DEV"
    hop["substantive_evidence_required"] = True  # legacy field must be harmless
    expected = _prepare_report(
        state,
        hop,
        route="REVIEW",
        body="# DEV report\nImplementation is unfinished; REVIEW should judge quality.\n",
    )

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["route"] == "REVIEW"
    assert hop["report_path"] == expected
    assert hop["report_sha256"]
    assert child["source_role"] == "DEV"
    assert child["target_role"] == "REVIEW"
    assert not any(
        event.get("kind") == "substantive_evidence_continuation"
        for event in state["route_timeline"]
    )


def test_budget_exhaustion_preserves_valid_report_and_decision_without_next_execution(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-exhaustion",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    hop["target_role"] = "DEV"
    hop["physical_role"] = state["roles"]["DEV"]["physical_role"]
    state["active_role"] = "DEV"
    state["execution_budget"] = {
        "epoch": 1,
        "limit": 12,
        "charged_hop_ids": list(range(1, 13)),
        "renewed_at": None,
    }
    expected = _prepare_report(state, hop, route="REVIEW")

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "autonomous_execution_budget_exhausted"
    assert state["active_hop_id"] == hop["hop_id"]
    assert hop["state"] == "routed"
    assert hop["route"] == "REVIEW"
    assert hop["report_path"] == expected
    assert hop["report_sha256"]
    assert len(state["hops"]) == 1
    assert RequestLedger(hop["ledger_path"]).peek(f"{state['task_id']}-hop2") is None


def test_budget_resume_preserves_pending_report_repair_instead_of_downstream_handoff(tmp_path):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-report-repair",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    state["execution_budget"] = {
        "epoch": 1,
        "limit": 12,
        "charged_hop_ids": list(range(1, 13)),
        "renewed_at": None,
    }
    expected = _prepare_report(state, hop, route="DEV")
    (Path(state["repository"]) / expected).unlink()

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "autonomous_execution_budget_exhausted"
    assert hop["route"] == "DEV"
    assert hop["report_path"] is None
    assert hop["budget_continuation"] == {
        "kind": "report_repair",
        "source_role": "PLAN",
        "target_role": "PLAN",
        "handoff": "report repair",
        "turn": hop["turn"],
        "repair_attempt": 1,
        "validation_error": hop["validation_error"],
        "locked_route": "DEV",
        "locked_handoff": expected,
    }
    path = Path(state["manifest_path"])
    store.save(path, state)

    resumed = store.request_resume(
        path,
        reason="renew budget for pending report repair",
        external_command_id="renew-report-repair-budget",
    )
    assert asyncio.run(worker._apply_control(resumed, FakeActions(), path)) is True

    repair = _active_hop(resumed)
    assert repair["kind"] == "report_repair"
    assert repair["source_role"] == "PLAN"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == hop["turn"]
    assert repair["repair_attempt"] == 1
    assert "missing" in repair["validation_error"].lower()
    assert repair["locked_route"] == "DEV"
    assert repair["locked_handoff"] == expected
    assert resumed["execution_budget"]["epoch"] == 2
    assert resumed["execution_budget"]["charged_hop_ids"] == [repair["hop_id"]]
    assert not any(
        item["kind"] == "handoff" and item["target_role"] == "DEV"
        for item in resumed["hops"]
        if item is not hop
    )


def test_budget_resume_recovers_legacy_pending_route_repair_context(tmp_path):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-route-repair",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    state["execution_budget"] = {
        "epoch": 1,
        "limit": 12,
        "charged_hop_ids": list(range(1, 13)),
        "renewed_at": None,
    }
    wrong = f".plan/{state['team']}/wrong-report.md"
    hop["expected_report_path"] = (
        f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_{state['task_id']}.md"
    )
    hop["response"] = json.dumps({"route": "DEV", "handoff": wrong})
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "autonomous_execution_budget_exhausted"
    assert hop["route"] == "PLAN"
    assert hop["budget_continuation"]["kind"] == "route_repair"
    hop.pop("budget_continuation")  # simulate a repair-blocked manifest from before this fix
    path = Path(state["manifest_path"])
    store.save(path, state)

    resumed = store.request_resume(
        path,
        reason="renew budget for pending route repair",
        external_command_id="renew-route-repair-budget",
    )
    assert asyncio.run(worker._apply_control(resumed, FakeActions(), path)) is True

    repair = _active_hop(resumed)
    assert repair["kind"] == "route_repair"
    assert repair["source_role"] == "PLAN"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == hop["turn"]
    assert repair["repair_attempt"] == 1
    assert repair["validation_error"] == hop["validation_error"]
    assert resumed["execution_budget"]["epoch"] == 2
    assert resumed["execution_budget"]["charged_hop_ids"] == [repair["hop_id"]]


def test_operator_resume_renews_exhausted_budget_and_allocates_exact_decided_route(tmp_path):
    _, store, state, worker = setup_task(
        tmp_path,
        task_id="task-budget-renewal",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    hop["target_role"] = "DEV"
    hop["physical_role"] = state["roles"]["DEV"]["physical_role"]
    state["active_role"] = "DEV"
    state["execution_budget"] = {
        "epoch": 3,
        "limit": 12,
        "charged_hop_ids": list(range(1, 13)),
        "renewed_at": "2026-09-15T00:00:00+00:00",
    }
    expected = _prepare_report(state, hop, route="REVIEW")
    worker._responded(state, hop)
    path = Path(state["manifest_path"])
    store.save(path, state)

    resumed = store.request_resume(
        path,
        reason="explicit operator renewal",
        external_command_id="renew-budget-1",
    )
    assert asyncio.run(worker._apply_control(resumed, FakeActions(), path)) is True

    child = _active_hop(resumed)
    assert resumed["status"] == "RUNNING"
    assert resumed["block_code"] is None
    assert resumed["execution_budget"]["epoch"] == 4
    assert resumed["execution_budget"]["charged_hop_ids"] == [child["hop_id"]]
    assert child["parent_hop_id"] == hop["hop_id"]
    assert child["source_role"] == "DEV"
    assert child["target_role"] == "REVIEW"
    assert child["handoff"] == expected
    assert child["state"] == "pre_send"
    assert RequestLedger(child["ledger_path"]).peek(child["request_id"]) is None


def test_clean_dev_review_plan_flow_can_finish_done(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-clean-closure",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    first = _active_hop(state)
    first["target_role"] = "DEV"
    first["physical_role"] = state["roles"]["DEV"]["physical_role"]
    state["active_role"] = "DEV"

    _prepare_report(state, first, route="REVIEW", body="# DEV\nImplementation complete.\n")
    worker._responded(state, first)
    review = _active_hop(state)
    _prepare_report(state, review, route="PLAN", body="# REVIEW\nNo concrete defect.\n")
    worker._responded(state, review)
    plan = _active_hop(state)
    _prepare_report(state, plan, route="DONE", body="# PLAN\nGoal closure verified.\n")
    worker._responded(state, plan)

    assert first["route"] == "REVIEW"
    assert review["route"] == "PLAN"
    assert plan["route"] == "DONE"
    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["active_hop_id"] is None


def test_review_can_route_a_concrete_defect_back_to_dev(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-review-defect",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    review = _active_hop(state)
    review["target_role"] = "REVIEW"
    review["physical_role"] = state["roles"]["REVIEW"]["physical_role"]
    state["active_role"] = "REVIEW"

    expected = _prepare_report(
        state,
        review,
        route="DEV",
        body="# REVIEW\nConcrete defect: accepted behavior is not yet satisfied.\n",
    )
    worker._responded(state, review)

    child = _active_hop(state)
    assert review["route"] == "DEV"
    assert review["report_path"] == expected
    assert child["source_role"] == "REVIEW"
    assert child["target_role"] == "DEV"
    assert child["state"] == "pre_send"


def test_legacy_policy_blocks_require_operator_resume_and_migrate_to_new_budget_epoch(tmp_path):
    for index, block_code in enumerate(
        ("consecutive_self_route_limit", "no_progress_route_cycle"), start=1
    ):
        repository = tmp_path / f"legacy-{index}"
        repository.mkdir()
        _, store, state, worker = setup_task(
            repository,
            task_id=f"task-legacy-policy-{index}",
            roles=("PLAN", "DEV", "REVIEW"),
        )
        hop = _active_hop(state)
        handoff = f".plan/{state['team']}/{hop['physical_role']}_turn{hop['turn']}_{state['task_id']}.md"
        report = Path(state["repository"]) / handoff
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# legacy accepted report\n", encoding="utf-8")
        hop.update(
            state="routed",
            route="PLAN",
            expected_report_path=handoff,
            report_path=handoff,
            report_sha256="legacy-preserved-report",
        )
        state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code=block_code,
            block_retryable=False,
            block_reason="explicit operator Resume required",
            active_action="blocked",
        )
        path = Path(state["manifest_path"])
        store.save(path, state)

        resumed = store.request_resume(
            path,
            reason="explicit operator legacy-policy migration",
            external_command_id=f"resume-legacy-policy-{index}",
        )
        assert asyncio.run(worker._apply_control(resumed, FakeActions(), path)) is True

        child = _active_hop(resumed)
        assert resumed["status"] == "RUNNING"
        assert resumed["block_code"] is None
        assert resumed["execution_budget"]["epoch"] == 2
        assert resumed["execution_budget"]["charged_hop_ids"] == [child["hop_id"]]
        assert child["source_role"] == "PLAN"
        assert child["target_role"] == "PLAN"
        assert child["handoff"] == handoff
        assert child["state"] == "pre_send"


def test_valid_done_decision_wins_over_exhausted_budget_without_extra_execution(tmp_path):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id="task-done-at-budget-boundary",
        roles=("PLAN", "DEV", "REVIEW"),
    )
    hop = _active_hop(state)
    state["execution_budget"] = {
        "epoch": 1,
        "limit": 12,
        "charged_hop_ids": list(range(1, 13)),
        "renewed_at": None,
    }
    _prepare_report(state, hop, route="DONE", body="# PLAN\nAuthorized terminal decision.\n")

    worker._responded(state, hop)

    assert hop["route"] == "DONE"
    assert state["status"] == "DONE"
    assert state["block_code"] is None
    assert state["active_hop_id"] is None
    assert len(state["hops"]) == 1
    assert RequestLedger(hop["ledger_path"]).peek(f"{state['task_id']}-hop2") is None
