from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger, RequestStatus
from types import SimpleNamespace
from test_cdpa_worker import FakeActions, setup_task


def blocked_fixture(tmp_path: Path, *, block_code="preboundary_acquisition_recovery_required"):
    _config, store, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_code"] = block_code
    state["block_reason"] = "original acquisition timeout"
    state["block_retryable"] = False
    hop["prompt"] = None
    hop["prompt_sha256"] = None
    hop["preboundary_recovery"] = {
        "phase": "recovery_required", "stage": "acquisition",
        "request_id": hop["request_id"], "attempts": 0,
    }
    if block_code == "resume_progress_unverified":
        state["controls"] = [{"control_id": 1, "action": "resume", "status": "applied", "result": {"outcome": "recovery_required"}}]
    role = state["roles"][state["active_role"]]
    role["page_id"] = None
    role["page_url"] = None
    role["conversation_generation"] = 0
    path = Path(state["manifest_path"])
    store.save(path, state)
    return store, worker, path


@pytest.mark.parametrize("cohort_index", range(10))
def test_ten_pristine_unbound_shapes_are_candidates_not_send_permission(tmp_path, cohort_index):
    code = "resume_progress_unverified" if cohort_index in (1, 2) else "preboundary_acquisition_recovery_required"
    store, worker, path = blocked_fixture(tmp_path, block_code=code)
    original = path.read_bytes()
    state = store.load(path)
    verdict = worker._classify_presend_acquisition(state)
    assert verdict["classification"] == "pristine_candidate"
    assert verdict["send_authorized"] is False
    assert path.read_bytes() == original
    assert not Path(_active_hop(state)["ledger_path"]).exists()


@pytest.mark.parametrize("ledger", [
    {"version": 1, "records": {"unrelated": {"status": "sent"}}},
    {"version": 1, "records": {"different-hop": {"status": "new"}}},
    {"version": 1, "records": "corrupt"},
    {"version": 2, "records": {}},
    {"version": 1, "records": {"request-a": {"status": "sending", "attempts": 1}}},
])
def test_ledger_unknown_variants_fail_closed_read_only(tmp_path, ledger):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    ledger_path = Path(_active_hop(state)["ledger_path"])
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    before = ledger_path.read_bytes()
    verdict = worker._classify_presend_acquisition(state)
    assert verdict["classification"] == "safe_hold"
    assert verdict["send_authorized"] is False
    assert ledger_path.read_bytes() == before


def test_corrupt_ledger_fails_closed(tmp_path):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    ledger_path = Path(_active_hop(state)["ledger_path"])
    ledger_path.write_text("{broken", encoding="utf-8")
    assert worker._classify_presend_acquisition(state)["classification"] == "safe_hold"


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(status="RUNNING"),
    lambda s: s.update(active_role="DEV"),
    lambda s: s["hops"][0].update(state="sending"),
    lambda s: s["hops"][0].update(receipt={"user_message_id": "accepted"}),
    lambda s: s["hops"][0]["preboundary_recovery"].update(request_id="other"),
    lambda s: s["roles"][s["active_role"]].update(page_id="unknown"),
    lambda s: s["hops"][0].setdefault("timestamps", {}).update(sending_at="2026-01-01T00:00:00Z"),
    lambda s: s["hops"].append(copy.deepcopy(s["hops"][0])),
])
def test_stale_and_send_ambiguous_manifest_fails_closed(tmp_path, mutation):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    mutation(state)
    assert worker._classify_presend_acquisition(state)["classification"] == "safe_hold"


def test_control_is_locked_one_shot_safe_hold_no_browser_mutation(tmp_path):
    store, worker, path = blocked_fixture(tmp_path)
    initial = store.load(path)
    prior_request = _active_hop(initial)["request_id"]
    state = store.request_control(
        path, "reconcile_presend_acquisition", role="PLAN",
        reason="historical exact request audit", external_command_id="fixture-presend-1",
    )
    assert state["controls"][-1]["status"] == "requested"

    class NoBrowserActions(FakeActions):
        def __getattribute__(self, name):
            if name in {"acquire", "fresh_chat", "new_chat", "reopen", "send"}:
                raise AssertionError(f"browser mutation must not be reached: {name}")
            return super().__getattribute__(name)

    assert asyncio.run(worker._apply_control(state, NoBrowserActions(), path))
    persisted = store.load(path)
    control = persisted["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "safe_hold"
    assert control["result"]["send_authorized"] is False
    assert persisted["status"] == "BLOCKED"
    assert _active_hop(persisted)["request_id"] == prior_request
    assert _active_hop(persisted)["preboundary_recovery"]["phase"] == "recovery_required"
    assert persisted["roles"]["PLAN"]["page_id"] is None
    reloaded = store.load(path)
    assert asyncio.run(worker._apply_control(reloaded, NoBrowserActions(), path)) is False
    assert store.load(path)["controls"][-1] == control
    repeated = store.request_control(path, "reconcile_presend_acquisition", role="PLAN",
        reason="historical exact request audit", external_command_id="fixture-presend-1")
    assert len(repeated["controls"]) == 1




@pytest.mark.parametrize("status", [RequestStatus.NEW, RequestStatus.PROMPT_SET])
def test_read_only_exact_pristine_ledger_record_is_only_candidate(tmp_path, status):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    hop = _active_hop(state)
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(
        role=state["roles"]["PLAN"]["physical_role"],
        prompt="first unsent prompt",
        request_id=hop["request_id"],
    )
    if status != RequestStatus.NEW:
        ledger.update(hop["request_id"], status=status)
    before = Path(hop["ledger_path"]).read_bytes()
    assert worker._classify_presend_acquisition(state)["classification"] == "pristine_candidate"
    assert Path(hop["ledger_path"]).read_bytes() == before


@pytest.mark.parametrize("status,attempts", [
    (RequestStatus.NEW, 1),
    (RequestStatus.PROMPT_SET, 1),
    (RequestStatus.SENDING, 1),
    (RequestStatus.SENT, 1),
    (RequestStatus.COMPLETED, 1),
])
def test_recorded_attempt_or_send_state_is_not_pristine(tmp_path, status, attempts):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    hop = _active_hop(state)
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(
        role=state["roles"]["PLAN"]["physical_role"],
        prompt="first unsent prompt",
        request_id=hop["request_id"],
    )
    raw_path = Path(hop["ledger_path"])
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["records"][hop["request_id"]]["status"] = status.value
    raw["records"][hop["request_id"]]["attempts"] = attempts
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    assert worker._classify_presend_acquisition(state)["classification"] == "safe_hold"


@pytest.mark.parametrize("tab_kind", ["anonymous", "exact_owner", "foreign_only", "broken_cdp", "two_possible"])
def test_read_only_orphan_inventory_never_authorizes_send(tmp_path, monkeypatch, tab_kind):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.load(path)
    metadata = {
        "page_id": "owned-1", "role": state["roles"]["PLAN"]["physical_role"],
        "task_id": state["task_id"], "team": state["team"],
    }
    if tab_kind == "anonymous":
        metadata.update(page_id=None, role=None, task_id=None, team=None)
    if tab_kind == "foreign_only":
        metadata.update(role="other", task_id="other", team="other")
    from playwright_auto import cdpa_browser_projection

    class Page:
        url = "https://chatgpt.com/"
        def is_closed(self):
            return False

    async def metadata_probe(_page):
        if tab_kind == "broken_cdp":
            raise ConnectionError("disconnected")
        return metadata

    monkeypatch.setattr(cdpa_browser_projection, "inspect_page_metadata", metadata_probe)
    pages = [Page(), Page()] if tab_kind == "two_possible" else [Page()]
    actions = SimpleNamespace(browser_context=SimpleNamespace(pages=pages))
    result = asyncio.run(worker._inspect_presend_acquisition_orphans(actions, state, "PLAN"))
    expected = {
        "anonymous": ("possible_orphan_or_anonymous_tab", 1),
        "exact_owner": ("possible_orphan_or_anonymous_tab", 1),
        "foreign_only": ("historical_external_send_absence_unproven", 0),
        "broken_cdp": ("browser_inventory_incomplete", None),
        "two_possible": ("possible_orphan_or_anonymous_tab", 2),
    }[tab_kind]
    assert (result["reason_code"], result["possible_orphans"]) == expected
    assert store.load(path)["status"] == "BLOCKED"


def test_another_control_cannot_reauthorize_after_committed_safe_hold(tmp_path):
    store, worker, path = blocked_fixture(tmp_path)
    state = store.request_control(path, "reconcile_presend_acquisition", role="PLAN", reason="first", external_command_id="c1")
    assert asyncio.run(worker._apply_control(state, FakeActions(), path))
    with pytest.raises(ValueError, match="one-shot"):
        store.request_control(path, "reconcile_presend_acquisition", role="PLAN", reason="second", external_command_id="c2")
    assert len(store.load(path)["controls"]) == 1


def test_stale_request_identity_rejects_control_without_rebind(tmp_path):
    store, worker, path = blocked_fixture(tmp_path)
    queued = store.request_control(path, "reconcile_presend_acquisition", role="PLAN", reason="first")
    stale = copy.deepcopy(queued)
    stale["controls"][-1]["acquisition_identity"]["request_id"] = "unknown"
    assert asyncio.run(worker._apply_control(stale, FakeActions(), path))
    current = store.load(path)
    assert current["status"] == "BLOCKED"
    assert _active_hop(current)["request_id"] != "unknown"
    assert current["controls"][-1]["status"] in {"rejected", "ineffective"}



def test_simultaneous_controls_serialize_single_one_shot_admission(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, worker, path = blocked_fixture(tmp_path)
    def submit(index):
        try:
            return store.request_control(
                path, "reconcile_presend_acquisition", role="PLAN",
                reason=f"concurrent-{index}", external_command_id=f"control-{index}"
            )["controls"][-1]["control_id"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(submit, (1, 2)))
    assert sorted(value for value in admitted if value is not None) == [1]
    assert len(store.load(path)["controls"]) == 1


@pytest.mark.parametrize("field,new_value", [
    ("browser_endpoint_id", "unexpected-endpoint"),
    ("active_role", "DEV"),
    ("conversation_generation", 4),
])
def test_immutable_authorization_change_rejected(tmp_path, field, new_value):
    store, worker, path = blocked_fixture(tmp_path)
    queued = store.request_control(
        path, "reconcile_presend_acquisition", role="PLAN", reason="stale snapshot"
    )
    current = store.load(path)
    if field == "conversation_generation":
        current["roles"]["PLAN"][field] = new_value
    else:
        current[field] = new_value
    # Mutate the fake working state only: never alter original durable endpoint.
    assert asyncio.run(worker._apply_control(current, FakeActions(), path))
    assert store.load(path)["status"] == "BLOCKED"
    assert store.load(path)["controls"][-1]["status"] in {"rejected", "ineffective"}

def test_other_pre_send_block_code_not_eligible(tmp_path):
    store, _worker, path = blocked_fixture(tmp_path)
    current = store.load(path)
    current["block_code"] = "provider_challenge"
    store.save(path, current)
    with pytest.raises(ValueError, match="acquisition"):
        store.request_control(path, "reconcile_presend_acquisition", role="PLAN",
            reason="wrong cohort")
