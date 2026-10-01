# Runtime V2 API Contract

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Draft - review before implementation
**Ports**: API `127.0.0.1:9227`, dashboard `127.0.0.1:9226`

## Contract Principles

- API 9227 is the source of truth for runtime-v2 state.
- Dashboard 9226 is read-only and may proxy `/api/*` to API 9227.
- API responses are JSON only.
- API must not mutate legacy 9225 state.
- Endpoint inventory is read-only.
- No endpoint in this skeleton may send prompts, adopt tabs, route responses, or repair routes.

## Common Response Envelope

All API endpoints return a JSON object.

Successful responses:

```json
{
  "ok": true,
  "data": {},
  "diagnostics": []
}
```

Failure responses:

```json
{
  "ok": false,
  "error": {
    "code": "string_code",
    "message": "human readable message",
    "details": {}
  },
  "diagnostics": []
}
```

Rules:

- `ok` is required.
- `data` is present only when `ok=true`.
- `error` is present only when `ok=false`.
- `diagnostics` is always an array and must be safe to show on the local dashboard.
- Do not include secrets, auth tokens, full prompt text, or private browser payloads.

## GET /api/health

Purpose: prove the runtime-v2 API process is alive without touching ChatGPT or CDP.

Response data:

```json
{
  "service": "hcuw-runtime-v2-api",
  "status": "ok",
  "host": "127.0.0.1",
  "port": 9227,
  "dashboard_port": 9226,
  "state_root": ".runtime-v2",
  "legacy_api_port": 9225,
  "legacy_isolation": true,
  "version": "0.1.0",
  "branch": "refactor/hcuw-runtime-v2",
  "head": "<git sha when available>",
  "started_at": "2026-09-30T00:00:00+09:00"
}
```

Failure behavior:

- If config cannot resolve, return HTTP 500 with `config_resolution_failed`.
- Health must not fail because endpoint inventory is unavailable.

## GET /api/config

Purpose: expose the resolved runtime-v2 configuration for dashboard display and review.

Response data:

```json
{
  "api": {
    "host": "127.0.0.1",
    "port": 9227
  },
  "dashboard": {
    "host": "127.0.0.1",
    "port": 9226,
    "api_proxy_prefix": "/api"
  },
  "state": {
    "root": ".runtime-v2",
    "runs_path": ".runtime-v2/runs.json",
    "diagnostics_path": ".runtime-v2/diagnostics.jsonl"
  },
  "legacy": {
    "api_port": 9225,
    "writable_state_shared": false
  },
  "endpoints": {
    "mode": "read_only_inventory",
    "source": "cdpa.yaml or resolved legacy endpoint config"
  },
  "limits": {
    "dashboard_min_refresh_seconds": 10,
    "endpoint_probe_timeout_seconds": 2
  }
}
```

Failure behavior:

- Return HTTP 500 with `config_resolution_failed` if the config owner cannot produce a complete config.
- Do not fallback silently to legacy config ports.

## GET /api/runs

Purpose: list runtime-v2 run records only.

Response data:

```json
{
  "items": [
    {
      "run_id": "run_...",
      "status": "created|observing|done|blocked|stopped",
      "goal": "operator supplied goal summary",
      "created_at": "2026-09-30T00:00:00+09:00",
      "updated_at": "2026-09-30T00:00:00+09:00",
      "endpoint_id": "9222",
      "mode": "observe_only",
      "last_event": "string summary"
    }
  ]
}
```

Skeleton scope:

- It is acceptable for `items` to be empty.
- Creating, starting, sending, or routing a run is out of scope until a later approved feature.

Failure behavior:

- If `.runtime-v2/runs.json` is missing, return an empty list and create no file unless the state store initialization path explicitly owns that decision.
- If the state file is malformed, return HTTP 500 with `state_read_failed` and preserve the bad file for inspection.

## GET /api/endpoints

Purpose: summarize configured CDP endpoints in read-only inventory mode.

Response data:

```json
{
  "items": [
    {
      "endpoint_id": "9222",
      "label": "Plus",
      "cdp_url": "http://127.0.0.1:9222",
      "reachable": true,
      "source": "cdpa.yaml",
      "mode": "read_only_inventory",
      "last_checked_at": "2026-09-30T00:00:00+09:00",
      "diagnostic": "ok"
    }
  ]
}
```

Read-only restrictions:

- May read endpoint config.
- May perform a bounded reachability probe.
- Must not inspect all tabs unless a later approved feature adds a scoped tab inventory contract.
- Must not write browser markers.
- Must not send prompt text.
- Must not call legacy worker mutation APIs.

Failure behavior:

- Missing config returns `ok=true` with `items=[]` and a diagnostic explaining the missing source.
- Malformed config returns `ok=false` with `endpoint_config_invalid`.
- One offline endpoint does not fail the entire response.


## GET /api/audit/snapshot

Purpose: expose dashboard-safe, read-only audit state for the 9226 audit view.

Response data:

```json
{
  "sources": [
    {
      "source": "dom|mutation|listen|actions",
      "status": "active|inactive|error",
      "message": "what this source can currently show",
      "updated_at": "2026-09-30T00:00:00+00:00",
      "last_event_id": 1,
      "payload": {}
    }
  ],
  "events": [
    {
      "event_id": 1,
      "source": "actions",
      "kind": "api_request",
      "message": "GET /api/health",
      "created_at": "2026-09-30T00:00:00+00:00",
      "payload": {}
    }
  ],
  "max_events": 300
}
```

Skeleton behavior:

- `actions` is active and records runtime-v2 API/action events.
- `dom`, `mutation`, and `listen` are visible as explicit inactive sources until later source owners attach.
- Inactive sources must not be hidden or faked.
- Audit routes must not send prompts, adopt tabs, inject MutationObserver, parse LISTEN frames, or mutate legacy state in this skeleton.

## GET /api/audit/events

Purpose: expose incremental audit events for realtime dashboard polling.

Query:

- `since=<event_id>`: returns only events with a greater `event_id`.

Response data:

```json
{
  "items": []
}
```
## Dashboard 9226 Contract

Dashboard server responsibilities:

- Serve a minimal static HTML page at `/`.
- Serve minimal static JS/CSS if split files are used.
- Expose `/api/*` as a same-origin dashboard proxy to API 9227.
- Show API unavailable state if proxy cannot reach 9227.
- Remain read-only.

Dashboard UI minimum fields:

- API status.
- API port and dashboard port.
- Runtime state path.
- Git branch/head when available.
- Endpoint table.
- Run table.
- Diagnostics panel.
- Manual refresh button.

Dashboard non-goals:

- No task creation button.
- No send/resume/retry buttons.
- No route repair UI.
- No live transcript stream.
- No tab adoption.
- No login/session controls.

## Owner Groups

Implementation must keep owner groups explicit:

| Owner | Responsibility | May Mutate |
|---|---|---|
| `RuntimeConfig` | Resolve ports, paths, limits, legacy isolation | No runtime state |
| `RuntimeStateStore` | Own `.runtime-v2` state files | `.runtime-v2` only |
| `RuntimeApiServer` | Own API routing and JSON envelopes | No browser state |
| `DashboardServer` | Own static dashboard and API proxy | No runtime state except logs if explicitly delegated |
| `EndpointInventory` | Read-only endpoint config and reachability summaries | Nothing |

Feature owner mapping follows canonical `HCU-ARCH-002` in `.agents/rules/project.md`:

- endpoint reachability is assigned to `EndpointInventory`; dashboard only displays the result;
- state file validity is assigned to `RuntimeStateStore`; API only formats the error.

## POST /api/tasks

Purpose: submit a real CDPA task through runtime-v2 API 9227 while keeping ownership explicit.

Request JSON:

```json
{
  "task": "review this repo without modifying files",
  "team": "hcuw-v2-smoke-review",
  "browser": "9222",
  "solo_role": "PLAN",
  "goal": "Smoke review through runtime-v2 API 9227"
}
```

Owner boundaries:

- `RuntimeApiServer` owns JSON parsing and response envelope only.
- `RuntimeTaskRunner` owns real task submission orchestration.
- `RuntimeStateStore` owns `.runtime-v2/tasks/*.json` persistence.
- Legacy CDPA remains owner of current Send/bind/route/browser behavior until a later runtime-v2 source owner replaces it.

This endpoint may start the legacy CDPA runtime if `127.0.0.1:9225` is unavailable. It must not directly implement DOM, MutationObserver, LISTEN, Send, bind, route, or route repair.
