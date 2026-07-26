# playwright-auto

Persistent Chromium automation through local CDP, with a multi-client interactive noVNC view.

## Endpoints

- CDP: `http://127.0.0.1:9222` (loopback only)
- Viewer: `http://<tailscale-ip>:9223/` (interactive browser screen)
- Profile: `.runtime/main-profile/` (inside this repository, ignored by Git)
- Display: `:100`

Port 9223 is only the visual keyboard/mouse view. Automation connects only to CDP 9222.

CDP clients must disconnect with `playwright.stop()` or `connected_browser(...)`. Do not call `browser.close()` after `connect_over_cdp`; it closes the persistent Chromium process and PM2 will restart it.

## Prerequisites

- Linux with Bash and Python 3.11 or newer.
- `uv`, Node.js, and PM2.
- Xvfb, `x11vnc`, nginx, `curl`, `tar`, and a Chromium-family browser.
- Tailscale for private remote viewer access.
- noVNC 1.7.0 and websockify 0.13.0 are installed under `.runtime/`.

Runtime executables are discovered automatically. On nonstandard installations, set:

```bash
export CHROMIUM_BIN=/path/to/chromium-or-chrome
export XVFB_BIN=/path/to/Xvfb
export X11VNC_BIN=/path/to/x11vnc
export NOVNC_ROOT=/path/to/noVNC
export WEBSOCKIFY_BIN=/path/to/websockify
```

## Start

```bash
uv sync
./scripts/install-novnc.sh
pm2 start ecosystem.config.cjs
pm2 save
```

```bash
pm2 status playwright-display playwright-vnc playwright-novnc playwright-browser playwright-role-ui
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
uv run python scripts/smoke.py
```

Stop or restart with `pm2 stop|restart playwright-browser playwright-novnc playwright-vnc playwright-display playwright-role-ui`.

The viewer uses noVNC 1.7.0 with websockify 0.13.0. x11vnc listens only on loopback port 5901 and accepts shared clients; web access is exposed on port 9223. The viewer has no application password and must stay inside the private Tailscale network. Do not expose port 9223 through Funnel or a public tunnel.

The display, VNC backend, web viewer, and Chromium are separate PM2 services. Restarting the viewer does not restart Chrome or affect its profile.

### Windows with an existing CDP browser

The PM2/Xvfb/noVNC stack above is Linux-only. On Windows, point the tools at a Chrome
instance already running with loopback CDP on port 9222:

```powershell
uv sync --frozen
Invoke-RestMethod http://127.0.0.1:9222/json/version
uv run playwright-roles --list
```

Do not run `uv pip install fcntl`; `fcntl` is a Unix standard-library module, not a PyPI
package. The durable ledger uses `msvcrt` locks on Windows and `fcntl` locks on Unix.

To keep the visible `SET ROLE` control injected across reloads, leave this running in a
separate PowerShell window:

```powershell
uv run playwright-role-ui
```

Use `uv run playwright-role-ui --once` when one-time injection is sufficient.

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

New task manifests default to workflow version 2. PLAN returns distinct assignments for
all DEV instances, REVIEW/TEST verify independently, DEV revises, REVIEW/TEST reverify,
and PLAN closes out from a deterministic accepted/blocked gate. Existing version-1 task
manifests continue to resume with their original round graph.

Requests are paced across the team. The known ChatGPT `Too many requests` dialog triggers
a bounded cooldown, safe `Got it` dismissal, and durable retry without resending an
already accepted prompt. Unknown dialogs still require manual intervention.

Normal runs require an authenticated profile and fail closed with
`waiting_for_login`. `--allow-guest` exists only for controlled testing because
anonymous ChatGPT sessions are not reliable for sustained multi-round work.

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
Xvfb :100 → x11vnc 127.0.0.1:5901 → websockify 127.0.0.1:9226
          → nginx/noVNC 0.0.0.0:9223 → browser viewers
```

x11vnc uses shared mode, so multiple desktop and mobile viewers can connect to the
same Chromium display simultaneously. noVNC scales the 1400×936 remote framebuffer
inside each client's viewport instead of letting clients fight over the X display size.
Legacy Selkies paths under `/webrtc/` return HTTP 410 and are never proxied into VNC.
The VNC protocol port is loopback-only; only the private Tailscale viewer port 9223 is
reachable remotely.

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
pm2 stop playwright-browser playwright-novnc playwright-vnc playwright-display
uv run playwright-auto start --headless
uv run playwright-auto status
uv run playwright-auto stop
```

GUI and headless must never run simultaneously with the same profile.
