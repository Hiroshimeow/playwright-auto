# Runtime V2 Code Ownership Map

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Draft - review before implementation

This file does not define repository-wide coding policy. Canonical project rules live in `.agents/rules/project.md`.

Applicable canonical rules:

- `HCU-ARCH-001`: one concept, one owner, one live path.
- `HCU-ARCH-002`: one runtime condition/state transition, one decision/mutation owner.
- `HCU-CODE-001`: responsibility grouping, top-down readable flow, explicit typed boundaries.
- `HCU-CODE-002`: focused functions, normally below 100 lines, split only at named responsibility boundaries.

## Feature owner map

| Owner | Owns | Does not own |
|---|---|---|
| `RuntimeConfig` | Resolve v2 ports, paths, limits, legacy isolation values | Runtime state mutation, HTTP response semantics, endpoint reachability |
| `RuntimeStateStore` | Validate/read/write `.runtime-v2` state owned by v2 | Browser state, endpoint probing, dashboard rendering |
| `RuntimeApiServer` | HTTP routing and API response envelopes | Re-deciding config/state/endpoint facts already returned by their owner |
| `DashboardServer` | Static dashboard and same-origin `/api/*` proxy behavior | API health semantics, endpoint reachability, runtime state mutation |
| `EndpointInventory` | Read endpoint configuration and produce read-only reachability summaries | Browser tab ownership, Send, route, repair, legacy worker mutation |
| Observation source modules (later feature) | Their explicitly named observation facts only | Route authority unless a later approved contract grants it |

## Planned top-down call shape

```text
runtime entrypoint
  -> RuntimeConfig resolves configuration
  -> RuntimeStateStore owns v2 persistence
  -> EndpointInventory builds read-only endpoint summaries
  -> RuntimeApiServer exposes owner results on 9227
  -> DashboardServer renders/proxies those results on 9226
```

Within an owner, planned code should read:

```text
public method / handler
  -> validate boundary input
  -> call named owner decision or I/O helper
  -> format typed result
  -> return
```

No caller may duplicate an owner's condition just to obtain a different presentation.

## Function map requirement before implementation

Each planned new public function/method must have a function-map entry with:

- owner group/class;
- purpose;
- input and output types;
- state/condition authority;
- side effects;
- forbidden responsibilities;
- failure reasons;
- planned tests.

Private helpers may be added during implementation only when their responsibility is obvious from the owning function and remains within the same owner boundary.

## Review gate

Before implementation, verify:

- every planned state write maps to exactly one owner (`HCU-ARCH-002`);
- every planned condition maps to exactly one owner (`HCU-ARCH-002`);
- each public endpoint maps to one handler owner;
- endpoint inventory remains read-only;
- dashboard displays/proxies owner results rather than re-deciding them;
- planned call trees expand from public behavior to smaller named helpers (`HCU-CODE-001`);
- no planned function needs to exceed 100 lines without a documented cohesion reason (`HCU-CODE-002`).
