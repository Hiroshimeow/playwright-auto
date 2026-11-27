from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.observability as observability
from playwright_auto.cdpa_actions import AcquiredRole, RoleOwnershipError
from playwright_auto.cdpa_store import task_goal_for_hop
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from playwright_auto.upload import FileIdentity
from test_cdpa_temporary_workflow import TemporaryWorkflowActions
from test_cdpa_worker import setup_task, _accept_test_hop


@pytest.fixture(autouse=True)
def isolated_events(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "_ACTION_EVENT_LOG", tmp_path / "action.jsonl")
    monkeypatch.setattr(observability, "_LIVE_EVENT_LOG", tmp_path / "live.jsonl")
    monkeypatch.setattr(observability, "_LIVE_EVENT_ENABLED", False)


def accepted_task(tmp_path):
    _, store, state, worker = setup_task(
        tmp_path, task_id="accepted-lost-temporary",
        conversation_mode="temporary", workflow_mode="solo", roles=("REVIEW",),
    )
    state["goal"] = "Review the retained candidate read-only; preserve all prior work and external effects."
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, TemporaryWorkflowActions()))
    _accept_test_hop(state, hop)
    worker._start_wait_budget_from_sent(hop)
    state = store.save(path, state)
    return store, state, worker, path, _active_hop(state)


def resume_control(state, hop, *, origin="operator", prior_status="BLOCKED"):
    control = {
        "control_id": 1, "action": "resume", "role": hop["target_role"],
        "origin": origin, "status": "recovering",
        "result": {"before": {"status": prior_status, "block_code": "empty_terminal_response"}},
    }
    state["controls"] = [control]
    state.update(status="RUNNING", block_code=None, block_reason=None)
    hop.setdefault("wait", {})["controller_reason"] = "empty_terminal_response"
    return control


class LostPage(TemporaryWorkflowActions):
    def __init__(self):
        super().__init__(owned=False)
        self.lookups = 0

    async def locate_owned(self, *_args, **_kwargs):
        self.lookups += 1
        return None

    async def fresh_chat(self, *_args, **_kwargs):
        raise AssertionError("Resume queues recovery; it must not create a page or Send")

    async def reopen(self, *_args, **_kwargs):
        raise AssertionError("Lost Temporary URLs are not reopen targets")


def test_operator_resume_lost_accepted_temporary_queues_one_new_execution(tmp_path):
    _, state, worker, _, hop = accepted_task(tmp_path)
    ledger = Path(hop["ledger_path"])
    before = ledger.read_bytes()
    parent = copy.deepcopy(hop)
    endpoint = state["browser_endpoint_id"]
    control = resume_control(state, hop)
    actions = LostPage()

    asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))

    child = _active_hop(state)
    assert state["status"] == "RUNNING"
    assert child is not hop and child["kind"] == "execution_recovery"
    assert child["request_id"] != parent["request_id"]
    assert child["recovery_of_request_id"] == parent["request_id"]
    assert child["state"] == "pre_send"
    assert hop["state"] == "abandoned"
    assert hop["receipt"] == parent["receipt"] and hop["prompt"] == parent["prompt"]
    assert hop["expected_report_path"] == parent["expected_report_path"]
    assert parent["prompt"] not in child["handoff"]
    assert state["browser_endpoint_id"] == endpoint
    assert task_goal_for_hop(state, child["hop_id"]) == state["goal"]
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "recovery_continuation_queued"
    assert ledger.read_bytes() == before
    records = json.loads(before)["records"]
    assert set(records) == {parent["request_id"]}
    assert records[parent["request_id"]]["attempts"] == 1
    assert RequestLedger(ledger).peek(child["request_id"]) is None
    assert actions.lookups == 1 and not actions.fresh_urls


@pytest.mark.parametrize("guard", [
    "automatic", "not_blocked", "receipt_mismatch", "no_receipt", "missing_report_contract",
    "task_attachment", "durable_files", "upload_receipt", "uploaded_generation",
    "persistent", "ownership_ambiguous", "other_live_page", "other_endpoint",
])
def test_lost_accepted_recovery_preserves_guards(tmp_path, monkeypatch, guard):
    _, state, worker, _, hop = accepted_task(tmp_path)
    ledger = RequestLedger(hop["ledger_path"])
    actions = LostPage()
    control = resume_control(
        state, hop, origin="automatic" if guard == "automatic" else "operator",
        prior_status="PAUSED" if guard == "not_blocked" else "BLOCKED",
    )
    if guard == "receipt_mismatch":
        wrong = copy.deepcopy(hop["receipt"])
        wrong["user_message_id"] = "different-message"
        ledger.update(hop["request_id"], receipt=wrong)
    elif guard == "no_receipt":
        hop["receipt"] = None
    elif guard == "missing_report_contract":
        hop["expected_report_path"] = None
    elif guard == "task_attachment":
        state["attachments"] = [{"path": "owned-attachment", "name": "evidence.txt"}]
    elif guard == "durable_files":
        # Fixture construction only: production accepted files are immutable.
        ledger_path = Path(hop["ledger_path"])
        fixture = json.loads(ledger_path.read_text(encoding="utf-8"))
        fixture["records"][hop["request_id"]]["files"] = [
            FileIdentity("owned", "evidence.txt", 3, "a"*64, "text/plain").to_dict()
        ]
        ledger_path.write_text(json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8")
    elif guard == "upload_receipt":
        ledger.update(hop["request_id"], upload_receipt={"request_marker": "owned", "files": []})
    elif guard == "uploaded_generation":
        state["roles"]["REVIEW"]["attachments_uploaded_generation"] = 1
    elif guard == "persistent":
        state["conversation_mode"] = "persistent"
    elif guard == "ownership_ambiguous":
        actions.locate_owned = AsyncMock(side_effect=RoleOwnershipError("multiple exact owners"))
    elif guard == "other_live_page":
        acquired = AcquiredRole(
            client=SimpleNamespace(), page_id="another-live-page",
            url="https://chatgpt.com/c/another", created=False, new_chat=False,
        )
        actions.locate_owned = AsyncMock(return_value=acquired)
    elif guard == "other_endpoint":
        monkeypatch.setattr(worker, "_execution_endpoint_id", lambda *_args: "foreign-endpoint")
    before = Path(hop["ledger_path"]).read_bytes()
    request = hop["request_id"]
    receipt = copy.deepcopy(hop["receipt"])

    asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))

    assert _active_hop(state) is hop and hop["request_id"] == request
    assert len(state["hops"]) == 1
    assert state["status"] == "BLOCKED"
    assert control["status"] == "recovery_required"
    assert hop["receipt"] == receipt
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert ledger.peek(request).attempts == 1
    assert not actions.fresh_urls


def test_valid_report_wins_without_new_execution_or_browser_lookup(tmp_path):
    _, state, worker, _, hop = accepted_task(tmp_path)
    expected = hop["expected_report_path"]
    report = tmp_path / expected
    report.parent.mkdir(parents=True, exist_ok=True)
    route = json.dumps({"route": "DONE", "handoff": expected})
    report.write_text("# Verified fixture report\n\nCDPA-RESULT " + hop["report_result"]["key"] + ": " + route + "\n", encoding="utf-8")
    ledger = Path(hop["ledger_path"])
    before = ledger.read_bytes()
    control = resume_control(state, hop)
    actions = LostPage()

    asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))

    assert state["status"] == "DONE"
    assert len(state["hops"]) == 1
    assert control["result"]["postcondition"] == "report_route_recovered"
    assert ledger.read_bytes() == before
    assert actions.lookups == 0


@pytest.mark.parametrize("action,expected_status", [("pause", "PAUSED"), ("stop", "STOPPED")])
def test_manual_pause_stop_never_queue_execution_recovery(tmp_path, action, expected_status):
    store, state, worker, path, hop = accepted_task(tmp_path)
    state.update(status="BLOCKED", kanban_column="BLOCKED", active_action="blocked",
                 block_code="empty_terminal_response", block_reason="accepted result missing", block_retryable=False)
    state = store.save(path, state)
    receipt = copy.deepcopy(hop["receipt"])
    request = hop["request_id"]
    pending = store.request_control(path, action, reason="explicit operator hold")
    actions = LostPage()

    assert asyncio.run(worker._apply_control(pending, actions, path)) is True
    saved = store.load(path)

    assert saved["status"] == expected_status
    assert len(saved["hops"]) == 1
    assert saved["hops"][0]["request_id"] == request
    assert saved["hops"][0]["receipt"] == receipt
    assert RequestLedger(hop["ledger_path"]).peek(request).attempts == 1
    assert not actions.fresh_urls


def test_soda_legacy_goal_revision_is_preserved_without_inventing_old_report(tmp_path):
    _, state, worker, _, hop = accepted_task(tmp_path)
    old_expected = None
    hop["expected_report_path"] = old_expected
    hop.pop("report_result", None)
    state["goal"] = ""
    revised = "Audit existing model research artefacts; preserve prior accepted work."
    state["effective_goal"] = revised
    state["goal_revisions"] = [{"revision": 1, "goal": revised, "applies_from_hop_id": 2}]
    before = Path(hop["ledger_path"]).read_bytes()
    control = resume_control(state, hop)
    actions = LostPage()

    asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))

    assert _active_hop(state) is hop
    assert hop["expected_report_path"] is None
    assert state["block_code"] == "accepted_send_provenance_unverified"
    assert task_goal_for_hop(state, 1) == ""
    assert task_goal_for_hop(state, 2) == revised
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert actions.lookups == 0
