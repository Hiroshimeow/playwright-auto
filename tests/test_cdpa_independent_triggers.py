from __future__ import annotations

from copy import deepcopy

from playwright_auto.cdpa_independent import (
    canonical_independent_events,
    claim_oldest_event,
    validate_trigger_settings,
)


def workflow(
    task_id: str,
    *,
    team: str,
    status: str,
    updated_at: str,
    block_code: str | None = None,
    block_reason: str | None = None,
    controls: list[dict] | None = None,
) -> dict:
    return {
        "task_mode": "workflow",
        "task_id": task_id,
        "team": team,
        "status": status,
        "updated_at": updated_at,
        "created_at": updated_at,
        "completed_at": updated_at if status == "DONE" else None,
        "stopped_at": updated_at if status == "STOPPED" else None,
        "active_role": "DEV" if status not in {"DONE", "STOPPED"} else None,
        "active_hop_id": 2 if status not in {"DONE", "STOPPED"} else None,
        "block_code": block_code,
        "block_reason": block_reason,
        "hops": [
            {
                "hop_id": 2,
                "target_role": "DEV",
                "state": "waiting",
                "request_id": f"{task_id}-hop2",
            }
        ],
        "controls": controls or [],
        "reports": [],
    }


def agent(*, name: str = "Maintainers", recovery: bool = True) -> dict:
    return {
        "task_mode": "independent",
        "task_id": f"agent-{name.casefold()}-g1",
        "team": f"agent-{name.casefold()}",
        "status": "WAITING",
        "independent": {
            "agent_name": name,
            "agent_key": name.casefold(),
            "enabled": True,
            "trigger_settings": validate_trigger_settings(
                {"recovery": recovery, "task_done": True}
            ),
            "active_event": None,
            "watermarks": {"seen_event_keys": []},
            "occurrence_counts": {},
        },
    }


def test_two_blocked_teams_claim_oldest_first_and_do_not_duplicate_active_event():
    current_agent = agent()
    alpha = workflow(
        "task-a",
        team="alpha",
        status="BLOCKED",
        updated_at="2026-07-26T00:00:00+00:00",
        block_code="role_offline",
        block_reason="alpha-dev tab is offline",
    )
    beta = workflow(
        "task-b",
        team="beta",
        status="BLOCKED",
        updated_at="2026-07-26T00:01:00+00:00",
        block_code="queue_release_failed",
        block_reason="queue release failed",
    )

    events = canonical_independent_events(current_agent, [beta, alpha, current_agent])
    first = claim_oldest_event(current_agent, events, all_tasks=[current_agent])

    assert first["independent"]["active_event"]["target_task_id"] == "task-a"
    assert first["status"] == "RUNNING"
    assert claim_oldest_event(first, events, all_tasks=[first]) == first

    successor = deepcopy(current_agent)
    successor["task_id"] = "agent-maintainers-g2"
    successor["independent"]["watermarks"]["seen_event_keys"] = [
        first["independent"]["active_event"]["event_key"]
    ]
    remaining = canonical_independent_events(successor, [beta, alpha, successor])
    second = claim_oldest_event(successor, remaining, all_tasks=[successor])
    assert second["independent"]["active_event"]["target_task_id"] == "task-b"


def test_recovery_requires_continuous_blocked_state():
    current_agent = agent()
    target = workflow(
        "task-queue",
        team="queue-team",
        status="WAITING",
        updated_at="2026-07-26T00:00:00+00:00",
    )
    target.update(
        waiting_code="queue_release_failed",
        waiting_reason="exact team owner is unreadable",
        waiting={
            "reason": "queue_release_failed",
            "since": "2026-07-26T00:00:00+00:00",
        },
    )
    assert not [
        item
        for item in canonical_independent_events(current_agent, [target, current_agent])
        if item["trigger_type"] == "recovery"
    ]

    target.update(
        status="BLOCKED",
        blocked_at="2026-07-26T00:00:00+00:00",
        block_code="queue_release_failed",
        block_reason="exact team owner is unreadable",
    )
    recovery = [
        item
        for item in canonical_independent_events(current_agent, [target, current_agent])
        if item["trigger_type"] == "recovery"
    ]
    assert len(recovery) == 1
    assert recovery[0]["target_task_id"] == "task-queue"
    assert recovery[0]["failure_signature"].startswith("queue_release_failed:")

def test_operator_stop_and_self_failure_do_not_create_recovery_events():
    current_agent = agent()
    operator_stop = workflow(
        "task-stopped",
        team="stopped",
        status="STOPPED",
        updated_at="2026-07-26T00:00:00+00:00",
        controls=[
            {
                "action": "stop",
                "origin": "operator",
                "status": "applied",
                "applied_at": "2026-07-26T00:00:00+00:00",
            }
        ],
    )
    self_failure = workflow(
        "agent-maintainers-g0",
        team="agent-maintainers",
        status="BLOCKED",
        updated_at="2026-07-26T00:01:00+00:00",
        block_code="unexpected_error",
        block_reason="agent failed",
    )

    events = canonical_independent_events(
        current_agent, [operator_stop, self_failure, current_agent]
    )

    assert not [item for item in events if item["trigger_type"] == "recovery"]






def test_check_all_interval_emits_full_review_trigger():
    current = agent(name="Monitor", recovery=False)
    current["independent"]["trigger_settings"] = validate_trigger_settings(
        {"interval_minutes": 30, "check_all": True}
    )

    events = canonical_independent_events(
        current,
        [current],
        now=__import__("datetime").datetime.fromisoformat(
            "2026-07-26T00:30:00+00:00"
        ),
    )

    assert len(events) == 1
    assert events[0]["trigger_type"] == "check_all"
    assert events[0]["event_key"].startswith("check-all:monitor:")
