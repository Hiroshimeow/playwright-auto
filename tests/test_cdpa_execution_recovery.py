from __future__ import annotations

import json
import subprocess
from pathlib import Path

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.durable import RequestLedger, RequestStatus
from test_cdpa_worker import _prepare_sent_waiting_task


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "notes.txt").write_text("unfinished work\n", encoding="utf-8")
    return path


def test_recovery_snapshot_uses_durable_workspace_and_send_evidence(tmp_path: Path):
    execution = _git_repo(tmp_path / "execution")
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-recovery-snapshot", repository=execution
    )
    state["reports"] = [{"path": ".plan/alpha/plan.md", "role": "PLAN"}]

    snapshot = worker._build_execution_recovery_snapshot(
        state, hop, reason="endpoint unavailable"
    )

    assert snapshot["task"]["task_id"] == state["task_id"]
    assert snapshot["task"]["team"] == state["team"]
    assert snapshot["execution"]["role"] == "PLAN"
    assert snapshot["execution"]["request_id"] == hop["request_id"]
    assert snapshot["send_provenance"]["ledger_status"] == "sent"
    assert snapshot["send_provenance"]["classification"] == "accepted_or_ambiguous"
    assert snapshot["workspace"]["repository"] == str(execution.resolve())
    assert "notes.txt" in snapshot["workspace"]["git_status"]
    assert snapshot["reports"][-1]["path"] == ".plan/alpha/plan.md"
    assert snapshot["reason"] == "endpoint unavailable"


def test_completed_durable_response_is_consumed_before_cross_endpoint_recovery(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-completed-before-relocate"
    )
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# completed PLAN work\n", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "assistant-completed",
        "turn-completed",
        json.dumps({"route": "TEST", "handoff": handoff}),
        (),
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.update(
        hop["request_id"],
        status=RequestStatus.COMPLETED,
        response=response.to_dict(),
    )

    assert worker._consume_completed_durable_response(state, hop) is True

    completed = state["hops"][0]
    child = _active_hop(state)
    assert completed["state"] == "routed"
    assert completed["request_id"] == hop["request_id"]
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    record = ledger.get(completed["request_id"])
    assert record is not None and record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_accepted_send_relocates_as_new_recovery_request_without_replay(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-cross-endpoint-recovery"
    )
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="old-page",
        page_url="https://chatgpt.com/c/old",
        online=False,
    )
    original_request_id = hop["request_id"]
    original_receipt = json.loads(json.dumps(hop["receipt"]))

    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="old endpoint offline"
    ) is True

    old_hop = state["hops"][0]
    recovery = _active_hop(state)
    assert old_hop["state"] == "abandoned"
    assert old_hop["request_id"] == original_request_id
    assert old_hop["receipt"] == original_receipt
    assert recovery["kind"] == "execution_recovery"
    assert recovery["target_role"] == "PLAN"
    assert recovery["request_id"] != original_request_id
    assert "Continue this existing role/task from durable state" in recovery["handoff"]
    assert original_request_id in recovery["handoff"]
    assert role["browser_endpoint_id"] == new_endpoint
    assert role["page_id"] is None
    assert role["page_url"] is None
    old_record = RequestLedger(old_hop["ledger_path"]).get(original_request_id)
    assert old_record is not None
    assert old_record.status is RequestStatus.SENT
    assert old_record.attempts == 1
