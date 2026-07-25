from __future__ import annotations

import asyncio
import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_commands import RepairRequest, WorkerCommand
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import _active_hop

from test_cdpa_worker import setup_task


def block_task(store: TaskStore, state: dict, *, hop_state: str = "pre_send") -> dict:
    path = Path(state["manifest_path"])

    def mutate(current: dict) -> dict:
        hop = _active_hop(current)
        hop["state"] = hop_state
        if hop_state == "waiting":
            hop["conversation_url"] = "https://chatgpt.com/c/exact"
            hop["receipt"] = {
                "user_turn_id": "turn-1",
                "binding": {
                    "page_id": "page-1",
                    "role": current["roles"]["PLAN"]["physical_role"],
                },
            }
            current["roles"]["PLAN"].update(
                page_id="page-1",
                page_url="https://chatgpt.com/c/exact",
                online=False,
            )
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            active_action="blocked",
            block_code="role_offline",
            block_reason="recorded role tab is offline",
            block_retryable=False,
        )
        return current

    return store.update(path, mutate)


def repair_request(state: dict, disposition: str) -> RepairRequest:
    return RepairRequest.create(
        root_cause="role-offline-open-tab-postcondition",
        affected_state=state,
        incident_id="maint-repair-1",
        disposition=disposition,
        reason="prevent repeated false-positive role recovery",
        reproduction="OPEN_ROLE_TAB must preserve the active hop and prove recovery.",
        source_areas=("cdpa_worker", "cdpa_store", "tests", "prompts"),
        required_tests=("role-offline focused regression", "full CDPA suite"),
        lesson="Record recovery as applied only after the matching operational block clears.",
    )


def test_hold_for_repair_atomically_gates_same_existing_task(tmp_path: Path):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-held")
    state = block_task(store, state, hop_state="waiting")
    before_hop = json.loads(json.dumps(_active_hop(state)))
    before_reports = json.loads(json.dumps(state["reports"]))
    request = repair_request(state, "HOLD_FOR_REPAIR")

    result = store.create_or_gate_repair(request)
    affected = store.load(state["manifest_path"])
    repair = result["repair"]

    assert affected["task_id"] == "task-held"
    assert affected["team"] == state["team"]
    assert affected["status"] == "WAITING"
    assert affected["active_hop_id"] == state["active_hop_id"]
    assert _active_hop(affected) == before_hop
    assert affected["reports"] == before_reports
    assert affected["depends_on_task_ids"][-1] == repair["task_id"]
    assert affected["repair_wait"]["disposition"] == "HOLD_FOR_REPAIR"
    assert affected["repair_wait"]["repair_task_id"] == repair["task_id"]
    assert repair["repair"]["priority"] == "urgent"
    assert repair["repair"]["root_cause_key"] == request.root_cause_key

    repeated = store.create_or_gate_repair(request)
    assert repeated["repair"]["task_id"] == repair["task_id"]
    assert store.load(state["manifest_path"])["depends_on_task_ids"].count(
        repair["task_id"]
    ) == 1


def test_continue_in_parallel_creates_repair_without_dependency_gate(tmp_path: Path):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-parallel")
    state = block_task(store, state)
    request = repair_request(state, "CONTINUE_IN_PARALLEL")

    result = store.create_or_gate_repair(request)
    affected = store.load(state["manifest_path"])

    assert result["repair"]["task_id"] not in affected["depends_on_task_ids"]
    assert affected.get("repair_wait") is None
    assert affected["status"] == "BLOCKED"
    assert affected["repair_links"][-1]["disposition"] == "CONTINUE_IN_PARALLEL"


def test_repair_done_releases_same_waiting_hop_without_resume_or_resend(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-release")
    state = block_task(store, state, hop_state="waiting")
    before = json.loads(json.dumps(_active_hop(state)))
    result = store.create_or_gate_repair(repair_request(state, "HOLD_FOR_REPAIR"))
    repair_path = Path(result["repair"]["manifest_path"])

    def finish(current: dict) -> dict:
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE_STOPPED",
            active_action="done",
            active_role=None,
            active_hop_id=None,
        )
        return current

    store.update(repair_path, finish)
    tasks = store.discover_with_errors()[0]
    affected, changed = store.refresh_scheduling(state["manifest_path"], tasks=tasks)

    assert changed is True
    assert affected["status"] == "RUNNING"
    assert affected["active_hop_id"] == before["hop_id"]
    assert _active_hop(affected) == before
    assert result["repair"]["task_id"] not in affected["depends_on_task_ids"]
    assert affected["repair_wait"]["state"] == "RELEASED"
    assert not any(control["action"] == "resume" for control in affected["controls"])


def test_open_tab_wrong_conversation_is_ineffective_and_keeps_block(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-wrong-tab")
    state = block_task(store, state)
    path = Path(state["manifest_path"])

    def record_exact_conversation(current: dict) -> dict:
        current["roles"]["PLAN"].update(
            page_id="page-exact",
            page_url="https://chatgpt.com/c/exact",
            online=False,
        )
        return current

    state = store.update(path, record_exact_conversation)

    def queue(current: dict) -> dict:
        store._queue_control(
            current,
            "open_tab",
            role="PLAN",
            reason="reopen exact role",
            origin="maintainers",
            maintenance_incident_id="maint-1",
            maintenance_request_id="maint-1-turn1",
        )
        return current

    state = store.update(path, queue)

    class WrongConversationActions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, **_kwargs):
            return SimpleNamespace(
                page_id="page-wrong",
                url="https://chatgpt.com/c/wrong",
                created=True,
                new_chat=False,
            )

    assert asyncio.run(worker._apply_control(state, WrongConversationActions())) is True
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "role_offline"
    assert state["controls"][-1]["status"] == "ineffective"
    assert "conversation" in str(state["controls"][-1]["result"]).lower()


def test_urgent_repair_waiter_precedes_ordinary_ready_waiter():
    from playwright_auto.cdpa_team import exact_team_ready_waiters

    ordinary = {
        "task_id": "ordinary",
        "team": "shared-team",
        "status": "WAITING",
        "created_at": "2026-07-25T00:00:00+00:00",
        "depends_on_task_ids": [],
        "queue": {"reuse_team": True, "released_at": None},
    }
    repair = {
        "task_id": "repair",
        "team": "shared-team",
        "status": "WAITING",
        "priority": "urgent_repair",
        "created_at": "2026-07-25T00:01:00+00:00",
        "depends_on_task_ids": [],
        "queue": {"reuse_team": True, "released_at": None},
    }

    ordered = exact_team_ready_waiters([ordinary, repair], "shared-team")
    assert [item["task_id"] for item in ordered] == ["repair", "ordinary"]


def test_worker_executes_validated_hold_repair_and_coordinator_resolves_incident(tmp_path: Path):
    from datetime import datetime, timezone

    from playwright_auto.cdpa_maintenance import (
        MaintainerCoordinator,
        MaintenanceDecision,
        ensure_maintenance_incident,
    )

    _config, store, state, worker = setup_task(tmp_path, task_id="task-repair-command")
    state = block_task(store, state)
    path = Path(state["manifest_path"])
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    incident["turn"] = 1
    incident["request_id"] = f"{incident['incident_id']}-turn1"
    state = store.save_maintenance(path, state)
    coordinator = MaintainerCoordinator(_config, store=store)
    decision = MaintenanceDecision(
        action="CREATE_REPAIR_TASK",
        reason="Prevent recurrence before resuming.",
        lesson="Recovery is applied only after its operational postcondition passes.",
        repair={
            "root_cause": "role-offline-open-tab-postcondition",
            "reason": "Prevent a false-positive exact-tab recovery.",
            "disposition": "HOLD_FOR_REPAIR",
            "reproduction": "Reopen a mismatched role conversation while blocked.",
            "source_areas": ["cdpa_worker", "cdpa_store", "tests", "prompts"],
            "required_tests": ["focused role-offline regression", "controlled live recovery"],
        },
        version=2,
    )
    committed, committed_incident, _evidence, control, stale = coordinator._commit_response(
        path,
        incident_id=str(incident["incident_id"]),
        expected_incident_key=str(incident["key"]),
        request_id=str(incident["request_id"]),
        turn=1,
        report="# Repair report\n\nCreate the bounded repair task.\n",
        report_at=datetime.now(timezone.utc),
        decision=decision,
    )
    assert stale is False
    assert control["action"] == "create_repair_task"
    assert control["origin"] == "maintainers"

    class NoopActions:
        pass

    persisted: list[bool] = []
    assert asyncio.run(
        worker._apply_control(
            committed,
            NoopActions(),
            manifest_path=path,
            persisted_result=persisted,
        )
    ) is True
    assert persisted == [True]
    held = store.load(path)
    assert held["status"] == "WAITING"
    assert held["controls"][-1]["status"] == "applied"
    repair_task_id = held["controls"][-1]["result"]["repair_task_id"]
    assert repair_task_id in held["depends_on_task_ids"]

    assert coordinator._reconcile_active(path, held) is True
    resolved = store.load(path)
    resolved_incident = resolved["maintenance"]["incidents"][0]
    assert resolved_incident["state"] == "RESOLVED"
    assert resolved_incident["repair_task_id"] == repair_task_id
    assert resolved_incident["pending_lesson"] == decision.lesson


def test_operator_stop_does_not_create_maintenance_incident(tmp_path: Path):
    from playwright_auto.cdpa_maintenance import ensure_maintenance_incident

    _config, store, state, worker = setup_task(tmp_path, task_id="task-operator-stop")
    path = Path(state["manifest_path"])
    state = store.request_control(path, "stop", reason="operator stop")

    class StopActions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def stop_if_active(self, *_args, **_kwargs):
            return False

    assert asyncio.run(worker._apply_control(state, StopActions())) is True
    assert state["status"] == "STOPPED"
    assert state["controls"][-1]["origin"] == "operator"
    assert ensure_maintenance_incident(state) is None


def test_repair_done_releases_same_pre_send_hop_without_new_hop(tmp_path: Path):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-release-pre-send")
    state = block_task(store, state, hop_state="pre_send")
    before = json.loads(json.dumps(_active_hop(state)))
    result = store.create_or_gate_repair(repair_request(state, "HOLD_FOR_REPAIR"))
    repair_path = Path(result["repair"]["manifest_path"])

    def finish(current: dict) -> dict:
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE_STOPPED",
            active_action="done",
            active_role=None,
            active_hop_id=None,
        )
        return current

    store.update(repair_path, finish)
    tasks = store.discover_with_errors()[0]
    affected, changed = store.refresh_scheduling(state["manifest_path"], tasks=tasks)

    assert changed is True
    assert affected["status"] == "RUNNING"
    assert affected["active_hop_id"] == before["hop_id"]
    assert _active_hop(affected) == before
    assert affected["active_action"] == "resume_preserved_hop"
    assert len(affected["hops"]) == len(state["hops"])


def test_repair_transaction_recovers_after_partial_install(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-crash-repair")
    state = block_task(store, state, hop_state="waiting")
    request = repair_request(state, "HOLD_FOR_REPAIR")
    original_write = store._phase4_write_bytes_unlocked
    calls = {"count": 0}

    def flaky_write(path: Path, data: bytes | None) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash between repair writes")
        original_write(path, data)

    monkeypatch.setattr(store, "_phase4_write_bytes_unlocked", flaky_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.create_or_gate_repair(request)
    assert store.repair_journal_path.is_file()

    monkeypatch.setattr(store, "_phase4_write_bytes_unlocked", original_write)
    recovered = store.recover_repair_transaction()
    assert recovered is not None
    assert not store.repair_journal_path.exists()
    affected = store.load(state["manifest_path"])
    repair = recovered["repair"]
    assert affected["status"] == "WAITING"
    assert affected["repair_wait"]["repair_task_id"] == repair["task_id"]
    assert repair["task_id"] in affected["depends_on_task_ids"]
    assert affected["active_hop_id"] == state["active_hop_id"]
    assert _active_hop(affected)["request_id"] == _active_hop(state)["request_id"]


def test_hold_for_repair_rejects_terminal_stopped_task_without_preserved_active_hop(
    tmp_path: Path,
):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-stopped-repair")
    path = Path(state["manifest_path"])

    def stop_non_operator(current: dict) -> dict:
        current.update(
            status="STOPPED",
            terminal_state="STOPPED",
            kanban_column="DONE_STOPPED",
            active_action="stopped",
            active_role=None,
            active_hop_id=None,
            stopped_at="2026-07-25T00:00:00+00:00",
            stop_reason="systemic CDPA defect",
            block_code="unexpected_error",
            block_reason="systemic CDPA defect",
        )
        return current

    state = store.update(path, stop_non_operator)
    with pytest.raises(ValueError, match="requires a non-operator BLOCKED"):
        store.create_or_gate_repair(repair_request(state, "HOLD_FOR_REPAIR"))



def repair_request_for(
    state: dict,
    disposition: str,
    *,
    incident_id: str,
) -> RepairRequest:
    return RepairRequest.create(
        root_cause="role-offline-open-tab-postcondition",
        affected_state=state,
        incident_id=incident_id,
        disposition=disposition,
        reason="prevent repeated false-positive role recovery",
        reproduction="OPEN_ROLE_TAB must preserve the active hop and prove recovery.",
        source_areas=("cdpa_worker", "cdpa_store", "tests", "prompts"),
        required_tests=("role-offline focused regression", "full CDPA suite"),
        lesson="Record recovery as applied only after the matching operational block clears.",
    )


def test_shared_root_cause_reuses_one_repair_and_gates_each_affected_task(tmp_path: Path):
    _config, store, task_a, _worker = setup_task(tmp_path, task_id="task-shared-a")
    task_a = block_task(store, task_a, hop_state="waiting")
    task_b = store.create_task(
        "Second affected task",
        requested_team="beta",
        task_id="task-shared-b",
    )
    task_b = block_task(store, task_b, hop_state="waiting")

    first = store.create_or_gate_repair(
        repair_request_for(task_a, "HOLD_FOR_REPAIR", incident_id="maint-shared-a")
    )
    second = store.create_or_gate_repair(
        repair_request_for(task_b, "HOLD_FOR_REPAIR", incident_id="maint-shared-b")
    )

    assert second["repair"]["task_id"] == first["repair"]["task_id"]
    held_b = store.load(task_b["manifest_path"])
    assert held_b["status"] == "WAITING"
    assert held_b["depends_on_task_ids"] == [first["repair"]["task_id"]]
    assert held_b["repair_wait"]["repair_task_id"] == first["repair"]["task_id"]
    assert held_b["repair_links"][-1]["incident_id"] == "maint-shared-b"
    repair = store.load(first["repair"]["manifest_path"])
    operations = repair["repair"]["affected_operations"]
    assert {(item["affected_task_id"], item["incident_id"]) for item in operations} == {
        ("task-shared-a", "maint-shared-a"),
        ("task-shared-b", "maint-shared-b"),
    }


def test_existing_repair_accepts_durable_parallel_to_hold_disposition_change(tmp_path: Path):
    _config, store, state, _worker = setup_task(tmp_path, task_id="task-disposition")
    state = block_task(store, state, hop_state="waiting")

    first = store.create_or_gate_repair(
        repair_request_for(
            state,
            "CONTINUE_IN_PARALLEL",
            incident_id="maint-disposition-parallel",
        )
    )
    still_blocked = store.load(state["manifest_path"])
    second_request = repair_request_for(
        still_blocked,
        "HOLD_FOR_REPAIR",
        incident_id="maint-disposition-hold",
    )
    second = store.create_or_gate_repair(second_request)
    repeated = store.create_or_gate_repair(second_request)

    assert second["repair"]["task_id"] == first["repair"]["task_id"]
    assert repeated["repair"]["task_id"] == first["repair"]["task_id"]
    held = store.load(state["manifest_path"])
    assert held["status"] == "WAITING"
    assert held["depends_on_task_ids"] == [first["repair"]["task_id"]]
    assert [item["disposition"] for item in held["repair_links"]] == [
        "CONTINUE_IN_PARALLEL",
        "HOLD_FOR_REPAIR",
    ]
    assert held["repair_wait"]["incident_id"] == "maint-disposition-hold"
    repair = store.load(first["repair"]["manifest_path"])
    assert [
        item["disposition"]
        for item in repair["repair"]["affected_operations"]
        if item["affected_task_id"] == state["task_id"]
    ] == ["CONTINUE_IN_PARALLEL", "HOLD_FOR_REPAIR"]


def test_worker_applies_hold_to_existing_deduplicated_repair(tmp_path: Path):
    _config, store, task_a, worker = setup_task(tmp_path, task_id="task-worker-shared-a")
    task_a = block_task(store, task_a, hop_state="waiting")
    repair = store.create_or_gate_repair(
        repair_request_for(task_a, "HOLD_FOR_REPAIR", incident_id="maint-worker-a")
    )["repair"]
    task_b = store.create_task(
        "Worker affected task",
        requested_team="beta",
        task_id="task-worker-shared-b",
    )
    task_b = block_task(store, task_b, hop_state="waiting")
    request_b = repair_request_for(
        task_b,
        "HOLD_FOR_REPAIR",
        incident_id="maint-worker-b",
    )
    path_b = Path(task_b["manifest_path"])

    def queue(current: dict) -> dict:
        store._queue_control(
            current,
            "create_repair_task",
            reason=request_b.reason,
            origin="maintainers",
            maintenance_incident_id=request_b.incident_id,
            maintenance_request_id=f"{request_b.incident_id}-repair",
            repair=request_b,
        )
        return current

    task_b = store.update(path_b, queue)

    class NoopActions:
        pass

    persisted: list[bool] = []
    assert asyncio.run(
        worker._apply_control(
            task_b,
            NoopActions(),
            manifest_path=path_b,
            persisted_result=persisted,
        )
    ) is True
    assert persisted == [True]
    held = store.load(path_b)
    assert held["status"] == "WAITING"
    assert held["depends_on_task_ids"] == [repair["task_id"]]
    assert held["controls"][-1]["status"] == "applied"
    assert held["controls"][-1]["result"]["repair_task_id"] == repair["task_id"]


def test_existing_repair_transaction_recovers_after_partial_attach(
    tmp_path: Path,
    monkeypatch,
):
    _config, store, task_a, _worker = setup_task(tmp_path, task_id="task-existing-crash-a")
    task_a = block_task(store, task_a, hop_state="waiting")
    repair = store.create_or_gate_repair(
        repair_request_for(task_a, "HOLD_FOR_REPAIR", incident_id="maint-existing-a")
    )["repair"]
    task_b = store.create_task(
        "Crash attach task",
        requested_team="beta",
        task_id="task-existing-crash-b",
    )
    task_b = block_task(store, task_b, hop_state="waiting")
    request_b = repair_request_for(
        task_b,
        "HOLD_FOR_REPAIR",
        incident_id="maint-existing-b",
    )
    original_write = store._phase4_write_bytes_unlocked
    calls = {"count": 0}

    def flaky_write(path: Path, data: bytes | None) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash while attaching existing repair")
        original_write(path, data)

    monkeypatch.setattr(store, "_phase4_write_bytes_unlocked", flaky_write)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.create_or_gate_repair(request_b)
    assert store.repair_journal_path.is_file()

    monkeypatch.setattr(store, "_phase4_write_bytes_unlocked", original_write)
    recovered = store.recover_repair_transaction()
    assert recovered is not None
    assert recovered["repair"]["task_id"] == repair["task_id"]
    held_b = store.load(task_b["manifest_path"])
    assert held_b["status"] == "WAITING"
    assert held_b["repair_wait"]["repair_task_id"] == repair["task_id"]
    updated_repair = store.load(repair["manifest_path"])
    assert any(
        item["affected_task_id"] == task_b["task_id"]
        for item in updated_repair["repair"]["affected_operations"]
    )


def test_shared_repair_done_releases_every_held_task(tmp_path: Path):
    _config, store, task_a, _worker = setup_task(tmp_path, task_id="task-release-shared-a")
    task_a = block_task(store, task_a, hop_state="waiting")
    task_b = store.create_task(
        "Release second task",
        requested_team="beta",
        task_id="task-release-shared-b",
    )
    task_b = block_task(store, task_b, hop_state="pre_send")
    before_a = json.loads(json.dumps(_active_hop(task_a)))
    before_b = json.loads(json.dumps(_active_hop(task_b)))
    first = store.create_or_gate_repair(
        repair_request_for(task_a, "HOLD_FOR_REPAIR", incident_id="maint-release-a")
    )
    store.create_or_gate_repair(
        repair_request_for(task_b, "HOLD_FOR_REPAIR", incident_id="maint-release-b")
    )
    repair_path = Path(first["repair"]["manifest_path"])

    def finish(current: dict) -> dict:
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE_STOPPED",
            active_action="done",
            active_role=None,
            active_hop_id=None,
        )
        return current

    store.update(repair_path, finish)
    tasks = store.discover_with_errors()[0]
    released_a, changed_a = store.refresh_scheduling(task_a["manifest_path"], tasks=tasks)
    tasks = store.discover_with_errors()[0]
    released_b, changed_b = store.refresh_scheduling(task_b["manifest_path"], tasks=tasks)

    assert changed_a is True and changed_b is True
    assert released_a["status"] == released_b["status"] == "RUNNING"
    assert _active_hop(released_a) == before_a
    assert _active_hop(released_b) == before_b
    assert released_a["repair_wait"]["state"] == "RELEASED"
    assert released_b["repair_wait"]["state"] == "RELEASED"


def test_worker_replays_hold_after_gate_before_control_result(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-replay-hold")
    state = block_task(store, state, hop_state="waiting")
    request = repair_request_for(
        state,
        "HOLD_FOR_REPAIR",
        incident_id="maint-replay-hold",
    )
    path = Path(state["manifest_path"])

    def queue(current: dict) -> dict:
        store._queue_control(
            current,
            "create_repair_task",
            reason=request.reason,
            origin="maintainers",
            maintenance_incident_id=request.incident_id,
            maintenance_request_id=f"{request.incident_id}-repair",
            repair=request,
        )
        return current

    queued = store.update(path, queue)
    store.create_or_gate_repair(request)
    replay = store.load(path)
    assert replay["status"] == "WAITING"
    assert replay["controls"][-1]["status"] == "requested"

    class NoopActions:
        pass

    persisted: list[bool] = []
    assert asyncio.run(
        worker._apply_control(
            replay,
            NoopActions(),
            manifest_path=path,
            persisted_result=persisted,
        )
    ) is True
    assert persisted == [True]
    applied = store.load(path)
    assert applied["status"] == "WAITING"
    assert applied["controls"][-1]["status"] == "applied"
    assert applied["controls"][-1]["command_state"] == "APPLIED"


def test_worker_applies_durable_hold_to_parallel_disposition_change(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-hold-to-parallel")
    state = block_task(store, state, hop_state="waiting")
    hold = repair_request_for(
        state,
        "HOLD_FOR_REPAIR",
        incident_id="maint-hold-first",
    )
    repair = store.create_or_gate_repair(hold)["repair"]
    held = store.load(state["manifest_path"])
    parallel = repair_request_for(
        held,
        "CONTINUE_IN_PARALLEL",
        incident_id="maint-parallel-second",
    )
    path = Path(held["manifest_path"])

    def queue(current: dict) -> dict:
        store._queue_control(
            current,
            "create_repair_task",
            reason=parallel.reason,
            origin="maintainers",
            maintenance_incident_id=parallel.incident_id,
            maintenance_request_id=f"{parallel.incident_id}-repair",
            repair=parallel,
        )
        return current

    held = store.update(path, queue)

    class NoopActions:
        pass

    persisted: list[bool] = []
    assert asyncio.run(
        worker._apply_control(
            held,
            NoopActions(),
            manifest_path=path,
            persisted_result=persisted,
        )
    ) is True
    assert persisted == [True]
    resumed = store.load(path)
    assert resumed["status"] == "RUNNING"
    assert repair["task_id"] not in resumed["depends_on_task_ids"]
    assert resumed["repair_wait"]["state"] == "DISPOSITION_CHANGED"
    assert resumed["controls"][-1]["status"] == "applied"


def test_done_repair_prevents_new_call_for_same_known_root_cause(tmp_path: Path):
    _config, store, task_a, _worker = setup_task(tmp_path, task_id="task-known-root-a")
    task_a = block_task(store, task_a, hop_state="waiting")
    repair = store.create_or_gate_repair(
        repair_request_for(task_a, "HOLD_FOR_REPAIR", incident_id="maint-known-a")
    )["repair"]
    repair_path = Path(repair["manifest_path"])

    def finish(current: dict) -> dict:
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE_STOPPED",
            active_action="done",
            active_role=None,
            active_hop_id=None,
        )
        return current

    store.update(repair_path, finish)
    task_b = store.create_task(
        "Recurring known root",
        requested_team="beta",
        task_id="task-known-root-b",
    )
    task_b = block_task(store, task_b, hop_state="waiting")

    with pytest.raises(ValueError, match="already resolved"):
        store.create_or_gate_repair(
            repair_request_for(
                task_b,
                "HOLD_FOR_REPAIR",
                incident_id="maint-known-b",
            )
        )
    unchanged = store.load(task_b["manifest_path"])
    assert unchanged["status"] == "BLOCKED"
    assert unchanged.get("repair_links") is None
    assert unchanged["depends_on_task_ids"] == []
