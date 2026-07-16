# playwright-auto

Persistent Chromium automation through local CDP, with a low-latency interactive Selkies/WebRTC view.

## Endpoints

- CDP: `http://127.0.0.1:9222` (loopback only)
- Viewer: `http://<tailscale-ip>:9223/` (interactive browser screen)
- Profile: `.runtime/main-profile/` (inside this repository, ignored by Git)
- Display: `:100`

Port 9223 is only the visual keyboard/mouse view. Automation connects only to CDP 9222.

CDP clients must disconnect with `playwright.stop()` or `connected_browser(...)`. Do not call `browser.close()` after `connect_over_cdp`; it closes the persistent Chromium process and PM2 will restart it.

## Prerequisites

- Linux with Bash and Python 3.11 or newer. Install the platform Tk package (for
  example `python3-tk`) to use `playwright-studio`.
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
pm2 status playwright-display playwright-selkies playwright-browser playwright-role-ui
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
uv run python scripts/smoke.py
```

Stop or restart with `pm2 stop|restart playwright-browser playwright-selkies playwright-display playwright-role-ui`.

Selkies v1.6.2 is unpacked user-locally at `~/.local/opt/selkies-gstreamer`. The viewer has no application password and must stay inside the private Tailscale network. Do not expose port 9223 through Funnel or a public tunnel.

The virtual display is a separate PM2 service, so restarting Selkies does not restart Chrome or affect its profile.

### Windows with an existing CDP browser

The PM2/Xvfb/Selkies stack above is Linux-only. On Windows, point the tools at a Chrome
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

## AI Multi-Agent Studio

The desktop studio is the primary interactive control surface for existing ChatGPT tabs.
It uses Tkinter and the current CDP/workflow APIs; it does not embed another browser and
never closes the persistent Chromium process.

Launch it on Windows or Linux:

```powershell
uv run playwright-studio
```

Useful options:

```powershell
uv run playwright-studio --cdp http://127.0.0.1:9222 --geometry 1480x900
uv run playwright-studio --runtime-dir .runtime/studio-test --no-auto-connect
```

Run a read-only discovery gate before opening the UI:

```powershell
uv run playwright-studio-smoke --pretty
```

The studio provides:

- Existing ChatGPT tabs as ordered worker cards. Unassigned tabs are visible but disabled
  until a role is applied and **Use** is selected.
- Direct role assignment/release, including custom roles.
- Drag-and-drop ordering. The active and completed prefix stays fixed; reordering during a
  run changes only workers that have not started.
- A pulsing green border around the active worker.
- Global goal, task ID, round count, response timeout, and context-limit controls.
- Per-worker prompt editing, latest response/history, elapsed time, stop, and release.
- **Stop & Retry** in the prompt editor for replacing an active worker attempt explicitly.
- All/System/per-role log tabs on the right, including complete worker responses.

Intervention semantics are deliberately fail-closed:

- **Pause** lets the accepted response finish and prevents the next worker from starting.
- **Stop** interrupts the active response when possible and skips remaining queued work.
- Editing a queued prompt applies immediately. Editing an active prompt affects the next
  attempt unless **Stop & Retry** is selected.
- The studio never deletes manual drafts, attachments, unknown dialogs, conversations, or
  unrelated tabs to make a task proceed.

Runtime state is ignored by Git and stored at:

```text
.runtime/studio/layout.json
.runtime/studio/events.jsonl
.runtime/studio/runs/<task-id>.json
```

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
