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
pm2 status playwright-display playwright-selkies playwright-browser playwright-role-ui playwright-dashboard-api playwright-dashboard playwright-cdpa-worker
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
curl http://127.0.0.1:9224/health
uv run python scripts/smoke.py
```

Stop or restart with `pm2 stop|restart playwright-browser playwright-selkies playwright-display playwright-role-ui playwright-dashboard-api playwright-dashboard playwright-cdpa-worker`.

Selkies v1.6.2 is unpacked user-locally at `~/.local/opt/selkies-gstreamer`. The viewer has no application password and must stay inside the private Tailscale network. Do not expose port 9223 through Funnel or a public tunnel.

The virtual display is a separate PM2 service, so restarting Selkies does not restart Chrome or affect its profile.

### Windows: install `cdpa` once and use it anywhere

The PM2/Xvfb/Selkies stack above is Linux-only. On Windows, only the global `cdpa`
command is required. These steps assume Chrome or Chromium is already running with
loopback CDP on port `9222` and ChatGPT is logged in in that browser profile.

Install the command once from PowerShell:

```powershell
uv tool install --force "git+https://github.com/Hiroshimeow/playwright-auto.git@develop"
uv tool update-shell
```

Open a new PowerShell window after `uv tool update-shell`. There is no need to clone or
enter the `playwright-auto` repository, and no local `cdpa.yaml` is required. The installed
package contains the default CDPA configuration, role constructors, dashboard HTML, worker,
and CLI.

From the repository that CDPA should modify, run only:

```powershell
cd E:\python_project\target-repository
cdpa
```

`cdpa` starts three independent services for the current directory: the static frontend/proxy,
the loopback projection/command API, and the persistent worker. It opens
`http://127.0.0.1:9224/` and remains in the foreground. Keep that PowerShell window open.
Press `Ctrl+C` to stop frontend, API, and worker; the existing Chrome process on port `9222`
remains open.

Submit tasks from another PowerShell window:

```powershell
cd E:\python_project\target-repository
cdpa "Implement and verify the requested behavior"
```

The current directory is the default target repository, as with Codex or Gemini CLI. To run
from any other directory, pass the target explicitly:

```powershell
cdpa start --repository E:\python_project\target-repository
cdpa "Implement and verify the requested behavior" `
  --repository E:\python_project\target-repository
cdpa ui --repository E:\python_project\target-repository
```

Common commands:

```powershell
# Start the independent frontend, loopback API, and worker, then open the UI. `cdpa` alone does the same thing.
cdpa start

# Reopen the UI when the runtime is already running.
cdpa ui

# Resume one exact existing nonterminal team.
cdpa --team <exact-team-name>

# Use a repository-specific override when needed.
cdpa start --config E:\path\to\custom-cdpa.yaml
```

A `cdpa.yaml` in the target repository automatically overrides the packaged defaults.
Task manifests and reports remain inside the target repository under `.plan/`. One runtime
uses port `9224` and is bound to one repository at a time; stop it with `Ctrl+C` before
starting CDPA for another repository.

The following command is optional troubleshooting only. It checks whether the existing
browser exposes CDP; it does not configure or start CDPA:

```powershell
Invoke-RestMethod http://127.0.0.1:9222/json/version
```

Keep port `9224` on loopback unless a separately reviewed launcher and firewall policy
are added. Do not install `fcntl`; the durable ledger uses Windows `msvcrt` file locks.

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
cdpa "Build parent" --team alpha
cdpa "Build child" --team beta --depends-on <parent-task-id>
cdpa "Continue with same context" --reuse-team alpha --depends-on <other-task-id>
cdpa "Analyze uploaded sources" --team analysis --inline-report --upload design.md
cdpa --team <exact-existing-team>
```

`--team` allocates a readable team base and adds a suffix only when needed. `--reuse-team`
creates queued work for one exact existing team without suffix allocation; only one task owns
that team's role tabs at a time, and queued work reuses conversations through the guarded
rebind path. Dependencies are a durable DAG: each child stores only `depends_on_task_ids`,
and parents/children are derived for display.

PLAN remains the only workflow role allowed to finish a normal task with DONE. Independent
agents are a separate manifest-backed task mode with one `AGENT` role, one exact team, one
conversation, and no PLAN/DEV/TEST/REVIEW/AUDIT routing or route JSON. The independent task
itself is the standby/active job holder; the worker does not create a second scheduler, queue,
event store, coordinator, or sidecar.

Each enabled independent agent has exactly one nonterminal task. While idle it is WAITING for
a trigger. The worker claims the oldest eligible canonical event, sends the saved system prompt
and shared independent rule once per conversation generation, sends compact trigger context for
each job, and accepts plain Markdown. The agent must use explicit mailbox commands:

- `independent_task_control` for the exact target in its active event;
- `independent_create_repair` for a bounded normal repair task;
- `independent_activate_agent` to activate another agent by immutable name;
- `independent_continue` for another bounded cycle;
- `independent_complete` to finish the job.

The worker never parses action or repair JSON from assistant prose. After completion it creates
exactly one deterministic WAITING successor with the same immutable agent identity, exact team,
settings, watermarks, and saved conversation URL. Accepted sends are never replayed. New Chat is
deferred to the next job, and an idle independent tab closes after 30 minutes while retaining the
exact URL for reopen.

Maintainers and Monitor are built-in agents on this same engine. Maintainers owns the exclusive
unexpected BLOCKED/STOPPED recovery trigger by default and may use at most five cycles. A second
enabled agent cannot own that trigger. Monitor supports 30-minute, 60-minute, custom interval,
task-DONE, role-completion, selected team/state, CHECK_ALL, and Run-now triggers, and may activate
Maintainers without duplicating a worker-owned recovery claim. Custom independent agents require
only a unique name and system prompt; team-member mode is intentionally not implemented.

Explicit operator Pause, Stop, Restart role, New Chat, and Clear Team remain authoritative and
are never automatically reversed. Independent-agent target controls carry immutable source-task
and source-event provenance and are rejected when the canonical event is stale or the requested
target differs. A control is `applied` only after its action-specific operational postcondition
passes.

Repair requests are root-cause deduplicated and repository-bounded. `CONTINUE_IN_PARALLEL` keeps
safe work progressing. `HOLD_FOR_REPAIR` adds the repair dependency, moves the affected task to
WAITING, preserves the exact hop/request/receipt/report provenance, and releases that same hop
after repair DONE. Root cause/reason are limited to 1200 characters, reproduction to 2400,
source areas to 1–8 allowlisted entries, required tests to 1–16 one-line entries of at most 300
characters, and lesson to one optional paragraph of at most 600 characters.

Port `9224` is the static compact Kanban frontend and `/api` proxy. The loopback-only API listens
on port `9225`, reads compact SQLite projections, and enqueues durable commands. The worker is
the only process allowed to mutate TaskStore, `.plan`, dependency state, triggers, or Chrome.
The board includes a final **INDEPENDENT AGENTS** lane with one current card per enabled agent,
plus Run now, Enable/Pause, Stop current job, Retry, Open tab, Close tab, New Chat next job,
Settings, History, and Reports controls. The frontend and API never touch the filesystem or CDP
directly.

`--upload` captures file identity at task creation, uploads the same bytes once per role
conversation generation, blocks pre-send source drift, and recovers crossed durable requests
without duplicate upload or Send. Dashboard attachment data contains only sanitized filename,
size, MIME type, and hash prefix—not raw paths or contents.

Port `9224` is the static compact Kanban frontend and `/api` proxy. The loopback-only API
listens on port `9225`, reads compact SQLite projections, and enqueues durable commands.
The worker is the only process allowed to mutate TaskStore, `.plan`, dependency state, or Chrome.
The control surface exposes durable Pause,
Resume, safe Retry, Stop, Restart role, Open tab, New Chat, Route PLAN, and Clear Team
requests. Controls are applied by the worker through the manifest state machine; the
browser endpoint never performs a blind Send. Reports are linked from each task card.
Keep port `9224` inside the private network because task titles and role state are visible.

```bash
uv run playwright-dashboard-api --repository . --host 127.0.0.1 --port 9225 --config cdpa.yaml
uv run playwright-dashboard --host 0.0.0.0 --port 9224 --config cdpa.yaml
uv run cdpa-worker --repository . --config cdpa.yaml
# or through PM2:
pm2 start ecosystem.config.cjs --only playwright-dashboard-api,playwright-dashboard,playwright-cdpa-worker
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
