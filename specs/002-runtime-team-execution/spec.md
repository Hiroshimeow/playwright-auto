# Feature Specification: Runtime Team Execution

**Feature Branch**: `002-runtime-team-execution`

**Created**: 2026-09-30

**Status**: Draft

**Input**: User description: "Runtime-v2 must become the minimal operator-facing execution surface for real CDPA Team Tasks before any larger implementation. It must submit only Team Tasks, never Solo Task or Independent Agent work, allow the operator to choose any starting role from the selected team roles, support teams without PLAN when REVIEW is available as the closing role, observe local-chatgpt accepted-send races without replaying Send, and expose enough audit/task state on 9226/9227 for operators and later agents to verify progress. Stop before implementation; produce spec, plan, contracts, quickstart, and tasks for a DEV agent."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Submit a real Team Task from runtime-v2 (Priority: P1)

An operator submits a real CDPA Team Task through runtime-v2 using only Team Task semantics. The operator can select the team name, browser endpoint, starting role, and role set. Runtime-v2 rejects Solo Task and Independent Agent fields instead of translating or silently accepting them.

**Why this priority**: This is the minimum usable execution path. Without it, 9227 remains a passive dashboard/API and cannot replace legacy task creation.

**Independent Test**: Submit a task with roles `DEV,REVIEW` and `start_role=DEV`; verify the task appears in the legacy board with exactly DEV and REVIEW roles, starts at DEV, and no independent-agent or solo task is created.

**Acceptance Scenarios**:

1. **Given** runtime-v2 API is running and legacy CDPA worker can be started or is already healthy, **When** the operator submits a task with `roles=DEV,REVIEW` and `start_role=DEV`, **Then** the created task contains only DEV and REVIEW, starts with DEV, and is tracked in runtime-v2 task history.
2. **Given** a request contains `workflow_mode=solo`, `solo_role`, `role`, or `conversation_url`, **When** runtime-v2 validates the request, **Then** it fails closed with a Team Task-only error and does not enqueue legacy work.
3. **Given** a task role set excludes PLAN but includes REVIEW, **When** the team task completes, **Then** REVIEW is the closing role allowed to route DONE.

---

### User Story 2 - Preserve exactly-once Send through local-chatgpt races (Priority: P2)

A CDPA worker observes a newly accepted ChatGPT message while the page still has a provisional `local-chatgpt:` URL. The worker keeps observing the exact owned page until canonical identity is proven, rather than blocking immediately or replaying the Send.

**Why this priority**: Cloudflare/network timing and ChatGPT save latency can otherwise turn successful sends into permanent blocks. Replaying would duplicate side effects and violate exactly-once execution.

**Independent Test**: Force or simulate a send acceptance on a `local-chatgpt:` page with the exact accepted user message visible; verify the task does not enter `accepted_conversation_identity_unresolved` immediately and later canonicalizes when the page URL becomes `/c/<canonical-id>`.

**Acceptance Scenarios**:

1. **Given** a hop has accepted Send, the receipt has the accepted user message id, and the exact owned page still shows that user turn, **When** the page URL is still provisional, **Then** the worker keeps the hop in waiting/observation state without reopening or resending.
2. **Given** the same hop later resolves to a canonical saved conversation URL, **When** the worker observes the exact owned page, **Then** the receipt and ledger are enriched with the canonical conversation id.
3. **Given** the exact page is gone or the accepted user turn is absent, **When** the worker cannot prove ownership and user provenance, **Then** it blocks safely without replaying Send and exposes an operator-safe reconciliation path.

---

### User Story 3 - Verify progress from runtime-v2 audit surfaces (Priority: P3)

An operator or later agent checks 9226/9227 to determine whether a runtime-v2-submitted task is queued, running, blocked, or done, and whether DOM/LISTEN/action sources are active enough to explain the current state.

**Why this priority**: Operators need enough visibility to decide whether to resume, route reconciliation, or hand off to a DEV agent. The audit surface must not become another hidden execution owner.

**Independent Test**: After submitting a team task through 9227, inspect `/api/tasks`, `/api/audit/snapshot`, and the 9226 dashboard; verify they show the runtime task id, legacy task id, selected roles, start role, current board state, and recent action/DOM/LISTEN status without writing legacy state.

**Acceptance Scenarios**:

1. **Given** runtime-v2 created a task, **When** the operator opens `/api/tasks`, **Then** the response includes runtime task id, legacy task id, team, role set, start role, browser endpoint, and latest known status.
2. **Given** a task is blocked, **When** the operator views 9226, **Then** the dashboard distinguishes old blocked tasks from the current verification task and shows the block code and updated time.
3. **Given** DOM/LISTEN/audit sources are active, **When** the operator views the audit snapshot, **Then** each source has a status, message, update time, and bounded recent events without requiring a manual refresh button.

---

### Edge Cases

- Legacy 9225/9224 is not running when runtime-v2 submits a task: runtime-v2 may start the legacy runtime using a bridge config, but the bridge must disable builtin independent-agent seeding and must not mutate `cdpa.yaml`.
- The selected roles do not include PLAN or REVIEW: the request must be rejected because no team closing role exists.
- `start_role` is not included in the selected roles: the request must be rejected or normalized only when it remains unambiguous; silent fallback to PLAN is not allowed.
- Existing legacy board contains old independent-agent tasks or old blocked smoke tasks: runtime-v2 audit must report them clearly but must not treat them as newly spawned runtime-v2 verification work.
- ChatGPT accepts Send on a provisional URL but never saves the conversation: the worker must eventually surface a safe block/reconciliation path and must not resend.
- Operator asks for Spec Kit work before implementation: runtime-v2 feature documents and tasks may be generated, but source implementation must wait for explicit DEV authorization.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Runtime-v2 MUST accept real task submissions only as Team Tasks.
- **FR-002**: Runtime-v2 MUST reject Solo Task and Independent Agent fields, including `workflow_mode=solo`, `solo_role`, `role`, and `conversation_url`.
- **FR-003**: Runtime-v2 MUST allow the operator to provide a role set and an explicit `start_role` for a Team Task.
- **FR-004**: Runtime-v2 MUST reject a Team Task if `start_role` is not one of the selected roles.
- **FR-005**: Runtime-v2 MUST reject a Team Task if the selected roles do not include PLAN or REVIEW as a closing role.
- **FR-006**: Team Task creation MUST start from the first selected role in canonical workflow order, not from a hardcoded PLAN default.
- **FR-007**: DONE routing MUST be owned by PLAN when PLAN exists in the selected team; otherwise REVIEW MUST be the closing role when REVIEW exists.
- **FR-008**: Runtime-v2 MUST use a legacy bridge config that adds the current repository to allowed roots without changing `cdpa.yaml`.
- **FR-009**: The legacy bridge config MUST disable builtin independent-agent seeding for runtime-v2-submitted work.
- **FR-010**: Accepted Send on a provisional `local-chatgpt:` page MUST keep observing the exact owned page when the accepted user turn is visible.
- **FR-011**: Accepted Send handling MUST NOT replay, resend, restart role, or create a new chat after the accepted-user boundary has been crossed.
- **FR-012**: If canonical conversation identity cannot be proven because the exact page or accepted user turn is missing, the system MUST block safely and expose a reconciliation route that uses existing reports and ledger evidence.
- **FR-013**: Runtime-v2 `/api/tasks` MUST expose runtime task records with team, role set, start role, legacy command id, legacy task id, browser endpoint, status, and timestamps.
- **FR-014**: Runtime-v2 `/api/audit/snapshot` MUST expose bounded source states for actions, DOM, mutation, and LISTEN without mutating legacy task state.
- **FR-015**: Runtime-v2 dashboard 9226 MUST lazily refresh only while the browser tab is visible and focused, with a 0.5-second polling interval during focus.
- **FR-016**: Runtime-v2 MUST distinguish current verification tasks from old legacy blocked tasks in operator-visible status.
- **FR-017**: All validation failures MUST fail closed with an explicit error code/message and no legacy task enqueue.

### Key Entities *(include if feature involves data)*

- **Runtime Task Submission**: Operator request containing task text, team, browser endpoint, selected roles, and start role.
- **Runtime Task Record**: Runtime-v2 persisted record linking a runtime task id to legacy task id, command id, selected roles, start role, status, and submit evidence.
- **Workflow Role Set**: Ordered role list selected for a Team Task; determines start role eligibility and closing role.
- **Closing Role**: Role authorized to route DONE; PLAN if selected, otherwise REVIEW.
- **Accepted Send Evidence**: Receipt, request ledger record, accepted user message id, page binding, and observed conversation identity.
- **Audit Source State**: Bounded runtime-v2 view of actions, DOM, mutation, and LISTEN sources for operator inspection.
- **Legacy Bridge Config**: Runtime-generated config copy used to start or talk to legacy CDPA safely without mutating `cdpa.yaml`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A `DEV,REVIEW` Team Task submitted through runtime-v2 starts at DEV and reaches DONE through REVIEW in a live smoke run without requiring PLAN.
- **SC-002**: Submitting any Solo Task or Independent Agent field through runtime-v2 returns a validation error and creates zero legacy commands.
- **SC-003**: A provisional `local-chatgpt:` accepted-send smoke run remains RUNNING/WAITING through the initial save window and later records a canonical `/c/<id>` without duplicate Send events.
- **SC-004**: Old blocked tasks remain visible but are not counted as failures of the latest verification task when `/api/tasks` and 9226 are inspected.
- **SC-005**: Runtime-v2 focused test suite and the role/accepted-send focused legacy test subset pass before a DEV agent marks implementation complete.
- **SC-006**: Operators can determine current task state, selected roles, start role, block code, and last update time from 9226/9227 in under 10 seconds.

## Assumptions

- Existing CDPA legacy worker, dashboard API, and task store remain the execution backend for this feature.
- Runtime-v2 remains a minimal orchestration/audit surface and does not directly own browser Send, route parsing, or legacy state mutation.
- The browser endpoint id `9222` may be used for smoke validation unless the operator specifies another endpoint.
- The current repository root is allowed through a runtime-generated bridge config rather than by editing the main `cdpa.yaml`.
- Independent-agent and Solo Task removal is incremental: this feature prevents runtime-v2 from creating them, but old legacy instances may still appear until separately cleaned.
- A separate DEV agent will perform implementation after this Spec Kit phase is reviewed.
