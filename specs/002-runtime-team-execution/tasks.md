# Tasks: Runtime Team Execution

**Input**: Design documents from `/specs/002-runtime-team-execution/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included because this feature changes runtime orchestration, legacy task ownership, accepted-send recovery, and operator-visible audit behavior.

**Organization**: Tasks are grouped by user story so each story can be implemented and validated independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel with other [P] tasks in the same phase.
- **[Story]**: User story label from spec.md.
- Every task names the concrete file path(s) to inspect or modify.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm implementation boundaries before touching source code.

- [X] T001 Read `.agents/rules/project.md`, `specs/002-runtime-team-execution/spec.md`, `specs/002-runtime-team-execution/plan.md`, and all files under `specs/002-runtime-team-execution/contracts/` before modifying source.
- [X] T002 [P] Inspect current runtime-v2 task submission implementation in `src/playwright_auto/hcuw_runtime/task_runner.py`, `src/playwright_auto/hcuw_runtime/api_server.py`, and `src/playwright_auto/hcuw_runtime/state.py` against `contracts/runtime-team-api.md`.
- [X] T003 [P] Inspect current legacy workflow role implementation in `src/playwright_auto/cdpa_store.py`, `src/playwright_auto/cdpa_workflow_agents.py`, `src/playwright_auto/cdpa_routes.py`, `src/playwright_auto/cdpa_prompts.py`, `src/playwright_auto/cdpa_cli.py`, and `src/playwright_auto/dashboard_api.py` against `contracts/workflow-role-contract.md`.
- [X] T004 [P] Inspect current accepted-send provisional conversation handling in `src/playwright_auto/cdpa_worker.py` and `tests/test_cdpa_pool_integration_edges.py` against `contracts/accepted-send-contract.md`.
- [X] T005 [P] Inspect current operator audit behavior in `src/playwright_auto/hcuw_runtime/audit.py`, `src/playwright_auto/hcuw_runtime/dashboard_server.py`, and `tests/hcuw_runtime/test_dashboard_server.py` against `contracts/runtime-team-api.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Add or update shared tests/contracts that block all story work.

**CRITICAL**: No user story implementation should begin until these tests and guardrails are present.

- [X] T006 [P] Add/adjust runtime-v2 request validation tests in `tests/hcuw_runtime/test_task_runner.py` for roles array, `start_role` membership, PLAN/REVIEW closing-role requirement, and rejection of Solo/Independent fields.
- [X] T007 [P] Add/adjust legacy role-selection tests in `tests/test_cdpa_core.py` for `DEV,REVIEW` teams starting at DEV and REVIEW being the DONE authority when PLAN is absent.
- [X] T008 [P] Add/adjust route parser tests in `tests/test_cdpa_core.py` or a focused route test file for closing-role-specific DONE errors: `only PLAN may route DONE` when PLAN exists and `only REVIEW may route DONE` when PLAN is absent.
- [X] T009 [P] Add/adjust accepted-send race tests in `tests/test_cdpa_pool_integration_edges.py` for provisional URL with exact accepted user turn, lost user turn, lost page, and reconciliation eligibility.
- [X] T010 [P] Add/adjust dashboard/CLI tests in `tests/test_dashboard.py` and a CLI-focused test file for dashboard role selection without forced PLAN and `cdpa --roles DEV,REVIEW` behavior.
- [X] T011 Confirm all new tests fail on missing behavior or document why current code already satisfies them in `specs/002-runtime-team-execution/quickstart.md` notes.

**Checkpoint**: Test harness can prove or reject all three user stories before implementation changes.

---

## Phase 3: User Story 1 - Submit a real Team Task from runtime-v2 (Priority: P1) MVP

**Goal**: Runtime-v2 submits real Team Tasks only, with explicit selected roles and start role.

**Independent Test**: Submit `roles=DEV,REVIEW` and `start_role=DEV` through 9227; verify legacy task contains only DEV/REVIEW, starts at DEV, and no Solo/Independent work is created.

### Tests for User Story 1

- [X] T012 [P] [US1] Add runtime-v2 API POST tests in `tests/hcuw_runtime/test_api_server.py` for `roles`, `start_role`, validation errors, and response shape from `POST /api/tasks`.
- [X] T013 [P] [US1] Add task-runner bridge config tests in `tests/hcuw_runtime/test_task_runner.py` proving `.runtime-v2/task-runner/legacy-cdpa-bridge.json` includes repository allowed root and `independent_agents.seed_builtins=false`.
- [X] T014 [P] [US1] Add legacy API/dashboard validation tests in `tests/test_dashboard.py` or `tests/test_dashboard_api.py` proving Team Task roles may omit PLAN when REVIEW is selected and Solo-only fields remain rejected where appropriate.

### Implementation for User Story 1

- [X] T015 [US1] Update `TaskSubmissionRequest` in `src/playwright_auto/hcuw_runtime/task_runner.py` to include selected `roles` and validated `start_role`.
- [X] T016 [US1] Update runtime-v2 request validation in `src/playwright_auto/hcuw_runtime/task_runner.py` to reject Solo/Independent fields and to reject invalid role sets before legacy enqueue.
- [X] T017 [US1] Update runtime-v2 legacy submission in `src/playwright_auto/hcuw_runtime/task_runner.py` to pass `roles` and `start_role` through the legacy Team Task creation path without adding PLAN silently.
- [X] T018 [US1] Update runtime-v2 task records in `src/playwright_auto/hcuw_runtime/state.py` and response construction in `src/playwright_auto/hcuw_runtime/api_server.py` so `/api/tasks` exposes `roles`, `start_role`, legacy task id, command id, browser, and status.
- [X] T019 [US1] Update legacy task creation owners in `src/playwright_auto/cdpa_store.py` and `src/playwright_auto/cdpa_workflow_agents.py` so selected Team Task roles do not require PLAN but do require PLAN or REVIEW.
- [X] T020 [US1] Update legacy CLI create-task handling in `src/playwright_auto/cdpa_cli.py` to support `--roles DEV,REVIEW` for Team Tasks and reject `--roles` in taskless resume mode.
- [X] T021 [US1] Update dashboard role-selection behavior in `src/playwright_auto/dashboard_assets/views/dashboard_actions.js` and validation in `src/playwright_auto/dashboard_api.py` so PLAN is not forced and invalid no-closer role sets fail closed.

**Checkpoint**: Runtime-v2 can create a DEV+REVIEW Team Task that starts at DEV.

---

## Phase 4: User Story 2 - Preserve exactly-once Send through local-chatgpt races (Priority: P2)

**Goal**: Accepted Send on a provisional local-chatgpt URL keeps observing exact page evidence instead of blocking immediately or replaying.

**Independent Test**: Simulate and live-test a provisional accepted-send page; verify no immediate `accepted_conversation_identity_unresolved` block while exact page/user turn persists, and verify canonical enrichment when `/c/<id>` appears.

### Tests for User Story 2

- [X] T022 [P] [US2] Add or adjust unit tests in `tests/test_cdpa_pool_integration_edges.py` for provisional accepted Send with exact user turn continuing observation without reopening.
- [X] T023 [P] [US2] Add or adjust unit tests in `tests/test_cdpa_pool_integration_edges.py` for provisional accepted Send with missing user turn or missing page blocking safely without reopen/resend.
- [X] T024 [P] [US2] Add or adjust recovery eligibility tests in `tests/test_cdpa_pool_integration_edges.py` or `tests/test_cdpa_commands.py` proving retry/restart/new_chat are not eligible after accepted Send, while reconciliation/route path is eligible.

### Implementation for User Story 2

- [X] T025 [US2] Update provisional conversation detection in `src/playwright_auto/cdpa_worker.py` so `local-chatgpt:` is treated as a live-page hint, not a durable reopen address.
- [X] T026 [US2] Update `_owned_or_block` in `src/playwright_auto/cdpa_worker.py` so exact page plus visible accepted user turn keeps observing and retries canonicalization on later ticks.
- [X] T027 [US2] Update canonical receipt enrichment in `src/playwright_auto/cdpa_worker.py` so canonical `/c/<id>` updates the receipt and request ledger only from exact owned page evidence.
- [X] T028 [US2] Update recovery control behavior in `src/playwright_auto/cdpa_commands.py` and `src/playwright_auto/cdpa_worker.py` so retry/restart/new_chat do not replay an accepted Send and reconciliation uses durable evidence.
- [X] T029 [US2] Update reconciliation handoff text in `src/playwright_auto/cdpa_worker.py` so the closing role is PLAN if available, otherwise REVIEW, and the handoff explicitly forbids replaying side effects.

**Checkpoint**: A live verification task can pass through `local-chatgpt:` acceptance and reach canonical `/c/<id>` without blocking or duplicate Send.

---

## Phase 5: User Story 3 - Verify progress from runtime-v2 audit surfaces (Priority: P3)

**Goal**: Operators and later agents can inspect task/audit state through 9226/9227 and distinguish current verification work from old blocks.

**Independent Test**: Submit a runtime-v2 Team Task and verify 9226/9227 display runtime task id, legacy task id, role set, start role, current state, block code if any, and active DOM/LISTEN/action source states.

### Tests for User Story 3

- [X] T030 [P] [US3] Add or adjust `/api/tasks` projection tests in `tests/hcuw_runtime/test_api_server.py` for latest projected status and old-block separation.
- [X] T031 [P] [US3] Add or adjust audit source tests in `tests/hcuw_runtime/test_audit.py` and `tests/hcuw_runtime/test_dashboard_server.py` for actions, DOM, mutation, LISTEN status fields and 0.5-second focused lazy refresh markers.
- [X] T032 [P] [US3] Add or adjust dashboard HTML/JS tests in `tests/hcuw_runtime/test_dashboard_server.py` for displaying roles, start role, block code, and latest update time.

### Implementation for User Story 3

- [X] T033 [US3] Update runtime-v2 task listing in `src/playwright_auto/hcuw_runtime/api_server.py` and `src/playwright_auto/hcuw_runtime/state.py` to include role set, start role, and projected legacy status without mutating legacy state.
- [X] T034 [US3] Update audit projection in `src/playwright_auto/hcuw_runtime/audit.py` and any observer adapter files so DOM/LISTEN/action states are bounded, timestamped, and clearly source-owned.
- [X] T035 [US3] Update dashboard 9226 rendering in `src/playwright_auto/hcuw_runtime/dashboard_server.py` so it distinguishes current runtime-v2 verification tasks from old legacy blocked tasks.
- [X] T036 [US3] Update dashboard lazy refresh in `src/playwright_auto/hcuw_runtime/dashboard_server.py` so polling occurs every 0.5 seconds only while `document.visibilityState === 'visible'` and `document.hasFocus()` is true.

**Checkpoint**: 9226/9227 provide enough evidence for an operator to decide resume/reconciliation/handoff in under 10 seconds.

---

## Final Phase: Validation, Handoff, and Non-Implementation Stop

**Purpose**: Prove the feature is ready for review and handoff to the operator/DEV agent.

- [X] T037 [P] Update `specs/002-runtime-team-execution/quickstart.md` with actual validation command output locations and any known unrelated failures.
- [X] T038 [P] Update `specs/002-runtime-team-execution/plan.md` Complexity Tracking only if implementation required a constitution deviation.
- [X] T039 [P] Update `specs/002-runtime-team-execution/data-model.md` and contracts if implementation changed any accepted entity or interface shape.
- [X] T040 Run `uv run pytest tests/hcuw_runtime -q` and record result in the DEV handoff report.
- [X] T041 Run focused legacy tests for `tests/test_cdpa_core.py`, `tests/test_cdpa_pool_integration_edges.py`, `tests/test_dashboard.py`, and any new CLI/dashboard API tests; document unrelated existing failures separately.
- [X] T042 Run `git diff --check` and remove BOM/whitespace issues from modified files.
- [ ] T043 Run live smoke validation from `specs/002-runtime-team-execution/quickstart.md` with a DEV+REVIEW task and capture `/api/tasks`, `/api/audit/snapshot`, and board evidence.
- [X] T044 Produce a DEV completion report under `.plan/<team>/` or the operator-specified report path summarizing files changed, tests, live evidence, known risks, and whether implementation should stop or continue.

---

## Dependencies & Execution Order

### Phase Dependencies

- Phase 1 must complete before touching implementation files.
- Phase 2 tests/guardrails must be created or consciously mapped to existing tests before user-story implementation.
- User Story 1 is the MVP because no live Team Task can be verified without runtime-v2 submission.
- User Story 2 can begin after Phase 2 and does not require User Story 3.
- User Story 3 can begin after Phase 2 and can run partly in parallel with User Story 2.
- Final validation depends on all selected user stories being complete.

### User Story Dependencies

- **US1**: No dependency on other user stories after foundational tests.
- **US2**: Depends on accepted-send test fixtures and legacy worker ownership review from Phase 2.
- **US3**: Depends on runtime-v2 task records from US1 for full live display, but audit source tests can be built earlier.

### Parallel Opportunities

- T002-T005 can run in parallel.
- T006-T010 can run in parallel.
- T012-T014 can run in parallel before US1 implementation.
- T022-T024 can run in parallel before US2 implementation.
- T030-T032 can run in parallel before US3 implementation.
- T037-T039 can run in parallel during final documentation cleanup.

## Implementation Strategy

### MVP First

1. Complete Phase 1 and Phase 2.
2. Complete US1 only.
3. Validate that runtime-v2 can submit `DEV,REVIEW` starting at DEV.
4. Stop and demo before touching accepted-send recovery if the operator wants a smaller checkpoint.

### Incremental Delivery

1. US1: runtime-v2 real Team Task submission.
2. US2: exactly-once accepted-send provisional URL handling.
3. US3: operator audit and lazy dashboard readiness.
4. Final validation/live smoke.

### Handoff Rule

This tasks file is a pre-implementation handoff. Do not check off or execute implementation tasks until the operator explicitly assigns a DEV agent to implement.
