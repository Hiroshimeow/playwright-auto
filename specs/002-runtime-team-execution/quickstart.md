# Quickstart: Runtime Team Execution Validation

## Purpose

Validate the feature before handoff to implementation review. These steps are for a DEV agent after implementation, not for this Spec Kit phase.

## Prerequisites

- Working directory: `C:\Users\DuongNH66\Desktop\git\cdpa`
- Browser CDP endpoint available on 9222 or a known replacement endpoint.
- Runtime-v2 API/dashboard can run on 9227/9226.
- Legacy CDPA API/dashboard can run on 9225/9224.
- No more than the operator-approved active task capacity on the endpoint.

## 1. Static validation

```powershell
uv run pytest tests/hcuw_runtime -q
uv run pytest tests/test_cdpa_core.py tests/test_cdpa_pool_integration_edges.py tests/test_dashboard.py -q -x
uv run python -m compileall src/playwright_auto/hcuw_runtime src/playwright_auto/cdpa_worker.py src/playwright_auto/cdpa_store.py src/playwright_auto/cdpa_workflow_agents.py src/playwright_auto/cdpa_routes.py src/playwright_auto/dashboard_api.py
```

Expected:
- runtime-v2 tests pass;
- focused role/accepted-send tests pass or only known unrelated dashboard JS/CommonJS failures are documented;
- compileall succeeds.

## 2. Start or reload services

```powershell
uv run python -m playwright_auto.hcuw_runtime.cli start-api
uv run python -m playwright_auto.hcuw_runtime.cli start-dashboard
```

Expected:
- 9227 and 9226 listen.
- Submitting a task starts or uses legacy 9225/9224 through the runtime-v2 bridge config.
- `.runtime-v2/task-runner/legacy-cdpa-bridge.json` has `independent_agents.seed_builtins=false`.

## 3. Submit DEV+REVIEW task through runtime-v2

```powershell
$payload = @{
  task = 'Runtime-v2 validation: review current repository changes at a high level, write required report, and finish when accepted.'
  team = 'hcuw-v2-verify-dev-review'
  browser = '9222'
  roles = @('DEV','REVIEW')
  start_role = 'DEV'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:9227/api/tasks -ContentType 'application/json' -Body $payload
```

Expected:
- Response contains `mode=team`, `roles=[DEV,REVIEW]`, `start_role=DEV`, legacy task id, and legacy command id.
- Legacy board shows exactly DEV and REVIEW roles.
- Active role starts as DEV.
- No Solo Task or Independent Agent is created.

## 4. Verify local-chatgpt accepted-send behavior

Observe live events or audit while DEV sends:

Expected:
- Initial send acceptance may show `https://chatgpt.com/c/local-chatgpt%3A...`.
- Task remains RUNNING/WAITING, not immediately BLOCKED.
- Later events show canonical `/c/<id>` for the same page.
- There is one send click/acceptance for the request id.

## 5. Verify REVIEW closing

Expected:
- DEV routes to REVIEW.
- REVIEW writes its report and routes DONE.
- Task reaches DONE without PLAN.
- `/api/tasks` and 9226 show DONE for the verification task.

## 6. Negative validation

Submit invalid requests:

- `workflow_mode=solo`
- `role=DEV`
- `conversation_url=https://chatgpt.com/c/...`
- `roles=['DEV','TEST']`
- `roles=['DEV','REVIEW']`, `start_role='PLAN'`

Expected:
- Each invalid request fails before legacy enqueue.
- No new legacy command appears for rejected requests.

## 7. Operator handoff evidence

Collect:
- `/api/tasks` response for the verification task;
- `/api/audit/snapshot` source states;
- legacy board item for the verification task;
- report paths under `.plan/<team>/`;
- focused test command output.

## Implementation validation notes (2026-09-30)

- `pytest tests/hcuw_runtime -q`: 52 passed.
- Focused feature subset (`tests/hcuw_runtime`, `tests/test_cdpa_core.py`, `tests/test_cdpa_pool_integration_edges.py`, `tests/test_cdpa_cli_runtime.py`, new dashboard/API role tests): 126 passed.
- Broader legacy regression run (worker, commands, routes, prompts, operator resume, dashboard API, exporter): 550 passed, 18 failed. The same 18 fail on a clean `HEAD` worktree and are unrelated: prompt asset mirror/contract tests (`test_cdpa_prompt_bootstrap_contract.py`, `test_plan_prompt_contract.py`, `test_cdpa_pause_route.py`) and six `test_operator_resume_recovery.py` fixture failures.
- `tests/test_dashboard.py` Node-based JS tests fail in this environment because Node loads `dashboard_assets/*.js` as CommonJS; unrelated to this feature.
- T011: the accepted-send, closing-role, CLI `--roles`, and dashboard tests were written against the prior blocking/PLAN-only behavior (the old same-page provisional test asserted an immediate block); runtime-v2 tests for `roles` required, explicit `start_role` forwarding, and 400 validation failed on the previous task runner, which prepended a start-role instruction to task text instead of forwarding `start_role`.
- Live smoke (sections 2-5) requires restarting the legacy worker and sending real ChatGPT prompts; it is pending explicit operator authorization.
