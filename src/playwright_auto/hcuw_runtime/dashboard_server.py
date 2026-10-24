from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .config import RuntimeConfig


class DashboardServerError(RuntimeError):
    """Raised when the dashboard server cannot complete its owner work."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class DashboardResponse:
    status: int
    body: bytes
    content_type: str


@dataclass
class DashboardServer:
    """Owner of the read-only dashboard and same-origin API proxy."""

    config: RuntimeConfig

    def handle_get_path(self, path: str) -> DashboardResponse:
        request_path = urlsplit(path).path
        if request_path == "/" or request_path == "/index.html":
            return self.serve_index()
        if request_path.startswith("/api/"):
            return self.proxy_api_request(request_path)
        return self._json_error_response(
            404,
            "dashboard_route_not_found",
            f"No dashboard route exists for {request_path}",
        )

    def serve_index(self) -> DashboardResponse:
        return DashboardResponse(
            status=200,
            content_type="text/html; charset=utf-8",
            body=_dashboard_html(self.config).encode("utf-8"),
        )

    def proxy_api_request(self, path: str) -> DashboardResponse:
        try:
            body = self._fetch_api_bytes(path)
        except urllib.error.URLError as exc:
            return self._json_error_response(
                503,
                "api_unavailable",
                f"Runtime-v2 API is unavailable: {exc.reason}",
            )
        except OSError as exc:
            return self._json_error_response(
                503,
                "api_unavailable",
                f"Runtime-v2 API is unavailable: {exc}",
            )
        return DashboardResponse(
            status=200,
            content_type="application/json; charset=utf-8",
            body=body,
        )

    def start(self) -> ThreadingHTTPServer:
        handler = self._build_request_handler()
        try:
            return ThreadingHTTPServer(
                (self.config.dashboard_host, self.config.dashboard_port),
                handler,
            )
        except OSError as exc:
            raise DashboardServerError(
                "port_in_use",
                "runtime-v2 dashboard could not bind "
                f"{self.config.dashboard_host}:{self.config.dashboard_port}",
            ) from exc

    def serve_forever(self) -> None:
        server = self.start()
        try:
            server.serve_forever()
        finally:
            server.server_close()

    def _fetch_api_bytes(self, path: str) -> bytes:
        url = f"http://{self.config.api_host}:{self.config.api_port}{path}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read()

    def _json_error_response(self, status: int, code: str, message: str) -> DashboardResponse:
        payload = {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "details": {},
            },
            "diagnostics": [],
        }
        return DashboardResponse(
            status=status,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    def _build_request_handler(self) -> type[BaseHTTPRequestHandler]:
        dashboard = self

        class DashboardRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                response = dashboard.handle_get_path(self.path)
                self._write_response(response)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if not self.path.startswith("/api/"):
                    response = dashboard._json_error_response(
                        404,
                        "dashboard_route_not_found",
                        f"No dashboard route exists for {urlsplit(self.path).path}",
                    )
                    self._write_response(response)
                    return
                response = dashboard.proxy_post_api_request(
                    self.path,
                    self._read_body(),
                    idempotency_key=self.headers.get("Idempotency-Key"),
                )
                self._write_response(response)

            def log_message(self, format: str, *args: object) -> None:
                return

            def _read_body(self) -> bytes:
                try:
                    length = max(0, int(self.headers.get("Content-Length") or "0"))
                except ValueError:
                    length = 0
                return self.rfile.read(length)

            def _write_response(self, response: DashboardResponse) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

        return DashboardRequestHandler

    def proxy_post_api_request(
        self, path: str, body: bytes, *, idempotency_key: str | None = None
    ) -> DashboardResponse:
        try:
            response_body = self._post_api_bytes(
                path, body, idempotency_key=idempotency_key
            )
        except urllib.error.HTTPError as exc:
            # API validation errors keep their own status and envelope.
            return DashboardResponse(
                status=exc.code,
                content_type="application/json; charset=utf-8",
                body=exc.read(),
            )
        except urllib.error.URLError as exc:
            return self._json_error_response(
                503,
                "api_unavailable",
                f"Runtime-v2 API is unavailable: {exc.reason}",
            )
        return DashboardResponse(
            status=202,
            content_type="application/json; charset=utf-8",
            body=response_body,
        )

    def _post_api_bytes(
        self, path: str, body: bytes, *, idempotency_key: str | None = None
    ) -> bytes:
        url = f"http://{self.config.api_host}:{self.config.api_port}{path}"
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.read()


def _dashboard_html(config: RuntimeConfig) -> str:
    return "\n".join(
        (
            '<!doctype html>',
            '<html lang="en">',
            '<head>',
            '  <meta charset="utf-8">',
            '  <title>HCUW Runtime V2 Audit</title>',
            _dashboard_style(),
            '</head>',
            _dashboard_body(),
            _dashboard_script(config.dashboard_min_refresh_seconds),
            '</html>',
        )
    )


def _dashboard_style() -> str:
    return """
  <style>
    body { font-family: system-ui, sans-serif; margin: 1.5rem; line-height: 1.4; }
    header { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1rem; }
    section { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; }
    textarea { font-family: ui-monospace, monospace; width: 100%; box-sizing: border-box; }
    pre { white-space: pre-wrap; word-break: break-word; background: #f6f6f6; padding: 0.75rem; max-height: 24rem; overflow: auto; }
    button { padding: 0.5rem 0.9rem; }
    .status { font-weight: 700; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ddd; padding: 0.3rem 0.5rem; text-align: left; font-size: 0.9rem; }
    tr.latest { background: #eef6ff; }
    tr.blocked td { color: #a00; }
  </style>"""


def _dashboard_body() -> str:
    return """
<body>
  <header>
    <h1>HCUW Runtime V2 Audit</h1>
    <span class="status" id="refresh-mode">Lazy refresh: active when tab is focused</span>
    <span id="last-refresh">Last refresh: never</span>
  </header>
  <p>Audit dashboard. API 9227 can submit a real CDPA task through the task runner.</p>

  <div class="grid">
    <section><h2>API status</h2><pre id="health">Loading...</pre></section>
    <section><h2>Runtime config</h2><pre id="config">Loading...</pre></section>
    <section><h2>Runs</h2><pre id="runs">Loading...</pre></section>
    <section><h2>Endpoints</h2><pre id="endpoints">Loading...</pre></section>
  </div>

  <section>
    <h2>Submitted tasks</h2>
    <table>
      <thead><tr><th>Latest</th><th>Runtime task</th><th>Legacy task</th><th>Team</th><th>Roles</th><th>Start role</th><th>Active role</th><th>Status</th><th>Block code</th><th>Browser control / WAIT</th><th>Updated</th></tr></thead>
      <tbody id="task-rows"><tr><td colspan="11">Loading...</td></tr></tbody>
    </table>
  </section>

  <section>
    <h2>Real CDPA Task via 9227</h2>
    <p>This submits a Team Task through <code>POST /api/tasks</code>. Solo Task and Independent Agent paths are disabled here. It may start legacy CDPA 9225 when needed.</p>
    <textarea id="task-text" rows="5">Review this repository at a high level. Do not modify files. Return a concise risk summary and stop.</textarea>
    <p><label>Roles <input id="team-roles" value="DEV,REVIEW" size="42" placeholder="ROLE_A,ROLE_B"></label> (comma-separated role keys; first role is the default start/closer)</p>
    <p><label>Route targets <input id="route-targets" value="" size="32" placeholder="optional lazy role keys"></label></p>
    <p><label>Start role <input id="start-role" value="" size="20" placeholder="first normalized role"></label></p>
    <p><label>Closing role <input id="closing-role" value="" size="20" placeholder="first normalized role"></label></p>
    <p><button id="submit-task">Submit real CDPA team task</button></p>
    <pre id="task-submit">No task submitted from dashboard yet.</pre>
  </section>

  <h2>Realtime Observation Audit</h2>
  <div class="grid">
    <section><h3>DOM source</h3><p class="status" id="dom-status">Loading...</p><pre id="dom-source">Loading...</pre></section>
    <section><h3>Mutation source</h3><p class="status" id="mutation-status">Loading...</p><pre id="mutation-source">Loading...</pre></section>
    <section><h3>LISTEN source</h3><p class="status" id="listen-status">Loading...</p><pre id="listen-source">Loading...</pre></section>
    <section><h3>Actions</h3><p class="status" id="actions-status">Loading...</p><pre id="actions-source">Loading...</pre></section>
  </div>
  <section><h2>Timeline</h2><pre id="audit-events">Loading...</pre></section>
  <section><h2>Gate B Runtime Diagnostics</h2><p>Shows Cron/Heartbeat triggers, TeamTask dependency/continuation state, and migration-only Independent Agent status. No second dependency graph is stored.</p><pre id="gateb-diagnostics">Loading...</pre></section>
  <section><h2>API diagnostics</h2><pre id="diagnostics">No diagnostics loaded yet.</pre></section>
"""


def _dashboard_script(refresh_seconds: int) -> str:
    return f"""
  <script>
    const minRefreshSeconds = {refresh_seconds};
    let pendingTaskSubmitKey = null;
    async function loadJson(path) {{
      const response = await fetch(path, {{ cache: 'no-store' }});
      return await response.json();
    }}
    function showJson(id, value) {{
      document.getElementById(id).textContent = JSON.stringify(value, null, 2);
    }}
    function sourceByName(snapshot, name) {{
      return snapshot.data.sources.find((item) => item.source === name) || {{ source: name, status: 'missing' }};
    }}
    function renderSource(snapshot, name, statusId, bodyId) {{
      const source = sourceByName(snapshot, name);
      document.getElementById(statusId).textContent = source.status + ': ' + (source.message || '');
      showJson(bodyId, source);
    }}
    async function refreshAudit() {{
      const snapshot = await loadJson('/api/audit/snapshot');
      renderSource(snapshot, 'dom', 'dom-status', 'dom-source');
      renderSource(snapshot, 'mutation', 'mutation-status', 'mutation-source');
      renderSource(snapshot, 'listen', 'listen-status', 'listen-source');
      renderSource(snapshot, 'actions', 'actions-status', 'actions-source');
      showJson('audit-events', snapshot.data.events);
    }}
    function controlSummary(item) {{
      const control = item.browser_control || {{}};
      const wait = item.async_continuation || null;
      const parts = [];
      if (item.browser_control) {{
        parts.push('stop=' + control.stop_visible, 'retry=' + control.retry_visible,
          'allow=' + (control.permission_prompt_count || 0) + '/' + (control.permission_click_count || 0) + ' clicked');
        if (control.last_permission_click) parts.push('last allow=' + control.last_permission_click.result);
        if (control.retry_decision) parts.push('retry decision=' + control.retry_decision.result);
        if (control.controller_decision || control.controller_reason) parts.push(
          'controller=' + [control.controller_decision, control.controller_reason].filter(Boolean).join('/')
        );
        if (control.terminal_reconciliation) parts.push('terminal=' + control.terminal_reconciliation.result + '@' + control.terminal_reconciliation.source);
      }}
      if (wait) {{
        parts.push('wait ' + wait.continuation_id + '=' + wait.state,
          'callback=' + (wait.callback ? wait.callback.status : 'pending'),
          'wake=' + (wait.wake ? wait.wake.status : 'none'));
      }}
      return parts.join('; ');
    }}
    function renderTasks(payload) {{
      const body = document.getElementById('task-rows');
      const items = (payload && payload.data && payload.data.items) || [];
      if (!items.length) {{
        body.innerHTML = '<tr><td colspan="11">No runtime-v2 tasks submitted yet.</td></tr>';
        return;
      }}
      body.textContent = '';
      for (const item of items) {{
        const row = body.insertRow();
        if (item.latest) row.classList.add('latest');
        if (item.status === 'BLOCKED') row.classList.add('blocked');
        const cells = [
          item.latest ? 'current' : 'older', item.runtime_task_id, item.legacy_task_id, item.team,
          (item.roles || []).join(','), item.start_role, item.active_role, item.status,
          item.block_code, controlSummary(item), item.updated_at,
        ];
        for (const value of cells) {{
          row.insertCell().textContent = value == null ? '' : String(value);
        }}
      }}
    }}
    async function refreshOnce() {{
      const targets = [
        ['health', '/api/health'],
        ['config', '/api/config'],
        ['runs', '/api/runs'],
        ['endpoints', '/api/endpoints'],
      ];
      const diagnostics = [];
      for (const [id, path] of targets) {{
        try {{
          const data = await loadJson(path);
          showJson(id, data);
          if (Array.isArray(data.diagnostics)) diagnostics.push(...data.diagnostics);
        }} catch (error) {{
          document.getElementById(id).textContent = 'API unavailable: ' + error;
          diagnostics.push({{ code: 'api_unavailable', message: String(error) }});
        }}
      }}
      try {{
        renderTasks(await loadJson('/api/tasks'));
      }} catch (error) {{
        diagnostics.push({{ code: 'tasks_unavailable', message: String(error) }});
      }}
      try {{
        showJson('gateb-diagnostics', await loadJson('/api/diagnostics'));
      }} catch (error) {{
        document.getElementById('gateb-diagnostics').textContent = 'diagnostics unavailable: ' + error;
        diagnostics.push({{ code: 'gateb_diagnostics_unavailable', message: String(error) }});
      }}
      try {{
        await refreshAudit();
      }} catch (error) {{
        diagnostics.push({{ code: 'audit_unavailable', message: String(error) }});
      }}
      showJson('diagnostics', diagnostics);
      document.getElementById('last-refresh').textContent = 'Last refresh: ' + new Date().toISOString();
    }}
    async function submitTask() {{
      const parseRoleList = (value) => value.split(',').map((item) => item.trim()).filter(Boolean);
      const roles = parseRoleList(document.getElementById('team-roles').value);
      const routeTargets = parseRoleList(document.getElementById('route-targets').value);
      const payload = {{
        task: document.getElementById('task-text').value,
        browser: '9222',
        roles
      }};
      if (routeTargets.length) payload.route_targets = routeTargets;
      const startRole = document.getElementById('start-role').value.trim();
      const closingRole = document.getElementById('closing-role').value.trim();
      if (startRole) payload.start_role = startRole;
      if (closingRole) payload.closing_role = closingRole;
      try {{
        if (!pendingTaskSubmitKey) pendingTaskSubmitKey = crypto.randomUUID();
        const response = await fetch('/api/tasks', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'Idempotency-Key': pendingTaskSubmitKey,
          }},
          body: JSON.stringify(payload)
        }});
        const data = await response.json();
        if (response.ok) pendingTaskSubmitKey = null;
        showJson('task-submit', data);
        await refreshOnce();
      }} catch (error) {{
        document.getElementById('task-submit').textContent = 'submit failed: ' + error;
      }}
    }}
    function isDashboardFocused() {{
      return document.visibilityState === 'visible' && document.hasFocus();
    }}
    async function lazyRefreshTick() {{
      const mode = document.getElementById('refresh-mode');
      if (!isDashboardFocused()) {{
        mode.textContent = 'Lazy refresh: paused until tab is focused';
        return;
      }}
      mode.textContent = 'Lazy refresh: active, polling every 0.5s';
      await refreshOnce();
    }}
    document.getElementById('submit-task').addEventListener('click', submitTask);
    document.addEventListener('visibilitychange', lazyRefreshTick);
    window.addEventListener('focus', lazyRefreshTick);
    window.addEventListener('blur', lazyRefreshTick);
    lazyRefreshTick();
    window.setInterval(lazyRefreshTick, 500);
  </script>
</body>"""
