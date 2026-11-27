"""Restart must wait for the existing Stop owner to release every writer."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from test_cdpa_stop_writer_cleanup import StopActions, owned_binding, stop
from test_cdpa_worker import setup_task


REASON = "Continue only after the exact Stop writer release"


def enqueue_restart(worker, state, key="restart-release", *, version="current", **payload):
    if version == "current":
        version = worker.runtime_db.get_task_version(state["task_id"])
    return worker.runtime_db.enqueue_command(
        command_id=key, idempotency_key=key, kind="task_control",
        task_id=state["task_id"], expected_task_version=version,
        payload={"action": "restart_team", "confirmed": True, "reason": REASON, **payload},
    )


def successors(store, state):
    return [item for item in store.discover() if item.get("replaces_task_id") == state["task_id"]]


def pending_stop(tmp_path, *, restart_first=False):
    config, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    writers, _ = owned_binding(worker, state, "PLAN", provisional=True)
    lease = writers.acquire("PLAN")
    attempt, _ = writers.record_send_intent(
        lease, idempotency_key="accepted-before-stop", text="already accepted",
        baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    writers.transition_attempt(attempt["attempt_id"], "accepted")
    evidence = copy.deepcopy(writers.db.get_command(attempt["attempt_id"]))
    actions = StopActions(missing=True)
    command = None
    if restart_first:
        state.update(status="BLOCKED", kanban_column="BLOCKED", block_code="test")
        state = store.save(path, state)
        worker.hydrate_runtime()
        command = enqueue_restart(worker, state)
        assert worker.dispatch_command_once()["status"] == "running"
        requested = store.load(path)
        assert requested["controls"][-1]["action"] == "stop"
        asyncio.run(worker._apply_control(requested, actions, path))
        state = store.load(path)
    else:
        state = stop(store, state, worker, actions)
        worker.hydrate_runtime()
    assert state["status"] == "STOPPED"
    assert state["cleanup"]["state"] == "CLEARING"
    assert state["cleanup"]["phase"] == "writer_stop_pending"
    assert state["controls"][-1]["status"] == "applied"
    assert state["controls"][-1]["result"]["writer_cleanup_pending"] is True
    assert not successors(store, state)
    return config, store, state, worker, writers, actions, attempt["attempt_id"], evidence, command


def release(store, state, worker, actions):
    actions.missing = False
    current = store.load(state["manifest_path"])
    assert asyncio.run(worker._continue_cleanup(
        current, actions, Path(state["manifest_path"]), force_retry=True,
    ))
    result = store.load(state["manifest_path"])
    assert result["cleanup"]["state"] == "STOP_RELEASED"
    assert result["cleanup"]["phase"] == "writer_released"
    assert result["controls"][-1]["result"]["writer_cleanup_pending"] is False
    return result


@pytest.mark.parametrize("restart_team", [False, True])
def test_store_refuses_to_make_pending_stop_parent_immutable(tmp_path, restart_team):
    _, store, state, _, writers, _, attempt_id, evidence, _ = pending_stop(tmp_path)
    path = Path(state["manifest_path"])
    before = path.read_bytes()
    catalog_before = store.catalog_path.read_bytes()
    with pytest.raises(ValueError, match="cleanup|writer"):
        store.replace_task_and_rewire(
            state["task_id"], "Continue after release", reuse_team=False,
            rewire_children=True, incident_id="pending-stop-replacement",
            restart_team=restart_team,
        )
    assert path.read_bytes() == before
    assert store.catalog_path.read_bytes() == catalog_before
    assert not successors(store, state)
    assert writers.db.get_command(attempt_id) == evidence
    assert writers.get_binding("PLAN")["binding_status"] != "sealed"


@pytest.mark.parametrize("entrypoint", ["already_stopped", "restart_then_stop"])
def test_restart_waits_for_stop_cleanup_and_creates_exactly_one_successor(tmp_path, entrypoint):
    _, store, state, worker, writers, actions, attempt_id, evidence, command = pending_stop(
        tmp_path, restart_first=entrypoint == "restart_then_stop",
    )
    original_hops = copy.deepcopy(state["hops"])
    stop_count = len([c for c in state["controls"] if c["action"] == "stop"])
    if command is None:
        command = enqueue_restart(worker, state)
        result = worker.dispatch_command_once()
    else:
        worker._sync_control_commands(state)
        result = worker.runtime_db.get_command(command["command_id"])
    assert result["status"] == "running", result
    assert not successors(store, state)
    assert writers.get_binding("PLAN")["binding_status"] != "sealed"
    pending = store.load(state["manifest_path"])
    assert pending["cleanup"]["phase"] == "writer_stop_pending"
    assert pending["hops"] == original_hops
    assert len([c for c in pending["controls"] if c["action"] == "stop"]) == stop_count

    released = release(store, state, worker, actions)
    worker._sync_control_commands(released)
    result = worker.runtime_db.get_command(command["command_id"])
    assert result["status"] == "applied", result
    children = successors(store, state)
    assert len(children) == 1
    assert result["result"]["task_id"] == children[0]["task_id"]
    assert children[0]["replacement_incident_id"] == f"restart-team:{state['task_id']}"
    assert store.load(state["manifest_path"])["hops"] == original_hops
    assert writers.get_binding("PLAN")["binding_status"] == "sealed"
    assert writers.db.get_command(attempt_id) == evidence
    assert actions.closed_teams == 0

    frozen_parent = Path(state["manifest_path"]).read_bytes()
    same = enqueue_restart(worker, state, command["command_id"],
                           version=command["expected_task_version"])
    assert same["status"] == "applied"
    # A second operator key must resolve the already-created replacement.
    second = enqueue_restart(worker, state, "restart-retry-new-key", version=None)
    result = worker.dispatch_command_once()
    assert result["command_id"] == second["command_id"]
    assert result["status"] == "applied"
    assert result["result"]["task_id"] == children[0]["task_id"]
    assert len(successors(store, state)) == 1
    assert Path(state["manifest_path"]).read_bytes() == frozen_parent


@pytest.mark.parametrize("cleanup_before_reload", [False, True])
def test_direct_pending_restart_survives_reload_and_release_save_gap(tmp_path, cleanup_before_reload):
    config, store, state, worker, writers, actions, attempt_id, evidence, _ = pending_stop(tmp_path)
    original_stop = copy.deepcopy(state["controls"][-1])
    command = enqueue_restart(worker, state)
    assert worker.dispatch_command_once()["status"] == "running"
    saved = store.load(state["manifest_path"])
    assert len(saved["controls"]) == len(state["controls"])
    assert saved["controls"][-1]["reason"] == original_stop["reason"]
    assert saved["controls"][-1]["confirmed"] == original_stop["confirmed"]
    assert command["command_id"] in saved["applied_command_ids"]
    if cleanup_before_reload:
        saved = release(store, state, worker, actions)

    restarted_store = TaskStore(config)
    restarted = CDPAWorker(config, store=restarted_store)
    restarted.hydrate_runtime()
    assert restarted.runtime_db.get_task_version(state["task_id"]) != command["expected_task_version"]
    restarted.runtime_db.requeue_running_commands()
    result = restarted.dispatch_command_once()
    if not cleanup_before_reload:
        assert result["status"] == "running", result
        assert not successors(restarted_store, state)
        saved = release(restarted_store, state, restarted, actions)
        restarted._sync_control_commands(saved)
        result = restarted.runtime_db.get_command(command["command_id"])
    assert result["status"] == "applied", result
    assert len(successors(restarted_store, state)) == 1
    assert writers.db.get_command(attempt_id) == evidence
    assert writers.get_binding("PLAN")["binding_status"] == "sealed"


def test_verified_stop_release_keeps_existing_direct_restart_path(tmp_path):
    _, store, state, worker, writers, actions, attempt_id, evidence, _ = pending_stop(tmp_path)
    released = release(store, state, worker, actions)
    worker._publish_command_state(released)
    command = enqueue_restart(worker, released)
    result = worker.dispatch_command_once()
    assert result["status"] == "applied", result
    assert result["command_id"] == command["command_id"]
    assert len(successors(store, state)) == 1
    assert writers.db.get_command(attempt_id) == evidence
    assert writers.get_binding("PLAN")["binding_status"] == "sealed"


def test_stale_initial_restart_never_links_or_creates_successor(tmp_path):
    _, store, state, worker, writers, _, attempt_id, evidence, _ = pending_stop(tmp_path)
    before = Path(state["manifest_path"]).read_bytes()
    version = worker.runtime_db.get_task_version(state["task_id"])
    enqueue_restart(worker, state, version=version - 1)
    result = worker.dispatch_command_once()
    assert result["status"] == "failed"
    assert "stale task version" in result["error"]
    assert Path(state["manifest_path"]).read_bytes() == before
    assert not successors(store, state)
    assert writers.db.get_command(attempt_id) == evidence


def test_unlinked_command_cannot_bypass_version_after_claim_crash(tmp_path):
    _, store, state, worker, _, actions, _, _, _ = pending_stop(tmp_path)
    command = enqueue_restart(worker, state)
    assert worker.runtime_db.claim_next_command()["command_id"] == command["command_id"]
    # The process died before admission/manifest linkage. Cleanup independently retries.
    current = store.load(state["manifest_path"])
    asyncio.run(worker._continue_cleanup(
        current, actions, Path(state["manifest_path"]), force_retry=True,
    ))
    worker._publish_command_state(store.load(state["manifest_path"]))
    before = Path(state["manifest_path"]).read_bytes()
    worker.runtime_db.requeue_running_commands()
    result = worker.dispatch_command_once()
    assert result["status"] == "failed"
    assert "stale task version" in result["error"]
    assert Path(state["manifest_path"]).read_bytes() == before
    assert not successors(store, state)


@pytest.mark.parametrize("change", ["reason", "confirmation", "role", "stop_identity", "cleanup_phase"])
def test_pending_restart_replay_requires_exact_admitted_provenance(tmp_path, change):
    _, store, state, worker, _, _, _, _, _ = pending_stop(tmp_path)
    command = enqueue_restart(worker, state)
    assert worker.dispatch_command_once()["status"] == "running"
    replay = copy.deepcopy(worker.runtime_db.get_command(command["command_id"]))
    if change == "reason":
        replay["payload"]["reason"] = "Different authorization"
    elif change == "confirmation":
        replay["payload"]["confirmed"] = False
    elif change == "role":
        replay["payload"]["role"] = "DEV"
    elif change == "stop_identity":
        store.update(state["manifest_path"], lambda current: (
            current["cleanup"].update(stop_control_id=999) or current
        ))
    else:
        def invalidate_release(current):
            current["cleanup"].update(state="ACTIVE", phase=None)
            return current
        store.update(state["manifest_path"], invalidate_release)
    with pytest.raises(RuntimeError, match="provenance|Stop|restart|cleanup"):
        worker._command_replay_state(replay)
    assert not successors(store, state)


def test_different_unlinked_restart_key_does_not_inherit_admitted_version(tmp_path):
    _, store, state, worker, _, _, _, _, _ = pending_stop(tmp_path)
    admitted = enqueue_restart(worker, state)
    assert worker.dispatch_command_once()["status"] == "running"
    before = Path(state["manifest_path"]).read_bytes()
    enqueue_restart(worker, state, "foreign-restart", version=admitted["expected_task_version"])
    result = worker.dispatch_command_once()
    assert result["status"] == "failed"
    assert "stale task version" in result["error"]
    assert Path(state["manifest_path"]).read_bytes() == before
    assert not successors(store, state)


def test_pending_restart_with_missing_stop_identity_fails_closed(tmp_path):
    _, store, state, worker, _, _, _, _, _ = pending_stop(tmp_path)
    broken = store.update(state["manifest_path"], lambda current: (
        current["cleanup"].update(stop_control_id=999) or current
    ))
    worker._publish_command_state(broken)
    before = Path(state["manifest_path"]).read_bytes()
    enqueue_restart(worker, broken)
    result = worker.dispatch_command_once()
    assert result["status"] in {"failed", "recovery_required"}, result
    assert "Stop" in result["error"] or "cleanup" in result["error"]
    assert Path(state["manifest_path"]).read_bytes() == before
    assert not successors(store, state)


def test_restart_completion_rechecks_waiter_provenance_after_release(tmp_path):
    _, store, state, worker, writers, actions, _, _, _ = pending_stop(tmp_path)
    command = enqueue_restart(worker, state)
    assert worker.dispatch_command_once()["status"] == "running"
    released = release(store, state, worker, actions)

    def change_saved_waiter(current):
        stop_control = next(
            c for c in current["controls"]
            if c["control_id"] == current["cleanup"]["stop_control_id"]
        )
        waiter = next(
            c for c in stop_control["external_commands"]
            if c["command_id"] == command["command_id"]
        )
        waiter["reason"] = "Changed authority after release"
        return current

    changed = store.update(released["manifest_path"], change_saved_waiter)
    worker._sync_control_commands(changed)
    result = worker.runtime_db.get_command(command["command_id"])
    assert result["status"] == "recovery_required", result
    assert "provenance" in result["error"]
    assert not successors(store, state)
    assert writers.get_binding("PLAN")["binding_status"] == "sealed"


def test_direct_restart_waits_for_existing_stop_at_boundary_without_new_stop(tmp_path):
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, "PLAN", provisional=True)
    path = Path(state["manifest_path"])
    requested = store.request_control(path, "stop_at_boundary", reason="finish safely")
    actions = StopActions(missing=True)
    asyncio.run(worker._apply_control(requested, actions, path))
    stopped = store.load(path)
    assert stopped["status"] == "STOPPED"
    assert stopped["cleanup"]["phase"] == "writer_stop_pending"
    worker.hydrate_runtime()
    command = enqueue_restart(worker, stopped)
    assert worker.dispatch_command_once()["status"] == "running"
    worker.runtime_db.requeue_running_commands()
    assert worker.dispatch_command_once()["status"] == "running"
    assert len(store.load(path)["controls"]) == 1
    released = release(store, stopped, worker, actions)
    worker._sync_control_commands(released)
    result = worker.runtime_db.get_command(command["command_id"])
    assert result["status"] == "applied", result
    assert len(successors(store, state)) == 1
    assert writers.get_binding("PLAN")["binding_status"] == "sealed"
    assert actions.stop_calls == 0
