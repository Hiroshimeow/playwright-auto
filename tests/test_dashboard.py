from __future__ import annotations

import ast
import http.client
import json
import threading
from pathlib import Path
from urllib.parse import urlencode

from playwright_auto import dashboard as dashboard_module
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.dashboard import ASSET_ROOT, DASHBOARD_HTML_PATH, create_server


class UpstreamHandler(__import__("http.server").server.BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _handle(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size) if size else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {"method": self.command, "path": self.path, "body": body, "headers": dict(self.headers.items())}
        )
        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "body": body.decode(),
                "idempotency": self.headers.get("Idempotency-Key"),
            }
        ).encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"upstream-1"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


def start_upstream():
    server = __import__("http.server").server.ThreadingHTTPServer(
        ("127.0.0.1", 0), UpstreamHandler
    )
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class SSEUpstreamHandler(__import__("http.server").server.BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.end_headers()
        self.wfile.write(b"event: stream.ready\ndata: {\"seq\":0}\n\n")
        self.wfile.flush()
        self.server.first_sent.set()  # type: ignore[attr-defined]
        self.server.release.wait(timeout=5)  # type: ignore[attr-defined]
        self.wfile.write(b"id: epoch:1\nevent: audit.append\ndata: {\"seq\":1}\n\n")
        self.wfile.flush()


def start_sse_upstream():
    server = __import__("http.server").server.ThreadingHTTPServer(
        ("127.0.0.1", 0), SSEUpstreamHandler,
    )
    server.first_sent = threading.Event()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_frontend(tmp_path: Path, *, api_port: int, auth_password: str | None = None):
    config = load_cdpa_config(None, repository_root=tmp_path)
    config = __import__("dataclasses").replace(config, dashboard_api_port=api_port)
    server = create_server(config, host="127.0.0.1", port=0, auth_password=auth_password)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def request(server, method: str, path: str, *, body: bytes | None = None, headers=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=5
    )
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, data


def test_dashboard_module_is_static_proxy_only():
    source = Path("src/playwright_auto/dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {
        "playwright_auto.cdpa_store",
        "playwright_auto.cdpa_worker",
        "playwright_auto.cdpa_runtime_db",
        "playwright_auto.chatgpt",
        "playwright_auto.connection",
    }
    assert modules.isdisjoint(forbidden)
    assert "DashboardMonitor" not in source
    assert "DashboardStore" not in source
    assert "snapshot(" not in source


def test_frontend_health_and_static_assets_work_with_api_stopped(tmp_path: Path):
    server, thread = start_frontend(tmp_path, api_port=65530)
    try:
        status, _headers, body = request(server, "GET", "/health")
        health = json.loads(body)
        assert status == 200
        assert health["ok"] is True
        assert health["service"] == "frontend"

        status, headers, body = request(server, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert body == DASHBOARD_HTML_PATH.read_bytes()

        status, headers, body = request(server, "GET", "/assets/app.js")
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert body == (ASSET_ROOT / "app.js").read_bytes()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_proxy_preserves_method_query_body_status_content_type_etag_and_idempotency(
    tmp_path: Path,
):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, headers, body = request(
            server,
            "POST",
            "/api/tasks?view=compact",
            body=b'{"task":"x"}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": "12",
                "Idempotency-Key": "same-key",
            },
        )
        payload = json.loads(body)
        assert status == 207
        assert headers["Content-Type"] == "application/json"
        assert headers["ETag"] == '"upstream-1"'
        assert payload == {
            "method": "POST",
            "path": "/api/tasks?view=compact",
            "body": '{"task":"x"}',
            "idempotency": "same-key",
        }
    finally:
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_proxy_streams_sse_without_buffering_until_upstream_eof(tmp_path: Path):
    upstream, upstream_thread = start_sse_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=5,
    )
    try:
        connection.request("GET", "/api/tasks/task-a/live/stream?role=DEV")
        response = connection.getresponse()
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-cache, no-transform"
        assert "Content-Length" not in response.headers
        assert upstream.first_sent.wait(timeout=1)

        assert response.readline() == b"event: stream.ready\n"
        assert response.readline() == b'data: {"seq":0}\n'
        assert response.readline() == b"\n"

        upstream.release.set()
        assert response.readline() == b"id: epoch:1\n"
        assert response.readline() == b"event: audit.append\n"
        assert response.readline() == b'data: {"seq":1}\n'
        assert response.readline() == b"\n"
    finally:
        connection.close()
        upstream.release.set()
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_proxy_returns_structured_503_when_api_is_unavailable(tmp_path: Path):
    server, thread = start_frontend(tmp_path, api_port=65530)
    try:
        status, headers, body = request(server, "GET", "/api/tasks")
        assert status == 503
        assert headers["Content-Type"].startswith("application/json")
        payload = json.loads(body)
        assert payload["error"]["code"] == "api_unavailable"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_public_auth_rejects_anonymous_before_proxy_even_with_alternate_host(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1], auth_password="correct")
    try:
        status, _headers, body = request(
            server, "GET", "/api/state", headers={"Host": "cdpa.hcu-lab.me"}
        )
        assert status == 401
        assert json.loads(body)["error"]["code"] == "authentication_required"

        status, _headers, _body = request(
            server,
            "GET",
            "/api/state",
            headers={"Host": "alternate.invalid", "Cf-Ray": "test-edge-request"},
        )
        assert status == 401
        assert upstream.requests == []
    finally:
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_public_login_rejects_wrong_password_and_accepts_opaque_secure_session(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1], auth_password="correct")
    public = {
        "Host": "cdpa.hcu-lab.me",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://cdpa.hcu-lab.me",
    }
    try:
        wrong = urlencode({"password": "wrong"}).encode()
        status, headers, body = request(server, "POST", "/auth/login", body=wrong, headers=public)
        assert status == 401
        assert "Set-Cookie" not in headers
        login_html = body.decode()
        assert "あなたは誰？<br>ここで何をしているの？" in login_html
        assert "https://" not in login_html
        assert "<script" not in login_html
        assert "@media (max-width: 480px)" in login_html
        assert upstream.requests == []

        correct = urlencode({"password": "correct"}).encode()
        status, headers, _body = request(server, "POST", "/auth/login", body=correct, headers=public)
        assert status == 303
        assert headers["Location"] == "/"
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert "correct" not in cookie
        session_cookie = cookie.split(";", 1)[0]

        status, _headers, body = request(
            server,
            "GET",
            "/",
            headers={"Host": "cdpa.hcu-lab.me", "Cookie": session_cookie},
        )
        assert status == 200
        assert body == DASHBOARD_HTML_PATH.read_bytes()
    finally:
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_authenticated_public_proxy_preserves_semantics_and_strips_only_auth_cookie(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1], auth_password="correct")
    try:
        login_body = urlencode({"password": "correct"}).encode()
        status, headers, _body = request(
            server,
            "POST",
            "/auth/login",
            body=login_body,
            headers={
                "Host": "cdpa.hcu-lab.me",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://cdpa.hcu-lab.me",
            },
        )
        assert status == 303
        session_cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, headers, body = request(
            server,
            "POST",
            "/api/tasks?view=compact",
            body=b'{"task":"x"}',
            headers={
                "Host": "cdpa.hcu-lab.me",
                "Content-Type": "application/json",
                "Content-Length": "12",
                "Origin": "https://cdpa.hcu-lab.me",
                "Idempotency-Key": "same-key",
                "Cookie": f"other=keep; {session_cookie}",
            },
        )
        payload = json.loads(body)
        assert status == 207
        assert headers["Content-Type"] == "application/json"
        assert headers["ETag"] == '"upstream-1"'
        assert payload == {
            "method": "POST",
            "path": "/api/tasks?view=compact",
            "body": '{"task":"x"}',
            "idempotency": "same-key",
        }
        upstream_headers = upstream.requests[-1]["headers"]
        assert upstream_headers.get("Cookie") == "other=keep"
        assert "correct" not in json.dumps(upstream.requests[-1], default=str)
    finally:
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_public_auth_missing_configuration_fails_closed_without_upstream(tmp_path: Path):
    upstream, upstream_thread = start_upstream()
    server, thread = start_frontend(tmp_path, api_port=upstream.server_address[1])
    try:
        status, _headers, body = request(
            server, "GET", "/api/state", headers={"Host": "cdpa.hcu-lab.me"}
        )
        assert status == 503
        assert json.loads(body)["error"]["code"] == "authentication_unavailable"

        status, _headers, body = request(
            server, "GET", "/", headers={"Host": "cdpa.hcu-lab.me"}
        )
        assert status == 503
        assert body != DASHBOARD_HTML_PATH.read_bytes()
        assert upstream.requests == []
    finally:
        server.shutdown()
        upstream.shutdown()
        thread.join(timeout=5)
        upstream_thread.join(timeout=5)


def test_dashboard_password_loader_reads_only_named_repository_env_value(tmp_path: Path, monkeypatch):
    env_path = tmp_path / ".env"
    assert dashboard_module._load_dashboard_password(tmp_path) is None

    env_path.write_text("OTHER=value\nCDPA_DASHBOARD_PASSWORD=correct\n", encoding="utf-8")
    assert dashboard_module._load_dashboard_password(tmp_path) == "correct"

    env_path.write_text("OTHER=value\n", encoding="utf-8")
    assert dashboard_module._load_dashboard_password(tmp_path) is None

    def unreadable(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert dashboard_module._load_dashboard_password(tmp_path) is None


def test_frontend_assets_are_local_modular_and_suspend_hidden_polling():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    api = (ASSET_ROOT / "api.js").read_text(encoding="utf-8")
    polling = (ASSET_ROOT / "polling.js").read_text(encoding="utf-8")
    store = (ASSET_ROOT / "store.js").read_text(encoding="utf-8")
    runtime = (ASSET_ROOT / "views" / "runtime.js").read_text(encoding="utf-8")
    all_assets = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [DASHBOARD_HTML_PATH, *ASSET_ROOT.rglob("*.js"), ASSET_ROOT / "dashboard.css"]
    )

    assert 'type="module"' in html
    assert "/assets/app.js" in html
    assert "cdn." not in all_assets
    assert "tailwind" not in all_assets.casefold()
    assert "document.hidden" in polling
    assert "visibilitychange" in polling
    assert "clearTimeout" in polling
    assert "inflight" in api
    assert "If-None-Match" in api
    assert "response.status === 304" in api
    assert "selectedTaskId" in store
    assert "pendingCommands" in store
    assert "localStorage" in store
    assert "loadTaskDetail" in app
    assert "loadHistory" in app
    assert "loadTimeline" in app
    assert 'data-dom-only' not in html
    assert "DOM only" not in html
    assert "dom_only" not in runtime
    assert '"/api/runtime/settings"' not in app






def test_notify_selector_keeps_latest_relevant_task_per_exact_team():
    module_path = ASSET_ROOT / "views" / "notify.js"
    assert module_path.is_file(), "Notify view module is not implemented yet"
    script = f"""
      import {{ selectNotifyTasks }} from {json.dumps(module_path.resolve().as_uri())};
      const rows = selectNotifyTasks([
        {{task_id: "alpha-old", team: "alpha", status: "DONE", elapsed_end_at: "2026-08-10T10:00:00Z", effective_activity_at: "2026-08-12T23:00:00Z", updated_at: "2026-08-12T23:30:00Z"}},
        {{task_id: "alpha-new", team: "alpha", status: "PAUSED", effective_activity_at: "2026-08-10T11:00:00Z", updated_at: "2026-08-12T23:59:00Z"}},
        {{task_id: "beta-block", team: "beta", status: "BLOCKED", elapsed_end_at: "2026-08-10T12:00:00Z"}},
        {{task_id: "gamma-stop", team: "gamma", status: "STOPPED", updated_at: "2026-08-10T09:00:00Z"}},
        {{task_id: "ignored", team: "delta", status: "RUNNING", updated_at: "2026-08-13T00:00:00Z"}},
      ]);
      console.log(JSON.stringify(rows.map(task => [task.task_id, task.status])));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        ["beta-block", "BLOCKED"],
        ["alpha-new", "PAUSED"],
        ["gamma-stop", "STOPPED"],
    ]


def test_notify_report_fallback_uses_chronologically_latest_cross_role_report():
    module = (ASSET_ROOT / "views" / "notify.js").resolve().as_uri()
    script = f"""
      import {{ notifyReportModel }} from {json.dumps(module)};
      const detail = {{
        status: "PAUSED",
        active_role: "AUDIT",
        roles: [
          {{logical_role: "TEST", physical_role: "alpha-test"}},
          {{logical_role: "REVIEW", physical_role: "alpha-review"}},
          {{logical_role: "PLAN", physical_role: "alpha-plan"}},
          {{logical_role: "AUDIT", physical_role: "alpha-audit"}},
        ],
        reports: [
          {{role: "TEST", turn: 4, url: "/test-turn4", created_at: "2026-08-11T14:04:00Z"}},
          {{role: "REVIEW", turn: 1, url: "/review-turn1", created_at: "2026-08-11T14:12:00Z"}},
          {{role: "PLAN", turn: 2, url: "/plan-turn2", created_at: "2026-08-11T14:24:00Z"}},
        ],
      }};
      const fallback = notifyReportModel(detail);
      const active = notifyReportModel({{
        ...detail,
        reports: [...detail.reports, {{role: "AUDIT", turn: 1, url: "/audit-turn1", created_at: "2026-08-11T14:20:00Z"}}],
      }});
      const done = notifyReportModel({{...detail, status: "DONE"}});
      console.log(JSON.stringify({{
        fallback: fallback.selectedReport?.url || null,
        active: active.selectedReport?.url || null,
        done: done.selectedReport?.url || null,
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "fallback": "/plan-turn2",
        "active": "/audit-turn1",
        "done": "/plan-turn2",
    }


def test_notify_frontend_contract_reuses_secondary_report_path_and_preserves_history():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    notify_path = ASSET_ROOT / "views" / "notify.js"
    assert notify_path.is_file(), "Notify view module is not implemented yet"
    notify = notify_path.read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'data-view="notify">Notify</button>' in html
    assert 'data-view="history">History</button>' in html
    assert 'data-action="reload_catalog"' not in html
    assert 'import {renderHistory} from "./views/history.js";' in app
    assert 'if (view === "history" && !state.history.length) loadHistory();' in app
    assert 'if (current.drawer === "history") renderHistory(roots.secondaryContent, current);' in app
    assert 'if (current.drawer === "notify") renderNotify(roots.secondaryContent, current);' in app
    assert 'if (view === "notify") delete roots.secondaryContent.dataset.secondaryView;' in app
    cache_key = "20260925-live-sse-v1"
    assert f'/assets/app.js?v={cache_key}' in html
    assert f'./views/task_detail.js?v={cache_key}' in app
    assert f'./views/notify.js?v={cache_key}' in app
    assert f'./task_detail.js?v={cache_key}' in notify
    assert 'data-notify-task-id' in notify
    assert 'workflowReportModel(detail, detail.active_role)' in notify
    assert 'reportBody(report, reportBodies)' in notify
    assert 'data-notify-back' in notify
    assert 'Open raw report' in notify
    assert 'empty.textContent = model.selectedMissingReason' in notify
    assert 'No report yet' not in notify
    assert 'event.target.closest("[data-notify-task-id]")' in app
    notify_open = app.split("async function openNotifyReport", 1)[1].split("\n}", 1)[0]
    assert "loadTaskDetail(taskId)" in notify_open
    assert "loadReports(detail)" in notify_open
    assert "selectTask(" not in notify_open
    assert 'kind: "reload_catalog"' not in app
    done = css.split('.notify-row[data-status="DONE"] {', 1)[1].split("}", 1)[0]
    blocked = css.split('.notify-row[data-status="BLOCKED"] {', 1)[1].split("}", 1)[0]
    paused = css.split('.notify-row[data-status="PAUSED"] {', 1)[1].split("}", 1)[0]
    stopped = css.split('.notify-row[data-status="STOPPED"] {', 1)[1].split("}", 1)[0]
    assert "var(--good)" in done
    assert "var(--bad)" in blocked
    assert "var(--warn)" in paused
    assert "#05070a" in stopped
    mobile = css.split("@media (max-width: 720px)", 1)[1]
    assert ".notify-row" in mobile
    assert "grid-template-columns: minmax(0, 1fr);" in mobile


def test_commands_are_a_full_height_board_lane_without_changing_command_semantics():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    board_open = html.index('<div id="board" class="board-grid"')
    board_close = html.index("</div>", board_open)
    commands_at = html.index('<aside id="commands"')
    workspace_at = html.index('<section id="task-workspace" class="task-workspace"')
    command_lane = css.split(".lane.command-lane {", 1)[1].split("}", 1)[0]

    assert board_open < commands_at < board_close < workspace_at
    assert 'class="lane command-lane"' in html
    assert 'data-column="COMMANDS"' in html
    assert 'count.className = "lane-count"' in app
    assert 'className = "lane-list command-list"' in app
    assert 'JSON.stringify([entries.length, entries.slice(0, 12)' in app
    assert 'String(b.createdAt).localeCompare(String(a.createdAt))' in app
    assert "commandPresentation(command)" in app
    assert 'button.dataset.renderDisabled === "true"' in app
    assert ".board-section > .command-panel" not in css
    assert '.lane[data-column="COMMANDS"]' in css
    assert ".command-list" in css
    assert "order: 8;" in command_lane
    assert "height:" not in command_lane
    assert "min-height:" not in command_lane
    assert "align-self:" not in command_lane
    assert ".lane {\n  --lane-accent:" in css
    assert "height: 388px;" in css
    assert ".lane-list {" in css
    assert "min-height: 0;" in css
    assert "overflow-y: auto;" in css
    mobile = css.split("@media (max-width: 720px)", 1)[1]
    assert "grid-template-columns: repeat(8, min(86vw, 340px));" in mobile
    assert "height: 388px;" in mobile
    assert "min-height: 388px;" in mobile


def test_workflow_report_model_selects_latest_per_role_and_retains_older_reports():
    module = (ASSET_ROOT / "views" / "task_detail.js").resolve().as_uri()
    script = f"""
      import {{ workflowReportModel }} from {json.dumps(module)};
      const detail = {{
        status: "RUNNING",
        roles: [
          {{logical_role: "PLAN", physical_role: "alpha-plan", turn: 2}},
          {{logical_role: "DEV", physical_role: "alpha-dev", turn: 2}},
          {{logical_role: "REVIEW", physical_role: "alpha-review", turn: 2}},
          {{logical_role: "AUDIT", physical_role: "alpha-audit", turn: 0}},
          {{logical_role: "TEST", physical_role: "alpha-test", turn: 0}},
        ],
        reports: [
          {{role: "PLAN", turn: 1, url: "/p1"}},
          {{role: "DEV", turn: 1, url: "/d1"}},
          {{role: "PLAN", turn: 2, url: "/p2"}},
          {{physical_role: "alpha-dev", turn: 2, url: "/d2"}},
          {{physical_role: "alpha-review", turn: 2, url: "/r2"}},
        ],
      }};
      const running = workflowReportModel(detail, "DEV");
      const fallback = workflowReportModel(detail, "AUDIT");
      const done = workflowReportModel({{...detail, status: "DONE"}}, "DEV");
      const explicit = workflowReportModel(detail, "PLAN", "/p1");
      console.log(JSON.stringify({{
        latest: running.roles.map(item => [item.role, item.latest?.url || null]),
        older: running.olderReports.map(item => item.url),
        running: running.selectedReport?.url || null,
        fallback: fallback.selectedReport?.url || null,
        done: done.selectedReport?.url || null,
        explicit: explicit.selectedReport?.url || null,
        explicitRole: explicit.selectedReport?._logicalRole || null,
        coverage: running.coverage,
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "latest": [["PLAN", "/p2"], ["DEV", "/d2"], ["REVIEW", "/r2"]],
        "older": ["/d1", "/p1"],
        "running": "/d2",
        "fallback": "/r2",
        "done": "/p2",
        "explicit": "/p1",
        "explicitRole": "PLAN",
        "coverage": "3/3",
    }


def test_workflow_report_availability_distinguishes_remote_without_fetchable_url():
    module = (ASSET_ROOT / "views" / "task_detail.js").resolve().as_uri()
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    script = f"""
      import {{ reportBody }} from {json.dumps(module)};
      const bodies = new Map();
      console.log(JSON.stringify([
        reportBody({{availability: "remote_unmirrored"}}, bodies),
        reportBody({{availability: "unavailable"}}, bodies),
      ]));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        "Report stored on remote execution host / not mirrored.",
        "Report is unavailable on this dashboard host.",
    ]
    assert "if (!report.url || reportBodies.has(report.url)) return;" in app


def test_workflow_report_polish_uses_coverage_rail_and_responsive_selector():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert '["reports", `Reports ${model.coverage}`]' in detail
    assert 'tabs.classList.add("workflow-detail-tabs")' in detail
    assert '"workflow-report-layout"' in detail
    assert '"workflow-report-content"' in detail
    assert '`report-role${item.role === model.selectedReport?._logicalRole ? " selected" : ""}`' in detail
    assert "role.disabled = !item.current;" in detail
    assert 'item.current ? `Turn ${item.current.turn ?? "—"}` : item.missingReason' in detail
    assert 'model.selectedMissingReason' in detail
    assert 'No workflow reports are available yet.' not in detail
    assert ".workflow-report-layout {" in css
    assert "grid-template-columns: 175px minmax(0, 1fr);" in css
    assert "box-shadow: inset 2px 0 0 var(--accent);" in css
    tablet = css.split("@media (max-width: 900px)", 1)[1]
    assert ".workflow-report-layout { grid-template-columns: minmax(0, 1fr); }" in tablet
    assert ".report-role-nav {" in tablet
    assert "overflow-x: auto;" in tablet
    mobile = css.split("@media (max-width: 720px)", 1)[1]
    assert ".workflow-detail-tabs.detail-tabs" in mobile
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in mobile


def test_task_goal_blocks_dedupe_and_disclosure_state_contract_is_keyed():
    module = (ASSET_ROOT / "views" / "task_detail.js").resolve().as_uri()
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    script = f"""
      import {{ taskGoalBlocks }} from {json.dumps(module)};
      console.log(JSON.stringify([
        taskGoalBlocks({{task_id: "x", task_text: "same", effective_goal: "same"}}),
        taskGoalBlocks({{task_id: "x", task_text: "task", effective_goal: "goal"}}),
      ]));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        [{"key": "task-goal", "label": "Task / Effective goal", "text": "same"}],
        [
            {"key": "task", "label": "Task", "text": "task"},
            {"key": "effective-goal", "label": "Effective goal", "text": "goal"},
        ],
    ]
    assert 'details.dataset.disclosureKey = block.key' in detail
    assert '"task-disclosure-preview"' in detail
    assert '"Show more"' in detail
    assert '"Show less"' in detail
    assert 'const knownDisclosureKeys = new Set' in detail
    assert 'previousDetails.filter(details => details.open)' in detail
    assert 'if (knownDisclosureKeys.has(details.dataset.disclosureKey))' in detail
    assert 'details.open = openDisclosureKeys.has(details.dataset.disclosureKey)' in detail


def test_dashboard_exposes_role_urls_live_audit_and_page_level_quick_navigation():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'class="page-rail"' in html
    assert 'data-page-jump="task-board"' in html
    for target in ("overview", "roles", "live", "timeline", "reports"):
        assert f'data-task-jump="{target}"' in html
    assert 'id="task-board"' in html
    assert 'id="task-workspace"' in html
    assert 'scrollIntoView({behavior: "smooth", block: "start"})' in app
    assert '[["overview", "Overview"], ["live", "Live transcript"], ["reports", `Reports ${model.coverage}`]]' in detail
    assert 'copy.dataset.copyRoleUrl = role.chat_url' in detail
    assert 'open.href = role.chat_url' in detail
    assert 'id: "task-roles"' in detail
    assert 'id: "task-live"' in detail
    assert 'id: "task-timeline"' in detail
    assert 'id: "task-reports"' in detail
    assert 'for (const source of ["DOM", "LISTEN", "CTRL", "ACTION"])' in detail
    assert 'audit.dataset.disclosureKey = "live-audit-signals"' in detail
    assert 'audit.open = true' in detail
    assert 'currentLive.replaceWith(nextLive)' not in detail
    assert 'scheduleWorkflowLivePatch(' in detail
    assert 'reconcileLiveList(' in detail
    assert 'message.author === "user" && message.text.length > 420' in detail
    assert 'message.key || message.messageId || message.turnId' in detail
    assert 'current.liveEventsByScope.set(scope, view)' in app
    assert 'summary?.status === "RUNNING" ? summary.active_role : null' in app
    assert 'selectTask(taskId, cached, preferredRole)' in app
    assert '.page-rail {' in css
    assert '.live-event-list {' in css
    assert '.role-url-actions {' in css
    assert 'grid-template-columns: 96px minmax(0, 1fr);' in css
    assert '.task-workspace {\n  display: block;' in css
    assert 'const inputSection = el("details", null, "role-input-section role-input-disclosure")' in detail
    assert 'inputSection.dataset.disclosureKey = `role-input-${inputRole}`' in detail
    assert 'section.className = "detail-section detail-card bootstrap-context-card"' in app
    assert 'disclosure.className = "bootstrap-context-disclosure"' in app
    assert 'current.selectedTaskJumpByTask.set(current.selectedTaskId, target)' in app
    assert 'node.setAttribute("aria-current", "location")' in app
    assert 'copyRoleUrl.textContent = "Copied ✓"' in app
    assert 'select.setAttribute("aria-pressed", String(selected))' in detail
    assert 'label: "not allocated"' in detail
    assert '"No tab allocated"' in detail
    assert 'DOM: "Observed semantic page state"' in detail
    assert 'sourceCounts[source]' in detail
    assert 'audit.dataset.disclosureKey = "live-audit-signals"' in detail
    assert 'live-transcript-preview' in detail
    assert 'currentLive.replaceWith(nextLive)' not in detail
    assert 'requestAnimationFrame(() =>' in detail
    assert 'root.dataset.baseSignature === baseSignature' in detail
    assert 'const preferredRole = summary?.status === "RUNNING" ? summary.active_role : null' in app
    assert 'selectTask(taskId, cached, preferredRole)' in app
    assert '<strong>Live audit</strong><span>' not in html
    assert 'details.append(el("summary", "Details"))' in detail
    responsive = css.split("@media (max-width: 920px)", 1)[1].split("@media (max-width: 720px)", 1)[0]
    assert "display: block;" in responsive
    assert "overflow-x: auto;" in responsive
    assert ".rail-task-links { display: flex;" in responsive


def test_runtime_uses_fixed_hybrid_observation_without_mode_help():
    runtime = (ASSET_ROOT / "views" / "runtime.js").read_text(encoding="utf-8")
    assert "dom_only" not in runtime
    assert "DOM-only" not in runtime
    assert "serviceChip" in runtime


def test_workflow_controls_fail_closed_from_projected_eligibility_with_reason_tooltips():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowOverview", 1)[1].split("function build", 1)[0]

    assert "detail.control_eligibility?.[action]" in workflow
    assert "eligible: false" in workflow
    assert "disabled: eligibility.eligible !== true" in workflow
    assert "reason: eligibility.reason || null" in workflow
    assert "node.title = String(reason)" in detail
    assert "node.dataset.disabledReason = String(reason)" in detail


def test_worktree_archive_control_is_operator_visible_and_requires_confirmation():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowOverview", 1)[1].split("function build", 1)[0]

    assert '["archive_worktree", "Archive worktree"]' in workflow
    assert '["stop", "clear_team", "archive_worktree"].includes(control.dataset.control)' in app


def test_task_controls_render_inside_operational_state_before_task_prompt():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowOverview", 1)[1].split("function build", 1)[0]
    independent = detail.split("function independentOverview", 1)[1].split("function independentHistory", 1)[0]

    assert 'const operations = operationalStateSection(detail, endpoints);' in workflow
    assert workflow.index("operations.append(controls);") < workflow.index("fragment.append(taskSection);")
    assert 'const operational = operationalStateSection(detail, endpoints);' in independent
    assert "operational.append(independentControls(detail));" in independent
    assert independent.index("fragment.append(operational);") < independent.index("fragment.append(section);")


def test_independent_agents_have_one_lane_and_operator_facing_controls():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    store = (ASSET_ROOT / "store.js").read_text(encoding="utf-8")

    assert 'task.task_mode === "independent" ? "INDEPENDENT_AGENTS"' in board
    assert 'text("span", "Independent Agent", "task-agent-context")' in board
    assert 'agent.tags' in board
    assert '"Custom Agent"' not in board
    assert 'agent.run_count' in board
    assert 'agent.run_count_truncated' in board
    assert 'agent.last_run_at' in board
    assert 'task.status === "RUNNING"' in board
    assert 'task.elapsed_end_at' in board
    assert 'fixedDuration' in board
    assert 'data-elapsed-at' not in board.split('if (agent)', 1)[-1].split('function ensureLane', 1)[0]
    agent_meta = board.split('meta.className = "task-meta";', 1)[1].split('} else {', 1)[0]
    assert 'field("Status", task.status)' in agent_meta
    assert 'field("Runs",' in agent_meta
    assert 'elapsed(agent.last_run_at)' in agent_meta
    assert 'field("Action", task.active_action)' not in agent_meta
    assert 'field("Tab",' not in agent_meta

    for label in [
        '"Run task"', '"Pause"', '"Enable"', '"Reset"', '"Settings"',
        '"Delete"', '"Open tab"', '"Close tab"', '"History"', '"Reports"',
    ]:
        assert label in detail
    for removed in ['"Run once"', '"Command"', '"Stop current job"', '"Renew"']:
        assert removed not in detail
    assert 'dataset.independentTab' in detail
    assert 'Overview' in detail
    assert 'Max turns per job' in detail
    assert 'Unlimited' in detail
    assert 'agent.tab_open' in detail
    assert 'agent.tab_keep_open_until' in detail
    assert 'selectedIndependentTabByTask' in store
    assert 'dataset.renderDisabled' in detail
    assert 'disabled: active || !enabled' in detail
    assert 'button.dataset.renderDisabled === "true"' in app
    assert 'if (state.selectedDetail?.task_id === taskId) return;' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/run' in app
    assert 'body: {trigger_type: "manual", instruction}' in app
    assert 'action: control.dataset.control' in app
    assert 'control.dataset.control === "reset"' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/reset' in app
    assert 'body: isReset ? {reason: "Operator reset"}' in app
    assert '.filter(item => !item.agent?.deleted_at)' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/delete' in app

    assert '<h2>Run task</h2>' in html
    assert '>Run task</button>' in html
    assert 'name="max_cycles"' in html
    assert 'Max turns per job' in html
    assert 'Workflow agent completed' in html
    assert 'Review all active workflow tasks' in html
    assert 'CHECK_ALL' not in html


def test_independent_delete_is_disabled_for_builtin_or_active_agents():
    module = (ASSET_ROOT / "views" / "task_detail.js").resolve().as_uri()
    script = f"""
      import {{ independentDeleteDisabled }} from {json.dumps(module)};
      const cases = [
        independentDeleteDisabled({{is_builtin: true}}, false),
        independentDeleteDisabled({{is_builtin: false}}, true),
        independentDeleteDisabled({{is_builtin: false}}, false),
      ];
      console.log(JSON.stringify(cases));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [True, True, False]


def test_create_task_sections_summary_validation_and_agent_panel_dismissal_are_explicit():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    for section in ["task-details", "team-repository", "workflow-agents", "dependencies-output"]:
        assert f'data-create-section="{section}"' in html
    assert 'id="create-selection-summary"' in html
    assert 'id="create-validation"' in html
    assert 'aria-live="assertive"' in html
    assert 'create-dialog-footer' in html
    assert 'position: sticky' in css
    assert 'updateCreateSummary' in app
    assert 'showCreateValidation' in app
    assert 'roots.form.reportValidity()' in app

    assert 'aria-expanded="false"' in html
    assert 'setAgentPanelOpen' in app
    assert 'roots.agentPanel.contains(event.target)' in app
    assert 'event.key === "Escape" && !roots.agentPanel.hidden' in app
    assert 'roots.agentOpener.focus()' in app
    assert 'workflow-task-team-options' in html
    assert 'data-workflow-agent-select' in html
    assert 'renderTriggerChoices' in app

def test_create_task_bootstrap_selection_is_disabled_and_legacy_detail_stays_visible():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert html.count('select name="bootstrap_id"') == 1
    assert '<option value="">Fresh context</option>' in html
    assert "loadBootstrapOptions" in app
    assert 'roots.bootstrapSelect.replaceChildren(new Option("Fresh context", ""));' in app
    assert "roots.bootstrapSelect.disabled = true;" in app
    assert 'Bootstrap branching is temporarily disabled; workflow tasks use Fresh context.' in app
    assert 'data-bootstrap-inline' in html
    assert "roots.bootstrapInline.hidden = true;" in app
    assert '"/api/bootstraps"' not in app
    assert "body.bootstrap_id" not in app
    assert "body.bootstrap_definition" not in app
    assert 'name="bootstrap_new_id"' not in html
    assert 'name="bootstrap_new_source"' not in html
    assert "renderBootstrapContext" in app
    assert "bootstrap_context" in app
    assert "terminal_assistant_message_id" not in app


def test_create_task_role_selection_defaults_and_reuse_lock_are_explicit():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    actions = (ASSET_ROOT / "views" / "dashboard_actions.js").read_text(encoding="utf-8")

    assert 'id="workflow-agent-options"' in html
    assert "renderWorkflowAgentOptions" in actions
    assert 'input.name = "roles"' in actions
    assert 'input.value = agent.route_key' in actions
    assert 'if (agent.route_key === "PLAN")' in actions
    assert 'DEFAULT_WORKFLOW_ROLES.has(agent.route_key)' in actions
    assert "selectedWorkflowRoles" in app
    assert "body.roles = selectedWorkflowRoles" in app
    assert "applyReuseRoleSelection" in actions
    assert "item.roles" in actions
    assert 'input.disabled = locked || input.value === "PLAN"' in actions


def test_parent_dependencies_render_team_names_and_exact_remove_actions():
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'const dependencies = detail.dependencies || []' in detail
    assert 'if (!dependencies.length) return null' in detail
    assert 'dependencies.length === 1 ? "Parent" : "Parents"' in detail
    assert 'dependency.team || dependency.task_id' in detail
    assert 'const remove = el("button", "Remove")' in detail
    assert 'remove.dataset.removeParent = dependency.task_id' in detail
    assert '[data-remove-parent]' in app
    assert '/parents/${encodeURIComponent(parentTaskId)}/remove' in app
    assert 'kind: "remove_parent_dependency"' in app
    assert 'expected_task_version' in app


def test_change_goal_ui_contract_is_running_only_and_uses_shared_command_path():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'id="change-goal-dialog"' in html
    assert 'id="change-goal-form"' in html
    assert 'name="goal"' in html
    assert 'detail.status === "RUNNING"' in detail
    assert '"Change goal"' in detail
    assert 'detail.effective_goal' in detail
    assert 'detail.goal_revisions' in detail
    assert '/api/tasks/${encodeURIComponent(taskId)}/goal' in app
    assert 'kind: "change_goal"' in app
    assert 'expected_task_version' in app
    assert 'roots.changeGoalForm' in app


def test_operational_topology_unifies_endpoint_health_and_task_placement():
    topology_path = ASSET_ROOT / "views" / "topology.js"
    assert topology_path.exists()
    module = topology_path.resolve().as_uri()
    script = f"""
      import {{endpointOperationalState, taskPlacement}} from {json.dumps(module)};
      const endpoints = [
        {{
          endpoint_id: "requested",
          label: "9202 Extra",
          cdp_url: "http://127.0.0.1:9202",
          connected: true,
          chatgpt_ready: true,
          health: "online",
          active_count: 0,
          capacity: 2,
          available_slots: 2,
        }},
        {{
          endpoint_id: "actual",
          label: "9222 Plus",
          cdp_url: "http://127.0.0.1:9222",
          connected: true,
          chatgpt_ready: true,
          health: "online",
          active_count: 2,
          capacity: 2,
          available_slots: 0,
        }},
      ];
      const workflow = taskPlacement({{
        requested_browser_endpoint_id: "requested",
        active_role: "DEV",
        roles: [{{logical_role: "DEV", browser_endpoint_id: "actual"}}],
      }}, endpoints);
      const independent = taskPlacement({{
        task_mode: "independent",
        agent: {{browser_endpoint_id: "requested"}},
        active_role: "AGENT",
        roles: [{{logical_role: "AGENT", browser_endpoint_id: "requested"}}],
      }}, endpoints);
      console.log(JSON.stringify({{
        ready: endpointOperationalState(endpoints[0]),
        full: endpointOperationalState(endpoints[1]),
        workflow,
        independent,
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ready"]["key"] == "ready"
    assert payload["full"]["key"] == "full"
    assert payload["workflow"]["requested"]["label"] == "9202 Extra"
    assert payload["workflow"]["actual"]["label"] == "9222 Plus"
    assert payload["workflow"]["relocated"] is True
    assert payload["independent"]["affinity"]["label"] == "9202 Extra"
    assert payload["independent"]["actual"]["label"] == "9202 Extra"

    browsers = (ASSET_ROOT / "views" / "browsers.js").read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")
    assert '"endpoint-scope-card"' in browsers
    assert '"endpoint-workload"' in browsers
    assert '"task-placement"' in board
    assert '"Operational state"' in detail
    assert ".endpoint-scope-card {" in css
    assert ".task-placement {" in css
    assert ".operational-state-grid {" in css


def test_board_lane_materialization_keeps_commands_after_operational_lanes():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")

    # Commands is present in the initial HTML. New workflow lanes must be inserted
    # before it so DOM order matches visual order and narrow Chromium does not
    # preserve scroll anchoring on Commands as that lane moves to the far right.
    assert 'const commands = root.querySelector(".command-lane");' in board
    assert "if (commands) root.insertBefore(section, commands);" in board
    assert 'const firstMaterialization = !root.querySelector(".lane:not(.command-lane)");' in board
    assert "if (firstMaterialization) root.scrollLeft = 0;" in board

    # board.js and its importing entrypoint both get fresh cache identities.
    assert './views/board.js?v=20260915-operator-ux-v1' in app
    assert '/assets/app.js?v=20260925-live-sse-v1' in html


def test_dashboard_compact_layout_contract_keeps_operational_content_visible():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    # CSS byte changes must publish under a new cache identity.
    assert '/assets/dashboard.css?v=20260925-live-sse-v1' in html

    # Endpoint scope is one compact equal-height topology; required operational
    # fields must not be hidden behind a fixed maximum card height.
    assert "grid-auto-rows: 1fr;" in css
    assert "height: 100%;" in css
    assert "max-height: 118px" not in css

    # A workflow card renders head, summary, placement, identity, and metadata.
    # The layout must account for all five rows instead of clipping the fifth.
    assert "grid-template-rows: auto auto auto auto auto;" in css
    assert ".task-operational {" in css
    assert "overflow-wrap: anywhere;" in css

    # Narrow viewports keep endpoint actions and cards bounded rather than
    # squeezing the topology beside the management controls.
    assert ".browser-scope { grid-template-columns: minmax(0, 1fr); }" in css


def test_dashboard_dense_operator_layout_contract():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    browsers = (ASSET_ROOT / "views" / "browsers.js").read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    # Published frontend bytes use a fresh cache identity through the changed import graph.
    assert '/assets/dashboard.css?v=20260925-live-sse-v1' in html
    assert '/assets/app.js?v=20260925-live-sse-v1' in html
    assert './views/board.js?v=20260915-operator-ux-v1' in app
    assert './views/browsers.js?v=20260915-endpoint-all-v1' in app

    # Endpoint controls are compact: state + CDP share a row and load gets visual priority.
    assert '"endpoint-card-statusline"' in browsers
    assert '"endpoint-load-value"' in browsers
    assert ".endpoint-card-statusline {" in css
    assert ".endpoint-load-value {" in css

    # Browser management uses the same balanced form/control system instead of native uneven fields.
    assert ".browser-manager input," in css
    assert ".browser-manager select {" in css
    assert ".browser-add-grid > label:nth-child(5)" in css
    assert ".browser-settings-grid > .check-row" in css

    # Lane already communicates status. Cards keep action/duration but do not repeat status/queue reason.
    assert 'meta.append(normalWaiting ? waitingField(task) : field("Status", task.status));' not in board
    assert 'meta.append(field("Queue reason", waiting.reason));' not in board
    assert 'secondary.textContent = placement.relocated' in board
    assert ': "";' in board

    # Preserve the proven board geometry from the pre-endpoint UI. Status lanes are
    # fixed grid items; responsive breakpoints change the column count instead of
    # widening lanes ad hoc. A 388px lane with two 150px cards fits two complete
    # teams before the lane needs vertical scrolling.
    board = css.split(".board-section {", 1)[1].split("}", 1)[0]
    lane = css.split(".lane {", 1)[1].split("}", 1)[0]
    lane_header = css.split(".lane > header {", 1)[1].split("}", 1)[0]
    lane_list = css.split(".lane-list {", 1)[1].split("}", 1)[0]
    task_card = css.split(".task-card {", 1)[1].split("}", 1)[0]
    wide = css.split("@media (min-width: 1600px)", 1)[1].split("@media (max-width: 1199px)", 1)[0]
    medium = css.split("@media (max-width: 1199px)", 1)[1].split("@media (max-width: 920px)", 1)[0]

    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in board
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in wide
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in medium
    assert "height: 388px;" in lane
    assert "min-height: 52px;" in lane_header
    assert "padding: .8rem .9rem;" in lane_header
    assert "gap: .6rem;" in lane_list
    assert "padding: .7rem;" in lane_list
    assert "height: 150px;" in task_card


def test_endpoint_all_scope_is_compact_and_does_not_consume_endpoint_card_width():
    browsers = (ASSET_ROOT / "views" / "browsers.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'button("All", {browserScope: "all"' in browsers
    assert 'button("All endpoints", {browserScope: "all"' not in browsers
    assert "grid-template-columns: 64px repeat(auto-fit, minmax(145px, 1fr));" in css


def test_task_card_ellipsis_keeps_full_text_and_distinguishes_waiting_from_blockers():
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert "team.title = cardName;" in board
    assert "titleNode.title = titleText;" in board
    assert "problemNode.title = problem;" in board
    assert 'task.status === "WAITING" ? "task-problem task-waiting" : "task-problem task-blocked"' in board
    assert "primary.title = primary.textContent;" in board
    assert "secondary.title = secondary.textContent;" in board
    assert ".task-waiting {" in css
    assert ".task-blocked {" in css


def test_browser_pool_waiting_board_uses_projected_fifo_queue_truth():
    module = (ASSET_ROOT / "views" / "board.js").resolve().as_uri()
    script = f"""
      import {{ waitingLaneSort, waitingPresentation, taskSignature }} from {json.dumps(module)};
      import assert from 'node:assert/strict';
      const older = {{
        task_id: "zzz-old",
        status: "WAITING",
        created_at: "2026-09-14T00:00:01Z",
        queue_position: 1,
        queue_length: 2,
        queue_since: "2026-09-14T00:01:00Z",
        queue_reason: "Waiting for browser capacity.",
        waiting_order: {{rank: 2, fifo: 1}},
        roles: [],
      }};
      const newerRepair = {{
        task_id: "aaa-new-repair",
        status: "WAITING",
        created_at: "2026-09-14T00:00:02Z",
        queue_position: 2,
        queue_length: 2,
        queue_since: "2026-09-14T00:02:00Z",
        queue_reason: "Waiting for browser capacity.",
        waiting_order: {{rank: 1, fifo: 0}},
        roles: [],
      }};
      const dependency = {{
        task_id: "dependency",
        status: "WAITING",
        waiting_order: {{rank: 3, fifo: 3}},
        roles: [],
      }};
      assert.deepEqual(
        [newerRepair, older].sort(waitingLaneSort).map(task => task.task_id),
        ["zzz-old", "aaa-new-repair"],
      );
      assert.deepEqual(waitingPresentation(older), {{
        kind: "browser_queue",
        label: "Queue",
        value: "1/2",
        since: "2026-09-14T00:01:00Z",
        reason: "Waiting for browser capacity.",
        intervention: null,
      }});
      assert.equal(waitingPresentation(dependency).label, "Order");
      assert.notEqual(
        taskSignature(older, new Map(), null),
        taskSignature({{...older, queue_position: 2}}, new Map(), null),
      );
    """
    __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_health_dashboard_running_operational_truth_distinguishes_unknown_from_offline():
    module = (ASSET_ROOT / "views" / "board.js").resolve().as_uri()
    script = f"""
      import {{ deriveOperationalTruth }} from {json.dumps(module)};
      const now = Date.parse("2026-09-14T03:00:00Z");
      const base = {{
        status: "RUNNING",
        active_role: "DEV",
        active_action: "wait_response",
        effective_activity_at: "2026-09-14T02:59:30Z",
        roles: [{{logical_role: "DEV", online: true}}],
        availability: "online",
      }};
      const runtime = {{worker_online: true, worker_stale: false, semantic_stale_after_seconds: 600}};
      console.log(JSON.stringify({{
        healthy: deriveOperationalTruth(base, runtime, now),
        unknown: deriveOperationalTruth({{...base, availability: "unknown", roles: [{{logical_role: "DEV", online: null}}]}}, runtime, now),
        offline: deriveOperationalTruth({{...base, availability: "offline", roles: [{{logical_role: "DEV", online: false}}]}}, runtime, now),
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["healthy"]["state"] == "healthy"
    assert payload["unknown"]["state"] == "unknown"
    assert payload["unknown"]["reason"] == "Active role ownership is unknown."
    assert payload["offline"]["state"] == "degraded"
    assert payload["offline"]["reason"] == "Active DEV ownership is offline."


def test_health_dashboard_report_model_does_not_show_prior_turn_as_current():
    module = (ASSET_ROOT / "views" / "task_detail.js").resolve().as_uri()
    script = f"""
      import {{ workflowReportModel }} from {json.dumps(module)};
      const detail = {{
        status: "RUNNING", active_role: "DEV", active_hop_id: 3,
        roles: [{{logical_role: "DEV", physical_role: "alpha-dev"}}],
        role_inputs: {{DEV: {{logical_role: "DEV", turn: 2, expected_report: "alpha-dev_turn2_task-a.md"}}}},
        active_input: {{logical_role: "DEV", turn: 2, expected_report: "alpha-dev_turn2_task-a.md"}},
        reports: [{{role: "DEV", physical_role: "alpha-dev", turn: 1, url: "/dev-turn1"}}],
      }};
      const model = workflowReportModel(detail, "DEV");
      console.log(JSON.stringify({{
        selected: model.selectedReport?.url || null,
        missing: model.selectedMissingReason || null,
        current: model.roles[0]?.current?.url || null,
        history: model.olderReports.map(report => report.url),
        coverage: model.coverage,
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "selected": None,
        "missing": "Expected alpha-dev_turn2_task-a.md; no validated DEV report has been recorded yet.",
        "current": None,
        "history": ["/dev-turn1"],
        "coverage": "0/1",
    }


def test_live_stream_reducer_uses_message_identity_and_listen_final_precedence():
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ createLiveReducer }} from {json.dumps(module)};
      const reducer = createLiveReducer();
      reducer.applySnapshot([
        {{
          event_id: "dom-partial",
          source: "TRANSCRIPT",
          kind: "message",
          at: "2026-09-25T00:00:01Z",
          values: {{
            author: "assistant", turn_id: "turn-1", text: "partial",
            complete: false, projection_source: "dom",
          }},
        }},
        {{
          event_id: "listen-final",
          source: "TRANSCRIPT",
          kind: "message",
          at: "2026-09-25T00:00:02Z",
          values: {{
            author: "assistant", message_id: "message-1", turn_id: "turn-1",
            text: "final", complete: true, projection_source: "listen",
          }},
        }},
        {{
          event_id: "dom-late",
          source: "TRANSCRIPT",
          kind: "message",
          at: "2026-09-25T00:00:03Z",
          values: {{
            author: "assistant", turn_id: "turn-1", text: "partial again",
            complete: false, projection_source: "dom",
          }},
        }},
      ]);
      const view = reducer.viewModel();
      console.log(JSON.stringify(view.messages.map(message => ({{
        key: message.key,
        messageId: message.messageId,
        text: message.text,
        authoritative: message.authoritative,
      }}))));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [{
        "key": "message-1",
        "messageId": "message-1",
        "text": "final",
        "authoritative": True,
    }]


def test_live_stream_reducer_preserves_distinct_real_message_ids_in_same_turn():
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ createLiveReducer }} from {json.dumps(module)};
      const reducer = createLiveReducer();
      reducer.applySnapshot([
        {{
          event_id: "analysis-event",
          source: "TRANSCRIPT",
          kind: "message",
          at: "2026-09-25T00:00:01Z",
          values: {{
            author: "assistant", message_id: "a1-analysis", turn_id: "t1",
            text: "analysis", complete: true, projection_source: "dom",
          }},
        }},
        {{
          event_id: "final-event",
          source: "TRANSCRIPT",
          kind: "message",
          at: "2026-09-25T00:00:02Z",
          values: {{
            author: "assistant", message_id: "a1-final", turn_id: "t1",
            text: "final", complete: true, projection_source: "dom",
          }},
        }},
      ]);
      console.log(JSON.stringify(reducer.viewModel().messages.map(message => ({{
        key: message.key,
        messageId: message.messageId,
        text: message.text,
        turnId: message.turnId,
      }}))));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [
        {
            "key": "a1-analysis",
            "messageId": "a1-analysis",
            "text": "analysis",
            "turnId": "t1",
        },
        {
            "key": "a1-final",
            "messageId": "a1-final",
            "text": "final",
            "turnId": "t1",
        },
    ]


def test_live_stream_reducer_alias_is_unique_and_ambiguity_safe():
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ createLiveReducer }} from {json.dumps(module)};
      const event = (eventId, messageId, turnId, text) => ({{
        event_id: eventId,
        source: "TRANSCRIPT",
        kind: "message",
        values: {{
          author: "assistant",
          ...(messageId ? {{message_id: messageId}} : {{}}),
          turn_id: turnId,
          text,
          complete: true,
          projection_source: "dom",
        }},
      }});

      const duplicate = createLiveReducer();
      duplicate.applySnapshot([
        event("d1", "a1", "t1", "real1"),
        event("d2", null, "t1", "fallback"),
        event("d3", "a1", "t1", "real2"),
      ]);

      const ambiguous = createLiveReducer();
      ambiguous.applySnapshot([
        event("a1", null, "t1", "fallback-first"),
        event("a2", "a1-analysis", "t1", "analysis"),
        event("a3", "a1-final", "t1", "final"),
        event("a4", null, "t1", "fallback-late"),
      ]);

      const bounded = createLiveReducer();
      const items = [];
      for (let i = 0; i < 120; i += 1) {{
        const turn = "turn-" + i;
        items.push(event("b-" + i + "-fallback", null, turn, "fallback-" + i));
        items.push(event("b-" + i + "-analysis", "analysis-" + i, turn, "analysis-" + i));
        items.push(event("b-" + i + "-final", "final-" + i, turn, "final-" + i));
      }}
      bounded.applySnapshot(items);

      console.log(JSON.stringify({{
        duplicate: {{
          order: duplicate.state.messageOrder,
          mapSize: duplicate.state.messages.size,
          messages: duplicate.viewModel().messages.map(message => ({{
            key: message.key, messageId: message.messageId, text: message.text,
          }})),
        }},
        ambiguous: {{
          alias: ambiguous.state.turnAliases.get("assistant:t1"),
          messages: ambiguous.viewModel().messages.map(message => ({{
            key: message.key, messageId: message.messageId, text: message.text,
          }})),
        }},
        bounded: {{
          orderSize: bounded.state.messageOrder.length,
          uniqueOrderSize: new Set(bounded.state.messageOrder).size,
          mapSize: bounded.state.messages.size,
          aliasSize: bounded.state.turnAliases.size,
        }},
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "duplicate": {
            "order": ["a1"],
            "mapSize": 1,
            "messages": [{"key": "a1", "messageId": "a1", "text": "real2"}],
        },
        "ambiguous": {
            "alias": None,
            "messages": [
                {"key": "a1-analysis", "messageId": "a1-analysis", "text": "analysis"},
                {"key": "a1-final", "messageId": "a1-final", "text": "final"},
                {"key": "assistant:t1", "messageId": None, "text": "fallback-late"},
            ],
        },
        "bounded": {
            "orderSize": 100,
            "uniqueOrderSize": 100,
            "mapSize": 100,
            "aliasSize": 50,
        },
    }


def test_live_stream_reducer_detects_seq_gap_dedupes_and_bounds_memory():
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ createLiveReducer }} from {json.dumps(module)};
      const reducer = createLiveReducer();
      reducer.applySnapshot([], {{epoch: "epoch-a", headSeq: 0}});
      const first = reducer.applyEnvelope({{
        type: "audit.append", stream_epoch: "epoch-a", seq: 1,
        event_id: "audit-1", source: "DOM", kind: "wait_probe",
      }});
      const duplicate = reducer.applyEnvelope({{
        type: "audit.append", stream_epoch: "epoch-a", seq: 1,
        event_id: "audit-1", source: "DOM", kind: "wait_probe",
      }});
      const gap = reducer.applyEnvelope({{
        type: "audit.append", stream_epoch: "epoch-a", seq: 3,
        event_id: "audit-3", source: "DOM", kind: "wait_probe",
      }});
      const bounded = createLiveReducer();
      const items = [];
      for (let i = 0; i < 350; i += 1) {{
        items.push({{
          event_id: "audit-" + i, source: "DOM", kind: "wait_probe",
          at: new Date(1700000000000 + i).toISOString(),
        }});
      }}
      for (let i = 0; i < 130; i += 1) {{
        items.push({{
          event_id: "message-event-" + i, source: "TRANSCRIPT", kind: "message",
          at: new Date(1800000000000 + i).toISOString(),
          values: {{
            author: "assistant", message_id: "message-" + i,
            text: "m" + i, complete: true, projection_source: "listen",
          }},
        }});
      }}
      bounded.applySnapshot(items);
      console.log(JSON.stringify({{
        first, duplicate, gap,
        resetRequired: reducer.state.resetRequired,
        audit: bounded.viewModel().audit.length,
        messages: bounded.viewModel().messages.length,
        seen: bounded.state.seen.size,
      }}));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "first": True,
        "duplicate": True,
        "gap": False,
        "resetRequired": True,
        "audit": 300,
        "messages": 100,
        "seen": 480,
    }


def test_live_transcript_dom_reconcile_updates_existing_message_in_place():
    http_server = __import__("http.server").server

    class AssetHandler(http_server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ASSET_ROOT), **kwargs)

        def log_message(self, _format, *_args):
            return

    server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                __import__("pytest").skip("Playwright Chromium is unavailable")
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                base = f"http://127.0.0.1:{server.server_address[1]}"
                page.goto(base + "/")
                result = page.evaluate(
                    """async url => {
                      const {reconcileLiveList} = await import(url);
                      const currentList = document.createElement("div");
                      const nextList = document.createElement("div");
                      const current = document.createElement("article");
                      current.dataset.liveMessageKey = "assistant:turn-1";
                      current.dataset.liveSignature = "old";
                      current.className = "live-transcript-message transcript-assistant";
                      current.innerHTML = '<div class="live-transcript-head"><strong>ASSISTANT</strong><time data-time-at="2026-09-25T00:00:01Z">old time</time><span class="transcript-state streaming">streaming</span></div><pre class="live-transcript-text">ABCDEF</pre>';
                      const next = document.createElement("article");
                      next.dataset.liveMessageKey = "assistant:turn-1";
                      next.dataset.liveSignature = "new";
                      next.className = "live-transcript-message transcript-assistant";
                      next.innerHTML = '<div class="live-transcript-head"><strong>ASSISTANT</strong><time data-time-at="2026-09-25T00:00:02Z">new time</time><span class="transcript-state complete">complete</span></div><pre class="live-transcript-text">ABXY</pre>';
                      currentList.append(current);
                      nextList.append(next);
                      const originalCard = current;
                      const originalState = current.querySelector(".transcript-state");
                      reconcileLiveList(currentList, nextList, "liveMessageKey");
                      return {
                        sameCard: currentList.firstElementChild === originalCard,
                        sameState: current.querySelector(".transcript-state") === originalState,
                        text: current.querySelector(".live-transcript-text").textContent,
                        state: originalState.textContent,
                        stateClass: originalState.className,
                        count: currentList.children.length,
                      };
                    }""",
                    base + "/views/task_detail.js",
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result == {
        "sameCard": True,
        "sameState": True,
        "text": "ABXY",
        "state": "complete",
        "stateClass": "transcript-state complete",
        "count": 1,
    }


def test_live_stream_lifecycle_contract_is_follow_only_and_not_global_polling():
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ shouldLiveStream }} from {json.dumps(module)};
      const task = {{status: "RUNNING", active_role: "DEV"}};
      console.log(JSON.stringify([
        shouldLiveStream({{hidden: false, task, selectedTab: "live", focusMode: "FOLLOW", selectedRole: "DEV"}}),
        shouldLiveStream({{hidden: true, task, selectedTab: "live", focusMode: "FOLLOW", selectedRole: "DEV"}}),
        shouldLiveStream({{hidden: false, task, selectedTab: "overview", focusMode: "FOLLOW", selectedRole: "DEV"}}),
        shouldLiveStream({{hidden: false, task, selectedTab: "live", focusMode: "PINNED", selectedRole: "DEV"}}),
        shouldLiveStream({{hidden: false, task, selectedTab: "live", focusMode: "FOLLOW", selectedRole: "REVIEW"}}),
      ]));
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == [True, False, False, False, False]
    refresh = app.split("async function refresh()", 1)[1].split("\n}", 1)[0]
    assert "loadLiveAudit()" not in refresh
    assert 'current.roleFocusModeByTask.set(role.dataset.taskId, "PINNED")' in app
    assert 'current.roleFocusModeByTask.set(taskId, "FOLLOW")' in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert "liveController.close();" in app


def test_live_stream_controller_buffers_bootstrap_keeps_one_source_and_ignores_stale_source():
    module = (ASSET_ROOT / "live_stream.js").resolve().as_uri()
    script = f"""
      import {{ createLiveStreamController }} from {json.dumps(module)};
      const sources = [];
      const snapshots = [];
      const views = [];
      class FakeSource {{
        constructor(url) {{
          this.url = url;
          this.closed = false;
          this.listeners = new Map();
          sources.push(this);
        }}
        addEventListener(name, callback) {{ this.listeners.set(name, callback); }}
        emit(name, data) {{ this.listeners.get(name)?.({{data: JSON.stringify(data)}}); }}
        close() {{ this.closed = true; }}
      }}
      let resolveSnapshot;
      const controller = createLiveStreamController({{
        snapshotLoader: scope => new Promise(resolve => {{
          snapshots.push(scope.key);
          resolveSnapshot = resolve;
        }}),
        onView: view => views.push(view),
        eventSourceFactory: url => new FakeSource(url),
      }});
      const one = {{key: "task:DEV", url: "/stream/dev", snapshotUrl: "/snapshot/dev"}};
      controller.sync(one);
      controller.sync(one);
      sources[0].emit("stream.ready", {{stream_epoch: "epoch", seq: 0}});
      sources[0].emit("audit.append", {{
        type: "audit.append", stream_epoch: "epoch", seq: 1,
        event_id: "event-1", source: "DOM", kind: "wait_probe",
      }});
      resolveSnapshot({{items: []}});
      await new Promise(resolve => setTimeout(resolve, 0));
      const afterBootstrap = views.at(-1);

      const old = sources[0];
      const two = {{key: "task:REVIEW", url: "/stream/review", snapshotUrl: "/snapshot/review"}};
      controller.sync(two);
      old.emit("audit.append", {{
        type: "audit.append", stream_epoch: "epoch", seq: 2,
        event_id: "stale", source: "DOM", kind: "wait_probe",
      }});
      console.log(JSON.stringify({{
        sourceCount: sources.length,
        firstClosed: old.closed,
        snapshots,
        auditIds: afterBootstrap.audit.map(item => item.event_id),
        currentScope: controller.scopeKey,
      }}));
      controller.close();
    """
    result = __import__("subprocess").run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "sourceCount": 2,
        "firstClosed": True,
        "snapshots": ["task:DEV"],
        "auditIds": ["event-1"],
        "currentScope": "task:REVIEW",
    }
