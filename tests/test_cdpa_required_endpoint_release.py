from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_core import write_config


ACTION = "release_required_browser_endpoint"


def _setup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    preferred = store.endpoints.resolve()
    task = store.create_task(
        "release endpoint pin",
        requested_team="release-pin",
        governing_objective="Release endpoint test objective.",
        governing_directives=[],
        browser_endpoint_id=preferred["endpoint_id"],
        required_browser_endpoint_id=preferred["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    return worker, store, preferred, task


def test_release_required_browser_endpoint_is_admitted_and_audited(tmp_path: Path):
    _worker, store, preferred, task = _setup(tmp_path)

    eligibility = workflow_control_eligibility(task, ACTION)
    assert eligibility["eligible"] is True

    queued = store.request_control(task["manifest_path"], ACTION, reason="operator release")
    control = queued["controls"][-1]
    assert control["action"] == ACTION
    assert control["command"]["action"] == ACTION
    assert control["command"]["snapshot"]["required_browser_endpoint_id"] == preferred["endpoint_id"]


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (lambda state: state.update(status="DONE"), "terminal"),
        (lambda state: state.pop("required_browser_endpoint_id"), "hard"),
        (lambda state: state.update(active_hop_id=None), "active"),
        (lambda state: _active_hop(state).update(state="sending"), "pre_send"),
    ],
)
def test_release_required_browser_endpoint_static_admission_fails_closed(
    tmp_path: Path, mutate, reason_fragment: str
):
    _worker, _store, _preferred, task = _setup(tmp_path)
    state = copy.deepcopy(task)
    mutate(state)

    eligibility = workflow_control_eligibility(state, ACTION)

    assert eligibility["eligible"] is False
    assert reason_fragment in str(eligibility["reason"]).lower()


def test_release_required_browser_endpoint_is_workflow_only(tmp_path: Path):
    _worker, store, _preferred, _task = _setup(tmp_path)
    agent = store.create_independent_agent(
        "independent",
        system_prompt="Inspect only.",
    )

    with pytest.raises(ValueError, match="only for workflow tasks"):
        store.request_control(agent["manifest_path"], ACTION)


def test_generic_taskstore_update_cannot_release_required_browser_endpoint(tmp_path: Path):
    _worker, store, preferred, task = _setup(tmp_path)

    def remove_pin(state):
        state.pop("required_browser_endpoint_id", None)
        return state

    with pytest.raises(ValueError, match="required_browser_endpoint_id is immutable"):
        store.update(task["manifest_path"], remove_pin)

    assert store.load(task["manifest_path"])["required_browser_endpoint_id"] == preferred["endpoint_id"]


def test_release_required_browser_endpoint_removes_only_hard_pin(tmp_path: Path):
    worker, store, preferred, task = _setup(tmp_path)
    queued = store.request_control(task["manifest_path"], ACTION, reason="operator release")

    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(task["manifest_path"]),
        )
    ) is True

    saved = store.load(task["manifest_path"])
    assert saved["browser_endpoint_id"] == preferred["endpoint_id"]
    assert "required_browser_endpoint_id" not in saved
    control = saved["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "required_browser_endpoint_released"
    assert control["result"]["before"]["required_browser_endpoint_id"] == preferred["endpoint_id"]
    assert control["result"]["after"]["required_browser_endpoint_id"] is None


def test_release_required_browser_endpoint_allows_pristine_new_ledger_record(tmp_path: Path):
    worker, store, preferred, task = _setup(tmp_path)
    state = copy.deepcopy(task)
    hop = _active_hop(state)
    RequestLedger(hop["ledger_path"]).begin(
        role=hop["physical_role"],
        prompt="prepared but unsent",
        request_id=hop["request_id"],
        render_request_marker=False,
    )

    def persist(current):
        current.clear()
        current.update(state)
        return current

    persisted = store.update(task["manifest_path"], persist)
    queued = store.request_control(persisted["manifest_path"], ACTION)

    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(task["manifest_path"]),
        )
    ) is True

    saved = store.load(task["manifest_path"])
    assert saved["browser_endpoint_id"] == preferred["endpoint_id"]
    assert "required_browser_endpoint_id" not in saved
    assert saved["controls"][-1]["status"] == "applied"


def test_release_required_browser_endpoint_rechecks_locked_current_send_boundary(
    tmp_path: Path, monkeypatch
):
    worker, store, preferred, task = _setup(tmp_path)
    queued = store.request_control(task["manifest_path"], ACTION, reason="operator release")
    original_update_for_control = store.update_for_control
    raced = False

    def race_before_locked_persist(path, action, mutator):
        nonlocal raced
        if not raced and action == ACTION:
            raced = True

            def cross_send_boundary(current):
                hop = _active_hop(current)
                hop["state"] = "sending"
                hop["receipt"] = {"accepted_via": "race"}
                return current

            store.update(path, cross_send_boundary)
        return original_update_for_control(path, action, mutator)

    monkeypatch.setattr(store, "update_for_control", race_before_locked_persist)

    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(task["manifest_path"]),
        )
    ) is True

    saved = store.load(task["manifest_path"])
    assert saved["required_browser_endpoint_id"] == preferred["endpoint_id"]
    hop = _active_hop(saved)
    assert hop["state"] == "sending"
    assert hop["receipt"] == {"accepted_via": "race"}
    control = saved["controls"][-1]
    assert control["status"] == "rejected"
    assert control["command_state"] == "REJECTED"


@pytest.mark.parametrize(
    "evidence",
    ["receipt", "message_identity", "upload", "ledger_attempt", "ledger_corrupt"],
)
def test_release_required_browser_endpoint_rejects_irreversible_or_ambiguous_evidence(
    tmp_path: Path, evidence: str
):
    worker, store, preferred, task = _setup(tmp_path)
    state = copy.deepcopy(task)
    hop = _active_hop(state)
    role = state["roles"][state["active_role"]]

    if evidence == "receipt":
        hop["receipt"] = {"accepted_via": "test"}
    elif evidence == "message_identity":
        hop["message_identity"] = {"user_message_id": "accepted"}
    elif evidence == "upload":
        role["attachments_uploaded_generation"] = role["conversation_generation"]
    elif evidence == "ledger_attempt":
        ledger = RequestLedger(hop["ledger_path"])
        ledger.begin(
            role=hop["physical_role"],
            prompt="already durable",
            request_id=hop["request_id"],
            render_request_marker=False,
        )
        ledger.update(hop["request_id"], attempts=1)
    else:
        Path(hop["ledger_path"]).write_text("{not-json", encoding="utf-8")

    def persist(current):
        current.clear()
        current.update(state)
        return current

    persisted = store.update(task["manifest_path"], persist)
    queued = store.request_control(persisted["manifest_path"], ACTION)

    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(task["manifest_path"]),
        )
    ) is True

    saved = store.load(task["manifest_path"])
    assert saved["required_browser_endpoint_id"] == preferred["endpoint_id"]
    assert saved["controls"][-1]["status"] == "rejected"
