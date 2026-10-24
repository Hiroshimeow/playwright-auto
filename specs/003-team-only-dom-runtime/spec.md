# Feature Specification: Team-only DOM Runtime

**Feature Branch**: 003-team-only-dom-runtime

**Created**: 2026-09-30

**Status**: Draft

**Input**: User description: "Runtime v2 must use one unified TeamTask model and DOM-only observation. Remove public Solo Task and Independent Agent execution concepts from the new runtime. A one-role team is the replacement for Solo. Cron and heartbeat create ordinary TeamTasks for monitoring and recovery. Route schemas must include the semantic repair destination; REVIEW must route DEV when it finds reproducible defects, and must never be forced to reroute to REVIEW just because DEV was omitted from the schema. For ChatGPT Web/browser-backed providers, DOM is the only observation source of truth, following the codex-chatgpt-web style: project transcript/state from DOM, not LISTEN or MutationObserver pipelines. Non-browser providers use ProviderResult/MaterialRef."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run every workflow as a TeamTask (Priority: P1)

An operator creates work using exactly one public task model: TeamTask. The team may contain one role or many roles. A one-role TeamTask replaces legacy Solo Task. The role name describes the execution slot for that task, not a hard-coded job category.

**Why this priority**: One task model removes routing ambiguity, makes dashboards easier to read, and prevents old Solo/Independent paths from bypassing new runtime rules.

**Independent Test**: Submit TeamTasks with `roles=["DEV"]`, `roles=["REVIEW"]`, and `roles=["DEV","TEST"]`; verify each runs without any Solo Task or Independent Agent projection and can close through its configured `closing_role`.

**Acceptance Scenarios**:

1. **Given** an operator submits a TeamTask with one role DEV, **When** runtime-v2 validates it, **Then** the task starts as DEV, has closing_role=DEV unless explicitly overridden, and no Solo fields or Solo mode are stored.
2. **Given** an operator submits a TeamTask with roles DEV,TEST and closing_role=TEST, **When** DEV routes to TEST and TEST accepts the work, **Then** TEST may route DONE.
3. **Given** a request uses workflow_mode=solo, solo_role, task_mode=independent, or independent-agent fields, **When** runtime-v2 receives it, **Then** the request is rejected with a migration message pointing to one-role TeamTask or CronJob.

---

### User Story 2 - Prevent route-schema self-reroute loops (Priority: P1)

A role that finds work for another role must be able to route there without hard-coded role-name branches. Route schemas must be generated from the task's generic RolePolicy and semantic route data, not from the current role name alone.

**Why this priority**: The observed REVIEW loop happened because prose required a DEV handoff while the JSON schema omitted DEV. The only remaining non-terminal route was REVIEW, causing repeated self-reroute.

**Independent Test**: Submit a one-role TeamTask with `roles=["REVIEW"]` where REVIEW's semantic route data includes `defect -> DEV`. Verify RolePolicy computes `reachable_roles=["REVIEW","DEV"]`, the REVIEW schema includes DEV, routing to DEV lazy-materializes a DEV RoleSlot in the same TeamTask, and DEV later hands control back to the configured closing role. Repeat with generic roles A -> B to prove no REVIEW/DEV special case exists.

**Acceptance Scenarios**:

1. **Given** a role's semantic route data names a valid destination not present in the submitted `roles`, **When** the TeamTask is created, **Then** RolePolicy includes that destination in immutable `reachable_roles` and the route schema may use it.
2. **Given** a role's semantic route data does not name DEV, **When** the prompt is rendered, **Then** it must not invent or mention DEV merely because the current role is REVIEW.
3. **Given** a route first reaches a declared but not yet materialized role, **When** the route is durably recorded, **Then** that RoleSlot is materialized idempotently in the same TeamTask before any Send intent for the new role.
4. **Given** self-routing is not explicitly bounded, **When** a route schema is generated, **Then** the current role is excluded from its own allowed routes.
5. **Given** `route_seq >= max_routes`, **When** runtime prepares the next role turn, **Then** a non-closing role blocks before Send with `ROUTE_BUDGET_EXHAUSTED`, while the closing role receives a route enum containing only `DONE`; runtime never preserves a pending non-terminal route across this block.
6. **Given** a role returns malformed JSON or a route outside the closed allowed-route enum, **When** route validation fails, **Then** the existing route-repair path may issue at most the configured bounded repair attempts; each repair Send uses the normal exactly-once send owner, and exhaustion blocks with `ROUTE_VALIDATION_EXHAUSTED` rather than silently widening routes or self-rerouting.

---

### User Story 3 - Observe ChatGPT through DOM only (Priority: P2)

The runtime observes ChatGPT state using DOM-derived projection only. DOM is the source of truth for transcript text, user-turn acceptance, assistant completion, composer state, stop/retry/error state, and canonical conversation identity. LISTEN/network and MutationObserver pipelines are not used as competing source owners.

**Why this priority**: The target design follows the codex-chatgpt-web direction: simple DOM projection is easier to audit than multiple competing observation paths.

**Independent Test**: Inspect runtime-v2 source and dashboard contracts; verify no route/send/complete decision depends on LISTEN or MutationObserver data, and dashboard labels do not advertise unattached LISTEN/Mutation sources.

**Acceptance Scenarios**:

1. **Given** ChatGPT accepts a prompt while the URL is provisional local-chatgpt, **When** DOM still shows the exact accepted user turn, **Then** runtime keeps observing DOM without replaying Send.
2. **Given** a network error is visible only in a network/LISTEN layer and not in DOM, **When** runtime evaluates the task, **Then** it does not route/block solely from that network signal.
3. **Given** dashboard 9226 shows observation state, **When** the page renders, **Then** it presents DOM projection status and does not present LISTEN/Mutation as runtime sources.

---

### User Story 4 - Replace Independent Agent with CronJob and Heartbeat (Priority: P2)

Operational monitoring and recovery are deterministic services. CronJob schedules and heartbeat stale checks create ordinary TeamTasks instead of running Independent Agent conversations.

**Why this priority**: Independent Agent created a second execution model and continued firing INDEPENDENT_AGENT_TRIGGER_CONTEXT even after runtime-v2 stopped creating independent work.

**Independent Test**: Disable or remove Independent Agent activation, create a CronJob that emits a single-role TeamTask, and verify stale heartbeat recovery also emits a TeamTask with an idempotency key.

**Acceptance Scenarios**:

1. **Given** a recurring monitor schedule is enabled, **When** the cron time arrives, **Then** the scheduler creates or reuses one TeamTask using a deterministic idempotency key.
2. **Given** a worker heartbeat becomes stale, **When** the stale threshold is exceeded, **Then** recovery creates a TeamTask rather than an Independent Agent trigger.
3. **Given** legacy independent agent records exist, **When** runtime-v2 starts, **Then** they remain disabled/hidden or are migrated; they must not create new prompts.

---

### Edge Cases

- Existing STOPPED or PAUSED Independent Agent records must not wake after reload.
- A one-role team must not be forced to include PLAN or REVIEW.
- A role may be named DEV, REVIEW, TEST, AUDIT, MONITOR, RECOVERY, or a custom route key if it passes route-key validation.
- A role may perform multiple duties when the task prompt says so.
- Route schemas must fail closed before send if prose instructions and allowed routes conflict.
- DOM projection must handle provisional local-chatgpt URLs without replaying accepted sends.
- CronJob idempotency must prevent duplicate task creation after reload.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Runtime-v2 MUST expose TeamTask as the only public task creation model.
- **FR-002**: Runtime-v2 MUST accept a TeamTask with any non-empty roles array after role-key validation; PLAN and REVIEW MUST NOT be required.
- **FR-003**: Runtime-v2 MUST support one-role TeamTasks and treat them as the replacement for legacy Solo Task.
- **FR-004**: Runtime-v2 MUST reject public Solo Task and Independent Agent creation fields with clear migration guidance.
- **FR-005**: TeamTask MUST include or derive start_role from the configured roles; start_role MUST be one of the roles.
- **FR-006**: TeamTask MUST include or derive closing_role; closing_role MUST be one of the roles.
- **FR-007**: DONE permission MUST be based on closing_role, not hard-coded PLAN or REVIEW.
- **FR-008**: Prompt prose and route JSON schema MUST be generated from the same workflow contract and MUST NOT conflict.
- **FR-009**: Every semantic destination named in rendered prompt prose MUST be present in that turn's closed route enum; prose/schema divergence or an enum value outside the frozen reachable set MUST block before Send with `ROUTE_CONTRACT_INVALID`.
- **FR-010**: Self-routing MUST be forbidden by default and may be included only when `self_route_policy=bounded(max_self_routes=N)` still has budget. Every self-route increments both `route_seq` and the current role's cumulative task-lifetime self-route count.
- **FR-011**: For `provider_id=chatgpt_web` and other browser-backed providers, DOM projection MUST be the only source of truth for send acceptance, transcript state, completion, and visible error state. Non-browser providers report through ProviderResult and MaterialRef.
- **FR-012**: Browser-provider dashboard views MUST remove or hide LISTEN and MutationObserver as decision sources unless clearly marked as deprecated diagnostics that cannot affect decisions.
- **FR-013**: Browser-backed providers MUST NOT route, block, close, or resend solely from LISTEN/network data.
- **FR-014**: Browser-backed providers MUST NOT route, block, close, or resend solely from MutationObserver data; if mutation wake-ups remain internally, they may only trigger a fresh DOM projection.
- **FR-015**: Independent Agent creation, activation, and trigger contexts MUST be disabled or removed from runtime-v2 execution.
- **FR-016**: CronJob MUST create ordinary TeamTasks using deterministic idempotency keys.
- **FR-017**: Heartbeat stale recovery MUST create ordinary TeamTasks using deterministic idempotency keys.
- **FR-018**: Existing legacy Independent Agent records MUST not wake after reload under runtime-v2.
- **FR-019**: Dependency relationships MUST form a DAG over existing TeamTask ids only. A TeamTask is dependency-ready only when all dependencies are DONE. Existing non-terminal prerequisites, including RUNNING, WAITING, PAUSED, or resumable BLOCKED, project `WAITING_DEPENDENCY`; STOPPED projects `DEPENDENCY_FAILED`. The projection is recalculated whenever prerequisite state changes. Cycles and missing dependency ids MUST fail validation. `WAITING_DEPENDENCY` and `DEPENDENCY_FAILED` are derived dependency projections, not TaskStatus values, and are reserved from role-key use. Under the current TaskStatus set, BLOCKED is always resumable for dependency purposes; only STOPPED maps to `DEPENDENCY_FAILED`. `ROUTE_BUDGET_EXHAUSTED` and `ROUTE_VALIDATION_EXHAUSTED` are resumable BLOCKED reasons. `ROUTE_CONTRACT_DRIFT` is non-terminal but has no in-place refresh in this feature: dependents remain `WAITING_DEPENDENCY` until the old prerequisite is explicitly resolved or STOPPED; a replacement TeamTask is a distinct id and dependencies are never re-pointed automatically.
- **FR-020**: The dashboard MUST show TeamTask, CronJob, Heartbeat, and Dependency state without requiring operators to understand legacy Solo/Independent Agent modes.

### Key Entities

- **TeamTask**: Unified work item with task text, team name, provider_id, roles, start role, closing role, dependency ids, current state, and route history.
- **RoleSlot**: A named execution slot within a TeamTask; it can perform any duties specified by the prompt.
- **RouteContract**: The generated agreement between prompt prose and allowed JSON route values for a role turn.
- **DomProjection**: DOM-derived transcript and UI state used for task decisions.
- **CronJob**: Deterministic scheduler entry that creates or reuses TeamTasks.
- **HeartbeatRecord**: Liveness record for workers, endpoints, pages, and active task roles.
- **DependencyEdge**: Directed dependency between TeamTask ids.

## Success Criteria *(mandatory)*

### Measurable Outcomes

Gate A outcomes:

- **SC-001**: A one-role TeamTask can run and route DONE without PLAN or REVIEW in focused tests.
- **SC-002**: Route prompt/schema conflict tests cover at least the observed REVIEW-defect-to-DEV loop and prevent self-reroute-only contracts.
- **SC-003**: Runtime-v2 public APIs reject Solo and Independent Agent task creation fields in focused tests.
- **SC-004**: Material-backed `/api/report` renders file-backed and non-file handoffs without a blank dashboard panel.

Full feature outcomes after later gates:

- **SC-005**: Dashboard source labels no longer present LISTEN or MutationObserver as runtime-v2 decision sources.
- **SC-006**: A stale heartbeat scenario creates exactly one recovery TeamTask per idempotency key across reload.
- **SC-007**: Existing legacy independent agent records remain disabled/hidden after runtime-v2 reload and produce no INDEPENDENT_AGENT_TRIGGER_CONTEXT prompts.

## Assumptions

- Existing legacy code may remain temporarily for migration, but runtime-v2 must not expose it as a public path.
- DOM projection may use safe wake signals internally only if every decision re-reads DOM before acting.
- CronJob and heartbeat are deterministic runtime services, not LLM agents.
- Implementation will preserve unrelated dirty work and avoid broad rewrites outside the named owner boundaries.

## Clarification: Provider Runtime

ChatGPT Web is only one provider. Runtime-v2 MUST NOT hard-code ChatGPT Web as the whole execution runtime. The worker MUST dispatch provider work through a common ProviderDispatcher and provider-specific modules/scripts.

### Additional User Story 5 - Dispatch to provider-specific scripts (Priority: P2)

An operator or trigger can request a provider such as `chatgpt_web`, `codex`, `pi`, or a future `copilot/custom` provider. The worker calls the common dispatcher, which then calls the provider module. Provider details stay outside TeamTask routing.

**Independent Test**: A task can select `provider="pi"` for a long lightweight job and `provider="codex"` for coding work without changing the TeamTask route logic.

**Acceptance Scenarios**:

1. **Given** a request sets `provider="pi"` with timeout `7h`, **When** the worker dispatches it, **Then** it calls the Pi provider wrapper and records a ProviderResult status such as `succeeded`, `timeout`, or `failed` back to the TeamTask context; this status is not a TeamTask route.
2. **Given** a request sets `provider="codex"`, **When** the worker dispatches it, **Then** it calls the Codex provider wrapper without importing ChatGPT Web DOM code.
3. **Given** a future provider such as `copilot` or `custom`, **When** it is registered, **Then** route logic does not change.

### Additional User Story 6 - Provider material API (Priority: P3)

Provider scripts may have only ACP/exec/curl access. They must fetch handoff, prompts, skills, rules, reports, task context, and logs through local API endpoints rather than file-tool reads.

**Independent Test**: A provider process can start by calling material APIs for the exact task handoff, prompt, skill, rule, and report content.

**Acceptance Scenarios**:

1. **Given** a provider has a material reference, **When** it calls the local API, **Then** it receives bounded content with version/id metadata.
2. **Given** a provider requests a report, **When** it calls `/api/report` or the v2 material endpoint, **Then** it receives the report content or a safe reference.

### Additional Functional Requirements

- **FR-021**: Runtime-v2 MUST define ChatGPT Web as `provider=chatgpt_web`, not as the whole runtime. For ChatGPT Web browser-dom execution on Business/Enterprise accounts, the provider MUST detect visible tool/MCP permission prompts and click approved `Allow` controls using the existing CDPA permission handler/ownership guard before judging the role turn as stalled, terminal, or repairable. Permission handling is part of browser-provider execution, not a TeamTask route.
- **FR-022**: Runtime-v2 MUST include a ProviderDispatcher that accepts a normalized ProviderInvocation and returns a normalized ProviderResult.
- **FR-023**: Provider-specific code MUST live behind separate modules/scripts, initially including `chatgpt_web`, `codex.py`, and `pi_coding.py`; future `copilot.py` and `custom.py` providers MUST be addable without route-logic changes.
- **FR-024**: ProviderInvocation MUST include provider id, task text, material references, workspace, timeout, output limits, environment policy, and execution mode.
- **FR-025**: ProviderResult MUST use statuses `succeeded`, `timeout`, `failed`, `cancelled`, or `partial`, include stdout/stderr/result references and provider job id, and MUST NOT directly change TeamTask route state.
- **FR-026**: Pi provider MUST support long-running lightweight command execution, including examples equivalent to `pi "<task>"` with operator-configurable timeouts such as 3h or 7h.
- **FR-027**: Codex provider MUST support coding-oriented execution through ACP or exec-compatible invocation without sharing ChatGPT Web DOM code.
- **FR-028**: Provider selection MUST be driven by explicit user request, trigger policy, or provider capability rules; it MUST NOT be inferred from legacy task mode names.
- **FR-029**: Runtime-v2 MUST expose local material API endpoints for handoff, prompt, skill, rule, report, and task context content so provider processes can fetch materials with curl/API access.
- **FR-030**: The material API MUST include version/id metadata and bounded content responses; large content MUST be exposed as references/artifacts rather than unbounded JSON.
- **FR-031**: The material API MUST reject arbitrary filesystem paths and serve only registered task/runtime materials.
- **FR-032**: Existing `/api/report` behavior, if present, MUST either be preserved or bridged to the new material API for compatibility.

### Additional Key Entities

- **ProviderRegistry**: Catalog of providers, module paths, execution modes, capabilities, and timeout limits.
- **ProviderInvocation**: Normalized worker request sent to provider modules.
- **ProviderJob**: Durable long-running provider process record.
- **ProviderResult**: Normalized provider completion/timeout/failure output.
- **MaterialRef**: API-addressable reference to handoff, prompt, skill, rule, report, task context, log, or artifact content.
## Clarification: Adopt Existing Conversation URL

Runtime-v2 must support continuing from a user-supplied existing conversation URL. ChatGPT Web remains only one provider, but it is the first provider that supports a bound browser conversation URL.

### User Story 7 - Continue work from an existing chat URL (Priority: P2, gated after core runtime)

As an operator, I want to give CDPA an existing ChatGPT conversation URL after I have already started work manually, so CDPA can take over follow-up control without waiting beside the browser. The adopted URL may be used by a multi-role TeamTask or by a single-role TeamTask that feels like legacy solo operation.

**Independent Test**: Submit a TeamTask with `conversation_url`, `provider_id=chatgpt_web`, `roles=["REVIEW"]`, `start_role="REVIEW"`, and a role contract that allows REVIEW to route DEV or DONE. Verify the worker opens or reuses the exact URL, confirms the visible conversation identity, injects only the follow-up prompt, and routes to DEV when REVIEW reports reproducible fixes.

**Acceptance Scenarios**:

1. **Given** a user supplies a canonical ChatGPT conversation URL and endpoint, **When** runtime-v2 creates the TeamTask, **Then** the ChatGPT Web provider binds that URL as the role conversation target and refuses to send if the opened page resolves to a different conversation.
2. **Given** a user treats the URL as a single-role task, **When** roles contains only DEV, REVIEW, TEST, or AUDIT, **Then** runtime-v2 runs it as a one-role TeamTask, not legacy Solo Task.
3. **Given** a role on an adopted URL discovers work for another role, **When** route contract permits that destination, **Then** the task can route to that role using the same TeamTask and dependency model.
4. **Given** the adopted URL task depends on another TeamTask, **When** that dependency is not DONE, **Then** the URL-bound task waits without touching the browser conversation. ProviderJobs remain owned by their TeamTask and are not dependency ids.
5. **Given** policy selects Pi or Codex for a follow-up step, **When** the provider trigger fires, **Then** the worker creates or resumes a ProviderJob and returns the result to the TeamTask context.

### Additional Functional Requirements

- **FR-033**: Runtime-v2 MUST accept an optional `conversation_url` for providers that support external conversation binding.
- **FR-034**: `conversation_url` MUST be provider-scoped; ChatGPT Web may use a ChatGPT URL, while Codex/Pi providers MUST reject unsupported browser URLs unless they explicitly implement compatible binding.
- **FR-035**: ChatGPT Web adopted URL execution MUST verify exact conversation identity before any Send. A URL mismatch MUST fail closed with block code `CONVERSATION_IDENTITY_MISMATCH` and MUST NOT fall back to a new chat.
- **FR-036**: Adopted URL tasks MUST support both multi-role TeamTasks and one-role TeamTasks; runtime-v2 MUST NOT reintroduce legacy Solo Task as a separate execution model.
- **FR-037**: Role routing from an adopted URL MUST use the same RouteContract as normal TeamTasks, including DEV/REVIEW/TEST/AUDIT/custom roles and DONE.
- **FR-038**: Adopted URL tasks MUST support `depends_on_task_ids` before browser interaction. Dependency ids refer only to TeamTasks; ProviderJobs are owned by one TeamTask and never appear in the dependency graph.
- **FR-039**: Provider policy MUST be able to trigger Pi, Codex, or a future provider from an adopted URL task when the role contract or operator request requires it.
- **FR-040**: The dashboard and `/api/tasks` MUST show whether a TeamTask is new-conversation, adopted-url, or provider-job-backed.
- **FR-041**: The material API MUST expose adopted URL handoff context so a provider can fetch the current role prompt, handoff, dependency state, and prior reports without local file reads.

## Clarification: Generic RolePolicy, reachable roles, and duplicate normalization

Role selection and delegation must be data-driven and must not use PLAN/REVIEW/DEV/TEST role-name branches. Runtime-v2 uses one RolePolicy algorithm.

### Role key rules

1. Canonicalize each submitted role key by trimming, uppercasing, and replacing runs of spaces or `-` with `_`.
2. A canonical role key MUST match `^[A-Z][A-Z0-9_]{0,31}$`.
3. Role keys that collide with terminal route tokens, reserved control route tokens such as `WAIT`, TaskStatus tokens, or dependency projection tokens `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` are reserved and rejected.
4. Valid role keys do not need pre-registration.
5. Remove empty keys and collapse duplicates while preserving first occurrence order.

### Initial roles and defaults

6. `roles` is the operator-submitted initial team, not necessarily the full set of roles the task may use.
7. If `start_role` is omitted, default it to the first normalized initial role.
8. If `closing_role` is omitted, default it to the first normalized initial role.
9. `start_role` and `closing_role` MUST belong to normalized initial `roles`.
10. DONE authority belongs only to `closing_role`.

### Reachable roles and lazy RoleSlots

11. TeamTask MAY include optional `route_targets`.
12. At creation, RolePolicy computes immutable `reachable_roles` as the transitive closure of normalized initial `roles`, canonicalized `route_targets`, and destinations declared by semantic route data for every role discovered in that closure.
13. The semantic-route data used for that closure is snapshotted/versioned at creation as the task route contract. Prompt-time config drift MUST NOT silently change reachability; a mismatch blocks with `ROUTE_CONTRACT_DRIFT`. This feature does not mutate the frozen contract in place; recovery requires creating a new TeamTask from the desired current configuration (and stopping/replacing the blocked task as appropriate).
14. Invalid/reserved destinations or an exceeded configured `max_reachable_roles` limit reject task creation before Send.
15. `reachable_roles` never changes after creation. A role starts as a declared RoleSlot and becomes materialized only when a validated route first reaches it.
16. Recording the route, incrementing `route_seq`, and materializing a new slot happen in one durable operation before that role's Send intent.
17. The dashboard/API preserve both `submitted_roles`/normalized initial `roles` and computed `reachable_roles`.

### Route derivation and loop safety

18. `allowed_routes(current_role) = reachable_roles - {current_role}`, plus `DONE` only when `current_role == closing_role`.
19. The current role is added back only under `self_route_policy=bounded(max_self_routes=N)`; default self-route policy is `forbidden`.
20. If the creation-time semantic-route snapshot contains a self-destination while self_route_policy is forbidden, task creation is rejected with `SELF_ROUTE_FORBIDDEN`; bounded self-destinations are allowed only within their configured budget.
21. If semantic route data for a non-closing role points to DONE, the RouteContract maps that semantic outcome to `closing_role` before generating prompt prose/schema. This is generic and role-name agnostic.
22. Prompt prose may mention only values present in the generated allowed-route enum.
23. A task has a configured positive `max_routes` budget for non-terminal role-to-role transitions. Budget is checked before a new role Send: when exhausted, a non-closing role becomes BLOCKED with `ROUTE_BUDGET_EXHAUSTED` before Send; a closing role receives only `DONE` in its enum. If that closing-role response nevertheless emits non-DONE, classify directly as `ROUTE_BUDGET_EXHAUSTED` without consuming route-repair budget. Exhaustion blocks store `block_retryable=false`, retain no pending route output, and recover only through `increase_route_limits` followed by ordinary Resume, which rebuilds the route enum and starts a new exactly-once Send.
24. `max_route_repairs` applies per active role turn and resets only after a valid route commits to another role or the task closes. Resume after `ROUTE_BUDGET_EXHAUSTED` or `ROUTE_VALIDATION_EXHAUSTED` never resets counters implicitly. Same-task continuation uses the existing `POST /api/tasks/{task_id}/controls` task-control chain with Gate A action `increase_route_limits` to monotonically increase `max_routes` and/or `max_route_repairs`; consumed counters remain unchanged. Every supplied limit MUST be strictly greater than both its prior configured limit and its consumed count. Empty or mixed-validity payloads reject atomically, the action is valid only when durable task `block_code` is one of the two exhaustion codes, and rejection changes neither limits nor counters. Existing Idempotency-Key replay returns the original matching command/result rather than re-running strict-increase validation; a fresh stale `expected_task_version` rejects before mutation. Without an accepted control update, the task remains BLOCKED or is replaced/stopped.

Examples:

```json
{"roles":["A","B","C"],"start_role":"C","closing_role":"B"}
```

is valid and starts at C while allowing B to close.

```json
{"roles":["REVIEW","REVIEW"],"start_role":"REVIEW","closing_role":"REVIEW"}
```

normalizes initial roles to `["REVIEW"]`. If REVIEW semantic route data declares `defect -> DEV`, `reachable_roles` is frozen as `["REVIEW","DEV"]`; DEV is materialized only if that route is taken, and DEV returns to REVIEW rather than routing DONE.

```json
{"roles":["A"],"route_targets":["B"],"start_role":"A","closing_role":"A"}
```

is a generic equivalent with no role-name special case.

## Clarification: Non-file handoff materialization and dashboard report rendering

Runtime-v2 must not assume every handoff, prompt, skill, rule, or report is backed by a filesystem path. Some handoffs are inline strings, generated prompt bodies, route JSON repair payloads, provider job stdout/stderr, adopted URL context, or API-only material. These materials must still be visible through the dashboard and report API.

Additional requirements:

- **FR-042**: Every handoff must be represented by a MaterialRef, even when it is not a file.
- **FR-043**: MaterialRef `source_kind` MUST use the common set `file`, `inline`, `api`, `generated_prompt`, `route_json`, `provider_result`, or `transcript`. `material_type` is a separate classification such as report, handoff, prompt, skill, rule, task_context, log, or artifact.
- **FR-044**: `/api/report` MUST resolve both file-backed reports and non-file MaterialRefs into bounded display content for dashboard use.
- **FR-045**: The dashboard MUST render handoff/report content from MaterialRef content, not only from local file paths.
- **FR-046**: If a handoff is a route JSON object, provider result, prompt body, or inline text, it MUST be shown with source kind, opaque string version, content length, and truncation state.
- **FR-047**: Unsafe or unavailable MaterialRefs MUST fail closed with a visible dashboard error, not a blank report panel. In Gate A, `/api/report` returns bounded inline display content; `content_ref` is opaque/nullable and the dashboard does not dereference it. Full `/api/materials/*` dereferencing belongs to Gate B.

## Clarification: Direct live chat dashboard across provider tabs/endpoints

Runtime-v2 must support a dashboard conversation console that can talk directly to a bound provider tab, especially ChatGPT Web tabs on multiple endpoints. The operator can select an endpoint/conversation, type a message in the dashboard, and the worker pastes it into the provider composer and sends it exactly once. The live response must stream back into the dashboard through DOM-only projection.

Endpoints are registry-backed and dynamic, not a fixed list of ports. The dashboard may add an external CDP endpoint from operator input such as `9222`, `192.168.1.10:9222`, `host:port`, or an explicit `http(s)://` / `ws(s)://` CDP URL. Runtime-v2 reuses the existing BrowserEndpoint registry/API and `normalize_cdp_endpoint()` owner rather than introducing a second endpoint store.

This console is not a separate task model. It is a control surface over a ProviderSession/TeamTask binding.

Additional requirements:

- **FR-048**: Dashboard MUST provide a Direct Chat control that can target a provider session by endpoint id and conversation binding.
- **FR-049**: Direct Chat send MUST use the same composer safety sequence as TeamTask sends: clear, fill, verify non-empty, send, record accepted-send evidence.
- **FR-050**: Direct Chat MUST be exactly-once; retries after accepted-send evidence MUST be reconciliation-only and MUST NOT resend.
- **FR-051**: For ChatGPT Web/browser-backed Direct Chat, response streaming MUST use DOM-only projection: visible conversation nodes, progress nodes, visible thinking summaries, tool activity nodes, final answer nodes, and visible error/terminal state. ChatGPT Web visible thinking/progress MUST include the current activity-header DOM family observed in production: `.group/activity-header`, `[class*="agent-activity"]`, `[class*="activity-header"]`, `.summary-lK7Lpm`, `.cadencedShimmerSweep-ICUAVH`, and `.cadencedShimmerHighlight-VcX29h`, with bounded text and visibility/viewport metadata.
- **FR-052**: Direct Chat streaming MUST NOT use LISTEN or MutationObserver as decision sources. If any wake signal remains internally, it must trigger fresh DOM projection before publishing.
- **FR-053**: Dashboard MUST support switching among any registered online browser endpoints without opening each browser tab manually; 9202, 9212, and 9222 are examples only and MUST NOT be hard-coded as the endpoint set.
- **FR-054**: Dashboard MUST display per-endpoint session state: endpoint id, normalized CDP URL, label/health, provider id, conversation url, composer state, stop visibility, retry visibility, permission/Allow prompt count, assistant rendering status, latest visible response segment, visible activity-header/shimmer thinking nodes, tool/progress nodes, and terminal state.
- **FR-055**: Direct Chat MUST support attaching MaterialRefs, such as a handoff or report, by reference rather than by copying arbitrary files.
- **FR-056**: Direct Chat MUST be dependency-aware when bound to a TeamTask; if dependencies are not ready, the dashboard must block send and show the dependency reason.
- **FR-057**: Direct Chat transcripts MUST be exposed through API so providers, workers, and the dashboard can read the same DOM-projected stream.
- **FR-058**: Runtime-v2 MUST reuse the existing BrowserEndpoint registry and `/api/browser-endpoints` command owner for endpoint add/update/remove/start/stop; it MUST NOT create a second endpoint registry for Direct Chat.
- **FR-059**: Endpoint creation MUST accept the existing `normalize_cdp_endpoint()` input forms: local port shorthand, `host:port`/`ip:port`, and explicit `http`, `https`, `ws`, or `wss` URL. Normalized `cdp_url` is stored in the registry and session/task bindings refer to stable `endpoint_id`, not raw operator text.
- **FR-060**: Adding an external endpoint MUST probe it before first registration and fail closed if unavailable; re-adding an already registered normalized address with identical settings may return the existing endpoint, while conflicting settings MUST be rejected.
- **FR-061**: Dashboard MUST allow the operator to add, update, remove/drain, and select registered endpoints from the same endpoint registry. A successfully added endpoint becomes selectable without a runtime restart.
- **FR-062**: Runtime-v2 MUST NOT silently retarget an active ProviderSession/TeamTask to a different CDP URL. Retargeting or removing an endpoint that has active/unresolved execution MUST obey the existing drain/in-use guard and MUST NOT reassign work to another endpoint.
- **FR-063**: External endpoints are attach-only by default (`management_mode=external`). Managed-local browser lifecycle settings remain an existing optional registry capability and MUST NOT be required merely to add `<ip>:<port>`.
- **FR-064**: ProviderSession and ConversationBinding MUST pin `endpoint_id`. Endpoint selector state is derived from the registry at render time, but an existing binding never follows a changed default endpoint implicitly.
- **FR-065**: Interactive writer exclusivity MUST NOT be keyed only by `endpoint_id`. A verified `(provider_id, canonical_conversation_id)` may have at most one active writer globally across endpoint aliases and distinct physical browsers/profiles. If canonical conversation identity is knowable before Send (existing/adopted/resumed conversation), the canonical writer lease MUST be acquired before Send intent. A provisional provider+physical-browser+durable-page-binding key is permitted only after the runtime has positively verified the provider's genuine new-conversation surface with no canonical id; an unresolved page that might already be an existing conversation MUST NOT Send provisionally. EndpointPool physical-browser conflict state is not eligible for browser Send under Gate C.
- **FR-066**: ConversationBinding MUST store bind-time endpoint evidence: `endpoint_id`, normalized `cdp_url`, probed `physical_browser_id`, and canonical conversation identity when available. Any endpoint/address/physical-identity drift before Send MUST block and require rebind; it MUST NOT silently follow an updated endpoint record.
- **FR-067**: Updating/removing an endpoint referenced by an unsealed ProviderSession/ConversationBinding MUST obey Gate C binding guards in addition to existing task/execution drain guards. Such operations MUST drain/reject until the binding is released or sealed; no idle binding may be silently orphaned or retargeted.
- **FR-068**: Browser endpoint mutation MUST remain on the existing operator-control `/api/browser-endpoints` surface and existing runtime access boundary; provider/material APIs MUST NOT gain endpoint-mutation authority. `normalize_cdp_endpoint()` remains the address parser and rejects credentials/userinfo, whitespace, unsupported schemes, and invalid ports. Remote plaintext `http`/`ws` endpoints are accepted only as an explicit operator trust decision, never by automatic discovery.
- **FR-069**: Every endpoint admission/revalidation path (first add, cdp_url update, start/attach, and recurring health refresh) MUST use the same bounded CDP-specific safety policy. For HTTP(S), probe only the admitted authority's `/json/version` with bounded timeout/size, reject redirects/cross-origin substitution, and ensure any returned `webSocketDebuggerUrl` is pinned/rewritten to the admitted authority rather than trusting a different host. For WS(S), validate through a bounded CDP connector handshake. Dashboard/API may expose structured health/auth/error state but MUST NOT echo arbitrary remote response bodies. Hostname endpoints are explicit operator-trust aliases and are re-resolved per CDP operation; this feature does not claim cross-operation IP pinning. Safety relies on CDP-specific validation plus bind-time `physical_browser_id` revalidation before Send.
- **FR-070**: Once Gate C writer safety is enabled, every browser-backed TeamTask role turn that can Send MUST acquire the same global writer-lease namespace used by adopted URL and Direct Chat, even when the role did not start with an explicit ConversationBinding. The runtime MUST project/create binding evidence before Send. For a new conversation, the provisional key uses provider + `physical_browser_id` + durable internal `page_binding_id`. When canonical identity becomes available, atomically claim the canonical key while retaining the provisional lease until the current SendAttempt becomes terminal. If canonical-key collision occurs, the canonical writer key enters `quarantined`: no binding, including the key winner, may start another Send until every conflicting non-terminal attempt reaches terminal state. The loser remains reconciliation-only under provisional ownership, cannot Send again, and seals after terminal state. After quarantine clears, the surviving canonical owner MUST capture a fresh DOM/user-turn baseline before its next Send. Concurrent re-key is serialized so one binding wins and all losers fail closed. If an unresolved conflicting attempt is operator-abandoned and cannot be reconciled, the canonical conversation remains quarantined/non-reusable for runtime writes rather than being re-adopted with a fresh baseline.
- **FR-071**: Provisional page ownership MUST be unique before Send. `page_binding_id` MUST map one-to-one to a live `(provider_id, physical_browser_id, page_target_identity)` and that page target may have only one live provisional writer binding. Allocation/claim is atomic before Send intent and recovery revalidates the same target evidence; conflict blocks with `WRITER_LEASE_BUSY`.
- **FR-072**: Every browser SendAttempt MUST record the current global writer lease owner and monotonically increasing `lease_epoch`. Recording Send intent is atomic with verifying that the caller still owns that writer key at the same epoch. Lease transfer/takeover increments epoch; a stale owner MUST fail before composer mutation/Send with `WRITER_LEASE_LOST`. Lease ownership covers the whole logical role/message turn: from before Send intent through assistant terminal DOM state and any route-repair Sends until a valid route/turn commit, block, or task terminal state. `WRITER_LEASE_BUSY` is retryable after the current owner releases; `WRITER_LEASE_QUARANTINED` is retryable only for temporary quarantine and non-retryable when unresolved abandon permanently seals the conversation.

## Clarification: Phase 1.5 browser control recovery before Gate B

Gate A acceptance showed the core route/report contract can pass while live ChatGPT Web control still stalls on three practical blockers: Business/Enterprise `Allow` prompts, accepted-send timeout/retry repair, and terminal route JSON that is visible in the assistant transcript/audit but not consumed into durable task state. Phase 1.5 is a small required gate before Gate B. It uses the existing DOM projection and existing CDPA browser controls; it must not add a new DOM crawler or a second browser transport owner.

Additional requirements:

- **FR-083**: Phase 1.5 MUST use the existing ChatGPT Web DOM projection as the single source for response-control decisions. Existing fields such as `stop_visible`, `retry_visible`, composer state, errors, assistant text/transcript, report existence, and route JSON are sufficient; new implementation may expose bounded diagnostics but MUST NOT introduce a second DOM source owner.
- **FR-084**: For ChatGPT Web Business/Enterprise accounts, visible MCP/tool permission prompts with `Allow` controls MUST be handled before declaring a turn stalled, terminal, or repairable. The runtime MUST reuse/carry forward the existing CDPA permission-click guard: exact task/team/page ownership, clean composer/no manual draft conflict, bounded click, timeline diagnostic, then continue the same `wait_response` turn.
- **FR-085**: Accepted-send repair MUST be driven by DOM state, not blind resend. `stop_visible=true` and `retry_visible=false` means the response is still active and the controller remains in `wait_response`. Stable `stop_visible=false` and `retry_visible=true` means provider Retry is available and may be clicked once for the exact accepted user turn after ownership and composer checks. The original prompt MUST NOT be resent.
- **FR-086**: If retry is unavailable and no terminal result is recorded, the runtime may queue route/report repair from existing workspace/report state only after reconciling the accepted-send boundary. Repair prompts are distinct sends and must not replay the original user prompt.
- **FR-087**: If the audit transcript or fresh DOM assistant text contains a valid terminal route JSON for the active hop, including `{"route":"DONE",...}`, and the active hop's expected report exists and is non-empty, the worker MUST record that response/report and apply the RouteContract transition instead of remaining in `wait_response`. This reconciliation must verify the route value against the active RouteContract and report path; invalid or mismatched evidence remains fail-closed.
- **FR-088**: Script/command timeout callback is part of Phase 1.5 as a minimal AsyncContinuation path. A confirmed long-running script spawn may expose `WAIT`; a valid WAIT moves the task to `WAITING` with `waiting_code=async_callback`; the callback records bounded stdout/stderr/MaterialRefs and wakes the same TeamTask and same role on the parent ChatGPT Web conversation. This minimal path is limited to existing TeamTask role conversations and does not implement Gate C Direct Chat, adopted URL, dynamic endpoint UI, or global writer-lease migration.
- **FR-089**: Dashboard/API MUST expose Phase 1.5 evidence: latest `stop_visible`, `retry_visible`, permission prompt/count, last permission click, retry-click/repair decision, terminal-route reconciliation source, continuation id/state, callback result, and wake status.
- **FR-090**: Gate B MUST NOT start until Phase 1.5 is accepted. Phase 1.5 acceptance is required because all ChatGPT Web provider roles on Business/Enterprise accounts are expected to see `Allow` prompts and because accepted-send recovery must be reliable before introducing ProviderDispatcher/ProviderJob concurrency.

## Clarification: Async WAIT route and wake-back continuation

Runtime-v2 must support long external work without keeping the ChatGPT Web response open or repeatedly polling CDP/M365. A role turn may durably start an async provider/script job, return a `WAIT` control route after job-spawn confirmation, and let the worker stop the browser turn. When the external job finishes, it calls back into CDPA with the durable continuation id; CDPA injects the result into the original task/conversation through the same writer-lease and adopted-URL safety rules.

`WAIT` is not `PAUSE`. `PAUSE` means operator intervention is required. `WAIT` means the runtime is waiting for a registered async callback and may wake automatically.

Additional requirements:

- **FR-073**: RouteContract MUST support reserved control route `WAIT` only when an `AsyncContinuation` has already been durably registered and the external job spawn has been confirmed. `WAIT` is a reserved route token and MUST be rejected as a role key.
- **FR-074**: A `WAIT` route MUST set task status `WAITING` with `waiting_code=async_callback` and MUST store `continuation_id`, provider/job identity, parent task id, role key, route_contract_id, and callback/wake policy. It MUST NOT use `PAUSED` or require operator action.
- **FR-075**: AsyncContinuation MUST own idempotency for job spawn and callback completion. Duplicate job-spawn acknowledgement or duplicate callback delivery MUST resolve to the existing continuation/result and MUST NOT create another wake message or another SendAttempt. A callback with the same idempotency key but a conflicting payload MUST fail closed with `ASYNC_CALLBACK_CONFLICT`.
- **FR-076**: External jobs may finish by calling a local callback API/CLI, for example `POST /api/async-continuations/{continuation_id}/complete`, with structured result refs, stdout/stderr refs, exit state, and optional wake prompt. Providers such as Pi/Codex/M365/custom scripts may also use their native session resume mechanism, but CDPA still records the result as ProviderResult/MaterialRef.
- **FR-077**: Callback completion MUST schedule a wake continuation for the same TeamTask and same role unless the continuation was cancelled, superseded, timed out, or the parent task is terminal. This feature supports only `wake_policy=same_role`; closing-role, route-contract-prompt, and manual-review wake targets are out of scope until separately specified. Wake uses the parent conversation binding/adopted URL when present; it MUST NOT silently fall back to a new conversation, new task, or new role slot.
- **FR-078**: While waiting for callback, the parent conversation binding keeps the canonical writer key in `reserved_async_wait`, not an active browser/page lease and not ordinary idle/released. Other Direct Chat/TeamTask writers are blocked from injecting turns into the same canonical conversation. Only the registered continuation wake may consume the reservation; operator cancel, timeout, failure, or supersede must durably release or seal the reservation before any other writer may proceed.
- **FR-079**: Wake-back Send MUST reuse the Gate C writer lease, SendAttempt, endpoint identity, and ambiguous-send reconciliation rules. If the original URL/conversation cannot be verified at wake time, CDPA blocks with `ASYNC_WAKE_IDENTITY_MISMATCH` and does not resend elsewhere.
- **FR-080**: Async timeouts/cancellations MUST produce a durable result state (`timeout`, `cancelled`, or `failed`). With `wake_policy=same_role`, success callbacks may wake the same role. Failed/timeout/cancelled callbacks either wake the same role with a bounded failure handoff only when `failure_policy=same_role_failure_handoff`, or otherwise block visibly with `ASYNC_CALLBACK_FAILED`. Conflicting callback payloads fail with `ASYNC_CALLBACK_CONFLICT`; no async failure may leave the task in silent WAIT forever.
- **FR-081**: Dashboard/API MUST show async waiting state, continuation id, provider/job id, last heartbeat/result state, callback age/timeout, wake target conversation, and whether wake is blocked by writer lease, dependency, identity, or cancellation.
- **FR-082**: ChatGPT Web provider MUST be able to return a final response containing only the WAIT route after confirmed async spawn. The outer CDPA worker must treat that as terminal for the current browser turn and stop polling/listening for the long provider result.



## Pre-implementation Review Update

This feature is intentionally broad. Implementation MUST be split by capability gate rather than attempted as one large change.

### MVP Gate A - Core runtime contract

Gate A is the next implementation target and includes only:

- Generic RolePolicy normalization, immutable `reachable_roles`, lazy RoleSlot materialization, route budget, and projection.
- TeamTask-only runtime-v2 public creation.
- RouteContract/prose consistency, closing-role DONE authority, and self/role-loop prevention.
- Material-backed `/api/report` for file and non-file handoffs using bounded inline display content.

Gate A MUST NOT add a new send path or send-state owner. It only adds validation/route bookkeeping before the existing command-ledger Send intent boundary. Existing exactly-once Send ownership remains unchanged.

Gate A explicitly excludes provider execution, CronJob/Heartbeat replacement, adopted URL Send, and Direct Chat live streaming unless the operator separately authorizes widening scope.

### Gate B - Browser/provider and scheduler integration

Gate B starts only after Gate A and Phase 1.5 are accepted. It includes:

- TeamTask dependency DAG validation and `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` readiness projection.

- DOM-only decision cleanup for ChatGPT Web/browser-backed providers.
- ProviderDispatcher and ProviderJob with aligned execution-mode/result contracts.
- ChatGPT Web, Codex, Pi, and future/custom provider wrappers.
- Full material API dereferencing endpoints.
- CronJob/Heartbeat TeamTask creation and Independent Agent deactivation/migration.

ProviderResult status is never a TeamTask route; route transitions remain owned by RouteContract.

### Gate C - Conversation/session operations

Gate C starts only after Gate A, Phase 1.5, and Gate B are accepted. It includes ProviderSession, adopted URL binding, Direct Chat, async WAIT/wake-back continuation, and dashboard reuse of the dynamic BrowserEndpoint registry.

Before Gate C can enable a second interactive writer surface, two entry invariants are mandatory:

1. **Global writer-identity lease**: every browser-backed writer (ordinary TeamTask, adopted URL, Direct Chat) participates in one global writer-key lease. Canonical conversation identity wins over endpoint/binding identity; provisional page ownership is unique per physical page target. Send intent is fenced by lease owner + `lease_epoch`, and takeover increments epoch so stale owners fail before composer mutation. Lease spans the full assistant/route turn, not merely user-message acceptance.
2. **Ambiguous-send reconciliation**: the existing command ledger must expose a durable send-attempt projection with states equivalent to `intent_recorded -> submitted -> accepted|unconfirmed -> reconciled_accepted|reconciled_absent`; recovery from an ambiguous `intent_recorded` or `submitted` state first enters reconciliation/unconfirmed handling and never treats the send as absent without evidence. Capture baseline last-user-message id and turn count before Send. `unconfirmed` never resends; only proven `reconciled_absent` may create a new attempt id.

Adopted conversation binding belongs to the starting role slot. A lazily materialized different role gets its own provider conversation and receives handoff through MaterialRef; when work routes back to the adopting role, that role continues the original adopted URL. Shared conversation ownership across different role slots is out of scope.

### Three-round external review result

M365 was re-probed successfully on both `9222` and `9202`. Three Opus review rounds were completed across the two authenticated endpoints. Round 1 found route/DONE/send/dependency/material inconsistencies. Round 2 separated Gate A blockers from Gate B/C concerns. Round 3 preserved the operator requirement that a one-role task may auto-delegate to another generic role by introducing immutable creation-time `reachable_roles` plus lazy RoleSlot materialization, rather than child TeamTasks or role-name branching.

**Review-plan status**: TECHNICAL PLAN PASS applied to the pre-Phase-1.5 plan. Phase 1.5 clarity additions after the previous technical pass require a fresh technical review before Gate B implementation; operator review-plan approval is still required before implementation.
