# Data Model: HCUW Runtime V2 Skeleton

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Draft - review before implementation

## RuntimeConfig

Represents resolved configuration for runtime-v2.

Fields:

- `host`: loopback bind host, default `127.0.0.1`.
- `api_port`: runtime-v2 API port, default `9227`.
- `dashboard_port`: runtime-v2 dashboard port, default `9226`.
- `legacy_api_port`: legacy CDPA API port, default `9225`.
- `state_root`: runtime-v2 state root, default `.runtime-v2`.
- `endpoint_config_source`: path or description of the read-only endpoint config source.
- `dashboard_api_proxy_prefix`: default `/api`.
- `endpoint_probe_timeout_seconds`: default `2`.
- `dashboard_min_refresh_seconds`: default `10`.

Owner:

- `RuntimeConfig` / `config.py`.

State authority:

- Owns configuration decisions.
- Does not own runtime state files.
- Does not own endpoint reachability.

Validation rules:

- `api_port` must not equal `legacy_api_port`.
- `dashboard_port` must not equal `api_port`.
- `state_root` must not be the legacy `.runtime` path.
- Missing configured ports must fail explicitly, not silently fallback.

## RuntimeStateStore

Represents the state owner for `.runtime-v2`.

Fields/paths:

- `state_root`: `.runtime-v2`.
- `runs_path`: `.runtime-v2/runs.json`.
- `diagnostics_path`: `.runtime-v2/diagnostics.jsonl`.
- `metadata_path`: `.runtime-v2/metadata.json`.

Owner:

- `RuntimeStateStore` / `state.py`.

State authority:

- Owns all `.runtime-v2` reads and writes.
- Returns typed state read/write results.
- Does not format HTTP responses.
- Does not decide dashboard display fallback.

Validation rules:

- Missing runs file can be treated as empty list.
- Malformed runs file must return a state error and preserve the file.
- Diagnostics must be safe for local display and must not include secrets.

## RuntimeRun

Represents a runtime-v2 run record.

Fields:

- `run_id`: stable runtime-v2 run ID.
- `goal`: operator goal summary.
- `status`: one of `created`, `observing`, `done`, `blocked`, `stopped`.
- `mode`: initial skeleton mode is `observe_only`.
- `endpoint_id`: optional endpoint identifier.
- `created_at`: ISO timestamp.
- `updated_at`: ISO timestamp.
- `last_event`: safe display summary.

Owner:

- `RuntimeStateStore` owns persistence.
- Later run execution owner is out of scope for this skeleton.

Validation rules:

- The skeleton does not need to create real prompt-executing runs.
- No run status may imply send/bind/route behavior in this feature.

## RuntimeDiagnostic

Represents one safe diagnostic entry.

Fields:

- `at`: ISO timestamp.
- `owner`: owner group name such as `RuntimeConfig`, `RuntimeStateStore`, `EndpointInventory`.
- `level`: `info`, `warning`, or `error`.
- `code`: stable diagnostic code.
- `message`: human-readable local message.
- `details`: optional safe structured data.

Owner:

- Emitted by owner groups.
- Written by `RuntimeStateStore` only when persistence is needed.

Validation rules:

- Must not include secrets, tokens, cookies, full prompts, or private browser payloads.
- Must identify the owner that produced the diagnostic.

## EndpointSummary

Represents read-only endpoint inventory output.

Fields:

- `endpoint_id`: stable endpoint ID, for example `9222`.
- `label`: human label when available.
- `cdp_url`: configured CDP URL or redacted safe value.
- `reachable`: boolean.
- `source`: source config path or source description.
- `mode`: always `read_only_inventory` in this skeleton.
- `last_checked_at`: ISO timestamp.
- `diagnostic`: safe summary.

Owner:

- `EndpointInventory` / `endpoint_inventory.py`.

State authority:

- Owns endpoint reachability result.
- Does not own endpoint allocation, browser page ownership, or workflow placement.

Validation rules:

- Offline endpoint is an item-level diagnostic, not whole API failure.
- Malformed config can fail endpoint inventory but must not fail `/api/health`.

## ApiResponseEnvelope

Represents every API response.

Fields:

- `ok`: boolean.
- `data`: object when successful.
- `error`: object when failed.
- `diagnostics`: array.

Owner:

- `RuntimeApiServer` / `api_server.py`.

Rules:

- API server formats the envelope.
- Domain owners return data/errors; they do not write HTTP directly.

## DashboardViewModel

Represents the data displayed by the dashboard.

Fields:

- `api_status`.
- `runtime_config`.
- `runs`.
- `endpoints`.
- `diagnostics`.
- `last_refresh_at`.
- `api_unavailable_message`.

Owner:

- `DashboardServer` / `dashboard_server.py` for display and proxy unavailable state.
- API/domain owners still own the underlying data.

Rules:

- Dashboard must not recompute domain conditions.
- Dashboard displays API/owner results.

## FunctionMapEntry

Represents one planned function or method before implementation.

Fields:

- `symbol`: function/method/class symbol.
- `owner_group`: one of the approved owner groups.
- `purpose`: one sentence.
- `input`: explicit data inputs.
- `output`: explicit result shape.
- `state_authority`: what state/condition it owns, if any.
- `side_effects`: file/network/browser side effects.
- `forbidden_responsibilities`: responsibilities it must not take.
- `failure_reasons`: typed failure cases.
- `planned_tests`: test files or checks.

Owner:

- Planning artifacts own this before implementation.

Validation rules:

- Every implementation task that creates a function/class must be traceable to a function map entry.
- A function with side effects must have an owner group that is allowed to perform that side effect.

## State Transitions

Runtime-v2 skeleton run status:

```text
created -> observing -> done
created -> blocked
observing -> blocked
created|observing|blocked -> stopped
```

This skeleton may store empty or manually created run records only. Prompt execution transitions are out of scope.

## Ownership Summary

| Condition / State | Owner |
|---|---|
| Resolved ports and paths | `RuntimeConfig` |
| `.runtime-v2` file validity | `RuntimeStateStore` |
| API response envelope | `RuntimeApiServer` |
| Dashboard proxy availability | `DashboardServer` |
| Endpoint reachability | `EndpointInventory` |
| Future DOM/LISTEN authority | `ObservationSourceContract`, docs only in this feature |
