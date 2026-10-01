# Specification Quality Checklist: Team-only DOM Runtime

**Purpose**: Validate spec/plan consistency before implementation.
**Created**: 2026-09-30
**Reviewed**: 2026-10-01
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] Mandatory spec sections exist.
- [x] Requirements are phrased as behavior/contracts rather than implementation code.
- [x] Public task model is TeamTask only.
- [x] No PLAN/REVIEW hard requirement remains in RolePolicy.
- [x] One-role TeamTask behavior is covered without restoring legacy Solo mode.

## Role / Route Contract

- [x] Role canonicalization and grammar are defined.
- [x] Reserved terminal/status tokens cannot become role keys.
- [x] Duplicate roles collapse deterministically.
- [x] `roles` means initial team; `reachable_roles` is immutable creation-time closure.
- [x] Single-role automatic delegation to another generic role is supported without role-name branches.
- [x] RoleSlot declared/materialized lifecycle is defined.
- [x] DONE authority belongs only to closing_role.
- [x] Non-closing semantic DONE returns to closing_role.
- [x] Self-route default is forbidden and bounded self-route is explicit.
- [x] max_routes prevents multi-role ping-pong loops.
- [x] Prompt prose and JSON route enum share one RouteContract.

## Provider / Dependency Contract

- [x] ChatGPT Web is one provider, not the runtime itself.
- [x] DOM-only observation is scoped to browser-backed providers.
- [x] Provider execution modes are defined consistently.
- [x] long_timeout is a capability, not an execution mode.
- [x] ProviderResult status is not a TeamTask route.
- [x] ProviderJobs belong to one TeamTask.
- [x] Dependency graph references TeamTask ids only.

## Material / Report Contract

- [x] source_kind and material_type are separate vocabularies.
- [x] version is an opaque string.
- [x] content_ref is opaque/nullable.
- [x] Gate A `/api/report` renders bounded inline content without requiring full material dereference endpoint.
- [x] Missing/unsafe materials show structured visible errors.
- [x] provider_result/transcript may be fixture kinds in Gate A tests before real producers exist.

## Adopted URL / Direct Chat Contract

- [x] Adopted URL binds to one role slot.
- [x] Delegated role uses its own conversation; route-back resumes the adopted URL.
- [x] Shared conversation across different role slots is explicitly out of scope.
- [x] Gate C requires one global writer-identity lease owner for all browser writers, unique physical-page provisional ownership, and lease-epoch fencing at Send intent.
- [x] Multi-endpoint selection is registry-backed; 9202/9212/9222 are examples only.
- [x] Dynamic external endpoints may be added using port, `<ip>:<port>`, `host:port`, or explicit HTTP/WebSocket CDP URL through the existing BrowserEndpoint owner.
- [x] ProviderSession/ConversationBinding pin endpoint_id and never silently follow a changed default or retarget active work.
- [x] Writer exclusivity is global: known existing/adopted/resumed conversations acquire canonical lease before Send across ordinary TeamTask/adopted/direct-chat writers and distinct physical browsers/profiles.
- [x] Provisional writer key is allowed only on a positively verified new-chat surface; unresolved identity cannot Send.
- [x] Canonical collision quarantines the key so winner and losers cannot Send until conflicts terminalize; surviving owner takes a fresh baseline; unresolved abandon leaves conversation non-reusable.
- [x] Normal turn end/terminal task release writer lease deterministically; endpoint draining waits only on unsealed live/idle bindings.
- [x] Gate B browser providers resolve explicit targets through BrowserEndpointRegistry/endpoint_id rather than a second raw-address/default-only transport path.
- [x] Bind-time endpoint cdp_url/physical-browser evidence is revalidated before Send; unsealed bindings participate in update/remove guards.
- [x] Arbitrary host:port mutation remains operator-control-only; normalized URL validation and bounded CDP-specific probes fail closed and provider/material APIs cannot mutate endpoints.
- [x] Gate C reuses existing command-ledger send ownership.
- [x] Ambiguous submitted/unconfirmed Send is reconciliation-only.
- [x] Pre-Send baseline evidence is defined.
- [x] Direct Chat stream is DOM-only for browser providers and visible-node only.

## Gate / Plan Consistency

- [x] Gate A scope is core role/route/report contract only.
- [x] Gate A adds no new send path/store.
- [x] Gate B owns browser DOM cleanup, provider integration, material API, Cron/Heartbeat, Independent Agent deactivation.
- [x] Gate C owns ProviderSession, adopted URL, Direct Chat, writer lease, and ambiguous-send projection.
- [x] Gate C starts only after Gate A and Gate B acceptance.
- [x] Tasks were regenerated after the reviewed plan.

## External Review

- [x] M365 full probe succeeded on 9222.
- [x] M365 full probe succeeded on 9202.
- [x] Opus round 1 completed as adversarial broad review.
- [x] Opus round 2 classified blockers vs deferred concerns.
- [x] Opus round 3 resolved single-role automatic delegation generically.

## Readiness

- [x] No unresolved clarification markers remain.
- [ ] review-plan operator gate approved.
- [ ] implementation started.

- [x] ChatGPT Web DOM projection explicitly covers activity-header/shimmer thinking nodes and Business/Enterprise Allow-button permission handling.

**Technical reviewer status**: TECHNICAL PLAN PASS after Sonnet final focused verification of dynamic BrowserEndpoint behavior and async WAIT/wake-back. Operator approval is still required before implement.

## Opus Review Resolution Coverage

- [x] Single-role auto-delegation is preserved through immutable `reachable_roles` and lazy RoleSlots, without role-name branching.
- [x] DONE authority is closing-role-only; non-closing semantic DONE returns to `closing_role`.
- [x] Self-route is forbidden by default and bounded when explicitly enabled; task-level route budget prevents ping-pong loops.
- [x] Malformed/out-of-enum route output uses bounded repair and fails to `ROUTE_VALIDATION_EXHAUSTED`.
- [x] ProviderResult status is not a TeamTask route.
- [x] DOM-only observation is scoped to browser-backed providers.
- [x] Dependency graph is TeamTask-only, acyclic, fail-closed on missing ids, with `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` projection.
- [x] MaterialRef fields and Gate A `/api/report` behavior are consistent; full material dereferencing remains Gate B.
- [x] Gate C requires a global writer-identity lease/reservation and existing-ledger ambiguous-send reconciliation.
- [x] Direct Chat stream cursor is session-local with reset/full-projection recovery after restart, not a second durable event store.
- [x] Scheduler/heartbeat recovery is idempotent per stale episode and non-recursive for the same episode.

- [x] Gate B owns TeamTask dependency DAG validation/readiness projection; resumable BLOCKED prerequisites remain waiting rather than terminal failure.
- [x] Gate A has a crash/idempotency test for atomic route record + route_seq + lazy RoleSlot materialization before Send intent.
- [x] Semantic-route snapshot/version is frozen at creation; prompt-time drift blocks with `ROUTE_CONTRACT_DRIFT`.
- [x] Gate C lease protection covers all non-terminal send states: intent_recorded, submitted, unconfirmed.
- [x] Public creation has both test and implementation mapping for rejecting legacy Solo/Independent fields with migration guidance.

- [x] Gate A negative tests reject explicit start/closing roles outside initial roles even when reachable.
- [x] Gate A negative tests reject client attempts to supply runtime-derived state fields.
- [x] Reserved role tokens include dependency projection tokens `WAITING_DEPENDENCY` and `DEPENDENCY_FAILED`.
- [x] Semantic self-destination under forbidden policy rejects creation with `SELF_ROUTE_FORBIDDEN`.
- [x] Route contract divergence/out-of-set enum blocks with `ROUTE_CONTRACT_INVALID`.
- [x] ROUTE_CONTRACT_DRIFT recovery is replacement-task only; no in-place immutable contract refresh.
- [x] Gate C tests cover stream reset marker, Direct Chat MaterialRef attachment, unsupported-provider conversation_url rejection, and named identity mismatch block code.
- [x] TeamTask data model includes provider_id and public creation rejects all named runtime-derived/system-owned fields.
- [x] Exhausted route/repair budgets can continue only through existing task-control monotonic limit increase; counters never reset implicitly.
- [x] Existing task-control owner is identified end-to-end: dashboard_api controls endpoint -> worker task_control dispatcher -> cdpa_store request_control.
- [x] Gate A explicitly extends that owner with `increase_route_limits`; current source is documented as not already supporting route-limit mutation.
- [x] Tests cover accepted/rejected max_routes and max_route_repairs increases and prove route/self-route/repair counters are unchanged by control updates.
- [x] `increase_route_limits` tests cover eligible BLOCKED reasons, empty/mixed-invalid payloads, strict increase over prior limits and consumed counts, all-or-nothing rejection, unchanged limits/counters, and post-increase resume for both route and repair budgets.
- [x] Route-limit eligibility reads durable task `block_code` stored by the existing task owner.
- [x] Route-limit tests cover matching Idempotency-Key replay, idempotency conflict, and stale expected_task_version without reapplying mutation.
- [x] Source trace confirms same Idempotency-Key lookup precedes payload/task/version comparison; mismatches are conflicts.
- [x] Exhaustion blocks use `block_retryable=false`; Retry is invalid and Resume is admitted only after a limit increase above consumed count.
- [x] Route budget is checked before Send, so exhaustion retains no pending route output; recovery resumes the pre-Send role state.
- [x] RouteContract itself names `workflow_control_eligibility`, block_retryable=false, Retry invalidity, Resume admission, pre-Send budget gating, and no-pending-route recovery.
- [x] Exhausted closing-role non-DONE output maps directly to `ROUTE_BUDGET_EXHAUSTED` without route-repair consumption; post-increase Resume rebuilds enum and sends fresh.
- [x] Lease is held through assistant terminal DOM + route/repair commit; Send acceptance alone does not release it.
- [x] Task-bound idle bindings preserve route-back evidence while releasing writer key; terminal task auto-releases, unresolved attempt seals; released/sealed bindings do not block endpoint finalization.


## Async WAIT Review

- [x] WAIT route is runtime-owned async wait, distinct from PAUSE/operator intervention.
- [x] WAIT requires durable AsyncContinuation registration and confirmed external job spawn.
- [x] Callback completion is idempotent and wakes the same TeamTask/conversation binding without new-chat fallback.
- [x] Dashboard/API must expose async waiting state and callback/wake errors.
- [x] Async WAIT/wake-back external review complete after Sonnet final verification returned no blockers.


## Phase 1.5 clarity additions

- [x] Phase 1.5 browser-control recovery requirements added before Gate B.
- [x] Business/Enterprise Allow prompt handling is explicitly mandatory for ChatGPT Web role turns.
- [x] Accepted-send repair uses existing DOM state (`stop_visible`, `retry_visible`) without prompt resend.
- [x] Terminal route JSON from audit transcript/DOM plus expected report evidence must be reconciled, including DONE.
- [x] Minimal script/command timeout WAIT callback is scoped to existing TeamTask role conversations before full Gate C.
