"""Regression gates for G6 CDPA Solo recovery across cleanup and stale projections."""
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "workflows" / "self-repair" / "supervisor.py"
spec = importlib.util.spec_from_file_location("g6_cdpa_supervisor_recovery", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "PROGRESS", tmp_path / "progress.md")
    monkeypatch.setattr(mod, "HERE", tmp_path)
    config = mod.load(mod.CONFIG)
    state = mod.new_state(config)
    return config, state


def stopped_task(*, task_id="task08", team="team08"):
    reason = ("Pi recovery: blocked permission_review with accepted Send preserved; "
              "stop before CDPA runtime reload")
    board = {
        "team": team, "task_id": task_id, "status": "STOPPED",
        "active_action": "cleared", "stop_reason": reason,
        "cleanup": {"state": "CLEARED", "status_before": "STOPPED",
                    "verified_empty_at": "2026-10-27T13:14:00+00:00"},
        "browser_control": {"stop_visible": True, "retry_visible": False},
    }
    detail = {**board, "controls": [{"action": "stop", "status": "applied",
                                      "reason": reason, "command_state": "APPLIED"}]}
    return board, detail


def receipt_06(tmp_path):
    folder = tmp_path / "receipts"
    folder.mkdir(exist_ok=True)
    (folder / "06-async-wait.json").write_text(json.dumps({
        "slice_id": "06-async-wait", "task_id": "task08", "status": "BLOCKED",
        "tests": [{"command": "pytest tests/test_spec003_gate_c_runtime.py",
                   "result": "PASS: 46 passed"}],
        "evidence": ["Async registration proven offline; browser terminal unresolved"],
    }), encoding="utf-8")


def test_cleared_verified_solo_06_stop_can_advance_code_only(monkeypatch, tmp_path):
    config, state = configured(monkeypatch, tmp_path)
    receipt_06(tmp_path)
    state["next_index"] = 7
    state["active"] = {"slice_id": "06-async-wait", "team": "team08", "task_id": "task08"}
    board, detail = stopped_task()
    monkeypatch.setattr(mod, "api", lambda method, path: detail)
    mod.process_active(config, state, [board])
    assert state["phase"] == "IDLE"
    assert state["active"] is None
    assert state["next_index"] == 8
    assert state["release"] == "NOT_ACCEPTED"
    assert state["completed"][-1]["task_terminal_status"] == "STOPPED"
    assert state["completed"][-1]["receipt_status"] == "BLOCKED"


@pytest.mark.parametrize("fault", ["stop_not_applied", "no_cleanup", "wrong_reason", "wrong_task"])
def test_cleared_solo_06_fails_closed_without_exact_proof(monkeypatch, tmp_path, fault):
    config, state = configured(monkeypatch, tmp_path)
    receipt_06(tmp_path)
    state["next_index"] = 7
    state["active"] = {"slice_id": "06-async-wait", "team": "team08", "task_id": "task08"}
    board, detail = stopped_task()
    if fault == "stop_not_applied":
        detail["controls"][0]["status"] = "queued"
    elif fault == "no_cleanup":
        board["cleanup"] = {}
        detail["cleanup"] = {}
    elif fault == "wrong_reason":
        board["stop_reason"] = "unrelated operator stop"
    elif fault == "wrong_task":
        detail["task_id"] = "another-task"
    monkeypatch.setattr(mod, "api", lambda method, path: detail)
    mod.process_active(config, state, [board])
    assert state["phase"] == "NEEDS_PI"
    assert state["active"]["task_id"] == "task08"
    assert state["next_index"] == 7


def test_stopped_regression_team_gets_distinct_retry_not_old_send(monkeypatch, tmp_path):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 8
    team = "g6-selfrepair-09-07-regression"
    why = ("G6 Pi recovery: retained accepted-Send regression task is stale "
           "after CDPA reload; preserve ledger and do not replay")
    old = {
        "team": team, "task_id": "task09", "status": "STOPPED",
        "active_action": "cleared", "stop_reason": why,
        "cleanup": {"state": "CLEARED", "status_before": "STOPPED",
                    "verified_empty_at": "2026-10-27T13:19:05+00:00"},
    }
    detail = {**old, "controls": [{"action": "stop", "status": "applied",
                                    "reason": why, "command_state": "APPLIED"}]}
    submitted = []

    def api(method, path, payload=None, key=None):
        if method == "GET":
            return detail
        submitted.append((path, payload, key))
        return {"task_id": "task09-fresh", "command_id": "cmd-fresh"}

    monkeypatch.setattr(mod, "api", api)
    mod.start_slice(config, state, [old], config["slices"][8])
    assert state["active"]["team"] == team + "-retry1"
    assert state["active"]["task_id"] == "task09-fresh"
    assert submitted[0][1]["requested_team"] == team + "-retry1"
    assert submitted[0][2] == mod.key_for(config, 8, "create-retry1")
    assert state["archived_blocked"] == ["task09"]


def test_unverified_stopped_regression_team_is_not_reused(monkeypatch, tmp_path):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 8
    team = "g6-selfrepair-09-07-regression"
    old = {"team": team, "task_id": "old", "status": "STOPPED",
           "active_action": "cleared", "stop_reason": "unrelated operator Stop",
           "cleanup": {"state": "CLEARED"}}
    monkeypatch.setattr(mod, "api", lambda *args, **kwargs: {"controls": []})
    with pytest.raises(RuntimeError, match="verified|proof|stop"):
        mod.start_slice(config, state, [old], config["slices"][8])
    assert state["next_index"] == 8


def test_stopped_real_browser_with_applied_stop_retries_new_conversation(monkeypatch, tmp_path):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 9
    state["phase"] = "NEEDS_PI"
    team = "g6-selfrepair-10-08-real-browser"
    old_id = "task-real-old"
    state["active"] = {"slice_id": "08-real-browser", "team": team, "task_id": old_id}
    why = ("G6 Pi recovery: paused real-browser task reconciled; stop CDPA "
           "before reload, preserve accepted Send ledger")
    original = {
        "team": team, "task_id": old_id, "status": "STOPPED",
        "active_action": "cleared", "stop_reason": why,
        "cleanup": {"state": "CLEARED", "status_before": "STOPPED",
                    "verified_empty_at": "2026-10-27T14:38:15+00:00"},
    }
    detail = {**original, "controls": [
        {"action": "stop", "status": "applied", "reason": why}
    ]}
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "08-real-browser.json").write_text(json.dumps({
        "slice_id": "08-real-browser", "task_id": old_id, "status": "BLOCKED",
        "tests": [{"command": "pytest -q tests/test_spec003_gate_c_runtime.py",
                   "result": "27 passed"}],
        "evidence": ["One accepted prior Send, but independent live release proof pending"],
        "release": "NOT_ACCEPTED",
    }), encoding="utf-8")
    calls = []
    def api(method, path, payload=None, key=None):
        if method == "GET":
            assert path.endswith(old_id)
            return detail
        calls.append((path, payload, key))
        return {"task_id": "task-real-retry1", "command_id": "cmd-real-retry1"}

    monkeypatch.setattr(mod, "api", api)
    mod.process_active(config, state, [original])
    assert state["phase"] == "IDLE"
    assert state["active"] is None
    assert state["next_index"] == 9
    assert state["release"] == "NOT_ACCEPTED"
    assert old_id in state["archived_blocked"]
    mod.start_slice(config, state, [original], config["slices"][9])
    assert state["active"]["team"] == team + "-retry1"
    assert state["active"]["task_id"] == "task-real-retry1"
    assert calls[-1][1]["requested_team"] == team + "-retry1"
    assert calls[-1][2] == mod.key_for(config, 9, "create-retry1")


def test_stopped_real_browser_missing_applied_proof_fails_closed(monkeypatch, tmp_path):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 9
    state["active"] = {
        "slice_id": "08-real-browser", "team": "g6-selfrepair-10-08-real-browser",
        "task_id": "task-real-old",
    }
    original = {
        "team": state["active"]["team"], "task_id": "task-real-old",
        "status": "STOPPED", "active_action": "cleared", "stop_reason": "operator stop",
        "cleanup": {"state": "CLEARED", "status_before": "STOPPED",
                    "verified_empty_at": "2026-10-27T14:38:15+00:00"},
    }
    monkeypatch.setattr(mod, "api", lambda *args, **kwargs: {**original, "controls": []})
    mod.process_active(config, state, [original])
    assert state["phase"] == "NEEDS_PI"
    assert state["next_index"] == 9
    assert state["active"]["task_id"] == "task-real-old"


@pytest.mark.parametrize("fault", ["none", "wrong_task", "wrong_team", "paused", "bad_receipt"])
def test_stopped_real_browser_ready_requires_exact_terminal_ownership(monkeypatch, fault):
    candidate = {"slice_id": "08-real-browser", "team": "team8", "task_id": "old8"}
    receipt = {"slice_id": "08-real-browser", "task_id": "old8", "status": "BLOCKED"}
    task = {"task_id": "old8", "team": "team8", "status": "STOPPED"}
    if fault == "wrong_task":
        task["task_id"] = "different"
    elif fault == "wrong_team":
        task["team"] = "other-team"
    elif fault == "paused":
        task["status"] = "PAUSED"
    elif fault == "bad_receipt":
        receipt["status"] = "PASS"
    monkeypatch.setattr(mod, "api", lambda method, path: task)
    assert mod.stopped_real_browser_ready(candidate, receipt) is (fault == "none")


def test_real_browser_valid_role_pause_resumes_once_without_replaying_send(
    monkeypatch, tmp_path
):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 9
    state["active"] = {
        "slice_id": "08-real-browser",
        "team": "g6-selfrepair-10-08-real-browser",
        "task_id": "task-08",
    }
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "08-real-browser.json").write_text(json.dumps({
        "slice_id": "08-real-browser", "task_id": "task-08",
        "status": "BLOCKED", "release": "NOT_ACCEPTED",
        "evidence": ["Live acceptance needs further direct evidence"],
        "tests": [{"command": "pytest tests/test_spec003_gate_c_runtime.py",
                   "result": "PASS: 27 passed"}],
    }))
    paused = {
        "task_id": "task-08", "team": state["active"]["team"],
        "status": "PAUSED", "active_action": "paused",
    }
    detail = {
        **paused, "active_hop": {"hop_id": 1, "state": "routed"},
        "control_eligibility": {"resume": {"eligible": True}},
    }
    submitted = []

    def fake_api(method, path, payload=None, key=None):
        if method == "GET":
            assert path.endswith("task-08")
            return detail
        submitted.append((path, payload, key))
        return {"command_id": "cmd-resume-08", "status": "queued"}

    monkeypatch.setattr(mod, "api", fake_api)
    mod.process_active(config, state, [paused])
    assert state["phase"] == "ACTIVE"
    assert state["active"]["auto_resume_count"] == 1
    assert state["active"]["resume_command_id"] == "cmd-resume-08"
    assert submitted[0][0] == "/api/tasks/resume"
    assert submitted[0][1] == {"team": state["active"]["team"]}
    # The next scheduled tick cannot issue a second Resume for the same pause.
    mod.process_active(config, state, [paused])
    assert len(submitted) == 1


@pytest.mark.parametrize("fault", ["missing_receipt", "wrong_owner", "manual_pause", "ineligible"])
def test_real_browser_pause_without_exact_safe_evidence_never_auto_resumes(
    monkeypatch, tmp_path, fault
):
    config, state = configured(monkeypatch, tmp_path)
    state["next_index"] = 9
    state["active"] = {"slice_id": "08-real-browser", "team": "team08", "task_id": "task08"}
    (tmp_path / "receipts").mkdir()
    if fault != "missing_receipt":
        (tmp_path / "receipts" / "08-real-browser.json").write_text(json.dumps({
            "slice_id": "08-real-browser",
            "task_id": "wrong" if fault == "wrong_owner" else "task08",
            "status": "BLOCKED", "release": "NOT_ACCEPTED",
            "evidence": ["Needs independent real-CDP proof"],
            "tests": [{"command": "pytest -q tests/test_spec003_gate_c_runtime.py",
                       "result": "27 passed"}],
        }))
    paused = {"team": "team08", "task_id": "task08",
              "status": "PAUSED", "active_action": "paused"}
    detail = {**paused, "active_hop": {"hop_id": 1, "state": (
        "waiting" if fault == "manual_pause" else "routed")},
        "control_eligibility": {"resume": {"eligible": fault != "ineligible"}}}
    actions = []
    def fake_api(method, path, payload=None, key=None):
        if method == "POST":
            actions.append(payload)
            raise AssertionError("unsafe auto-resume attempted")
        return detail
    monkeypatch.setattr(mod, "api", fake_api)
    mod.process_active(config, state, [paused])
    assert not actions
    assert state["phase"] == "NEEDS_PI"



