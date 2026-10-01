# Tasks: Team-only DOM Runtime

**Input**: reviewed artifacts under `specs/003-team-only-dom-runtime/`
**Implementation rule**: start with Gate A only unless operator explicitly widens scope.

## Phase 1: Boundary Review

- [ ] T001 Read `.agents/rules/project.md`, spec, plan, research, data-model, quickstart, checklist, and all contracts.
- [ ] T002 [P] Inspect current runtime-v2 TeamTask request/state/projection owners and the existing task-control chain: `dashboard_api.py` controls endpoint, `cdpa_worker.py::_apply_next_command(task_control)`, `cdpa_store.py::request_control`, and `cdpa_commands.py::workflow_control_eligibility`; record its current action allowlist, durable `block_code` fields, Idempotency-Key dedupe behavior, replay-before-version ordering, and confirm route-limit mutation is absent before extension.
- [ ] T003 [P] Inspect current prompt/route generation and DONE-authority owners.
- [ ] T004 [P] Inspect current report/handoff API/dashboard owners.
- [ ] T005 [P] Inspect legacy Solo/Independent creation/bridge paths reachable from runtime-v2.
- [ ] T006 Record exact owner boundaries, including the role prompt/template semantic-route source owner, task-control chain, and BrowserEndpoint chain (`dashboard_api.py` API normalization, worker browser_endpoint_* commands, BrowserEndpointRegistry, EndpointPool/physical_id, `normalize_cdp_endpoint`). For each reviewed capability record already-supported vs must-extend; do not create a new owner where an existing owner already matches Also verify normalize_cdp_endpoint rejection rules and record the actual /api/browser-endpoints bind/auth/access boundary plus any must-harden gap.

## Gate A: Core Runtime Contract MVP

### Gate A tests

- [ ] T007 [P] Add RolePolicy canonicalization tests: trim, uppercase, spaces/hyphens to `_`, grammar `^[A-Z][A-Z0-9_]{0,31}$`, reserved terminal/status/projection/control tokens including `WAIT`, `WAITING_DEPENDENCY`, and `DEPENDENCY_FAILED`.
- [ ] T008 [P] Add duplicate-collapse/default tests for `roles=["REVIEW","REVIEW"]`, default start_role/closing_role, and rejection when explicit start_role or closing_role is outside normalized initial roles even if that role is only reachable.
- [ ] T009 [P] Add no-special-role tests for DEV+TEST, A, AUDIT, and custom valid role keys without PLAN/REVIEW requirements.
- [ ] T010 [P] Add reachable-role closure tests for initial roles + route_targets + transitive semantic-route destinations, including max_reachable_roles rejection and frozen route_contract_id/semantic-route snapshot drift -> ROUTE_CONTRACT_DRIFT.
- [ ] T011 [P] Add single-role delegation regression: `roles=["REVIEW"]` with semantic `defect -> DEV` freezes reachable_roles REVIEW,DEV and routes to lazy DEV slot; assert API/dashboard projection shows submitted_roles, initial roles, reachable_roles, and declared/materialized slot state before/after route.
- [ ] T012 [P] Add generic delegation regression: `roles=["A"]` with semantic/route target B behaves identically; no role-name branch.
- [ ] T013 [P] Add DEV-only delegation regression: DEV may reach REVIEW; when DEV is closing_role, REVIEW's semantic DONE returns to DEV rather than DONE.
- [ ] T014 [P] Add lazy RoleSlot crash/idempotency test: route record + route_seq + first materialization commit atomically before Send intent; replay/restart creates no second slot or increment and leaves roles/reachable_roles byte-identical.
- [ ] T015 [P] Add TeamTask boundary tests rejecting public legacy Solo/Independent mode fields and rejecting client-supplied runtime-derived/system-owned state including `task_id`, `submitted_roles`, `reachable_roles`, `route_contract_id`, `self_route_counts`, `route_seq`, `active_role`, `status`, timestamps, or RoleSlot materialization state.
- [ ] T016 [P] Add RouteContract tests proving prompt prose and enum derive from one contract and contain only reachable/terminal values; prose/schema or enum/reachable divergence blocks with `ROUTE_CONTRACT_INVALID`; malformed/out-of-enum output uses per-active-role-turn bounded repair and exhausts to `ROUTE_VALIDATION_EXHAUSTED`; repair count resets only after a valid route commits to another role or the task closes; resume alone never resets it. For `increase_route_limits(max_route_repairs=...)`, accept only a value strictly above both prior configured limit and current-turn consumed repairs; reject decrease/equal/not-above-consumed, empty payload, wrong task state/block reason, and mixed payload where either field is invalid. Assert all-or-nothing rejection leaves both limits plus `route_seq`, `self_route_counts`, and current-turn repair count unchanged. Test same-key/same-payload retry returns the original accepted command/result without re-validating equal limits; same-key/different payload conflicts; stale `expected_task_version` rejects before mutation; eligibility reads durable `block_code`. Assert exhaustion block uses `block_retryable=false`, legacy Retry rejects, plain Resume before increase is ineligible, and after accepted increase a separate Resume is eligible and issues the next repair prompt as a new exactly-once Send without resetting consumed repair count.
- [ ] T017 [P] Add closing-role authority tests: only closing_role has DONE; non-closing semantic DONE maps to closing_role.
- [ ] T018 [P] Add self-route tests: forbidden by default including creation rejection `SELF_ROUTE_FORBIDDEN` for semantic self-destination; bounded self-route includes current role only while budget remains; each self-route increments route_seq, consumes max_routes, and increments the role's cumulative task-lifetime self-route count.
- [ ] T019 [P] Add route-budget tests: max_routes exhaustion blocks with `ROUTE_BUDGET_EXHAUSTED`; resume does not reset counters. For `increase_route_limits(max_routes=...)`, accept only a value strictly above both prior configured limit and `route_seq`; reject decrease/equal/not-above-consumed, empty payload, wrong task state/block reason, and mixed payload where either field is invalid. Assert all-or-nothing rejection leaves both limits plus `route_seq`, `self_route_counts`, and current-turn repair count unchanged; same-key replay/stale-version/durable-block-code assertions mirror T016. Assert budget exhaustion is detected before Send: non-closing role has no pending route output and blocks with `block_retryable=false`; closing role exposes only DONE; if it nevertheless emits non-DONE, classify directly as `ROUTE_BUDGET_EXHAUSTED`, do not charge repair budget, and retain no pending route. Plain Resume before increase rejects; after accepted increase a separate Resume returns to the same pre-Send role state, rebuilds full allowed routes from frozen contract, and a normal new Send may proceed.
- [ ] T020 [P] Add material-backed `/api/report` tests for file, inline, generated_prompt, route_json, provider_result, transcript and api fixtures and structured unsafe/missing errors.
- [ ] T021 [P] Add dashboard tests proving non-file handoffs render bounded inline content and never a blank report panel.

### Gate A implementation

- [ ] T022 Implement or identify one RolePolicy owner for normalization, initial roles, route_targets, reachable-role closure, defaults, explicit start_role/closing_role membership in initial roles, reachability bounds, and creation-time `SELF_ROUTE_FORBIDDEN` validation.
- [ ] T023 Replace hard-coded PLAN/REVIEW/DEV/TEST role-selection branches with RolePolicy outputs; prompt-template selection remains data/config.
- [ ] T024 Extend TeamTask request/state with provider_id, submitted_roles, normalized initial roles, optional route_targets, immutable reachable_roles, route_contract_id/semantic-route snapshot, max_routes, max_route_repairs, route_seq, self_route_policy, and per-role self_route_counts; public creation rejects attempts to supply runtime-derived state.
- [ ] T025 Reject legacy Solo/Independent public creation fields (`workflow_mode=solo`, `solo_role`, `task_mode=independent`, independent-agent fields) with migration guidance to one-role TeamTask or CronJob.
- [ ] T026 Add RoleSlot declared/materialized state; durably record route + route_seq + first materialization atomically before Send intent.
- [ ] T027 Ensure one-role TeamTask and lazy RoleSlots stay TeamTask through the legacy bridge/store; never translate to legacy Solo.
- [ ] T028 Update API/dashboard projection to show submitted_roles, roles, reachable_roles, slot states, start_role, closing_role, and route_seq/max_routes.
- [ ] T029 Implement/update RouteContract builder from frozen route_contract_id/semantic-route snapshot + reachable_roles + closing_role + self-route/route budgets; detect prompt-time drift as ROUTE_CONTRACT_DRIFT; reuse the existing invalid-route repair owner with `max_route_repairs` scoped per active role turn, reset only after valid route commit to another role or task close, and block with `ROUTE_VALIDATION_EXHAUSTED` when exhausted without widening reachability.
- [ ] T030 Enforce DONE only for closing_role and generic non-closing DONE -> closing_role mapping.
- [ ] T031 Enforce prompt/schema consistency and route_contract_id drift check before the existing command-ledger Send intent boundary; contract divergence/out-of-set enum blocks with `ROUTE_CONTRACT_INVALID`.
- [ ] T032 Extend the existing `dashboard_api.py` -> `cdpa_worker.py` -> `cdpa_store.py::request_control` owner chain plus `cdpa_commands.py::workflow_control_eligibility` with `task_control` action `increase_route_limits` (payload `max_routes` and/or `max_route_repairs`, at least one required) for durable `block_code` `ROUTE_BUDGET_EXHAUSTED` / `ROUTE_VALIDATION_EXHAUSTED`; preserve existing Idempotency-Key replay and `expected_task_version` semantics, enforce strict-increase-over-current-limit and above-consumed validation, eligible-block-state validation, non-empty atomic/all-or-nothing payload validation, preserve both limits on rejection plus all counters/role/route state, and keep resume as a separate control including the post-repair-limit path. Store both exhaustion blocks with `block_retryable=false`; extend `workflow_control_eligibility` so Resume requires raised limits and Retry remains invalid. Enforce route budget before Send so non-closing exhaustion has no pending route; closing exhaustion exposes DONE only and a non-DONE response maps directly to route-budget exhaustion without repair-budget consumption. After max_routes increase + Resume rebuild the enum and use a new Send; never commit a blocked route output. Also enforce self-route/max_routes rules including `ROUTE_BUDGET_EXHAUSTED`.
- [ ] T033 Implement/extend MaterialRef registration with unified source_kind/material_type/version/content metadata.
- [ ] T034 Bridge existing `/api/report` to bounded MaterialRef inline display content while preserving file-backed behavior; content_ref remains opaque/nullable in Gate A.
- [ ] T035 Update dashboard report/handoff panels to render bounded content/structured errors without dereferencing content_ref.
- [ ] T036 Run Gate A focused tests T007-T021.
- [ ] T037 Run `python -m compileall -q src/playwright_auto`.
- [ ] T038 Run `git diff --check -- src tests specs/003-team-only-dom-runtime`.
- [ ] T039 Review actual Gate A diff for one-owner boundaries and unrelated rewrites.

## Phase 1.5: Browser Control Recovery and Script WAIT Callback (required before Gate B)

Phase 1.5 closes the live-operation blockers found after Gate A: Business/Enterprise Allow prompts, accepted-send retry/repair, terminal route reconciliation from transcript/report evidence, and script-timeout callback via `WAIT`. It MUST use the existing ChatGPT Web DOM projection and existing CDPA browser controls; it MUST NOT introduce a second DOM crawler, raw browser transport path, or full Gate C writer-lease/adopted-url/direct-chat implementation.

### Phase 1.5 tests

- [ ] T040 [P] Add ChatGPT Web Business/Enterprise permission tests: visible MCP/tool `Allow` prompts are detected from the existing DOM projection, clicked only under exact task/team/page ownership and composer-safety checks, recorded in diagnostics/timeline, and never treated as route JSON, terminal response, or provider result.
- [ ] T041 [P] Add accepted-send DOM repair tests using existing snapshot fields only: `stop_visible=true` and `retry_visible=false` keeps the turn in `wait_response`; stable `stop_visible=false` and `retry_visible=true` clicks the provider Retry button once for the accepted user turn; retry absence with no terminal result queues route/report repair from existing workspace/report state; none of these paths replay the original prompt.
- [ ] T042 [P] Add terminal-route reconciliation tests: when the audit transcript/DOM assistant text contains a valid terminal route JSON such as `{"route":"DONE","handoff":"...review_turn3...md"}` and the expected report file exists/non-empty for the active hop, the worker records the response/report and commits the route instead of remaining in `wait_response`.
- [ ] T043 [P] Add script-timeout WAIT tests: a long script/command can register a confirmed continuation, expose route `WAIT`, set `status=WAITING` with `waiting_code=async_callback`, and later callback with bounded stdout/stderr/MaterialRefs to wake the same TeamTask and same role on the parent ChatGPT Web conversation.
- [ ] T044 [P] Add dashboard/API projection tests for Phase 1.5 control state: latest `stop_visible`, `retry_visible`, permission prompt/count, retry-click/repaired decision, terminal-route reconciliation source, continuation id, callback state, and wake result are visible without adding another DOM source.

### Phase 1.5 implementation

- [ ] T045 Carry forward the existing CDPA ChatGPT Web permission/Allow click handler into the TeamTask role runtime; preserve exact ownership, composer-safety, and no-route/no-terminal semantics.
- [ ] T046 Implement accepted-send DOM repair in the current response controller using existing DOM snapshot fields (`stop_visible`, `retry_visible`, composer/error/terminal evidence). Treat `stop_visible=true && retry_visible=false` as active response; treat stable provider Retry as an owned retry-click, not a prompt resend; fall back to route/report repair only when no retry is available and no terminal route/result exists.
- [ ] T047 Implement terminal-route reconciliation from existing transcript/report evidence: if the active hop has accepted-send evidence, the expected report exists, and fresh DOM/audit transcript contains a valid route JSON matching the RouteContract, commit the route (`DONE` included for the closing role) without another Send, manual prompt, or new chat.
- [ ] T048 Implement minimal AsyncContinuation for scripts/command timeouts before ProviderDispatcher: confirmed script spawn registers continuation metadata, route `WAIT` is admitted only for that continuation, callback records MaterialRefs/ProviderResult-like output, and wake schedules the same TeamTask/same role on the parent ChatGPT Web conversation. This is limited to existing TeamTask role conversations; adopted URL, Direct Chat, dynamic endpoints, and full writer-lease wake-back remain Gate C.
- [ ] T049 Project Phase 1.5 repair/permission/terminal-route/WAIT state in `/api/tasks`, task detail, and dashboard; run Phase 1.5 focused tests, `python -m compileall -q src/playwright_auto`, and `git diff --check -- src tests specs/003-team-only-dom-runtime`.

## Gate B: Browser/Provider and Scheduler Integration (deferred)

### Gate B tests

- [ ] T050 [P] Add browser-provider DOM-only decision tests: LISTEN/Mutation cannot route/block/close/resend; ChatGPT Web Business/Enterprise MCP/tool `Allow` prompts are detected and handled by the permission owner without being treated as routes or terminal responses.
- [ ] T051 [P] Add runtime-v2 dashboard/audit tests showing DOM as browser-provider decision source.
- [ ] T052 [P] Add ProviderDispatcher/browser provider tests for chatgpt_web, codex, pi, disabled/unknown provider, including explicit ChatGPT Web endpoint_id resolution through BrowserEndpointRegistry and rejection of raw/unregistered endpoint transport bypass.
- [ ] T053 [P] Add ProviderInvocation tests for unified execution modes and long_timeout as capability.
- [ ] T054 [P] Add ProviderResult tests for succeeded/timeout/failed/cancelled/partial plus result refs and provider_job_id; assert provider status never becomes route.
- [ ] T055 [P] Add ProviderJob ownership tests proving each job belongs to one TeamTask and never appears in dependency ids.
- [ ] T056 [P] Add TeamTask dependency DAG tests: missing ids/cycles reject; DONE is ready; RUNNING/WAITING/PAUSED/resumable BLOCKED project WAITING_DEPENDENCY; STOPPED projects DEPENDENCY_FAILED; projections recalculate on resume/state change.
- [ ] T057 [P] Add CronJob/Heartbeat deterministic TeamTask idempotency tests, same-stale-episode non-recursion, bounded/no-backlog catch-up behavior, and legacy Independent Agent inert-on-reload tests.
- [ ] T058 [P] Add full material API tests for safe task-scoped registered material dereferencing, cross-task/arbitrary path rejection, and existing runtime bind/auth boundary.

### Gate B implementation

- [ ] T059 Update browser-provider audit/dashboard/worker decisions to use fresh DOM projection only; wake signals only trigger DOM reads. Carry forward the existing CDPA allow-button permission clicker for ChatGPT Web Business/Enterprise role turns, guarded by exact task/team/page ownership and composer safety, and record permission-click diagnostics before continuing the same wait_response turn.
- [ ] T060 Implement ProviderDispatcher as single provider-selection owner.
- [ ] T061 Implement durable ProviderJob owner and ProviderResult normalization.
- [ ] T062 Add ChatGPT Web browser_dom provider wrapper that resolves explicit browser targets through BrowserEndpointRegistry/endpoint_id and does not establish a second raw-address/default-only transport path.
- [ ] T063 Add Codex ACP/exec provider wrapper.
- [ ] T064 Add Pi exec/long_timeout provider wrapper.
- [ ] T065 Add custom/future provider fixture without route-logic change.
- [ ] T066 Wire provider selection from explicit request, trigger policy, or capability policy; never legacy task mode names.
- [ ] T067 Add full material API endpoints for handoff/prompt/skill/rule/report/task-context/log/artifact access under existing runtime bind/auth policy, including adopted-URL handoff context required by FR-041.
- [ ] T068 Add CronJob -> TeamTask creation with deterministic idempotency.
- [ ] T069 Add stale-heartbeat recovery -> TeamTask creation with deterministic idempotency.
- [ ] T070 Gate/remove runtime-v2 Independent Agent activation and hide/deprecate its dashboard surfaces while preserving migration evidence.
- [ ] T071 Implement TeamTask dependency DAG validation and readiness projection in the existing dependency/store owner; do not create ProviderJob dependency ids.
- [ ] T072 Project CronJob, Heartbeat, and Dependency state in runtime-v2 API/dashboard as required by FR-020.
- [ ] T073 Run Gate B focused tests.

## Gate C: Adopted URL and Direct Chat Sessions (deferred)

### Gate C safety tests

- [ ] T074 [P] Add ProviderSession/ConversationBinding + dynamic BrowserEndpoint tests: accepted normalize forms; credentials/userinfo/whitespace/bad scheme/bad port rejection; operator-surface mutation only; HTTP(S) redirect/cross-origin and returned-webSocket-authority hardening on add/update/start/health; WS(S) bounded connector validation; structured-only probe output; duplicate-identical reuse vs conflicting-settings rejection; physical-browser alias conflict; bind-time endpoint/cdp_url/physical_id drift; unsealed binding update/remove guard; no implicit default reassignment.
- [ ] T075 [P] Add global lease/page/fencing tests: one physical page/target can have only one provisional page_binding_id before Send; same-tab TeamTask vs Direct Chat second provisional writer rejects pre-Send. Known canonical writers use one global key. Lease takeover increments epoch; stale owner cannot record Send intent or mutate composer. Hold lease through assistant terminal + route/repair commit; route-away idle release then reacquire conflict -> WRITER_LEASE_BUSY; temporary/permanent quarantine retryability; restart/reload page-target revalidation failure -> reconciliation-only/seal; STOPPED with unresolved attempt seals rather than releases; released/sealed bindings stop blocking endpoint finalization.
- [ ] T076 [P] Add command-ledger SendAttempt projection tests for intent_recorded/submitted/accepted/unconfirmed/reconciled_accepted/reconciled_absent, including recovery of ambiguous intent_recorded/submitted through reconciliation before retry Also assert SendAttempt stores writer key/binding/lease_epoch and intent creation atomically fences stale lease owners before composer mutation.
- [ ] T077 [P] Add baseline evidence tests using pre-Send last-user-message id + turn count to distinguish identical historical text.
- [ ] T078 [P] Add adopted URL validation tests for exact canonical identity, wrong conversation/endpoint-profile binding -> `CONVERSATION_IDENTITY_MISMATCH`, no fallback/new-send on mismatch, and rejection of conversation_url for providers (Pi/Codex) without adopted_url capability (FR-034/FR-035).
- [ ] T079 [P] Add adopted URL role delegation tests: start-role keeps adopted binding; lazy other role gets own conversation; route-back resumes adopted URL.
- [ ] T080 [P] Add Direct Chat exactly-once composer tests, MaterialRef attachment-by-reference tests (FR-055), and assert Direct Chat messages are not parsed as route JSON outside a bound TeamTask role turn.
- [ ] T081 [P] Add DOM stream tests for user_message, assistant_text, visible_thinking, tool_activity, progress, error_banner, terminal, plus restart/reset marker followed by a fresh full projection. Include ChatGPT Web activity-header/shimmer selectors observed in production: `.group/activity-header`, `[class*="agent-activity"]`, `[class*="activity-header"]`, `.summary-lK7Lpm`, `.cadencedShimmerSweep-ICUAVH`, and `.cadencedShimmerHighlight-VcX29h`.
- [ ] T082 [P] Add adopted/direct-chat dependency regression using the Gate B DAG owner: waiting dependencies prevent browser contact/Send and expose dependency reason; DONE allows progress; STOPPED exposes `DEPENDENCY_FAILED`.

### Gate C implementation

- [ ] T083 Implement ConversationWriterLease global owner plus binding lifecycle: unique provisional physical page-target claim; canonical/global key; monotonic lease_epoch fencing; page recovery validation; full-turn lease lifetime through assistant terminal/route repairs; route-away idle lease release/reacquire; WRITER_LEASE_BUSY/LOST/QUARANTINED outcomes; terminal auto-release vs unresolved seal; task-bound idle binding remains live endpoint reference for route-back.
- [ ] T084 Extend existing command-ledger SendAttempt projection/reconciliation for Gate C and persist/validate writer_identity_key, owner_binding_id, and lease_epoch atomically at Send intent before composer mutation; do not add a second send-state store.
- [ ] T085 Implement ChatGPT Web adopted URL binding with exact identity verification at binding and Send boundary.
- [ ] T086 Enforce adopted URL mismatch block `CONVERSATION_IDENTITY_MISMATCH` with no new-chat fallback, endpoint reassignment, or blind resend.
- [ ] T087 Implement `POST /api/sessions/{session_id}/messages` using existing exactly-once composer/send owner and writer lease, accepting registered MaterialRefs by reference rather than arbitrary file copying.
- [ ] T088 Implement `GET /api/sessions/{session_id}/dom-stream` from browser DOM projection only, with monotonic session-local event_id and reset+full-projection behavior after restart rather than a second durable stream journal. Project visible ChatGPT Web activity-header/shimmer thinking/progress nodes with bounded selector metadata so dashboard/debug views can see where thinking is rendered.
- [ ] T089 Add dashboard Direct Chat UI backed by the existing BrowserEndpoint registry: list/select any registered endpoint; add external endpoints from port, `<ip>:<port>`, `host:port`, or explicit HTTP/WebSocket URL; expose update/remove/drain where existing commands permit; newly accepted endpoints appear without restart; show per-endpoint state, send status, live DOM stream, and dependency reason (FR-053/FR-054/FR-058-FR-064) Include explicit close for idle standalone Direct Chat sessions; task-bound role bindings stay live for route-back until task terminal/stop.
- [ ] T090 Project task kind (new-conversation/adopted-url/provider-job-backed), BrowserEndpoint registry identity/normalized CDP URL/health, plus adopted/direct-chat session state to `/api/tasks`, dashboard, and audit without sharing one conversation across role slots (FR-040/FR-054/FR-064).
- [ ] T091 Inspect and, where needed, extend the existing BrowserEndpoint backend owner for FR-058-FR-070: close any `/api/browser-endpoints` access-boundary gap found by T006; preserve normalize/add/duplicate behavior; apply safe CDP probe policy to add/cdp_url-update/start/health, including redirect/cross-origin rejection and admitted-authority pinning/rewriting of returned webSocketDebuggerUrl; expose only structured probe state; validate EndpointPool physical-browser conflict; reject cdp_url update while unsealed binding exists; remove drains/no-new-bindings until explicit release/seal then tombstones; keep mutation operator-surface-only and never auto-reassign sessions.
- [ ] T092 Run Gate C focused tests, including dynamic BrowserEndpoint normalization/add/probe/duplicate/in-use-retarget safety and dashboard no-restart selection.


### Gate C async WAIT/wake-back implementation

- [ ] T093 [P] Add AsyncContinuation tests: durable registration, spawn confirmation, WAIT accepted only with matching continuation_id, task status WAITING with waiting_code=async_callback, duplicate spawn/callback idempotency, `ASYNC_CALLBACK_CONFLICT` for conflicting callback payloads, timeout/cancel/fail states, same_role-only wake_policy, and no PAUSE usage.
- [ ] T094 [P] Add wake-back tests: callback schedules same-TeamTask and same-role wake against parent conversation binding/adopted URL; WAITING stores `reserved_async_wait` and blocks other writers; duplicate callback creates no second Send; parent terminal/cancelled/superseded blocks wake; identity mismatch blocks with `ASYNC_WAKE_IDENTITY_MISMATCH`; no new-chat fallback.
- [ ] T095 Implement AsyncContinuation store/API/CLI callback owner, including `POST /api/async-continuations/{continuation_id}/complete` or equivalent local command, MaterialRef/ProviderResult recording, timeout/cancel/failure policy, `reserved_async_wait` writer reservation, dashboard/API projection for all FR-081 fields, `ASYNC_CALLBACK_CONFLICT`, and idempotency.
- [ ] T096 Extend RouteContract builder/validator with reserved `WAIT` control route gated by confirmed AsyncContinuation; reject WAIT without registered continuation or with mismatched continuation id.
- [ ] T097 Extend ProviderJob/ProviderDispatcher so Pi/Codex/M365/custom scripts can run async, return spawn confirmation immediately, and wake via callback instead of keeping ChatGPT Web polling.
- [ ] T098 Implement wake scheduler for same TeamTask and same role using parent ConversationBinding/adopted URL plus Gate C `reserved_async_wait` reservation, writer lease reacquire, fresh Send baseline, and SendAttempt reconciliation.
- [ ] T099 Run async WAIT focused tests.

## Final Validation

- [ ] T100 Run narrow focused pytest set for implemented gate(s), then only broader affected suites.
- [ ] T101 Run `python -m compileall -q src/playwright_auto`.
- [ ] T102 Run `git diff --check -- src tests specs/003-team-only-dom-runtime`.
- [ ] T103 Review actual diff against spec/plan/contracts and repository one-owner rules.
- [ ] T104 Update quickstart only when implemented endpoint names/commands differ from reviewed contract.
- [ ] T105 Keep live ChatGPT/M365 smoke deferred until operator explicitly authorizes it.
