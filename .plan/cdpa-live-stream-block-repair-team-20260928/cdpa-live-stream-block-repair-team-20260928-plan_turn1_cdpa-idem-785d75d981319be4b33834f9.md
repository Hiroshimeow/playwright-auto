# PLAN report — CDPA live stream / BLOCKED recovery repair (current-truth refresh)

Task: `cdpa-idem-785d75d981319be4b33834f9`  
Team: `cdpa-live-stream-block-repair-team-20260928`  
Workspace: `E:\git-project\playwright-auto-multicdp`  
Execution constraint: g6 only via @md; endpoint 9202 only; no push.

## Decision

**Route REVIEW, not DEV.**

The repository advanced materially after PLAN intake. Current HEAD is now:
`a058d29d964e3040838461fb48db564ed0626456` (`fix: scope exact Retry to current response`).

The originally planned implementation has largely already landed through the Solo chain. Current evidence says A/B/C/F are implemented and focused tests are green. D/E were already implemented before that chain and remain green. G/H remain preserve-only.

The remaining gap is **independent review of the latest committed state**, because:
- the first independent REVIEW found 3 real defects;
- those were fixed in `721db28`;
- a later exact-Retry scoping fix landed in `a058d29`;
- the intended REVIEW r2 never inspected those fixes because it blocked before Send on `preboundary_acquisition_recovery_required`;
- live ChatGPT runtime proof for incremental middle-response projection on endpoint 9202 is still not independently established.

Therefore another implementation pass would be wasteful unless REVIEW reproduces a concrete defect.

## Repository truth

PLAN initially inspected HEAD `2e17db3ba29b17322b75dd40167a3a30513be7d7`. Re-check on 2026-09-29 found:

- `a3e87d6` — PLAN checkpoint.
- `fceb5a8` — bounded current-response timeline/projector + TRANSCRIPT wiring + initial scoped Retry.
- `f731f8d` — Resume-only exact terminal response consumption without normal completion controls.
- `01830e2` — EndpointPool physical browser identity revalidation / reattach.
- `b1acecf` — projection/recovery contract tightening.
- `332804c` — DEV evidence report.
- `e8a8e76` — separate user-steering behavior change; current HEAD includes it but it is not part of this repair's original owned scope.
- `721db28` — fixes REVIEW findings: projected/real transcript dedup, latest-turn Retry scope, completed activity veto.
- `a058d29` — further scopes even exact testid Retry to the current response.

Current worktree still contains pre-existing dirty/untracked material that must not be reset/stashed/cleaned or accidentally committed:
- modified: `.agents/agents/code-reviewer.md`, `.gitignore`, `AGENTS.md`, `PROBLEM.md`, `README.md`, `prompts/cdpa/BASE_CONTEXT.md`, `prompts/cdpa/RESPONSE_GUIDE.md`, `src/playwright_auto/cdpa_defaults/prompts/cdpa/BASE_CONTEXT.md`
- untracked: `.github/`, `.skills/solo-role/`, `.skills/using-codex/`, `.skills/using-pi-coding/`, `NUL`, `state_api_dump.json`, `task_api_dump.json`.

Do not attribute or stage those files/hunks to this task.

## Upstream research retained from PLAN

Current upstream `miuuyy/codex-chatgpt-web` was verified at commit:
`2d73f626290a5062825bb595dabb89aaf88d16c5`, MIT license.

Relevant current concepts:
- logical turn identity is more reliable than display order;
- visible status rows / reasoning / commentary are projected separately from final answer;
- answer chunks can legitimately occur between multiple tool/status containers;
- unresolved tool activity must veto completion;
- completion requires a fresh stable final-answer projection after tool boundaries;
- progress/status proves liveness only, never completion.

CDPA should continue borrowing these narrow lifecycle concepts only; no broker/orchestration port is justified.

## Current implementation assessment

### A. Incremental current-response projection — COMPLETE pending independent review/live proof

Current owner:
- `src/playwright_auto/chatgpt_response_projection.py`
- `src/playwright_auto/chatgpt.py`
- `src/playwright_auto/chatgpt_models.py`
- `src/playwright_auto/role_runtime/signals.py`.

Current projector:
- bounds output to 24 segments / 24k text;
- scopes to the logical response turn after the latest user;
- preserves DOM order;
- emits answer/tool/reasoning/status segments;
- uses stable synthetic keys based on turn + kind + ordinal;
- keeps projected activity outside terminal `ChatGPTSnapshot.messages`;
- exposes `response_projection_active` only from unresolved non-answer activity.

`721db28` additionally fixed the first REVIEW's finding that completed retained tool/status rows could keep activity permanently active and fixed projected/real answer duplication.

Focused/browser/runtime projection tests are currently green.

Still not independently proven on real ChatGPT 9202 after the final fixes: visible middle reasoning/tool/status must generate assistant TRANSCRIPT before final answer in an actual current UI response.

### B. Text-only / exact Retry scoping — COMPLETE pending independent review

Current resolver is `RETRY_RESOLVER_JS` in `chatgpt_response_projection.py`.

Current behavior:
- exact `data-testid=regenerate-thread-error-button` is no longer globally accepted;
- exact and text-only Retry must be visible/enabled, structurally inside a response root, and at/after the latest user turn;
- text-only fallback requires exactly one matching `Retry`;
- historical Retry before latest user is ignored;
- no automatic Retry/Regenerate caller was added.

`a058d29` specifically added coverage for historical exact-testid Retry vs current response.

### C. Resume valid terminal response consumption — COMPLETE pending independent review

Current owners:
- `role_runtime/response.py`
- `cdpa_worker.py::_recover_resume_waiting`.

`f731f8d` adds a transient Resume-only relaxation for exact-bound, parser-valid, stable terminal response when completion action controls are absent.

Safety guards remain:
- no unresolved response projection activity;
- no Retry;
- no blocking dialog/error;
- exact accepted binding/receipt preserved;
- normal live waiting still requires normal completion evidence.

Focused regression now passes for both `response_timeout` and `response_without_terminal_result` recovery, with zero Send/Retry/Regenerate replay and ledger attempts remaining exactly 1.

### D. P-043 malformed_result — COMPLETE / PRESERVE

Current controller already surfaces a stable invalid candidate as `malformed_result` after normal stability once current-page generation activity clears.

Current focused tests pass:
- stable malformed result -> repair with exact validation error;
- stale LISTEN `IS_STREAMING` cannot hide stable malformed DOM result.

Do not reimplement unless REVIEW reproduces a current regression.

### E. P-044 Solo route repair exact parser error — COMPLETE / PRESERVE

Current Solo format repair:
- includes exact validation/parser error;
- includes malformed candidate as inert JSON-string data;
- excludes substantive goal;
- asks only for corrected route JSON;
- keeps bounded repair retries.

Current focused tests pass.

### F. EndpointPool stale BrowserContext after physical restart — COMPLETE pending independent review

`01830e2` changed `EndpointPool.refresh` to revalidate physical browser identity on the existing readiness/auth cadence.

Current behavior:
- same-address changed `/json/version` browser identity invalidates only the stale Playwright attachment;
- reconnect uses the existing path;
- healthy peers remain untouched;
- duplicate physical-browser ownership remains fail-closed.

Current endpoint/pool tests are green.

### G. preboundary acquisition — PRESERVE / NO PRODUCTION CHANGE

Current clean-ready/preboundary focused tests are green.

Important: Solo REVIEW r2 itself blocked with `preboundary_acquisition_recovery_required` before reviewing the latest fixes. That task-state incident is **not proof of a current production regression** in this scope. Do not weaken acquisition ownership merely to clear that old blocked task.

### H. ownership guardrails — PRESERVE

No current false positive was reproduced for:
- `foreign_user_before_bound_assistant`
- `conversation_owned_by_other_task`.

Do not weaken them.

## Independent-review history

### DEV implementation report

`.plan/cdpa-live-stream-block-repair-team-20260928/cdpa-live-stream-block-repair-solo-dev-turn1.md`

DEV reported checkpoints through `b1acecf`, with:
- 124 passed role-runtime/browser/signals/live-stream/dashboard;
- 68 passed + 4 skipped endpoint/pool/solo/presend/exact-send group;
- 7 focused Resume ownership/no-replay passes.

### REVIEW r1

Task `cdpa-idem-438bf9cf2536dc82261141cf` completed and found 3 concrete defects:
1. duplicate projected + real assistant transcript rows;
2. text-only Retry could bind historical response instead of latest logical turn;
3. retained completed status/tool rows could keep `response_projection_active=true` indefinitely.

It spawned DEV r3.

### DEV r3

Task `cdpa-idem-b2f83c9f92b5955919cbba80` completed:
- fixed all 3 REVIEW r1 defects in `721db28`;
- reported focused + role/signals/live-stream + browser/dashboard + Resume/no-replay tests green;
- spawned REVIEW r2.

### REVIEW r2

Task `cdpa-idem-b12b44bc67025b078e7d2a84` did **not** perform the review. It blocked in `pre_send` with:
`preboundary_acquisition_recovery_required`.

Therefore there is no valid independent clean review after `721db28`, and certainly none after later `a058d29`.

## Fresh PLAN verification on current HEAD a058d29

Used the existing venv directly because `uv run` previously conflicted with the running worker binary.

### Core response projection/live stream/dashboard

`.\.venv\Scripts\python.exe -m pytest -q tests\test_role_runtime.py tests\test_role_runtime_browser.py tests\test_role_runtime_signals.py tests\test_live_stream.py tests\test_dashboard.py`

Result: **129 passed in 22.60s**.

This covers current response projection, ordered segments, dedup/reducer behavior, Retry scoping fixtures, activity veto, and dashboard/live-stream contracts.

### Focused Resume + P-043/P-044

Focused command covering:
- valid accepted response consumed without replay;
- missing-terminal-result repair preservation;
- exact Solo repair prompt/error/candidate;
- bounded Solo repair;
- malformed_result and stale-LISTEN malformed handling.

Result: **8 passed in 1.57s**.

### Endpoint / preboundary / exact-send safety

`.\.venv\Scripts\python.exe -m pytest -q tests\test_cdpa_endpoint_pool.py tests\test_cdpa_pool_audit.py tests\test_cdpa_presend_cleanready_rootfix.py tests\test_cdpa_late_send_identity.py tests\test_cdpa_send_recovery_audit.py`

Result: **34 passed, 4 skipped in 2.63s**.

### Full operator Resume file

`tests/test_operator_resume_recovery.py --maxfail=1`

Result before stop: **30 passed, 1 failed**.

Failure:
`test_reacquired_pristine_request_crosses_send_boundary_exactly_once`

Cause is the already-known test-fixture debt: its local `SimpleNamespace` lacks `logical_turn_ids`, while unchanged durable send code calls:
`capture_message_baseline(before_send.messages, before_send.logical_turn_ids)`.
This is outside the live-stream/Resume terminal fix range and was already documented by DEV. Treat it as an independent repo test debt, not a PASS. REVIEW should confirm whether it needs a separate task; do not silently fix it as scope creep.

### Diff hygiene

Task-relevant current range from PLAN checkpoint to HEAD:
- 11 relevant files;
- 630 insertions / 25 deletions across projector/model/wiring/Resume/EndpointPool/tests;
- `git diff --check` on that relevant range is clean.

This size is larger than the original minimal first patch because it includes later review fixes and the separate current-head user-steering changes in shared files. REVIEW must distinguish task-owned commits from unrelated `e8a8e76` rather than judging the aggregate as one authored patch.

## REVIEW execution contract

Review current HEAD `a058d29d964e3040838461fb48db564ed0626456`; do not review only `b1acecf`.

Inspect these task-owned checkpoints:
- `fceb5a8`
- `f731f8d`
- `01830e2`
- `b1acecf`
- `721db28`
- `a058d29`

Treat `e8a8e76` as concurrent/shared-file context, not automatically as task-owned scope.

REVIEW must independently verify:
1. projector chooses the actual current response turn, not historical roots;
2. mixed status/answer/status/answer preserves all answer/activity order;
3. synthetic keys remain stable under rerender/class churn;
4. projected + real answer events do not duplicate;
5. completed historical tool/status rows do not keep active forever;
6. unresolved current tool/status still vetoes terminal completion;
7. both text-only and exact-testid Retry are scoped to the current response and ambiguous/global Retry is ignored;
8. no automatic Retry/Regenerate/Send replay path was introduced;
9. Resume missing-completion-action relaxation is reachable only during Resume, remains exact-bound/parser-valid/stable, and is vetoed by activity/Retry/error/dialog;
10. `response_without_terminal_result` consumes an already-rendered valid result before queuing route repair;
11. EndpointPool reattach cannot steal another endpoint's browser and does not close Chromium;
12. P-043/P-044 and preboundary/ownership guardrails remain intact;
13. no pre-existing dirty/untracked content entered the task commits;
14. no unnecessary second response engine/state-machine/platform was introduced.

If REVIEW finds a concrete code defect, route DEV.

If code review is clean but live current-ChatGPT evidence is still missing, route TEST with endpoint **9202 only**. TEST should obtain runtime evidence that middle reasoning/progress/tool activity yields incremental assistant TRANSCRIPT before final and that terminal completion waits for unresolved activity to clear.

If REVIEW + required live TEST are both clean, route PLAN for closure. Only PLAN may route DONE.

## Non-goals

- No new orchestration framework.
- No selector registry platform.
- No second response state machine.
- No global arbitrary text Retry.
- No progress/tool signal as completion authority.
- No ownership/preboundary weakening to clear stale manifests.
- No unrelated cleanup of the current dirty worktree.
- No push.

## Route

**REVIEW**.
