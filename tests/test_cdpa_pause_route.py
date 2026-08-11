from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_routes import parse_route_response
from playwright_auto.cdpa_store import task_goal_for_hop
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.cdpa_workflow_agents import normalize_workflow_definition
from playwright_auto.chatgpt import MessageBaseline, PageBinding, SendReceipt, prompt_digest
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_worker import FakeActions, setup_task


def _prepare_pause_response(tmp_path: Path, *, task_id: str, role: str):
    roles = ("PLAN", "DEV") if role == "DEV" else None
    _config, store, state, worker = setup_task(tmp_path, task_id=task_id, roles=roles)
    hop = _active_hop(state)
    if role != "PLAN":
        hop["target_role"] = role
        hop["physical_role"] = state["roles"][role]["physical_role"]
        state["active_role"] = role
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    binding = PageBinding(f"page-{hop['physical_role']}", hop["physical_role"])
    receipt = SendReceipt(
        prompt=hop["prompt"],
        prompt_sha256=prompt_digest(hop["prompt"]),
        binding=binding,
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id=f"user-{task_id}",
        user_turn_id=f"turn-{task_id}",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    hop["receipt"] = receipt.to_dict()
    hop["response"] = json.dumps(
        {"route": "PAUSE", "handoff": hop["expected_report_path"]}
    )
    hop["state"] = "responded"
    return store, state, worker, Path(state["manifest_path"]), hop


def test_pause_is_lifecycle_route_but_not_workflow_role_definition(tmp_path: Path):
    assert parse_route_response(
        '{"route":"PAUSE","handoff":"x"}', source_role="DEV"
    ).route == "PAUSE"
    assert parse_route_response(
        '{"route":"PAUSE","handoff":"x"}',
        source_role="WF_CUSTOM",
        allowed_routes=("PLAN", "WF_CUSTOM", "PAUSE", "DONE"),
    ).route == "PAUSE"
    with pytest.raises(ValueError, match="unknown workflow roles"):
        setup_task(
            tmp_path,
            task_id="task-pause-is-not-role",
            roles=("PLAN", "PAUSE"),
        )
    for reserved in ("PAUSE", "DONE"):
        with pytest.raises(ValueError, match="reserved lifecycle route"):
            normalize_workflow_definition(
                {
                    "route_key": reserved,
                    "display_name": reserved,
                    "system_prompt": "reserved",
                    "is_system": False,
                    "deleted_at": None,
                }
            )


def test_invalid_pause_handoff_uses_existing_route_repair(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-invalid-pause-handoff"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = '{"route":"PAUSE","handoff":""}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert "handoff must not be empty" in repair["validation_error"]
    assert state["reports"] == []


@pytest.mark.parametrize("role", ["PLAN", "DEV"])
def test_role_pause_completes_accepted_request_without_child(tmp_path: Path, role: str):
    _store, state, worker, _path, source = _prepare_pause_response(
        tmp_path, task_id=f"task-{role.lower()}-pause", role=role
    )

    worker._responded(state, source)

    record = RequestLedger(source["ledger_path"]).get(source["request_id"])
    assert source["state"] == "routed"
    assert source["route"] == "PAUSE"
    assert source["report_path"] == source["expected_report_path"]
    assert state["status"] == "PAUSED"
    assert state["kanban_column"] == "PAUSED"
    assert state["active_hop_id"] == source["hop_id"]
    assert state["active_role"] == role
    assert len(state["hops"]) == 1
    assert state["block_code"] is None
    assert state["block_reason"] is None
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_operator_resume_from_role_pause_appends_one_fresh_same_role_hop(tmp_path: Path):
    store, state, worker, path, source = _prepare_pause_response(
        tmp_path, task_id="task-role-pause-resume", role="PLAN"
    )
    worker._responded(state, source)
    source_hop_id = source["hop_id"]
    source_request_id = source["request_id"]
    source_handoff = source["report_path"]
    state["effective_goal"] = "Revised goal after external prerequisite"
    state["goal_revisions"] = [
        {
            "revision": 1,
            "changed_at": state["updated_at"],
            "applies_from_hop_id": source_hop_id + 1,
            "goal": state["effective_goal"],
            "external_command_id": "pause-goal-revision",
        }
    ]
    store.save(path, state)
    queued = store.request_resume(
        path,
        reason="external prerequisite completed",
        external_command_id="resume-role-pause-once",
    )

    class NoSourceTabActions(FakeActions):
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("role-initiated PAUSE resume must not locate source tab")

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("role-initiated PAUSE resume must not reopen source tab")

    assert asyncio.run(worker._apply_control(queued, NoSourceTabActions(), path)) is True

    control = queued["controls"][-1]
    child = _active_hop(queued)
    old_record = RequestLedger(source["ledger_path"]).get(source_request_id)
    assert control["status"] == "applied"
    assert control["result"]["action"] == "release_role_pause"
    assert control["result"]["postcondition"] == "child_hop_appended"
    assert child["hop_id"] == source_hop_id + 1
    assert child["parent_hop_id"] == source_hop_id
    assert child["source_role"] == "PLAN"
    assert child["target_role"] == "PLAN"
    assert child["handoff"] == source_handoff
    assert child["state"] == "pre_send"
    assert old_record is not None
    assert old_record.status is RequestStatus.COMPLETED
    assert old_record.attempts == 1
    assert RequestLedger(child["ledger_path"]).peek(child["request_id"]) is None
    assert task_goal_for_hop(queued, child["hop_id"]) == queued["effective_goal"]

    replay = store.request_resume(
        path,
        reason="duplicate resume command",
        external_command_id="resume-role-pause-once",
    )
    assert len(replay["hops"]) == 2
    assert len(replay["controls"]) == len(queued["controls"])
    assert asyncio.run(worker._apply_control(replay, FakeActions(), path)) is False


def test_runtime_resume_command_finishes_when_role_pause_control_is_applied(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, source = _prepare_pause_response(
        tmp_path, task_id="task-role-pause-runtime-command", role="PLAN"
    )
    worker._responded(state, source)
    store.save(path, state)
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-role-pause-runtime-resume",
        idempotency_key="role-pause-runtime-resume",
        kind="resume_team",
        task_id=state["task_id"],
        expected_task_version=None,
        payload={
            "team": state["team"],
            "reason": "external prerequisite completed",
        },
    )

    dispatched = worker._apply_next_command()
    assert dispatched is not None
    assert dispatched["status"] == "running"
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: FakeActions())

    advanced = asyncio.run(
        worker.advance(
            path,
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )

    command = worker.runtime_db.get_command("cmd-role-pause-runtime-resume")
    source_record = RequestLedger(source["ledger_path"]).get(source["request_id"])
    assert command is not None
    assert command["status"] == "applied"
    assert command["finished_at"] is not None
    assert command["result"]["action"] == "release_role_pause"
    assert command["result"]["postcondition"] == "child_hop_appended"
    assert len(advanced["hops"]) == 2
    assert _active_hop(advanced)["parent_hop_id"] == source["hop_id"]
    assert source_record is not None
    assert source_record.status is RequestStatus.COMPLETED
    assert source_record.attempts == 1


def test_non_operator_resume_cannot_release_role_pause(tmp_path: Path):
    store, state, worker, path, source = _prepare_pause_response(
        tmp_path, task_id="task-role-pause-agent-resume", role="PLAN"
    )
    worker._responded(state, source)
    store.save(path, state)
    queued = store.load(path)
    store._queue_control(
        queued,
        "resume",
        role="PLAN",
        reason="automatic agent must not release intentional pause",
        origin="independent_agent",
        source_task_id="agent-maintainers-g1",
        source_event_key="recovery:role-pause",
    )

    assert asyncio.run(worker._apply_control(queued, FakeActions())) is True
    assert queued["controls"][-1]["status"] == "rejected"
    assert queued["status"] == "PAUSED"
    assert queued["active_hop_id"] == source["hop_id"]
    assert len(queued["hops"]) == 1


def test_pause_prompt_contract_is_shared_and_plan_never_self_routes_to_wait():
    root = Path(__file__).resolve().parents[1]
    guides = [
        root / "prompts/cdpa/RESPONSE_GUIDE.md",
        root / "src/playwright_auto/cdpa_defaults/prompts/cdpa/RESPONSE_GUIDE.md",
    ]
    for path in guides:
        text = path.read_text(encoding="utf-8")
        assert "PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE" in text
        assert "concrete external/manual prerequisite" in text
        assert "exact resume condition" in text
        assert "must not be used" in text

    for path in (
        root / "prompts/cdpa/PLAN.md",
        root / "src/playwright_auto/cdpa_defaults/prompts/cdpa/PLAN.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert "PLAN -> PAUSE" in text
        assert "PLAN -> PLAN" in text
