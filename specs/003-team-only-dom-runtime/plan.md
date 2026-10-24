# Implementation Plan: Team-only DOM Runtime

**Branch**: 003-team-only-dom-runtime
**Date**: 2026-10-01
**Spec**: [spec.md](./spec.md)

## Spec Kit Workflow Status

- `specify`: complete.
- `review-spec`: complete after full markdown review and three external Opus debate rounds through M365.
- `plan`: revised to match the reviewed spec and contracts.
- `review-plan`: TECHNICAL PLAN PASS after Sonnet final focused verification; operator approval remains the human gate before implementation.
- `tasks`: regenerated from this revised plan.
- `implement`: not started.

## Summary

Runtime-v2 converges on one public work model: TeamTask. `roles` is the normalized initial team; one-role TeamTask replaces legacy Solo Task. RolePolicy has no PLAN/REVIEW/DEV/TEST branches. It computes immutable `reachable_roles` at creation from initial roles, optional route_targets, and semantic-route destinations, so a single-role REVIEW task may delegate to DEV (or A to B) without mutating the submitted roles or spawning child TeamTasks.

DONE authority belongs only to `closing_role`. A non-closing role whose semantic outcome is DONE hands control back to closing_role. Route schemas are closed, self-route is forbidden by default, and a task-level route budget prevents role ping-pong loops.

ChatGPT Web is one browser-backed provider. Browser-backed decisions are DOM-only. Pi/Codex/custom providers use ProviderResult/MaterialRef and never inherit ChatGPT DOM logic.

Implementation is split into Gate A/B/C. Gate A changes no send-state owner or interactive send path.

## Technical Context

- **Language**: Python 3.11+, JavaScript dashboard assets.
- **Runtime**: existing CDPA legacy worker/store plus runtime-v2 API/dashboard owners.
- **Target**: FJP Windows local runtime.
- **Testing**: focused pytest first; broader suites only when owner boundaries warrant them.
- **Safety invariants**: exactly-once Send, ambiguous-send reconciliation, exact conversation identity, preservation of unrelated dirty work.
- **Dependency model**: TeamTask ids only.

## Constitution / Repository Rule Check

This is SPEC scope because it changes public contracts, durable routing state, provider boundaries, recovery/scheduler behavior, and future interactive send surfaces.

The design follows `.agents/rules/project.md`:

- one concept / one owner;
- no second worker engine;
- role/provider details stay at their boundary;
- exactly-once Send remains owned by the existing command ledger;
- obsolete public Solo/Independent paths are removed or hidden rather than duplicated;
- new owners are introduced only for materially distinct responsibilities.

## Core Invariants

1. Runtime-v2 public work item is TeamTask only.
2. `roles` records initial operator intent and never mutates after create.
3. RolePolicy computes/fixes `reachable_roles` at create; role slots only transition declared -> materialized.
4. Role names do not grant routing or DONE authority.
5. DONE belongs only to closing_role.
6. Prompt prose and route enum come from one RouteContract.
7. Self-route default is forbidden; bounded self-route is explicit.
8. Non-terminal role-to-role transitions consume `max_routes`; exhaustion blocks instead of looping.
9. TeamTask dependency graph contains TeamTask ids only.
10. ProviderResult status is not a TeamTask route.
11. Browser-backed providers decide from fresh DOM only.
12. Gate A validation runs before the existing Send-intent boundary and does not add a send path/store.
13. Gate C must have one global writer-identity lease owner shared by every browser-backed TeamTask/adopted/direct-chat writer, unique provisional page ownership, lease-epoch fencing at Send intent, and command-ledger ambiguous-send reconciliation before enabling interactive browser Send. DOM stream cursors are session-local; after restart clients receive reset + fresh full projection rather than requiring a second durable stream journal.

## Artifact Set

```text
specs/003-team-only-dom-runtime/
|-- spec.md
|-- plan.md
|-- research.md
|-- data-model.md
|-- quickstart.md
|-- tasks.md
|-- checklists/requirements.md
`-- contracts/
    |-- team-task-contract.md
    |-- route-contract.md
    |-- dom-observation-contract.md
    |-- provider-contract.md
    |-- material-api-contract.md
    |-- material-report-contract.md
    |-- scheduler-heartbeat-contract.md
    |-- adopted-url-contract.md
    `-- direct-chat-contract.md
```

## Gate A: Core Runtime Contract MVP

**Semantic-route ownership**: role semantics remain owned by the existing prompt/template configuration owner inspected in T003 and recorded in T006. RolePolicy only consumes and freezes its normalized snapshot; it does not introduce a second role-semantics owner.

Gate A includes creation-time semantic-route snapshot/version (`route_contract_id`) and prompt-time drift blocking with `ROUTE_CONTRACT_DRIFT`.
Gate A also fixes invalid-route repair semantics to per-active-role-turn `max_route_repairs`, resetting only after a valid route commits to another role or the task closes and blocking with `ROUTE_VALIDATION_EXHAUSTED` when exhausted.
Route budget is gated before Send; non-closing exhaustion holds no pending route, closing exhaustion exposes only DONE. Both exhaustion codes use block_retryable=false and recover only by accepted limit increase followed by Resume admitted after the relevant limit exceeds consumed count.
If a closing role emits non-DONE under an exhausted budget, classify it directly as `ROUTE_BUDGET_EXHAUSTED` without consuming repair attempts. Resume after max_routes increase rebuilds the route enum and starts a new Send; no blocked route output is committed later.
Resume does not reset exhausted route/repair counters. Same-task continuation reuses the existing task-control owner for monotonic limit increase above consumed count; no new control API family is introduced.
The concrete owner chain is `dashboard_api.py` controls endpoint -> `cdpa_worker.py::_apply_next_command(kind="task_control")` -> `cdpa_store.py::request_control`, with admission shared through `cdpa_commands.py::workflow_control_eligibility`. Inspection confirms the current action set has no route-limit mutation, so Gate A extends this same chain with `increase_route_limits` and exhaustion-aware Resume admission; it does not assume the capability already exists.
Existing command semantics are preserved: `dashboard_api.enqueue()` deduplicates matching Idempotency-Key requests, worker replay reconciliation runs before strict validation, fresh `expected_task_version` is checked before mutation, and route-limit eligibility reads the store's durable `block_code`.

### Scope

- Generic RolePolicy canonicalization, duplicate collapse, reachable-role closure, lazy RoleSlot materialization, max_routes, and projection.
- TeamTask-only runtime-v2 public creation and legacy bridge behavior.
- RouteContract/prose/schema consistency, closing-role DONE authority, self-route policy, route budget.
- MaterialRef registration and `/api/report` compatibility for file/non-file handoffs using bounded inline display content.

### Existing owners to inspect first

```text
src/playwright_auto/hcuw_runtime/task_runner.py
src/playwright_auto/hcuw_runtime/state.py
src/playwright_auto/hcuw_runtime/api_server.py
src/playwright_auto/hcuw_runtime/dashboard_server.py
src/playwright_auto/hcuw_runtime/legacy_audit.py
src/playwright_auto/cdpa_store.py
src/playwright_auto/cdpa_prompts.py
src/playwright_auto/cdpa_routes.py
src/playwright_auto/cdpa_workflow_agents.py
```

A small RolePolicy or MaterialRef owner may be introduced only if no existing owner cleanly holds that responsibility and the new owner centralizes duplicated logic rather than creating a parallel path.

### Send boundary

Gate A does not modify send execution/retry ownership. Route validation and role-slot materialization complete before the existing command-ledger Send intent. No new SendAttempt store is introduced.

Gate A does not redesign dependency semantics: non-empty `depends_on_task_ids` continue through the existing dependency owner. Gate B is responsible for centralizing/validating the TeamTask DAG and runtime-v2 readiness projection.

### Acceptance

- one-role REVIEW can precompute reachable DEV from semantic route data and lazy-materialize DEV when routed;
- generic A -> B behaves identically;
- one-role task with no extra reachable roles may close directly if its initial role is closing_role;
- non-closing DONE semantic outcome returns to closing_role;
- repeated self-route cannot happen by default;
- max_routes exhaustion blocks;
- `/api/report` renders non-file MaterialRefs without blank dashboard content.


- route_contract_id/semantic-route snapshot is frozen at creation and drift blocks before Send with `ROUTE_CONTRACT_DRIFT`.
## Phase 1.5: Browser Control Recovery and Script WAIT Callback

Starts only after Gate A acceptance and before Gate B. This gate is deliberately narrow: reuse existing ChatGPT Web DOM projection and existing CDPA permission/retry/report-repair owners. Do not add a new DOM crawler, new browser transport owner, Direct Chat, adopted URL, dynamic endpoint UI, or full Gate C writer lease.

Scope:

- Business/Enterprise MCP/tool `Allow` prompt handling for every ChatGPT Web TeamTask role turn.
- Accepted-send repair based on current DOM state: `stop_visible=true && retry_visible=false` means still active; stable provider Retry means click Retry once for the exact accepted user turn; otherwise reconcile/repair without resending the original prompt.
- Terminal route reconciliation from existing audit transcript/DOM assistant text and expected report evidence, including DONE.
- Minimal script/command timeout AsyncContinuation: confirmed spawn -> `WAIT` -> callback -> same-TeamTask/same-role wake on the parent ChatGPT Web conversation.

Acceptance:

- no Business/Enterprise `Allow` prompt blocks a normal role turn when ownership/composer safety is clean;
- retry/error timeout no longer requires the operator to type repair manually;
- an assistant transcript containing valid `{"route":"DONE",...}` plus the expected report file closes the task instead of leaving it in REVIEW `wait_response`;
- long scripts can return `WAIT` and callback without holding the ChatGPT response open.

## Gate B: Browser/Provider and Scheduler Integration

Gate B starts only after Phase 1.5 acceptance. Gate B also owns TeamTask dependency DAG validation plus `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` readiness projection.
For `ROUTE_CONTRACT_DRIFT`, the old task stays non-terminal BLOCKED until operator resolution/STOP; recreating a replacement TeamTask does not rewire dependency ids.
Gate B dashboard/API projection also exposes CronJob, Heartbeat, and Dependency state required by FR-020.

Starts only after Gate A acceptance.

### Scope

- Browser-provider DOM-only source cleanup in audit/dashboard/worker decision paths, including visible activity-header/shimmer progress capture and Business/Enterprise MCP/tool `Allow` prompt handling carried forward from the existing CDPA permission handler.
- ProviderDispatcher and ProviderJob contracts.
- execution modes: browser_dom, acp, exec, http, custom; long_timeout is a capability.
- ProviderResult statuses: succeeded, timeout, failed, cancelled, partial.
- ChatGPT Web, Codex, Pi, custom/future provider wrappers.
- Full material dereferencing/API endpoints for provider curl/ACP/exec workflows.
- CronJob and heartbeat recovery create ordinary TeamTasks with deterministic idempotency.
- legacy Independent Agent activation is removed/gated from runtime-v2 and migration records stay inert.

### Provider invariant

Provider completion never closes or routes a TeamTask directly. ProviderResult becomes task context/material; the next route still comes through RouteContract. AsyncContinuation is the exception that wakes a waiting same-TeamTask turn after a prior WAIT route, but it still uses RouteContract and writer-lease safety for the wake Send.

Browser-backed Gate B providers resolve browser targets through the existing BrowserEndpoint registry/endpoint_id owner when explicitly bound; raw-address/default-only transport resolution is not a second model. Gate B tests/provider wrapper must cover explicit endpoint_id resolution. Gate C adds interactive endpoint management and ProviderSession bindings on top of the same transport owner.

## Gate C: Adopted URL, Direct Chat, and Async WAIT Sessions

Starts only after Gate A and Gate B acceptance.

### Scope

- ProviderSession and ConversationBinding.
- adopted existing conversation URL binding for capable browser providers.
- Direct Chat dashboard send/stream surface.
- registry-backed multi-endpoint session switching with dynamic endpoint add/update/remove/select; 9202/9212/9222 are examples, not hard-coded choices.
- reuse the existing BrowserEndpoint owner: `dashboard_api.py` `/api/browser-endpoints` -> worker `browser_endpoint_add/update/remove/start/stop` -> endpoint registry; reuse `connection.py::normalize_cdp_endpoint`, EndpointPool probe/physical-browser identity, and active-execution drain/retarget guards. Phase-1 inspection must record each capability as already supported or requiring extension; Gate C extends the same owner where ConversationBinding-aware guards or probe hardening are missing.

### Entry invariant 1: single writer

Gate C entry invariant is a global ConversationWriterLease, not a per-binding mutex. Canonical/provisional writer keys have one owner and a monotonically fenced lease_epoch. Page-level provisional ownership is 1:1 with the live physical page target. Send intent atomically checks owner+epoch before composer mutation. Takeover increments epoch and cannot cross an in-flight logical turn.

ProviderSession/ConversationBinding pins a stable `endpoint_id`. The dashboard lists the current BrowserEndpoint registry dynamically and may add external endpoints from `port`, `host:port`, `ip:port`, or explicit HTTP/WebSocket CDP URL input. Adding an external endpoint probes health before first registration. Existing sessions are never silently reassigned when defaults or endpoint settings change.

Writer identity is stronger than endpoint identity. Every browser-backed TeamTask writer, not only adopted URL/Direct Chat, participates in one global writer-lease namespace. Existing/adopted/resumed conversations must acquire canonical `(provider_id, canonical_conversation_id)` before Send. Only positively verified provider new-conversation surfaces use durable provisional `(provider_id, physical_browser_id, page_binding_id)`; unresolved identity cannot Send provisionally. When canonical id appears, claim canonical key atomically while retaining provisional lease through the current SendAttempt; collision quarantines the canonical key so the winner also cannot Send while any conflicting attempt is non-terminal. Losers reconcile only then seal; after quarantine clears the surviving owner takes a fresh baseline. Unreconciled abandon leaves the canonical conversation non-reusable for runtime writes. Canonical uniqueness is global across endpoint aliases and distinct physical browsers/profiles. Bind-time evidence stores endpoint_id + normalized cdp_url + physical_browser_id; endpoint changes or aliases cannot silently move an existing binding. Endpoint cdp_url update is rejected while any unsealed binding references the endpoint. Remove marks the endpoint draining while such bindings exist, preventing new bindings; tombstone/removal may finalize only after bindings are explicitly released or sealed. A TeamTask binding routed away becomes idle only after assistant terminal DOM + valid route commit and releases its writer lease while keeping endpoint/conversation evidence for route-back. It remains a live endpoint reference until task terminal/stop. Standalone Direct Chat may explicitly close an idle session. DONE/STOPPED auto-release task bindings; unresolved attempts seal instead. Released/sealed bindings do not block endpoint finalization.

Endpoint mutation stays on the existing operator-control surface. Arbitrary host input is parsed only by `normalize_cdp_endpoint`; providers/material callers receive no mutation authority. All endpoint add/update/start/health probe paths use one CDP-specific safety policy. HTTP(S) uses bounded `/json/version` checks, fail-closed redirect/cross-origin handling, and pins/rewrites returned WebSocket authority to the admitted authority; WS(S) uses bounded CDP connector validation. Plaintext remote CDP is an explicit operator trust choice, never automatic discovery.
If an unresolved attempt cannot be reconciled, operator stop/abandon may release runtime resources only by sealing that binding as non-reusable; it never permits replay of the unresolved attempt.

### Entry invariant 2: ambiguous-send reconciliation

Reuse the existing command ledger. Expose a logical SendAttempt projection:

`intent_recorded -> submitted -> accepted | unconfirmed -> reconciled_accepted | reconciled_absent`; ambiguous intent_recorded/submitted recovers through reconciliation/unconfirmed before retry

Before Send capture baseline last-user-message id and turn count. `unconfirmed` never resends. Only proven reconciled_absent may create a new attempt id.

### Role/conversation policy

The adopted URL binds to the start-role slot. A lazily materialized different role gets its own provider conversation and receives handoff through MaterialRef. Returning to the adopting role resumes the original adopted URL. Sharing one conversation among multiple role slots is out of scope.

## Complexity Tracking

| Added concept | Why required | Simplification / owner rule |
|---|---|---|
| reachable_roles | Preserve automatic single-role delegation without open-ended/invented routes | Computed once by RolePolicy, immutable |
| RoleSlot declared/materialized | Defer role conversation creation until route is used | Same TeamTask; no child-task workflow |
| max_routes | Prevent multi-role ping-pong loops | One generic task-level budget |
| ProviderDispatcher/ProviderJob | Multiple backend execution types | Gate B only; routing remains separate |
| ProviderSession/ConversationWriterLease | Direct Chat, adopted URL, and async WAIT wake-back create multiple potential writers | Gate C only; global writer identity plus async reservation |
| SendAttempt projection | Needed for new Gate C writer surface under exactly-once safety | Projection of existing command ledger, not a new store |

## External Review Evidence

M365 full probes succeeded on both 9222 and 9202 on 2026-10-01. Three Opus rounds were run:

1. broad adversarial review: PLAN REVISE;
2. blocker/defer/reject classification: PLAN REVISE;
3. targeted debate preserving single-role automatic delegation: recommended immutable reachable_roles + lazy same-TeamTask RoleSlots, rejecting child TeamTasks and open dynamic role growth.

The artifacts in this revision incorporate the accepted findings from all three rounds while rejecting suggestions that contradicted the operator's explicit single-role delegation requirement.

## Review-plan Gate Criteria

Assistant consistency review may mark the plan technically PASS only if:

- spec, plan, research, data-model, contracts, quickstart, checklist, and tasks use the same role/reachability/DONE/dependency/material/provider vocabulary;
- all requirement ids and task ids are unique/continuous;
- every Gate A invariant has at least one test task and implementation task;
- no Gate B/C implementation is accidentally required by Gate A;
- no source code implementation has started.

Operator approval remains the explicit Spec Kit review-plan gate before implementation.
