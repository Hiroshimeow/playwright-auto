from __future__ import annotations

import ast
import http.client
import json
import threading
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.dashboard import ASSET_ROOT, DASHBOARD_HTML_PATH, create_server


class UpstreamHandler(__import__("http.server").server.BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _handle(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size) if size else b""
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_frontend(tmp_path: Path, *, api_port: int):
    config = load_cdpa_config(None, repository_root=tmp_path)
    config = __import__("dataclasses").replace(config, dashboard_api_port=api_port)
    server = create_server(config, host="127.0.0.1", port=0)
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


def test_favicon_is_handled_without_application_404(tmp_path: Path):
    server, thread = start_frontend(tmp_path, api_port=65530)
    try:
        status, headers, body = request(server, "GET", "/favicon.ico")
        assert status == 204
        assert body == b""
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_static_path_traversal_and_symlinks_fail_closed(tmp_path: Path):
    server, thread = start_frontend(tmp_path, api_port=65530)
    try:
        status, _headers, _body = request(server, "GET", "/assets/../dashboard.py")
        assert status in {403, 404}
        status, _headers, _body = request(server, "GET", "/assets/%2e%2e/dashboard.py")
        assert status in {403, 404}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_frontend_assets_are_local_modular_and_suspend_hidden_polling():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    api = (ASSET_ROOT / "api.js").read_text(encoding="utf-8")
    polling = (ASSET_ROOT / "polling.js").read_text(encoding="utf-8")
    store = (ASSET_ROOT / "store.js").read_text(encoding="utf-8")
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


def test_board_uses_product_breakpoints_and_mobile_horizontal_pan():
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert ".board-grid {" in css
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in css
    assert "@media (min-width: 1600px)" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr));" in css
    assert "@media (max-width: 1199px)" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert "@media (max-width: 920px) and (min-width: 721px)" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "@media (max-width: 720px)" in css
    mobile = css.split("@media (max-width: 720px)", 1)[1]
    board = mobile.split(".board-grid {", 1)[1].split("}", 1)[0]
    lane = mobile.split(".lane {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: repeat(7, min(86vw, 340px));" in board
    assert "overflow-x: auto;" in board
    assert "overflow-y: hidden;" in board
    assert "scroll-snap-type: x proximity;" in board
    assert "scroll-snap-align: start;" in lane
    assert "grid-template-rows: auto minmax(0, 1fr);" in css
    assert "overflow-y: auto;" in css
    assert "scrollbar-gutter: stable;" in css
    assert "height: 388px;" in css
    assert "height: 150px;" in css
    assert "max-height: 152px;" not in css


def test_product_shell_keeps_board_full_width_and_detail_below():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'class="app-main"' in html
    assert 'class="board-section"' in html
    assert 'class="task-workspace"' in html
    assert ".app-main { width: 100%;" in css
    assert ".task-workspace {" in css
    assert "grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);" in css
    assert "position: fixed" not in css.split(".task-workspace", 1)[1].split("}", 1)[0]


def test_seven_lane_board_and_independent_agent_controls_are_explicit():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    runtime = (ASSET_ROOT / "views" / "runtime.js").read_text(encoding="utf-8")
    store = (ASSET_ROOT / "store.js").read_text(encoding="utf-8")

    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'const COLUMNS = ["RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED", "INDEPENDENT_AGENTS"]' in board
    assert 'INDEPENDENT_AGENTS: "INDEPENDENT AGENTS"' in board
    assert 'task.task_mode === "independent"' in board
    assert 'RUNNING for ${agent.target_team}' in board
    assert '"Run once"' in detail
    assert '"Command"' in detail
    assert '"Stop current job"' in detail
    assert '"Close tab"' in detail
    assert '"Renew"' in detail
    assert '"Settings"' in detail
    assert '"Reports"' in detail
    assert 'data-open-agent' in html
    assert 'id="agent-form"' in html
    assert 'name="mode"' in html
    assert '>Independent<' in html
    assert 'id="agent-settings-form"' in html
    assert '/api/independent-agents' in app
    assert "data-copy-task-id" in board
    assert 'task.started_at || task.created_at' in board
    assert 'text("span", task.active_role || "—", "task-role")' in board
    assert '"task-role-clock"' in board
    assert "taskSignature(task, state.board)" in board
    assert "activeRoleStartedAt(task)" in board
    assert 'const identity = `${task.active_role}:${task.active_hop_id ?? "none"}`' in board
    assert "waitingDisplay(task, board)" in board
    assert "Waiting for ${teams.join" in board
    assert 'current.dataset.signature !== signature' in board
    assert 'current.classList.toggle("selected", selected)' in board
    assert 'id="secondary-dialog"' in html
    assert '<dialog id="secondary-dialog"' in html
    assert 'roots.secondaryDialog.showModal()' in app
    assert 'event.target === roots.secondaryDialog' in app
    assert 'addEventListener("cancel"' in app
    assert 'addEventListener("popstate"' in app
    assert 'root.dataset.secondaryView !== "runtime"' in runtime
    assert "selectedRoleByTask" in store
    assert "detailCache" in store
    assert "laneScroll" in store


def test_role_availability_preserves_unknown_browser_state():
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'role.online == null ? "unknown"' not in board
    assert 'role.online == null ? "unknown"' in detail
    assert '.role-dots i[data-online="unknown"]' not in css
    assert '.role-row .unknown' in css
