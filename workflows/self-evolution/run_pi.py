"""Single-owner Pi operations runner. CDPA alone owns task and browser Send."""
from __future__ import annotations

import ctypes
import hashlib
import json
import msvcrt
import os
import socket
import subprocess
import sys
import time
import tempfile
import uuid
from urllib.request import urlopen
from urllib.error import HTTPError
from urllib.parse import quote
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from core import canonical_goal, health
from playwright_auto.cdpa_identity import generate_idempotent_task_id

RUNTIME = ROOT / ".runtime" / "self-evolution"
LOCK = RUNTIME / "pi-runner.lock"
STATE = RUNTIME / "pi-runner.json"
STATE_LOCK = RUNTIME / "pi-state.lock"
LOG = RUNTIME / "pi-runner.log"
PI = Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
_JOB_HANDLE = None


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


@contextmanager
def state_guard():
    """Serialize Pi's runner checkpoint and its canonical constructor intent."""
    STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with STATE_LOCK.open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _replace_state(values):
    # A unique draft prevents stale writers/readers from clobbering one another.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=STATE.parent,
        prefix=STATE.name + ".", suffix=".tmp", delete=False,
    ) as output:
        draft = Path(output.name)
        json.dump(values, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    try:
        for attempt in range(5):
            try:
                os.replace(draft, STATE)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                # Windows readers can briefly deny replacement of an open file.
                time.sleep(0.05 * (attempt + 1))
    finally:
        draft.unlink(missing_ok=True)


def write_state(**values):
    with state_guard():
        previous = read_state()
        # Heartbeats must never erase a parent reservation made by Pi's launcher.
        if "transition" not in values and "transition" in previous:
            values["transition"] = previous["transition"]
        if values.get("transition") and "dispatch_allowed" not in values:
            values["dispatch_allowed"] = False
        if values.get("status") == "RUNNING":
            for label, pid in (("owner", values.get("owner_pid")),
                               ("pi", values.get("pi_pid"))):
                if pid and label + "_creation_id" not in values:
                    values[label + "_creation_id"] = process_creation_id(int(pid))
        _replace_state(values)


def record_transition_intent(*, parent, team, role, goal, idempotency_key):
    """Reserve immutable parent/command identity before CDPA submission."""
    title = f"CDPA Self-Evolution {role}: {team}"
    transition = {
        "parent_task_id": parent,
        "team": team,
        "role": role,
        "idempotency_key": idempotency_key,
        "task_id": generate_idempotent_task_id(title, idempotency_key),
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
    }
    with state_guard():
        state = read_state()
        if state.get("status") not in {"RUNNING", "INTENT_PENDING"} or state.get("dispatch_allowed") is not False:
            raise RuntimeError("Pi is not the active dispatch owner")
        old = state.get("transition")
        if old and old != transition:
            raise RuntimeError("parent transition conflict: reconcile previous command first")
        state["transition"] = transition
        state["dispatch_allowed"] = False
        _replace_state(state)
    return transition


def campaign_tasks():
    """Read only CDPA's existing task projection; unknown cannot authorize dispatch."""
    try:
        with urlopen("http://127.0.0.1:9225/api/tasks", timeout=5) as response:
            data = json.load(response)
        if not isinstance(data.get("items"), list):
            return None
        return {
            item["task_id"]: {"status": item.get("status"), "team": item.get("team"),
                              "parent_task_id": item.get("parent_task_id")}
            for item in data["items"]
            if isinstance(item, dict)
            and isinstance(item.get("task_id"), str)
            and item.get("task_title", "").startswith("CDPA Self-Evolution ")
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def verified_successor(before, after, task_id, claim):
    """Inspect the actual CDPA-owned manifest and Send receipt, never Pi text alone."""
    record = after.get(task_id) or {}
    team = record.get("team")
    if not isinstance(team, str) or not team or "/" in team or "\\" in team:
        return False
    claimed_parents = [parent for parent in before if parent in claim.split()]
    if len(claimed_parents) != 1:
        return False
    parent = claimed_parents[0]
    task_dir = ROOT / ".plan" / team / task_id
    try:
        candidates = [path for path in task_dir.glob("*.json")
                      if path.name != "requests.json"]
        if len(candidates) != 1:
            return False
        manifest = json.loads(candidates[0].read_text(encoding="utf-8"))
        ledger = json.loads((task_dir / "requests.json").read_text(encoding="utf-8"))
        hop = manifest["hops"][0]
        request_id = task_id + "-hop1"
        request = ledger["records"][request_id]
        receipt = hop["receipt"]
        prompt = hop["prompt"]
        goal = canonical_goal()
        return (
            manifest.get("task_id") == task_id
            and manifest.get("team") == team
            and manifest.get("parent_task_id") == parent
            and request.get("request_id") == request_id
            and hop.get("request_id") == request_id
            and request.get("attempts") == 1
            and request.get("status") in {"sent", "completed"}
            and request.get("prompt") == prompt
            and request.get("receipt", {}).get("prompt") == prompt
            and receipt.get("attempts") == 1
            and receipt.get("prompt") == prompt
            and receipt.get("accepted_via") == "user_message_identity"
            and bool(receipt.get("user_message_id"))
            and (
                bool(receipt.get("conversation_id"))
                or (
                    isinstance(hop.get("conversation_url"), str)
                    and len(hop["conversation_url"]) > len("https://chatgpt.com/c/")
                    and hop["conversation_url"].startswith("https://chatgpt.com/c/")
                    and bool(receipt.get("binding", {}).get("page_id"))
                    and bool(receipt.get("binding", {}).get("role"))
                    and manifest["roles"][hop["target_role"]].get("page_url") == hop["conversation_url"]
                    and manifest["roles"][hop["target_role"]].get("page_id") == receipt.get("binding", {}).get("page_id")
                    and manifest["roles"][hop["target_role"]].get("physical_role") == hop.get("physical_role")
                    and hop.get("physical_role") == receipt.get("binding", {}).get("role")
                )
            )
            and all(
                request["receipt"].get(field) == receipt.get(field)
                for field in ("accepted_via", "attempts", "conversation_id", "user_message_id", "binding")
            )
            and goal in prompt
            and str(HERE / "GOAL_AND_RULES.md") in prompt
            and "Current bounded instruction:" in prompt
            and f"Parent task: {parent}" in prompt
            and "spawn_cdpa.py" in prompt
            and "9202_PLUSHCU.bat" in prompt
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def command_by_key(key):
    """Read-only exact command provenance via CDPA's authoritative ledger."""
    try:
        with urlopen(
            "http://127.0.0.1:9225/api/commands/by-idempotency/" + quote(key, safe=""),
            timeout=5,
        ) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def reconcile_transition(transition):
    """No submit/replay here. Advance only after CDPA command and Send agree."""
    parent = transition["parent_task_id"]
    task_id = transition["task_id"]
    try:
        command = command_by_key(transition["idempotency_key"])
        if command is None:
            return "SAFETY_HOLD", f"{parent}: command absent; reconcile before any resubmit"
        required = {
            "idempotency_key": transition["idempotency_key"],
            "task_id": task_id,
            "parent_task_id": parent,
            "team": transition["team"],
            "kind": "create_task",
            "status": "applied",
        }
        if any(command.get(key) != value for key, value in required.items()):
            return "SAFETY_HOLD", f"{parent}: command identity/status mismatch for {task_id}"
        tasks = campaign_tasks()
        if tasks is None:
            return "SAFETY_HOLD", f"{parent}: task projection unavailable"
        record = tasks.get(task_id)
        if not record or record.get("parent_task_id") != parent or record.get("team") != transition["team"]:
            return "SAFETY_HOLD", f"{parent}: child projection missing/mismatched {task_id}"
        if not verified_successor(
            {parent: {"status": "DONE"}}, tasks, task_id,
            f"DISPATCH {task_id} parent {parent}",
        ):
            return "SAFETY_HOLD", f"{parent}: child prompt, receipt or request unverified {task_id}"
        # Validate exact instruction hash as well as full contract and stable parent.
        task_dir = ROOT / ".plan" / transition["team"] / task_id
        files = [p for p in task_dir.glob("*.json") if p.name != "requests.json"]
        if len(files) != 1:
            return "SAFETY_HOLD", f"{parent}: ambiguous child manifest"
        manifest = json.loads(files[0].read_text(encoding="utf-8"))
        actual_goal = manifest.get("goal")
        if not isinstance(actual_goal, str):
            return "SAFETY_HOLD", f"{parent}: child goal unavailable"
        actual_hash = hashlib.sha256(actual_goal.encode("utf-8")).hexdigest()
        # CLI normalizes the exact --goal with .strip() before committing it.
        # Older Pi checkpoints hash the unstripped constructor (one trailing LF).
        if command.get("goal_sha256") != actual_hash or transition["goal_sha256"] not in {
            actual_hash, hashlib.sha256((actual_goal + "\n").encode("utf-8")).hexdigest(),
        }:
            return "SAFETY_HOLD", f"{parent}: child bounded instruction mismatch"
        command_role = command.get("role")
        if command_role is None:
            if (manifest.get("start_role") != transition["role"] or
                    manifest.get("hops", [{}])[0].get("target_role") != transition["role"]):
                return "SAFETY_HOLD", f"{parent}: child role unverified"
        elif command_role != transition["role"]:
            return "SAFETY_HOLD", f"{parent}: command role mismatch"
        if command.get("command_id") not in manifest.get("applied_command_ids", []):
            return "SAFETY_HOLD", f"{parent}: child missing exact applied creation-command provenance"
        return "DISPATCH_VERIFIED", task_id
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        return "SAFETY_HOLD", f"{parent}: reconciliation unavailable: {error!r}"


def classify_cycle(before, after, output, exit_code):
    """Only a proven CDPA handoff may advance; uncertain cycles need reconciliation."""
    if before is None or after is None:
        return "SAFETY_HOLD", "CDPA task inventory unavailable; reconcile exact command ledger"
    created = set(after) - set(before)
    claims = [line.partition("PI_ACTION:")[2].strip()
              for line in output.splitlines() if "PI_ACTION:" in line]
    claim = claims[-1] if claims else ""
    if len(created) > 1:
        return "SAFETY_HOLD", "multiple new CDPA tasks: " + ",".join(sorted(created))
    if created:
        task_id = next(iter(created))
        if exit_code:
            return "RECOVERY_PENDING", task_id + ": reconcile committed command before dispatch"
        if claim.startswith("DISPATCH ") and task_id in claim.split() and verified_successor(before, after, task_id, claim):
            return "DISPATCH_VERIFIED", task_id
        return "SAFETY_HOLD", "unverified successor " + task_id + ": reconcile manifest, parent, request and receipt"
    if exit_code:
        return "RECOVERY_PENDING", "Pi exited without verified dispatch; reconcile previous command"
    if claim.startswith("OBSERVE") or claim.startswith("WAIT"):
        task_id = next((identity for identity in before if identity in claim.split()), None)
        if task_id and before[task_id].get("status") not in {"DONE", "STOPPED", "PAUSED", "BLOCKED"}:
            return "OBSERVE", task_id
    return "NO_PROGRESS", "No verified CDPA operation; no durable handoff or hold"


def reconcile_stale_running(previous):
    """Clear a transitionless dead heartbeat only against existing CDPA authority."""
    if previous.get("status") != "RUNNING" or previous.get("transition"):
        return "SAFETY_HOLD", "running cycle has an unresolved transition"
    for label, pid in (("owner", previous.get("owner_pid")),
                       ("pi", previous.get("pi_pid"))):
        if not isinstance(pid, int) or pid <= 0:
            return "SAFETY_HOLD", f"missing {label} process identity"
        current = process_creation_id(pid)
        recorded = previous.get(label + "_creation_id")
        if current is not None and (recorded is None or current == recorded):
            return "SAFETY_HOLD", f"{label} process could still own the cycle"
    tasks = campaign_tasks()
    if tasks is None:
        return "SAFETY_HOLD", "CDPA task projection unavailable"
    terminal = {"DONE", "STOPPED", "CLEARED"}
    active = [task_id for task_id, task in tasks.items()
              if task.get("status") not in terminal]
    if active:
        return "SAFETY_HOLD", "live campaign tasks require reconciliation: " + ",".join(sorted(active))
    # A historical applied command is harmless only when its exact child
    # and parent identities remain terminal in the authoritative task ledger.
    for parent in tasks:
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, "cdpa:self-evolution:successor:" + parent))
        try:
            command = command_by_key(key)
        except (OSError, ValueError, TypeError) as error:
            return "SAFETY_HOLD", f"command ledger unavailable for {parent}: {error!r}"
        if command is None:
            continue
        child = tasks.get(command.get("task_id"))
        if not (
            command.get("kind") == "create_task"
            and command.get("status") == "applied"
            and command.get("parent_task_id") == parent
            and child is not None
            and child.get("status") in terminal
            and child.get("parent_task_id") == parent
            and child.get("team") == command.get("team")
        ):
            return "SAFETY_HOLD", f"parent {parent} has an unreconciled creation command"
    return "OBSERVE", "transitionless stale RUNNING heartbeat; owner and child dead; ledger reconciled"


def unreconciled_state():
    """A prior ambiguous cycle cannot silently authorize another dispatch."""
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"status": "SAFETY_HOLD", "reason": "Pi checkpoint unreadable"}
    if previous.get("status") in {
        "SAFETY_HOLD", "RECOVERY_PENDING", "NO_PROGRESS",
        "INTENT_PENDING", "RUNNING",
    }:
        return previous
    return None


def commit_cycle_decision(*, iteration, before, after, output, exit_code):
    """Atomically checkpoint the exact observed outcome before any next cycle."""
    transition = read_state().get("transition")
    status, reason = (
        reconcile_transition(transition) if transition
        else classify_cycle(before, after, output, exit_code)
    )
    affected = sorted(set(before or {}) | set(after or {}))
    new_tasks = sorted(set(after or {}) - set(before or {}))
    unresolved = status not in {"DISPATCH_VERIFIED", "OBSERVE"}
    if unresolved:
        status = "SAFETY_HOLD"
    write_state(
        owner_pid=os.getpid(), iteration=iteration, status=status, reason=reason,
        last_exit=exit_code, affected_task_ids=affected,
        possible_request_ids=[f"{task_id}-hop1" for task_id in new_tasks],
        dispatch_allowed=not unresolved, health=health(),
        transition=transition if unresolved else None,
        last_transition=transition if not unresolved else None,
    )
    return status, reason


def coordinator_prompt():
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    streak = previous.get("no_progress_cycles", 0)
    recovery = (
        f"\n\nWARNING: {streak} consecutive NO_PROGRESS cycles: "
        f"{previous.get('reason', 'unknown')}. Investigate this exact blocker "
        "and choose a bounded evidence-backed action this cycle.\n"
        if isinstance(streak, int) and streak >= 2 else ""
    )
    return (
        canonical_goal()
        + recovery
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
    try:
        intent = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        intent = {}
    context = {"affected_task_ids": intent.get("affected_task_ids", []),
               "dispatch_allowed": False}
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
                        iteration=iteration, status="RUNNING", health=health(),
                        **context)
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
                            no_output_seconds=idle, health=health(),
                            **context)
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


def process_creation_id(pid):
    """Return Windows process creation FILETIME; unknown access fails closed."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x101000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise RuntimeError(f"Cannot identify process {pid}: WinError {error}")
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:  # Exited process can retain a queryable PID/handle.
            return None
        if wait != 0x102:
            raise RuntimeError(f"Cannot determine process {pid} liveness: {wait}")
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def previous_pi_still_running():
    """Fail closed on a live legacy PID, unless creation identity proves reuse."""
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if previous.get("status") != "RUNNING" or not previous.get("pi_pid"):
        return False
    pid = int(previous["pi_pid"])
    identity = process_creation_id(pid)
    if identity is None:
        return False
    recorded = previous.get("pi_creation_id")
    return recorded is None or identity == recorded


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
            pending = unreconciled_state()
            if pending:
                # Only a proven-dead, transitionless cycle can be retired.
                # The owner file lock remains held throughout reconciliation.
                if pending.get("status") == "RUNNING" and not pending.get("transition"):
                    try:
                        result, reason = reconcile_stale_running(pending)
                    except Exception as error:
                        result, reason = "SAFETY_HOLD", f"stale checkpoint inspection failed: {error!r}"
                    if result == "OBSERVE":
                        write_state(**{**pending, "owner_pid": os.getpid(),
                                       "status": "RECOVERED", "reason": reason,
                                       "dispatch_allowed": False, "health": health()})
                        continue
                    pending = {**pending, "reason": reason}
                # Reconcile the same durable command after a restart without
                # repeating task creation or any browser Send.
                transition = pending.get("transition")
                if transition:
                    result, reason = reconcile_transition(transition)
                    if result == "DISPATCH_VERIFIED":
                        write_state(**{**pending, "owner_pid": os.getpid(),
                                       "status": result, "reason": reason,
                                       "transition": None, "last_transition": transition,
                                       "dispatch_allowed": True, "health": health()})
                        continue
                    pending = {**pending, "reason": reason}
                write_state(**{**pending, "owner_pid": os.getpid(),
                               "status": "SAFETY_HOLD", "dispatch_allowed": False,
                               "health": health()})
                time.sleep(45)
                continue
            try:
                before = campaign_tasks()
                if before is None:
                    commit_cycle_decision(iteration=iteration, before=None, after=None,
                                          output="", exit_code=1)
                    continue
                # Persist intent BEFORE Pi can cross the task-submit boundary.
                write_state(owner_pid=os.getpid(), iteration=iteration,
                            status="INTENT_PENDING", dispatch_allowed=False,
                            affected_task_ids=sorted(before),
                            reason="Cycle may submit a task; reconcile after crash",
                            health=health())
                offset = LOG.stat().st_size if LOG.exists() else 0
                exit_code = run_cycle(iteration)
                after = campaign_tasks()
                with LOG.open("r", encoding="utf-8", errors="replace") as output:
                    output.seek(offset)
                    cycle_output = output.read()
                status, reason = commit_cycle_decision(
                    iteration=iteration, before=before, after=after,
                    output=cycle_output, exit_code=exit_code)
                with LOG.open("a", encoding="utf-8") as output:
                    output.write("PI_VERIFIED_OUTCOME=" + status + ": " + reason + "\n")
            except Exception as error:
                with LOG.open("a", encoding="utf-8") as output:
                    output.write("PI_OPERATOR_ERROR=" + repr(error) + "\n")
                write_state(owner_pid=os.getpid(), iteration=iteration,
                            status="SAFETY_HOLD", reason=repr(error),
                            dispatch_allowed=False, health=health())
            time.sleep(45)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pi-child":
        # One bounded child job per Pi cycle. On normal exit, exception or
        # parent termination, its handle closes and reaps lingering descendants.
        contain_current_process_tree()
        raise SystemExit(subprocess.call(sys.argv[2:], cwd=str(ROOT)))
    run()
