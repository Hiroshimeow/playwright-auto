"""Operator Resume must establish a substantive turn without replaying accepted work."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import FakeActions, _prepare_sent_waiting_task

GOAL = "RESEARCH ONLY: finish verified documents; no GPU, training, package install, or private audio."
REASON = "The shell prerequisite is verified resolved. Finish the unfinished research under the existing scope."
HEADER = "OPERATOR RESUME FOR THIS TURN"


def _resumed_report_pause(tmp_path, *, temporary=False, origin=None):
    store, state, worker, path, parent, _, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="resume-context", solo=True,
        conversation_mode="temporary" if temporary else None,
    )
    state["goal"] = GOAL
    expected = parent["expected_report_path"]
    parent.update(kind="report_repair", locked_route="PAUSE", locked_handoff=expected)
    footer = json.dumps({"route": "PAUSE", "handoff": expected})
    (tmp_path / expected).write_text(
        "# Report repair\nCONTROLLER_LOCKED_REPORT_ONLY\n"
        "The shell prerequisite is resolved; research is unfinished.\n"
        f"CDPA-RESULT {parent['report_result']['key']}: {footer}\n",
        encoding="utf-8",
    )
    ledger = Path(parent["ledger_path"])
    before_ledger = ledger.read_bytes()
    assert worker._poll_running_report_result(state, parent, force=True)
    assert state["status"] == "PAUSED"
    store.save(path, state)
    state = store.request_resume(path, reason=REASON)
    if origin is not None:
        state["controls"][-1]["origin"] = origin
    if temporary:
        from test_cdpa_temporary_workflow import TemporaryWorkflowActions
        actions = TemporaryWorkflowActions(owned=True)
    else:
        actions = FakeActions()
    asyncio.run(worker._apply_control(state, actions, path))
    child = _active_hop(state)
    assert child["kind"] == "solo_resume"
    assert ledger.read_bytes() == before_ledger
    return state, worker, child, actions, ledger, before_ledger, parent


@pytest.mark.parametrize("temporary", [False, True])
@pytest.mark.parametrize("origin", [None, "operator"])
def test_applied_operator_resume_carries_scope_and_reason_into_child_prompt(
    tmp_path, temporary, origin,
):
    state, worker, child, actions, ledger, before, parent = _resumed_report_pause(
        tmp_path, temporary=temporary, origin=origin,
    )
    parent_before = copy.deepcopy(next(h for h in state["hops"] if h["hop_id"] == parent["hop_id"]))
    request = child["request_id"]
    asyncio.run(worker._pre_send(state, child, actions))
    assert child["state"] == "sending"
    assert HEADER in child["prompt"]
    assert REASON in child["prompt"]
    assert GOAL in child["prompt"]
    assert "new substantive work turn" in child["prompt"]
    assert "report-only and format-repair restrictions applied only to those repair turns" in child["prompt"]
    assert "real external or manual prerequisite" in child["prompt"]
    assert "never replay accepted Sends or completed external side effects" in child["prompt"]
    assert parent["expected_report_path"] in child["prompt"]
    assert child["expected_report_path"] != parent["expected_report_path"]
    assert child["request_id"] == request != parent["request_id"]
    assert child["prompt_sha256"] == hashlib.sha256(child["prompt"].encode()).hexdigest()
    assert next(h for h in state["hops"] if h["hop_id"] == parent["hop_id"]) == parent_before
    assert ledger.read_bytes() == before
    assert actions.restart_roles == []


@pytest.mark.parametrize("mismatch", [
    "other_child", "other_after_child", "other_request", "other_role",
    "nonoperator", "nonoperator_command", "not_applied", "other_operation",
])
def test_resume_context_does_not_cross_control_provenance(tmp_path, mismatch):
    state, worker, child, actions, ledger, before, _ = _resumed_report_pause(tmp_path)
    control = state["controls"][-1]
    if mismatch == "other_child":
        control["result"]["child_hop_id"] = child["hop_id"] + 100
    elif mismatch == "other_after_child":
        control["result"]["after"]["active_hop_id"] = child["hop_id"] + 100
    elif mismatch == "other_request":
        control["result"]["after"]["active_request_id"] = "unrelated-request"
    elif mismatch == "other_role":
        control["role"] = "DEV"
    elif mismatch == "nonoperator":
        control["origin"] = "worker"
    elif mismatch == "nonoperator_command":
        control["command"] = {"origin": "independent_agent"}
    elif mismatch == "not_applied":
        control["status"] = "requested"
    else:
        control["result"]["action"] = "restart_role_fallback"
    asyncio.run(worker._pre_send(state, child, actions))
    assert child["state"] == "sending"
    assert HEADER not in child["prompt"]
    assert REASON not in child["prompt"]
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("repair_kind", ["route_repair", "report_repair"])
def test_repair_after_operator_resume_keeps_its_own_repair_scope(tmp_path, repair_kind):
    state, worker, resumed, actions, ledger, before, _ = _resumed_report_pause(tmp_path)
    repair = worker._append_hop(
        state, source_role=resumed["target_role"], target_role=resumed["target_role"],
        handoff="repair the role report", kind=repair_kind,
    )
    repair["validation_error"] = "report is missing"
    if repair_kind == "report_repair":
        repair["locked_route"] = "PAUSE"
        repair["locked_handoff"] = worker._expected_workflow_report(state, repair)
    asyncio.run(worker._pre_send(state, repair, actions))
    assert repair["state"] == "sending"
    assert HEADER not in repair["prompt"]
    assert REASON not in repair["prompt"]
    assert ("FORMAT REPAIR" if repair_kind == "route_repair" else "report-only repair") in repair["prompt"]
    assert ledger.read_bytes() == before
