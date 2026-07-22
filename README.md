# playwright-auto

Persistent Chromium automation through local CDP, with a low-latency interactive Selkies/WebRTC view.

## Endpoints

- CDP: `http://127.0.0.1:9222` (loopback only)
- Viewer: `http://<tailscale-ip>:9223/` (interactive browser screen)
- CDPA Kanban: `http://<tailscale-ip>:9224/` (task creation, durable controls, reports, and live telemetry)
- Profile: `.runtime/main-profile/` (inside this repository, ignored by Git)
- Display: `:100`

Port 9223 is only the visual keyboard/mouse view. Automation connects only to CDP 9222.

CDP clients must disconnect with `playwright.stop()` or `connected_browser(...)`. Do not call `browser.close()` after `connect_over_cdp`; it closes the persistent Chromium process and PM2 will restart it.

## Prerequisites

- Linux with Bash and Python 3.11 or newer.
- `uv`, Node.js, and PM2.
- Xvfb and a Chromium-family browser.
- Tailscale for private remote viewer access.
- Selkies GStreamer unpacked under `~/.local/opt/selkies-gstreamer`, or set `SELKIES_ROOT`.

Runtime executables are discovered automatically. On nonstandard installations, set:

```bash
export CHROMIUM_BIN=/path/to/chromium-or-chrome
export XVFB_BIN=/path/to/Xvfb
export SELKIES_ROOT=/path/to/selkies-gstreamer
# Only needed when Selkies has a nonstandard Python package layout:
export SELKIES_PYTHON_PACKAGE=/path/to/site-packages/selkies_gstreamer
```

## Start

```bash
uv sync
pm2 start ecosystem.config.cjs
pm2 save
```

```bash
pm2 status playwright-display playwright-selkies playwright-browser playwright-role-ui playwright-dashboard playwright-cdpa-worker
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
curl http://127.0.0.1:9224/health
uv run python scripts/smoke.py
```

Stop or restart with `pm2 stop|restart playwright-browser playwright-selkies playwright-display playwright-role-ui playwright-dashboard playwright-cdpa-worker`.

Selkies v1.6.2 is unpacked user-locally at `~/.local/opt/selkies-gstreamer`. The viewer has no application password and must stay inside the private Tailscale network. Do not expose port 9223 through Funnel or a public tunnel.

The virtual display is a separate PM2 service, so restarting Selkies does not restart Chrome or affect its profile.

### Windows with an existing CDP browser

The PM2/Xvfb/Selkies stack above is Linux-only. The Python CDPA dashboard, worker, and CLI
use cross-platform package entry points and do not require Bash or PM2. These steps assume
Chrome or Chromium is already running with loopback CDP on port `9222` and ChatGPT is already logged in in that browser profile.

Clone and prepare the repository once:

```powershell
git clone --branch develop https://github.com/Hiroshimeow/playwright-auto.git
cd playwright-auto
uv sync --frozen
Invoke-RestMethod http://127.0.0.1:9222/json/version
```

The last command must return browser/version information. If it cannot connect, do not
start CDPA yet; fix the browser's `--remote-debugging-port=9222` launch first.

Keep the following two processes running in separate PowerShell windows.

**PowerShell 1 — CDPA dashboard/UI on port 9224:**

```powershell
cd <path-to>\playwright-auto
uv run playwright-dashboard `
  --host 127.0.0.1 `
  --port 9224 `
  --cdp http://127.0.0.1:9222 `
  --config cdpa.yaml
```

**PowerShell 2 — persistent CDPA worker:**

```powershell
cd <path-to>\playwright-auto
uv run cdpa-worker --repository . --config cdpa.yaml
```

Open and verify the UI from a third PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:9224/health
Start-Process http://127.0.0.1:9224/
```

The health response must report `ok`, `task_store_ready`, and `cdp_connected` as `true`.
Create a task either from the **Create** dialog in the dashboard or from PowerShell:

```powershell
cd <path-to>\playwright-auto
uv run cdpa `
  "Implement and verify the requested behavior" `
  --repository . `
  --config cdpa.yaml
```

The CLI returns after writing the durable task. Leave the dashboard and worker windows
running; the worker attaches to the existing `9222` browser, creates/reuses ChatGPT tabs,
and advances the PLAN/DEV/TEST/REVIEW/AUDIT route. Progress, reports, Pause, Resume, Stop,
Retry, New Chat, Route PLAN, and Clear Team are available at `http://127.0.0.1:9224/`.
Task manifests and reports are stored under `.plan/` and survive process restarts.

Useful commands:

```powershell
# Resume one exact existing nonterminal team without creating another task
uv run cdpa --team <exact-team-name> --repository . --config cdpa.yaml

# Inspect currently open ChatGPT tabs and their role ownership
uv run playwright-roles --list

# Optional: keep the visible SET ROLE control injected across reloads
uv run playwright-role-ui
```

Press `Ctrl+C` in the dashboard or worker window to stop that process. Stopping either
process does not close the already-running Chrome instance. Use `--host 0.0.0.0` only when
the dashboard must be reached from another trusted machine, and restrict port `9224` with
the Windows firewall or a private network.

Do not run `uv pip install fcntl`; `fcntl` is a Unix standard-library module, not a PyPI
package. The durable ledger uses `msvcrt` locks on Windows and `fcntl` locks on Unix.

## Run a multi-role task

Open the private viewer and log in to ChatGPT once:

```text
http://<tailscale-ip>:9223/
```

Start a task with the default team `PLAN=1, DEV=2, REVIEW=1, TEST=1`:

```bash
uv run playwright-team "Implement and verify the requested feature"
```

Override role counts without editing Python:

```bash
uv run playwright-team \
  "Implement and verify the requested feature" \
  --team DEV=3,REVIEW=2,TEST=2
```

Preview allocation without sending anything:

```bash
uv run playwright-team "Implement the requested feature" --dry-run
```

If login or a recoverable runtime interruption blocks the task, the CLI persists its
manifest and prints the exact resume command:

```bash
uv run playwright-team --resume <task-id>
```

Each physical tab displays `⟦ROLE⟧` in its browser title and
`ROLE · TASK-ID · page-id` in a fixed in-page badge. The same task resumes its
existing role conversations. A new task uses New Chat after the whole participating
team passes draft/attachment/dialog/stream preflight.

### Change roles on tabs that are already open

No agent, Tampermonkey installation, or `chatgpt_probe.py` rerun is required. List only the
open ChatGPT tabs:

```powershell
uv run playwright-roles --list
```

Assign roles directly. When the browser has more tabs than roles, an interactive terminal
prompts for the correct tab for each role:

```powershell
uv run playwright-roles REVIEW REVIEW1
```

For scripts or CI, pass the tab indices printed by `--list`:

```powershell
uv run playwright-roles REVIEW REVIEW1 --tabs 3,4
```

Alternatively, run `uv run playwright-role-ui` and use the page itself:

1. Click the `SET ROLE` or current-role badge at the top center of the ChatGPT tab.
2. Enter any valid role such as `DEV`, `REVIEW`, `SECURITY`, or `REVIEW1`.
3. Click **Apply**. Use **Release** to leave the tab unassigned.

Changing a role preserves the current conversation URL, task ID, page ID, draft, and
streaming response. A workflow that already leased the old role fails its next ownership
check instead of continuing on the wrong tab. The next runner attaches the tab using its
new role. Duplicate role names across physical tabs turn both badges red; rename one role,
for example `REVIEW` → `REVIEW1`.

`playwright-role-ui` records changes in `.runtime/role-ui-events.jsonl` and reinjects the
control after reload/navigation while it is running. `chatgpt_probe.py` remains only an
emergency low-level fallback.

### Submit and control CDPA tasks

`cdpa` writes one durable manifest and returns immediately; the PM2 worker performs the
browser work. PLAN is created first, while DEV/TEST/REVIEW/AUDIT tabs are created only when
a validated route first needs them.

```bash
uv run cdpa "Implement and verify the requested behavior"
uv run cdpa "Implement and verify the requested behavior" --team release --new plan,dev
uv run cdpa "Implement and verify the requested behavior" --new-all
```

Port `9224` is the compact Kanban creation/control surface. It exposes durable Pause,
Resume, safe Retry, Stop, Restart role, Open tab, New Chat, Route PLAN, and Clear Team
requests. Controls are applied by the worker through the manifest state machine; the
browser endpoint never performs a blind Send. Reports are linked from each task card.
Keep port `9224` inside the private network because task titles and role state are visible.

```bash
uv run playwright-dashboard --host 0.0.0.0 --port 9224 --config cdpa.yaml
uv run cdpa-worker --repository . --config cdpa.yaml
# or through PM2:
pm2 start ecosystem.config.cjs --only playwright-dashboard,playwright-cdpa-worker
```

### Run a two-role review exchange

The short form defaults to `REVIEW` and `REVIEW1`:

```powershell
uv run python scripts/two_role_review_flow.py `
  "Review the repository for concrete correctness and operational defects" `
  --repo E:\git-project\qmh
```

Use arbitrary roles when the common flow is implementation followed by review:

```powershell
uv run python scripts/two_role_review_flow.py `
  "Implement the task, then independently review it" `
  --repo E:\git-project\target-repo `
  --roles DEV REVIEW
```

Bash uses the same arguments with `\` line continuations. The original `--task` form remains
supported for backward compatibility. `--repo` defaults to the current directory, and
`--roles` defaults to `REVIEW REVIEW1`. The MCP defaults to `mcp-thinkbook` on Windows and
`mcp-g8` on Unix; override it explicitly with `--mcp <tool-name>`.

The fixed round order is `ROLE_A → ROLE_B → ROLE_A`. The second role receives the first
report; the final role receives both earlier reports and returns one consolidated verdict.
The flow is durable, keeps one request ledger per role/round, and resumes completed turns
without resending them. It reads and tests the target repository but its standard review
prompt forbids editing, staging, resetting, committing, or deploying. Use `--task-id` to
provide a stable identity for an explicit resume or audit trail.

## Viewer implementation

Port 9223 uses this path:

```text
Xvfb :100 → Selkies GStreamer/WebRTC → x264 H.264 → browser viewer
```

It is not VNC/noVNC. The private Tailscale profile is video-only, uses UDP ICE only
for loopback/Tailscale/LAN addresses, and keeps port 9223 fixed. Current settings are
1280×720, 15 FPS, and 1.2 Mbps. The Selkies web and Python runtime patches are
generated under `.runtime/`; the installed Selkies package is not modified.

## ChatGPT workflows

`ChatGPTPage` wraps one tab. Reusable blocks are composed by `Workflow`:

```python
workflow = Workflow(
    "dev-task",
    [
        SetRoleBlock("DEV"),
        NewChatBlock(),
        SendPromptBlock(lambda ctx: ctx.require("prompt")),
        WaitStateBlock(ChatGPTState.RESPONDING),
        StopResponseBlock(),
    ],
)

run = await workflow.run(
    ChatGPTPage(page),
    variables={"prompt": "Implement phase 1"},
)
```

Blocks have stable IDs and can be changed without rewriting the runner:

```python
workflow.replace("set_role", SetRoleBlock("REVIEW"))
workflow.insert_after("set_role", CaptureSnapshotBlock("before_prompt"))
workflow.remove("stop_response")
```

For restart-safe Send/upload flows, start from `workflows/chatgpt_durable_loop.py`.
See `docs/chatgpt-workflows.md`, `docs/tampermonkey-edge-cases.md`, and
`scripts/chatgpt_workflow_example.py`.

## Headless

Stop both PM2 apps before reusing the profile headlessly:

```bash
pm2 stop playwright-browser playwright-selkies playwright-display
uv run playwright-auto start --headless
uv run playwright-auto status
uv run playwright-auto stop
```

GUI and headless must never run simultaneously with the same profile.
