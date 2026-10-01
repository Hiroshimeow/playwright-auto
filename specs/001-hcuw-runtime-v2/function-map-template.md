# Function Map Template

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Draft - must be filled during implementation, before or alongside each new owner group

Use this template to make each function/class readable and debuggable. A reviewer should be able to locate a bug by owner group and symbol without scanning unrelated gates.

## Entry Format

```markdown
## `<symbol>`

- **Owner group**: `RuntimeConfig | RuntimeStateStore | RuntimeApiServer | DashboardServer | EndpointInventory | CLI`
- **File**: `src/playwright_auto/hcuw_runtime/<file>.py`
- **Purpose**: One sentence.
- **Input**: Explicit arguments or constructor dependencies.
- **Output**: Explicit return type/result shape.
- **State authority**: What state or condition this symbol owns. Use `None` when it only consumes data.
- **Side effects**: File/network/process/browser effects. Use `None` when pure.
- **Forbidden responsibilities**: What this symbol must not decide or mutate.
- **Failure reasons**: Stable failure reason names.
- **Planned tests**: Exact test path(s).
- **Readability notes**: Any top-down expansion or helper split required.
```

## Required Initial Entries

Implementation must create entries for these before source code is considered reviewable:

### `RuntimeConfig`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Hold resolved runtime-v2 ports, paths, limits, and legacy isolation settings.
- **Input**: Defaults, environment/CLI overrides if approved.
- **Output**: Immutable config object.
- **State authority**: Resolved configuration only.
- **Side effects**: None.
- **Forbidden responsibilities**: State writes, HTTP serving, endpoint probing.
- **Failure reasons**: `invalid_port`, `legacy_port_conflict`, `invalid_state_root`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Keep validation helpers named by rule, for example `validate_api_port_is_not_legacy_port`.

### `load_runtime_config()`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Resolve the default runtime-v2 config.
- **Input**: Optional overrides object.
- **Output**: `RuntimeConfig` or typed config error.
- **State authority**: Config resolution.
- **Side effects**: None.
- **Forbidden responsibilities**: Creating directories, opening sockets.
- **Failure reasons**: `config_resolution_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Avoid compact magic env parsing; each override should be named.

### `RuntimeStateStore`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Own all `.runtime-v2` reads and writes.
- **Input**: `RuntimeConfig`.
- **Output**: Store object with explicit read/write methods.
- **State authority**: `.runtime-v2` files.
- **Side effects**: File reads/writes under `.runtime-v2` only.
- **Forbidden responsibilities**: HTTP response formatting, endpoint probing.
- **Failure reasons**: `state_read_failed`, `state_write_failed`, `state_json_invalid`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: Separate file IO helpers from JSON validation helpers.

### `RuntimeApiServer`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Serve JSON API on 9227.
- **Input**: `RuntimeConfig`, `RuntimeStateStore`, `EndpointInventory`.
- **Output**: Local HTTP server process/loop.
- **State authority**: API response envelope only.
- **Side effects**: Bind local socket and write HTTP responses.
- **Forbidden responsibilities**: Dashboard HTML rendering, endpoint config parsing decisions, browser mutation.
- **Failure reasons**: `port_in_use`, `route_not_found`, `owner_error`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Route handlers should be small and call owner methods.

### `DashboardServer`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Serve minimal dashboard on 9226 and proxy `/api/*` to 9227.
- **Input**: `RuntimeConfig`.
- **Output**: Local dashboard HTTP server process/loop.
- **State authority**: Dashboard proxy availability display only.
- **Side effects**: Bind local socket, serve static HTML, proxy local API requests.
- **Forbidden responsibilities**: Runtime state decisions, endpoint reachability decisions, prompt execution.
- **Failure reasons**: `port_in_use`, `api_unavailable`, `dashboard_route_not_found`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: Keep `serve_index` and `proxy_api_request` separate.

### `EndpointInventory`

- **Owner group**: `EndpointInventory`
- **File**: `src/playwright_auto/hcuw_runtime/endpoint_inventory.py`
- **Purpose**: Produce read-only endpoint summaries.
- **Input**: `RuntimeConfig`.
- **Output**: Endpoint inventory result with items and diagnostics.
- **State authority**: Endpoint reachability result.
- **Side effects**: Optional bounded local network probe only.
- **Forbidden responsibilities**: Tab listing, page ownership, send, route, repair, worker placement.
- **Failure reasons**: `endpoint_config_missing`, `endpoint_config_invalid`, `endpoint_probe_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_endpoint_inventory.py`.
- **Readability notes**: Keep config loading and reachability probing separate.

## Review Rule

Do not merge implementation tasks that introduce a new owner, condition, or side effect without adding or updating the corresponding function map entry.
