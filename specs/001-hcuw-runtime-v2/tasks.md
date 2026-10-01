# Tasks: HCUW Runtime V2 Skeleton

**Input**: Design documents from `specs/001-hcuw-runtime-v2/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Status**: Draft - stop before implementation until operator approval

**Tests**: TDD is required for implementation tasks after approval. This task list is prepared now, but source implementation must not start until review approval.

**Organization**: Tasks are grouped by independently reviewable user story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel after dependencies are satisfied.
- **[Story]**: Maps to User Story in `spec.md`.
- All implementation tasks include exact file paths and owner group names.

---

## Phase 0: Review Gate - No Source Implementation Yet

**Purpose**: Preserve the operator's final review boundary.

- [x] T000 Confirm this planning pass does not create or modify runtime implementation files under `src/playwright_auto/hcuw_runtime/`
- [ ] T001 Operator reviews `specs/001-hcuw-runtime-v2/spec.md`, `plan.md`, `contracts/runtime-api.md`, and `contracts/code-organization-contract.md` before implementation begins

**Checkpoint**: Stop here until the operator explicitly approves implementation.

---

## Phase 1: Setup After Approval

**Purpose**: Create the isolated package/test frame only after review approval.

- [ ] T002 Create package directory `src/playwright_auto/hcuw_runtime/` with `__init__.py` for runtime-v2 namespace isolation
- [ ] T003 Create test directory `tests/hcuw_runtime/` for focused runtime-v2 tests
- [ ] T004 [P] Create `src/playwright_auto/hcuw_runtime/config.py` owner group `RuntimeConfig` skeleton with no side effects
- [ ] T005 [P] Create `src/playwright_auto/hcuw_runtime/state.py` owner group `RuntimeStateStore` skeleton with `.runtime-v2` as the only writable state root
- [ ] T006 [P] Create `src/playwright_auto/hcuw_runtime/endpoint_inventory.py` owner group `EndpointInventory` skeleton in read-only mode
- [ ] T007 [P] Create `src/playwright_auto/hcuw_runtime/api_server.py` owner group `RuntimeApiServer` skeleton for API 9227
- [ ] T008 [P] Create `src/playwright_auto/hcuw_runtime/dashboard_server.py` owner group `DashboardServer` skeleton for dashboard 9226
- [ ] T009 Create `src/playwright_auto/hcuw_runtime/cli.py` entrypoint skeleton that wires owner groups without starting hidden background behavior

---

## Phase 2: Foundational Ownership and Function Map

**Purpose**: Establish owner boundaries before user-story behavior.

- [ ] T010 Update `specs/001-hcuw-runtime-v2/function-map-template.md` with actual implemented symbols before merging any owner group code
- [ ] T011 [P] Add `tests/hcuw_runtime/test_config.py` for `RuntimeConfig` validation: API port `9227`, dashboard port `9226`, legacy port `9225`, and state root `.runtime-v2`
- [ ] T012 [P] Add `tests/hcuw_runtime/test_state_store.py` for `RuntimeStateStore` ownership: missing runs returns empty, malformed runs returns typed error, writes stay under `.runtime-v2`
- [ ] T013 [P] Add `tests/hcuw_runtime/test_endpoint_inventory.py` proving endpoint inventory is read-only and does not request tab ownership or prompt sending
- [ ] T014 [P] Add `tests/hcuw_runtime/test_api_server.py` for JSON envelope shape and route ownership
- [ ] T015 [P] Add `tests/hcuw_runtime/test_dashboard_server.py` for static dashboard and `/api/*` proxy unavailable behavior
- [ ] T016 Implement `RuntimeConfig` in `src/playwright_auto/hcuw_runtime/config.py` with explicit validation helpers named by rule
- [ ] T017 Implement `RuntimeStateStore` in `src/playwright_auto/hcuw_runtime/state.py` with separate file IO helpers and JSON validation helpers
- [ ] T018 Implement `EndpointInventory` in `src/playwright_auto/hcuw_runtime/endpoint_inventory.py` with read-only config loading separated from bounded reachability probing

**Checkpoint**: Foundation ready. No API/dashboard server behavior beyond owner construction should be needed before this phase passes.

---

## Phase 3: User Story 1 - Isolated API Frame on 9227 (Priority: P1) MVP

**Goal**: Start API 9227 beside legacy 9225 and expose health/config/runs/endpoints without legacy state mutation.

**Independent Test**: `uv run pytest tests/hcuw_runtime/test_api_server.py tests/hcuw_runtime/test_config.py tests/hcuw_runtime/test_state_store.py -q`

### Tests for User Story 1

- [ ] T019 [P] [US1] Add failing test in `tests/hcuw_runtime/test_api_server.py` for `GET /api/health` returning API port `9227`, dashboard port `9226`, legacy port `9225`, and `.runtime-v2`
- [ ] T020 [P] [US1] Add failing test in `tests/hcuw_runtime/test_api_server.py` for `GET /api/config` returning the config envelope from `RuntimeConfig`
- [ ] T021 [P] [US1] Add failing test in `tests/hcuw_runtime/test_api_server.py` for `GET /api/runs` reading only `RuntimeStateStore`
- [ ] T022 [P] [US1] Add failing test in `tests/hcuw_runtime/test_api_server.py` for port conflict producing `port_in_use` without fallback to 9225

### Implementation for User Story 1

- [ ] T023 [US1] Implement `RuntimeApiServer` routing in `src/playwright_auto/hcuw_runtime/api_server.py` for `/api/health`, `/api/config`, `/api/runs`, and `/api/endpoints`
- [ ] T024 [US1] Implement JSON response envelope helper in `src/playwright_auto/hcuw_runtime/api_server.py` owned by `RuntimeApiServer`, without moving domain decisions into the helper
- [ ] T025 [US1] Implement `start-api` command in `src/playwright_auto/hcuw_runtime/cli.py` using explicit top-down construction: config -> store -> endpoint inventory -> API server
- [ ] T026 [US1] Verify `uv run pytest tests/hcuw_runtime/test_api_server.py tests/hcuw_runtime/test_config.py tests/hcuw_runtime/test_state_store.py -q`

**Checkpoint**: API MVP is independently functional.

---

## Phase 4: User Story 2 - Minimal Dashboard Web on 9226 (Priority: P2)

**Goal**: Serve a simple read-only web dashboard on 9226 that calls `/api/*` and proxies to API 9227.

**Independent Test**: `uv run pytest tests/hcuw_runtime/test_dashboard_server.py -q`

### Tests for User Story 2

- [ ] T027 [P] [US2] Add failing test in `tests/hcuw_runtime/test_dashboard_server.py` for dashboard `/` returning HTML with API status, config, runs, endpoints, diagnostics, and refresh UI placeholders
- [ ] T028 [P] [US2] Add failing test in `tests/hcuw_runtime/test_dashboard_server.py` for `/api/health` proxying to `127.0.0.1:9227/api/health`
- [ ] T029 [P] [US2] Add failing test in `tests/hcuw_runtime/test_dashboard_server.py` for API unavailable display/proxy error without tight retry behavior
- [ ] T030 [P] [US2] Add failing test in `tests/hcuw_runtime/test_dashboard_server.py` proving dashboard routes do not create runs or write state

### Implementation for User Story 2

- [ ] T031 [US2] Implement `DashboardServer.serve_index()` in `src/playwright_auto/hcuw_runtime/dashboard_server.py` with minimal readable HTML/CSS/JS, no framework toolchain
- [ ] T032 [US2] Implement `DashboardServer.proxy_api_request()` in `src/playwright_auto/hcuw_runtime/dashboard_server.py` as the only owner of dashboard proxy availability handling
- [ ] T033 [US2] Implement `start-dashboard` command in `src/playwright_auto/hcuw_runtime/cli.py` using explicit config loading and dashboard server construction
- [ ] T034 [US2] Verify `uv run pytest tests/hcuw_runtime/test_dashboard_server.py -q`

**Checkpoint**: Dashboard is read-only and calls API through `/api/*`.

---

## Phase 5: User Story 3 - Read-Only CDP Endpoint Inventory (Priority: P3)

**Goal**: Display existing endpoint availability without claiming browser ownership.

**Independent Test**: `uv run pytest tests/hcuw_runtime/test_endpoint_inventory.py -q`

### Tests for User Story 3

- [ ] T035 [P] [US3] Add failing test in `tests/hcuw_runtime/test_endpoint_inventory.py` for loading endpoint records from configured source without modifying that source
- [ ] T036 [P] [US3] Add failing test in `tests/hcuw_runtime/test_endpoint_inventory.py` for offline endpoint item diagnostic without whole-response failure
- [ ] T037 [P] [US3] Add failing test in `tests/hcuw_runtime/test_endpoint_inventory.py` proving no browser tab list, no page marker write, and no send action is called

### Implementation for User Story 3

- [ ] T038 [US3] Implement endpoint config read adapter in `src/playwright_auto/hcuw_runtime/endpoint_inventory.py` with clearly named read-only boundary
- [ ] T039 [US3] Implement bounded reachability summary in `src/playwright_auto/hcuw_runtime/endpoint_inventory.py` with timeout owned by `EndpointInventory`
- [ ] T040 [US3] Connect `/api/endpoints` in `src/playwright_auto/hcuw_runtime/api_server.py` to `EndpointInventory` result without recomputing endpoint conditions in API server
- [ ] T041 [US3] Verify `uv run pytest tests/hcuw_runtime/test_endpoint_inventory.py tests/hcuw_runtime/test_api_server.py -q`

**Checkpoint**: Endpoint inventory is visible and read-only.

---

## Phase 6: User Story 4 - Function Ownership Map (Priority: P4)

**Goal**: Make owner groups and function boundaries reviewable so bugs can be localized quickly.

**Independent Test**: Review function-map entries and ensure each implemented symbol has one owner, one state authority, and explicit forbidden responsibilities.

- [ ] T042 [P] [US4] Fill actual function map entries in `specs/001-hcuw-runtime-v2/function-map-template.md` for every new public class/function implemented under `src/playwright_auto/hcuw_runtime/`
- [ ] T043 [P] [US4] Review `src/playwright_auto/hcuw_runtime/config.py` against canonical `HCU-CODE-001`/`HCU-CODE-002` and the feature owner map in `contracts/code-organization-contract.md`
- [ ] T044 [P] [US4] Review `src/playwright_auto/hcuw_runtime/state.py` against canonical `HCU-ARCH-002` and the feature owner map for single state ownership
- [ ] T045 [P] [US4] Review `src/playwright_auto/hcuw_runtime/api_server.py` and `dashboard_server.py` against `HCU-ARCH-002` for no duplicate condition decisions
- [ ] T046 [US4] Document any approved deviation in `specs/001-hcuw-runtime-v2/plan.md` Complexity Tracking before merging implementation

---

## Final Phase: Validation and Review

- [ ] T047 Run `uv run pytest tests/hcuw_runtime -q`
- [ ] T048 Run `git diff --check`
- [ ] T049 Review `git diff -- src/playwright_auto/hcuw_runtime tests/hcuw_runtime specs/001-hcuw-runtime-v2`
- [ ] T050 Confirm legacy 9225 files/process config were not modified by runtime-v2 implementation
- [ ] T051 Update `specs/001-hcuw-runtime-v2/quickstart.md` only if implementation commands differ from the approved plan

---

## Dependencies & Execution Order

### Phase Dependencies

- Phase 0 blocks all source implementation.
- Phase 1 starts only after operator approval.
- Phase 2 depends on Phase 1.
- User Story phases depend on Phase 2.
- User Story 1 is MVP and should complete before dashboard/endpoints are treated as usable.
- User Story 2 can start after API route contracts exist.
- User Story 3 can start after `EndpointInventory` skeleton exists.
- User Story 4 runs alongside implementation and must complete before final review.

### Parallel Opportunities

- Setup skeleton files T004-T008 can be created in parallel after approval.
- Test files T011-T015 can be drafted in parallel because they target different owners.
- US1 tests T019-T022 can run in parallel.
- US2 tests T027-T030 can run in parallel.
- US3 tests T035-T037 can run in parallel.
- US4 review tasks T042-T045 can run in parallel after implementation exists.

## MVP Scope

MVP after approval is:

```text
T002-T026
```

This creates only API 9227 and supporting config/state owners. Dashboard 9226 is the next increment.

## Non-Goals for This Task List

- No prompt sending.
- No DOM snapshot extraction.
- No MutationObserver implementation.
- No LISTEN/network response parsing.
- No send/bind/route authority.
- No route repair.
- No worker orchestration.
- No legacy 9225 refactor.

## Stop Rule

Do not start T002 or any source implementation until the operator approves this review package.
