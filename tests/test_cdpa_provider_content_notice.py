from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.durable import RequestLedger, RequestStatus
from test_cdpa_worker import FakeActions, _accept_test_hop, setup_task

NOTICE = (
    "This content can’t be shown This content was flagged for possible cybersecurity risk. "
    "You may be eligible for Daybreak Blue, which reduces refusals for authorized defensive "
    "work on supported models. Other safeguards still apply. Learn more"
)
SHORT_NOTICE = "This content can't be shown This content was flagged for possible cybersecurity risk."


def prepared_response(tmp_path, *, mode="legacy_solo", response=NOTICE, exhausted=False):
    _config, store, state, worker = setup_task(
        tmp_path, roles=("PLAN",), workflow_mode="solo" if "solo" in mode else "team"
    )
    hop = state["hops"][-1]
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    _accept_test_hop(state, hop)
    # Cover historical Solo manifests as well as current file reports.
    if "solo" in mode:
        state["workflow_mode"] = "solo"
    if mode == "legacy_solo":
        hop["expected_report_path"] = None
    hop["state"] = "responded"
    hop["response"] = response
    if exhausted:
        state["route_repair_count"] = state["max_route_repairs"]
        state.update(status="BLOCKED", block_code="ROUTE_VALIDATION_EXHAUSTED",
                     block_retryable=False, block_reason="Old format-error diagnosis")
    return store, state, hop, worker


@pytest.mark.parametrize("mode", ["legacy_solo", "modern_solo", "team"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_provider_notice_preserves_accepted_response_without_format_repair(tmp_path, mode, exhausted):
    _store, state, hop, worker = prepared_response(tmp_path, mode=mode, exhausted=exhausted)
    ledger = RequestLedger(hop["ledger_path"])
    before = ledger.get(hop["request_id"])
    hops = len(state["hops"])
    budget = state["route_repair_count"]

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "provider_content_restricted"
    assert state["block_retryable"] is False
    assert len(state["hops"]) == hops
    assert state["active_hop_id"] == hop["hop_id"]
    assert state["route_repair_count"] == budget
    after = ledger.get(hop["request_id"])
    assert after.status is RequestStatus.COMPLETED
    assert after.receipt == before.receipt
    assert after.attempts == before.attempts == 1
    assert after.response["text"] == NOTICE
    saved = Path(hop["ledger_path"]).read_bytes()

    worker._responded(state, hop)
    assert state["block_code"] == "provider_content_restricted"
    assert Path(hop["ledger_path"]).read_bytes() == saved


@pytest.mark.parametrize("response", [
    SHORT_NOTICE, NOTICE.replace("’", "'").replace(" ", "\n"),
])
def test_provider_notice_whitespace_and_apostrophe_variants(tmp_path, response):
    _store, state, hop, worker = prepared_response(tmp_path, response=response)
    worker._responded(state, hop)
    assert state["block_code"] == "provider_content_restricted"
    assert len(state["hops"]) == 1


@pytest.mark.parametrize("response", [
    "Historical quotation: " + NOTICE,
    NOTICE + " Here is my actual review.",
    chr(96) * 3 + "\n" + NOTICE + "\n" + chr(96) * 3,
    json.dumps({"route": "PAUSE", "handoff": "The report quotes: " + NOTICE}),
    "The cybersecurity review found two bugs.",
])
def test_provider_notice_requires_exact_terminal_envelope(tmp_path, response):
    _store, state, hop, worker = prepared_response(tmp_path, response=response)
    worker._responded(state, hop)
    assert state.get("block_code") != "provider_content_restricted"


def test_exhausted_notice_allows_diagnosis_but_not_higher_repair_budget(tmp_path):
    _store, state, hop, _worker = prepared_response(tmp_path, exhausted=True)
    assert workflow_control_eligibility(state, "resume")["eligible"]
    denied = workflow_control_eligibility(state, "increase_route_limits")
    assert not denied["eligible"]
    assert "provider" in denied["reason"].lower()
    assert state["route_repair_count"] == state["max_route_repairs"]


def test_notice_resume_is_read_only_and_idempotent(tmp_path):
    _store, state, hop, worker = prepared_response(tmp_path, exhausted=True)
    worker._complete_request_response(hop)
    saved = Path(hop["ledger_path"]).read_bytes()
    before_hops = len(state["hops"])
    before_budget = state["route_repair_count"]
    for index in range(2):
        control = {"control_id": f"notice-diagnosis-{index}", "action": "resume",
                   "status": "recovering", "role": "PLAN"}
        asyncio.run(worker._recover_resume_control(state, control, object()))
        assert state["status"] == "BLOCKED"
        assert state["block_code"] == "provider_content_restricted"
        assert control["status"] == "recovery_required"
        assert Path(hop["ledger_path"]).read_bytes() == saved
        assert len(state["hops"]) == before_hops
        assert state["route_repair_count"] == before_budget


def test_exhausted_generic_format_error_remains_bounded(tmp_path):
    _store, state, hop, _worker = prepared_response(
        tmp_path, response="not a route object", exhausted=True
    )
    assert not workflow_control_eligibility(state, "resume")["eligible"]
    assert workflow_control_eligibility(state, "increase_route_limits")["eligible"]


class NoNoticeBrowserActions:
    def __init__(self):
        self.calls = []

    async def preflight_team(self, *_args, **_kwargs):
        self.calls.append("preflight_team")
        return [object()]

    async def close_team(self, *_args, **_kwargs):
        self.calls.append("close_team")

    def __getattr__(self, name):
        raise AssertionError(f"Provider diagnosis must not invoke browser action {name}")


def test_provider_notice_block_preserves_owned_page_for_review(tmp_path):
    _store, state, hop, worker = prepared_response(tmp_path)
    worker._responded(state, hop)
    actions = NoNoticeBrowserActions()
    asyncio.run(worker._release_blocked_workflow_tabs(state, actions))
    assert actions.calls == []


def test_provider_notice_command_and_following_tick_do_not_touch_browser(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from playwright_auto import cdpa_worker as worker_module

    store, state, hop, worker = prepared_response(tmp_path, exhausted=True)
    worker._complete_request_response(hop)
    saved_ledger = Path(hop["ledger_path"]).read_bytes()
    path = Path(state["manifest_path"])
    store.save(path, state)
    queued = store.request_resume(path, reason="reconcile retained provider notice")
    control = queued["controls"][-1]
    assert worker_module._control_can_run_endpointless(queued, control)
    original_request = hop["request_id"]
    original_hops = len(state["hops"])
    original_budget = state["route_repair_count"]
    actions = NoNoticeBrowserActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    async def run_command_and_tick():
        result = await worker.advance(
            path, SimpleNamespace(pages=[]),
            expected_endpointless_control=(control["control_id"], "resume"),
        )
        assert result["controls"][-1]["status"] == "recovery_required"
        assert result["block_code"] == "provider_content_restricted"
        return await worker.advance(path, SimpleNamespace(pages=[]))

    after = asyncio.run(run_command_and_tick())
    assert actions.calls == []
    assert after["status"] == "BLOCKED"
    assert after["block_code"] == "provider_content_restricted"
    assert len(after["hops"]) == original_hops
    assert after["hops"][-1]["request_id"] == original_request
    assert after["route_repair_count"] == original_budget
    assert Path(hop["ledger_path"]).read_bytes() == saved_ledger
