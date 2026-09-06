# CDPA Listen + DOM, Verified Resume and Operator Controls Acceptance Implementation Plan

> **For agentic workers:** execute through the existing CDPA workflow only. Use TDD for reproduced defects, preserve durable send provenance, and do not commit/push/reset/stash/switch/merge unless the operator separately authorizes it.

**Goal:** Replace automation-originated full-conversation graph/history reads with passive browser observation plus the existing DOM reader, retain `stream_status` as the only active ChatGPT status request, make Resume and every workflow control truthful and verifiable, and finish three fresh freelance acceptance workflows on the final loaded build.

**Architecture:** Keep the single production path `cdpa/dashboard -> TaskStore -> CDPAWorker -> CDPATabActions/ChatGPTPage`. Extend the current page-owned network capture with one bounded passive observation path for naturally occurring ChatGPT conversation traffic, preserve DOM provenance/stability as the response authority, and persist only the irreversible approval boundary in the existing request ledger. Centralize durable control eligibility beside the existing worker-command snapshot/admission logic so the API, worker and dashboard project one policy rather than three variants.

**Tech Stack:** Python 3.12, asyncio, Playwright async/CDP, existing JSON durable request ledger and runtime SQLite projections, vanilla dashboard JS, pytest.

## Global constraints

- Authoritative checkout/runtime: `/home/ayumi/Workspace/git_project/playwright-auto` on G8. ThinkBook is read-only compatibility/background evidence unless a later TEST step explicitly checks it.
- Persistent Chromium is `127.0.0.1:9222`; dashboard/API are `9224/9225`. Never call `browser.close()`.
- Preserve the existing scheduler/store/runtime. No second queue, scheduler, event bus, database, private-API client, browser-wide recorder or generic transport framework.
- No automation-originated GET/POST of full conversation graph/history on normal wait, terminal reconciliation, Resume, deadline recovery, bootstrap branching, controls or dashboard inspection. A browser-native graph response may be observed and reduced when ChatGPT naturally fetches it.
- `stream_status` remains the only active ChatGPT status request. It is shared per exact conversation, never more frequent than once per 30 seconds, and obeys existing profile cooldown/Retry-After/backoff.
- `dom_only=true` remains a real rollback/compatibility mode. `dom_only=false` means Listen + DOM + `stream_status`, never the old graph-fetch backend mode.
- Preserve `response.poll_ms=5000`, `stable_ms=1000`, `stream_status_terminal_settle_seconds=5.0`, `refresh_after_seconds=1200`, `timeout_seconds=7200` unless executable evidence demonstrates a required bounded change. The two-sample stability gate remains mandatory.
- `COMPLETE` is only a reconciliation trigger. Pending/unknown approval outranks routing. Preserve typed stop/failure stream states; unknown status is not silently treated as RUNNING.
- Approval is authorized only for the exact owned request/conversation/account/connector scope and the server-offered `allow` action with exact `target_message_id` and `remember_answer=true`. Do not locator-click, fabricate a backend request, patch permissions, force visibility, or approve unrelated risky actions.
- Old PAUSED/WAITING/BLOCKED freelance teams are read-only provenance. Never Resume/Retry/New chat/Clear/Stop/reassociate/edit them.
- Current unrelated dirty files are owned by completed task `cdpa-preboundary-newchat-recovery` (`cdpa-idem-9de92ce2ae014a4d81383805`) and must be preserved as baseline content while this task edits overlapping functions deliberately. Baseline SHA-256 recorded in this task evidence.
- `llm-wiki-migration-audit` remains independent and untouched.

## Evidence already established by PLAN turn 1

- Git HEAD: `30b99340174d04354a1b94f27f049a02a467a6ba`, branch `develop`.
- Dirty fingerprints at 2026-09-06 19:04 JST:
  - `LEARNING.md` `36abc11679695671b5eabd7d14a16191349e4f4bbdc9c2ae44ff22df235a8661`
  - `PROBLEM.md` `deea67f2bcd2f952afe1a23e4a6f4df85dacff90b324a4375136a00fbbdc9247`
  - `src/playwright_auto/cdpa_worker.py` `11852e56586b0e57f9c25c264a88cabc6d8bf32cb1743b68095c4881f92ab3a1`
  - `tests/test_operator_resume_recovery.py` `b9b937152c33b194fe0b099127256419c796132920ec7bd8476e35c5aaae08b2`
- Those four dirty files are the accepted, DONE output of `cdpa-preboundary-newchat-recovery`; its final PLAN report says the worker/test changes implement provenance-aware pre-Send New Chat recovery and must remain intact.
- Live `/api/state`: `settings.dom_only=true`; worker PID `1183394`, browser connected, not degraded; worker process started 2026-09-06 10:14:15 JST after the accepted preboundary repair.
- Existing `ChatGPTPage` has a one-shot page `response` listener for frontend send identity around `/backend-api/f/conversation`; it removes itself after the first matching response. That one-shot acceptance enrichment must stay separate from the new bounded passive lifecycle observer.
- Current `backend_stream_status()` accepts only `IS_STREAMING` and `COMPLETE`; this is insufficient for typed `FAILURE` / `IS_STOP_REQUESTED` handling.
- Current `backend_conversation()` is an active full-graph GET and is called from bootstrap donor validation/materialization, normal/terminal reconciliation and Resume recovery paths.
- Existing MCP auto-Allow path `click_mcp_permission_allow()` requires visible buttons and invokes `primary.click()`. It cannot satisfy the proven hidden-node + offered split-handler acceptance contract.
- Workflow detail currently renders all nine workflow control buttons enabled by default; JS disables only an already-pending command. Durable action eligibility/reason is therefore not projected into the UI.

## File map and responsibilities

### Primary implementation files
- `src/playwright_auto/chatgpt.py`
  - existing backend token/context state, frontend response identity capture, DOM snapshots, ownership/mutation guard, MCP permission behavior;
  - add/host only the minimal passive page-observation primitives that logically belong to `ChatGPTPage` if a small separate observer file is not clearer.
- `src/playwright_auto/chatgpt_observer.py` **only if needed after DEV confirms the one-shot identity listener cannot cleanly own persistent observation state**
  - bounded page-local natural-response reduction and attach/detach lifecycle; no active requests.
- `src/playwright_auto/cdpa_actions.py`
  - keep browser/page ownership actions; remove active full-graph surface; expose only stream-status and local/passive evidence needed by worker.
- `src/playwright_auto/cdpa_worker.py`
  - completion ordering, Resume/reconnect/deadline reconciliation, bootstrap donor behavior without graph GET, approval lifecycle, control application.
- `src/playwright_auto/durable.py`
  - extend the existing exact request ledger with one bounded approval-attempt record if required; no second store.
- `src/playwright_auto/cdpa_commands.py`
  - canonical durable workflow-control eligibility/admission result shared by worker/store/projection.
- `src/playwright_auto/cdpa_store.py`
  - queue-time validation, exact-team Resume admission and durable control results using the shared eligibility contract.
- `src/playwright_auto/cdpa_runtime_db.py`, `src/playwright_auto/dashboard_api.py`
  - project control eligibility/reasons and truthful command terminal outcomes; no graph inspection.
- `src/playwright_auto/dashboard_assets/views/task_detail.js`, `src/playwright_auto/dashboard_assets/views/runtime.js`, `src/playwright_auto/dashboard_assets/app.js`
  - consume server-projected eligibility, show disabled reasons/tooltips, label mode semantics, and report pending/recovering/applied/refused outcomes accurately.
- `docs/chatgpt-automation-contract.md`, `docs/chatgpt-workflows.md`
  - document Listen + DOM + stream_status semantics and control/Resume acceptance boundaries.

### Primary tests
- Create if needed: `tests/test_chatgpt_observer.py`.
- Modify: `tests/test_chatgpt.py`, `tests/test_chatgpt_safety.py`, `tests/test_chatgpt_recovery.py`, `tests/test_chatgpt_mcp_permission.py`.
- Modify: `tests/test_operator_resume_recovery.py`, preserving existing accepted preboundary cases.
- Modify: `tests/test_cdpa_backend_conversation_identity.py`, `tests/test_cdpa_actions.py`, `tests/test_cdpa_worker.py`, `tests/test_cdpa_core.py`, `tests/test_cdpa_runtime_stability.py`, `tests/test_cdpa_runtime_db.py`, `tests/test_dashboard_api.py`, `tests/test_cdpa_cli_dashboard.py`, `tests/test_dashboard.py`.
- Include affected durable/bootstrap/ownership tests: `tests/test_cdpa_bootstraps.py` and any exact existing request-ledger/browser-ownership suites reached by focused failures.

## Task 1 — Freeze ownership, establish baseline and install acceptance instrumentation

**Deliverable:** reproducible pre-change evidence, no source behavior changed yet.

- [ ] Re-read `AGENTS.md`, `LEARNING.md`, this plan and the old ThinkBook plan; treat this plan/task contract as authoritative.
- [ ] Record current git status, SHA-256/size/mtime for every dirty file and every file to be edited. Confirm no newer nonterminal task owns the four dirty files before overlapping them.
- [ ] Record worker/API/dashboard PID/start time, current loaded-source evidence, frontend asset identity, `/api/state`, configured timers, browser tab inventory, read-only task catalog/projection, current control projections and current task manifest/request ledger.
- [ ] Capture one same-account baseline window with the final intended tab count/model/task mix: idle plus one active managed task and then three managed tasks. Separately sample Python worker, Playwright driver node and Chrome tree RSS/CPU. Record DOM snapshot count, stream-status request count, automation graph-request count, browser-native graph-response count, observer/listener count (currently zero), heartbeat freshness and control command latency.
- [ ] Add test instrumentation that can classify ChatGPT requests as `automation_status`, `automation_full_graph`, or `browser_native_observed`; do not log bodies/credentials.
- [ ] Seed/update `.plan/cdpa-listen-controls-acceptance/evidence/acceptance-ledger.md` with exact evidence paths and keep every unexecuted row `NOT RUN`.

**Stop condition:** baseline window is comparable and all pre-change ownership/runtime evidence is durable. No implementation begins while overlapping dirty-file ownership is unknown.

## Task 2 — Passive observation and shared typed stream-status polling

**Deliverable:** page-local passive observation exists; stream_status is the only active response-status request.

- [ ] Write failing tests for attach-once, detach/close, reconnect/rebind idempotency, unrelated tab/URL rejection, wrong conversation/generation/request rejection, late attach => UNKNOWN, buffer truncation => UNKNOWN, no event, partial body/frame, natural graph historical resolved approval and listener cleanup.
- [ ] Preserve the current one-shot `/backend-api/f/conversation` acceptance identity capture. Add one separate persistent page-local observation path because the current listener is intentionally one-shot and cannot safely represent lifecycle state after the first response.
- [ ] Filter before body work. Accept only current owned ChatGPT conversation traffic. Callback work is bounded enqueue/state replacement; asynchronous reducers parse only minimum current-request/current-branch facts.
- [ ] Bound cache by account/browser scope + conversation + conversation generation + request. Store only freshness/coverage, candidate response identity/content subset required by existing validators, pending approval/action metadata, and relevant response IDs. Never retain an unbounded mapping/transcript.
- [ ] Reuse `_BACKEND_CONTEXT_STATES` to share per-conversation stream-status cache/due/in-flight/cooldown across watcher/wait/Resume. Enforce >=30 s between automation polls for one exact conversation; reuse a fresh observed/cached status instead of sending another request.
- [ ] Preserve existing authentication refresh, profile cooldown and Retry-After/backoff. Ensure only owned active work polls; terminal/closed work releases its slot.
- [ ] Extend status interpretation so `COMPLETE`, `IS_STREAMING`, `FAILURE`, `IS_STOP_REQUESTED` remain distinct. Unknown status produces explicit UNKNOWN/fallback evidence; it is never coerced to RUNNING.
- [ ] Keep 5 s DOM loop timing and current two-sample stability behavior unchanged.

**Focused tests:** `tests/test_chatgpt.py`, `tests/test_chatgpt_observer.py` (if created), stream-status cases in worker/recovery tests.

**Stop condition:** tests prove no duplicate observer or duplicate status poll under wait+Resume/reconnect, and unknown/typed terminal statuses preserve behavior.

## Task 3 — Remembered approval with exact durable attempt boundary

**Deliverable:** hidden-node remembered approval is invoked through the loaded client handler and cannot duplicate across crash/reconnect.

- [ ] TDD fixtures: `COMPLETE + approval + stale candidate`; hidden node; missing handler; same target twice; wrong connector/account/branch/generation/request; no remember option; manual deny/allow; outbound HTTP 200 followed by stream error; crash after dispatch before acknowledgement; reconnect/duplicate tab; different target/scope.
- [ ] Replace the current visible-primary-click permission path for this workflow with one ownership-guarded `page.evaluate()` that finds the exact approval card/React ancestry even when hidden, chooses the **offered** split option whose action is `type=allow`, exact `target_message_id`, `remember_answer=true`, and calls the loaded `onSelectOption(event, option.action)` handler. No `locator.click`, no coordinates, no forced visibility, no direct private request.
- [ ] Respect task-specific connector authorization. Missing/offered-but-disabled/mismatched scope returns a concrete pending/blocked reason and never routes the old candidate.
- [ ] Extend `DurableRequestRecord` with one optional bounded `approval_attempt` object (or an equivalent existing-ledger field if DEV finds one already suitable): exact target, connector/scope hash, state `pending|dispatched_unknown|confirmed|failed`, timestamps and correlated continuation identity. Guard update transitions under the existing ledger lock.
- [ ] Before invocation persist `pending`; atomically cross to `dispatched_unknown` at the invocation boundary. Callback return/HTTP 200 alone is not confirmation. Confirm only from correlated outbound action plus new continuation/tool/result/generation progress. Crash from `dispatched_unknown` must reconcile and never blind re-approve.
- [ ] On confirmed continuation invalidate pre-approval terminal/candidate observation and continue waiting under the original task/hop/request identity.
- [ ] Compact observability only: target ID, connector label/hash, outcome, conversation/generation/request identity. Never raw args/body/tokens.

**Focused tests:** `tests/test_chatgpt_mcp_permission.py`, `tests/test_chatgpt_safety.py`, `tests/test_chatgpt_recovery.py`, durable/request-ledger tests.

**Stop condition:** duplicate invocation count is zero for same durable target after callback error, crash, reload and worker restart; old candidate is never routed before approval continuation settles.

## Task 4 — Delete automation full-graph reads from every reachable CDPA path

**Deliverable:** normal, bootstrap, Resume, recovery, deadline and controls execute without `backend_conversation()`.

- [ ] First add a fail-fast test seam that raises on **any automation-originated** `/backend-api/conversation/<id>` graph/history request while allowing browser-native natural graph responses and `/stream_status`.
- [ ] Remove the active `backend_conversation()` surface from `CDPATabActions`/`ChatGPTPage` and all worker call sites after equivalent local/passive fail-closed behavior is covered.
- [ ] Normal/terminal response reconciliation: use accepted request ledger + passive observation + current DOM final-response/provenance/stability validators. Missing passive coverage means UNKNOWN and DOM fallback; never fetch graph.
- [ ] Resume/deadline recovery: accepted or ambiguous durable sends are reconciled only from the exact ledger, current owned tab/DOM and fresh passive facts. If provenance cannot be proved, return `recovery_required` with precise next action; never resend or graph-fetch.
- [ ] Bootstrap donors already persist exact `conversation_id + assistant_message_id`: remove graph prevalidation and let existing `branch_from_anchor()` plus its UI/ownership/branch response validation be authoritative. A branch failure classifies donor availability without a graph GET.
- [ ] For a legacy/source-only bootstrap requiring latest terminal assistant discovery, navigate the source conversation only through normal browser UI and consume its natural rehydration/DOM through the passive observer; if exact anchor provenance is unavailable, fail closed as bootstrap unavailable rather than issuing an active graph GET.
- [ ] Remove graph-dependent tests/mocks and replace them with passive/natural-response or DOM evidence fixtures. Keep graph parsers only if they reduce already-observed browser-native payloads; parsers must have no request capability.
- [ ] Assert zero automation full-graph requests in normal wait, terminal, Resume, worker restart, CDP reconnect, F5/deadline, bootstrap branch and every control fixture.

**Stop condition:** repository search finds no reachable active graph request call, and instrumentation records `automation_full_graph=0` for all focused/live acceptance runs.

## Task 5 — Canonical Resume semantics and nine workflow-control eligibility

**Deliverable:** one durable admission policy, truthful command lifecycle, no identity drift or no-op success.

- [ ] Add a pure/shared workflow-control eligibility result beside `WorkerCommand`/`validate_worker_command` in `cdpa_commands.py`. It returns `eligible`, stable reason code/text, and role requirement from the durable snapshot. Worker locked admission calls the same helper; store/runtime projection reuses it. Browser-only facts may refine/refuse at execution time but the UI must never invent a second eligibility policy.
- [ ] Project per-action eligibility/reason through runtime DB/task detail. Render disabled controls with reason/title; pending commands remain disabled. Keep optimistic task-version checks.
- [ ] Unify task-card Resume, empty-create/exact-team Resume, CLI `cdpa --team`, worker restart, CDP reconnect, tab reopen, route/operator PAUSE release, self-route guard and dependency WAITING on the same durable recovery predicates.
- [ ] Resume preserves team/task/manifest/hop/request/turn/bootstrap/receipt/reports/repair count unless a documented semantic action changes exactly one field. Manual composer/attachments are read-only reconciliation evidence and never cleared/sent automatically.
- [ ] Resume command remains `requested/recovering` until one verified postcondition: response consumption, exact draft acceptance once, genuine generation progress, ownership reacquisition before Send, or hop advancement. Otherwise terminal command outcome is `recovery_required|failed|ineffective` with reason and next safe action. A 200/202/queued response is not success.
- [ ] Dependency WAITING remains blocked until parent readiness; terminal-only/corrupt/duplicate catalog/mismatched account/duplicate owner/ambiguous send fail closed.

### Exact control rules to prove

| Control | Positive contract | Negative/refusal contract |
|---|---|---|
| Pause | INBOX/RUNNING parks dispatch after durable boundary; accepted work remains recoverable | waiting/terminal/ineligible state visibly refused; no tab/send mutation |
| Resume | same durable identity continues/reconciles | no new task/suffix, no accepted-send replay, precise blocked/recovery reason |
| Retry hop | only structured retryable BLOCKED; intended attempt/hop mutation once | nonretryable/PAUSED/terminal refused; never generic Resume |
| Restart role | exact chosen role at safe boundary; generation/replacement once | in-flight/ambiguous send refused; other roles/reports untouched |
| New chat | exact chosen owned role, safe boundary, generation once | manual draft/attachments/accepted in-flight refuse; no hidden resend |
| Open tab | reopen exact saved role conversation or safely report account/history prerequisite | never duplicate owner or send original task |
| Route PLAN | one PLAN handoff from eligible non-PLAN safe role | PLAN-active/self-route-guard/in-flight refused |
| Stop | terminal STOPPED, future dispatch cancelled, receipts/history retained | no automatic recovery resurrection or post-stop send |
| Clear team | confirmed exact-owner, restartable phases to connected zero-owner proof | uncertainty remains CLEARING; old/historical/unrelated/free tabs untouched |

**Focused tests:** `tests/test_operator_resume_recovery.py`, `tests/test_cdpa_actions.py`, `tests/test_cdpa_worker.py`, `tests/test_cdpa_core.py`, `tests/test_cdpa_runtime_db.py`, `tests/test_dashboard_api.py`, `tests/test_cdpa_cli_dashboard.py`, `tests/test_dashboard.py`.

**Stop condition:** every control has a positive and negative durable test, duplicate/stale command test, restart boundary test, and dashboard projection test using the shared reason.

## Task 6 — Browser/UI control acceptance and mode-switch acceptance

**Deliverable:** real dashboard click -> API command -> locked admission -> worker effect -> durable result -> projected UI feedback is proven.

- [ ] Use disposable fresh workflow fixture teams only. Never destructive-test old-account or successful freelance UAT teams.
- [ ] For each of nine buttons capture precondition, actual dashboard click, command ID, API payload, worker result, durable postcondition and UI status/reason.
- [ ] Include eligible and ineligible states, duplicate click, stale version, worker restart while command pending, CDP loss, manual action race, dependency WAITING, missing tab and duplicate owner.
- [ ] Exercise `dom_only=true -> false -> true` while active work exists. No task/hop/request reset, Send replay, observer duplication or stale-mode event routing into a newer generation.
- [ ] Update runtime label/help to say `DOM only` versus `Listen + DOM + stream_status`; never label false mode as full backend/graph mode.
- [ ] Verify status polling remains <=1/30s/conversation across hot switch and Resume.

**Stop condition:** all nine rows plus Resume entrypoints are PASS through the actual UI and negative tests show zero task/tab/send mutation.

## Task 7 — Focused regression, full suite, deploy and performance comparison

**Deliverable:** final candidate is regression-clean and the loaded runtime is provably the tested build.

- [ ] Run focused suites with `.venv/bin/python -m pytest` covering every test file named in the task contract plus newly added observer/durable/bootstrap tests.
- [ ] Run `git diff --check`.
- [ ] Run full `.venv/bin/python -m pytest -q` in a log-backed supervised process. A timeout/interruption is NOT PASS; capture exit code and totals.
- [ ] Before runtime restart/cutover, byte-copy every explicitly changed deployed source/asset/config file to the task evidence rollback directory and record in-flight request/control ledgers. Do not snapshot or rewrite unrelated data.
- [ ] Restart through supported lifecycle only. Verify worker heartbeat/browser connection, source/process start after final file mtimes, served frontend cache identity and runtime settings. Preserve all old tasks and in-flight provenance.
- [ ] Repeat the identical baseline workload/window: one then three managed tasks, idle and streaming, attach/reload cycles. Report p50/p95 where applicable for CPU/control/approval/routing latency, memory, DOM snapshots, listener count, stream-status polls and graph request counts.
- [ ] Acceptance is no reproducible regression beyond baseline variability, no listener/memory leak, zero duplicate approval/route/send, zero automation full-graph requests and zero introduced runtime errors. Ambiguous result requires one same-condition repeat.

**Stop condition:** tested bytes == loaded bytes/served assets, full suite exits 0, performance evidence is non-regressive or explained/resolved, rollback material is complete.

## Task 8 — Three fresh freelance workflows on final build

**Deliverable:** exactly three new `cdpa-listen-uat-*` teams finish normal PLAN-route DONE with useful artifacts.

- [ ] Read the canonical old inputs under `.plan` for repository `/home/ayumi/Workspace/git_project/ai-job-search`:
  1. `continuous-job-freelancer-digest-a-20260818` / `cdpa-idem-33f9098ce0a96ec5234c7a91`
  2. `continuous-job-freelancer-digest-b-20260818` / `cdpa-idem-917fccde2fac28c099c7a913`
  3. `continuous-job-micro-sourcing-r52` / `cdpa-idem-e2919e1a08f9e6999e87f211`
- [ ] Hash and reuse only task requirements/brief/source evidence. Never reuse task/hop/request IDs, receipts, conversations, approvals, control history or account authorization.
- [ ] Create exactly three fresh teams `cdpa-listen-uat-*` with fresh task IDs/conversations on the current account, max three concurrent. Use `g8-bootstrap` only with a valid current-account donor; otherwise create/use an explicitly safe current-account bootstrap/Fresh setup without editing old donors.
- [ ] Job A: evidence-backed freelance shortlist + suitability assessment and complete uninterrupted PLAN -> worker -> independent REVIEW/TEST -> PLAN cycle.
- [ ] Job B: deduped shortlist + truthful proposal drafts; perform Pause/Resume and exact-team CLI Resume, proving identity stability and no accepted-send replay.
- [ ] Job C: bounded current micro-sourcing check; perform safe tab-loss/reopen plus worker/CDP reconnect/Resume, proving no replay and truthful unavailable/expired leads when applicable.
- [ ] No application, bid, message, purchase, KYC, interview or external candidate assessment submission. Approval authorization is not inherited from history.
- [ ] Store new reports/artifacts only in each new team/evidence namespace. Record source brief/hash, task ID, account-scope hash (no credential), conversation/generation references, routes, control receipts, request/poll/graph counters and implementation hashes.
- [ ] Any reproducible anomaly returns to the smallest DEV fix, then TEST/REVIEW and **three new clean final-build runs**; pre-fix job runs do not count.

**Stop condition:** exactly three clean final-build workflows reach DONE via normal PLAN validation, with useful deliverables and zero unexplained anomaly.

## Task 9 — Independent REVIEW and PLAN closure

**REVIEW must:**
- [ ] Inspect final diff against the contract and old-plan corrections.
- [ ] Confirm no parallel architecture, no active graph fallback, no weakened provenance/two-sample gates, no click-based hidden approval shortcut, and no UI-specific eligibility policy.
- [ ] Verify dirty preboundary changes remain semantically intact and unrelated tasks/files are not commandeered.
- [ ] Inspect decisive TEST/live/performance evidence and three final freelance DONE runs.
- [ ] Run a minimal-complexity pass: every new helper/state field must correspond to a demonstrated invariant or test.
- [ ] Route clean work to PLAN; any reproducible defect routes to the smallest authorized DEV/TEST follow-up.

**Final PLAN closure requires all of the following:**
1. Replacement plan + updated prompt saved.
2. `automation_full_graph=0` on all executable acceptance paths.
3. DOM-only and Listen+DOM modes both functional and truthfully labeled.
4. Hidden remembered approval proven without visible UI click and without premature routing/duplicate approval.
5. Nine-button matrix + every Resume entrypoint PASS through UI/API/worker/durable evidence.
6. Focused and full repository tests green; `git diff --check` green.
7. Three fresh freelance teams DONE on the final loaded build with useful artifacts.
8. Old-account task identities/history unchanged.
9. No reproducible CPU/latency/memory/listener regression and no observed unaddressed anomaly.
10. Independent REVIEW clean, rollback/source/cache identity recorded, remaining blockers empty.

Before `DONE`, PLAN performs exactly one bounded learning pass using the newest implementation/TEST/REVIEW evidence and root `LEARNING.md`, choosing exactly `ADDED`, `REVISED`, or `NONE` and using only the guarded `playwright_auto.cdpa_learning` writer for a mutation.

## Role routing

- Required now: `DEV` for Tasks 1-5 and candidate implementation evidence.
- Required independently after candidate code: `TEST` for Tasks 6-8 operational/browser/full-suite/performance evidence.
- Required after TEST: `REVIEW` for correctness/minimal-complexity review.
- `AUDIT` is not preselected; add it only if TEST/REVIEW finds a concrete cross-cutting ambiguity that neither can prove.
- Any executable FAIL/INCOMPLETE returns to the smallest necessary DEV/TEST follow-up. No `PLAN -> PLAN` waiting loop. `PAUSE` only for a precise external prerequisite no authorized role can change.

## Completion matrix

| Area | Required evidence | Final state |
|---|---|---|
| Passive Listen | attach/detach/reconnect/late/partial/unknown fixtures + live counts | PASS |
| Active backend | stream_status <=1/30s/exact conversation; full graph active count zero | PASS |
| Approval | hidden offered remember action, durable boundary, continuation confirmation, no duplicate | PASS |
| DOM-only | unchanged two-sample/stability/provenance behavior | PASS |
| Hot mode switch | both directions, no reset/replay/observer leak/stale generation | PASS |
| Resume | task card, exact-team form/API/CLI, restart/CDP/tab/Pause/guard/dependency cases | PASS |
| 9 controls | real dashboard positive + refusal + duplicate/stale/restart postconditions | PASS |
| Full regression | focused suites + full pytest exit 0 + diff-check | PASS |
| Deployment | rollback snapshot + loaded hash/start time/cache identity + healthy heartbeat | PASS |
| Performance | comparable one/three-task windows; no reproducible regression/leak | PASS |
| Freelance UAT | exactly three fresh useful workflows DONE on final build | PASS |
| Old provenance | old-account teams unchanged | PASS |
| Review | independent correctness + minimal-complexity pass | PASS |
| Remaining blockers | empty | PASS |

No finite run may be reported as universal future 100% reliability. Completion means 100% of this explicit matrix passed with zero observed unaddressed anomaly.

## Deployment-gate clarification after TEST turn 2

Read `.plan/cdpa-listen-controls-acceptance/evidence/supervisor-deployment-gate-clarification.md` before resuming work. The unavailable historical worker SHA-256 is not a reason to pause ALL still-unimplemented work. Preserve the current production process while completing the remaining observer, controls, Resume, browser fixtures, full regression and measurement work. A source-traceable rollback build preserving the previously accepted pre-boundary fixes can satisfy rollback readiness only after independent review, baseline/regression verification and a boot test. It must never be represented as byte-identical to the unavailable old snapshot. Do not deploy without a validated recovery strategy. All user-level acceptance criteria and the three fresh final-build freelance DONE workflows remain mandatory. Isolated tests must not share production ownership/state or become a second scheduler.
