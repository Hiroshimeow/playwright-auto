"""Regression tests: allocation-lock contention must not cause recovery_required.

Root cause: TaskStore.load_task_id() used exclusive_file_lock(blocking=False),
so ordinary scheduler contention on the allocation lock raised BlockingIOError
before any mutation.  The command-dispatch broad exception handler converted that
to terminal recovery_required (historical: 13 occurrences, cmd-4a3ad80f...).

Fix: load_task_id must use blocking=True so it waits for the lock instead of
raising. Command dispatch runs in a worker thread; bounded blocking is safe and
does not block the asyncio heartbeat.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.file_lock import exclusive_file_lock

from test_cdpa_core import write_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup(tmp_path: Path, *, task_id: str = "task-a") -> tuple[Any, TaskStore, Any, CDPAWorker]:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Regression anchor task",
        requested_team="alpha",
        task_id=task_id,
        report_mode="file",
    )
    return config, store, state, CDPAWorker(config, store=store)


# ---------------------------------------------------------------------------
# Red evidence: without the fix, these tests fail because load_task_id raises
# BlockingIOError under allocation-lock contention, which the command dispatch
# broad except-handler converts to recovery_required.
# ---------------------------------------------------------------------------

def test_create_command_replay_preflight_waits_under_allocation_lock_contention(
    tmp_path: Path,
) -> None:
    """Command replay preflight (load_task_id) must wait, not raise, when the
    allocation lock is momentarily held by another writer.  The command must
    apply exactly once and must NOT produce recovery_required."""
    config, store, _existing, worker = _setup(tmp_path, task_id="task-existing-lock-test")
    worker.hydrate_runtime()

    worker.runtime_db.enqueue_command(
        command_id="cmd-contention-create",
        idempotency_key="contention-create",
        kind="create_task",
        task_id="task-created-under-contention",
        expected_task_version=None,
        payload={
            "task": "Task created while allocation lock is held",
            "requested_team": "contention-team",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN", "REVIEW"],
        },
    )

    # Hold the allocation lock for 200 ms in a background thread.
    # This simulates the scheduler or another operation holding the lock
    # while the command dispatch preflight tries to read via load_task_id.
    lock_held = threading.Event()
    lock_released = threading.Event()

    def hold_lock() -> None:
        with exclusive_file_lock(store.allocation_lock, blocking=True):
            lock_held.set()
            time.sleep(0.2)
        lock_released.set()

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert lock_held.wait(timeout=5), "background thread failed to acquire allocation lock"

    # Dispatch while the allocation lock is held.  This must block (wait) and
    # then succeed after the lock is released — not raise recovery_required.
    result = worker._apply_next_command()
    t.join(timeout=5)
    assert lock_released.is_set(), "background lock holder did not release cleanly"

    # The command must have applied successfully, not become recovery_required.
    assert result is not None, "command dispatch returned None"
    assert result["status"] == "applied", (
        f"expected 'applied' but got {result['status']!r} — "
        "BlockingIOError contention was incorrectly promoted to recovery_required"
    )
    assert not result.get("result", {}).get("recovery_required"), (
        "command was incorrectly marked recovery_required due to lock contention"
    )

    # Exact-once: the task manifest must exist exactly once.
    created = store.load_task_id("task-created-under-contention")
    assert created is not None, "task manifest was not created"
    assert "cmd-contention-create" in created["applied_command_ids"], (
        "command was not recorded as applied"
    )


def test_create_command_replay_idempotency_preserved_under_allocation_lock_contention(
    tmp_path: Path,
) -> None:
    """When a create command is replayed (idempotency check) under allocation-lock
    contention, it must reconcile exactly once and must NOT re-create the task or
    produce recovery_required."""
    config, store, _existing, worker = _setup(tmp_path, task_id="task-existing-replay")
    worker.hydrate_runtime()

    worker.runtime_db.enqueue_command(
        command_id="cmd-idempotent-create",
        idempotency_key="idempotent-create",
        kind="create_task",
        task_id="task-replay-contention",
        expected_task_version=None,
        payload={
            "task": "Idempotent task creation",
            "requested_team": "replay-team",
            "repository": str(tmp_path),
            "report_mode": "file",
            "roles": ["PLAN"],
        },
    )

    # First apply: no contention.
    first = worker._apply_next_command()
    assert first["status"] == "applied", f"first apply failed: {first['status']!r}"

    # Reset the command to queued to force a replay on the second dispatch.
    with worker.runtime_db.connection() as conn:
        conn.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL "
            "WHERE command_id = ?",
            ("cmd-idempotent-create",),
        )

    # Hold the allocation lock during the replay preflight.
    lock_held = threading.Event()
    lock_released = threading.Event()

    def hold_lock() -> None:
        with exclusive_file_lock(store.allocation_lock, blocking=True):
            lock_held.set()
            time.sleep(0.2)
        lock_released.set()

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert lock_held.wait(timeout=5), "background thread failed to acquire allocation lock"

    # Replay while the lock is held — must wait and then reconcile, not error.
    replayed = worker._apply_next_command()
    t.join(timeout=5)
    assert lock_released.is_set(), "background lock holder did not release cleanly"

    assert replayed is not None, "replayed command dispatch returned None"
    assert replayed["status"] == "applied", (
        f"expected 'applied' on replay but got {replayed['status']!r}"
    )
    assert replayed.get("result", {}).get("reconciled") is True, (
        "second dispatch should reconcile (exact-once) but reconciled flag is missing"
    )

    # Exact-once: command_id must appear exactly once in applied_command_ids.
    task = store.load_task_id("task-replay-contention")
    assert task is not None
    applied = task.get("applied_command_ids", [])
    assert applied.count("cmd-idempotent-create") == 1, (
        f"command was applied {applied.count('cmd-idempotent-create')} time(s); "
        "exact-once invariant violated"
    )


def test_load_task_id_does_not_raise_under_allocation_lock_contention(
    tmp_path: Path,
) -> None:
    """load_task_id must block and return the task, not raise BlockingIOError,
    when the allocation lock is held by another thread."""
    config, store, state, _worker = _setup(tmp_path, task_id="task-load-contention")
    task_id = state["task_id"]

    lock_held = threading.Event()
    lock_released = threading.Event()

    def hold_lock() -> None:
        with exclusive_file_lock(store.allocation_lock, blocking=True):
            lock_held.set()
            time.sleep(0.2)
        lock_released.set()

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    assert lock_held.wait(timeout=5), "background thread failed to acquire allocation lock"

    # This must block and succeed, not raise BlockingIOError.
    loaded = store.load_task_id(task_id)
    t.join(timeout=5)
    assert lock_released.is_set()

    assert loaded is not None, "load_task_id returned None under contention"
    assert loaded["task_id"] == task_id
