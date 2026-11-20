"""Windows process-containment tests use only disposable subprocesses, never real Pi."""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "workflows/self-evolution"
spec = importlib.util.spec_from_file_location("self_evolution_runner", HERE / "run_pi.py")
sys.path.insert(0, str(HERE))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner")


def _wait_for_file(path, process, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if path.is_file():
            return path.read_text(encoding="utf-8")
        if process.poll() is not None:
            raise AssertionError(f"harness exited before ready: {process.returncode}")
        time.sleep(0.05)
    raise AssertionError("harness did not become ready")


def test_runner_parent_crash_kills_its_disposable_child(tmp_path):
    import ctypes

    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,time\n"
        f"sys.path.insert(0,{str(HERE)!r})\n"
        "from run_pi import contain_current_process_tree\n"
        "contain_current_process_tree()\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'])\n"
        f"open({str(pid_file)!r},'w').write(str(child.pid))\n"
        "time.sleep(90)\n"
    )
    parent = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        child_pid = int(_wait_for_file(pid_file, parent))
        child_handle = kernel.OpenProcess(0x00100000, 0, child_pid)
        assert child_handle, "disposable child did not start"
        assert kernel.WaitForSingleObject(child_handle, 0) == 0x102
        parent.kill()
        parent.wait(timeout=10)
        assert kernel.WaitForSingleObject(child_handle, 10000) == 0, (
            "orphan: parent exited but child still runs"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        if child_handle:
            # Clean only the test child if the regression still exists.
            if kernel.WaitForSingleObject(child_handle, 0) == 0x102:
                cleanup = kernel.OpenProcess(0x0001, 0, child_pid)
                if cleanup:
                    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    kernel.TerminateProcess(cleanup, 1)
                    kernel.CloseHandle(cleanup)
            kernel.CloseHandle(child_handle)


def test_file_lock_prevents_second_owner_and_releases_after_crash(tmp_path):
    lock = tmp_path / "owner.lock"
    ready = tmp_path / "owned.txt"
    code = (
        "import sys,time\n"
        f"sys.path.insert(0,{str(HERE)!r})\n"
        "from run_pi import single_owner\n"
        "from pathlib import Path\n"
        f"with single_owner(Path({str(lock)!r})):\n"
        f"    Path({str(ready)!r}).write_text('ready')\n"
        "    time.sleep(90)\n"
    )
    parent = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_for_file(ready, parent) == "ready"
        duplicate = subprocess.run(
            [sys.executable, "-c",
             f"import sys;sys.path.insert(0,{str(HERE)!r});"
             f"from run_pi import single_owner;"
             f"from pathlib import Path;"
             f"with_owner=single_owner(Path({str(lock)!r}));"
             f"with_owner.__enter__()"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        assert duplicate.returncode != 0
        assert "already active" in duplicate.stderr
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
    restored = subprocess.run(
        [sys.executable, "-c",
         f"import sys;sys.path.insert(0,{str(HERE)!r});"
         f"from run_pi import single_owner;from pathlib import Path;"
         f"with_owner=single_owner(Path({str(lock)!r}));"
         f"with_owner.__enter__();print('ACQUIRED')"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert restored.returncode == 0, restored.stderr
    assert "ACQUIRED" in restored.stdout


def test_exception_after_spawn_reaps_child_before_retry(tmp_path, monkeypatch):
    class DisposableChild:
        pid = 101
        returncode = None
        terminated = False
        waited = False
        def poll(self):
            return self.returncode
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            self.waited = True
            self.returncode = 0
            return 0
        def kill(self):
            self.returncode = -1

    child = DisposableChild()
    monkeypatch.setattr(runner, "LOG", tmp_path / "pi.log")
    monkeypatch.setattr(runner, "build_pi_command", lambda: ["fake-pi"])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(runner, "health", lambda: {"ready": True})

    def fail_state(**kwargs):
        raise OSError("simulated telemetry failure")
    monkeypatch.setattr(runner, "write_state", fail_state)
    with pytest.raises(OSError, match="telemetry failure"):
        runner.run_cycle(1)
    assert child.terminated and child.waited

def test_restart_refuses_surviving_legacy_child(tmp_path, monkeypatch):
    import json

    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state = tmp_path / "pi-runner.json"
    monkeypatch.setattr(runner, "STATE", state)
    try:
        state.write_text(json.dumps({"status": "RUNNING", "pi_pid": child.pid}))
        assert runner.previous_pi_still_running() is True
        with pytest.raises(RuntimeError, match="prior Pi child"):
            if runner.previous_pi_still_running():
                raise RuntimeError("SAFETY_HOLD: prior Pi child is still alive")
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    assert runner.previous_pi_still_running() is False


def test_completed_cycle_does_not_reconcile_stale_pid(tmp_path, monkeypatch):
    import json

    state = tmp_path / "pi-runner.json"
    monkeypatch.setattr(runner, "STATE", state)
    state.write_text(json.dumps({"status": "NEXT_CYCLE", "pi_pid": 1234}))
    assert runner.previous_pi_still_running() is False

def test_pi_cycle_reaps_descendant_after_direct_child_exit(tmp_path):
    import ctypes

    grandchild_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])\n"
        f"open({str(grandchild_file)!r},'w').write(str(p.pid))\n"
    )
    wrapper = subprocess.Popen(
        [sys.executable, str(HERE / "run_pi.py"), "--pi-child",
         sys.executable, "-c", code],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = None
    try:
        pid = int(_wait_for_file(grandchild_file, wrapper))
        # The wrapper may reap this process before OpenProcess can observe it.
        handle = kernel.OpenProcess(0x00100000, 0, pid)
        open_error = ctypes.get_last_error() if not handle else 0
        stdout, stderr = wrapper.communicate(timeout=15)
        assert wrapper.returncode == 0, stderr.decode(errors="replace")
        if handle:
            assert kernel.WaitForSingleObject(handle, 5000) == 0, (
                "Pi cycle exited but its grandchild survived"
            )
        else:
            assert open_error == 87  # process already exited
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=10)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 0x102:
                cleanup = kernel.OpenProcess(0x0001, 0, pid)
                if cleanup:
                    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    kernel.TerminateProcess(cleanup, 1)
                    kernel.CloseHandle(cleanup)
            kernel.CloseHandle(handle)


def test_pi_operator_must_act_or_record_exact_safety_hold(tmp_path, monkeypatch):
    """Regression for four healthy but read-only Pi cycles with free 9202."""
    fake_pi = tmp_path / "cli.js"
    fake_pi.write_text("// disposable Pi CLI marker", encoding="utf-8")
    monkeypatch.setattr(runner, "PI", fake_pi)
    command = runner.build_pi_command()
    prompt = command[-1]
    assert runner.canonical_goal() in prompt
    assert "dispatch exactly one" in prompt
    assert "spawn_cdpa.py --role" in prompt
    assert "historical PAUSED" in prompt
    assert "exact accepted/ambiguous Send" in prompt
    assert "safety hold" in prompt.lower()
    assert "not an operational checkpoint" in prompt
    assert "9202" in prompt and "9212" in prompt and "FJP" in prompt

def test_campaign_task_inventory_reads_existing_cdpa_api_only(monkeypatch):
    import io
    import json

    payload = {"items": [
        {"task_id": "cdpa-old", "task_title": "CDPA Self-Evolution DEV: old", "status": "DONE"},
        {"task_id": "cdpa-new", "task_title": "CDPA Self-Evolution TEST: new", "status": "RUNNING"},
        {"task_id": "unrelated", "task_title": "Not the campaign", "status": "RUNNING"},
    ]}

    class Response(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs:
                        Response(json.dumps(payload).encode("utf-8")))
    assert runner.campaign_tasks() == {
        "cdpa-old": {"status": "DONE", "team": None, "parent_task_id": None},
        "cdpa-new": {"status": "RUNNING", "team": None, "parent_task_id": None},
    }


def test_pi_cycle_ignores_checkpoint_and_unverified_dispatch_claim():
    before = {"cdpa-old": {"status": "DONE"}}
    assert runner.classify_cycle(before, before, "", 0)[0] == "NO_PROGRESS"
    assert runner.classify_cycle(before, before, "PI_ACTION: DISPATCH cdpa-new", 0)[0] == "NO_PROGRESS"
    assert runner.classify_cycle(before, before, "PI_ACTION: WAIT cdpa-old", 0)[0] == "NO_PROGRESS"


def test_pi_cycle_requires_exact_new_task_and_reconciles_failed_child():
    before = {"cdpa-old": {"status": "DONE"}}
    after = {**before, "cdpa-new": {"status": "RUNNING"}}
    assert runner.classify_cycle(before, after, "PI_ACTION: DISPATCH cdpa-new", 0)[0] == "SAFETY_HOLD"
    assert runner.classify_cycle(before, after, "PI_ACTION: DISPATCH cdpa-new", 1)[0] == "RECOVERY_PENDING"
    assert runner.classify_cycle(before, after, "PI_ACTION: DISPATCH cdpa-other", 0)[0] == "SAFETY_HOLD"
    assert runner.classify_cycle(before, {**after, "cdpa-third": {"status": "RUNNING"}}, "", 0)[0] == "SAFETY_HOLD"


def test_pi_cycle_does_not_count_unverified_hold_as_progress():
    before = {"cdpa-old": {"status": "DONE"}}
    assert runner.classify_cycle(before, before, "PI_ACTION: SAFETY_HOLD no evidence", 0)[0] == "NO_PROGRESS"
    assert runner.classify_cycle(before, before, "PI_ACTION: SAFETY_HOLD task cdpa-old blocked", 0)[0] == "NO_PROGRESS"
    assert runner.classify_cycle(None, before, "PI_ACTION: DISPATCH cdpa-new", 0)[0] == "SAFETY_HOLD"


def test_two_failed_cycles_are_included_in_next_pi_prompt(tmp_path, monkeypatch):
    import json

    state = tmp_path / "pi-runner.json"
    monkeypatch.setattr(runner, "STATE", state)
    state.write_text(json.dumps({"status": "NO_PROGRESS", "no_progress_cycles": 2,
                                 "reason": "No verified CDPA operation"}), encoding="utf-8")
    prompt = runner.coordinator_prompt()
    assert "2 consecutive NO_PROGRESS cycles" in prompt
    assert "No verified CDPA operation" in prompt
