"""Single-owner Pi operations runner. CDPA alone owns task and browser Send."""
from __future__ import annotations

import ctypes
import json
import msvcrt
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from core import canonical_goal, health

RUNTIME = ROOT / ".runtime" / "self-evolution"
LOCK = RUNTIME / "pi-runner.lock"
STATE = RUNTIME / "pi-runner.json"
LOG = RUNTIME / "pi-runner.log"
PI = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
_JOB_HANDLE = None


def write_state(**values):
    draft = STATE.with_suffix(".json.tmp")
    draft.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(draft, STATE)


def coordinator_prompt():
    return (
        canonical_goal()
        + "\n\n# LIVE OPERATOR MISSION - Pi Coding on G6\n"
        "You are the continuous OPERATOR, not the CDPA source editor. "
        "First read E:\\git-project\\cdpa\\workflows\\self-evolution\\GOAL_AND_RULES.md "
        "and AGENTS.md. Use the existing CDPA API/CLI and durable task/request "
        "ledger to determine exact G6 9202 ownership, active RUNNING/WAITING "
        "tasks, task identity, and exact accepted/ambiguous Send provenance. "
        "An API HEALTHY result or an accepted Send is NOT terminal success. "
        "A historical PAUSED, STOPPED or BLOCKED task is not by itself a live "
        "9202 writer; never reuse or replay its request or steal its tab. "
        "\n\nMANDATORY ONE-ACTION DECISION EACH CYCLE: "
        "(1) If 9202 has no active owner, CDPA is ready, and no previously "
        "submitted successor or ambiguous submit remains unreconciled, "
        "choose the highest-value unfinished campaign gate using exact evidence "
        "and dispatch exactly one fresh, bounded CDPA Solo successor through "
        "workflows/self-evolution/spawn_cdpa.py --role <DEV|TEST|REVIEW> "
        "--team <unique-team> --instruction <one-specific-test-or-repair> "
        "--parent <exact-parent-task-id>. Execute the canonical Python "
        "constructor once; do not construct a second prompt or Send yourself. "
        "Inspect the created task and actual first-turn hops[0].prompt/receipt "
        "for complete policy, bounded instruction and correct parent. "
        "If submission times out or is ambiguous, reconcile its exact team "
        "and request before any further dispatch; NEVER invoke it twice. "
        "(2) Otherwise diagnose the blocking current task/provider condition "
        "with exact task/endpoint/request evidence; observe or use an existing "
        "authorized CDPA Resume only if its immutable Send provenance permits. "
        "If recovery is unsafe, record an explicit safety hold, with the "
        "specific failed invariant, exact owner/request, evidence path, and "
        "next falsifiable action. Do not spin or spawn into the same hold. "
        "An unresolved historical accepted Send forbids replay of THAT request, "
        "not a separate fresh task on a demonstrably unowned endpoint. "
        "\n\nPriority examples for one bounded task: "
        "CONVERSATION_IDENTITY_MISMATCH; response_terminal_missing; "
        "preboundary_acquisition_recovery_required; safe PAUSED-task "
        "reconciliation; CDPA-only runtime reload and live restart E2E. "
        "Require independent M365 Opus review on FJP and CDPA Solo REVIEW "
        "on G6 for significant fixes. Never touch 9212/FJP browsers, "
        "restart Chrome for source drift, reload Temporary Chat, replay an "
        "accepted/ambiguous Send, or bypass CAPTCHA. A genuine 9202 provider "
        "challenge permits only the bounded reconciled recovery in the policy. "
        "\n\nEnd EVERY cycle with PI_ACTION: DISPATCH, OBSERVE, or SAFETY_HOLD "
        "and the exact team/task/request, evidence, result, next action. "
        "A read-only inventory with no action or specific safety hold is "
        "not an operational checkpoint. Do not claim the campaign is DONE "
        "before full independent E2E release acceptance.\n"
    )


def build_pi_command():
    if not PI.is_file():
        raise FileNotFoundError(str(PI))
    return ["node.exe", str(PI), "-p",
            "--model", "cliproxy-g8/gpt-5.6-luna", "--thinking", "high",
            "--offline", "--no-session", "--no-approve", "--no-context-files",
            "--no-prompt-templates", "--no-skills", "--no-mcp",
            "--no-extensions", "--tools", "read,bash", coordinator_prompt()]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def contain_current_process_tree():
    """Windows kill-on-close job includes runner and all later descendants."""
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        _JOB_HANDLE = job
    except BaseException:
        kernel.CloseHandle(job)
        raise


@contextmanager
def single_owner(path=LOCK):
    """The byte-range file lock excludes a competing runner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        # Lock before touching the byte: Windows rejects reads of a locked range.
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Pi operator already active") from error
        lock.seek(0)
        if not lock.read(1):
            lock.seek(0)
            lock.write(b"x")
            lock.flush()
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def reap_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    else:
        child.wait()


def run_cycle(iteration):
    """Reap Pi and its descendants before another cycle can begin."""
    cmd = [sys.executable, str(HERE / "run_pi.py"), "--pi-child", *build_pi_command()]
    child = None
    with LOG.open("a", encoding="utf-8") as output:
        output.write("\n=== PI CYCLE " + str(iteration) + " ===\n")
        output.flush()
        try:
            child = subprocess.Popen(
                cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            write_state(owner_pid=os.getpid(), pi_pid=child.pid,
                        iteration=iteration, status="RUNNING", health=health())
            started = time.monotonic()
            last_log_bytes = LOG.stat().st_size
            last_output_at = started
            while child.poll() is None:
                time.sleep(5)
                size = LOG.stat().st_size
                if size > last_log_bytes:
                    last_log_bytes = size
                    last_output_at = time.monotonic()
                elapsed = round(time.monotonic() - started)
                idle = round(time.monotonic() - last_output_at)
                write_state(owner_pid=os.getpid(), pi_pid=child.pid,
                            iteration=iteration, status="RUNNING",
                            elapsed_seconds=elapsed,
                            no_output_seconds=idle, health=health())
                if elapsed > 1800 and idle > 900:
                    output.write("\nPI_CYCLE_STALLED: restart Pi only, "
                                 "retain CDPA tasks and accepted Sends.\n")
                    output.flush()
                    break
        finally:
            if child is not None:
                reap_child(child)
                output.write("\nPI_EXIT_CODE=" + str(child.returncode) + "\n")
                output.flush()
    return child.returncode


def previous_pi_still_running():
    """Fail closed when an older runner's Pi survived without containment."""
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if previous.get("status") != "RUNNING" or not previous.get("pi_pid"):
        return False
    pid = int(previous["pi_pid"])
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        # Access denied could hide a live process: do not risk overlap.
        error = ctypes.get_last_error()
        if error == 87:  # invalid parameter: PID no longer exists
            return False
        raise RuntimeError(f"Cannot reconcile prior Pi PID {pid}: WinError {error}")
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def run():
    if socket.gethostname().upper() != "DESKTOP-B5E4IUE":
        raise RuntimeError("G6 only")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with single_owner():
        contain_current_process_tree()  # Fail closed before creating Pi.
        if previous_pi_still_running():
            raise RuntimeError("SAFETY_HOLD: prior Pi child is still alive; "
                               "reconcile ownership before relaunch")
        iteration = 0
        while True:
            iteration += 1
            try:
                exit_code = run_cycle(iteration)
                write_state(owner_pid=os.getpid(), iteration=iteration,
                            status="NEXT_CYCLE", last_exit=exit_code,
                            health=health())
            except Exception as error:
                with LOG.open("a", encoding="utf-8") as output:
                    output.write("PI_OPERATOR_ERROR=" + repr(error) + "\n")
                write_state(owner_pid=os.getpid(), iteration=iteration,
                            status="RECOVERY_PENDING", error=repr(error))
            time.sleep(45)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pi-child":
        # One bounded child job per Pi cycle. On normal exit, exception or
        # parent termination, its handle closes and reaps lingering descendants.
        contain_current_process_tree()
        raise SystemExit(subprocess.call(sys.argv[2:], cwd=str(ROOT)))
    run()
