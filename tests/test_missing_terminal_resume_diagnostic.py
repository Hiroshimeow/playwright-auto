from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

import playwright_auto.observability as observability
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_store import task_goal_for_hop
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from playwright_auto.upload import FileIdentity
from test_accepted_temporary_lost_resume import accepted_task, LostPage


@pytest.fixture(autouse=True)
def isolated_events(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_ACTION_EVENT_LOG", tmp_path / "action.jsonl")
    monkeypatch.setattr(observability, "_LIVE_EVENT_LOG", tmp_path / "live.jsonl")
    monkeypatch.setattr(observability, "_LIVE_EVENT_ENABLED", False)


def missing_terminal_task(tmp_path):
    store, state, worker, path, hop = accepted_task(tmp_path)
    state.update(status="BLOCKED", block_code="empty_terminal_response")
    hop.setdefault("wait", {})["controller_reason"] = "empty_terminal_response"
    return store, state, worker, path, hop


def operator_resume(state, hop, worker):
    # Preserve the previous result's real diagnostic; do not rewrite controller history.
    before = {"status": state["status"], "block_code": state.get("block_code")}
    control = {
        "control_id": len(state.get("controls") or []) + 1,
        "action": "resume", "role": hop["target_role"],
        "origin": "operator", "status": "recovering", "result": {"before": before},
    }
    state.setdefault("controls", []).append(control)
    state.update(status="RUNNING", block_code=None, block_reason=None)
    actions = LostPage()
    asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))
    return control, actions


@pytest.mark.parametrize("missing_path", [None, ""])
def test_missing_contract_diagnostic_does_not_claim_receipt_mismatch(tmp_path, missing_path):
    _, state, worker, _, hop = missing_terminal_task(tmp_path)
    hop["expected_report_path"] = missing_path
    state["goal"] = ""
    revised = "Review retained model measurements read-only with the current task goal."
    state["goal_revisions"] = [{"revision": 1, "goal": revised, "applies_from_hop_id": 2}]
    ledger = Path(hop["ledger_path"])
    before = ledger.read_bytes()
    receipt = copy.deepcopy(hop["receipt"])

    control, actions = operator_resume(state, hop, worker)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "accepted_send_provenance_unverified"
    assert "no recorded expected report path" in state["block_reason"]
    assert "does not match its durable Send evidence" not in state["block_reason"]
    assert "Do not infer it from a later goal revision" in control["result"]["next_safe_action"]
    assert hop["receipt"] == receipt and ledger.read_bytes() == before
    assert hop["expected_report_path"] == missing_path
    assert task_goal_for_hop(state, 1) == "" and task_goal_for_hop(state, 2) == revised
    assert len(state["hops"]) == 1 and actions.lookups == 0


@pytest.mark.parametrize("guard", [
    "missing_report_contract", "receipt_mismatch", "task_attachment",
    "durable_files", "upload_receipt", "uploaded_generation",
])
def test_repeated_resume_cannot_drop_missing_terminal_guards(tmp_path, guard):
    _, state, worker, _, hop = missing_terminal_task(tmp_path)
    ledger = RequestLedger(hop["ledger_path"])
    if guard == "missing_report_contract":
        hop["expected_report_path"] = None
    elif guard == "receipt_mismatch":
        wrong = copy.deepcopy(hop["receipt"])
        wrong["user_message_id"] = "different-message"
        ledger.update(hop["request_id"], receipt=wrong)
    elif guard == "task_attachment":
        state["attachments"] = [{"path": "retained", "name": "evidence.txt"}]
    elif guard == "durable_files":
        path = Path(hop["ledger_path"])
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["records"][hop["request_id"]]["files"] = [
            FileIdentity("retained", "evidence.txt", 3, "a" * 64, "text/plain").to_dict()
        ]
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8")
    elif guard == "upload_receipt":
        ledger.update(hop["request_id"], upload_receipt={"request_marker": "retained", "files": []})
    else:
        state["roles"]["REVIEW"]["attachments_uploaded_generation"] = 1
    before = Path(hop["ledger_path"]).read_bytes()
    receipt = copy.deepcopy(hop["receipt"])

    for _ in range(2):
        control, actions = operator_resume(state, hop, worker)
        assert state["status"] == "BLOCKED"
        assert control["status"] == "recovery_required"
        assert _active_hop(state) is hop and len(state["hops"]) == 1
        assert hop["receipt"] == receipt
        assert Path(hop["ledger_path"]).read_bytes() == before
        assert not actions.fresh_urls
        if guard == "receipt_mismatch":
            assert "does not match its durable Send evidence" in state["block_reason"]
        elif guard == "missing_report_contract":
            assert "no recorded expected report path" in state["block_reason"]


def test_restored_exact_contract_allows_existing_recovery_without_parent_replay(tmp_path):
    _, state, worker, _, hop = missing_terminal_task(tmp_path)
    original = hop["expected_report_path"]
    hop["expected_report_path"] = None
    before = Path(hop["ledger_path"]).read_bytes()
    operator_resume(state, hop, worker)
    assert state["status"] == "BLOCKED"
    # Only the fixture's retained original contract is restored, never a generated identity.
    hop["expected_report_path"] = original

    control, actions = operator_resume(state, hop, worker)

    child = _active_hop(state)
    assert state["status"] == "RUNNING" and control["status"] == "applied"
    assert child["kind"] == "execution_recovery" and child["recovery_of_request_id"] == hop["request_id"]
    assert hop["state"] == "abandoned" and hop["expected_report_path"] == original
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert RequestLedger(hop["ledger_path"]).peek(child["request_id"]) is None
    assert not actions.fresh_urls


@pytest.mark.parametrize("action", ["restart_role", "new_chat"])
def test_confirmed_context_reset_cannot_skip_accepted_boundary(tmp_path, action):
    store, state, _, path, hop = missing_terminal_task(tmp_path)
    state = store.save(path, state)
    manifest_before = path.read_bytes()
    ledger_before = Path(hop["ledger_path"]).read_bytes()

    eligibility = workflow_control_eligibility(state, action, role="REVIEW")
    assert eligibility["eligible"] is False
    with pytest.raises(ValueError, match="accepted Send boundary"):
        store.request_control(path, action, role="REVIEW", confirmed=True)

    assert path.read_bytes() == manifest_before
    assert Path(hop["ledger_path"]).read_bytes() == ledger_before
