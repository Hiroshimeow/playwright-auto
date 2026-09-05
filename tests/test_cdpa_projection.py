from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_projection import (
    build_dashboard_actions,
    build_task_projection,
    build_waiting_order,
)
from playwright_auto.cdpa_team import exact_team_ready_waiters
from playwright_auto.dashboard_api import APIError, DashboardAPI


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


def test_projection_hydrates_missing_file_report_evidence_inside_task_team_root(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["manifest_path"] = str(tmp_path / ".plan" / "alpha" / "task-a" / "task-a.json")
    report = raw["reports"][0]
    report_path = Path(report["path"])
    report["path"] = str(report_path.relative_to(tmp_path))
    report["sha256"] = None
    report["size"] = None

    projection = build_task_projection(raw, tasks=[raw])

    assert projection.private["reports"]["r1"] == {
        "path": str(report_path.resolve()),
        "sha256": hashlib.sha256(b"report").hexdigest(),
        "size": 6,
        "repository": str(tmp_path.resolve()),
        "team": "alpha",
        "availability": "available",
    }


def test_projection_hydrates_missing_file_report_evidence_from_cross_workspace_repository(tmp_path: Path):
    control = tmp_path / "control"
    execution = tmp_path / "execution"
    control.mkdir()
    execution.mkdir()
    raw = raw_task(control)
    raw["repository"] = str(execution)
    raw["manifest_path"] = str(control / ".plan" / "alpha" / "task-a" / "task-a.json")
    report_path = execution / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("cross-workspace report", encoding="utf-8")
    report = raw["reports"][0]
    report["path"] = str(report_path.relative_to(execution))
    report["sha256"] = None
    report["size"] = None

    projection = build_task_projection(raw, tasks=[raw])
    locator = projection.private["reports"]["r1"]

    assert projection.private["repository"] == str(execution)
    assert locator["sha256"] == hashlib.sha256(b"cross-workspace report").hexdigest()
    assert locator["size"] == len(b"cross-workspace report")

    class ProjectionDB:
        def ensure_schema(self):
            pass

        def get_task_private(self, task_id):
            return projection.private if task_id == projection.task_id else None

    api = DashboardAPI(
        SimpleNamespace(
            repository_root=control,
            plans_root=control / ".plan",
            repository_allowed_roots=(tmp_path.resolve(),),
        ),
        db=ProjectionDB(),
    )
    assert api.report_bytes("task-a", "r1", maintenance=False) == b"cross-workspace report"


def test_projection_recovers_legacy_report_from_strict_declared_repository(tmp_path: Path):
    control = tmp_path / "control"
    execution = tmp_path / "execution"
    control.mkdir()
    execution.mkdir()
    raw = raw_task(control)
    raw["task_text"] = f"QMH task. Target repository {execution}, use @mcp-g8 only.\nDetails"
    raw["repository"] = str(control)
    control_report = Path(raw["reports"][0]["path"])
    control_report.unlink()
    report = raw["reports"][0]
    report["path"] = f".plan/alpha/{control_report.name}"
    report["sha256"] = None
    report["size"] = None
    execution_report = execution / report["path"]
    execution_report.parent.mkdir(parents=True)
    execution_report.write_bytes(b"legacy qmh report")

    projection = build_task_projection(
        raw,
        tasks=[raw],
        repository_allowed_roots=(tmp_path,),
    )
    public = projection.detail["reports"][0]
    locator = projection.private["reports"]["r1"]

    assert public["availability"] == "available"
    assert public["url"] == "/api/reports/task-a/r1"
    assert locator == {
        "path": str(execution_report.resolve()),
        "sha256": hashlib.sha256(b"legacy qmh report").hexdigest(),
        "size": len(b"legacy qmh report"),
        "repository": str(execution.resolve()),
        "team": "alpha",
        "availability": "available",
    }

    class ProjectionDB:
        def ensure_schema(self):
            pass

        def get_task_private(self, task_id):
            return projection.private if task_id == projection.task_id else None

    api = DashboardAPI(
        SimpleNamespace(
            repository_root=control,
            plans_root=control / ".plan",
            repository_allowed_roots=(tmp_path.resolve(),),
        ),
        db=ProjectionDB(),
    )
    assert api.report_bytes("task-a", "r1", maintenance=False) == b"legacy qmh report"


def test_projection_legacy_report_fallback_fails_closed(tmp_path: Path):
    control = tmp_path / "control"
    execution = tmp_path / "execution"
    control.mkdir()
    execution.mkdir()

    def projected(*, task_text: str, report_path: str, make_file: bool = False, symlink: bool = False, allowed=(tmp_path,)):
        raw = raw_task(control)
        Path(raw["reports"][0]["path"]).unlink(missing_ok=True)
        raw["repository"] = str(control)
        raw["task_text"] = task_text
        raw["reports"][0].update({"path": report_path, "sha256": None, "size": None})
        target = execution / report_path
        if make_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            if symlink:
                outside = execution / "outside.md"
                outside.write_text("outside", encoding="utf-8")
                target.symlink_to(outside)
            else:
                target.write_text("candidate", encoding="utf-8")
        return build_task_projection(raw, tasks=[raw], repository_allowed_roots=allowed)

    declared = f"Legacy. Target repository {execution}, use @mcp-g8 only."
    cases = [
        projected(task_text=declared, report_path=".plan/alpha/missing.md"),
        projected(task_text=declared, report_path=".plan/other/wrong-team.md", make_file=True),
        projected(task_text=declared, report_path="not-a-report-path"),
        projected(
            task_text=f"Legacy. Repository {execution}, Target repository {control}, use @mcp-g8 only.",
            report_path=".plan/alpha/ambiguous.md",
            make_file=True,
        ),
        projected(
            task_text=declared,
            report_path=".plan/alpha/not-allowed.md",
            make_file=True,
            allowed=(control,),
        ),
    ]
    symlink_projection = projected(
        task_text=declared,
        report_path=".plan/alpha/symlink.md",
        make_file=True,
        symlink=True,
    )
    cases.append(symlink_projection)

    for projection in cases:
        public = projection.detail["reports"][0]
        assert public["availability"] == "unavailable"
        assert "url" not in public
        assert projection.private["reports"]["r1"]["availability"] == "unavailable"


def test_projection_marks_strict_windows_remote_report_unmirrored(tmp_path: Path):
    raw = raw_task(tmp_path)
    Path(raw["reports"][0]["path"]).unlink()
    raw["task_text"] = (
        "Screens task\n"
        "HARD EXECUTION AUTHORITY\n"
        r"- Actual product repository: E:\python_project\Screens-Trans-Chatbot on Windows ThinkBook."
        "\n- Use @mcp-thinkbook ONLY for product source and role reports."
    )
    report = raw["reports"][0]
    report.update({"path": ".plan/alpha/alpha-plan_turn1_task-a.md", "sha256": None, "size": None})

    projection = build_task_projection(raw, tasks=[raw], repository_allowed_roots=(tmp_path,))
    public = projection.detail["reports"][0]
    locator = projection.private["reports"]["r1"]

    assert public["availability"] == "remote_unmirrored"
    assert "url" not in public
    assert locator["availability"] == "remote_unmirrored"

    class ProjectionDB:
        def ensure_schema(self):
            pass

        def get_task_private(self, task_id):
            return projection.private

    api = DashboardAPI(
        SimpleNamespace(
            repository_root=tmp_path,
            plans_root=tmp_path / ".plan",
            repository_allowed_roots=(tmp_path.resolve(),),
        ),
        db=ProjectionDB(),
    )
    try:
        api.report_bytes("task-a", "r1", maintenance=False)
    except APIError as exc:
        assert exc.status == 409
        assert exc.code == "report_remote_unmirrored"
    else:
        raise AssertionError("remote unmirrored report must not be served")


def test_independent_projection_identifies_only_canonical_builtins(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["independent"] = {
        "agent_key": "maintainers",
        "agent_name": "Maintainers",
        "agent_generation": 1,
        "enabled": True,
    }

    builtin = build_task_projection(raw, tasks=[raw])
    assert builtin.summary["agent"]["is_builtin"] is True
    assert builtin.detail["agent"]["is_builtin"] is True

    raw["independent"]["agent_key"] = "monitor"
    raw["independent"]["agent_name"] = "Monitor"
    assert build_task_projection(raw, tasks=[raw]).summary["agent"]["is_builtin"] is True

    raw["independent"]["agent_key"] = "release-watcher"
    raw["independent"]["agent_name"] = "Release Watcher"
    custom = build_task_projection(raw, tasks=[raw])
    assert custom.summary["agent"]["is_builtin"] is False
    assert custom.detail["agent"]["is_builtin"] is False


def test_independent_projection_exposes_run_count_and_last_run(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["independent"] = {
        "agent_key": "monitor",
        "agent_name": "Monitor",
        "agent_generation": 1,
        "enabled": True,
        "active_event": {
            "event_key": "interval:monitor:3",
            "trigger_type": "interval",
            "occurred_at": "2026-07-25T03:00:00+00:00",
        },
        "job_history": [
            {"event_key": "interval:monitor:1", "released_at": "2026-07-25T01:05:00+00:00"},
            {"event_key": "interval:monitor:2", "released_at": "2026-07-25T02:05:00+00:00"},
            {"event_key": "interval:monitor:2", "released_at": "2026-07-25T02:06:00+00:00"},
        ],
    }

    projection = build_task_projection(raw, tasks=[raw])

    assert projection.summary["agent"]["run_count"] == 3
    assert projection.summary["agent"]["run_count_truncated"] is False
    assert projection.summary["agent"]["last_run_at"] == "2026-07-25T03:00:00+00:00"

    raw["independent"]["active_event"] = None
    raw["independent"]["job_history"] = [
        {"event_key": f"interval:monitor:{index}", "released_at": f"2026-07-25T02:05:{index % 60:02d}+00:00"}
        for index in range(200)
    ]
    capped = build_task_projection(raw, tasks=[raw])
    assert capped.summary["agent"]["run_count"] == 200
    assert capped.summary["agent"]["run_count_truncated"] is True


def test_projection_exposes_fixed_elapsed_end_for_non_running_tasks(tmp_path: Path):
    cases = {
        "DONE": ("completed_at", "2026-07-25T04:00:00+00:00"),
        "BLOCKED": ("blocked_at", "2026-07-25T03:00:00+00:00"),
        "STOPPED": ("stopped_at", "2026-07-25T02:00:00+00:00"),
    }
    for status, (field, end_at) in cases.items():
        raw = raw_task(tmp_path)
        raw.update(status=status, started_at="2026-07-25T01:00:00+00:00")
        raw[field] = end_at

        projection = build_task_projection(raw, tasks=[raw])

        assert projection.summary["elapsed_end_at"] == end_at


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














def test_projection_active_role_clock_uses_active_hop_creation_time(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw["started_at"] = "2026-07-25T00:00:00+00:00"
    raw["last_role_activity_at"] = "2026-07-25T00:09:00+00:00"
    raw["hops"][1]["timestamps"] = {
        "created_at": "2026-07-25T00:06:00+00:00",
        "sending_at": "2026-07-25T00:09:00+00:00",
    }

    projection = build_task_projection(raw, tasks=[raw])

    assert projection.summary["started_at"] == "2026-07-25T00:00:00+00:00"
    assert projection.summary["active_role_started_at"] == "2026-07-25T00:06:00+00:00"
    assert projection.summary["effective_activity_at"] == "2026-07-25T00:09:00+00:00"
    assert projection.summary["running_elapsed_seconds"] is None
    assert projection.summary["running_since"] is None
    assert projection.summary["active_role_running_elapsed_seconds"] is None
    assert projection.summary["active_role_running_since"] is None

def test_projection_resolves_parent_dependency_team_names(tmp_path: Path):
    raw = raw_task(tmp_path)
    parent = raw_task(tmp_path)
    parent["task_id"] = "parent-task-id"
    parent["team"] = "human-readable-parent-team"
    raw["depends_on_task_ids"] = ["parent-task-id"]

    projection = build_task_projection(raw, tasks=[raw, parent])

    assert projection.detail["dependencies"] == [
        {"task_id": "parent-task-id", "team": "human-readable-parent-team"}
    ]


def test_projection_rewrites_waiting_dependency_ids_to_team_names(tmp_path: Path):
    raw = raw_task(tmp_path)
    raw.update(
        status="WAITING",
        waiting_code="dependency",
        waiting_reason="Waiting for dependencies: parent-task-id",
        waiting={"reason": "dependency", "since": raw["updated_at"]},
    )
    parent = raw_task(tmp_path)
    parent["task_id"] = "parent-task-id"
    parent["team"] = "human-readable-parent-team"

    projection = build_task_projection(raw, tasks=[raw, parent])

    assert projection.summary["waiting_reason"] == (
        "Waiting for dependencies: human-readable-parent-team"
    )
    assert projection.summary["primary_problem"]["message"] == (
        "Waiting for dependencies: human-readable-parent-team"
    )


def test_projection_summary_is_compact_and_deterministic(tmp_path: Path):
    raw = raw_task(tmp_path)
    first = build_task_projection(raw, tasks=[raw])
    second = build_task_projection(raw, tasks=[raw])

    assert first == second
    assert len(json.dumps(first.summary, separators=(",", ":")).encode()) <= 4096
    assert set(first.summary) <= {
        "task_id", "team", "status", "column", "surface", "active_role",
        "active_hop_id", "active_action", "created_at", "started_at", "updated_at",
        "effective_activity_at", "active_role_started_at", "running_elapsed_seconds",
        "running_since", "active_role_running_elapsed_seconds", "active_role_running_since",
        "elapsed_end_at",
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
    roles: tuple[str, ...] = ("DEV",),
) -> dict:
    task = raw_task(tmp_path)
    template = next(iter(task["roles"].values()))
    task["roles"] = {
        role: {**template, "physical_role": f"{team}-{role.lower()}"}
        for role in roles
    }
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
        _action_task(
            tmp_path,
            task_id="dep-one",
            team="single",
            status="DONE",
            title="Single dependency",
            roles=("PLAN", "REVIEW"),
        ),
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
        _action_task(
            tmp_path,
            task_id="mixed-a",
            team="mixed",
            status="DONE",
            roles=("PLAN", "REVIEW"),
        ),
        _action_task(
            tmp_path,
            task_id="mixed-b",
            team="mixed",
            status="DONE",
            roles=("PLAN", "DEV"),
        ),
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
        {"team": "blocked", "status": "available", "roles": ["DEV"]},
        {"team": "offline", "status": "available", "roles": ["DEV"]},
        {"team": "online", "status": "available", "roles": ["DEV"]},
        {"team": "paused", "status": "available", "roles": ["DEV"]},
        {"team": "reuse", "status": "available", "roles": ["DEV"]},
        {"team": "single", "status": "available", "roles": ["PLAN", "REVIEW"]},
        {"team": "stopped", "status": "available", "roles": ["DEV"]},
    ]
    assert "mixed" not in {item["team"] for item in actions["reuse_teams"]}
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
