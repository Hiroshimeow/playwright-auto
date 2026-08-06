from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker

from test_cdpa_core import write_config


class ExplodingBrowserContext:
    def __getattr__(self, name: str):
        raise AssertionError(f"browser accessed before dependency barrier: {name}")


def make_store(tmp_path: Path) -> tuple[TaskStore, object]:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config), config


def force_state(store: TaskStore, state: dict, status: str) -> dict:
    def mutate(current: dict) -> dict:
        current["status"] = status
        current["kanban_column"] = status
        current["active_action"] = status.lower()
        if status == "BLOCKED":
            current["block_code"] = "forced_fixture"
            current["block_retryable"] = True
            current["block_reason"] = "forced fixture block"
        if status == "PAUSED":
            current["resume_column"] = "PLANNING"
            current["pause_reason"] = "forced fixture pause"
        return current

    return store.update(state["manifest_path"], mutate)


@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED", "INBOX"])
def test_unfinished_dependency_reconciles_executable_state_before_transport(
    tmp_path: Path, status: str
):
    store, _config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    forced = force_state(store, child, status)
    preserved_hop = copy.deepcopy(forced["hops"][0])

    reconciled, changed = store.refresh_scheduling(
        forced["manifest_path"], tasks=[parent, forced], state=forced
    )

    assert changed is True
    assert reconciled["status"] == "WAITING"
    assert reconciled["kanban_column"] == "WAITING"
    assert reconciled["active_action"] == "waiting_dependency"
    assert reconciled["waiting_code"] == "dependency"
    assert reconciled["waiting"]["waiting_on"] == [parent["task_id"]]
    assert reconciled["block_code"] is None
    assert reconciled["block_retryable"] is False
    assert reconciled["block_reason"] is None
    assert reconciled["pause_reason"] is None
    assert reconciled["active_hop_id"] == preserved_hop["hop_id"]
    assert reconciled["hops"][0] == preserved_hop


@pytest.mark.parametrize("status", ["RUNNING", "BLOCKED"])
def test_worker_advance_converges_invalid_executable_state_before_browser_access(
    tmp_path: Path,
    status: str,
):
    store, config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    forced = force_state(store, child, status)
    preserved_hop = copy.deepcopy(forced["hops"][0])

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            forced["manifest_path"],
            ExplodingBrowserContext(),
            scheduling_tasks=[parent, forced],
        )
    )

    assert result is not None
    assert result["status"] == "WAITING"
    assert result["waiting_code"] == "dependency"
    assert result["hops"][0] == preserved_hop


def test_resume_with_unfinished_parent_applies_waiting_postcondition_without_browser_access(
    tmp_path: Path,
):
    store, config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    paused = force_state(store, child, "PAUSED")
    preserved_hop = copy.deepcopy(paused["hops"][0])
    requested = store.request_control(
        paused["manifest_path"], "resume", reason="operator force resume"
    )

    result = asyncio.run(
        CDPAWorker(config, store=store).advance(
            requested["manifest_path"],
            ExplodingBrowserContext(),
            scheduling_tasks=[parent, requested],
        )
    )

    assert result is not None
    assert result["status"] == "WAITING"
    assert result["waiting_code"] == "dependency"
    assert result["active_hop_id"] == preserved_hop["hop_id"]
    assert result["hops"][0] == preserved_hop
    control = result["controls"][-1]
    assert control["status"] == "applied"
    assert control["command_state"] == "APPLIED"
    assert control["result"]["reason_code"] == "dependency_barrier"
    assert control["result"]["postcondition"] == "waiting_dependency"


def test_dependency_readiness_preserves_explicit_missing_stopped_and_done_states(
    tmp_path: Path,
):
    store, _config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )

    missing, changed = store.refresh_scheduling(
        child["manifest_path"], tasks=[child], state=child
    )
    assert changed is True
    assert missing["status"] == "WAITING"
    assert missing["waiting_code"] == "dependency_missing"
    assert missing["waiting"]["missing"] == [parent["task_id"]]

    stopped_parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "kanban_column": "DONE_STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": state["updated_at"],
        },
    )
    stopped, changed = store.refresh_scheduling(
        child["manifest_path"], tasks=[stopped_parent, missing], state=missing
    )
    assert changed is True
    assert stopped["waiting_code"] == "dependency_stopped"
    assert stopped["waiting"]["stopped"] == [parent["task_id"]]

    done_parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "kanban_column": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": None,
            "completed_at": state["updated_at"],
        },
    )
    released, changed = store.refresh_scheduling(
        child["manifest_path"], tasks=[done_parent, stopped], state=stopped
    )
    assert changed is True
    assert released["status"] == "INBOX"
    assert released["waiting_code"] is None


def test_accepted_waiting_hop_survives_dependency_barrier_and_releases_without_resend(
    tmp_path: Path,
):
    store, _config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )

    def accepted(current: dict) -> dict:
        hop = current["hops"][0]
        hop["state"] = "waiting"
        hop["receipt"] = {
            "request_id": hop["request_id"],
            "conversation_id": "conversation-preserved",
            "user_message_id": "message-preserved",
            "accepted_at": "2026-08-05T18:20:00+00:00",
        }
        hop["message_identity"] = {
            "conversation_id": "conversation-preserved",
            "user_message_id": "message-preserved",
        }
        hop["attempts"] = 2
        hop["response"] = {"partial": "evidence-preserved"}
        current["status"] = "RUNNING"
        current["kanban_column"] = "PLANNING"
        current["active_action"] = "observe_response"
        return current

    accepted_child = store.update(child["manifest_path"], accepted)
    preserved = copy.deepcopy(accepted_child["hops"][0])
    waiting, changed = store.refresh_scheduling(
        child["manifest_path"],
        tasks=[parent, accepted_child],
        state=accepted_child,
    )
    assert changed is True
    assert waiting["status"] == "WAITING"
    assert waiting["hops"][0] == preserved

    done_parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "kanban_column": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": state["updated_at"],
        },
    )
    released, changed = store.refresh_scheduling(
        child["manifest_path"],
        tasks=[done_parent, waiting],
        state=waiting,
    )
    assert changed is True
    assert released["status"] == "RUNNING"
    assert released["active_action"] == "observe_response"
    assert released["hops"][0] == preserved


def test_runtime_hydration_reconciles_running_child_before_projection(
    tmp_path: Path,
):
    store, config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    running = force_state(store, child, "RUNNING")

    worker = CDPAWorker(config, store=store)
    catalog = worker.hydrate_runtime()

    assert catalog["complete"] is True
    persisted = store.load_task_id(running["task_id"])
    projected = worker.runtime_db.get_task_detail(running["task_id"])
    assert persisted["status"] == "WAITING"
    assert persisted["waiting_code"] == "dependency"
    assert projected["status"] == "WAITING"
    assert projected["depends_on_task_ids"] == [parent["task_id"]]


def test_parent_removal_command_enforces_version_and_updates_projection_immediately(
    tmp_path: Path,
):
    store, config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    version = worker.runtime_db.get_task_version(child["task_id"])
    assert version is not None

    worker.runtime_db.enqueue_command(
        command_id="cmd-remove-stale",
        idempotency_key="remove-stale",
        kind="remove_parent_dependency",
        task_id=child["task_id"],
        expected_task_version=version + 1,
        payload={"parent_task_id": parent["task_id"]},
    )
    stale = worker.dispatch_command_once()
    assert stale["status"] == "failed"
    assert "stale task version" in stale["error"]
    assert store.load_task_id(child["task_id"])["depends_on_task_ids"] == [
        parent["task_id"]
    ]

    worker.runtime_db.enqueue_command(
        command_id="cmd-remove-current",
        idempotency_key="remove-current",
        kind="remove_parent_dependency",
        task_id=child["task_id"],
        expected_task_version=version,
        payload={"parent_task_id": parent["task_id"]},
    )
    applied = worker.dispatch_command_once()
    assert applied["status"] == "applied"
    projected = worker.runtime_db.get_task_detail(child["task_id"])
    assert projected["depends_on_task_ids"] == []
    assert projected["status"] == "INBOX"
    assert projected["version"] > version

    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL "
            "WHERE command_id = ?",
            ("cmd-remove-current",),
        )
    replayed = worker.dispatch_command_once()
    assert replayed["status"] == "applied"
    assert replayed["result"]["reconciled"] is True
    assert sum(
        event.get("external_command_id") == "cmd-remove-current"
        for event in store.load_task_id(child["task_id"])["dependency_events"]
    ) == 1


def test_parent_removal_is_exact_idempotent_and_reconciles_remaining_dependencies(
    tmp_path: Path,
):
    store, _config = make_store(tmp_path)
    first = store.create_task("First", requested_team="first", task_id="parent-first")
    second = store.create_task("Second", requested_team="second", task_id="parent-second")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(first["task_id"], second["task_id"]),
    )
    preserved_hop = copy.deepcopy(child["hops"][0])

    one_left = store.remove_parent_dependency(
        child["manifest_path"],
        first["task_id"],
        external_command_id="cmd-remove-first",
    )
    assert one_left["depends_on_task_ids"] == [second["task_id"]]
    assert one_left["status"] == "WAITING"
    assert one_left["waiting"]["waiting_on"] == [second["task_id"]]
    assert one_left["hops"][0] == preserved_hop
    event = next(
        item
        for item in one_left["dependency_events"]
        if item.get("external_command_id") == "cmd-remove-first"
    )
    assert event["status"] == "PARENT_REMOVED"
    assert event["parent_task_id"] == first["task_id"]
    assert event["remaining_parent_task_ids"] == [second["task_id"]]
    assert event["external_command_id"] == "cmd-remove-first"

    replay = store.remove_parent_dependency(
        child["manifest_path"],
        first["task_id"],
        external_command_id="cmd-remove-first",
    )
    assert replay == one_left
    assert sum(
        item.get("external_command_id") == "cmd-remove-first"
        for item in replay["dependency_events"]
    ) == 1

    released = store.remove_parent_dependency(
        child["manifest_path"],
        second["task_id"],
        external_command_id="cmd-remove-second",
    )
    assert released["depends_on_task_ids"] == []
    assert released["status"] == "INBOX"
    assert released["active_hop_id"] == preserved_hop["hop_id"]
    assert released["hops"][0] == preserved_hop


@pytest.mark.parametrize("parent_task_id", ["not-a-parent", ""])
def test_parent_removal_rejects_invalid_exact_parent(tmp_path: Path, parent_task_id: str):
    store, _config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )

    with pytest.raises(ValueError):
        store.remove_parent_dependency(child["manifest_path"], parent_task_id)


def test_parent_removal_rejects_terminal_child(tmp_path: Path):
    store, _config = make_store(tmp_path)
    parent = store.create_task("Parent", requested_team="parent", task_id="parent-task")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="child-task",
        depends_on_task_ids=(parent["task_id"],),
    )
    terminal = store.update(
        child["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "kanban_column": "DONE_STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": state["updated_at"],
        },
    )

    with pytest.raises(ValueError, match="terminal"):
        store.remove_parent_dependency(terminal["manifest_path"], parent["task_id"])
