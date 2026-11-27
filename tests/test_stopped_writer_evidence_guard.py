from __future__ import annotations

import json
from pathlib import Path

import pytest

from playwright_auto.file_lock import exclusive_file_lock
from test_cdpa_stop_writer_cleanup import StopActions, stop
from test_cdpa_worker import setup_task


def stopped_task(tmp_path):
    _, store, state, worker = setup_task(tmp_path, conversation_mode="persistent")
    state = stop(store, state, worker, StopActions())
    path = Path(state["manifest_path"])
    ledger = path.with_name("requests.json")
    ledger.write_text(json.dumps({"version": 1, "records": {}}), encoding="utf-8")
    return store, state, path, ledger


def guard(store, path):
    method = getattr(store, "stopped_writer_evidence_guard", None)
    assert callable(method), "TaskStore has no read-only stopped-writer evidence guard"
    return method(path)


@pytest.mark.parametrize("immutable", [False, True])
def test_guard_keeps_manifest_catalog_and_ledger_bytes_unchanged(tmp_path, immutable):
    store, state, path, ledger = stopped_task(tmp_path)
    if immutable:
        store.replace_task_and_rewire(
            state["task_id"], "Approved replacement after normal Stop release",
            reuse_team=False, rewire_children=True, restart_team=True,
            incident_id="read-only-maintenance-guard",
        )
    before = {p: p.read_bytes() for p in (path, ledger, store.catalog_path)}
    with guard(store, path) as (current, historical, records):
        assert current["task_id"] == state["task_id"]
        assert current["status"] == "STOPPED" and historical is immutable
        assert records == {}
        current["stop_reason"] = "local copy only"
        records["local-copy"] = {}
    assert all(p.read_bytes() == content for p, content in before.items())


def test_guard_holds_existing_manifest_and_ledger_locks_without_rewriting(tmp_path):
    store, _, path, ledger = stopped_task(tmp_path)
    before = ledger.read_bytes()
    with guard(store, path):
        for lock in (store.allocation_lock, store._lock_path(path), ledger.with_suffix(".json.lock")):
            with pytest.raises(BlockingIOError):
                with exclusive_file_lock(lock, blocking=False):
                    pytest.fail("evidence lock was not held")
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("problem", ["missing", "version", "records"])
def test_unknown_ledger_does_not_create_or_normalize_evidence(tmp_path, problem):
    store, _, path, ledger = stopped_task(tmp_path)
    if problem == "missing":
        ledger.unlink()
    else:
        ledger.write_text(json.dumps({"version": 99 if problem == "version" else 1,
                                      "records": [] if problem == "records" else {}}), encoding="utf-8")
    before = ledger.read_bytes() if ledger.exists() else None
    with pytest.raises((ValueError, FileNotFoundError)):
        with guard(store, path):
            pytest.fail("unknown evidence was admitted")
    assert (ledger.read_bytes() if ledger.exists() else None) == before


def test_nonstopped_task_cannot_enter_maintenance_guard(tmp_path):
    _, store, state, _ = setup_task(tmp_path, conversation_mode="persistent")
    path = Path(state["manifest_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        with guard(store, path):
            pytest.fail("non-stopped task was admitted")
    assert path.read_bytes() == before
