"""Failing-first regressions for G6 Pi bootstrap and Windows checkpoint recovery."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "workflows" / "self-evolution"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("bootstrap_recovery_runner", HERE / "run_pi.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="G6 Windows runner")


def _isolated_state(tmp_path, monkeypatch, **values):
    monkeypatch.setattr(runner, "STATE", tmp_path / "pi-runner.json")
    monkeypatch.setattr(runner, "STATE_LOCK", tmp_path / "pi-state.lock")
    runner.STATE.write_text(json.dumps(values), encoding="utf-8")


def _stale_checkpoint():
    return {"status": "RUNNING", "owner_pid": 32200, "pi_pid": 35124,
            "iteration": 344, "dispatch_allowed": False}


def test_stale_dead_pids_reconcile_verified_empty_authoritative_ledger(monkeypatch):
    monkeypatch.setattr(runner, "process_creation_id", lambda pid: None, raising=False)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        "cdpa-idem-old": {"status": "DONE", "team": "prior", "parent_task_id": None}})
    queried = []
    monkeypatch.setattr(runner, "command_by_key", lambda key: queried.append(key) or None)
    status, reason = runner.reconcile_stale_running(_stale_checkpoint())
    assert status == "OBSERVE", reason
    assert "dead" in reason.lower()
    assert len(queried) == 1


def test_stale_checkpoint_remains_held_for_unknown_or_active_ledger(monkeypatch):
    monkeypatch.setattr(runner, "process_creation_id", lambda pid: None, raising=False)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: None)
    assert runner.reconcile_stale_running(_stale_checkpoint())[0] == "SAFETY_HOLD"
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        "cdpa-idem-active": {"status": "RUNNING", "team": "active"}})
    assert runner.reconcile_stale_running(_stale_checkpoint())[0] == "SAFETY_HOLD"
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        "cdpa-idem-old": {"status": "DONE", "team": "old"}})
    monkeypatch.setattr(runner, "command_by_key", lambda key: {
        "kind": "create_task", "status": "queued", "parent_task_id": "cdpa-idem-old",
        "task_id": "cdpa-idem-child", "team": "new"})
    assert runner.reconcile_stale_running(_stale_checkpoint())[0] == "SAFETY_HOLD"


def test_applied_historical_child_with_terminal_identity_does_not_block(monkeypatch):
    import uuid

    monkeypatch.setattr(runner, "process_creation_id", lambda pid: None)
    parent = "cdpa-idem-parent"
    child = "cdpa-idem-terminal-child"
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        parent: {"status": "DONE", "team": "old"},
        child: {"status": "DONE", "team": "new", "parent_task_id": parent},
    })
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "cdpa:self-evolution:successor:" + parent))
    monkeypatch.setattr(runner, "command_by_key", lambda current: {
        "kind": "create_task", "status": "applied", "task_id": child,
        "parent_task_id": parent, "team": "new",
    } if current == key else None)
    assert runner.reconcile_stale_running(_stale_checkpoint())[0] == "OBSERVE"


@pytest.mark.parametrize("status", ["PAUSED", "BLOCKED"])
def test_paused_or_blocked_task_without_command_is_not_safe_to_replace(monkeypatch, status):
    monkeypatch.setattr(runner, "process_creation_id", lambda pid: None)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        "cdpa-idem-active": {"status": status, "team": "owned"}})
    monkeypatch.setattr(runner, "command_by_key", lambda key: None)
    assert runner.reconcile_stale_running(_stale_checkpoint())[0] == "SAFETY_HOLD"


def test_pid_inspection_failure_is_persisted_as_hold_without_dispatch(tmp_path, monkeypatch):
    from contextlib import nullcontext

    _isolated_state(tmp_path, monkeypatch, **_stale_checkpoint())
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner, "LOG", tmp_path / "pi-runner.log")
    monkeypatch.setattr(runner, "single_owner", lambda: nullcontext())
    monkeypatch.setattr(runner, "contain_current_process_tree", lambda: None)
    monkeypatch.setattr(runner, "previous_pi_still_running", lambda: False)
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "DESKTOP-B5E4IUE")
    monkeypatch.setattr(runner, "health", lambda: {"ready": True})
    monkeypatch.setattr(runner, "process_creation_id",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("WinError 5")))
    monkeypatch.setattr(runner, "run_cycle",
                        lambda iteration: pytest.fail("must not dispatch on PID error"))

    class StopTest(Exception):
        pass

    monkeypatch.setattr(runner.time, "sleep", lambda *_: (_ for _ in ()).throw(StopTest))
    with pytest.raises(StopTest):
        runner.run()
    state = json.loads(runner.STATE.read_text(encoding="utf-8"))
    assert state["status"] == "SAFETY_HOLD"
    assert "WinError 5" in state["reason"]
    assert state["pi_pid"] == 35124
    assert state["dispatch_allowed"] is False


def test_stale_checkpoint_with_live_owned_process_remains_held(monkeypatch):
    monkeypatch.setattr(runner, "process_creation_id",
                        lambda pid: 777 if pid == 32200 else None, raising=False)
    checkpoint = {**_stale_checkpoint(), "owner_creation_id": 777}
    assert runner.reconcile_stale_running(checkpoint)[0] == "SAFETY_HOLD"
    monkeypatch.setattr(runner, "process_creation_id",
                        lambda pid: 888 if pid == 35124 else None, raising=False)
    checkpoint = {**_stale_checkpoint(), "pi_creation_id": 888}
    assert runner.reconcile_stale_running(checkpoint)[0] == "SAFETY_HOLD"


def test_reused_pid_is_not_mistaken_for_the_original_pi_child(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _isolated_state(tmp_path, monkeypatch, status="RUNNING",
                        owner_pid=123, pi_pid=child.pid, pi_creation_id=0)
        assert runner.previous_pi_still_running() is False
        assert runner.process_creation_id(child.pid) is not None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_atomic_checkpoint_retries_windows_share_violation(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch, status="OLD", iteration=1)
    original_replace = os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append(Path(src))
        if len(attempts) <= 2:
            raise PermissionError(13, "Access is denied", str(dst))
        return original_replace(src, dst)

    monkeypatch.setattr(runner.os, "replace", flaky_replace)
    runner.write_state(status="RECOVERED", iteration=2)
    assert json.loads(runner.STATE.read_text()) == {"status": "RECOVERED", "iteration": 2}
    assert len(attempts) == 3
    assert not list(tmp_path.glob("pi-runner.json.*.tmp"))


def test_atomic_checkpoint_permanent_denial_keeps_prior_file_and_owner_lock(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch, status="OLD", iteration=1)
    monkeypatch.setattr(runner.os, "replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError(13, "Access is denied")))
    owner_lock = tmp_path / "pi-runner.lock"
    with runner.single_owner(owner_lock):
        with pytest.raises(PermissionError):
            runner.write_state(status="NEW", iteration=2)
        with pytest.raises(RuntimeError, match="already active"):
            with runner.single_owner(owner_lock):
                pass
    assert json.loads(runner.STATE.read_text()) == {"status": "OLD", "iteration": 1}
    assert not list(tmp_path.glob("pi-runner.json.*.tmp"))


def test_checkpoint_records_real_process_creation_identity(tmp_path, monkeypatch):
    _isolated_state(tmp_path, monkeypatch, status="OLD")
    runner.write_state(status="RUNNING", owner_pid=os.getpid(), pi_pid=os.getpid())
    state = json.loads(runner.STATE.read_text())
    assert state["owner_creation_id"] == runner.process_creation_id(os.getpid())
    assert state["pi_creation_id"] == state["owner_creation_id"]
