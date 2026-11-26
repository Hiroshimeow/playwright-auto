# Contract: RouteContract

## Inputs

- current_role
- reachable_roles
- route_contract_id / semantic-route snapshot version
- closing_role
- semantic_routes
- self_route_policy
- current route_seq

## Allowed-route derivation

1. Start with `reachable_roles - {current_role}`.
2. Add `DONE` only if current_role == closing_role.
3. Add current_role only when `self_route_policy=bounded(max_self_routes=N)` and the bound is not exhausted.
4. Default self_route_policy is `forbidden`. Under bounded policy, every self-route increments route_seq and the current role's cumulative task-lifetime self-route counter. A creation-time semantic self-destination under forbidden policy is rejected with `SELF_ROUTE_FORBIDDEN`.
5. Add `PAUSE` for a prerequisite requiring user input or approval. Record `block_code` and `resume_condition` in the report.
6. Prompt prose may mention only allowed routes.

If semantic route data for a non-closing role maps an outcome to DONE, the builder rewrites that outcome to closing_role before rendering prompt prose/schema.

## Transition accounting

Non-terminal role-to-role routes increment `route_seq`. Allowed routes are independent of that count. Saved `max_routes` and `route_budget_start_seq` remain available for history. Team execution counts still apply to registered async wakes. Legacy Solo execution retains its own limit.

For an existing `ROUTE_BUDGET_EXHAUSTED` block, exact-task Resume uses the saved hop and the normal Send and report verification paths. Resume of a recorded role PAUSE keeps its accepted Send and report and continues with a distinct child hop.

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

The builder rewrites `clean -> B` because B is the closing role. Allowed routes for C are `["A","B","PAUSE"]`. DONE appears only when B is current.

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


## Route-repair limit control

Reuse and extend the existing task-control chain; do not create a separate API family:

- HTTP ingress: `src/playwright_auto/dashboard_api.py` `POST /api/tasks/{task_id}/controls`, which enqueues `kind="task_control"` with `expected_task_version` and idempotency key support.
- command application: `src/playwright_auto/cdpa_worker.py::_apply_next_command`, `kind == "task_control"`.
- durable task mutation/control provenance: `src/playwright_auto/cdpa_store.py::request_control`.
- shared durable admission for Resume/Retry and the new action: `src/playwright_auto/cdpa_commands.py::workflow_control_eligibility`.

`increase_route_limits` requires an integer `max_route_repairs` strictly above the configured limit and the consumed repair count. Eligibility requires a `BLOCKED` or `PAUSED` task with durable `block_code=ROUTE_VALIDATION_EXHAUSTED`. The control preserves counters and role state. After an accepted increase, a separate Resume can continue repair with a new exactly-once Send. Exact-report recovery remains available for eligible accepted turns with a blank terminal response.

The existing command idempotency/version contract remains authoritative. Source inspection confirms `dashboard_api.enqueue()` first looks up by Idempotency-Key alone, then compares kind/payload/task/expected version; a mismatch is an idempotency conflict:

- a retry with the same `Idempotency-Key`, same task id, same payload, and same `expected_task_version` resolves to the existing command/result and is not re-evaluated by strict-increase validation;
- reusing the same key with a different payload/task/version is an idempotency conflict;
- a new command with stale `expected_task_version` is rejected before task mutation;
- idempotent replay or stale-version rejection preserves the repair limit and all counters.
