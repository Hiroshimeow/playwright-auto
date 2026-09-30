# Contract: Runtime-v2 Team Task API

## Owner boundary

Runtime-v2 API owns request parsing, validation, response envelopes, runtime-v2 task records, and audit events for Team Task submission. It does not own browser Send, route parsing, or legacy task manifest mutation.

## POST /api/tasks

Creates one real CDPA Team Task through the runtime-v2 orchestration boundary.

### Request body

```json
{
  "task": "Review the current repository changes and report risks.",
  "team": "hcuw-v2-verify-dev-review",
  "browser": "9222",
  "roles": ["DEV", "REVIEW"],
  "start_role": "DEV"
}
```

### Required validation

- `task` must be non-empty.
- `team` must be non-empty or a generated team name must be explicit in the response.
- `roles` must be a non-empty array of unique workflow role keys (`PLAN`, `DEV`, `TEST`, `REVIEW`, `AUDIT`); it is stored in canonical order.
- `start_role` must be a valid role key and must exist in `roles`; when omitted it defaults to the first selected role in canonical order (never a hardcoded PLAN).
- `roles` must include `PLAN` or `REVIEW`.
- Only `task`, `team`, `browser`, `roles`, `start_role`, and `workflow_mode=team` are accepted; any other field (including `solo_role`, `role`, `conversation_url`, `goal`, and independent-agent fields) fails closed.
- Validation failure must occur before calling legacy API/CLI and returns HTTP 400 with `invalid_task_request`.
- Runtime-v2 forwards `roles` and `start_role` unchanged to legacy `POST /api/tasks`; the task text is not rewritten.

### Success response

```json
{
  "ok": true,
  "data": {
    "runtime_task_id": "rtask-...",
    "mode": "team",
    "status": "submitted",
    "team": "hcuw-v2-verify-dev-review",
    "browser": "9222",
    "roles": ["DEV", "REVIEW"],
    "start_role": "DEV",
    "legacy_task_id": "cdpa-idem-...",
    "legacy_command_id": "cmd-...",
    "legacy_status": "queued",
    "legacy_runtime": {"status": "already_running", "api": "http://127.0.0.1:9225"},
    "created_at": "2026-09-30T00:00:00+00:00"
  },
  "diagnostics": []
}
```

### Error response

```json
{
  "ok": false,
  "error": {
    "code": "invalid_task_request",
    "message": "start_role must be included in roles",
    "details": {}
  },
  "diagnostics": []
}
```

## GET /api/tasks

Returns runtime-v2 task records and projected legacy status when available.

### Required response data

Each item must include:
- `runtime_task_id`
- `team`
- `browser`
- `roles`
- `start_role`
- `legacy_task_id`
- `legacy_command_id`
- `status`
- `created_at` and latest update timestamp when available
- block code when projected from legacy board

Implemented projection fields: items are newest-first; `latest=true` marks the current verification task; `submission_status` keeps the runtime-v2 submit status while `status`, `active_role`, `block_code`, `updated_at`, and `legacy` are projected read-only from the legacy board when the legacy task is visible there.

## POST side effects

Allowed:
- create/update `.runtime-v2/task-runner/legacy-cdpa-bridge.json`;
- start legacy runtime process if 9225 is unavailable;
- call legacy task creation API/CLI once after validation;
- persist runtime-v2 task record.

Forbidden:
- modify `cdpa.yaml`;
- create Solo Task;
- create Independent Agent;
- write legacy task manifests directly;
- send ChatGPT prompts directly from runtime-v2 API;
- route or repair legacy tasks directly from runtime-v2 API.
