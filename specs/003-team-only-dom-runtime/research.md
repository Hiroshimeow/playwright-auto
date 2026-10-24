# Research: Team-only DOM Runtime

## Decision: TeamTask is the only public work item

Runtime-v2 exposes TeamTask only. A one-role TeamTask replaces legacy Solo Task. Independent Agent is not a second public work model.

## Decision: Generic RolePolicy owns role normalization and reachability

Role names have no built-in semantic authority. PLAN, REVIEW, DEV, TEST, A, B, C, and custom keys all pass through the same algorithm.

Canonicalization: trim, uppercase, replace runs of spaces or `-` with `_`. Keys must match `^[A-Z][A-Z0-9_]{0,31}$` and must not collide with terminal route, TaskStatus, or dependency projection tokens (`WAITING_DEPENDENCY`, `DEPENDENCY_FAILED`). Valid role keys do not require registration.

`roles` is the initial team. `start_role` and `closing_role` default to the first normalized initial role and must belong to initial roles.

Runtime-v2 computes immutable `reachable_roles` at task creation from:

- normalized initial roles;
- optional `route_targets`;
- destinations declared by semantic route data for every discovered role, transitively.

This satisfies the operator requirement that a one-role REVIEW task can delegate to DEV, or a one-role DEV task to REVIEW, without role-name branches. The same mechanism works for A -> B.

Rejected alternatives:

- Dynamically mutate `roles` at route time: loses the submitted-team record and creates an open route set.
- Spawn child TeamTask for each delegated role: creates a second DONE authority, runtime dependency edges, extra crash windows, and fragmented dashboard rows.
- Accept arbitrary role keys from model output: allows typos/invented roles and breaks pre-Send schema validation.

## Decision: Lazy RoleSlot materialization in the same TeamTask

Every reachable role starts as a declared slot. A slot becomes materialized only when a validated route first reaches it. Route record, route sequence increment, and slot materialization are one durable operation before any Send intent for that role.

`reachable_roles` is frozen at creation and does not change when slots materialize.

## Decision: Closing-role authority and generic return-to-closer rule

DONE authority belongs only to `closing_role`.

If semantic route data for a non-closing role says an outcome is DONE, RouteContract maps that outcome to `closing_role` before prompt/schema generation. This is generic and does not special-case DEV or REVIEW.

## Decision: Route schemas are closed and loop-bounded

`allowed_routes(current) = reachable_roles - {current}`, plus DONE only when current is closing_role. Self-route is forbidden by default and may be enabled only with `bounded(max_self_routes=N)`.

Each task has a positive `max_routes` budget for non-terminal role-to-role transitions. Exhaustion blocks with `ROUTE_BUDGET_EXHAUSTED` unless the current closing role can legitimately finish with DONE.

## Decision: Route prose and JSON schema share one RouteContract

Prompt prose may mention only destinations present in the generated allowed-route enum. Grammar/reserved-word violations, unreachable destinations, or reachability-limit overflow fail closed before Send.

This directly fixes the observed REVIEW -> REVIEW loop caused by prose saying `defect -> DEV` while schema omitted DEV.


## Decision: Invalid route output uses bounded repair

Malformed route JSON or a route outside the closed enum never changes `reachable_roles`. Reuse the existing route-repair owner with a configured small repair limit. Each repair prompt is its own exactly-once Send. Exhaustion blocks with `ROUTE_VALIDATION_EXHAUSTED`.


## Decision: Dependency graph is TeamTask-only and fail-closed

Dependencies reference existing TeamTask ids only and must remain acyclic. Readiness requires every prerequisite TeamTask to be `DONE`. RUNNING/WAITING/PAUSED and resumable BLOCKED prerequisites project `WAITING_DEPENDENCY`; STOPPED projects `DEPENDENCY_FAILED`. Missing ids and cycles are rejected at validation. ProviderJob ids never enter the dependency graph. Dependency projection is recalculated whenever prerequisite state changes.

## Decision: Browser-backed observation is DOM-only

ChatGPT Web and future browser-backed providers use DOM projection as the only decision source for accepted user turn, transcript/completion, visible errors, composer state, and terminal state.

LISTEN/network and MutationObserver may at most wake a fresh DOM read; they never decide route/block/close/resend.

This rule does not apply to non-browser Pi/Codex execution, which reports through ProviderResult and MaterialRef.

## Decision: Provider status is not a TeamTask route

Provider execution is a backend action. ProviderResult statuses are `succeeded`, `timeout`, `failed`, `cancelled`, or `partial`. They are context for the next role turn and never directly transition TeamTask to DONE.

TeamTask routing remains owned by validated RouteContract output.

Execution modes are `browser_dom`, `acp`, `exec`, `http`, or `custom`; `long_timeout` is a provider capability, not an execution mode.

## Decision: Dynamic browser endpoints reuse the existing registry

Direct Chat and adopted browser sessions do not own a second endpoint list. Runtime-v2 reuses the current BrowserEndpoint command/registry chain: dashboard API `/api/browser-endpoints`, worker `browser_endpoint_add/update/remove/start/stop`, endpoint registry storage, `connection.py::normalize_cdp_endpoint`, and EndpointPool. Source inspection shows normalized-address uniqueness in the registry, external probe-before-first-registration, physical-browser identity derived from CDP `webSocketDebuggerUrl`, conflict state when the same physical browser is already owned, and active-execution retarget/remove guards. Gate C must extend this owner only where ConversationBinding-aware guards or probe redirect hardening are absent.

Accepted operator forms include a local port, `host:port`/`ip:port`, or explicit HTTP/WebSocket URL. Source inspection confirms `normalize_cdp_endpoint()` rejects credentials/userinfo, whitespace/control characters, unsupported schemes, malformed host/port, and ports outside 1..65535. External endpoints are attach-only. ProviderSession binds stable `endpoint_id` plus bind-time normalized `cdp_url` and physical-browser evidence. Writer exclusivity is globally keyed by verified provider+canonical-conversation identity across endpoint aliases and distinct physical browsers/profiles; all browser-backed TeamTask writers join that same namespace under Gate C. Endpoint mutation remains an operator-control capability, not a provider/material capability. T006 must record the actual `/api/browser-endpoints` bind/auth/access boundary and classify any hardening gap before Gate C implementation.

## Decision: Provider jobs are owned by TeamTasks, not dependency nodes

`depends_on_task_ids` references TeamTask ids only. A ProviderJob is owned by exactly one TeamTask. Other tasks wait on the owning TeamTask, not the ProviderJob id. Missing ids and cycles fail validation. DONE prerequisites are ready; RUNNING/WAITING/PAUSED/resumable BLOCKED prerequisites project `WAITING_DEPENDENCY`; STOPPED projects `DEPENDENCY_FAILED`. Projection is recomputed if a prerequisite resumes or changes state.
`ROUTE_BUDGET_EXHAUSTED` and `ROUTE_VALIDATION_EXHAUSTED` are resumable BLOCKED reasons and remain `WAITING_DEPENDENCY` until resumed/resolved. `ROUTE_CONTRACT_DRIFT` is non-terminal but has no in-place refresh; dependents remain waiting on the old task until it is resolved or STOPPED. Recreating a replacement TeamTask never rewires existing dependency ids automatically.

## Decision: MaterialRef separates source kind from material type

`source_kind`: file, inline, api, generated_prompt, route_json, provider_result, transcript.

`material_type`: report, handoff, prompt, skill, rule, task_context, log, artifact.

`version` is an opaque string. `content_ref` is opaque and nullable.

Gate A keeps `/api/report` compatible and renders bounded inline content. Full `/api/materials/*` dereferencing belongs to Gate B.

## Decision: CronJob and heartbeat replace Independent Agent

CronJob and stale-heartbeat recovery are deterministic services that create ordinary TeamTasks with deterministic idempotency keys. Legacy Independent Agent records may remain for migration evidence but cannot wake under runtime-v2.

## Decision: Adopted URL is a role-slot binding

A user-supplied existing conversation URL binds to the start-role slot for a browser provider that supports it. Exact conversation identity is verified before Send and again at the Send boundary.

When work delegates to another role, that lazily materialized slot receives its own provider conversation and the handoff through MaterialRef. Returning to the start role resumes the adopted URL. Sharing one conversation among multiple role slots is out of scope.

## Decision: Direct Chat is a control surface, not another task model

Direct Chat targets a ProviderSession/ConversationBinding. It reuses the same composer and exactly-once send boundary as TeamTask sends.

Before Gate C, two invariants are required:

- one global writer-identity lease owner shared by all browser-backed TeamTask/adopted/direct-chat writers, with unique physical-page provisional ownership and lease-epoch fencing;
- durable ambiguous-send reconciliation using the existing command ledger, with pre-Send baseline last-user-message id and turn count.

Direct Chat never owns a second retry/send implementation.

`max_route_repairs` is per active role turn and resets only after a valid route is committed to another role or the task closes.


## Semantic-route source

Semantic-route data comes from the existing role prompt/template configuration owner. RolePolicy does not define role semantics; it consumes a normalized, creation-time snapshot supplied by the existing prompt/route configuration owner identified during T003 and recorded during T006.

Resume never resets `max_routes`, `route_seq`, or `max_route_repairs` implicitly. Same-task continuation reuses the existing task-control owner to monotonically raise the exhausted limit; counters stay unchanged and the new limit must exceed consumed count. Otherwise the task remains BLOCKED or is replaced/stopped.


## Route-limit control owner

Existing operator task control is owned end-to-end by `dashboard_api.py` (`POST /api/tasks/{task_id}/controls`), `cdpa_worker.py::_apply_next_command` for `task_control`, and `cdpa_store.py::request_control`. The current action allowlist does not mutate route limits. Gate A extends this same owner chain with `increase_route_limits` rather than creating a new API/control subsystem. Every supplied limit must strictly increase its configured value and exceed consumed count. Empty or mixed-invalid payloads reject atomically; the action is valid only for `ROUTE_BUDGET_EXHAUSTED` / `ROUTE_VALIDATION_EXHAUSTED`. Rejection changes neither limit nor counter state. Accepted mutation preserves consumed counters; resume remains a separate control, including the repair path after a max_route_repairs increase.


## Idempotent retry semantics for route-limit control

The existing control chain already persists `block_code`, `block_retryable`, and `block_reason` on task state. `increase_route_limits` eligibility reads durable `block_code`, not dashboard text.

`dashboard_api.enqueue()` first looks up the command by Idempotency-Key alone; when a key already exists it compares kind/payload/task/expected version and returns the existing command only on an exact match, otherwise raising idempotency conflict. The worker checks command replay before expected-version validation and checks a fresh command's `expected_task_version` before mutation. Therefore an accepted route-limit command replay returns/reconciles the original command instead of failing the later strict-increase rule; a new stale-version command fails before limit mutation.


## Exhaustion recovery lifecycle

Route budget is checked before Send. A non-closing role with no remaining route budget blocks before any prompt, so there is no pending route output to preserve. A closing role with no remaining route budget gets only DONE in the route enum.
If that closing role emits a non-DONE route anyway, classify it directly as `ROUTE_BUDGET_EXHAUSTED`; do not spend `max_route_repairs` and do not retain the output.

Both `ROUTE_BUDGET_EXHAUSTED` and `ROUTE_VALIDATION_EXHAUSTED` use `block_retryable=false`; legacy Retry is not the recovery path. Existing Resume admission is extended through `cdpa_commands.py::workflow_control_eligibility` to reject exhaustion blocks until the relevant limit is raised above consumed count. After increase + Resume, route-budget recovery restarts from pre-Send role state with a normal Send; validation recovery issues the next repair prompt as a new exactly-once Send.

## Decision: Binding release, re-key, and endpoint drain

Gate C treats existing/resumed conversations differently from brand-new chats. If canonical conversation id is knowable before Send, the canonical global writer key is acquired first. A new conversation receives a durable `page_binding_id` and provisional physical-browser key before first Send. Canonical re-key atomically claims the global key without releasing provisional ownership until the current SendAttempt is terminal. Collision does not replay/cancel the in-flight attempt: the loser reconciles only, then seals.

Endpoint cdp_url update is rejected while an unsealed binding references that endpoint. Remove enters draining and blocks new bindings while unsealed references remain; final tombstone happens only after explicit release or sealing. Idle binding release is explicit and requires no non-terminal SendAttempt plus released writer lease. Hostnames are intentionally re-resolved per CDP operation; there is no cross-operation IP-pinning claim, and physical-browser identity is revalidated before Send.

## Decision: Canonical-key quarantine and binding lifecycle

A provisional writer lease is permitted only after positive verification of the provider's genuine new-conversation surface. An unresolved page that could already be an existing conversation never Sends under a provisional key. When a new conversation receives a canonical id, the runtime claims the canonical key atomically while retaining provisional ownership until the current SendAttempt is terminal. Any collision quarantines the canonical key: the winner is also barred from new Sends until every conflicting attempt terminalizes. Losers reconcile only, then seal. The surviving owner must capture a fresh baseline after quarantine clears. If an unresolved conflicting attempt is operator-abandoned without reconciliation, the canonical conversation remains non-reusable for runtime writes.

At normal turn end with no non-terminal SendAttempt, writer lease is released and the binding may become idle. DONE/STOPPED automatically releases and marks the binding released unless unresolved evidence requires sealing. Idle bindings may later reacquire the canonical key before Send. Endpoint removal guards use binding status: released/sealed references no longer prevent final tombstone, while bound/active/idle/blocked unsealed references keep the endpoint draining.

`page_binding_id` is durable bookkeeping, not sufficient page proof. Restart/reload recovery must revalidate the provider new-chat surface and stored pre-Send evidence; if exact recovery cannot be proven while an attempt is non-terminal, no new Send occurs and recovery remains reconciliation-only/seal-on-failure.

## Decision: Page exclusivity and lease fencing

A provisional new-chat binding is not unique merely because its binding id is unique. Before Send, Gate C atomically claims the live `(provider_id, physical_browser_id, page_target_identity)` so one physical page/tab has at most one provisional writer. `page_binding_id` durably records that claim and recovery must revalidate the target; failure is reconciliation-only.

Every writer lease grant/transfer increments `lease_epoch`. Send-intent persistence and lease-owner/epoch verification are one admission step before composer mutation. A stale owner that wakes after heartbeat takeover cannot record a new intent or Send; it blocks with `WRITER_LEASE_LOST`. The lease is held through assistant terminal state and route/repair completion, not released at user-message acceptance.

Task-bound browser bindings keep endpoint/conversation evidence while idle for route-back but release the writer key after a completed turn. Reacquire failure uses `WRITER_LEASE_BUSY` or `WRITER_LEASE_QUARANTINED`. DONE/STOPPED releases automatically unless unresolved send evidence requires sealing. Endpoint update/remove uses one live-reference set (`bound|active|idle|blocked`); `released|sealed` no longer block finalization.


## Decision: WAIT is runtime-owned async wait, not PAUSE

Long provider work must not force ChatGPT Web to hold the response open while CDPA polls M365/CDP. The runtime registers an AsyncContinuation, spawns the provider/script, confirms job identity, and then permits a reserved `WAIT` route. WAIT sets the parent task to `WAITING` with `waiting_code=async_callback` and releases the current browser turn. PAUSE remains operator-intervention state and is not used for normal async completion.

Provider callbacks are idempotent and record ProviderResult/MaterialRefs. Wake-back targets the same TeamTask and parent conversation binding/adopted URL. If the original conversation identity cannot be verified, wake blocks rather than opening a new chat. This mirrors Pi/Codex session-resume behavior while giving ChatGPT Web a callback mechanism instead of long polling.


## Decision: Async WAIT is Gate C because wake-back needs writer reservation

ProviderJob async execution may exist in Gate B, but ChatGPT Web `WAIT` route plus same-conversation wake-back belongs to Gate C. During WAITING, the canonical writer key enters `reserved_async_wait`: the browser/page lease is not active, but the canonical conversation is reserved for the registered continuation and rejects other writers. The continuation wake consumes the reservation and reacquires the active ConversationWriterLease before composer mutation. Cancel, timeout, failure, or supersede must durably release or seal the reservation before any other writer may proceed.
