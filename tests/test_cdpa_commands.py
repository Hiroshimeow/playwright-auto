from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from playwright_auto.cdpa_commands import (
    RepairRequest,
    WorkerCommand,
    command_snapshot,
    normalize_root_cause_key,
    validate_worker_command,
)


def state() -> dict:
    return {
        "task_id": "task-a",
        "team": "alpha",
        "repository": "/repo",
        "status": "BLOCKED",
        "updated_at": "2026-07-25T00:00:00+00:00",
        "active_hop_id": 7,
        "active_role": "DEV",
        "block_code": "role_offline",
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "page_id": "page-7",
                "page_url": "https://chatgpt.com/c/exact",
                "conversation_generation": 2,
            }
        },
        "hops": [
            {
                "hop_id": 7,
                "target_role": "DEV",
                "physical_role": "alpha-dev",
                "turn": 3,
                "state": "waiting",
                "request_id": "task-a-hop7",
                "handoff": ".plan/alpha/alpha-plan_turn3_task-a.md",
                "conversation_url": "https://chatgpt.com/c/exact",
                "receipt": {
                    "user_turn_id": "turn-7",
                    "binding": {"page_id": "page-7", "role": "alpha-dev"},
                },
            }
        ],
    }


def test_worker_command_is_frozen_and_round_trips_complete_snapshot():
    current = state()
    snapshot = command_snapshot(current, role="DEV")
    command = WorkerCommand.create(
        origin="independent_agent",
        action="open_tab",
        reason="restore exact role ownership",
        state=current,
        role="DEV",
        source_task_id="agent-maintainers-g1",
        source_event_key="recovery:task-a:role-offline",
    )

    assert command.snapshot == snapshot
    assert WorkerCommand.from_dict(command.to_dict()) == command
    with pytest.raises(FrozenInstanceError):
        command.action = "resume"  # type: ignore[misc]


def test_worker_command_rejects_stale_hop_conversation_and_receipt():
    current = state()
    command = WorkerCommand.create(
        origin="independent_agent",
        action="open_tab",
        reason="restore exact role ownership",
        state=current,
        role="DEV",
        source_task_id="agent-maintainers-g1",
        source_event_key="recovery:task-a:role-offline",
    )
    validate_worker_command(command, current)

    current["hops"][0]["request_id"] = "other-request"
    with pytest.raises(ValueError, match="active request"):
        validate_worker_command(command, current)


def test_repair_request_is_repository_bounded_and_rejects_arbitrary_scope():
    request = RepairRequest.create(
        root_cause="OPEN_ROLE_TAB reported success without clearing role_offline",
        affected_state=state(),
        incident_id="maint-1",
        disposition="HOLD_FOR_REPAIR",
        reason="same irreversible send boundary is at risk",
        reproduction="Reopen exact role tab while the active waiting receipt is preserved.",
        source_areas=("cdpa_worker", "cdpa_store", "tests", "prompts"),
        required_tests=("focused role-offline regression", "live controlled recovery"),
        lesson="A recovery control is successful only after its operational postcondition passes.",
    )

    assert request.repository == "/repo"
    assert request.root_cause_key == normalize_root_cause_key(
        "OPEN_ROLE_TAB reported success without clearing role_offline"
    )
    assert request.affected_hop_id == 7
    assert request.affected_request_id == "task-a-hop7"

    with pytest.raises(ValueError, match="source area"):
        RepairRequest.create(
            root_cause="x",
            affected_state=state(),
            incident_id="maint-1",
            disposition="CONTINUE_IN_PARALLEL",
            reason="x",
            reproduction="x",
            source_areas=("../../unrelated",),
            required_tests=("x",),
            lesson=None,
        )


def test_repair_request_accepts_exact_bounds_and_task_text_remains_bounded():
    from playwright_auto.cdpa_commands import (
        REPAIR_REASON_MAX_CHARS,
        REPAIR_REPRODUCTION_MAX_CHARS,
        REPAIR_REQUIRED_TEST_MAX_CHARS,
        REPAIR_REQUIRED_TEST_MAX_COUNT,
        REPAIR_ROOT_CAUSE_MAX_CHARS,
        REPAIR_SOURCE_AREA_MAX_COUNT,
        REPAIR_SOURCE_AREAS,
    )

    areas = tuple(sorted(REPAIR_SOURCE_AREAS)[:REPAIR_SOURCE_AREA_MAX_COUNT])
    tests = tuple(
        f"{index:02d}-" + "t" * (REPAIR_REQUIRED_TEST_MAX_CHARS - 3)
        for index in range(REPAIR_REQUIRED_TEST_MAX_COUNT)
    )
    request = RepairRequest.create(
        root_cause="r" * REPAIR_ROOT_CAUSE_MAX_CHARS,
        affected_state=state(),
        incident_id="maint-1",
        disposition="HOLD_FOR_REPAIR",
        reason="q" * REPAIR_REASON_MAX_CHARS,
        reproduction="p" * REPAIR_REPRODUCTION_MAX_CHARS,
        source_areas=areas,
        required_tests=tests,
        lesson="bounded lesson",
    )

    assert len(request.root_cause) == REPAIR_ROOT_CAUSE_MAX_CHARS
    assert len(request.reason) == REPAIR_REASON_MAX_CHARS
    assert len(request.reproduction) == REPAIR_REPRODUCTION_MAX_CHARS
    assert len(request.source_areas) == REPAIR_SOURCE_AREA_MAX_COUNT
    assert len(request.required_tests) == REPAIR_REQUIRED_TEST_MAX_COUNT
    assert all(len(item) == REPAIR_REQUIRED_TEST_MAX_CHARS for item in request.required_tests)
    assert RepairRequest.from_dict(request.to_dict()) == request
    assert len(request.task_text()) < 20_000




@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"reason": ""}, "reason"),
        ({"reproduction": ""}, "reproduction"),
        ({"required_tests": []}, "required tests"),
        ({"source_areas": []}, "source areas"),
        ({"lesson": "x\ny"}, "lesson"),
        ({"incident_id": ""}, "identity"),
        ({"affected_task_id": ""}, "identity"),
        ({"repository": ""}, "identity"),
        ({"affected_hop_id": -1}, "hop"),
    ],
)
def test_repair_request_from_dict_revalidates_corrupted_durable_payload(
    mutation: dict,
    message: str,
):
    request = RepairRequest.create(
        root_cause="root cause",
        affected_state=state(),
        incident_id="maint-1",
        disposition="HOLD_FOR_REPAIR",
        reason="reason",
        reproduction="reproduction",
        source_areas=("cdpa_worker", "tests"),
        required_tests=("focused regression",),
        lesson="lesson",
    )
    payload = request.to_dict()
    payload.update(mutation)

    with pytest.raises(ValueError, match=message):
        RepairRequest.from_dict(payload)


def _strict_repair_request() -> RepairRequest:
    return RepairRequest.create(
        root_cause="strict root cause",
        affected_state=state(),
        incident_id="maint-strict",
        disposition="HOLD_FOR_REPAIR",
        reason="strict reason",
        reproduction="strict reproduction",
        source_areas=("cdpa_worker", "tests"),
        required_tests=("focused regression", "full suite"),
        lesson="strict lesson",
    )
