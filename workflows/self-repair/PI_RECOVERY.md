# Pi Recovery Record

- Recorded UTC: 2026-10-27T08:12:00+00:00
- Host/repository/branch: DESKTOP-B5E4IUE / `E:\git-project\cdpa` / `rebuild/runtime-v2-r11`
- Campaign/task: `g6-cdpa-r11-gate-c-20261027` / `cdpa-idem-86b069e80cbb1060363c3a93`
- Slice/team: `01b-terminal-recovery` / `g6-selfrepair-02-01b-terminal-recovery`
- Release: `NOT_ACCEPTED`

## Exact incident

The active TeamTask is durably `BLOCKED` with `block_code=provider_challenge`, not
`response_terminal_missing`. The accepted-send ledger has one hop and one accepted
Send (`send:click` 07:42:15.683545, `send_acceptance` 07:42:17.079748,
`outcome=accepted`, `accepted_via=exact_user_message`). There is no second Send.
Immediately after acceptance the exact owned page reported `cloudflare_challenge Retry`,
then its DOM probe had no assistant text, no assistant turn, no Stop, no Retry button,
and one HTTP error. RoleController therefore selected `provider_challenge` before any
terminal-missing path. The evidence is in `.runtime/live-events.jsonl` records for
`cdpa-idem-86b069e80cbb1060363c3a93` and `/api/tasks/<task_id>`.

The earlier inventory task separately reached `response_terminal_missing` after its
assistant DOM remained empty; that is historical evidence, not the active task's current
block code. The requested terminal-DOM repair is therefore not proven by this incident:
the provider challenge prevented a terminal response from being rendered.

## Recovery diagnosis

- `/health`: `ok=true`, worker online/ready/runtime-ready/command-loop-ready,
  `worker_status=HEALTHY`, `worker_stale=false`.
- Pinned endpoint `4bc12276-da9c-4a7a-8ef9-ddcfcca6113f` at
  `http://127.0.0.1:9202`: connected, online, `chatgpt_ready=true`, not draining or
  removed. This does not clear the provider challenge.
- Current task exposes Resume/Retry eligibility, but those controls must not be used:
  the Send was accepted and the provider outcome is not reconciled. Retrying would risk
  replaying an irreversible request; resuming would not establish terminal evidence.
- Pi recovery attempts were exhausted by the supervisor's bounded Pi CLI invocation:
  `TimeoutExpired` after two attempts. This is a coordinator availability failure,
  not evidence that CDPA or the browser is safe to restart.
- No browser, CDPA worker, endpoint, unrelated tab, or durable task was killed,
  reloaded, resumed, or retried during this recovery.

## Verification and local change decision

- `uv run pytest -q tests/test_spec003_gate_c_runtime.py tests/test_gate_c_direct_chat_worker.py`:
  `22 passed`.
- `uv run pytest -q tests/test_role_runtime.py -k
  "cloudflare_challenge or terminal_missing or terminal_route_reconciled"`:
  `1 passed`.
- Fresh `git diff --check -- src tests`: pass (only existing LF/CRLF warnings).
- No product or test correction was proven necessary or safely applicable. The working
  tree remains otherwise untouched; existing dirty/untracked work is preserved.

## Follow-up incident: 01c fresh retry

- Recorded UTC: 2026-10-27T08:30:00+00:00
- Task: `cdpa-idem-ba7a0091c4a42870bc74c695`
- Slice/team: `01c-terminal-fresh-retry` / `g6-selfrepair-03-01c-terminal-fresh-retry`
- Durable state: `BLOCKED`, `response_terminal_missing`, version `11`.

The fresh task did complete one new Send on a new conversation. Evidence shows one
`send_acceptance` at 08:14:31Z via `user_message_identity`, with no challenge marker,
followed by DOM evidence for the exact bound turn. The assistant text was not a final
route: it was a progress/status response (105, 200, 120, and 269 characters), while
Stop remained visible. It then changed to a new empty assistant shell (`message_id`
`6e98674c-476e-43a1-abd3-451dfce6ee49`) and later Stop disappeared. The final probe
had assistant length 0, no Retry, no errors, and diagnostics showing `other_conversation`,
`stale_document`, and request failures. RoleController consequently blocked with
`response_terminal_missing`; it did not have valid terminal route evidence to commit.
The worker's subsequent `close_tab` events are historical task cleanup, not a recovery
action taken here.

The `01c` receipt reports the bounded synthetic h3 regression and source-owner fix in
`chatgpt.py`; fresh verification also passed the affected suite: `96 passed` across
`test_role_runtime.py`, `test_role_runtime_browser.py`, `test_cdpa_repair.py`, and
`test_spec003_gate_c_runtime.py`. This proves the h3/h4 semantic-heading case, but not
real-browser terminal acceptance for this task. No additional product/test correction
is proven by the live evidence, so none was applied in this recovery.

## Current safety checks

- `/health`: worker `HEALTHY`, online/ready/runtime-ready/command-loop-ready, not stale,
  no source drift.
- Pinned 9202 endpoint: online, connected, `chatgpt_ready=true`, not removed or draining.
- The accepted Send has no terminal result and must not be replayed. Do not use the
  exposed Retry or Resume controls, create another fresh retry, reload a browser, or
  restart the worker from this recovery. The authorized fresh retry has already been
  consumed and the task is safely held for explicit reconciliation.
- Release remains `NOT_ACCEPTED`.

## Safe route

`PI_RECOVERY_HOLD`: preserve the exact accepted-send ledger and task identity. The
current block is a genuine missing-terminal-result outcome, not proof of a remaining
DOM-selector defect. Continue only after explicit reconciliation or operator evidence
of the exact bound conversation; never blindly Send, Retry, Resume, reload, or close/reopen
the pinned browser/worker.

## Coordinator recovery: 06 async-wait blocked and bounded CDPA reload

- Recorded UTC: 2026-10-27T12:59:30+00:00.
- Host/repository/branch: `DESKTOP-B5E4IUE` / `E:\git-project\cdpa` /
  `rebuild/runtime-v2-r11`.
- Task: `cdpa-idem-bdaa8331af55fe1c962c5d9e`, team
  `g6-selfrepair-08-06-async-wait`, slice `06-async-wait`.

### Exact blocked state and accepted-Send ledger

The authoritative task was `BLOCKED`, `active_action=blocked`,
`block_code=permission_review`, `hop_state=waiting`, with `stop_visible=true`,
`retry_visible=false`, and one permission-review prompt. The existing request ledger
contains exactly one request for hop 1, `attempts=1`, status `sent`, and one accepted
Send event at `2026-10-27T12:25:56Z` (`accepted_via=exact_user_message`). No second
Send, Retry, Resume, or route completion was performed. The browser evidence did not
establish a terminal route; the permission review was an actionable block, not a
terminal result.

Pi issued exactly one idempotent official Stop with idempotency key
`pi-recovery-stop-20261027-130000-bdaa8331`, returned command
`cmd-7a2afdf6-6c9b-4270-8218-5f1543205a78`, and reconciled it to `status=applied` with
`result.stopped_response=false`. The task is now terminal `STOPPED`, version `132`,
with stop reason preserving the accepted Send. The previous task must not be resumed,
retried, or resent.

### Source/test evidence

The pre-existing dirty slice change in `cdpa_async_continuation.py` requires the
active hop to be `sent` or `waiting` and to contain a receipt mapping before async
source registration; the corresponding failing-first tests are present in
`tests/test_spec003_gate_c_runtime.py`. This is code-only evidence and does not prove
real-browser release acceptance. Receipt: `workflows/self-repair/receipts/06-async-wait.json`.

Focused offline verification after reload:

- `46 passed` in
  `tests/test_spec003_gate_c_runtime.py`, `tests/test_gate_c_direct_chat_worker.py`,
  `tests/test_cdpa_browser_management.py`, and `tests/test_cdpa_browser_admission.py`.
- `git diff --check -- src tests workflows/self-repair`: PASS, with existing
  LF/CRLF warnings only.

### CDPA runtime reload outcome

Before reload, `/health` was healthy but the source identity was from the prior worker
start. The exact CDPA launcher tree was stopped through `reload_cdpa.ps1`; the script
terminated only the G6 CDPA launcher/API/dashboard/worker process tree. ChatGPT CDP
9202 and 9212 were not stopped or reloaded. The launcher restarted successfully and
reported `RELOAD_OK worker_ready=true source_drift=false`.

After reload, API `/health` reports `ok=true`, database `ok`, worker `HEALTHY`,
online/ready/runtime-ready/command-loop-ready, `worker_stale=false`,
`worker_source_drift.drifted=false`, and a new worker lease owner. Dashboard
`http://127.0.0.1:9224/health` is also `ok=true`. CDP 9202 remains reachable with the
same browser WebSocket identity and its pre-existing pages remain present; 9212 was
also reachable and was not touched. No browser tab was manipulated.

### Recovery decision

No code correction beyond the already-present bounded async guard was required in this
recovery. Do not dispatch a successor until the coordinator has reconciled the stopped
ledger and independently verifies the mandatory High/Extra High model choice in the
owned browser session before any new Send. If dispatch is authorized, create exactly
one fresh subsequent `07-regression` Solo TeamTask with a new idempotency key; never
reuse the stopped task or its accepted Send. Release remains `NOT_ACCEPTED`.

`PI_RECOVERY_DONE`

## Subsequent-slice dispatch evidence

- Recorded UTC: 2026-10-27T13:02:35+00:00.
- After the stopped task and runtime reload, the coordinator independently inspected
  the owned 9202 root ChatGPT page and verified the displayed `Thinking effort High`
  control before dispatch. No other endpoint was selected and no browser tab was
  reloaded or manually manipulated.
- One fresh subsequent task was dispatched with a new idempotency key and reconciled:
  command `cmd-651df3f6-e2f5-4492-9bd1-12d11e83c477` -> task
  `cdpa-idem-b8d040f7d6fa013b8c129ab4`, team `g6-selfrepair-09-07-regression`.
  The command applied with initial status `INBOX`; no duplicate create occurred.
- The fresh task has one accepted Send only (`accepted_via=exact_user_message`) and is
  currently `RUNNING / wait_response` on the pinned 9202 endpoint, with hop state
  `sent`. This is a new task and new conversation, not a replay of the stopped
  06-async-wait Send. No completion or release claim is made; future supervision must
  use its ledger/receipt and must not resend this accepted turn.
- Post-dispatch `/health` remains `HEALTHY`, online/ready, `worker_stale=false`, and
  `worker_source_drift.drifted=false`; endpoint 9202 is connected and pinned.

Recovery record remains complete for the blocked/stopped 06 task. Campaign release is
still `NOT_ACCEPTED`.

## Coordinator follow-up: stale Pi recovery projection

- Recorded UTC: 2026-10-27T08:46:00+00:00
- Active task: `cdpa-idem-ba7a0091c4a42870bc74c695`
- Supervisor phase: `NEEDS_PI`; `pi_attempts=4`.

The authoritative API still reports task status `BLOCKED`, action `blocked`,
`block_code=response_terminal_missing`, version `11`. The campaign state's
`signature=RUNNING:wait_response:None` and old `progress_at` are stale projection
fields and must not be interpreted as current execution. The 01c receipt is a PASS
for the bounded synthetic h3 DOM regression and test suite, but it is not a PASS for
real browser terminal acceptance; the task remains blocked and release remains
`NOT_ACCEPTED`.

The local supervisor's latest Pi recovery result is `Pi recovery unavailable` after
its bounded coordinator invocation. This is a Pi/coordinator timeout, not evidence
of a CDPA worker or browser failure. Current local checks remain: `/health` reports
worker `HEALTHY`, online/ready/runtime-ready/command-loop-ready, not stale and with
no source drift; endpoint 9202 is connected, online and `chatgpt_ready=true`; all
campaign tasks are terminally BLOCKED with no additional active self-repair task.

No local code correction is proven necessary from this evidence. No Send, Retry,
Resume, reload, browser close, worker restart, or unrelated-tab operation was
performed. Hold the exact accepted-send provenance and stop for explicit
reconciliation rather than launching another fresh retry.

## Coordinator follow-up: repeated bounded recovery timeout

- Recorded UTC: 2026-10-27T09:02:00+00:00
- Campaign phase: `NEEDS_PI`; supervisor `pi_attempts=5`.

A fresh authoritative check confirms the same state: task
`cdpa-idem-ba7a0091c4a42870bc74c695` is `BLOCKED` with
`block_code=response_terminal_missing`, `active_action=blocked`, version `11`.
The live evidence remains the final empty assistant shell, no Retry, and the prior
single accepted Send; there is no terminal route to reconcile and no basis for replay.
The stale campaign signature remains `RUNNING:wait_response:None` and is not runtime
truth.

Verification at this recovery:

- Affected owner suite: `96 passed`.
- `git diff --check -- src tests workflows/self-repair`: passed; only existing
  LF/CRLF warnings were emitted.
- `/health`: worker `HEALTHY`, online/ready/runtime-ready/command-loop-ready,
  not stale, no source drift.
- Pinned 9202: online, connected, ChatGPT-ready, not draining or removed; 12 pages.

The supervisor's repeated `Pi recovery unavailable` result is a bounded coordinator
failure. It does not authorize a browser or worker restart. No code/test correction,
Send, Retry, Resume, reload, browser close, worker restart, or unrelated-tab operation
was performed. Release remains `NOT_ACCEPTED`.

## Coordinator follow-up: sixth recovery timeout

- Recorded UTC: 2026-10-27T09:17:00+00:00
- Campaign phase: `NEEDS_PI`; supervisor `pi_attempts=6`.

The latest authoritative API check is unchanged: task
`cdpa-idem-ba7a0091c4a42870bc74c695` is `BLOCKED`,
`block_code=response_terminal_missing`, `active_action=blocked`, version `11`.
The accepted-send ledger remains a single accepted fresh Send. The final owned-page
observation was an empty assistant shell with Stop gone, no Retry, and no terminal
route evidence; the stale campaign signature `RUNNING:wait_response:None` is not
execution truth. The prior h3/h4 semantic-heading fix remains verified only by
synthetic DOM coverage and does not justify another browser attempt.

Fresh local evidence:

- `/health`: worker `HEALTHY`, online/ready/runtime-ready/command-loop-ready,
  not stale, no source drift.
- The pinned endpoint remains the exact 9202 endpoint and the prior task remains the
  only active campaign identity; no successor or competing self-repair task exists.
- `uv run pytest -q tests/test_role_runtime.py tests/test_role_runtime_browser.py
  tests/test_cdpa_repair.py tests/test_spec003_gate_c_runtime.py`: `96 passed`.
- `git diff --check -- src tests workflows/self-repair`: passed, with only existing
  LF/CRLF warnings.

Pi recovery has timed out six times. This is coordinator unavailability, not proof
that the accepted Send was absent or safe to replay. No code/test correction, Send,
Retry, Resume, reload, browser close, worker restart, or unrelated-tab operation was
performed. Release remains `NOT_ACCEPTED`.

## Coordinator recovery: exact current blocked state

- Recorded UTC: 2026-10-27T10:36:25Z.
- Authoritative `/api/tasks/cdpa-idem-6edb1fca84409f0f6f617b40` reports `status=BLOCKED`, `active_action=blocked`, `block_code=response_terminal_missing`, version `11`, endpoint `4bc12276-da9c-4a7a-8ef9-ddcfcca6113f`, `terminal_reconciliation=null`, `retry_visible=false`, and `stop_visible=false`. This supersedes the stale projection's `NEEDS_PI` label but does not make the accepted turn safe to replay.
- `.runtime/live-events.jsonl` proves exactly one accepted Send for this task at 2026-10-27T09:39:50Z (`accepted_via=user_message_identity`, `outcome=accepted`) on the owned 9202 page. Subsequent DOM probes saw assistant text lengths 217, 254, then an empty assistant shell (0; Stop disappeared); the final controller decision at 09:49:56Z was `response_terminal_missing`, with diagnostics including `other_conversation`, `stale_document`, and request failures. No terminal route or reconciliation evidence exists.
- The same evidence shows no second Send, no Retry click, and no terminal-route commit. The 01b/01c historical challenges and response-missing tasks remain archived blocked history; they do not authorize another fresh retry.
- `/health` at 10:36:25Z: database/API healthy; worker online, ready, runtime-ready, command-loop-ready, `worker_status=HEALTHY`, `worker_stale=false`, no source drift. Endpoint 9202 is connected/online/ChatGPT-ready, not draining or removed. Four campaign tasks are BLOCKED and none is RUNNING.
- Verification without external mutation: `uv run pytest -q tests/test_spec003_gate_c_runtime.py tests/test_gate_c_direct_chat_worker.py` -> `23 passed`; `uv run python -m compileall -q src/playwright_auto` -> PASS; `git diff --check -- src tests workflows/self-repair` -> PASS (existing CRLF warnings only). The initial bare `python` compile probe hit the Windows Store alias; the `uv run` compile command passed.
- No local product/test correction is proven necessary from this evidence. Existing dirty changes were preserved. No Send, Retry, Resume, reload, browser close, worker restart, or unrelated-tab operation was performed. Release remains `NOT_ACCEPTED`.

`PI_RECOVERY_HOLD`

## Coordinator recovery: paused 08-real-browser task, bounded CDPA reload

- Recorded UTC: `2026-10-27T14:33:50Z`.
- Host/repository/branch: `DESKTOP-B5E4IUE` / `E:\git-project\cdpa` /
  `rebuild/runtime-v2-r11`.
- Task: `cdpa-idem-5b197a6721fa68fae479c629`, team
  `g6-selfrepair-10-08-real-browser`, slice `08-real-browser`.
- Release: `NOT_ACCEPTED`.

### Exact diagnosis and ledger reconciliation

The campaign projection was stale and misleading: `.runtime/self-repair/state.json`
reported `PAUSED` with the old signature `RUNNING:wait_response:None`, while the
authoritative API task had already reached `PAUSED`, `active_action=paused`,
`active_hop_id=1`, and a valid terminal ChatGPT response had been observed by
CDPA. The task's durable `requests.json` proves exactly one hop, `attempts=1`, one
accepted Send at `2026-10-27T14:25:24Z`, and one accepted user identity
`d7815175-6d9a-4a4e-bd36-fd91d4d46550`; there was no second Send, Retry, or Resume.
The response JSON is the worker's `PAUSE` handoff, not release acceptance, so the
real-browser gate remains unproven.

The final pre-recovery API detail showed `browser_control.controller_reason`
`valid_bound_result`, `stop_visible=false`, `retry_visible=false`, and
`active_hop.state=routed`, but the task remained operator-paused. I issued exactly
one idempotent official Stop using expected task version `52`, idempotency key
`g6-pi-recovery-stop-08-20261027-v52`, and received queued command
`cmd-46dcdbe0-65f1-45ba-8e0b-f4e1e44977e9`. After worker application, the durable
record is `STOPPED`, version `56`, with applied internal command
`cmd-52163adaafc5ef59179a2668`, `result.stopped_response=false`, and the original
accepted-Send provenance preserved. The old task must not be resumed, retried, or
resent.

### CDPA reload outcome and browser preservation

The existing launcher `workflows/self-repair/reload_cdpa.ps1` stopped only the G6
CDPA API/dashboard/worker process tree and relaunched it. It returned
`RELOAD_OK worker_ready=true source_drift=false`. The browser was not restarted.
Post-reload evidence:

- API `9225/health`: `ok=true`, database `ok`, worker `HEALTHY`, online/ready/
  runtime-ready/command-loop-ready, `worker_stale=false`, `SOURCE_DRIFT=false`,
  `worker_reload_required=false`; new worker lease owner PID `19536`, source
  identity captured `2026-10-27T14:33:45Z`.
- Dashboard `9224/health`: `ok=true`.
- Endpoint `4bc12276-da9c-4a7a-8ef9-ddcfcca6113f` at `http://127.0.0.1:9202`:
  connected, online, authenticated and ChatGPT-ready; the same browser WebSocket
  identity `devtools:db458823-c481-4135-8e28-91585dc3127b` remained present.
- Endpoint `027faaaa-7344-4b0c-a9d4-58583fec8fc4` at `http://127.0.0.1:9212`:
  not manipulated; it remained connected and ChatGPT-ready.
- Saved 9202 tab inventory after reload still contains the owned task URL and the
  pre-existing unrelated tabs. No tab was reloaded, closed, focused, or sent to.

The code/test worktree was preserved. `git diff --check --
src tests specs/003-team-only-dom-runtime workflows/self-repair` passed. No minimal
root-cause product/test fix was justified by this recovery; the evidence shows a
reconciled accepted Send and a valid but non-release PAUSE result, not a new source
failure.

### Recovery decision

Do not dispatch another real-browser Send from this recovery. The campaign has
consumed its bounded real-browser attempt and the release remains `NOT_ACCEPTED`.
Any future work requires explicit reconciliation of the stopped task and a newly
authorized task identity; never replay the accepted request.

`PI_RECOVERY_DONE`

## Coordinator recovery: stale 07-regression task, CDPA reload, and bounded fresh follow-up

- Recorded UTC: `2026-10-27T13:18:30Z`.
- Host/repository/branch: `DESKTOP-B5E4IUE` / `E:\git-project\cdpa` /
  `rebuild/runtime-v2-r11`.

### Exact diagnosis and ledger reconciliation

The campaign projection was stale: it still named `06-async-wait` as active and
reported `NEEDS_PI`, while the authoritative API had a newer fresh task
`cdpa-idem-b8d040f7d6fa013b8c129ab4` / team
`g6-selfrepair-09-07-regression` in `RUNNING / wait_response`. Its task prompt,
endpoint, and page binding were distinct from the stopped 06 task. The 07 task's
command was `cmd-651df3f6-e2f5-4492-9bd1-12d11e83c477`; its durable request ledger
contains exactly one hop with `attempts=1`, one accepted Send, and no safe terminal
receipt. Live DOM evidence showed `stop_visible=true`, no Retry, changing assistant
text, stale-document/request-failure diagnostics, and the worker remained in
`malformed_result_stability` / `wait_response`; this was a stalled accepted turn,
not proof of completion and not permission to resend.

Pi first issued the exact idempotent Stop while the task was still running:
`cmd-8121cd7d-7d49-495c-add1-e07ab66e803a`. That command was queued but the task
remained running until the required CDPA reload. The reload then stopped only the
G6 CDPA launcher/API/dashboard/worker tree; it did not stop or reload ChatGPT 9202
or 9212. After reload, the task was reconciled and the same Stop was reissued with
fresh expected version, command `cmd-ca8db71d-0a96-444f-86b1-3427fe97fa33`, and
applied as `cmd-f46a47cfe9f27185af329c43`, leaving the task terminal `STOPPED`,
version `100`, with its accepted Send provenance preserved. No Retry, Resume, or
old Send replay was performed.

### Reload outcome and browser evidence

Before reload, `/health` was API/worker healthy but the accepted 07 turn was stale;
9202 was connected and pinned to endpoint
`4bc12276-da9c-4a7a-8ef9-ddcfcca6113f` (`http://127.0.0.1:9202`). The bounded
`workflows/self-repair/reload_cdpa.ps1` launcher reload returned
`RELOAD_OK worker_ready=true source_drift=false`. After reload:

- dashboard `9224/health`: `ok=true`;
- API `9225/health`: database `ok`, worker `HEALTHY`, online/ready/runtime-ready/
  command-loop-ready, `worker_stale=false`, `SOURCE_DRIFT=false`, and a new worker
  lease owner;
- endpoint 9202: connected, online, `chatgpt_ready=true`, same endpoint identity;
- endpoint 9212: not manipulated; its normal page remained reachable;
- 9202 root page: exactly one `https://chatgpt.com/` root tab with a visible composer;
  no Cloudflare/login challenge was observed. No unrelated tab was changed.

The independent model gate was also rechecked on the owned 9202 root page: the
visible control reported `Thinking effort / High` (the model menu reported `High`,
3 of 5). This is evidence for a future fresh task only; it does not repair or
reclassify either accepted task.

### Fresh subsequent slice and second bounded stop

After proving the stopped 07 task was terminal and the runtime was healthy, one fresh
subsequent Solo was dispatched with new team/idempotency identity:
`g6-selfrepair-10-07-regression-recovery` /
`cdpa-idem-5e0a1861289d6b53b457b8d2`, command
`cmd-b3519b92-4715-4f5a-a3ca-bd59ef271523`. It was accepted once on a new ChatGPT
conversation (`6ae0a466-404c-83ee-b4e6-1eb3b85eaa43`; one `send_acceptance`, request
hop 1, attempts 1). No old request was replayed. That new turn immediately stalled
with an empty assistant response, `stop_visible=true`, no Retry, and no terminal
route. Its authoritative Stop was issued once with idempotency key tied to task
version 5, command `cmd-850cc1c4-5699-4890-b2a9-4886128ca72b`, and applied, leaving
it terminal `STOPPED`, version `9`, with accepted-send provenance preserved.

This second live result demonstrates that the remaining blocker is real-browser
terminal production/acceptance instability, not an unverified code-only regression.
No minimal product/test fix is justified by the evidence. Existing dirty changes
were preserved; release remains `NOT_ACCEPTED`.

### Verification

- Focused offline receipt already present for 06: `46 passed` across the recorded
  Gate C/browser-management suites.
- Reload health checks passed as recorded above; post-reload worker source drift is
  false.
- `git diff --check -- src tests workflows/self-repair`: passed with only existing
  LF/CRLF warnings.
- 9202/9212 browser readiness probes passed without browser reload or unrelated-tab
  manipulation.

`PI_RECOVERY_DONE`
