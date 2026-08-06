from __future__ import annotations

import ast
import hashlib
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from playwright_auto.cdpa_bootstraps import BootstrapCatalog
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import TaskProjection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.dashboard_api import create_server

from test_cdpa_core import write_config
from test_cdpa_independent_commands import fail_after


def projection(tmp_path: Path, *, report: Path | None = None) -> TaskProjection:
    summary = {
        "task_id": "task-a",
        "team": "alpha",
        "status": "RUNNING",
        "surface": "active",
        "updated_at": "2026-07-25T00:00:00+00:00",
        "version": 0,
    }
    reports = []
    private_reports = {}
    if report is not None:
        body = report.read_bytes()
        reports.append({"report_id": "r1", "url": "/api/reports/task-a/r1"})
        private_reports["r1"] = {
            "path": str(report),
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }
    return TaskProjection(
        task_id="task-a",
        team="alpha",
        status="RUNNING",
        surface="active",
        active_role="PLAN",
        updated_at=summary["updated_at"],
        summary=summary,
        detail={**summary, "timeline": [{"key": "1", "at": summary["updated_at"]}], "reports": reports},
        private={
            "manifest_path": str(tmp_path / ".plan" / "alpha" / "task-a.json"),
            "reports": private_reports,
            "maintenance_reports": {},
            "timeline": [{"key": "1", "at": summary["updated_at"]}],
        },
    )


def start_api(tmp_path: Path, *, report: Path | None = None):
    config = load_cdpa_config(None, repository_root=tmp_path)
    db = RuntimeDB(config.runtime_database)
    db.ensure_schema()
    db.replace_task_projections(
        [projection(tmp_path, report=report)],
        catalog={
            "complete": True,
            "discovered_at": "2026-07-25T00:00:00+00:00",
            "errors": [],
            "control_repository": str(tmp_path),
        },
    )
    server = create_server(config, host="127.0.0.1", port=0, db=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return config, db, server, thread


def request(server, method: str, path: str, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    payload = None if body is None else json.dumps(body).encode()
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(payload))
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, data


def test_api_module_has_no_taskstore_worker_or_browser_imports():
    source = Path("src/playwright_auto/dashboard_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        name.endswith(
            (
                "cdpa_store",
                "cdpa_worker",
                "cdpa_dependencies",
                "cdpa_team",
                "connection",
                "chatgpt",
            )
        )
        for name in imported
    )


def test_bootstrap_api_filters_anchor_ids_and_create_payload_is_optional(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    base = {
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_assistant_message_id": "22222222-2222-4222-8222-222222222222",
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
        "expires_at": None,
        "last_verified_at": None,
        "source_fingerprints": {},
    }
    catalog.upsert({**base, "bootstrap_id": "general-team-bootstrap", "enabled": True})
    catalog.upsert(
        {
            **base,
            "bootstrap_id": "disabled-bootstrap",
            "name": "Disabled",
            "conversation_id": "33333333-3333-4333-8333-333333333333",
            "terminal_assistant_message_id": "44444444-4444-4444-8444-444444444444",
            "enabled": False,
        }
    )
    try:
        status, _headers, body = request(server, "GET", "/api/bootstraps")
        assert status == 200
        payload = json.loads(body)
        assert payload == {
            "items": [
                {
                    "bootstrap_id": "general-team-bootstrap",
                    "name": "General Team Bootstrap",
                    "description": "Reusable task-neutral context",
                    "tags": ["general"],
                }
            ],
            "default_id": "general-team-bootstrap",
        }
        serialized = body.decode()
        assert base["conversation_id"] not in serialized
        assert base["terminal_assistant_message_id"] not in serialized

        common = {"task": "bootstrap payload", "repository": str(tmp_path)}
        status, _headers, data = request(
            server,
            "POST",
            "/api/tasks",
            body={**common, "bootstrap_id": "general-team-bootstrap"},
            headers={"Idempotency-Key": "bootstrap-payload"},
        )
        assert status == 202
        command = db.get_command(json.loads(data)["command_id"])
        assert command["payload"]["bootstrap_id"] == "general-team-bootstrap"

        status, _headers, data = request(
            server,
            "POST",
            "/api/tasks",
            body={**common, "task": "fresh payload", "bootstrap_id": None},
            headers={"Idempotency-Key": "fresh-payload"},
        )
        assert status == 202
        assert "bootstrap_id" not in db.get_command(json.loads(data)["command_id"])["payload"]

        for index, invalid in enumerate(("", "   ", 7, [], {})):
            status, _headers, _data = request(
                server,
                "POST",
                "/api/tasks",
                body={**common, "task": f"invalid bootstrap {index}", "bootstrap_id": invalid},
                headers={"Idempotency-Key": f"invalid-bootstrap-{index}"},
            )
            assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_agents_api_etag_and_workflow_catalog_commands(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    workflow = [
        {
            "route_key": "PLAN",
            "display_name": "Planner",
            "system_prompt": "PLAN_PROMPT",
            "is_system": True,
            "deleted_at": None,
        },
        {
            "route_key": "WF_123456789ABC",
            "display_name": "Researcher",
            "system_prompt": "CUSTOM_PROMPT",
            "is_system": False,
            "deleted_at": None,
        },
    ]
    db.put_snapshot("agents", {"workflow": workflow, "independent": []})
    try:
        status, headers, body = request(server, "GET", "/api/agents")
        assert status == 200
        assert json.loads(body) == {"workflow": workflow, "independent": []}
        etag = headers["ETag"]
        status, _headers, body = request(
            server,
            "GET",
            "/api/agents",
            headers={"If-None-Match": etag},
        )
        assert status == 304
        assert body == b""

        status, _headers, body = request(
            server,
            "POST",
            "/api/workflow-agents",
            body={"name": "Writer", "system_prompt": "WRITER_PROMPT"},
            headers={"Idempotency-Key": "create-writer"},
        )
        assert status == 202
        command = db.get_command(json.loads(body)["command_id"])
        assert command["kind"] == "create_workflow_agent"
        assert command["payload"] == {
            "name": "Writer",
            "system_prompt": "WRITER_PROMPT",
        }

        status, _headers, body = request(
            server,
            "POST",
            "/api/workflow-agents/WF_123456789ABC/settings",
            body={"name": "Researcher v2", "system_prompt": "CUSTOM_PROMPT_V2"},
            headers={"Idempotency-Key": "update-researcher"},
        )
        assert status == 202
        command = db.get_command(json.loads(body)["command_id"])
        assert command["kind"] == "update_workflow_agent"
        assert command["payload"]["route_key"] == "WF_123456789ABC"

        status, _headers, body = request(
            server,
            "POST",
            "/api/workflow-agents/WF_123456789ABC/delete",
            body={},
            headers={"Idempotency-Key": "delete-researcher"},
        )
        assert status == 202
        command = db.get_command(json.loads(body)["command_id"])
        assert command["kind"] == "delete_workflow_agent"
        assert command["payload"] == {"route_key": "WF_123456789ABC"}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_remove_parent_api_requires_version_and_queues_exact_relationship(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    try:
        status, _headers, body = request(
            server,
            "POST",
            "/api/tasks/task-a/parents/parent-b/remove",
            body={"expected_task_version": 0},
            headers={"Idempotency-Key": "remove-parent-b"},
        )
        assert status == 202
        command = db.get_command(json.loads(body)["command_id"])
        assert command["kind"] == "remove_parent_dependency"
        assert command["task_id"] == "task-a"
        assert command["expected_task_version"] == 0
        assert command["payload"] == {"parent_task_id": "parent-b"}

        invalid_versions = (
            {},
            {"expected_task_version": "0"},
            {"expected_task_version": True},
            {"expected_task_version": -1},
        )
        for index, invalid in enumerate(invalid_versions):
            status, _headers, body = request(
                server,
                "POST",
                "/api/tasks/task-a/parents/parent-c/remove",
                body=invalid,
                headers={"Idempotency-Key": f"remove-parent-invalid-{index}"},
            )
            assert status == 400
            assert json.loads(body)["error"]["code"] == "invalid_request"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_board_etag_304_and_worker_offline_last_known_good(tmp_path: Path):
    _config, _db, server, thread = start_api(tmp_path)
    try:
        status, headers, body = request(server, "GET", "/api/tasks")
        assert status == 200
        payload = json.loads(body)
        assert payload["generation"] == 1
        assert payload["items"][0]["task_id"] == "task-a"
        assert "control_repository" not in payload["catalog"]
        assert str(tmp_path) not in body.decode()
        etag = headers["ETag"]

        status, _headers, body = request(
            server, "GET", "/api/tasks", headers={"If-None-Match": etag}
        )
        assert status == 304
        assert body == b""

        status, _headers, body = request(server, "GET", "/health")
        health = json.loads(body)
        assert status == 200
        assert health["ok"] is True
        assert health["worker_online"] is False
        assert health["worker_stale"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_tasks_api_returns_worker_projected_waiting_order_without_derivation(
    tmp_path: Path,
):
    _config, db, server, thread = start_api(tmp_path)
    ranked = projection(tmp_path)
    ranked.summary.update(
        status="WAITING",
        column="WAITING",
        waiting_order={"rank": 2, "fifo": 7, "intervention": None},
    )
    ranked.detail.update(ranked.summary)
    db.upsert_task_projections([ranked])
    try:
        status, headers, body = request(server, "GET", "/api/tasks")
        assert status == 200
        payload = json.loads(body)
        assert payload["items"][0]["waiting_order"] == {
            "rank": 2,
            "fifo": 7,
            "intervention": None,
        }
        first_etag = headers["ETag"]

        ranked.summary["waiting_order"] = {
            "rank": None,
            "fifo": 7,
            "intervention": "Intervention required: missing dependency parent-a",
        }
        ranked.detail.update(ranked.summary)
        db.upsert_task_projections([ranked])

        status, headers, body = request(
            server,
            "GET",
            "/api/tasks",
            headers={"If-None-Match": first_etag},
        )
        assert status == 200
        assert headers["ETag"] != first_etag
        assert json.loads(body)["items"][0]["waiting_order"]["rank"] is None
    finally:
        server.shutdown()
        thread.join(timeout=5)






def test_mutations_require_idempotency_and_reuse_identical_command(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    body = {"task": "new task", "requested_team": "new", "repository": str(tmp_path)}
    try:
        status, _headers, _data = request(server, "POST", "/api/tasks", body=body)
        assert status == 400

        headers = {"Idempotency-Key": "create-key"}
        status, _headers, data = request(server, "POST", "/api/tasks", body=body, headers=headers)
        assert status == 202
        first = json.loads(data)
        assert first["status"] == "queued"
        assert first["task_id"].startswith("cdpa-")

        status, _headers, data = request(server, "POST", "/api/tasks", body=body, headers=headers)
        assert status == 202
        assert json.loads(data) == first
        command = db.get_command(first["command_id"])
        assert command["status"] == "queued"
        assert "roles" not in command["payload"]

        explicit_body = {**body, "task": "selected roles", "roles": ["REVIEW", "PLAN"]}
        status, _headers, explicit_data = request(
            server,
            "POST",
            "/api/tasks",
            body=explicit_body,
            headers={"Idempotency-Key": "selected-role-key"},
        )
        assert status == 202
        explicit = json.loads(explicit_data)
        assert db.get_command(explicit["command_id"])["payload"]["roles"] == [
            "PLAN",
            "REVIEW",
        ]

        for index, roles in enumerate(([], ["DEV"], ["PLAN", "UNKNOWN"], ["PLAN", "PLAN"])):
            status, _headers, _data = request(
                server,
                "POST",
                "/api/tasks",
                body={**body, "task": f"bad roles {index}", "roles": roles},
                headers={"Idempotency-Key": f"bad-role-key-{index}"},
            )
            assert status == 400

        status, _headers, _data = request(
            server,
            "POST",
            "/api/tasks",
            body={**body, "task": "different"},
            headers=headers,
        )
        assert status == 409
    finally:
        server.shutdown()
        thread.join(timeout=5)






def test_tasks_api_is_file_only_for_same_and_cross_workspace(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    execution_repository = tmp_path.parent / f"{tmp_path.name}-api-execution"
    execution_repository.mkdir()
    try:
        cases = (
            ("same-root-file", tmp_path, "file"),
            ("cross-root-default", execution_repository, None),
            ("cross-root-file", execution_repository, "file"),
        )
        for key, repository, requested_mode in cases:
            body = {"task": key, "repository": str(repository)}
            if requested_mode is not None:
                body["report_mode"] = requested_mode
            status, _headers, data = request(
                server,
                "POST",
                "/api/tasks",
                body=body,
                headers={"Idempotency-Key": key},
            )
            assert status == 202
            command = db.get_command(json.loads(data)["command_id"])
            assert command["payload"]["report_mode"] == "file"

        for mode in ("inline", "remote"):
            status, _headers, _data = request(
                server,
                "POST",
                "/api/tasks",
                body={
                    "task": f"invalid-report-mode-{mode}",
                    "repository": str(execution_repository),
                    "report_mode": mode,
                },
                headers={"Idempotency-Key": f"invalid-report-mode-{mode}"},
            )
            assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_independent_create_accepts_initial_triggers_and_preserves_idempotency(
    tmp_path: Path,
):
    _config, db, server, thread = start_api(tmp_path)
    body = {
        "name": "Initial Trigger Agent",
        "system_prompt": "Inspect only matching work.",
        "mode": "Independent",
        "trigger_settings": {
            "task_done": True,
            "interval_minutes": 20,
            "role_completed": ["dev"],
            "teams": ["unused-team"],
            "states": ["blocked"],
        },
    }
    headers = {"Idempotency-Key": "initial-trigger-agent"}
    try:
        status, _headers, data = request(
            server,
            "POST",
            "/api/independent-agents",
            body=body,
            headers=headers,
        )
        assert status == 202
        first = json.loads(data)
        command = db.get_command(first["command_id"])
        assert command["payload"]["trigger_settings"] == {
            "recovery": False,
            "interval_minutes": 20,
            "task_done": True,
            "role_completed": ["DEV"],
            "teams": ["unused-team"],
            "states": ["BLOCKED"],
            "check_all": False,
        }

        status, _headers, data = request(
            server,
            "POST",
            "/api/independent-agents",
            body=body,
            headers=headers,
        )
        assert status == 202
        assert json.loads(data) == first

        status, _headers, data = request(
            server,
            "POST",
            "/api/independent-agents",
            body={**body, "system_prompt": "Changed prompt."},
            headers=headers,
        )
        assert status == 409
        assert json.loads(data)["error"]["code"] == "idempotency_conflict"

        invalid_payloads = (
            {**body, "trigger_settings": {"unknown": True}},
            {**body, "trigger_settings": {"interval_minutes": 19}},
            {**body, "trigger_settings": {"role_completed": ["UNKNOWN"]}},
            {**body, "trigger_settings": {"states": ["BLOCKED"]}},
            {**body, "unexpected": True},
        )
        for index, invalid in enumerate(invalid_payloads):
            status, _headers, data = request(
                server,
                "POST",
                "/api/independent-agents",
                body=invalid,
                headers={"Idempotency-Key": f"invalid-agent-{index}"},
            )
            assert status == 400
            assert json.loads(data)["error"]["code"] == "invalid_request"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_report_uses_exact_hashed_private_locator(tmp_path: Path):
    report = tmp_path / ".plan" / "alpha" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# report\n", encoding="utf-8")
    _config, _db, server, thread = start_api(tmp_path, report=report)
    try:
        status, headers, body = request(server, "GET", "/api/reports/task-a/r1")
        assert status == 200
        assert headers["Content-Type"].startswith("text/markdown")
        assert body == b"# report\n"

        report.write_text("changed", encoding="utf-8")
        status, _headers, _body = request(server, "GET", "/api/reports/task-a/r1")
        assert status == 409
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_change_goal_endpoint_enqueues_strict_idempotent_command(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    headers = {"Idempotency-Key": "goal-key"}
    try:
        status, _headers, data = request(server, "POST", "/api/tasks/task-a/goal", body={"goal": "Full replacement", "expected_task_version": 4}, headers=headers)
        assert status == 202
        queued = json.loads(data)
        command = db.get_command(queued["command_id"])
        assert command["kind"] == "change_goal"
        assert command["task_id"] == "task-a"
        assert command["payload"] == {"goal": "Full replacement"}
        assert command["expected_task_version"] == 4
        status, _headers, repeated = request(server, "POST", "/api/tasks/task-a/goal", body={"goal": "Full replacement", "expected_task_version": 4}, headers=headers)
        assert status == 202
        assert json.loads(repeated) == queued
        bad_bodies = ({"goal": "   "}, {"goal": "x", "extra": True}, {"goal": "x", "expected_task_version": "bad"})
        for index, body in enumerate(bad_bodies):
            status, _headers, _data = request(server, "POST", "/api/tasks/task-a/goal", body=body, headers={"Idempotency-Key": f"bad-{index}"})
            assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)
