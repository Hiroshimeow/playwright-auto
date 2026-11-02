"""Root-cause regressions: an unavailable Pi must not starve G6 CDPA reload or
prevent reconciled terminal Stop from advancing the existing campaign."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FILE = Path(__file__).resolve().parents[1] / "workflows" / "self-repair" / "supervisor.py"
spec = importlib.util.spec_from_file_location("g6_cdpa_selfrepair_watchdog", FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def env(monkeypatch, tmp_path):
    config = mod.load(mod.CONFIG)
    monkeypatch.setattr(mod, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "PROGRESS", tmp_path / "progress.md")
    monkeypatch.setattr(mod, "LOCK", tmp_path / "supervisor.lock")
    monkeypatch.setattr(mod, "HERE", tmp_path)
    monkeypatch.setattr(mod, "CONFIG", tmp_path / "slices.json")
    (tmp_path / "slices.json").write_text(json.dumps(config), encoding="utf-8")
    state = mod.new_state(config)
    return config, state


def stopped_real_browser():
    reason = ("G6 Pi recovery: paused real-browser task reconciled; "
              "stop CDPA before reload, preserve accepted Send ledger")
    board = dict(team="team08", task_id="task08", status="STOPPED",
                 active_action="cleared", stop_reason=reason,
                 cleanup=dict(state="CLEARED", status_before="STOPPED",
                              verified_empty_at="2026-10-27T14:38:15+00:00"))
    detail = dict(**board, controls=[dict(action="stop", status="applied", reason=reason)])
    return board, detail


def test_pi_timeout_cannot_hold_supervisor_across_scheduled_watchdog_tick(monkeypatch, tmp_path):
    config, state = env(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr(mod.Path, "is_file", lambda self: True)
    def run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="PI_RECOVERY_DONE", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", run)
    success, output = mod.pi_command(config, "recovery", state)
    assert success
    # This scheduler runs every minute. A 720-second Pi blocks all watchdog
    # ticks and source-drift reloads under the supervisor's exclusive lock.
    assert 0 < captured["timeout"] <= 25
    assert captured["stdin"] is mod.subprocess.DEVNULL


def test_stopped_real_browser_reconciles_from_recovery_review_without_resume(monkeypatch, tmp_path):
    config, state = env(monkeypatch, tmp_path)
    state["phase"] = "RECOVERY_REVIEW"
    state["next_index"] = 9
    state["active"] = {"slice_id": "08-real-browser", "team": "team08", "task_id": "task08"}
    folder = tmp_path / "receipts"
    folder.mkdir()
    (folder / "08-real-browser.json").write_text(json.dumps({
        "slice_id": "08-real-browser", "task_id": "task08",
        "status": "BLOCKED", "release": "NOT_ACCEPTED",
        "tests": [{"command": "pytest tests/test_spec003_gate_c_runtime.py", "result": "27 passed"}],
        "evidence": ["One accepted Send, browser verdict remains unverified"],
    }), encoding="utf-8")
    board, detail = stopped_real_browser()
    def api(method, url, payload=None, key=None):
        if url == "/api/tasks":
            return {"items": [board]}
        if url == "/api/tasks/task08":
            return detail
        if url == "/health":
            return {"ok": True, "worker_online": True, "worker_ready": True,
                    "worker_runtime_ready": True, "worker_command_loop_ready": True,
                    "worker_source_drift": {"drifted": False}}
        raise AssertionError("unexpected call " + url)
    monkeypatch.setattr(mod, "api", api)
    monkeypatch.setattr(mod, "check_environment", lambda *a, **k: {"worker_source_drift":{"drifted":False}})
    monkeypatch.setattr(mod, "resume_after_recovery", lambda *a: pytest.fail("Never resume stopped accepted Send"))
    monkeypatch.setattr(mod, "pi_command", lambda *a: pytest.fail("No Pi needed for proven Stop"))
    mod.save(state)
    mod.supervise_once()
    after = mod.load(mod.STATE)
    assert after["phase"] == "IDLE"
    assert after["next_index"] == 9
    assert after["active"] is None
    assert after["release"] == "NOT_ACCEPTED"
    assert "task08" in after["archived_blocked"]


def test_recovery_review_reloads_unhealthy_cdpa_without_running_pi(monkeypatch,tmp_path):
    config, state = env(monkeypatch, tmp_path)
    state["phase"] = "RECOVERY_REVIEW"
    state["next_index"] = 9
    state["active"] = {"slice_id": "08-real-browser", "team": "team08", "task_id": "task08"}
    monkeypatch.setattr(mod, "api", lambda method,url,*args: {"ok":False, "worker_online":False}
                        if url == "/health" else {"status":"RUNNING","team":"team08","task_id":"task08"})
    monkeypatch.setattr(mod, "check_environment", lambda *a,**k: pytest.fail("Health failure must reload first"))
    monkeypatch.setattr(mod, "pi_command", lambda *a: pytest.fail("Pi cannot block reload"))
    reloads = []
    monkeypatch.setattr(mod, "reload_g6_runtime", lambda state: reloads.append(state["active"]["task_id"]))
    mod.save(state)
    mod.supervise_once()
    assert reloads == ["task08"]


def test_parallel_cdpa_reload_lock_is_pending_not_recovery_failure(monkeypatch,tmp_path):
    config,state=env(monkeypatch,tmp_path)
    expected_path=tmp_path/"reload_cdpa.ps1"
    expected_path.write_text("stub",encoding="utf-8")
    # The other reload process owns reload.lock. Never launch a second reload,
    # kill Chrome or exhaust Pi recovery; next tick rechecks runtime health.
    monkeypatch.setattr(mod.subprocess,"run",lambda *a,**k:
                        SimpleNamespace(returncode=2,stdout="RELOAD_ALREADY_IN_PROGRESS\n",stderr=""))
    mod.reload_g6_runtime(state)
    assert state["reason"]=="CDPA reload already in progress; verify health next tick"


def test_parallel_cdpa_reload_failure_is_not_hidden(monkeypatch,tmp_path):
    config,state=env(monkeypatch,tmp_path)
    (tmp_path/"reload_cdpa.ps1").write_text("stub",encoding="utf-8")
    monkeypatch.setattr(mod.subprocess,"run",lambda *a,**k:
                        SimpleNamespace(returncode=1,stdout="RELOAD_FAILED",stderr="reload failed"))
    with pytest.raises(RuntimeError,match="reload failed"):
        mod.reload_g6_runtime(state)


def test_control_race_stopped_real_browser_is_reconciled_only_with_cleared_tab(monkeypatch,tmp_path):
    config,state=env(monkeypatch,tmp_path)
    state["phase"]="NEEDS_PI"
    state["next_index"]=9
    state["active"]={"slice_id":"08-real-browser",
                     "team":"g6-selfrepair-10-08-real-browser-retry1",
                     "task_id":"old-task"}
    receipt_dir=tmp_path/"receipts"
    receipt_dir.mkdir()
    (receipt_dir/"08-real-browser.json").write_text(json.dumps({
        "slice_id":"08-real-browser","task_id":"old-task","status":"BLOCKED",
        "release":"NOT_ACCEPTED",
        "tests":[{"command":"pytest tests/test_chatgpt_model_gate.py","result":"10 passed"}],
        "evidence":["Accepted Send is completed; control race required Stop and cleanup"],
    }),encoding="utf-8")
    why=("G6 R11 rootfix: BLOCKED control 1 changed during Resume; stop affected "
         "CDPA task, preserve completed accepted Send; reload runtime without browser")
    stopped={"task_id":"old-task","team":state["active"]["team"],
             "status":"STOPPED","active_action":"cleared","stop_reason":why,
             "cleanup":{"state":"CLEARED","status_before":"STOPPED",
                        "verified_empty_at":"2026-10-27T15:07:45+00:00"},
             "browser_control":{"stop_visible":False,"retry_visible":False}}
    detail={**stopped,"controls":[{"action":"stop","status":"applied",
                                 "command_state":"APPLIED","reason":why}]}
    monkeypatch.setattr(mod,"api",lambda method,path:detail)
    mod.process_active(config,state,[stopped])
    assert state["phase"]=="IDLE"
    assert state["active"] is None
    assert state["next_index"]==9
    assert "old-task" in state["archived_blocked"]
    assert state["release"]=="NOT_ACCEPTED"


def test_control_race_stop_without_cleanup_remains_fenced(monkeypatch,tmp_path):
    config,state=env(monkeypatch,tmp_path)
    state["next_index"]=9
    state["active"]={"slice_id":"08-real-browser","team":"t","task_id":"old"}
    reason=("G6 R11 rootfix: BLOCKED control 1 changed during Resume; stop affected "
            "CDPA task, preserve completed accepted Send; reload runtime without browser")
    stopped={"task_id":"old","team":"t","status":"STOPPED",
             "active_action":"stopped","stop_reason":reason,
             "browser_control":{"stop_visible":False,"retry_visible":False},
             "cleanup":{"state":"ACTIVE"}}
    monkeypatch.setattr(mod,"api",lambda method,path:
                        {**stopped,"controls":[{"action":"stop","status":"applied",
                                               "reason":reason}]})
    mod.process_active(config,state,[stopped])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="old"
