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
    assert 'data-dom-only' in html
    assert "DOM only" in html
    assert "runtime.settings?.dom_only" in runtime
    assert 'dataset.pending !== "true"' in runtime
    assert '"/api/runtime/settings"' in app
    assert 'method: "POST"' in app
    assert "response.data.settings" in app
    assert "previousDomOnly" in app
    assert "toast(error.message)" in app






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
    assert '/assets/app.js?v=20260907-live-audit-nav-v2' in html
    assert './views/notify.js?v=20260906-listen-controls-v1' in app
    assert 'data-notify-task-id' in notify
    assert 'workflowReportModel(detail, detail.active_role)' in notify
    assert 'reportBody(report, reportBodies)' in notify
    assert 'data-notify-back' in notify
    assert 'Open raw report' in notify
    assert 'No report yet' in notify
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
          {{logical_role: "PLAN", physical_role: "alpha-plan"}},
          {{logical_role: "DEV", physical_role: "alpha-dev"}},
          {{logical_role: "REVIEW", physical_role: "alpha-review"}},
          {{logical_role: "AUDIT", physical_role: "alpha-audit"}},
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
        "latest": [["PLAN", "/p2"], ["DEV", "/d2"], ["REVIEW", "/r2"], ["AUDIT", None]],
        "older": ["/d1", "/p1"],
        "running": "/d2",
        "fallback": "/r2",
        "done": "/p2",
        "explicit": "/p1",
        "explicitRole": "PLAN",
        "coverage": "3/4",
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
    assert "role.disabled = !item.latest;" in detail
    assert 'item.latest ? `Turn ${item.latest.turn ?? "—"}` : "No report"' in detail
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
    assert 'sameTask ? new Set' in detail
    assert 'querySelectorAll("details[data-disclosure-key][open]")' in detail
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
    assert '[["overview", "Overview"], ["live", "Live audit"], ["reports", `Reports ${model.coverage}`]]' in detail
    assert 'copy.dataset.copyRoleUrl = role.chat_url' in detail
    assert 'open.href = role.chat_url' in detail
    assert 'id: "task-roles"' in detail
    assert 'id: "task-live"' in detail
    assert 'id: "task-timeline"' in detail
    assert 'id: "task-reports"' in detail
    assert 'for (const source of ["DOM", "LISTEN", "CTRL", "ACTION"])' in detail
    assert 'current.liveEventsByScope.set(scope, response.data.items || [])' in app
    assert '.page-rail {' in css
    assert '.live-event-list {' in css
    assert '.role-url-actions {' in css
    assert 'grid-template-columns: 184px minmax(0, 1fr);' in css
    assert '.task-workspace {\n  display: block;' in css
    assert 'const inputSection = el("details", null, "role-input-section role-input-disclosure")' in detail
    assert 'inputSection.dataset.disclosureKey = `role-input-${inputRole}`' in detail
    assert 'section.className = "detail-section detail-card bootstrap-context-card"' in app
    assert 'disclosure.className = "bootstrap-context-disclosure"' in app


def test_runtime_dom_only_help_describes_both_true_modes_truthfully():
    runtime = (ASSET_ROOT / "views" / "runtime.js").read_text(encoding="utf-8")
    assert "DOM-only compatibility / rollback mode." in runtime
    assert "Listen + DOM mode with sparse stream_status" in runtime
    assert "no automation full-conversation graph reads" in runtime


def test_workflow_controls_fail_closed_from_projected_eligibility_with_reason_tooltips():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowOverview", 1)[1].split("function build", 1)[0]

    assert "detail.control_eligibility?.[action]" in workflow
    assert "eligible: false" in workflow
    assert "disabled: eligibility.eligible !== true" in workflow
    assert "reason: eligibility.reason || null" in workflow
    assert "node.title = String(reason)" in detail
    assert "node.dataset.disabledReason = String(reason)" in detail


def test_task_controls_render_before_task_prompt():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowOverview", 1)[1].split("function build", 1)[0]
    independent = detail.split("function independentOverview", 1)[1].split("function independentHistory", 1)[0]

    assert workflow.index("fragment.append(operations);") < workflow.index("fragment.append(taskSection);")
    assert independent.index("fragment.append(independentControls(detail));") < independent.index("fragment.append(section);")


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

def test_create_task_bootstrap_select_load_submit_and_detail_are_explicit():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert html.count('select name="bootstrap_id"') == 1
    assert "None / Fresh context" in html
    assert '"/api/bootstraps"' in app
    assert "loadBootstrapOptions" in app
    assert "data.default_id" in app
    assert "body.bootstrap_id" in app
    assert 'data-bootstrap-inline' in html
    assert 'name="bootstrap_new_id"' in html
    assert 'name="bootstrap_new_name"' in html
    assert 'name="bootstrap_new_source"' in html
    assert 'name="bootstrap_new_prewarm_prompt"' in html
    assert 'name="bootstrap_new_max_backups"' in html
    assert "body.bootstrap_definition" in app
    assert "source: bootstrapSource" in app
    assert 'name="bootstrap_kind"' not in html
    assert 'name="bootstrap_import_generated"' not in html
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
