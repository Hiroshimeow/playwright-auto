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
    public = {"Host": "cdpa.hcu-lab.me", "Content-Type": "application/x-www-form-urlencoded"}
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
            headers={"Host": "cdpa.hcu-lab.me", "Content-Type": "application/x-www-form-urlencoded"},
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






def test_commands_fill_board_gap_without_moving_task_workspace():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    board_section_at = html.index('<section class="board-section"')
    board_at = html.index('<div id="board" class="board-grid"')
    commands_at = html.index('<aside id="commands"')
    board_section_end = html.index("</section>", board_section_at)
    workspace_at = html.index('<section class="task-workspace"')

    assert board_section_at < board_at < commands_at < board_section_end < workspace_at
    assert ".board-section {\n  display: grid;" in css
    assert ".board-grid {\n  display: contents;" in css
    assert ".board-section > .command-panel { grid-column: 1 / -1;" in css
    assert "@media (min-width: 1600px)" in css
    assert ".board-section > .command-panel { grid-column: span 3; }" in css
    assert "@media (max-width: 1199px)" in css
    assert ".board-section > .command-panel { grid-column: span 2;" in css
    assert "@media (max-width: 920px) and (min-width: 721px)" in css
    assert ".board-section > .command-panel { grid-column: 1 / -1; }" in css
    assert ".task-workspace {\n  display: grid;\n  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);" in css
    mobile = css.split("@media (max-width: 720px)", 1)[1]
    assert ".board-section { display: block; }" in mobile
    assert ".board-grid {\n    display: grid;" in mobile


def test_task_controls_render_before_task_prompt():
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    workflow = detail.split("function workflowContent", 1)[1].split("function build", 1)[0]
    independent = detail.split("function independentOverview", 1)[1].split("function independentHistory", 1)[0]

    assert workflow.index("fragment.append(controls);") < workflow.index("fragment.append(taskSection);")
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


def test_parent_dependencies_render_exact_remove_actions_through_shared_command_path():
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'detail.depends_on_task_ids' in detail
    assert '"Remove parent"' in detail
    assert 'dataset.removeParent' in detail
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
