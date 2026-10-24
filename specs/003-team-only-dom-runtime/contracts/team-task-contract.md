# Contract: TeamTask

## Request shape

```json
{
  "task": "...",
  "team": "...",
  "roles": ["A", "B", "C"],
  "route_targets": [],
  "start_role": "C",
  "closing_role": "B",
  "max_routes": 8,
  "max_route_repairs": 2,
  "self_route_policy": "forbidden",
  "provider_id": "chatgpt_web",
  "depends_on_task_ids": []
}
```

## Generic RolePolicy

1. Canonicalize role keys by trim + uppercase + spaces/hyphens to `_`.
2. Canonical keys must match `^[A-Z][A-Z0-9_]{0,31}$`.
3. Keys that collide with terminal route tokens, reserved control route tokens such as `WAIT`, TaskStatus tokens, or dependency projection tokens `WAITING_DEPENDENCY` / `DEPENDENCY_FAILED` are reserved.
4. No role registration is required.
5. Remove empty values and collapse duplicates while preserving first occurrence order.
6. Initial `roles` must be non-empty.
7. Omitted start_role and closing_role both default to the first normalized initial role.
8. start_role and closing_role must belong to initial roles.
9. DONE authority belongs only to closing_role.

## Reachable roles

At creation, RolePolicy computes immutable `reachable_roles` from:

- normalized initial roles;
- optional route_targets;
- semantic-route destinations for every role discovered in that set, transitively.

The closure must stay within configured `max_reachable_roles` and all discovered keys must satisfy role-key validation.

A reachable role begins as a declared RoleSlot and becomes materialized only on first validated route-in. Materialization does not mutate the submitted/initial roles.

## Example: generic initial team

```json
{"roles":["A","B","C"],"start_role":"C","closing_role":"B"}
```

## Example: duplicate collapse with auto-delegation

```json
{"roles":["REVIEW","REVIEW"],"start_role":"REVIEW","closing_role":"REVIEW"}
```

Initial roles normalize to `["REVIEW"]`. If REVIEW semantic route data includes `defect -> DEV`, creation freezes reachable_roles as `["REVIEW","DEV"]`. DEV is materialized only when the route is taken.

## Legacy boundary

Runtime-v2 public requests do not create Solo Task or Independent Agent. A one-role TeamTask remains a TeamTask even when more reachable roles may be lazily materialized later.


## Contract snapshot and defaults

The semantic-route data used to compute `reachable_roles` is frozen/versioned at creation as `route_contract_id`. Prompt-time config drift does not mutate the task; it blocks with `ROUTE_CONTRACT_DRIFT`. In-place contract refresh is out of scope; recovery requires creating a replacement TeamTask from the desired configuration.

`max_route_repairs` defaults from runtime config when omitted. `self_route_policy` defaults to `forbidden`. Under bounded self-route policy, every self-route increments `route_seq`, consumes `max_routes`, and increments that role's cumulative task-lifetime self-route counter.


## Derived state fields

`task_id`, `submitted_roles`, `reachable_roles`, `route_contract_id`, `self_route_counts`, `route_seq`, `active_role`, `status`, timestamps, and RoleSlot materialization state are runtime-derived/system-owned state, not client-writable request fields. Runtime-v2 rejects attempts to supply or override derived state through public creation input.
