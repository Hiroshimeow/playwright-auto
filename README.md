# playwright-auto

Persistent Chromium automation over local CDP, with a durable CDPA workflow runtime and an optional interactive noVNC browser viewer.

## Runtime layout

| Surface | Default | Purpose |
|---|---|---|
| Chromium CDP | `127.0.0.1:9222` | Automation transport; loopback only |
| noVNC viewer | `0.0.0.0:9223` | Interactive view of the Xvfb desktop |
| noVNC WebSocket | `127.0.0.1:9226` | Internal websockify bridge |
| VNC backend | `127.0.0.1:5901` | Internal x11vnc endpoint |
| CDPA dashboard | `0.0.0.0:9224` | Kanban, task controls, reports, runtime state |
| CDPA API | `127.0.0.1:9225` | Projection reads and durable command mailbox |
| Virtual display | `:100` | Chromium GUI display |

The browser viewer and automation path are independent. CDPA/Playwright connects to CDP `9222`; noVNC only exposes the pixels, keyboard, and mouse of the Xvfb display.

Do not call `browser.close()` on a browser obtained through `connect_over_cdp`; it closes the persistent Chromium process. Disconnect the Playwright client instead.

## Linux prerequisites

Required commands:

- Python 3.11+
- `uv`
- Node.js + PM2
- Chromium/Chrome
- Xvfb + `xdpyinfo`
- `x11vnc`
- `nginx`
- `curl` and `tar`

Optional overrides:

```bash
export CHROMIUM_BIN=/path/to/chromium
export XVFB_BIN=/path/to/Xvfb
export X11VNC_BIN=/path/to/x11vnc
export NGINX_BIN=/path/to/nginx
export PLAYWRIGHT_PROFILE_DIR=/path/to/persistent-profile
export PLAYWRIGHT_DISPLAY=:100
export PLAYWRIGHT_VIEWER_PORT=9223
```

The default Chromium profile is `$HOME/Workspace/playwright-profile`. It is intentionally outside the repository so repository cleanup does not remove the logged-in browser profile.

## Linux setup and start

Install Python dependencies and the pinned noVNC runtime:

```bash
uv sync
./scripts/install-novnc.sh
```

`install-novnc.sh` installs noVNC `v1.7.0` and websockify `0.13.0` under `.runtime/`; nothing is installed system-wide by that script.

Start the services:

```bash
pm2 start ecosystem.config.cjs
pm2 save
```

Expected services:

```text
playwright-display
playwright-vnc
playwright-novnc
playwright-browser
playwright-role-ui
playwright-dashboard-api
playwright-dashboard
playwright-cdpa-worker
```

Check them with:

```bash
pm2 status
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
curl http://127.0.0.1:9224/health
uv run python scripts/smoke.py
```

The viewer path is:

```text
Xvfb :100
   ↓
x11vnc 127.0.0.1:5901
   ↓
websockify 127.0.0.1:9226
   ↓
nginx/noVNC :9223
   ↓
browser viewer
```

The VNC and WebSocket backends stay on loopback. Port `9223` is the network-facing viewer. Keep it on a trusted network or put an appropriate access layer in front of it.

## Persistent Chromium behavior

`scripts/browser-gui.sh` starts Chromium on the Xvfb display with CDP bound to loopback. Chromium starts at `about:blank`; after CDP becomes ready the launcher opens the configured default URL through the DevTools HTTP endpoint. This avoids a native X11/VNC keyboard-input issue seen when ChatGPT is passed directly as Chromium's startup URL.

Default URL:

```bash
export PLAYWRIGHT_DEFAULT_URL=https://chatgpt.com/
```

The persistent profile survives PM2/browser restarts. Restarting the viewer does not restart Chromium.

## CDPA architecture

CDPA is a single-operator durable workflow runtime around real ChatGPT browser tabs.

```text
Dashboard :9224
    ↓ /api
Loopback API :9225
    ↓ durable command mailbox / SQLite projections
CDPA worker
    ↓
TaskStore + .plan manifests/reports
    ↓
Persistent Chromium via CDP :9222
```

Important ownership rule: **the worker is the only process that mutates TaskStore workflow state or controls ChatGPT tabs.** The frontend/API only read projections and enqueue commands.

Normal workflow roles are `PLAN`, `DEV`, `REVIEW`, `TEST`, and `AUDIT`. PLAN is the only normal role allowed to route a task to `DONE`.

A task preserves:

- task/team identity;
- active hop and role;
- exact request ID;
- accepted-send receipt and user-message identity;
- exact conversation URL and physical page binding;
- reports and route history;
- dependency/queue state;
- operator-control provenance.

Accepted sends are never intentionally replayed during recovery.

## Start CDPA directly

```bash
uv run playwright-dashboard-api --repository . --host 127.0.0.1 --port 9225 --config cdpa.yaml
uv run playwright-dashboard --host 0.0.0.0 --port 9224 --config cdpa.yaml
uv run cdpa-worker --repository . --config cdpa.yaml
```

The PM2 configuration starts the same three services for the repository runtime.

Submit a task:

```bash
cdpa "Implement and verify the requested behavior"
```

Examples:

```bash
cdpa "Build parent" --team alpha
cdpa "Build child" --team beta --depends-on <parent-task-id>
cdpa "Continue with the same team context" --reuse-team alpha
cdpa "Analyze these sources" --upload design.md
cdpa --team <exact-existing-team>
```

The dashboard provides durable controls such as Pause, Resume, Retry hop, Stop, Restart role, Open tab, New Chat, Route PLAN, Clear Team, and Change Goal. Controls are applied through the worker state machine rather than by direct frontend browser actions.

## Independent agents

Independent agents use the same manifest/task runtime but have one `AGENT` role instead of PLAN/DEV/REVIEW routing.

Built-in agents include Maintainers and Monitor. The runtime supports trigger-driven jobs, run-now jobs, bounded continuation cycles, repair creation, and exact-target controls. Recovery controls carry source task/event provenance and are rejected when the canonical event is stale.

A recurring independent agent returns to `WAITING / waiting_trigger` after its job. Operator Pause/Stop/Restart/New Chat/Clear Team remains authoritative.

## Workflow agents

Workflow-role definitions are configurable through the workflow-agent catalog. System routes remain stable route keys; custom workflow agents receive stable custom route keys and may be created, renamed, updated, or deleted through the supported control surface.

## Reports and recovery

Role responses use a file-backed report contract under `.plan/<team>/...`. Recovery distinguishes:

- pre-acceptance preparation from an accepted Send boundary;
- transport activity from a stable final assistant response;
- response validation from report materialization;
- operator intent from automatic recovery;
- immediate release from durable root-cause repair.

`PROBLEM.md` is the unresolved root-cause backlog. Once a repair/task is created from a problem and completed, remove that problem entry; durable operational rules belong in `LEARNING.md` or `.learning/`.

## Dashboard authentication

The public host guard reads the dashboard password from repository `.env`:

```dotenv
CDPA_DASHBOARD_PASSWORD=change-me
```

The loopback API remains on `127.0.0.1:9225`. Public requests to the dashboard require the password session; the dashboard frontend proxies `/api` to the loopback API after authentication.

## Browser roles

List currently open ChatGPT role tabs:

```bash
uv run playwright-roles --list
```

Assign roles:

```bash
uv run playwright-roles DEV REVIEW --tabs 1,2
```

`playwright-role-ui` also provides the in-page role badge/control. Role ownership includes task/team/page identity so a runner fails closed if the physical tab or conversation no longer matches its lease.

## Windows usage

The Linux Xvfb/noVNC/PM2 viewer stack is not required on Windows. If Chrome/Chromium is already running with loopback CDP `9222`, install the CLI globally:

```powershell
uv tool install --force "git+https://github.com/Hiroshimeow/playwright-auto.git@develop"
uv tool update-shell
```

Then, from the target repository:

```powershell
cd E:\python_project\target-repository
cdpa
```

Submit work from another terminal:

```powershell
cdpa "Implement and verify the requested behavior"
```

Useful commands:

```powershell
cdpa start
cdpa ui
cdpa --team <exact-team-name>
cdpa start --repository E:\python_project\target-repository
```

`Ctrl+C` stops the local CDPA frontend/API/worker started by the CLI; it does not close the existing browser on `9222`.

## Headless mode

Do not run GUI and headless Chromium simultaneously with the same profile.

```bash
pm2 stop playwright-browser playwright-vnc playwright-novnc playwright-display
uv run playwright-auto start --headless
uv run playwright-auto status
uv run playwright-auto stop
```

## Development verification

```bash
uv run python -m compileall -q src tests
uv run pytest -q
git diff --check
```

For browser/runtime changes, also verify the real persistent CDP instance and exact task/hop/page ownership before claiming acceptance.
