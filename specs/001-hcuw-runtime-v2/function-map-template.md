# Function Map

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Implementation map active

This file records public runtime-v2 symbols before source implementation so a reviewer can locate ownership, state authority, side effects, and tests without scanning unrelated gates.

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

## `RuntimeConfig`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Immutable resolved runtime-v2 ports, paths, limits, and legacy isolation settings.
- **Input**: Repository root plus explicit scalar overrides.
- **Output**: `RuntimeConfig` dataclass.
- **State authority**: Resolved configuration only.
- **Side effects**: None.
- **Forbidden responsibilities**: State writes, HTTP serving, endpoint probing, browser mutation.
- **Failure reasons**: `invalid_port`, `legacy_port_conflict`, `invalid_state_root`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Validation helpers are named by condition; no implicit fallback to 9225.

## `RuntimeConfig.to_health_payload()`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Build configuration-owned health fields for API display.
- **Input**: `started_at`, optional branch/head strings.
- **Output**: JSON-safe dictionary.
- **State authority**: Config display fields.
- **Side effects**: None.
- **Forbidden responsibilities**: Endpoint health, run state, HTTP status decisions.
- **Failure reasons**: None.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Keep display formatting separate from HTTP envelope formatting.

## `RuntimeConfig.to_config_payload()`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Build the JSON-safe `/api/config` payload.
- **Input**: None beyond instance state.
- **Output**: JSON-safe dictionary.
- **State authority**: Config display fields.
- **Side effects**: None.
- **Forbidden responsibilities**: Endpoint inventory, state file validation.
- **Failure reasons**: None.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Use explicit nested dictionaries instead of shorthand merging.

## `load_runtime_config()`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Resolve and validate the default runtime-v2 config.
- **Input**: Optional repository root and explicit scalar overrides.
- **Output**: `RuntimeConfig`.
- **State authority**: Config resolution.
- **Side effects**: None.
- **Forbidden responsibilities**: Creating directories, opening sockets, reading endpoint inventory.
- **Failure reasons**: `config_resolution_failed`, `invalid_port`, `legacy_port_conflict`, `invalid_state_root`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Each validated condition has a named helper.

## `RuntimeStateStore`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Own all `.runtime-v2` reads and writes.
- **Input**: `RuntimeConfig`.
- **Output**: Store object with explicit read/write methods.
- **State authority**: `.runtime-v2` files.
- **Side effects**: File reads/writes under `.runtime-v2` only.
- **Forbidden responsibilities**: HTTP response formatting, endpoint probing, browser mutation.
- **Failure reasons**: `state_read_failed`, `state_write_failed`, `state_json_invalid`, `state_path_escape`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: Separate path guard, file IO, and JSON validation helpers.

## `RuntimeStateStore.initialize()`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Create the runtime-v2 state root when the state owner explicitly initializes it.
- **Input**: None beyond store config.
- **Output**: `None`.
- **State authority**: `.runtime-v2` directory existence.
- **Side effects**: Creates `.runtime-v2` only.
- **Forbidden responsibilities**: Creating legacy `.runtime`, creating run records, endpoint probing.
- **Failure reasons**: `state_write_failed`, `state_path_escape`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: Directory creation is not hidden in read methods.

## `RuntimeStateStore.list_runs()`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Return runtime-v2 run records from `.runtime-v2/runs.json`.
- **Input**: None beyond store config.
- **Output**: `list[dict[str, Any]]`.
- **State authority**: Run file validity.
- **Side effects**: File read only; missing file creates no file.
- **Forbidden responsibilities**: Creating runs, formatting HTTP envelopes, reading legacy tasks.
- **Failure reasons**: `state_read_failed`, `state_json_invalid`, `state_path_escape`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Missing file is a typed owner decision: empty list.

## `RuntimeStateStore.append_diagnostic()`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Append safe local diagnostics to `.runtime-v2/diagnostics.jsonl`.
- **Input**: Diagnostic code and message.
- **Output**: `None`.
- **State authority**: Diagnostic file writes.
- **Side effects**: Appends under `.runtime-v2` only.
- **Forbidden responsibilities**: Logging secrets, HTTP envelope formatting, endpoint reachability decisions.
- **Failure reasons**: `state_write_failed`, `state_path_escape`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: The caller supplies safe display text; the store owns the file write.

## `EndpointInventory`

- **Owner group**: `EndpointInventory`
- **File**: `src/playwright_auto/hcuw_runtime/endpoint_inventory.py`
- **Purpose**: Produce read-only endpoint summaries.
- **Input**: `RuntimeConfig`.
- **Output**: Inventory object with summary method.
- **State authority**: Endpoint config parsing and reachability result.
- **Side effects**: Optional bounded local network probe only.
- **Forbidden responsibilities**: Tab listing, page ownership, send, route, repair, worker placement, registry mutation.
- **Failure reasons**: `endpoint_config_missing`, `endpoint_config_invalid`, `endpoint_probe_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_endpoint_inventory.py`.
- **Readability notes**: Keep config loading and reachability probing separate.

## `EndpointInventory.summarize_endpoints()`

- **Owner group**: `EndpointInventory`
- **File**: `src/playwright_auto/hcuw_runtime/endpoint_inventory.py`
- **Purpose**: Return endpoint items and diagnostics for `/api/endpoints`.
- **Input**: `probe` boolean choosing whether to perform bounded reachability checks.
- **Output**: `EndpointInventoryResult`.
- **State authority**: Endpoint inventory result.
- **Side effects**: Optional socket probe; no file writes.
- **Forbidden responsibilities**: Browser tab inspection, prompt sending, route decisions, legacy API mutation.
- **Failure reasons**: `endpoint_config_missing`, `endpoint_config_invalid`, `endpoint_probe_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_endpoint_inventory.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: The boolean is limited to a single bounded side effect within one owner; it does not switch unrelated behavior.

## `RuntimeApiServer`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Serve JSON API on 9227.
- **Input**: `RuntimeConfig`, `RuntimeStateStore`, `EndpointInventory`.
- **Output**: API server object.
- **State authority**: API route mapping and response envelope only.
- **Side effects**: Bind local socket when `start()` is called; write HTTP responses in request handler.
- **Forbidden responsibilities**: Dashboard HTML rendering, endpoint config parsing decisions, browser mutation.
- **Failure reasons**: `port_in_use`, `route_not_found`, `owner_error`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Route handlers call owner methods and do not recompute owner conditions.

## `RuntimeApiServer.handle_get_path()`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Route a GET path to the correct owner and wrap the result in an API response.
- **Input**: Request path string.
- **Output**: `ApiResponse`.
- **State authority**: API route mapping.
- **Side effects**: None.
- **Forbidden responsibilities**: Reading state files directly, parsing endpoint config directly, rendering dashboard HTML.
- **Failure reasons**: `route_not_found`, `owner_error`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Use explicit if/elif route names; no hidden dispatch table for this small skeleton.

## `RuntimeApiServer.start()`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Bind the local API socket.
- **Input**: None beyond constructor dependencies.
- **Output**: `ThreadingHTTPServer`.
- **State authority**: API socket bind outcome.
- **Side effects**: Opens local listening socket.
- **Forbidden responsibilities**: Retrying another port, mutating legacy state, starting dashboard.
- **Failure reasons**: `port_in_use`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Port conflict must fail clearly and never fallback to 9225.

## `DashboardServer`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Serve minimal dashboard on 9226 and proxy `/api/*` to 9227.
- **Input**: `RuntimeConfig`.
- **Output**: Dashboard server object.
- **State authority**: Dashboard route mapping and proxy availability display only.
- **Side effects**: Bind local socket, serve static HTML, proxy local API requests.
- **Forbidden responsibilities**: Runtime state decisions, endpoint reachability decisions, prompt execution.
- **Failure reasons**: `port_in_use`, `api_unavailable`, `dashboard_route_not_found`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: Keep `serve_index` and `proxy_api_request` separate.

## `DashboardServer.handle_get_path()`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Route dashboard GET paths to static HTML or API proxy.
- **Input**: Request path string.
- **Output**: `DashboardResponse`.
- **State authority**: Dashboard route mapping.
- **Side effects**: Local API proxy only when path starts with `/api/`.
- **Forbidden responsibilities**: Creating runs, reading runtime state directly, endpoint probing.
- **Failure reasons**: `api_unavailable`, `dashboard_route_not_found`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: The proxy path is the single dashboard owner for API unavailable presentation.

## `DashboardServer.start()`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Bind the local dashboard socket.
- **Input**: None beyond constructor config.
- **Output**: `ThreadingHTTPServer`.
- **State authority**: Dashboard socket bind outcome.
- **Side effects**: Opens local listening socket.
- **Forbidden responsibilities**: Starting API server, mutating runtime state, falling back to 9225.
- **Failure reasons**: `port_in_use`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: Port conflict fails clearly.

## `main()`

- **Owner group**: `CLI`
- **File**: `src/playwright_auto/hcuw_runtime/cli.py`
- **Purpose**: Parse runtime-v2 CLI commands and wire owners in top-down order.
- **Input**: Optional argv list.
- **Output**: Process exit code.
- **State authority**: CLI command selection only.
- **Side effects**: Starts API/dashboard server only for explicit commands.
- **Forbidden responsibilities**: Hidden background behavior, prompt sending, tab adoption, route repair.
- **Failure reasons**: `invalid_command`, `port_in_use`, `config_resolution_failed`.
- **Planned tests**: covered indirectly by owner tests; CLI smoke may be added later.
- **Readability notes**: Construction order is explicit: config -> store/inventory -> server.

## Review Rule

Do not merge implementation tasks that introduce a new owner, condition, or side effect without adding or updating the corresponding function map entry.
## Additional Public Symbol Entries

### `ConfigError`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Report stable configuration validation failures from the config owner.
- **Input**: Failure reason and human-readable message.
- **Output**: Exception carrying the config failure.
- **State authority**: Config failure reasons only.
- **Side effects**: None.
- **Forbidden responsibilities**: State mutation, HTTP formatting, endpoint probing.
- **Failure reasons**: `invalid_port`, `legacy_port_conflict`, `invalid_state_root`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Keep config failures local to config validation.

### `load_runtime_config`

- **Owner group**: `RuntimeConfig`
- **File**: `src/playwright_auto/hcuw_runtime/config.py`
- **Purpose**: Resolve runtime-v2 defaults and explicit overrides into one immutable config.
- **Input**: Optional repository root, API host/port, dashboard host/port, API base URL, and endpoint source overrides.
- **Output**: `RuntimeConfig`.
- **State authority**: Config resolution.
- **Side effects**: None.
- **Forbidden responsibilities**: Creating state directories, opening sockets, probing endpoints.
- **Failure reasons**: `config_resolution_failed`, `invalid_port`, `legacy_port_conflict`, `invalid_state_root`.
- **Planned tests**: `tests/hcuw_runtime/test_config.py`.
- **Readability notes**: Overrides stay explicit; no hidden fallback to legacy 9225.

### `StateStoreError`

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Report stable state read/write/validation failures from the store owner.
- **Input**: Failure reason and human-readable message.
- **Output**: Exception carrying the state-store failure.
- **State authority**: `.runtime-v2` failure reasons.
- **Side effects**: None.
- **Forbidden responsibilities**: HTTP formatting, endpoint probing, legacy state mutation.
- **Failure reasons**: `state_read_failed`, `state_write_failed`, `state_json_invalid`.
- **Planned tests**: `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: Preserve malformed state files for operator inspection.

### `RuntimeApiServerError`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Report API-server startup failures owned by the API layer.
- **Input**: Failure reason and human-readable message.
- **Output**: Exception carrying the API-server failure.
- **State authority**: API socket/startup failure reasons.
- **Side effects**: None.
- **Forbidden responsibilities**: Config validation, endpoint reachability decisions, dashboard rendering.
- **Failure reasons**: `port_in_use`, `api_start_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Port conflict never falls back to 9225 or any alternate port.

### `DashboardServerError`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Report dashboard startup failures owned by the dashboard layer.
- **Input**: Failure reason and human-readable message.
- **Output**: Exception carrying the dashboard failure.
- **State authority**: Dashboard socket/startup failure reasons.
- **Side effects**: None.
- **Forbidden responsibilities**: API health semantics, runtime state decisions, endpoint probing.
- **Failure reasons**: `port_in_use`, `dashboard_start_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: Dashboard port conflict never replaces or reuses legacy dashboard behavior.

### `EndpointInventoryError`

- **Owner group**: `EndpointInventory`
- **File**: `src/playwright_auto/hcuw_runtime/endpoint_inventory.py`
- **Purpose**: Report endpoint-config parse failures owned by endpoint inventory.
- **Input**: Failure reason and human-readable message.
- **Output**: Exception carrying the endpoint inventory failure.
- **State authority**: Endpoint config parse failure reasons.
- **Side effects**: None.
- **Forbidden responsibilities**: Browser tab listing, prompt sending, route repair, registry mutation.
- **Failure reasons**: `endpoint_config_invalid`.
- **Planned tests**: `tests/hcuw_runtime/test_endpoint_inventory.py`.
- **Readability notes**: Offline endpoint reachability is an item diagnostic, not this exception.

### `main`

- **Owner group**: `CLI`
- **File**: `src/playwright_auto/hcuw_runtime/cli.py`
- **Purpose**: Parse runtime-v2 CLI commands and wire owner groups in top-down order.
- **Input**: Optional argv list.
- **Output**: Process exit by serving API/dashboard or parser error.
- **State authority**: None; consumes owner groups.
- **Side effects**: Starts local API or dashboard process after explicit command selection.
- **Forbidden responsibilities**: Hidden background behavior, implicit fallback, send/bind/route behavior.
- **Failure reasons**: `config_resolution_failed`, `port_in_use`, `dashboard_start_failed`.
- **Planned tests**: Covered indirectly by owner tests; add CLI-specific tests only when CLI behavior expands.
- **Readability notes**: Keep construction order explicit: config -> store/inventory -> server.

### `AuditEvent`

- **Owner group**: `AuditTimeline`
- **File**: `src/playwright_auto/hcuw_runtime/audit.py`
- **Purpose**: Represent one dashboard-safe audit event.
- **Input**: Event id, source, kind, message, timestamp, payload.
- **Output**: Immutable event object and JSON-safe payload.
- **State authority**: None; it is a value object.
- **Side effects**: None.
- **Forbidden responsibilities**: DOM extraction, network listening, browser mutation.
- **Failure reasons**: None.
- **Planned tests**: `tests/hcuw_runtime/test_audit.py`.
- **Readability notes**: Keep event shape explicit and dashboard-safe.

### `AuditSourceState`

- **Owner group**: `AuditTimeline`
- **File**: `src/playwright_auto/hcuw_runtime/audit.py`
- **Purpose**: Represent current status of a dashboard audit source.
- **Input**: Source name, status, message, timestamp, optional event id, payload.
- **Output**: Immutable source-state object and JSON-safe payload.
- **State authority**: Current audit-source display state only.
- **Side effects**: None.
- **Forbidden responsibilities**: Deciding DOM/LISTEN authority or mutating browser state.
- **Failure reasons**: None.
- **Planned tests**: `tests/hcuw_runtime/test_audit.py`.
- **Readability notes**: Source states describe what can be shown now; they do not fake unavailable sources.

### `AuditTimeline`

- **Owner group**: `AuditTimeline`
- **File**: `src/playwright_auto/hcuw_runtime/audit.py`
- **Purpose**: Own runtime-v2 audit events and source display state for dashboard 9226.
- **Input**: Max event count plus events/source-state updates from other owners.
- **Output**: Snapshot and incremental event payloads for `/api/audit/*`.
- **State authority**: In-memory audit timeline only.
- **Side effects**: Thread lock updates in memory; no file/browser side effects.
- **Forbidden responsibilities**: DOM extraction, MutationObserver injection, LISTEN transport parsing, prompt send, tab adoption.
- **Failure reasons**: None in skeleton; future source owners must publish typed failures.
- **Planned tests**: `tests/hcuw_runtime/test_audit.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: DOM, mutation, and LISTEN remain explicit source states; inactive sources must be visible rather than hidden.
### `RuntimeTaskRunnerError`

- **Owner group**: `TaskRunner`
- **File**: `src/playwright_auto/hcuw_runtime/task_runner.py`
- **Purpose**: Represent stable task-runner failure reasons for API response formatting.
- **Input**: Failure reason and message.
- **Output**: Exception with `reason` and `message`.
- **State authority**: None.
- **Side effects**: None.
- **Forbidden responsibilities**: Starting legacy runtime or formatting API envelopes.
- **Failure reasons**: N/A; this type carries failures.
- **Planned tests**: `tests/hcuw_runtime/test_task_runner.py`.
- **Readability notes**: Keep reason strings stable for dashboard/audit review.

### `TaskSubmissionRequest`

- **Owner group**: `TaskRunner`
- **File**: `src/playwright_auto/hcuw_runtime/task_runner.py`
- **Purpose**: Hold validated real CDPA task submission inputs.
- **Input**: Parsed JSON payload fields.
- **Output**: Immutable request object.
- **State authority**: Task submission request shape only.
- **Side effects**: None.
- **Forbidden responsibilities**: Process spawning, legacy API probing, browser ownership.
- **Failure reasons**: `invalid_task_request` before construction.
- **Planned tests**: `tests/hcuw_runtime/test_task_runner.py`.
- **Readability notes**: Keeps validation separate from subprocess execution.

### `RuntimeTaskRunner`

- **Owner group**: `TaskRunner`
- **File**: `src/playwright_auto/hcuw_runtime/task_runner.py`
- **Purpose**: Submit a real CDPA Team Task through runtime-v2 by ensuring legacy CDPA is running and calling the legacy `POST /api/tasks` once.
- **Input**: `RuntimeConfig`, `RuntimeStateStore`, `AuditTimeline`, and task JSON payload (`task`, `team`, `browser`, `roles`, `start_role`, `workflow_mode=team` only).
- **Output**: Runtime-v2 task record persisted under `.runtime-v2/tasks`.
- **State authority**: Real-task submission orchestration and task-runner process metadata; persistence is delegated to `RuntimeStateStore`.
- **Side effects**: May start one legacy CDPA runtime subprocess (never when the legacy API port is already bound), writes the `.runtime-v2/task-runner/legacy-cdpa-bridge.json` bridge config, calls the legacy API once after validation, writes `.runtime-v2` task records through `RuntimeStateStore`.
- **Forbidden responsibilities**: DOM authority, MutationObserver authority, LISTEN authority, Send/bind/route implementation, direct legacy durable state edits, Solo/Independent Agent creation, `cdpa.yaml` mutation.
- **Failure reasons**: `invalid_task_request`, `legacy_runtime_unavailable`, `legacy_config_bridge_failed`, `team_task_submit_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_task_runner.py`.
- **Readability notes**: Top-down flow is `submit_task` -> validate -> `ensure_legacy_runtime` -> legacy API submit -> build record -> persist/audit. The closing-role rule is consumed from legacy `workflow_closing_role`, not re-implemented.

### `RuntimeTaskRunner.submit_task` / `RuntimeTaskRunner.ensure_legacy_runtime`

- **Owner group**: `TaskRunner`
- **File**: `src/playwright_auto/hcuw_runtime/task_runner.py`
- **Purpose**: Public entrypoints for Team Task submission and legacy runtime availability.
- **Input**: Task payload / none.
- **Output**: Persisted task record / `{"status": "already_running" | "started", "api", "pid"?}`.
- **State authority**: See `RuntimeTaskRunner`.
- **Side effects**: See `RuntimeTaskRunner`; validation failures have no side effects.
- **Forbidden responsibilities**: See `RuntimeTaskRunner`.
- **Failure reasons**: `invalid_task_request`, `legacy_runtime_unavailable`, `team_task_submit_failed`.
- **Planned tests**: `tests/hcuw_runtime/test_task_runner.py`.
- **Readability notes**: A bound-but-unhealthy legacy port waits for health instead of spawning a duplicate runtime.

### `RuntimeApiServer.handle_post_path`

- **Owner group**: `RuntimeApiServer`
- **File**: `src/playwright_auto/hcuw_runtime/api_server.py`
- **Purpose**: Route `POST /api/tasks` to `RuntimeTaskRunner.submit_task` and wrap the result.
- **Input**: Request path and raw body.
- **Output**: `ApiResponse` (202 success, 400 `invalid_task_request`/`invalid_json`, 500 owner errors).
- **State authority**: API route mapping and HTTP status only.
- **Side effects**: Those of the task runner.
- **Forbidden responsibilities**: Re-validating task payloads, writing legacy state.
- **Failure reasons**: `invalid_task_request`, `invalid_json`, `route_not_found`, `owner_error`.
- **Planned tests**: `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: `GET /api/tasks` joins records read-only with `LegacyObservationAudit.task_summaries()` (newest first, `latest` marker).

### `LegacyObservationAudit` (`refresh`, `task_summaries`)

- **Owner group**: `AuditTimeline` source adapter
- **File**: `src/playwright_auto/hcuw_runtime/legacy_audit.py`
- **Purpose**: Publish DOM/mutation/LISTEN source states from read-only legacy board, live-event, and runtime-log evidence; expose board summaries keyed by legacy task id.
- **Input**: `RuntimeConfig`; `refresh` also takes the timeline and the set of runtime-v2 legacy task ids.
- **Output**: Source-state updates / `dict[legacy_task_id, summary]` (empty when the board is unavailable).
- **State authority**: None beyond in-memory previous board signature.
- **Side effects**: Local HTTP GET to legacy `/api/tasks`; reads `.runtime/live-events.jsonl` and the task-runner log.
- **Forbidden responsibilities**: Legacy manifest writes, route/Send decisions, browser access.
- **Failure reasons**: Board unavailability is reported as `unavailable` source status, never raised.
- **Planned tests**: `tests/hcuw_runtime/test_audit.py`, `tests/hcuw_runtime/test_api_server.py`.
- **Readability notes**: Runtime-v2 tasks are listed first and counted separately from old legacy blocks; role summaries carry `page_url_kind` (`root`, `provisional`, `canonical`).

### `RuntimeStateStore` task records (`save_task_record`, `list_task_records`, `save_task_runner_process`, `task_runner_log_path`)

- **Owner group**: `RuntimeStateStore`
- **File**: `src/playwright_auto/hcuw_runtime/state.py`
- **Purpose**: Persist runtime-v2 task records and legacy runtime process metadata.
- **Input**: JSON-safe record dictionaries.
- **Output**: `None` / list of records.
- **State authority**: `.runtime-v2/tasks/*.json` and `.runtime-v2/task-runner/*`.
- **Side effects**: File reads/writes under `.runtime-v2` only.
- **Forbidden responsibilities**: Legacy `.runtime`/`.plan` writes, HTTP formatting.
- **Failure reasons**: `state_read_failed`, `state_write_failed`, `state_json_invalid`, `state_path_escape`.
- **Planned tests**: `tests/hcuw_runtime/test_task_runner.py`, `tests/hcuw_runtime/test_state_store.py`.
- **Readability notes**: Path accessors (`task_runner_dir`, `task_records_dir`) all pass the state-root guard.

### `DashboardServer.proxy_post_api_request`

- **Owner group**: `DashboardServer`
- **File**: `src/playwright_auto/hcuw_runtime/dashboard_server.py`
- **Purpose**: Forward dashboard Team Task submissions to the 9227 API.
- **Input**: API path and raw body.
- **Output**: `DashboardResponse`; API 4xx/5xx envelopes are passed through with their status, connection failures become 503 `api_unavailable`.
- **State authority**: None.
- **Side effects**: One local HTTP POST to 9227.
- **Forbidden responsibilities**: Payload validation, legacy calls, retries.
- **Failure reasons**: `api_unavailable`.
- **Planned tests**: `tests/hcuw_runtime/test_dashboard_server.py`.
- **Readability notes**: The dashboard page polls only while visible and focused (0.5 s).

Value objects and trivial accessors (`ApiResponse`, `DashboardResponse`, `EndpointInventoryResult`, `*.to_payload`, `RuntimeConfig.runs_path`/`diagnostics_path`, `serve_forever`, `serve_index`, `proxy_api_request`, `AuditTimeline.record_event`/`update_source_state`/`snapshot`/`events_since`, `RuntimeStateStore.recent_diagnostics`) are covered by their owner entries above.
