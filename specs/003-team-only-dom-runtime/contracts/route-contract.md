# Contract: RouteContract

## Inputs

- current_role
- reachable_roles
- route_contract_id / semantic-route snapshot version
- closing_role
- semantic_routes
- self_route_policy
- max_routes / current route_seq

## Allowed-route derivation

1. Start with `reachable_roles - {current_role}`.
2. Add `DONE` only if current_role == closing_role.
3. Add current_role only when `self_route_policy=bounded(max_self_routes=N)` and the bound is not exhausted.
4. Default self_route_policy is `forbidden`. Under bounded policy, every self-route increments route_seq, consumes max_routes, and increments the current role's cumulative task-lifetime self-route counter. A creation-time semantic self-destination under forbidden policy is rejected with `SELF_ROUTE_FORBIDDEN`.
5. Prompt prose may mention only allowed routes.

If semantic route data for a non-closing role maps an outcome to DONE, the builder rewrites that outcome to closing_role before rendering prompt prose/schema.

## Loop budget

Non-terminal role-to-role routes increment `route_seq`. Route budget is checked before every new role Send/route prompt.

- If `route_seq < max_routes`, normal allowed-route derivation applies.
- If `route_seq >= max_routes` and current_role is not closing_role, task becomes BLOCKED **before Send** with `ROUTE_BUDGET_EXHAUSTED`; no route output/pending route exists.
- If `route_seq >= max_routes` and current_role is closing_role, the rendered closed enum contains only `DONE`. If the model nevertheless emits any non-DONE route, validation classifies it directly as `ROUTE_BUDGET_EXHAUSTED`, not as `ROUTE_VALIDATION_EXHAUSTED`; no repair attempt is charged and the invalid output is not retained for later commit.

Both route/repair exhaustion blocks use `block_retryable=false`; legacy `retry` is not their recovery control.

After an accepted route-limit increase, ordinary `resume` is admitted only when the relevant limit is strictly above its consumed count. Route-budget recovery returns to the same durable pre-Send role state, rebuilds the enum from the frozen RouteContract, and performs a normal new Send; it never commits a previously blocked route output. Validation-exhaustion recovery issues the next repair prompt as a new exactly-once Send.

## Fail-closed behavior

Task creation fails when role grammar/reserved-token validation fails or reachable-role closure exceeds its configured bound. Prompt-time checks are defensive assertions over the frozen task contract.

Prompt generation blocks before Send when:

- `route_contract_id` / semantic-route snapshot drift is detected (`ROUTE_CONTRACT_DRIFT`);
- prose/schema destinations diverge: block with `ROUTE_CONTRACT_INVALID`;
- a route enum contains a value outside the precomputed reachable set plus allowed terminal tokens: block with `ROUTE_CONTRACT_INVALID`.

## Generic example

Given:

```json
{
  "reachable_roles": ["A", "B", "C"],
  "current_role": "C",
  "closing_role": "B",
  "semantic_routes": {"clean": "DONE", "needs-review": "A"}
}
```

The builder rewrites `clean -> B` because C is not the closing role. Allowed routes for C are `["A","B"]`. DONE appears only when B is current.

## Observed REVIEW-loop regression

For initial `roles=["REVIEW"]`, if REVIEW semantic route data declares `defect -> DEV`, RolePolicy freezes reachable_roles as `["REVIEW","DEV"]`. REVIEW may route DEV; DEV may not DONE unless DEV is closing_role. With closing_role=REVIEW, a DEV semantic DONE outcome becomes a return route to REVIEW.

## Invalid output and repair

Malformed route JSON or a route outside `allowed_routes` must not widen `reachable_roles` or be treated as a self-route. Reuse the existing bounded route-repair path. Each repair prompt crosses the normal exactly-once Send boundary as a distinct attempt. After `max_route_repairs` is exhausted, block with `ROUTE_VALIDATION_EXHAUSTED`. `max_route_repairs` is per active role turn and resets only after a valid route commits to another role or the task closes. Resume alone never resets the repair count; continuing after exhaustion requires an explicit operator increase of `max_route_repairs`.

## WAIT control route

`WAIT` is a reserved control route, not a role. It is included in a route schema only after the runtime has durably registered an `AsyncContinuation` and confirmed external job spawn before rendering or validating the route output.

A valid WAIT output must reference the registered `continuation_id` and leaves the task in `status=WAITING`, `waiting_code=async_callback`. The current browser turn is complete after WAIT is accepted; the worker must stop polling the long external provider result in this ChatGPT response.

WAIT is fail-closed:

- no registered continuation -> route validation rejects WAIT;
- continuation id mismatch -> `ASYNC_CONTINUATION_MISMATCH`;
- duplicate accepted WAIT for the same continuation -> idempotent replay, no second job/wake;
- parent task terminal/cancelled/superseded -> callback records result but does not wake.

## Drift recovery

`ROUTE_CONTRACT_DRIFT` never mutates the frozen task contract in place. In-place route-contract refresh is not supported in this feature; recovery requires a replacement TeamTask created from the desired configuration.


## Route-limit control

Reuse and extend the existing task-control chain; do not create a separate API family:

- HTTP ingress: `src/playwright_auto/dashboard_api.py` `POST /api/tasks/{task_id}/controls`, which enqueues `kind="task_control"` with `expected_task_version` and idempotency key support.
- command application: `src/playwright_auto/cdpa_worker.py::_apply_next_command`, `kind == "task_control"`.
- durable task mutation/control provenance: `src/playwright_auto/cdpa_store.py::request_control`.
- shared durable admission for Resume/Retry and the new action: `src/playwright_auto/cdpa_commands.py::workflow_control_eligibility`.

The current owner does **not** yet support route-limit mutation or exhaustion-aware Resume admission. Gate A extends this same chain with action `increase_route_limits`; its payload may contain `max_routes` and/or `max_route_repairs`, with at least one required. The control is valid only for a task blocked by `ROUTE_BUDGET_EXHAUSTED` or `ROUTE_VALIDATION_EXHAUSTED`.

Each supplied field must be a strict increase over its existing configured limit **and** strictly greater than its already-consumed count (`route_seq` for `max_routes`, current active-role-turn repair count for `max_route_repairs`). Equal values are rejected. The payload is atomic/all-or-nothing: if either supplied field is invalid, no route-limit field changes. Empty payload is rejected. The action is rejected unless the task is currently BLOCKED specifically by `ROUTE_BUDGET_EXHAUSTED` or `ROUTE_VALIDATION_EXHAUSTED`; it is invalid for RUNNING, WAITING, PAUSED, DONE, STOPPED, or other BLOCKED reasons such as `ROUTE_CONTRACT_DRIFT`. Accepted or rejected control processing never resets or changes `route_seq`, `self_route_counts`, current-turn repair count, role state, reachable roles, or route contract; rejection also leaves `max_routes` and `max_route_repairs` unchanged apart from normal command/audit rejection evidence. After an accepted increase, a separate normal `resume` control may continue from the same durable state. Gate A extends `workflow_control_eligibility` so plain Resume is rejected while the relevant exhaustion limit is still consumed, and accepted only after the corresponding limit has been raised. `block_retryable` stays false, so Retry remains invalid. After increasing `max_route_repairs`, Resume issues the next repair prompt as a new exactly-once Send; after increasing `max_routes`, Resume rebuilds the full route enum from the frozen contract and returns to normal pre-Send execution.

The action reads eligibility from the durable task `block_code` (`ROUTE_BUDGET_EXHAUSTED` or `ROUTE_VALIDATION_EXHAUSTED`) stored by the existing task store; it does not infer eligibility from UI text.

The existing command idempotency/version contract remains authoritative. Source inspection confirms `dashboard_api.enqueue()` first looks up by Idempotency-Key alone, then compares kind/payload/task/expected version; a mismatch is an idempotency conflict:

- a retry with the same `Idempotency-Key`, same task id, same payload, and same `expected_task_version` resolves to the existing command/result and is not re-evaluated by strict-increase validation;
- reusing the same key with a different payload/task/version is an idempotency conflict;
- a new command with stale `expected_task_version` is rejected before task mutation;
- idempotent replay or stale-version rejection does not mutate either limit or any consumed counter.
