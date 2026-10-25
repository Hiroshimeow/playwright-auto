"""Runtime-v2 -> live legacy HTTP adapter without touching a real browser.

These are on-loopback E2E fixtures. Source owners: RuntimeTaskRunner admission
and DashboardAPI /health; no foreign CDP, process, or user task is touched.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.audit import AuditTimeline
from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.state import RuntimeStateStore
from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner, RuntimeTaskRunnerError


@contextmanager
def legacy_http_fixture(health: dict):
    submissions = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            raw = json.dumps(health).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != "/api/tasks":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            submissions.append((self.headers.get("Idempotency-Key"), body))
            raw = json.dumps({"task_id": "legacy-task-1", "command_id": "cmd-1", "status": "queued"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, submissions
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def make_runner(tmp_path: Path, port: int) -> RuntimeTaskRunner:
    config = replace(load_runtime_config(repository_root=tmp_path), legacy_api_port=port)
    store = RuntimeStateStore(config)
    store.initialize()
    return RuntimeTaskRunner(config, store, AuditTimeline())


@pytest.mark.parametrize("unsafe", [
    {"independent_activation_enabled": True},
    {},  # Unknown old backend is not evidence that Independent Agent is off.
    {"independent_activation_enabled": False, "repository_root": "/foreign/repo"},
    {"independent_activation_enabled": False, "repository_root": "\x00"},
])
def test_existing_incompatible_legacy_http_backend_fails_closed_before_team_post(tmp_path, monkeypatch, unsafe):
    health = {"ok": True, "repository_root": str(tmp_path.resolve()),
              "independent_activation_enabled": False}
    health.update(unsafe)
    if not unsafe:
        health.pop("independent_activation_enabled", None)
    with legacy_http_fixture(health) as (port, submissions):
        runner = make_runner(tmp_path, port)
        monkeypatch.setattr(RuntimeTaskRunner, "_start_legacy_runtime_process", lambda _self: pytest.fail("never spawn"))
        with pytest.raises(RuntimeTaskRunnerError) as err:
            runner.submit_task(
                {"task": "review code", "roles": ["REVIEW"], "team": "owner-safe"},
                idempotency_key="owner-safe-review",
            )
        assert err.value.reason == "legacy_runtime_incompatible"
        assert submissions == []
        assert runner.state_store.list_task_records() == []


def test_existing_legacy_backend_with_worker_source_drift_rejects_team_submit(tmp_path, monkeypatch):
    health = {"ok": True, "repository_root": str(tmp_path.resolve()),
              "independent_activation_enabled": False, "worker_reload_required": True}
    with legacy_http_fixture(health) as (port, submissions):
        runner = make_runner(tmp_path, port)
        monkeypatch.setattr(RuntimeTaskRunner, "_start_legacy_runtime_process", lambda _self: pytest.fail("never spawn"))
        with pytest.raises(RuntimeTaskRunnerError) as err:
            runner.submit_task(
                {"task": "review code", "roles": ["REVIEW"]},
                idempotency_key="source-drift-review",
            )
        assert err.value.reason == "legacy_runtime_source_drift"
        assert submissions == []


def test_compatible_http_backend_team_submission_idempotency_and_record(tmp_path, monkeypatch):
    health = {"ok": True, "repository_root": str(tmp_path.resolve()),
              "independent_activation_enabled": False, "worker_reload_required": False}
    with legacy_http_fixture(health) as (port, submissions):
        runner = make_runner(tmp_path, port)
        monkeypatch.setattr(RuntimeTaskRunner, "_start_legacy_runtime_process", lambda _self: pytest.fail("never spawn"))
        task = {"task": "test v11 without real browser", "roles": ["DEV"]}
        first = runner.submit_task(task, idempotency_key="same-trigger-123")
        second = runner.submit_task(task, idempotency_key="same-trigger-123")
        assert first == second
        assert first["team"].startswith("hcuw-v2-team-")
        assert first["legacy_task_id"] == "legacy-task-1"
        assert len(submissions) == 1
        assert submissions[0][0] == "same-trigger-123"
        assert submissions[0][1]["workflow_mode"] == "team"
        assert submissions[0][1]["roles"] == ["DEV"]
        assert runner.state_store.list_task_records() == [first]


def test_legacy_dashboard_health_advertises_actual_execution_policy(tmp_path):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.dashboard_api import create_server
    from urllib.request import urlopen

    config = load_cdpa_config(None, repository_root=tmp_path)
    server = create_server(config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=4) as response:
            health = json.load(response)
        assert health["repository_root"] == str(tmp_path.resolve())
        assert health["independent_activation_enabled"] is config.independent_activation_enabled
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_runtime_v2_submits_to_actual_legacy_api_without_enabling_independent(tmp_path):
    """One real loopback API request, SQLite command, and runtime-v2 record."""
    from playwright_auto.cdpa_config import DEFAULT_CONFIG_PATH, DEFAULTS_ROOT, load_cdpa_config
    from playwright_auto.dashboard_api import create_server
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError

    settings = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assets = settings["paths"]
    for role, location in assets["constructors"].items():
        assets["constructors"][role] = str((DEFAULTS_ROOT / location).resolve())
    for key in ("independent_rule", "response_guide"):
        assets[key] = str((DEFAULTS_ROOT / assets[key]).resolve())
    settings.setdefault("independent_agents", {})["activation_enabled"] = False
    settings["independent_agents"]["seed_builtins"] = False
    settings.setdefault("repositories", {})["allowed_roots"] = [str(tmp_path.resolve())]
    (tmp_path / "cdpa.yaml").write_text(json.dumps(settings), encoding="utf-8")
    config = load_cdpa_config(None, repository_root=tmp_path)
    server = create_server(config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner = make_runner(tmp_path, server.server_port)
        task = {"task": "real queued task, do not open browser", "team": "actual-e2e", "roles": ["DEV"]}
        first = runner.submit_task(task, idempotency_key="real-e2e-key")
        second = runner.submit_task(task, idempotency_key="real-e2e-key")
        assert first == second
        assert len(runner.state_store.list_task_records()) == 1
        with server.application.db.connection() as connection:
            created = connection.execute(
                "SELECT kind, idempotency_key, payload_json FROM command_queue "
                "WHERE kind='create_task'"
            ).fetchall()
        assert len(created) == 1
        assert created[0]["idempotency_key"] == "real-e2e-key"
        queued = json.loads(created[0]["payload_json"])
        assert "workflow_mode" not in queued  # canonical TeamTask omits the legacy mode flag
        assert queued["roles"] == ["DEV"]
        assert queued["browser_endpoint_id"] == "9222"
        assert server.application.db.get_task_private(first["legacy_task_id"]) is None
        # Independent Agent history may be readable; creation remains disabled.
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/independent-agents",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(HTTPError) as err:
            urlopen(request, timeout=4)
        assert err.value.code == 410
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_actual_legacy_queue_deduplicates_crash_between_http_post_and_local_record(tmp_path, monkeypatch):
    """The legacy command idempotency key survives a lost runtime-v2 acknowledgment."""
    from playwright_auto.cdpa_config import DEFAULT_CONFIG_PATH, DEFAULTS_ROOT, load_cdpa_config
    from playwright_auto.dashboard_api import create_server

    settings = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assets = settings["paths"]
    for role, location in assets["constructors"].items():
        assets["constructors"][role] = str((DEFAULTS_ROOT / location).resolve())
    for key in ("independent_rule", "response_guide"):
        assets[key] = str((DEFAULTS_ROOT / assets[key]).resolve())
    settings.setdefault("independent_agents", {})["activation_enabled"] = False
    settings.setdefault("repositories", {})["allowed_roots"] = [str(tmp_path.resolve())]
    (tmp_path / "cdpa.yaml").write_text(json.dumps(settings), encoding="utf-8")
    server = create_server(load_cdpa_config(None, repository_root=tmp_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runner = make_runner(tmp_path, server.server_port)
        task = {"task": "crash-safety E2E", "roles": ["DEV"]}
        from playwright_auto.hcuw_runtime.state import RuntimeStateStore
        original = RuntimeStateStore.save_task_record

        def crash_after_remote_accept(_self, _record):
            raise OSError("simulated crash after legacy API accepted, before local record")

        monkeypatch.setattr(RuntimeStateStore, "save_task_record", crash_after_remote_accept)
        with pytest.raises(OSError, match="simulated crash after legacy API accepted"):
            runner.submit_task(task, idempotency_key="crash-e2e-identity")
        monkeypatch.setattr(RuntimeStateStore, "save_task_record", original)
        assert runner.state_store.list_task_records() == []

        restarted = make_runner(tmp_path, server.server_port)
        record = restarted.submit_task(task, idempotency_key="crash-e2e-identity")
        assert record["team"].startswith("hcuw-v2-team-")
        assert record["legacy_task_id"]
        assert restarted.state_store.list_task_records() == [record]
        with server.application.db.connection() as conn:
            rows = conn.execute(
                "SELECT command_id FROM command_queue WHERE kind='create_task' "
                "AND idempotency_key='crash-e2e-identity'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["command_id"] == record["legacy_command_id"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
