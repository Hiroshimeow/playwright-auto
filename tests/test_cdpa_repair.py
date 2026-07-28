from __future__ import annotations

import asyncio
import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_commands import RepairRequest, WorkerCommand
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import _active_hop

from test_cdpa_core import write_config
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


@pytest.mark.parametrize("disposition", ["CONTINUE_IN_PARALLEL", "HOLD_FOR_REPAIR"])
def test_cross_repository_repair_is_created_in_control_repository_with_provenance(
    tmp_path: Path,
    disposition: str,
):
    control_root = tmp_path / "control"
    product_root = tmp_path / "product"
    control_root.mkdir()
    product_root.mkdir()
    config = load_cdpa_config(write_config(control_root), repository_root=control_root)
    store = TaskStore(config)
    state = store.create_task(
        "Cross-repository affected task",
        requested_team="product-task",
        task_id=f"task-cross-repair-{disposition.lower()}",
        repository=product_root,
    )
    state = block_task(store, state, hop_state="waiting")
    request = RepairRequest.create(
        root_cause="cross-repository-route-report-recovery",
        affected_state=state,
        repair_repository=control_root,
        incident_id=f"incident-{disposition.lower()}",
        disposition=disposition,
        reason="repair the CDPA runtime without moving the product task",
        reproduction="A valid file-mode route names a report that the role cannot materialize.",
        source_areas=("cdpa_worker", "cdpa_store", "tests"),
        required_tests=("cross-repository repair provenance",),
        lesson=None,
    )

    result = store.create_or_gate_repair(request)
    repair = result["repair"]
    affected = result["affected"]
    operation = repair["repair"]["affected_operations"][-1]
    link = affected["repair_links"][-1]

    assert request.repository == str(product_root.resolve())
    assert request.repair_repository == str(control_root.resolve())
    assert repair["repository"] == str(control_root.resolve())
    assert repair["repair"]["repository"] == str(product_root.resolve())
    assert repair["repair"]["repair_repository"] == str(control_root.resolve())
    assert operation["affected_repository"] == str(product_root.resolve())
    assert operation["repair_repository"] == str(control_root.resolve())
    assert link["affected_repository"] == str(product_root.resolve())
    assert link["repair_repository"] == str(control_root.resolve())
    if disposition == "HOLD_FOR_REPAIR":
        assert affected["status"] == "WAITING"
        assert affected["depends_on_task_ids"] == [repair["task_id"]]
    else:
        assert affected["status"] == "BLOCKED"
        assert affected["depends_on_task_ids"] == []

    repeated = store.create_or_gate_repair(request)
    assert repeated["repair"]["task_id"] == repair["task_id"]


def test_cross_repository_repair_cannot_select_repository_outside_allowlist(
    tmp_path: Path,
):
    control_root = tmp_path / "control"
    product_root = tmp_path / "product"
    control_root.mkdir()
    product_root.mkdir()
    config = load_cdpa_config(write_config(control_root), repository_root=control_root)
    store = TaskStore(config)
    state = store.create_task(
        "Cross-repository allowlist target",
        requested_team="allowlist-target",
        task_id="task-cross-repair-allowlist",
        repository=product_root,
    )
    state = block_task(store, state, hop_state="waiting")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-repair"
    request = RepairRequest.create(
        root_cause="cross-repository-repair-allowlist",
        affected_state=state,
        repair_repository=str(outside),
        incident_id="incident-cross-repair-allowlist",
        disposition="CONTINUE_IN_PARALLEL",
        reason="prove repair repository selection remains bounded",
        reproduction="Attempt to place the repair task outside repositories.allowed_roots.",
        source_areas=("cdpa_store", "tests"),
        required_tests=("reject out-of-allowlist repair repository",),
        lesson=None,
    )

    with pytest.raises(ValueError, match="outside repositories.allowed_roots"):
        store.create_or_gate_repair(request)

    assert not outside.exists()
    assert not any(
        isinstance(item.get("repair"), dict) for item in store.discover()
    )


@pytest.mark.parametrize(
    "action",
    ["pause", "stop", "clear_team", "restart_role", "new_chat"],
)
def test_repair_creation_rejects_pending_operator_lifecycle_control(
    tmp_path: Path,
    action: str,
):
    _config, store, state, _worker = setup_task(
        tmp_path,
        task_id=f"task-pending-operator-{action}",
    )
    state = block_task(store, state, hop_state="waiting")
    request = repair_request(state, "HOLD_FOR_REPAIR")
    role = "PLAN" if action in {"restart_role", "new_chat"} else None
    state = store.request_control(
        state["manifest_path"],
        action,
        role=role,
        confirmed=action == "clear_team",
    )

    with pytest.raises(ValueError, match="pending operator lifecycle control"):
        store.create_or_gate_repair(request)

    unchanged = store.load(state["manifest_path"])
    assert unchanged["status"] == "BLOCKED"
    assert unchanged.get("repair_links") is None
    assert unchanged["depends_on_task_ids"] == []
    assert unchanged["controls"][-1]["status"] == "requested"
    assert not any(
        isinstance(item.get("repair"), dict) for item in store.discover()
    )


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
            origin="independent_agent",
            source_task_id="agent-maintainers-g1",
            source_event_key="recovery:task-wrong-tab:role-offline",
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
