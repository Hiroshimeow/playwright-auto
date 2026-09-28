# PLAN report — CDPA live stream / BLOCKED recovery repair

Task: `cdpa-idem-785d75d981319be4b33834f9`  
Team: `cdpa-live-stream-block-repair-team-20260928`  
Workspace: `E:\git-project\playwright-auto-multicdp`  
Execution constraint: g6 only via @md; this workflow is pinned to endpoint `9202` (registry endpoint `56b44af5-6790-430f-afe3-4f78766d58be` -> `http://127.0.0.1:9202`). Do not use 9212/9222. No push.

## Repository truth and protected baseline

- PLAN started from HEAD `2e17db3ba29b17322b75dd40167a3a30513be7d7` on `feat/cdpa-multi-cdp-temporary-recovery`.
- Relevant recent commits inspected: `2e17db3`, `ab03276`, `1c86a3d`, `a96e070`, `61a4753`, `d088084`, `940eb6b`, `a2c06a3`, `0b5fcd7`, `5168cd8`.
- Read current `AGENTS.md`, `LEARNING.md`, `PROBLEM.md`, repo HCU rules/profile and relevant source/tests. The HCU fingerprint differs because the worktree already contains unrelated dirty documentation; no rule change relevant to this task was inferred from that mismatch.
- Pre-existing dirty/untracked state is protected and must remain uncommitted by this task:
  - modified: `.agents/agents/code-reviewer.md`, `.gitignore`, `AGENTS.md`, `PROBLEM.md`, `README.md`, `prompts/cdpa/BASE_CONTEXT.md`, `prompts/cdpa/RESPONSE_GUIDE.md`, `src/playwright_auto/cdpa_defaults/prompts/cdpa/BASE_CONTEXT.md`
  - untracked: `.github/`, `.skills/solo-role/`, `.skills/using-codex/`, `.skills/using-pi-coding/`, `NUL`, `state_api_dump.json`, `task_api_dump.json`
- No files were staged at PLAN intake. Never reset/stash/clean/switch branch. Use selective staging and inspect `git diff --cached --name-status` before every commit.

## Current upstream research

Verified current upstream `https://github.com/miuuyy/codex-chatgpt-web` main/HEAD on 2026-09-28:
- commit: `2d73f626290a5062825bb595dabb89aaf88d16c5`
- license: MIT, Copyright (c) 2026 codex-chatgpt-web contributors.
- sources inspected at that exact commit:
  - `docs/architecture.md`
  - `docs/security-model.md`
  - `src/adapters/chatgpt-web/index.ts`
  - issue `#221` (closed)
  - PR `#222` (closed, not merged; do not treat it as authoritative implementation).

Useful current upstream concepts, to borrow rather than port:
1. Browser submission/response binding is based on logical `data-turn-id` / persistent turn containers rather than display indexes.
2. Visible ChatGPT status rows project to reasoning summaries; stable prose between status rows projects to commentary; final answer remains a distinct phase.
3. Current adapter emits reasoning as `thinking_delta`, commentary as `text_delta phase=commentary`, and final answer as `text_delta phase=final_answer`.
4. Current security model explicitly records an answer projection before a tool batch, blocks completion while the tool is unresolved, then requires a new stable final-answer projection after that causal boundary; a two-phase completion fence re-reads DOM and commits only if activity revision is unchanged and no invocation is active.
5. The regression pattern documented around #221 includes answer content between multiple status/tool containers. Therefore CDPA must preserve DOM order such as `status -> answer part 1 -> status -> answer part 2`; a later status row must not cause preceding answer text to be discarded or reclassified.
6. During active CoT/tool phases there may be visible status text without a final markdown answer. That is liveness/activity evidence, not completion evidence.

These patterns align with CDPA's existing exact-send/no-resend contract and do not justify copying upstream's broker/orchestration architecture.

## Current CDPA ownership and root causes

### Response extraction / live transcript

Current owners:
- `src/playwright_auto/chatgpt.py`
  - `_WAIT_PROBE_INSTALL_SCRIPT`
  - `inspect_chatgpt_wait_probe`
  - `inspect_chatgpt_page`
- `src/playwright_auto/chatgpt_models.py`: `WaitProbe`, `ChatGPTSnapshot`
- `src/playwright_auto/role_runtime/page_observer.py`: MutationObserver wake source
- `src/playwright_auto/role_runtime/signals.py`: `transcript_messages`, `record_probe`, `record_full_snapshot`, `record_cycle`
- `src/playwright_auto/live_stream.py` and `dashboard_assets/live_stream.js`: existing TRANSCRIPT upsert path/reducer.

Verified current gap: both sparse wait probe and full snapshot still derive most assistant content from:
- `[data-message-author-role][data-message-id]`,
- exact semantic `main h4` labels (`You said:` / `ChatGPT said:`),
- `[data-streaming-response-status]`.
Current Retry detection is only `[data-testid="regenerate-thread-error-button"]`.

The MutationObserver is already the correct wake mechanism. It must stay wake-only; do not add a second observer/response engine.

Live durable evidence from `.runtime/live-events.jsonl` for clone-repackage task `cdpa-idem-896a33fc42e7f712be4ca99e` reproduces defect A:
- user TRANSCRIPT at `2026-09-28T01:24:45.110315Z`;
- DOM at `01:24:45.109315Z`: `assistant_text_length=0`, `response_activity_length=0`, no assistant message/turn, `stop_visible=true`;
- no assistant TRANSCRIPT during the long visible middle work;
- final assistant first appears around `01:43:09.854254Z`, with lengths jumping to 935.
This is a current runtime reproduction, not a hypothetical fixture.

### Resume / terminal result

Current owners:
- `src/playwright_auto/role_runtime/response.py::observe/handle`
- `src/playwright_auto/role_runtime/controller.py::RoleController.decide/run`
- `src/playwright_auto/cdpa_worker.py::_recover_resume_waiting`, `_record_response`, `_responded`, `_complete_request_response`.

Focused current-source reproduction:
`tests/test_operator_resume_recovery.py::test_resume_routes_valid_local_report_response_without_replay`
is RED on HEAD:
- expected `postcondition=response_consumed`
- actual `observation_rearmed`.
The same focused run produced `1 failed, 26 passed`.

Root cause: `response.handle` requires a completion UI action (copy/feedback/regenerate) in addition to stable valid content. The Resume fixture has an exact accepted bound assistant response and valid expected report, but no action controls, so it is not consumed. Also, `_recover_resume_waiting` currently queues the `response_without_terminal_result` repair before giving a newly rendered exact terminal response a narrow chance to be consumed.

The fix must be Resume-specific or equivalently narrow. Do not globally remove completion-action guards from normal live waiting.

### P-043 / P-044

Current source is ahead of `PROBLEM.md`:
- P-043 behavior exists in `role_runtime/controller.py`: stable invalid candidate becomes `Action.REPAIR / malformed_result` after normal stability once current-page activity no longer indicates generation.
- P-044 behavior exists in `cdpa_prompts.py::PromptBuilder.build_solo` plus `cdpa_worker.py::_pre_send`: Solo route repair carries the exact validation error and malformed candidate, excludes the substantive goal, asks for JSON-only correction, and uses bounded repair attempts.

Focused tests passed:
- `test_stable_malformed_result_repairs_with_exact_validation_error_without_reload`
- `test_stale_listen_stream_status_cannot_hide_stable_dom_malformed_result`
- `test_solo_route_repair_prompt_carries_exact_error_and_candidate_without_goal`
- `test_solo_route_repair_actual_send_persists_exact_repair_prompt_once`
- `test_solo_repeated_invalid_repairs_are_bounded`.

Therefore do not re-implement P-043/P-044 unless another phase regresses them. `PROBLEM.md` is also pre-existing dirty; update/mark resolved only after final evidence and only with selective staging of task-owned hunks.

### EndpointPool physical restart

Current owner: `src/playwright_auto/cdpa_endpoint_pool.py::EndpointPool.refresh/_connect/_stop_attachment`.

Current `refresh` invalidates an attachment only when endpoint address changes or Playwright `browser.is_connected()` becomes false. `/json/version` physical identity is checked only on `_connect`, so an online runtime can retain a stale BrowserContext if Chromium is physically restarted behind the same 9202 address while the old Browser proxy still reports connected.

PLAN reproduced this deterministically with current test fakes:
- initial physical ID `devtools:11111111-1111-1111-1111-111111111111`;
- same endpoint later reports `devtools:22222222-2222-2222-2222-222222222222`;
- after `EndpointPool.refresh`: version probe count for that endpoint remained 1, context was reused, runtime physical ID remained the old ID, health remained `online`.
So F is a current-source FIX, not speculative.

Worker-level `TargetClosedError` handling already calls `mark_unavailable`; preserve it. The missing piece is periodic physical-identity validation before a stale context has to throw.

## Requirement classification

| Item | Decision | Evidence / boundary |
|---|---|---|
| A current-response timeline | **FIX** | live reproduction + brittle current extractor |
| B text-only scoped Retry | **FIX** | current selector only recognizes regenerate-thread-error-button |
| C Resume terminal consumption | **FIX** | focused regression currently RED |
| D P-043 malformed_result | **COMPLETE/PRESERVE** | current branch + focused tests green |
| E P-044 Solo format repair | **COMPLETE/PRESERVE** | current branch + focused tests green |
| F stale BrowserContext after physical restart | **FIX** | deterministic current-source reproduction |
| G preboundary acquisition | **PRESERVE / NO CHANGE** | `tests/test_cdpa_presend_cleanready_rootfix.py` green in focused run; no current gap reproduced |
| H ownership safety blocks | **PRESERVE / NO CHANGE** | no current false positive reproduced; exact identity is authoritative |

## Implementation contract for DEV

### Phase 2 — one small response projector, RED -> GREEN

Create one cohesive primitive/module rather than growing another large branch in `chatgpt.py`, recommended path:
`src/playwright_auto/chatgpt_response_projection.py`.

It should own the bounded current-turn projection/extractor shared by sparse wait probe and full snapshot. Add only the minimal model surface required in `chatgpt_models.py` (for example `ResponseSegment` plus `response_segments` / explicit active flag on `WaitProbe` and `ChatGPTSnapshot`).

Mandatory invariants:
1. Scope extraction to the current logical response turn using existing `data-turn-id`, `data-turn-key`, `data-turn-id-container`, exact latest user identity and assistant/search-unit evidence.
2. Preserve DOM order across reasoning/commentary/status/tool/answer segments.
3. Do not put synthetic thinking/tool/status segments into `ChatGPTSnapshot.messages`; terminal response binding must continue to operate only on real assistant response candidates. The timeline is observability/liveness data, not route authority.
4. Stable synthetic segment identity must not use changing text hashes. Prefer exact user/generation identity + small semantic phase/kind + stable structural ordinal. Native message IDs remain authoritative when available.
5. Bound work: inspect only the current response/turn subtree; cap segment count and total text (recommended <=24 segments and <=24k combined chars). Never serialize `document.body` or generic mutation noise.
6. Historical completed tool/status rows do not veto forever. Only current unresolved/streaming activity is active.
7. Progress/tool/reasoning alone can never produce `Action.ACCEPT`.
8. If a final-looking answer exists while a current unresolved tool/status phase remains active, that activity vetoes terminal completion.
9. Preserve `status -> answer1 -> status -> answer2`; classify commentary by local/ordered structural position, not “any later status exists”.

Focused browser fixture tests belong in `tests/test_role_runtime_browser.py` (or one narrowly named new projector test file if that keeps fixtures cohesive):
- visible status/reasoning/tool content before final assistant message produces non-empty response segments/activity even with no `data-message-author-role`, semantic h4, or streaming-status marker;
- mixed ordered segments preserve both answer chunks and order;
- rerender/class churn preserves synthetic logical keys and does not create duplicate logical transcript rows;
- progress-only projection remains outside terminal `messages` and never accepts;
- unresolved tool/status activity vetoes a final-looking candidate.

Checkpoint only the projector/model/tests.

### Phase 3 — TRANSCRIPT wiring + safe Retry fallback

Modify `role_runtime/signals.py` to emit projected segments through the existing `TRANSCRIPT message` contract. Reuse `live_stream.py` and the dashboard reducer. The dashboard already preserves multiple distinct real message IDs in one turn and performs upsert/authoritative LISTEN precedence; do not redesign it unless a focused failing test proves one extra `phase`/`kind` field is needed.

Rules:
- synthetic segment rows get stable `message_id` keys so multiple segments within one turn do not collapse to `assistant:<turn>`;
- active reasoning/tool/status/partial rows are `complete=false`;
- only existing validated terminal authority may produce `complete=true`;
- LISTEN terminal response remains authoritative over DOM partials.

Add one shared response-scoped Retry resolver used by wait/full read paths:
1. first prefer exact `data-testid=regenerate-thread-error-button`;
2. fallback only to exactly one visible, enabled `button,[role=button]` whose normalized visible text is exactly `Retry` and which is structurally inside the current response/assistant turn;
3. ambiguous or out-of-response Retry text is ignored/fail-closed;
4. do not globally search/click arbitrary “Retry” text;
5. do not introduce automatic Retry/Regenerate. Current controller behavior is detection -> 5s stable -> bounded `retry_visible_stable` repair/block.

Also update narrow read-only page-state checks in `chatgpt.py` to use the same resolver where they currently only recognize the testid, if a stale error surface could otherwise be misclassified as send-ready. Do not change explicit `retry_generation` into an automatic path; production currently has no caller.

Tests:
- response-scoped text-only Retry is detected without testid/aria-label;
- unrelated composer/dialog/page Retry text is not detected;
- stable scoped Retry reaches `retry_visible_stable` bounded recovery;
- no Send, Retry or Regenerate click occurs in this recovery path;
- live-stream/dashboard existing reducer tests stay green.

Checkpoint projector-to-TRANSCRIPT + Retry only.

### Phase 4 — consume already-rendered valid response on Resume

Repair `CDPAWorker._recover_resume_waiting` with the smallest narrow path:
1. after reacquiring and verifying the exact saved page/binding/conversation, first allow the existing exact accepted bound assistant response to be observed/validated;
2. if it is exact-bound, parser-valid, references the expected report handoff, the expected report evidence exists, is stable, and there is no current unresolved projected activity / Retry / blocking error, consume it via the existing `_record_response -> _responded -> _complete_request_response` flow;
3. this Resume-only “already-rendered terminal evidence” path may tolerate missing completion action controls; normal live waiting must retain its stronger completion-action guard;
4. only if a `response_without_terminal_result` Resume still has no valid exact terminal result should the existing route-only repair be queued;
5. never call Send, Retry, Regenerate, or create another original request. RequestLedger attempts must remain exactly 1 and immutable receipt/page/user identities must remain unchanged.

Mandatory tests:
- make `test_resume_routes_valid_local_report_response_without_replay` green with `response_consumed`;
- keep `test_resume_missing_terminal_result_block_queues_route_repair_without_timeout_extension` green;
- preserve exact binding divergence/manual-composer/streaming Resume tests;
- assert Send/Retry/Regenerate counts remain zero and ledger attempts remain one.

Checkpoint Resume fix only.

### Phase 5 — preserve P-043/P-044, no duplicate implementation

Re-run the existing focused P-043/P-044 tests after the projector/Resume changes. Add a regression fixture only if the new DOM projection exposes a new malformed-path bug. Otherwise make no production change.

Do not edit `PROBLEM.md` yet merely because unit tests pass. At final integration, mark P-043/P-044 resolved only if required test + runtime/live evidence is sufficient. Because `PROBLEM.md` was dirty before this task, selective-stage only task-owned lines if it is changed.

### Phase 6 — EndpointPool physical identity reattach

Modify only `EndpointPool.refresh` / a tiny helper:
- on the same periodic cadence already used for online auth/readiness refresh (not every tight scheduler iteration), probe `/json/version` for each reused online runtime;
- compute current `_physical_identity`;
- if it differs from `runtime.physical_id`, stop only the Playwright attachment via `_stop_attachment`, mark runtime unavailable/unknown with immediate reconnect eligibility, then reuse the existing `_connect` path in the same/next normal refresh;
- never close the user's Chromium process;
- rebuild/maintain the `claimed` set so the existing duplicate-physical-browser conflict guard remains authoritative; never steal another endpoint's browser;
- preserve existing worker `TargetClosedError -> mark_unavailable` handling.

Tests in `tests/test_cdpa_endpoint_pool.py`:
- same-address physical identity change replaces only that endpoint's stale attachment and obtains the new context;
- healthy peer context is untouched;
- same physical identity does not reconnect unnecessarily;
- new identity colliding with another registered runtime remains conflict/fail-closed.

Checkpoint endpoint reattach only.

### Phase 7 — integration evidence and cleanup

Run focused + regression suites with the existing environment interpreter. `uv run pytest` is currently unusable because the running worker holds `.venv\Scripts\cdpa-worker.exe` and uv fails with Access Denied before test execution. This is an environment conflict, not a test failure. Use:
`.\.venv\Scripts\python.exe -m pytest ...`
unless DEV intentionally stops/restarts the worker under an authorized phase.

Minimum regression groups:
- `tests/test_role_runtime.py`
- `tests/test_role_runtime_browser.py`
- `tests/test_live_stream.py`
- relevant live-stream reducer cases in `tests/test_dashboard.py`
- `tests/test_operator_resume_recovery.py`
- `tests/test_cdpa_solo_task.py`
- `tests/test_cdpa_endpoint_pool.py`
- `tests/test_cdpa_pool_audit.py`
- `tests/test_cdpa_presend_cleanready_rootfix.py`
- exact-send/binding/no-resend tests touched by the changed boundaries.

Then obtain appropriate live/runtime evidence on endpoint **9202 only**:
- a response with visible middle reasoning/progress/tool activity yields incremental assistant TRANSCRIPT before final;
- text-only scoped Retry transitions to bounded recovery if safely reproducible without destructive mutation;
- final route still waits for all active tool/progress evidence to clear;
- no replay / ledger attempts remain one.

Inspect `git status --short`, `git diff`, every phase commit, and `git diff <baseline>..HEAD --name-status`. Verify no protected pre-existing dirty/untracked file/hunk entered a task commit. No push.

Final independent routing expectation: DEV -> REVIEW. REVIEW inspects actual commits/diff and over-engineering boundary. If implementation is clean but live/runtime evidence remains incomplete, REVIEW routes TEST; otherwise REVIEW routes PLAN for closure. Only PLAN may DONE.

## Focused evidence already collected by PLAN

Command (existing venv):
`.\.venv\Scripts\python.exe -m pytest -q tests/test_operator_resume_recovery.py::test_resume_routes_valid_local_report_response_without_replay tests/test_role_runtime.py::test_stable_malformed_result_repairs_with_exact_validation_error_without_reload tests/test_role_runtime.py::test_stale_listen_stream_status_cannot_hide_stable_dom_malformed_result tests/test_cdpa_solo_task.py::test_solo_route_repair_prompt_carries_exact_error_and_candidate_without_goal tests/test_cdpa_solo_task.py::test_solo_route_repair_actual_send_persists_exact_repair_prompt_once tests/test_cdpa_solo_task.py::test_solo_repeated_invalid_repairs_are_bounded tests/test_cdpa_endpoint_pool.py tests/test_cdpa_presend_cleanready_rootfix.py`

Result: **1 failed, 26 passed in 2.41s**. Only failure is the required C regression: `response_consumed` expected, `observation_rearmed` actual. P-043/P-044, current EndpointPool unit suite, and preboundary clean-ready tests were green; the additional physical-identity-change probe independently reproduced F because the existing suite does not cover that case.

## Non-goals / invariants

- No new orchestration framework, generalized state machine, selector-registry platform, or second response engine.
- MutationObserver remains a wake source, not semantic authority.
- Exact accepted Send ownership, request ledger, no-resend, task/team/page binding, conversation ownership and fail-closed safety remain authoritative.
- Never weaken `foreign_user_before_bound_assistant` or `conversation_owned_by_other_task` without a new current-source false-positive reproduction and a safer exact-identity fix.
- Do not touch preboundary acquisition solely to clear old manifests.
- Progress/tool/reasoning evidence may prove liveness but never completion.
- Existing final route/report parser and report evidence gates remain authoritative.
- Keep text/DOM work bounded.

## Commit contract

Required small checkpoints:
1. PLAN/research report (this file).
2. Response projector + RED->GREEN fixture tests.
3. TRANSCRIPT wiring + scoped Retry detection/recovery.
4. Resume response-consumption fix.
5. P-043/P-044 only if a real regression requires code; otherwise no commit.
6. Endpoint physical-identity reattach.
7. Final integration/evidence/report cleanup.

Record every DEV/REVIEW/TEST checkpoint SHA in subsequent reports. A Git commit cannot embed its own final SHA in the file it hashes without changing that SHA; therefore this PLAN report identifies its checkpoint by path and consumers should resolve it with `git log -1 -- <this report path>`, then record that SHA in the next handoff.

## Acceptance gate for later PLAN closure

DONE is forbidden until all user acceptance bullets are verified, including ordered mixed segments, rerender dedupe, scoped text-only Retry, activity completion veto, Resume response consumption with zero replay, P-043/P-044 preservation, endpoint reattach if reproduced (it was), exact-send/no-resend/ownership/preboundary regressions, and independent REVIEW/TEST evidence as needed.

Route now: **DEV**.
