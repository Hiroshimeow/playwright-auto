# Tasks: Solo Existing-Conversation Correctness

**Input**: Design documents from `specs/001-fix-solo-reuse/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Required. This feature repairs stable orchestration regressions and must follow focused RED -> GREEN coverage before production edits.

**Organization**: Tasks are grouped by user story. Preserve unrelated dirty/untracked work and commit only task-owned hunks when the workflow authorizes a checkpoint.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel only when files and prerequisites do not conflict.
- **[Story]**: Maps to the user story in `spec.md`.

## Phase 1: Setup (Shared Test Scaffolding)

**Purpose**: Establish focused reusable test setup without changing production behavior.

- [X] T001 Add or reuse narrowly scoped Solo existing-conversation fixtures/helpers without duplicating current dirty work in `tests/test_cdpa_solo_task.py`

**Checkpoint**: Focused test setup exists and current unrelated work remains untouched.

---

## Phase 2: Foundational Safety Proofs

**Purpose**: Lock the existing invariants that every story depends on before production changes.

- [X] T002 [P] Add/confirm regression coverage for canonical conversation identity extraction from short-form and project-scoped paths in `tests/test_cdpa_commands.py`
- [X] T003 [P] Add/confirm a focused proof that `_is_pristine_preboundary_sending_record()` rejects any attempts/acceptance/binding/receipt evidence in `tests/test_cdpa_worker.py`

**Checkpoint**: Canonical identity and pristine pre-Send proof are covered before story implementation.

---

## Phase 3: User Story 1 - New Goal Is Always Delivered (Priority: P1) 🎯 MVP

**Goal**: A new Solo Task reusing an existing conversation sends its current Goal in the first substantive prompt.

**Independent Test**: New Solo task + existing idle conversation + distinctive Goal produces a first task prompt containing that Goal and current task role, not only `Continue from the current state.`

### Tests for User Story 1

- [X] T004 [US1] Replace the regression expectation that an idle reused conversation omits Goal with a RED expectation for full first-hop Goal delivery in `tests/test_cdpa_solo_task.py`
- [X] T005 [P] [US1] Add prompt-builder coverage proving true same-task/format-repair continuation remains compact while first task hop remains substantive in `tests/test_cdpa_solo_task.py`

### Implementation for User Story 1

- [X] T006 [US1] Separate `hop.kind == "task"` semantics from existing `conversation_url` reuse when selecting Solo continuation mode in `src/playwright_auto/cdpa_worker.py`
- [X] T007 [US1] Keep `build_solo()` compact-continuation behavior limited to real continuation/repair semantics and adjust only if tests expose an ownership ambiguity in `src/playwright_auto/cdpa_prompts.py`

**Checkpoint**: An idle existing conversation receives the new Goal exactly once; format repair remains compact.

---

## Phase 4: User Story 2 - Responding Conversation Waits Instead of Blocking (Priority: P1)

**Goal**: Pre-existing conversation activity is observed without blocking or duplicate Send, and the pending new Goal resumes only after the prior response ends.

**Independent Test**: Reproduce both already-responding pre-send and ready-to-sending race paths; verify zero new Send attempts while prior activity is live, no old response is consumed as the new task result, and the pending request resumes safely afterward.

### Tests for User Story 2

- [X] T008 [US2] Strengthen the already-responding existing-URL test to require the single receiptless observation marker and zero prompt/send side effects in `tests/test_cdpa_solo_task.py`
- [X] T009 [P] [US2] Add a RED advance-level regression reproducing `pre_send -> sending -> UnsafePageStateError("page is already responding")` with RequestLedger NEW/attempts=0 and assert RUNNING/waiting rather than BLOCKED in `tests/test_cdpa_runtime_stability.py`
- [X] T010 [US2] Add a RED regression proving a receiptless observed prior assistant response is never accepted/routed/repaired as the new Solo task result and that completion resumes the pending Goal in `tests/test_cdpa_solo_task.py`
- [X] T011 [P] [US2] Add a safety regression proving attempted/accepted/non-pristine `sending` records are not converted to receiptless existing-response wait in `tests/test_operator_resume_recovery.py`

### Implementation for User Story 2

- [X] T012 [US2] Unify initial existing-response adoption on `observed_existing_response` and remove the unused parallel `adopted_existing_response` marker in `src/playwright_auto/cdpa_worker.py`
- [X] T013 [US2] Extend responding-exception recovery for a `sending` Solo task only when `_is_pristine_preboundary_sending_record()` proves the current request has not crossed the Send boundary in `src/playwright_auto/cdpa_worker.py`
- [X] T014 [US2] Add the receiptless observed-response waiting branch that observes activity only, never consumes the prior assistant answer, and resumes to `pre_send` or pristine `sending` as appropriate in `src/playwright_auto/cdpa_worker.py`
- [X] T015 [US2] Reuse existing role/page observation signals for active-response detection without adding a new polling loop or durable state in `src/playwright_auto/cdpa_worker.py` and `src/playwright_auto/role_runtime/` only if required by focused tests

**Checkpoint**: Normal responding state never creates `unexpected_error` before Send; old response cannot become the new task result; exactly-once safety remains intact.

---

## Phase 5: User Story 3 - Project-Scoped ChatGPT URLs Are Reusable (Priority: P2)

**Goal**: Accept valid project-scoped ChatGPT conversation URLs, preserve the exact user URL for navigation, and compare them by canonical conversation identity.

**Independent Test**: A project URL such as `https://chatgpt.com/g/<project>/c/<id>` is accepted by task creation/store, opened unchanged, and compares equal to `https://chatgpt.com/c/<id>`.

### Tests for User Story 3

- [X] T016 [P] [US3] Add API request validation tests for project-scoped Solo `conversation_url` and invalid host/scheme cases in `tests/test_dashboard_api.py`
- [X] T017 [P] [US3] Add durable store tests proving project-scoped URLs are accepted and preserved exactly in `tests/test_cdpa_solo_task.py`
- [X] T018 [P] [US3] Add actions/reopen coverage proving navigation uses the exact project-scoped URL while canonical identity verification still succeeds in `tests/test_cdpa_actions.py`

### Implementation for User Story 3

- [X] T019 [US3] Add one shared user-supplied ChatGPT conversation URL validator/normalizer beside `conversation_identity()` in `src/playwright_auto/cdpa_commands.py`, preserving the original URL and deriving identity only for validation/comparison
- [X] T020 [P] [US3] Replace duplicate exact-two-segment URL validation with the shared validator in `src/playwright_auto/dashboard_api.py`
- [X] T021 [P] [US3] Replace duplicate store URL validation with the shared validator in `src/playwright_auto/cdpa_store.py`
- [X] T022 [US3] Align exact adoption/reopen checks with the shared identity semantics without rewriting the supplied navigation URL in `src/playwright_auto/cdpa_actions.py`

**Checkpoint**: Project and short-form URLs share identity while the exact supplied URL is retained for navigation.

---

## Phase 6: User Story 4 - True Concurrent Ownership Still Fails Closed (Priority: P2)

**Goal**: Terminal historical metadata no longer false-blocks explicit reuse, while a non-terminal foreign owner remains a hard conflict.

**Independent Test**: Same conversation identity referenced by a terminal task permits new Solo adoption; the same identity referenced by a non-terminal task still blocks before Send.

### Tests for User Story 4

- [X] T023 [US4] Add paired RED/GREEN ownership tests for terminal historical versus non-terminal foreign task ownership in `tests/test_cdpa_solo_task.py`

### Implementation for User Story 4

- [X] T024 [US4] Skip terminal tasks in foreign durable conversation-owner detection while preserving non-terminal fail-closed ownership in `src/playwright_auto/cdpa_worker.py`

**Checkpoint**: Historical ownership is not live ownership; actual concurrent control remains prohibited.

---

## Phase 7: Polish & Cross-Cutting Validation

**Purpose**: Align operator/agent guidance with verified behavior and prove no regression across the changed boundaries.

- [X] T025 [P] Update project-scoped existing-conversation wording and new-task Goal semantics after behavior is GREEN in `.skills/solo-role/SKILL.md` without overwriting unrelated untracked content
- [X] T026 [P] Update only directly conflicting Solo URL wording in `AGENTS.md` if still stale after implementation
- [X] T027 Run focused feature validation from `specs/001-fix-solo-reuse/quickstart.md` and record failures before widening scope
- [X] T028 Run relevant recovery/worker safety suites `tests/test_operator_resume_recovery.py`, `tests/test_cdpa_worker.py`, and `tests/test_chatgpt_safety.py`
- [X] T029 Run the full repository pytest suite if shared identity/worker changes warrant it, then run `git diff --check`
- [X] T030 Review the final diff against `specs/001-fix-solo-reuse/spec.md`, `plan.md`, and `.specify/memory/constitution.md`; remove duplicate policy/unused compatibility paths and preserve unrelated dirty work

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 Setup**: starts immediately.
- **Phase 2 Foundational**: depends on Phase 1; establishes shared safety proofs.
- **US1 and US3**: may proceed after Phase 2 and mostly touch independent boundaries.
- **US2**: depends on foundational pristine-ledger proof and should complete before final ownership/runtime validation.
- **US4**: can proceed after Phase 2 but final verification should include US2/US3 because ownership uses canonical identity.
- **Polish**: depends on all selected stories being GREEN.

### User Story Dependencies

- **US1 (P1)**: no production dependency on other stories.
- **US2 (P1)**: depends on the existing pristine-ledger invariant; does not require the project-URL implementation to reproduce with a short URL.
- **US3 (P2)**: independently testable at API/store/actions identity boundaries.
- **US4 (P2)**: uses canonical conversation identity and should be re-run after US3.

### Parallel Opportunities

- T002 and T003 can run in parallel.
- US1 prompt work and US3 URL-boundary work can proceed in parallel if different workers avoid overlapping `tests/test_cdpa_solo_task.py` edits.
- T016, T017, and T018 are parallelizable because they target different test files.
- After T019, T020 and T021 are parallelizable because they target different production files.
- Documentation T025/T026 is parallelizable only after production behavior is verified.

## Parallel Example: URL Story

```text
Task: T016 API project-URL tests in tests/test_dashboard_api.py
Task: T017 store project-URL tests in tests/test_cdpa_solo_task.py
Task: T018 actions exact-navigation tests in tests/test_cdpa_actions.py
```

After T019:

```text
Task: T020 dashboard API adopts shared URL validator
Task: T021 store adopts shared URL validator
```

## Implementation Strategy

### MVP First

1. Complete shared safety proofs.
2. Complete US1 so a reused idle conversation receives the new Goal.
3. Complete US2 so active conversations wait safely and then deliver that Goal.
4. Validate both P1 stories before broadening to URL-shape and historical-owner cleanup.

### Incremental Delivery

1. Goal authority GREEN.
2. Responding/race semantics GREEN with zero duplicate sends.
3. Project-scoped URL support GREEN.
4. Historical/live ownership distinction GREEN.
5. Cross-cutting recovery/full-suite validation.

## Notes

- Tests for stable bugs must be RED before production repair where deterministic reproduction is practical.
- Do not mark a task `[X]` until its acceptance evidence has been run.
- Do not modify custom checklist markers from `$speckit-implement`; requirements review owns them.
- Do not push. Checkpoint commits must contain only task-owned hunks if the active Team workflow authorizes commits.
