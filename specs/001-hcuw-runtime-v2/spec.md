# Feature Specification: HCUW Runtime V2 Skeleton

**Feature Branch**: `refactor/hcuw-runtime-v2`

**Created**: 2026-09-30

**Status**: Draft - awaiting final review before implementation

**Input**: Build the minimal runtime-v2 frame for the CDPA/HCUW refactor in `C:\Users\DuongNH66\Desktop\git\cdpa`. Keep legacy CDPA running on 9225, create a test API on 9227, create a minimal dashboard on 9226, reuse online CDP endpoints read-only, and keep code readable by grouping functions by clear owner/class responsibility.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Isolated API frame on 9227 (Priority: P1)

As the operator, I need a new runtime-v2 API on port 9227 that can be started beside the legacy 9225 server, report health/config/state paths, and persist only runtime-v2 state so I can develop the refactor without risking the current CDPA runtime.

**Why this priority**: This is the safety boundary for all later work. No dashboard or endpoint probing should exist before the new API is isolated from 9225 and legacy state.

**Independent Test**: Start only the runtime-v2 API, call its health/config endpoints, verify it writes only to `.runtime-v2`, and verify no legacy 9225 process, state path, or endpoint ownership is changed.

**Acceptance Scenarios**:

1. **Given** legacy CDPA may still be running on 9225, **When** runtime-v2 API starts, **Then** it listens on 9227 and uses a distinct process name, config namespace, and state path.
2. **Given** runtime-v2 API is running, **When** `/api/health` and `/api/config` are requested, **Then** both return deterministic JSON without connecting to ChatGPT or mutating browser state.
3. **Given** runtime-v2 API has no runs, **When** `/api/runs` is requested, **Then** it returns an empty list from `.runtime-v2`, not legacy `.runtime`.

---

### User Story 2 - Minimal dashboard web on 9226 (Priority: P2)

As the operator, I need a basic dashboard web UI on port 9226 that calls the runtime-v2 API through `/api/*`, displays current health/config/runs/endpoints, and remains read-only at first so I can observe changes while building.

**Why this priority**: The user wants to see and adjust the new frame while it is being built, but the first dashboard must not add control buttons or duplicate worker authority.

**Independent Test**: Start the dashboard server with the API server, open `http://127.0.0.1:9226`, and verify the page fetches `/api/health`, `/api/config`, `/api/runs`, and `/api/endpoints` through the dashboard `/api/*` proxy to 9227.

**Acceptance Scenarios**:

1. **Given** API 9227 is healthy, **When** dashboard 9226 loads, **Then** it shows API status, runtime state path, configured ports, runs table, endpoints table, and last refresh time.
2. **Given** API 9227 is down or unreachable, **When** dashboard 9226 loads, **Then** it shows an explicit API unavailable state without crashing or retry-spamming.
3. **Given** the dashboard is loaded, **When** the operator clicks refresh, **Then** it refreshes displayed state once and does not create runs, send prompts, claim tabs, or mutate state.

---

### User Story 3 - Read-only CDP endpoint inventory (Priority: P3)

As the operator, I need runtime-v2 to reuse the same online CDP endpoint registry in read-only mode and display endpoint availability so I can plan later send/bind work without competing with legacy CDPA.

**Why this priority**: Endpoint visibility is useful early, but endpoint control must remain off until ownership and send authority are redesigned.

**Independent Test**: With existing CDP endpoints online, call `/api/endpoints`; verify endpoint records are read and displayed, but no tab is adopted, no conversation is resumed, and no prompt is sent.

**Acceptance Scenarios**:

1. **Given** legacy endpoint config is present, **When** runtime-v2 endpoint inventory runs, **Then** it reports endpoint IDs, labels, ports, reachability, and source config path without mutating the registry.
2. **Given** an endpoint is offline, **When** `/api/endpoints` is requested, **Then** it reports the endpoint as unavailable with a reason instead of blocking API response.
3. **Given** endpoint inventory completes, **When** logs/state are inspected, **Then** no browser tab ownership marker or ChatGPT send action is created.

---

### User Story 4 - Function ownership map before implementation (Priority: P4)

As a maintainer, I need every planned runtime-v2 function to have a named owner group/class, input, output, side-effect boundary, and failure responsibility before implementation so future debugging can isolate faults quickly.

**Why this priority**: The current code degraded because one state can be managed by many gates/functions. The refactor must make ownership obvious before code exists.

**Independent Test**: Review `function-map-template.md` plus the planned task list and confirm each implementation task names the owning module/class group, the state it may manage, and what it must not manage.

**Acceptance Scenarios**:

1. **Given** a planned function belongs to config, state, API, dashboard, endpoint inventory, or observation contracts, **When** the task is reviewed, **Then** exactly one owner group/class is named.
2. **Given** a condition or runtime status exists, **When** ownership is reviewed, **Then** one owner is responsible for deciding it and other functions consume the decision instead of duplicating the condition.
3. **Given** implementation has not started, **When** the maintainer reviews the spec, **Then** no source files under `src/` have been created or modified by this planning step.

## Edge Cases

- API 9227 port is already in use: startup must fail with a clear message and must not fall back to 9225 or another port silently.
- Dashboard 9226 port is already in use: startup must fail clearly and must not replace the legacy dashboard.
- API 9227 is unavailable while dashboard 9226 is running: dashboard must show a single clear unavailable state and avoid repeated tight-loop polling.
- Endpoint registry file is missing or malformed: `/api/endpoints` must return a structured error item while `/api/health` remains healthy.
- The legacy runtime is running and has active tasks: runtime-v2 must not read/modify legacy durable task state beyond read-only endpoint configuration needed for inventory.
- Manual or future send/bind logic is out of scope for this skeleton; no prompt submission is allowed in this feature.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a runtime-v2 API server that defaults to `127.0.0.1:9227` and exposes `/api/health`, `/api/config`, `/api/runs`, and `/api/endpoints`.
- **FR-002**: The system MUST provide a minimal read-only dashboard server that defaults to `127.0.0.1:9226` and serves a basic web page.
- **FR-003**: The dashboard MUST call runtime-v2 API data through `/api/*`; the dashboard server MAY proxy `/api/*` to `127.0.0.1:9227/api/*` so the browser UI stays simple and same-origin.
- **FR-004**: Runtime-v2 MUST use `.runtime-v2` for its state, logs, and run records and MUST NOT share writable state with the legacy 9225 runtime.
- **FR-005**: Endpoint inventory MUST be read-only and MUST NOT adopt tabs, resume conversations, send prompts, click buttons, or write page ownership markers.
- **FR-006**: Runtime-v2 MUST expose enough data for the dashboard to show API status, configured ports, state path, version/branch/head when available, run summaries, endpoint summaries, and latest diagnostic messages.
- **FR-007**: Feature implementation MUST comply with `HCU-CODE-001`; the feature owner map MUST name the responsible owner for config, state, API, dashboard, endpoint inventory, and observation-contract work.
- **FR-008**: Feature implementation MUST comply with `HCU-ARCH-002`; concrete owner assignments MUST be recorded in `contracts/code-organization-contract.md` instead of redefining the project rule here.
- **FR-009**: Feature implementation MUST comply with `HCU-CODE-001` for readable control flow and explicit boundaries.
- **FR-010**: Feature implementation MUST comply with `HCU-CODE-001` and `HCU-CODE-002`; planned public functions MUST be traceable through the function map to their owner and smaller helpers.
- **FR-011**: Planning artifacts MUST define the implementation boundary and tasks before source implementation starts.
- **FR-012**: This feature MUST stop before source implementation until the operator gives final approval.

### Key Entities

- **RuntimeConfig**: The resolved runtime-v2 configuration: API port, dashboard port, bind host, state root, endpoint config source, legacy isolation rules, refresh policy.
- **RuntimeStateStore**: The single owner of `.runtime-v2` reads/writes for runs, diagnostics, and runtime metadata.
- **RuntimeApiServer**: The owner of HTTP API routing, JSON response shaping, and API error envelopes for port 9227.
- **DashboardServer**: The owner of dashboard static web serving and `/api/*` proxy behavior on port 9226.
- **EndpointInventory**: The owner of read-only endpoint registry loading and endpoint availability summaries.
- **ObservationSourceContract**: The documented authority rules for DOM, WaitProbe, MutationObserver, LISTEN, and backend stream status; not fully implemented in this skeleton.
- **FunctionMapEntry**: The planning record for each new function/class group: owner, purpose, input, output, state authority, side effects, and failure reasons.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reviewer can start the planned API and dashboard design without any required change to legacy 9225 configuration.
- **SC-002**: Dashboard load requires at most one initial API refresh and one manual refresh action; no automatic retry loop faster than 10 seconds is allowed in the design.
- **SC-003**: Every implementation task names the target file and owner group/class before code is written.
- **SC-004**: Every runtime condition introduced by the skeleton has exactly one owner in the plan, data model, or contracts.
- **SC-005**: No task in this feature authorizes prompt sending, tab adoption, route parsing, route repair, or response binding implementation.
- **SC-006**: Planning validation shows no source files under `src/` were modified by the pre-implementation review step.

## Assumptions

- The first implementation will use Python 3.11+ and repository-native pytest.
- The minimal web/dashboard may use Python standard library HTTP primitives to avoid adding dependencies unless implementation evidence justifies a dependency later.
- Legacy endpoint configuration can be read by runtime-v2 for inventory, but runtime-v2 must keep its writable state separate.
- The dashboard is local operator tooling, not a public web application.
- `9227` and `9226` are reserved for runtime-v2 during development; if occupied, startup fails rather than silently selecting another port.
- Final implementation approval is required after this spec/plan/tasks review.
