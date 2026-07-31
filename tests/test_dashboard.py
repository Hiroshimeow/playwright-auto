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
    assert 'task.task_mode !== "independent" && task.status === "RUNNING"' in board
    assert 'task.status === "DONE"' in board
    assert 'completed_at' in board
    assert 'data-elapsed-at' not in board.split('if (agent)', 1)[-1].split('function ensureLane', 1)[0]

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
    assert 'button.dataset.renderDisabled === "true"' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/run' in app
    assert 'body: {trigger_type: "manual", instruction}' in app
    assert 'action: control.dataset.control' in app
    assert 'control.dataset.control === "reset"' in app
    assert '/api/independent-agents/${encodeURIComponent(taskId)}/delete' in app

    assert '<h2>Run task</h2>' in html
    assert '>Run task</button>' in html
    assert 'name="max_cycles"' in html
    assert 'Max turns per job' in html
    assert 'Workflow agent completed' in html
    assert 'Review all active workflow tasks' in html
    assert 'CHECK_ALL' not in html


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
