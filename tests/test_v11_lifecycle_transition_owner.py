"""R9: TaskStore owns admission for persisted TeamTask status transitions."""
from __future__ import annotations

from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from test_cdpa_core import write_config


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(load_cdpa_config(write_config(tmp_path), repository_root=tmp_path))


def _terminal_task(store: TaskStore, *, outcome: str) -> tuple[dict, int, str]:
    state = store.create_task(
        "terminal transition owner",
        requested_team="life",
        task_id=f"task-{outcome.lower()}",
    )
    hop_id = int(state["active_hop_id"])
    role = str(state["active_role"])
    terminal = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": outcome,
            "terminal_state": outcome,
            "active_role": None,
            "active_hop_id": None,
        },
    )
    assert terminal["status"] == outcome
    return terminal, hop_id, role


@pytest.mark.parametrize("outcome", ["DONE", "STOPPED"])
def test_terminal_task_cannot_be_reopened_through_generic_store_update(
    tmp_path: Path, outcome: str
):
    store = _store(tmp_path)
    terminal, hop_id, role = _terminal_task(store, outcome=outcome)

    with pytest.raises(ValueError, match="terminal task status is immutable"):
        store.update(
            terminal["manifest_path"],
            lambda current: {
                **current,
                "status": "RUNNING",
                "terminal_state": None,
                "active_role": role,
                "active_hop_id": hop_id,
            },
        )

    current = store.load(terminal["manifest_path"])
    assert current["status"] == outcome
    assert current["terminal_state"] == outcome


def test_terminal_task_allows_same_outcome_metadata_update(tmp_path: Path):
    store = _store(tmp_path)
    terminal, _hop_id, _role = _terminal_task(store, outcome="DONE")

    saved = store.update(
        terminal["manifest_path"],
        lambda current: {**current, "cleanup_note": "verified"},
    )

    assert saved["status"] == "DONE"
    assert saved["terminal_state"] == "DONE"
    assert saved["cleanup_note"] == "verified"


def test_nonterminal_legacy_transition_remains_compatible(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_task(
        "legacy compatibility", requested_team="legacy", task_id="task-legacy"
    )

    running = store.update(
        state["manifest_path"],
        lambda current: {
            **current,
            "status": "RUNNING",
            "kanban_column": "PLANNING",
        },
    )

    assert running["status"] == "RUNNING"


def test_repair_disposition_release_restarts_running_clock_through_lifecycle_owner(tmp_path):
    from test_cdpa_repair import block_task, repair_request
    from test_cdpa_worker import setup_task

    _config, store, state, _worker = setup_task(
        tmp_path, task_id="task-r10-repair-clock"
    )
    state = block_task(store, state, hop_state="waiting")
    store.create_or_gate_repair(repair_request(state, "HOLD_FOR_REPAIR"))
    held = store.load(state["manifest_path"])
    assert held["status"] == "WAITING"
    assert held.get("running_since") is None

    store.create_or_gate_repair(
        repair_request(held, "CONTINUE_IN_PARALLEL")
    )
    released = store.load(state["manifest_path"])

    assert released["status"] == "RUNNING"
    assert released.get("running_since")
    assert released.get("active_role_running_since")
