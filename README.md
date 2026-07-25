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

`cdpa` starts the dashboard and persistent worker for the current directory, opens
`http://127.0.0.1:9224/`, and remains in the foreground. Keep that PowerShell window open.
Press `Ctrl+C` to stop the dashboard and worker; the existing Chrome process on port `9222`
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
# Start dashboard + worker and open the UI. `cdpa` alone does the same thing.
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
parents/children are derived for display, and a STOPPED parent leaves its child WAITING until
Maintainers repairs or replaces it.

PLAN remains the only task role allowed to finish with DONE. One global `MAINTAINERS` role
serves CDP 9222 outside all task teams and normal routes. It is the default recovery authority
for non-operator BLOCKED/STOPPED incidents and receives the complete task/hop/send/ownership,
control-origin, dependency/queue, report, error, repair, and runtime snapshot. It may choose up
to three bounded recovery steps or propose one repository-bounded repair task; the worker
validates immutable command snapshots and records a control as applied only after its recovery
postcondition succeeds. Repair proposals use one canonical validator at model parsing, command creation, and durable reload: root cause/reason are limited to 1200 characters, reproduction to 2400, source areas to 1–8 allowlisted entries, required tests to 1–16 one-line entries of at most 300 characters, and lesson to one optional paragraph of at most 600 characters. Every declared textual repair field must already be a JSON/string value; optional identity and lesson fields are null or strings. Source areas and required tests must be concrete arrays at the model boundary and list/tuple collections in worker code. The worker checks raw entry count before trimming or other normalization, rejects non-string items, rejects exact duplicates, and rejects values that become duplicates after trimming; it never applies `str()` coercion or silent duplicate collapse. Explicit operator Pause/Stop/Restart/New Chat/Clear Team is never
automatically reversed. Inline task reports are also materialized by the worker without
weakening route or provenance checks.

Repair proposals are root-cause deduplicated and urgent. One active repair may serve multiple
affected tasks, but every affected task, incident, and disposition is attached as a separate
idempotent operation; reusing the repair never skips the current task. A later durable decision
may change that task's disposition. `CONTINUE_IN_PARALLEL` keeps the safe affected task progressing
without an unnecessary dependency. `HOLD_FOR_REPAIR` atomically adds the repair task to the same
affected task's existing `depends_on_task_ids`, moves it to WAITING, preserves the exact active
hop/request/receipt/reports, and automatically resumes that same hop after repair DONE. Accepted
waiting work resumes response observation without resend; pre-send work continues the same
request. Repair relationships, disposition history, priority, dependency gate, and release event
are projected by `/api/tasks`, the unified timeline, and the selected-task Repair Relationships
panel. Environmental Maintainers failures suspend after three durable attempts. Browser/CDP
recovery requires `browser.is_connected()` plus a bounded live `context.cookies()` command;
network recovery uses a bounded no-redirect `HEAD` against the exact failed endpoint when available,
otherwise the current ChatGPT origin for ChatGPT transport failures; filesystem recovery performs a real
temporary write/fsync/delete under `.plan`. MCP/tooling is tracked separately. When `maintenance.tooling_probe` is configured, the production
Maintainers path preflights that worker-owned dependency before browser acquisition or Send. The
descriptor stores only dependency, canonical loopback endpoint, auth-profile reference, method, and
required tools; the bearer is resolved from the worker environment and is never persisted or projected.
Worker-owned sanitization strips URL userinfo/fragments and redacts sensitive query plus Authorization/credential values before manifest, global-state, prompt, dashboard, timeline, or report surfaces. URL path credentials are also secret material: JWT-like segments and directly credential-bearing segments are redacted, while exact or tokenized compound high-risk route markers such as webhooks, OAuth, reset, capability, signed-url, magic-link, or token start a fail-closed context. Before matching, percent-decoded camelCase/acronym boundaries are canonicalized. Separator-free labels are classified by a bounded exact credential-operation grammar: explicit qualifier+noun pairs cover access/refresh/id/api/bearer/auth/session/CSRF tokens, client secrets or credentials, API keys, session IDs, and verification/activation/invite/reset codes; explicit operation+suffix rules cover password-reset links and OAuth, authorization, magic-link, signed-URL, and webhook callback, redirect, or incoming routes. The grammar materializes exact compact identities only; generic prefix, suffix, and substring matching are forbidden. The marker segment itself and every remaining non-empty path segment are redacted, including bare markers and marker-plus-payload forms; static intermediary labels, version segments, callbacks, status names, and completion routes cannot end that context. Any URL containing the context is unprobeable unless the worker has an explicit secret-free descriptor/auth profile. Normal resource identifiers and near-match names outside exact high-risk contexts remain intact. The same worker-owned boundary applies before exception-derived operational text enters task/hop/role errors, block or waiting reasons, refresh state, cleanup state, route-repair evidence, or control results; dashboard task, hop, role, cleanup, route, error, and control projections sanitize those fields again as defense in depth. Maintainers and dashboard payloads receive only allowlisted incident summaries; stored full prompts and raw evidence arrays are excluded. A credential-bearing network URL without a secret-free endpoint and explicit auth profile remains suspended and is not probed.
The bounded Streamable HTTP lifecycle disables redirects, authenticates, performs `initialize`, sends `notifications/initialized` only when the server returns a session ID,
accepts only an empty successful notification response, verifies `tools/list` contains every required
capability, and requires successful deletion of that temporary session. Stateless servers use
`initialize` followed directly by `tools/list`. Missing credentials, malformed/non-loopback/
unallowlisted descriptors, protocol errors, or missing tools remain suspended. Cached pages and unrelated HTTP success are not recovery evidence.
`--upload` captures file identity at task creation, uploads the same bytes once per role
conversation generation, blocks pre-send source drift, and recovers crossed durable requests
without duplicate upload or Send. Dashboard attachment data contains only sanitized filename,
size, MIME type, and hash prefix—not raw paths or contents.

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


Repair creation is accepted only through the version-2 top-level `repair` object; legacy `CREATE_REPAIR_TASK` actions and recovery-list repair actions are rejected before report/control/task mutation.
