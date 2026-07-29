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


def test_goal_revision_detail_is_full_sanitized_and_summary_stays_compact(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["effective_goal"] = "Replacement goal"
    raw["goal_revisions"] = [{
        "revision": 1, "changed_at": "2026-07-25T01:00:00+00:00",
        "applies_from_hop_id": 3, "goal": "Replacement goal", "external_command_id": "cmd-private",
    }]
    projection = build_task_projection(raw, tasks=[raw])
    assert "effective_goal" not in projection.summary
    assert "goal_revisions" not in projection.summary
    assert projection.detail["effective_goal"] == "Replacement goal"
    assert projection.detail["goal_revisions"] == [{
        "revision": 1, "changed_at": "2026-07-25T01:00:00+00:00",
        "applies_from_hop_id": 3, "goal": "Replacement goal",
    }]
    assert "cmd-private" not in json.dumps(projection.detail)
