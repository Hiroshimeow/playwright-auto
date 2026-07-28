from __future__ import annotations

import ast
import hashlib
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import TaskProjection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.dashboard_api import create_server

from test_cdpa_core import write_config
from test_cdpa_independent_commands import fail_after


class FakeTelemetry:
    def snapshot(self):
        return {
            "sample_version": 1,
            "sampled_at": "2026-07-25T00:00:00+00:00",
            "host": {"cpu_percent": 0.0},
            "processes": {"api": None, "worker": None},
            "gpu": None,
        }


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
    server = create_server(config, host="127.0.0.1", port=0, db=db, telemetry=FakeTelemetry())
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


def test_dashboard_actions_endpoint_is_projection_only_etagged_and_compact(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    payload = {
        "dependency_teams": [
            {
                "team": "alpha",
                "ambiguous": False,
                "tasks": [{"task_id": "task-a", "title": "Task A", "status": "RUNNING"}],
            }
        ],
        "resume_teams": [
            {"team": "alpha", "task_id": "task-a", "title": "Task A", "status": "BLOCKED", "reason": "blocked"}
        ],
        "reuse_teams": [{"team": "alpha", "status": "available"}],
    }
    db.put_snapshot("dashboard_actions", payload)
    try:
        status, headers, body = request(server, "GET", "/api/dashboard-actions")
        assert status == 200
        assert json.loads(body) == payload
        assert len(body) < 16 * 1024
        etag = headers["ETag"]

        status, _headers, body = request(
            server,
            "GET",
            "/api/dashboard-actions",
            headers={"If-None-Match": etag},
        )
        assert status == 304
        assert body == b""
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_dashboard_actions_endpoint_has_fail_closed_empty_default(tmp_path: Path):
    _config, _db, server, thread = start_api(tmp_path)
    try:
        status, headers, body = request(server, "GET", "/api/dashboard-actions")
        assert status == 200
        assert headers["ETag"] == '"dashboard-actions-0"'
        assert json.loads(body) == {
            "degraded": True,
            "dependency_teams": [],
            "resume_teams": [],
            "reuse_teams": [],
        }
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
        assert db.get_command(first["command_id"])["status"] == "queued"

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


def test_settings_endpoint_reaches_terminal_applied_command_status(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    agent = store.create_independent_agent(
        "API Watcher",
        system_prompt="Review API-triggered checks.",
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime(startup=False)
    server = create_server(
        config,
        host="127.0.0.1",
        port=0,
        db=worker.runtime_db,
        telemetry=FakeTelemetry(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, data = request(
            server,
            "POST",
            f"/api/independent-agents/{agent['task_id']}/settings",
            body={
                "enabled": False,
                "trigger_settings": {"interval_minutes": 60},
            },
            headers={"Idempotency-Key": "api-settings-key"},
        )
        queued = json.loads(data)
        assert status == 202
        assert queued["status"] == "queued"

        with fail_after(2):
            command = worker.dispatch_command_once()

        assert command["status"] == "applied"
        status, _headers, data = request(
            server,
            "GET",
            f"/api/commands/{queued['command_id']}",
        )
        terminal = json.loads(data)
        assert status == 200
        assert terminal["status"] == "applied"
        assert terminal["error"] is None
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_concurrent_identical_create_requests_share_one_atomic_reservation(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    body = {"task": "concurrent create", "requested_team": "same", "repository": str(tmp_path)}
    headers = {"Idempotency-Key": "concurrent-create-key"}
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(
                pool.map(
                    lambda _index: request(
                        server, "POST", "/api/tasks", body=body, headers=headers
                    ),
                    range(24),
                )
            )
        assert {status for status, _headers, _data in responses} == {202}
        payloads = [json.loads(data) for _status, _headers, data in responses]
        assert len({item["command_id"] for item in payloads}) == 1
        assert len({item["task_id"] for item in payloads}) == 1
        command = db.get_command(payloads[0]["command_id"])
        assert command["status"] == "queued"
        assert command["task_id"] == payloads[0]["task_id"]
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


def test_invalid_public_identifiers_and_cursors_return_400(tmp_path: Path):
    _config, _db, server, thread = start_api(tmp_path)
    try:
        status, _headers, _body = request(server, "GET", "/api/tasks/bad%2Fid")
        assert status == 400
        status, _headers, _body = request(
            server, "GET", "/api/tasks/task-a/timeline?before=%%%"
        )
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_report_rejects_symlinked_parent_even_when_target_stays_in_plan_root(tmp_path: Path):
    target = tmp_path / ".plan" / "target"
    target.mkdir(parents=True)
    (target / "report.md").write_text("safe bytes", encoding="utf-8")
    link_parent = tmp_path / ".plan" / "alpha"
    link_parent.mkdir(parents=True)
    (link_parent / "linked").symlink_to(target, target_is_directory=True)
    report = link_parent / "linked" / "report.md"
    _config, _db, server, thread = start_api(tmp_path, report=report)
    try:
        status, _headers, _body = request(server, "GET", "/api/reports/task-a/r1")
        assert status == 403
    finally:
        server.shutdown()
        thread.join(timeout=5)
