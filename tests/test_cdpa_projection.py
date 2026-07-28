from __future__ import annotations

import json
from pathlib import Path

from playwright_auto.cdpa_projection import (
    build_dashboard_actions,
    build_task_projection,
    build_waiting_order,
)
from playwright_auto.cdpa_team import exact_team_ready_waiters


def raw_task(tmp_path: Path) -> dict:
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("report", encoding="utf-8")
    return {
        "task_id": "task-a",
        "team": "alpha",
        "status": "RUNNING",
        "kanban_column": "WORKING",
        "active_role": "DEV",
        "active_hop_id": 2,
        "active_action": "waiting",
        "updated_at": "2026-07-25T00:00:00+00:00",
        "created_at": "2026-07-24T00:00:00+00:00",
        "task_text": "Build the runtime\nsecret details",
        "manifest_path": str(tmp_path / ".plan" / "alpha" / "task-a.json"),
        "repository": str(tmp_path),
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "status": "active",
                "turn": 2,
                "page_id": "page-1",
                "page_url": "https://chatgpt.com/c/abc?token=secret",
                "online": True,
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "state": "routed",
                "target_role": "PLAN",
                "source_role": None,
                "route": "REVIEW",
                "turn": 1,
                "prompt": "PLAN input at /home/ayumi/private/repository",
                "handoff": ".plan/alpha/alpha-plan_turn1_task-a.md",
                "report_path": "/home/ayumi/private/.plan/alpha/report.md",
            },
            {
                "hop_id": 2,
                "state": "waiting",
                "target_role": "DEV",
                "source_role": "PLAN",
                "turn": 2,
                "prompt": "DEV input with token=secret-value",
                "handoff": ".plan/alpha/alpha-dev_turn2_task-a.md",
            },
        ],
        "dependency_events": [
            {
                "at": "2026-07-25T00:02:00+00:00",
                "kind": "dependency_wait",
                "reason": "Waiting for task parent-a at /home/ayumi/private/manifest.json",
            }
        ],
        "queue_events": [
            {
                "at": "2026-07-25T00:03:00+00:00",
                "kind": "queue_admitted",
                "message": "Task admitted to team queue",
            }
        ],
        "errors": [
            {
                "at": "2026-07-25T00:04:00+00:00",
                "code": "role_offline",
                "error": "Role offline at /home/ayumi/private/report.md",
            }
        ],
        "route_timeline": [
            {
                "at": "2026-07-25T00:00:00+00:00",
                "kind": "route",
                "source_role": "PLAN",
                "route": "REVIEW",
                "hop_id": 1,
                "report_path": "/home/ayumi/private/.plan/alpha/report.md",
            },
            {
                "at": "2026-07-25T00:01:00+00:00",
                "kind": "route_repair",
                "source_role": "REVIEW",
                "route": "PLAN",
                "hop_id": 2,
                "report_path": "/home/ayumi/private/.plan/alpha/repair.md",
            },
            {
                "at": "2026-07-25T00:01:30+00:00",
                "kind": "route",
                "source_role": "AUDIT",
                "route": "PLAN",
                "hop_id": 3,
                "report_path": "/home/ayumi/private/.plan/alpha/audit.md",
            },
            *[
                {"at": f"2026-07-25T00:{i:02d}:30+00:00", "status": "RUNNING", "message": f"event {i}"}
                for i in range(2, 60)
            ],
        ],
        "reports": [
            {
                "report_id": "r1",
                "path": str(report),
                "sha256": "00" * 32,
                "size": 6,
                "physical_role": "alpha-plan",
                "turn": 1,
            }
        ],
        "attachments": [{"path": "/private/secret.txt", "name": "secret.txt"}],
        "request_ledger": {"secret": "must-not-leak"},
        "cleanup": {"state": "ACTIVE", "workspace": "/home/ayumi/private/repository"},
        "controls": [
            {
                "action": "restart_role",
                "reason": "Recover role at /home/ayumi/private/report.md",
                "command": {
                    "role": "DEV",
                    "repository": "/home/ayumi/private/repository",
                    "manifest_path": "/home/ayumi/private/manifest.json",
                },
            }
        ],
        "options": {"report_mode": "file"},
        "depends_on_task_ids": [],
    }


def test_independent_projection_exposes_agent_job_without_system_prompt(tmp_path: Path):
    raw = {
        "task_mode": "independent",
        "task_id": "agent-maintainers-g1",
        "team": "agent-maintainers",
        "status": "RUNNING",
        "kanban_column": "INDEPENDENT_AGENTS",
        "active_role": "AGENT",
        "active_hop_id": 1,
        "active_action": "wait_response",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "created_at": "2026-07-25T00:00:00+00:00",
        "task_text": "Independent agent: Maintainers",
        "manifest_path": str(tmp_path / ".plan" / "agent-maintainers" / "task.json"),
        "repository": str(tmp_path),
        "roles": {
            "AGENT": {
                "physical_role": "agent-maintainers-agent",
                "status": "active",
                "turn": 1,
                "page_id": "page-agent",
                "page_url": "https://chatgpt.com/c/agent",
                "online": True,
                "last_error": (
                    "ownership failed for page-agent at https://chatgpt.com/c/agent; "
                    "see https://example.com/help"
                ),
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "kind": "independent_job",
                "state": "waiting",
                "target_role": "AGENT",
                "turn": 1,
                "request_id": "request-secret-123",
                "conversation_url": "https://chatgpt.com/c/agent",
                "receipt": {
                    "page_id": "page-agent",
                    "user_message_id": "message-secret-456",
                    "user_turn_id": "turn-secret-789",
                },
                "prompt": "private constructor and trigger context",
                "handoff": "trigger context",
            }
        ],
        "independent": {
            "agent_name": "Maintainers",
            "agent_key": "maintainers",
            "agent_generation": 1,
            "enabled": True,
            "system_prompt": "SECRET SYSTEM PROMPT",
            "trigger_settings": {"recovery": True},
            "active_event": {
                "event_key": "recovery:task-a",
                "trigger_type": "recovery",
                "target_team": "alpha",
                "target_task_id": "task-a",
                "occurrence_count": 2,
                "check_count": 1,
            },
            "cycle": 3,
            "max_cycles": 5,
            "last_outcome": None,
        },
        "route_timeline": [],
        "dependency_events": [
            {
                "kind": "dependency_wait",
                "reason": (
                    "waiting on page-agent at https://chatgpt.com/c/agent; "
                    "public docs https://example.com/help"
                ),
            }
        ],
        "queue_events": [],
        "errors": [
            {
                "code": "role_offline",
                "error": (
                    "request-secret-123 failed for message-secret-456 / "
                    "turn-secret-789 on page-agent at https://chatgpt.com/c/agent"
                ),
            }
        ],
        "reports": [],
        "attachments": [],
        "cleanup": {
            "state": "ACTIVE",
            "last_error": (
                "cleanup page-agent at https://chatgpt.com/c/agent; "
                "see https://example.com/help"
            ),
        },
        "controls": [
            {
                "action": "stop",
                "reason": (
                    "stop request-secret-123 on page-agent at "
                    "https://chatgpt.com/c/agent"
                ),
                "result": (
                    "message-secret-456 and turn-secret-789 remain bound; "
                    "docs https://example.com/help"
                ),
                "command": {
                    "request_id": "request-secret-123",
                    "receipt": {"user_message_id": "message-secret-456"},
                    "ledger_path": "/home/ayumi/private/requests.json",
                    "manifest_path": "/home/ayumi/private/task.json",
                    "system_prompt": "CONTROL SYSTEM SECRET",
                    "prompt": "CONTROL PROMPT SECRET",
                    "response": "CONTROL RESPONSE SECRET",
                    "snapshot": {
                        "page_id": "page-agent",
                        "page_url": "https://chatgpt.com/c/page-secret",
                        "conversation_url": "https://chatgpt.com/c/conversation-secret",
                    },
                },
            }
        ],
        "options": {"report_mode": "inline"},
        "depends_on_task_ids": [],
    }

    projection = build_task_projection(raw, tasks=[raw])
    public = json.dumps({"summary": projection.summary, "detail": projection.detail})

    assert projection.summary["task_mode"] == "independent"
    assert projection.summary["column"] == "INDEPENDENT_AGENTS"
    assert projection.summary["agent"]["name"] == "Maintainers"
    assert projection.summary["agent"]["target_team"] == "alpha"
    assert projection.summary["agent"]["trigger_type"] == "recovery"
    assert projection.summary["agent"]["occurrence_count"] == 2
    assert projection.summary["agent"]["cycle"] == 3
    assert projection.detail["roles"][0]["logical_role"] == "AGENT"

    def nested_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from nested_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from nested_keys(item)

    forbidden_keys = {
        "page_id",
        "page_url",
        "conversation_url",
        "request_id",
        "receipt",
        "ledger_path",
        "manifest_path",
        "system_prompt",
        "prompt",
        "response",
    }
    assert forbidden_keys.isdisjoint(set(nested_keys(projection.summary)))
    assert forbidden_keys.isdisjoint(set(nested_keys(projection.detail)))
    for secret in (
        "page-agent",
        "https://chatgpt.com/c/agent",
        "https://chatgpt.com/c/page-secret",
        "https://chatgpt.com/c/conversation-secret",
        "request-secret-123",
        "message-secret-456",
        "turn-secret-789",
        "CONTROL SYSTEM SECRET",
        "CONTROL PROMPT SECRET",
        "CONTROL RESPONSE SECRET",
    ):
        assert secret not in public
    assert "https://example.com/help" in public
    assert "SECRET SYSTEM PROMPT" not in public
    assert "private constructor" not in public


def test_projection_splits_public_and_private_data(tmp_path: Path):
    projection = build_task_projection(raw_task(tmp_path), tasks=[raw_task(tmp_path)])

    public = json.dumps({"summary": projection.summary, "detail": projection.detail})
    assert "manifest_path" not in public
    assert str(tmp_path) not in public
    assert "request_ledger" not in public
    assert "must-not-leak" not in public
    assert "/private/secret.txt" not in public
    assert projection.private["manifest_path"].endswith("task-a.json")
    assert projection.private["reports"]["r1"]["path"].endswith("alpha-plan_turn1_task-a.md")
    assert projection.detail["reports"][0]["url"] == "/api/reports/task-a/r1"
    assert len(projection.detail["timeline"]) == 50
    assert projection.detail["task_text"] == "Build the runtime\nsecret details"
    assert projection.detail["active_input"]["logical_role"] == "DEV"
    assert projection.detail["active_input"]["hop_id"] == 2
    assert projection.detail["role_inputs"]["PLAN"]["handoff"].endswith("alpha-plan_turn1_task-a.md")
    assert "/home/ayumi" not in public
    assert "secret-value" not in public


def test_disconnected_browser_marks_role_availability_unknown(tmp_path: Path):
    raw = raw_task(tmp_path)

    projection = build_task_projection(
        raw,
        tasks=[raw],
        browser_connected=False,
    )

    assert projection.surface == "active"
    assert projection.summary["availability"] == "unknown"
    assert projection.summary["roles"][0]["online"] is None
    assert projection.detail["roles"][0]["online"] is None


def _waiting_task(
    task_id: str,
    *,
    team: str | None = None,
    created_at: str = "2026-07-25T00:00:00+00:00",
    depends_on: object = (),
    status: str = "WAITING",
    priority: str | None = None,
) -> dict:
    task = {
        "task_id": task_id,
        "team": team or task_id,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "depends_on_task_ids": list(depends_on) if isinstance(depends_on, tuple) else depends_on,
    }
    if priority is not None:
        task["priority"] = priority
    return task


def test_waiting_order_projects_dependency_levels_and_shared_frontiers():
    done = _waiting_task("done", status="DONE")
    running = _waiting_task("running", status="RUNNING")
    tasks = [
        _waiting_task("serial-1"),
        _waiting_task("serial-2", depends_on=("serial-1",)),
        _waiting_task("serial-3", depends_on=("serial-2",)),
        _waiting_task("frontier-peer"),
        done,
        _waiting_task("after-done", depends_on=("done",)),
        running,
        _waiting_task("after-running", depends_on=("running",)),
    ]

    order = build_waiting_order(tasks)

    assert order["serial-1"]["rank"] == 1
    assert order["serial-2"]["rank"] == 2
    assert order["serial-3"]["rank"] == 3
    assert order["frontier-peer"]["rank"] == 1
    assert order["after-done"]["rank"] == 1
    assert order["after-running"]["rank"] == 1
    assert all(record["intervention"] is None for record in order.values())


def test_waiting_order_puts_scheduler_ready_same_team_waiter_first():
    running_parent = _waiting_task("running-parent", status="RUNNING")
    older_blocked = _waiting_task(
        "older-blocked",
        team="shared",
        created_at="2026-07-25T00:00:00+00:00",
        depends_on=("running-parent",),
    )
    older_blocked["queue"] = {"reuse_team": True, "released_at": None}
    later_ready = _waiting_task(
        "later-ready",
        team="shared",
        created_at="2026-07-25T01:00:00+00:00",
    )
    later_ready["queue"] = {"reuse_team": True, "released_at": None}
    tasks = [running_parent, older_blocked, later_ready]

    scheduler = exact_team_ready_waiters(
        tasks,
        "shared",
        dependency_tasks=tasks,
    )
    order = build_waiting_order(tasks)
    projected = sorted(
        (older_blocked, later_ready),
        key=lambda task: (
            order[task["task_id"]]["rank"],
            order[task["task_id"]]["fifo"],
        ),
    )

    assert [task["task_id"] for task in scheduler] == ["later-ready"]
    assert [task["task_id"] for task in projected] == [
        "later-ready",
        "older-blocked",
    ]
    assert order["later-ready"]["rank"] == 1
    assert order["older-blocked"]["rank"] == 2


def test_waiting_order_does_not_propagate_intervention_to_valid_same_team_waiter():
    bad = _waiting_task(
        "bad",
        team="shared",
        created_at="2026-07-25T00:00:00+00:00",
        depends_on="not-a-list",
    )
    parent = _waiting_task("parent", status="RUNNING")
    valid_blocked = _waiting_task(
        "valid-blocked",
        team="shared",
        created_at="2026-07-25T01:00:00+00:00",
        depends_on=("parent",),
    )

    order = build_waiting_order([bad, parent, valid_blocked])

    assert order["bad"]["rank"] is None
    assert "malformed dependencies" in order["bad"]["intervention"]
    assert order["valid-blocked"]["rank"] == 1
    assert order["valid-blocked"]["intervention"] is None


def test_waiting_order_uses_exact_team_scheduler_fifo_and_maximum_predecessor():
    tasks = [
        _waiting_task(
            "regular-old",
            team="shared",
            created_at="2026-07-25T00:00:00+00:00",
        ),
        _waiting_task(
            "urgent-new",
            team="shared",
            created_at="2026-07-25T01:00:00+00:00",
            priority="urgent_repair",
        ),
        _waiting_task(
            "regular-later",
            team="shared",
            created_at="2026-07-25T02:00:00+00:00",
        ),
        _waiting_task("dependency-1"),
        _waiting_task("dependency-2", depends_on=("dependency-1",)),
        _waiting_task(
            "mixed",
            team="shared",
            created_at="2026-07-25T03:00:00+00:00",
            depends_on=("dependency-2",),
        ),
    ]

    order = build_waiting_order(tasks)

    assert order["urgent-new"]["rank"] == 1
    assert order["regular-old"]["rank"] == 2
    assert order["regular-later"]["rank"] == 3
    assert order["dependency-1"]["rank"] == 1
    assert order["dependency-2"]["rank"] == 2
    assert order["mixed"]["rank"] == 4
    assert sorted(order, key=lambda task_id: order[task_id]["fifo"]) == [
        "urgent-new",
        "dependency-1",
        "dependency-2",
        "regular-old",
        "regular-later",
        "mixed",
    ]


def test_waiting_order_fails_closed_for_invalid_dependencies_and_ancestors():
    tasks = [
        _waiting_task("valid"),
        _waiting_task("missing", depends_on=("absent",)),
        _waiting_task("stopped-parent", status="STOPPED"),
        _waiting_task("stopped-child", depends_on=("stopped-parent",)),
        _waiting_task("malformed", depends_on="not-a-list"),
        _waiting_task("duplicate-parent", depends_on=("valid", "valid")),
        _waiting_task("cycle-a", depends_on=("cycle-b",)),
        _waiting_task("cycle-b", depends_on=("cycle-a",)),
        _waiting_task("invalid-ancestor", depends_on=("missing",)),
        _waiting_task("ambiguous"),
        _waiting_task("ambiguous", team="other-team"),
        _waiting_task("ambiguous-child", depends_on=("ambiguous",)),
    ]

    order = build_waiting_order(tasks)

    assert order["valid"]["rank"] == 1
    assert order["valid"]["intervention"] is None
    assert isinstance(order["valid"]["fifo"], int)
    for task_id in (
        "missing",
        "stopped-child",
        "malformed",
        "duplicate-parent",
        "cycle-a",
        "cycle-b",
        "invalid-ancestor",
        "ambiguous",
        "ambiguous-child",
    ):
        assert order[task_id]["rank"] is None
        assert order[task_id]["intervention"].startswith("Intervention required:")
    assert "missing dependency" in order["missing"]["intervention"].lower()
    assert "stopped" in order["stopped-child"]["intervention"].lower()
    assert "cycle" in order["cycle-a"]["intervention"].lower()
    assert "ambiguous" in order["ambiguous-child"]["intervention"].lower()


def test_waiting_order_is_compact_and_changes_projection_hash(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw.update(
        status="WAITING",
        kanban_column="WAITING",
        team="alpha",
        depends_on_task_ids=[],
    )
    first_order = {"task-a": {"rank": 1, "fifo": 0, "intervention": None}}
    second_order = {"task-a": {"rank": 2, "fifo": 0, "intervention": None}}

    first = build_task_projection(raw, tasks=[raw], waiting_order=first_order)
    second = build_task_projection(raw, tasks=[raw], waiting_order=second_order)

    assert first.summary["waiting_order"] == first_order["task-a"]
    assert first.detail["waiting_order"] == first_order["task-a"]
    assert first.summary["projection_sha256"] != second.summary["projection_sha256"]
    assert set(first.summary["waiting_order"]) == {"rank", "fifo", "intervention"}


def test_projection_summary_is_compact_and_deterministic(tmp_path: Path):
    raw = raw_task(tmp_path)
    first = build_task_projection(raw, tasks=[raw])
    second = build_task_projection(raw, tasks=[raw])

    assert first == second
    assert len(json.dumps(first.summary, separators=(",", ":")).encode()) <= 4096
    assert set(first.summary) <= {
        "task_id", "team", "status", "column", "surface", "active_role",
        "active_hop_id", "active_action", "created_at", "started_at", "updated_at",
        "effective_activity_at",
        "availability", "primary_problem", "waiting_reason", "block_code",
        "queue_position", "queue_length", "waiting_order", "roles", "has_reports", "task_title",
        "version", "projection_sha256",
    }


def test_detail_preserves_full_task_and_latest_role_input(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["task_text"] = "T" * 25000
    raw["hops"][-1]["prompt"] = "P" * 30000

    projection = build_task_projection(raw, tasks=[raw])

    assert projection.detail["task_text"] == "T" * 25000
    assert projection.detail["active_input"]["input"] == "P" * 30000
    assert projection.detail["role_inputs"]["DEV"]["input"] == "P" * 30000


def test_route_timeline_projects_explicit_transitions_without_paths(tmp_path: Path):
    projection = build_task_projection(raw_task(tmp_path), tasks=[raw_task(tmp_path)])
    routes = [item for item in projection.private["timeline"] if item["level"] == "ROUTE"]

    repair = next(item for item in routes if item["kind"] == "route_repair")
    routed = next(
        item for item in routes
        if item["kind"] == "route" and item["source_role"] == "PLAN" and item["route"] == "REVIEW"
    )
    assert repair["message"] == "REVIEW → PLAN"
    audit = next(
        item for item in routes
        if item["source_role"] == "AUDIT" and item["route"] == "PLAN"
    )
    assert repair["hop_id"] == 2
    assert routed["message"] == "PLAN → REVIEW"
    assert audit["message"] == "AUDIT → PLAN"
    serialized = json.dumps(routes)
    assert "report_path" not in serialized
    assert "/home/ayumi" not in serialized


def test_route_projection_tolerates_missing_transition_fields(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["route_timeline"].append(
        {
            "at": "2026-07-25T00:05:00+00:00",
            "kind": "route",
            "report_path": "/home/ayumi/private/malformed.md",
        }
    )
    projection = build_task_projection(raw, tasks=[raw])
    malformed = next(
        item for item in projection.private["timeline"]
        if item["at"] == "2026-07-25T00:05:00+00:00"
    )

    assert malformed["message"] == "route"
    assert malformed["source_role"] is None
    assert malformed["route"] is None
    assert "report_path" not in malformed


def test_dependency_queue_and_error_timeline_events_are_bounded_and_sanitized(tmp_path: Path):
    projection = build_task_projection(raw_task(tmp_path), tasks=[raw_task(tmp_path)])
    timeline = projection.private["timeline"]

    dependency = next(item for item in timeline if item["level"] == "DEPENDENCY")
    queued = next(item for item in timeline if item["level"] == "QUEUE")
    error = next(item for item in timeline if item["level"] == "ERROR")

    assert dependency["kind"] == "dependency_wait"
    assert dependency["message"].startswith("Waiting for task parent-a")
    assert queued["kind"] == "queue_admitted"
    assert queued["message"] == "Task admitted to team queue"
    assert error["kind"] == "role_offline"
    assert "Role offline" in error["message"]
    assert "/home/ayumi" not in json.dumps([dependency, queued, error])


def test_dependency_and_queue_lifecycle_event_kinds_are_preserved(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["dependency_events"].extend(
        [
            {"at": "2026-07-25T00:06:00+00:00", "kind": "dependency_resolved"},
            {"at": "2026-07-25T00:07:00+00:00", "kind": "dependency_rewired"},
        ]
    )
    raw["queue_events"].extend(
        [
            {"at": "2026-07-25T00:08:00+00:00", "kind": "queue_enqueued"},
            {"at": "2026-07-25T00:09:00+00:00", "kind": "queue_started"},
            {"at": "2026-07-25T00:10:00+00:00", "kind": "queue_completed"},
        ]
    )
    projection = build_task_projection(raw, tasks=[raw])
    kinds = {item["kind"] for item in projection.private["timeline"]}

    assert {
        "dependency_wait",
        "dependency_resolved",
        "dependency_rewired",
        "queue_admitted",
        "queue_enqueued",
        "queue_started",
        "queue_completed",
    } <= kinds


def _action_task(
    tmp_path: Path,
    *,
    task_id: str,
    team: str,
    status: str,
    title: str | None = None,
) -> dict:
    task = raw_task(tmp_path)
    task.update(
        task_id=task_id,
        team=team,
        team_base=team,
        team_suffix=1,
        status=status,
        kanban_column=status,
        task_text=title or f"{team} {status} task",
        manifest_path=str(tmp_path / ".plan" / team / task_id / f"{task_id}.json"),
        cleanup={"state": "ACTIVE", "phase": None},
    )
    for role in task["roles"].values():
        role["page_id"] = f"page-{task_id}"
        role["online"] = False
    if status in {"DONE", "STOPPED"}:
        task["active_role"] = None
        task["active_hop_id"] = None
    return task


def test_dashboard_actions_are_worker_owned_compact_and_fail_closed(tmp_path: Path):
    tasks = [
        _action_task(tmp_path, task_id="dep-one", team="single", status="DONE", title="Single dependency"),
        _action_task(tmp_path, task_id="dep-a", team="ambiguous", status="RUNNING"),
        _action_task(tmp_path, task_id="dep-b", team="ambiguous", status="PAUSED"),
        _action_task(tmp_path, task_id="stopped-hidden", team="stopped", status="STOPPED"),
        _action_task(tmp_path, task_id="blocked-one", team="blocked", status="BLOCKED"),
        _action_task(tmp_path, task_id="paused-one", team="paused", status="PAUSED"),
        _action_task(tmp_path, task_id="offline-one", team="offline", status="RUNNING"),
        _action_task(tmp_path, task_id="online-one", team="online", status="RUNNING"),
        _action_task(tmp_path, task_id="reuse-done", team="reuse", status="DONE"),
        _action_task(tmp_path, task_id="reuse-owner", team="reuse", status="BLOCKED"),
        _action_task(tmp_path, task_id="conflict-a", team="conflict", status="BLOCKED"),
        _action_task(tmp_path, task_id="conflict-b", team="conflict", status="PAUSED"),
        _action_task(tmp_path, task_id="conflict-done", team="conflict", status="DONE"),
    ]
    actions = build_dashboard_actions(
        tasks,
        browser_connected=True,
        browser_pages=[
            {
                "page_id": "page-online-one",
                "team": "online",
                "task_id": "online-one",
                "online": True,
                "url": "https://chatgpt.com/c/online?secret=hidden",
            }
        ],
    )

    dependencies = {item["team"]: item for item in actions["dependency_teams"]}
    assert "single" not in dependencies
    assert dependencies["ambiguous"] == {
        "team": "ambiguous",
        "ambiguous": False,
        "tasks": [
            {
                "task_id": "dep-a",
                "title": "ambiguous RUNNING task",
                "status": "RUNNING",
            }
        ],
    }
    assert "stopped" not in dependencies
    assert "blocked" not in dependencies
    assert "paused" not in dependencies
    assert "reuse" not in dependencies
    assert "conflict" not in dependencies

    resume = {item["team"]: item for item in actions["resume_teams"]}
    assert resume["blocked"]["reason"] == "blocked"
    assert resume["paused"]["reason"] == "paused"
    assert resume["offline"]["reason"] == "offline"
    assert "online" not in resume
    assert "conflict" not in resume
    assert "stopped" not in resume

    assert actions["reuse_teams"] == [
        {"team": "blocked", "status": "available"},
        {"team": "offline", "status": "available"},
        {"team": "online", "status": "available"},
        {"team": "paused", "status": "available"},
        {"team": "reuse", "status": "available"},
        {"team": "single", "status": "available"},
        {"team": "stopped", "status": "available"},
    ]
    serialized = json.dumps(actions, separators=(",", ":"))
    assert str(tmp_path) not in serialized
    assert "secret=hidden" not in serialized
    assert len(serialized.encode()) < 16 * 1024


def test_dashboard_dependency_choices_only_publish_running_and_waiting(tmp_path: Path):
    tasks = [
        _action_task(tmp_path, task_id="running-task", team="running", status="RUNNING"),
        _action_task(tmp_path, task_id="waiting-task", team="waiting", status="WAITING"),
        _action_task(tmp_path, task_id="done-task", team="done", status="DONE"),
        _action_task(tmp_path, task_id="stopped-task", team="stopped-only", status="STOPPED"),
        _action_task(tmp_path, task_id="paused-task", team="paused-only", status="PAUSED"),
        _action_task(tmp_path, task_id="blocked-task", team="blocked-only", status="BLOCKED"),
    ]

    actions = build_dashboard_actions(tasks)

    dependencies = {item["team"]: item for item in actions["dependency_teams"]}
    assert set(dependencies) == {"running", "waiting"}
    assert dependencies["running"]["tasks"][0]["status"] == "RUNNING"
    assert dependencies["waiting"]["tasks"][0]["status"] == "WAITING"


def test_dashboard_reuse_eligibility_rejects_inconsistent_exact_team_identity(tmp_path: Path):
    first = _action_task(
        tmp_path,
        task_id="identity-a",
        team="identity-conflict",
        status="DONE",
    )
    second = _action_task(
        tmp_path,
        task_id="identity-b",
        team="identity-conflict",
        status="STOPPED",
    )
    second["team_base"] = "other-base"

    actions = build_dashboard_actions([first, second])

    assert actions["reuse_teams"] == []


def test_dashboard_actions_do_not_infer_offline_when_browser_is_unknown(tmp_path: Path):
    running = _action_task(
        tmp_path,
        task_id="unknown-running",
        team="unknown",
        status="RUNNING",
    )

    actions = build_dashboard_actions([running], browser_connected=False)

    assert actions["resume_teams"] == []
