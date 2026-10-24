# Quickstart: Team-only DOM Runtime

## Current Spec Kit stage

- specify: complete
- review-spec: complete
- plan: revised after three Opus review rounds
- review-plan: TECHNICAL PLAN PASS after Sonnet final focused verification; operator approval required
- tasks: regenerated from revised plan
- implement: not started

Runtime servers may remain stopped during documentation review.

## Static prerequisites

```powershell
Get-Content .specify/feature.json
.\.specify\scripts\powershell\check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks
```

Expected active feature: `specs\\003-team-only-dom-runtime`.

## Gate A validation after implementation

Validate at minimum:

- role canonicalization/grammar/reserved tokens;
- duplicate collapse and start/closing defaults;
- initial roles remain immutable;
- reachable_roles closure from roles + route_targets + snapshotted semantic route data;
- route_contract_id is frozen at creation and prompt-time config drift blocks with ROUTE_CONTRACT_DRIFT; prose/schema or enum/reachability divergence blocks with ROUTE_CONTRACT_INVALID;
- single REVIEW -> DEV lazy RoleSlot and generic A -> B equivalent;
- closing-role-only DONE and non-closing DONE -> closing_role mapping;
- default self-route forbidden, semantic self-destination rejected with `SELF_ROUTE_FORBIDDEN`, and bounded self-route behavior;
- max_routes exhaustion -> ROUTE_BUDGET_EXHAUSTED; resume never resets exhausted route/repair counters; same-task continuation uses existing task-control to monotonically raise the relevant limit above consumed count;
- malformed/out-of-enum route repair is bounded per active role turn and exhausts to ROUTE_VALIDATION_EXHAUSTED;
- lazy RoleSlot crash/replay keeps route + route_seq + first materialization atomic/idempotent before Send intent;
- legacy Solo/Independent public fields rejected;
- `/api/report` renders file/non-file MaterialRef bounded content and structured errors;
- Gate A does not introduce a new send path/store and all route validation completes before existing Send intent.

## Gate B validation after implementation

- ChatGPT Web/browser-backed decisions are DOM-only.
- Provider execution modes are consistent; long_timeout is a capability.
- ProviderResult statuses never become TeamTask routes.
- ProviderJob belongs to exactly one TeamTask and never becomes a dependency node;
- dependency DAG rejects cycles/missing ids and projects `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` deterministically.
- material API serves only registered material under existing runtime bind/auth policy.
- CronJob/Heartbeat create TeamTasks idempotently.
- Independent Agent cannot wake under runtime-v2.

## Gate C validation after implementation

- endpoint selector is registry-backed, not hard-coded to 9202/9212/9222;
- add `9222`, `<ip>:<port>`, `host:port`, and explicit HTTP/WebSocket CDP URLs through the existing browser-endpoint API and verify normalized `cdp_url`;
- external endpoint add probes availability before first registration; duplicate-identical add reuses existing endpoint while conflicting settings reject;
- reject unsafe/malformed endpoint input; HTTP(S) admission stays CDP-specific/bounded and rejects redirect/cross-origin substitution; provider/material callers cannot mutate endpoint registry;
- known existing/adopted/resumed conversations acquire global canonical-conversation lease before Send across ordinary TeamTask/adopted/direct-chat writers and across distinct physical browsers/profiles;
- provisional lease is allowed only on a positively verified new-chat surface; unresolved existing identity cannot Send;
- canonical re-key collision quarantines the conversation key so even the winner cannot Send until conflicting attempts terminalize; loser reconciliation-only then sealed; surviving owner takes fresh baseline; unresolved abandon makes canonical conversation non-reusable;
- add/update/start/health all use the same bounded probe safety; HTTP(S) returned WebSocket authority cannot pivot away from the admitted authority;
- bind-time endpoint_id/cdp_url/physical_id drift or update/remove of an unsealed binding blocks/requires rebind rather than silent retarget;
- newly added endpoint becomes selectable without restart; cdp_url update rejects while unsealed binding exists; remove drains/no-new-bindings until explicit idle close/unbind or sealing allows final tombstone; no silent reassignment;
- one global writer-identity lease shared by ordinary TeamTask/adopted/direct-chat browser writers, plus unique physical-page provisional ownership and lease-epoch fencing before composer mutation;
- adopted URL exact identity at binding and Send boundary;
- command-ledger SendAttempt projection handles submitted/unconfirmed without blind resend;
- pre-Send baseline includes last-user-message id and turn count;
- start-role adopted URL persists while delegated role uses its own conversation;
- Direct Chat reuses the existing send owner and is never a second retry implementation;
- DOM stream shows visible provider nodes only and does not expose hidden reasoning;
- task dependencies reference TeamTask ids only.

## Live smoke gate

Do not run real ChatGPT/M365 workflow smoke until the operator explicitly authorizes it. When authorized, test one bounded scenario at a time and capture `/api/tasks`, relevant material/report state, DOM projection, route history, and send evidence.


- hold writer lease through assistant terminal DOM and route/repair commit, not just Send acceptance; route-away releases writer key but preserves idle task binding for route-back; reacquire conflict blocks with WRITER_LEASE_BUSY/QUARANTINED.


## WAIT / async callback smoke

When Gate C async continuation is implemented, verify that a ChatGPT Web role can spawn a long provider job, return `WAIT`, stop the browser turn, reserve the canonical writer key as `reserved_async_wait`, and later wake the same task/role/conversation from a callback result without polling CDP during the wait.

Expected checks:

- WAIT appears only after durable AsyncContinuation registration and confirmed job spawn.
- Task status is WAITING with `waiting_code=async_callback`, not PAUSED.
- Duplicate callback does not create a second Send.
- Wake verifies the original conversation binding and never falls back to a new chat.


## Phase 1.5 operator gate

Before starting Gate B, run Phase 1.5 clarity/implementation for browser-control recovery:

- ChatGPT Web Business/Enterprise Allow prompts are clicked by the existing guarded permission handler.
- accepted-send timeout/retry recovery uses existing DOM state; `stop_visible=true` and `retry_visible=false` remains active wait, while stable retry clicks provider Retry once.
- terminal route JSON already visible in audit transcript/DOM, including DONE, is reconciled with the expected report and RouteContract instead of leaving the task in wait_response.
- long scripts may return WAIT and callback to wake the same TeamTask/same role.
