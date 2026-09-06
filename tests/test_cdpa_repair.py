from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from pathlib import Path
from types import SimpleNamespace

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_commands import RepairRequest, WorkerCommand
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import _active_hop, _waiting_working_copy
from playwright_auto.chatgpt import ChatGPTState, MessageSnapshot
from playwright_auto.durable import RequestLedger

from test_cdpa_core import write_config
from test_cdpa_worker import (
    _backend_graph,
    _enable_backend_wait_identity,
    _prepare_sent_waiting_task,
    send_snapshot,
    setup_task,
)


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








def test_repair_gate_preserves_accepted_send_and_prevents_browser_actions(
    tmp_path: Path,
    monkeypatch,
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-held-accepted-send"
    )
    state, hop, receipt = _enable_backend_wait_identity(
        store,
        state,
        path,
        hop,
        receipt,
        conversation_id="conversation-held",
    )
    hop["wait"].update(
        completion_mode="dom_fallback",
        backend_fallback_category="graph_not_ready",
        refresh_count=1,
        activity_signature="semantic-signature",
        activity_length=607,
        terminal_continuation_unresolved={
            "request_id": hop["request_id"],
            "started_at": "2026-08-05T19:00:00+00:00",
            "refresh_baseline": 0,
            "block_ready_at": "2026-08-05T19:00:30+00:00",
        },
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        active_action="blocked",
        block_code="terminal_continuation_unresolved",
        block_reason="terminal continuation did not materialize",
        block_retryable=False,
    )
    state = store.save(path, state)
    before_hop = json.loads(json.dumps(_active_hop(state)))
    before_record = RequestLedger(before_hop["ledger_path"]).get(
        before_hop["request_id"]
    )
    assert before_record is not None
    assert before_record.attempts == 1

    result = store.create_or_gate_repair(
        repair_request(state, "HOLD_FOR_REPAIR")
    )
    held = store.load(path)

    assert held["status"] == "WAITING"
    assert held["repair_wait"]["state"] == "WAITING"
    assert _active_hop(held) == before_hop
    assert held["active_hop_id"] == before_hop["hop_id"]
    assert before_hop["request_id"] == held["repair_wait"]["preserved_request_id"]
    assert before_hop["receipt"]["conversation_id"] == "conversation-held"
    assert before_hop["receipt"]["user_message_id"] == receipt.user_message_id
    assert before_hop["receipt"]["user_turn_id"] == receipt.user_turn_id
    assert before_hop["wait"]["activity_length"] == 607
    assert before_hop["wait"]["terminal_continuation_unresolved"][
        "refresh_baseline"
    ] == 0
    held_record = RequestLedger(before_hop["ledger_path"]).get(before_hop["request_id"])
    assert held_record is not None
    assert held_record.attempts == 1
    assert held_record.receipt == before_record.receipt
    assert not any(
        item.get("action") == "resume" for item in held.get("controls") or []
    )

    def browser_actions_forbidden(*_args, **_kwargs):
        raise AssertionError("repair-held task must not construct browser actions")

    monkeypatch.setattr(worker_module, "CDPATabActions", browser_actions_forbidden)
    observed = asyncio.run(worker.advance(path, object()))
    assert observed is not None
    assert observed["status"] == "WAITING"
    assert _active_hop(observed) == before_hop

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
    released, changed = store.refresh_scheduling(
        path,
        tasks=store.discover_with_errors()[0],
    )

    assert changed is True
    assert released["status"] == "RUNNING"
    assert released["repair_wait"]["state"] == "RELEASED"
    assert _active_hop(released) == before_hop
    released_record = RequestLedger(before_hop["ledger_path"]).get(
        before_hop["request_id"]
    )
    assert released_record is not None
    assert released_record.attempts == 1
    assert released_record.receipt == before_record.receipt
    assert not any(
        item.get("action") == "resume" for item in released.get("controls") or []
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


def _released_terminal_continuation_task(tmp_path: Path, *, task_id: str):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id=task_id
    )
    state, hop, receipt = _enable_backend_wait_identity(
        store,
        state,
        path,
        hop,
        receipt,
        conversation_id=f"conversation-{task_id}",
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    hop["wait"].update(
        completion_mode="dom_fallback",
        backend_fallback_category="graph_not_ready",
        dom_fallback_ready_at=past,
        deadline_at=past,
        refresh_count=1,
        terminal_graph_attempts=3,
        terminal_graph_ready_at=past,
        terminal_continuation_unresolved={
            "request_id": hop["request_id"],
            "started_at": past,
            "refresh_baseline": 0,
            "block_ready_at": past,
        },
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        active_action="blocked",
        block_code="terminal_continuation_unresolved",
        block_reason="terminal continuation did not materialize",
        block_retryable=False,
    )
    state = store.save(path, state)
    before_hop = json.loads(json.dumps(_active_hop(state)))
    before_record = RequestLedger(before_hop["ledger_path"]).get(before_hop["request_id"])
    assert before_record is not None and before_record.attempts == 1

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
    released, changed = store.refresh_scheduling(
        path,
        tasks=store.discover_with_errors()[0],
    )
    assert changed is True
    assert released["repair_wait"]["state"] == "RELEASED"
    assert released["repair_wait"]["original_block_code"] == "terminal_continuation_unresolved"
    assert _active_hop(released) == before_hop
    return store, released, worker, path, _active_hop(released), receipt, before_record


def test_waiting_working_copy_isolates_repair_release_marker(tmp_path: Path):
    store, state, _worker, path, _hop, _receipt, _before_record = (
        _released_terminal_continuation_task(
            tmp_path, task_id="task-repair-release-copy-isolation"
        )
    )
    baseline = store.load(path)
    working = _waiting_working_copy(baseline)

    working["repair_wait"]["transport_rearmed_request_id"] = _active_hop(working)[
        "request_id"
    ]

    assert "transport_rearmed_request_id" not in baseline["repair_wait"]


def _accepted_user_only_snapshot(state: dict, receipt):
    snapshot = send_snapshot(
        messages=(
            MessageSnapshot(
                "user",
                receipt.user_message_id,
                receipt.user_turn_id,
                receipt.prompt,
                (),
            ),
        ),
        state=ChatGPTState.WAITING_PROMPT,
        task_id=state["task_id"],
        team=state["team"],
    )
    return SimpleNamespace(
        **{
            **snapshot.__dict__,
            "composer_empty": True,
            "manual_input_pending": False,
            "stop_visible": False,
            "response_activity_text": "",
            "response_activity_structure": "",
            "response_activity_turn_id": None,
            "response_activity_length": 0,
        }
    )


def test_repair_release_rearms_stale_terminal_wait_and_consumes_materialized_backend_response(
    tmp_path: Path,
):
    store, state, worker, path, hop, receipt, before_record = _released_terminal_continuation_task(
        tmp_path, task_id="task-repair-release-terminal-arrives"
    )
    report_relative = hop["expected_report_path"]
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("repair release terminal response", encoding="utf-8")
    response_text = json.dumps({"route": "REVIEW", "handoff": report_relative})
    snapshot = _accepted_user_only_snapshot(state, receipt)
    response = MessageSnapshot(
        "assistant", "assistant-repaired", "assistant-turn-repaired", response_text, ()
    )
    calls = {"status": 0, "graph": 0, "locate": 0, "send": 0, "retry": 0, "restart": 0, "new_chat": 0}

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def locate_owned(self, *_args, **_kwargs):
            calls["locate"] += 1
            return acquired

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("repair release recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("repair release recovery must not Retry")

        async def restart(self, *_args, **_kwargs):
            calls["restart"] += 1
            raise AssertionError("repair release recovery must not Restart")

        async def new_chat(self, *_args, **_kwargs):
            calls["new_chat"] += 1
            raise AssertionError("repair release recovery must not open New Chat")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    hop = _active_hop(state)

    assert state["status"] == "RUNNING"
    assert hop["state"] == "responded"
    assert hop["response"] == response_text
    assert hop["wait"]["completion_mode"] == "controller_recovery"
    assert calls == {"status": 0, "graph": 0, "locate": 1, "send": 0, "retry": 0, "restart": 0, "new_chat": 0}
    assert state["repair_wait"]["transport_rearmed_request_id"] == hop["request_id"]
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None and record.attempts == 1 and record.receipt == before_record.receipt
    assert not any(control["action"] == "resume" for control in state["controls"])


def test_repair_release_rearms_once_when_terminal_continuation_is_still_missing(tmp_path: Path):
    store, state, worker, path, hop, receipt, before_record = _released_terminal_continuation_task(
        tmp_path, task_id="task-repair-release-terminal-missing"
    )
    snapshot = _accepted_user_only_snapshot(state, receipt)
    calls = {"status": 0, "graph": 0, "locate": 0, "send": 0, "retry": 0, "restart": 0, "new_chat": 0}

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("no DOM continuation")

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def locate_owned(self, *_args, **_kwargs):
            calls["locate"] += 1
            return acquired

        async def send(self, *_args, **_kwargs):
            calls["send"] += 1
            raise AssertionError("repair release recovery must not Send")

        async def retry(self, *_args, **_kwargs):
            calls["retry"] += 1
            raise AssertionError("repair release recovery must not Retry")

        async def restart(self, *_args, **_kwargs):
            calls["restart"] += 1
            raise AssertionError("repair release recovery must not Restart")

        async def new_chat(self, *_args, **_kwargs):
            calls["new_chat"] += 1
            raise AssertionError("repair release recovery must not open New Chat")

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))
    hop = _active_hop(state)

    assert state["status"] == "RUNNING"
    assert hop["state"] == "waiting"
    assert hop["wait"]["completion_mode"] == "controller_recovery"
    assert "terminal_graph_attempts" not in hop["wait"]
    assert hop["wait"]["terminal_continuation_unresolved"]["request_id"] == hop["request_id"]
    assert hop["wait"]["terminal_continuation_unresolved"]["refresh_baseline"] == 0
    assert hop["wait"]["refresh_count"] == 1
    assert worker_module.parse_time(hop["wait"]["deadline_at"]) > datetime.now(timezone.utc)
    first_rearm = state["repair_wait"]["transport_rearmed_at"]
    assert state["repair_wait"]["transport_rearmed_request_id"] == hop["request_id"]
    assert calls == {"status": 0, "graph": 0, "locate": 1, "send": 0, "retry": 0, "restart": 0, "new_chat": 0}

    state = store.load(path)
    hop = _active_hop(state)
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert state["repair_wait"]["transport_rearmed_at"] == first_rearm
    assert calls == {"status": 0, "graph": 0, "locate": 2, "send": 0, "retry": 0, "restart": 0, "new_chat": 0}
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert record is not None and record.attempts == 1 and record.receipt == before_record.receipt
    assert not any(control["action"] == "resume" for control in state["controls"])


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
