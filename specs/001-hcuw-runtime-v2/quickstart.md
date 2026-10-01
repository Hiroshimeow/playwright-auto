# Quickstart: HCUW Runtime V2 Skeleton

**Feature**: HCUW Runtime V2 Skeleton
**Status**: Draft - review before implementation

This guide defines how the minimal web/API skeleton should be validated after implementation. It is intentionally written before implementation so the operator can review the target behavior.

## Pre-Implementation Review

Before source implementation, verify only planning artifacts:

```powershell
cd C:\Users\DuongNH66\Desktop\git\cdpa

git status --short --branch
Get-ChildItem specs\001-hcuw-runtime-v2
Get-ChildItem specs\001-hcuw-runtime-v2\contracts
```

Expected:

- Branch is `refactor/hcuw-runtime-v2`.
- Spec Kit artifacts exist.
- No implementation files under `src/playwright_auto/hcuw_runtime/` are required yet.
- Reviewer approves API 9227, dashboard 9226, `/api/*` proxy, and owner groups.

## Future Implementation Validation: API 9227

After approval and implementation, start API server:

```powershell
cd C:\Users\DuongNH66\Desktop\git\cdpa
uv run python -m playwright_auto.hcuw_runtime.cli start-api --host 127.0.0.1 --port 9227
```

In another shell:

```powershell
Invoke-RestMethod http://127.0.0.1:9227/api/health
Invoke-RestMethod http://127.0.0.1:9227/api/config
Invoke-RestMethod http://127.0.0.1:9227/api/runs
Invoke-RestMethod http://127.0.0.1:9227/api/endpoints
```

Expected:

- `/api/health` returns `ok=true` and port `9227`.
- `/api/config` shows dashboard port `9226`, legacy port `9225`, and `.runtime-v2` state root.
- `/api/runs` returns runtime-v2 runs only, initially empty if no runs exist.
- `/api/endpoints` returns read-only endpoint inventory or safe diagnostics.
- No prompt is sent.
- No ChatGPT tab is adopted.
- No legacy 9225 state file is modified.

## Future Implementation Validation: Dashboard 9226

Start dashboard server:

```powershell
cd C:\Users\DuongNH66\Desktop\git\cdpa
uv run python -m playwright_auto.hcuw_runtime.cli start-dashboard --host 127.0.0.1 --port 9226 --api http://127.0.0.1:9227
```

Open:

```text
http://127.0.0.1:9226
```

Expected visible UI:

- API status card.
- Runtime config card.
- Runs table.
- Endpoints table.
- Diagnostics panel.
- Manual refresh button.
- Last refresh timestamp.

Expected network behavior:

- Browser calls dashboard-local paths such as `/api/health`.
- Dashboard server proxies to API 9227.
- No browser CORS setup is required.
- No automatic retry loop faster than 10 seconds.

## API Unavailable Scenario

Stop API 9227 while leaving dashboard 9226 running, then refresh dashboard.

Expected:

- Dashboard loads.
- Dashboard shows API unavailable state.
- Dashboard does not crash.
- Dashboard does not create a tight retry loop.
- Dashboard does not mutate `.runtime-v2` to hide the error.

## Port Conflict Scenarios

Start any process on 9227, then attempt to start runtime-v2 API.

Expected:

- API startup fails clearly with `port_in_use`.
- It does not fall back to 9225.
- It does not choose another port silently.

Start any process on 9226, then attempt to start dashboard.

Expected:

- Dashboard startup fails clearly with `port_in_use`.
- It does not replace or reuse legacy dashboard behavior.

## Read-Only Endpoint Inventory Scenario

With existing CDP endpoints configured, call:

```powershell
Invoke-RestMethod http://127.0.0.1:9227/api/endpoints
```

Expected:

- Endpoint summaries show configured endpoint IDs/labels/reachability.
- Offline endpoints show item-level diagnostics.
- No tab inventory is required in this skeleton.
- No browser marker is written.
- No message is sent.

## Function Ownership Review

Before approving implementation diff, review:

```powershell
Get-Content specs\001-hcuw-runtime-v2\function-map-template.md
Get-Content specs\001-hcuw-runtime-v2\contracts\code-organization-contract.md
```

Expected:

- Every new owner group has a function map entry.
- Every new state write belongs to exactly one owner.
- Every condition belongs to exactly one owner.
- Public entrypoints expand top-down into named owner methods and helpers.
- No clever/abbreviated code is required by design.

## Test Commands After Implementation

Run narrow tests first:

```powershell
uv run pytest tests/hcuw_runtime -q
```

Then run relevant broader checks only if touched boundaries require them:

```powershell
uv run pytest -q

git diff --check
```

## Stop Rule

This quickstart does not authorize implementation. It defines future validation once the operator approves the plan.
