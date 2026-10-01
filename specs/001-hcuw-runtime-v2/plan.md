# Implementation Plan: HCUW Runtime V2 Skeleton

**Branch**: `refactor/hcuw-runtime-v2` | **Date**: 2026-09-30 | **Spec**: `specs/001-hcuw-runtime-v2/spec.md`

**Input**: Feature specification from `specs/001-hcuw-runtime-v2/spec.md`

**Status**: Draft - awaiting final operator review before source implementation

## Summary

Create an isolated runtime-v2 frame beside the legacy CDPA runtime: API on 9227, minimal read-only dashboard web on 9226, read-only CDP endpoint inventory, `.runtime-v2` state, and explicit owner-group boundaries before any send/bind/route logic is rebuilt.

The first implementation must be intentionally small. It proves process separation, state isolation, and dashboard visibility. It must not implement prompt sending, tab ownership, response binding, route parsing, route repair, or worker orchestration.

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**: Existing repository dependencies only for the skeleton. Prefer Python standard library HTTP server primitives for the first API/dashboard frame unless implementation evidence proves a framework is necessary.

**Storage**: Local files under `.runtime-v2` only. Legacy `.runtime` and existing CDPA durable state are read-prohibited except for explicitly allowed read-only endpoint config.

**Testing**: pytest. Start with focused unit tests for config/state/API response shaping, then integration tests for local HTTP servers if the implementation phase proceeds.

**Target Platform**: Local Windows development on fjp, with code remaining portable for existing CDPA development environments.

**Project Type**: Python CLI/local web service/operator dashboard.

**Performance Goals**: Health/config/runs responses should complete in under 200ms locally when no endpoint probe is requested. Endpoint inventory should bound each reachability probe to 2 seconds or less.

**Constraints**:

- Legacy API 9225 must remain untouched.
- New API defaults to 9227.
- New dashboard defaults to 9226.
- Dashboard is read-only.
- Endpoint inventory is read-only.
- No prompt sending or tab adoption.
- No hidden fallback from occupied 9227/9226 to another port.
- Repository-wide ownership and readability requirements come from `HCU-ARCH-001`, `HCU-ARCH-002`, `HCU-CODE-001`, and `HCU-CODE-002` in `.agents/rules/project.md`.

**Scale/Scope**: One local operator, one local API process, one local dashboard process, zero active runtime-v2 prompt execution. Endpoint inventory may summarize the existing configured CDP endpoints.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **`HCU-ARCH-001`**: PASS. Runtime-v2 is isolated and does not create duplicate live send/bind/route paths.
- **`HCU-ARCH-002`**: PASS WITH OWNER MAP. Config, state, API, dashboard, and endpoint conditions each have one planned owner.
- **`HCU-CODE-001` / `HCU-CODE-002`**: PASS WITH FUNCTION-MAP GATE. Planned public functions are mapped before implementation and must expand top-down into focused helpers.
- **`HCU-POLICY-001`**: PASS. Feature artifacts reference canonical rule IDs rather than acting as a second project rulebook.

Post-design re-check: PASS. `runtime-api.md` and `code-organization-contract.md` preserve feature-specific boundaries while delegating project-wide rules to `.agents/rules/project.md`.

## Project Structure

### Documentation (this feature)

```text
specs/001-hcuw-runtime-v2/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── function-map-template.md
├── contracts/
│   ├── runtime-api.md
│   ├── observation-source-contract.md
│   └── code-organization-contract.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (planned, not yet implemented)

```text
src/playwright_auto/hcuw_runtime/
├── __init__.py
├── config.py              # RuntimeConfig owner
├── state.py               # RuntimeStateStore owner
├── api_server.py          # RuntimeApiServer owner for 9227
├── dashboard_server.py    # DashboardServer owner for 9226 and /api proxy
├── endpoint_inventory.py  # EndpointInventory owner, read-only
└── cli.py                 # Runtime-v2 entrypoints, start-api/start-dashboard

tests/hcuw_runtime/
├── test_config.py
├── test_state_store.py
├── test_api_server.py
├── test_dashboard_server.py
└── test_endpoint_inventory.py
```

**Structure Decision**: Use a new `src/playwright_auto/hcuw_runtime/` package to avoid mixing runtime-v2 skeleton code into existing large CDPA files. This is an isolated namespace, not a duplicate live implementation of legacy worker behavior. The package must not import or mutate legacy worker state. It may read endpoint configuration through a narrow read-only adapter.

## Owner Groups and Responsibilities

| Owner Group | Planned File | Owns | Must Not Own |
|---|---|---|---|
| `RuntimeConfig` | `config.py` | Ports, paths, limits, legacy isolation config | Runtime state writes, endpoint probing |
| `RuntimeStateStore` | `state.py` | `.runtime-v2` initialization, run reads, diagnostics writes | HTTP routing, endpoint probing |
| `RuntimeApiServer` | `api_server.py` | 9227 HTTP routing, JSON envelopes, API error formatting | Dashboard HTML, endpoint config parsing decisions |
| `DashboardServer` | `dashboard_server.py` | 9226 static dashboard, `/api/*` proxy, API unavailable UI state | Runtime state decisions, endpoint reachability decisions |
| `EndpointInventory` | `endpoint_inventory.py` | Read-only endpoint config load and reachability summaries | Browser tab ownership, prompt sending, route observation |
| `ObservationSourceContract` | docs only in this feature | Future source authority rules | Runtime behavior in this skeleton |

## Function Expansion Pattern

Implementation must expand from coarse to fine responsibilities:

```text
cli.main()
  -> parse_runtime_command()
  -> load_runtime_config()
  -> create_runtime_state_store()
  -> create_endpoint_inventory()
  -> start_api_server() or start_dashboard_server()
```

API request handling:

```text
RuntimeApiServer.handle_request()
  -> route_request_path()
  -> call owner method
  -> build_json_response()
  -> write_response()
```

Dashboard handling:

```text
DashboardServer.handle_request()
  -> serve_index() OR proxy_api_request()
  -> render_unavailable_state_when_needed()
```

Endpoint inventory:

```text
EndpointInventory.summarize_endpoints()
  -> load_endpoint_records()
  -> summarize_each_endpoint()
  -> return EndpointInventoryResult
```

No helper may silently take ownership of a condition that belongs to another owner.

## Phase 0 Research Results

See `research.md`.

Key decisions:

- Use isolated runtime-v2 namespace and `.runtime-v2` state.
- Use API 9227 and dashboard 9226.
- Serve minimal dashboard as local operator UI.
- Use `/api/*` dashboard proxy to keep browser-side code simple and avoid CORS complexity.
- Keep endpoint inventory read-only.
- Do not implement send/bind/route in this skeleton.

## Phase 1 Design Outputs

- `data-model.md`: RuntimeConfig, RuntimeStateStore, RuntimeRun, RuntimeDiagnostic, EndpointSummary, FunctionMapEntry.
- `contracts/runtime-api.md`: API/dashboard routes and response envelopes.
- `contracts/observation-source-contract.md`: Future observation authority rules.
- `contracts/code-organization-contract.md`: Feature owner/function map that references canonical `HCU-ARCH-*` and `HCU-CODE-*` rules.
- `quickstart.md`: Review and future validation commands.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| New local API service on 9227 | Required to develop runtime-v2 beside legacy 9225 without mutating production runtime | Reusing 9225 would risk legacy state and process coupling |
| New local dashboard on 9226 | Required for visual review while building runtime-v2 | Extending legacy dashboard would mix old and new authority during refactor |
| New `.runtime-v2` state root | Required to prove isolation and avoid legacy state mutation | Sharing `.runtime` would make tests and failures ambiguous |

All added complexity is bounded to the skeleton and must not include send/bind/route behavior.

## Pre-Implementation Review Gate

Implementation must not begin until the operator approves this plan. Review should confirm:

- API 9227 + dashboard 9226 ports are acceptable.
- Dashboard `/api/*` proxy design is acceptable.
- Owner groups are acceptable.
- Function expansion pattern is acceptable.
- No source implementation is required before approval.
