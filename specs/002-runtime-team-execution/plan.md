# Implementation Plan: Runtime Team Execution

**Branch**: `002-runtime-team-execution` | **Date**: 2026-09-30 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-runtime-team-execution/spec.md`

## Summary

Runtime-v2 becomes the minimal operator-facing execution surface for real CDPA Team Tasks. It submits Team Tasks only, accepts explicit role sets and start roles, supports teams that close via REVIEW when PLAN is absent, preserves exactly-once Send through provisional `local-chatgpt:` races, and exposes enough task/audit state through 9227/9226 for operators and later agents to verify progress. This Spec Kit phase stops before implementation and produces a DEV-agent handoff.

## Technical Context

**Language/Version**: Python >=3.11 for runtime/legacy CDPA; browser-side dashboard JavaScript for 9226 UI.

**Primary Dependencies**: Existing repository dependencies only: Playwright automation stack, standard-library HTTP server utilities, file-backed runtime state, pytest for validation. No new third-party dependency is planned.

**Storage**: Runtime-v2 file state under `.runtime-v2/`; legacy CDPA file/database state under existing `.runtime/` and `.plan/` owners. Runtime-v2 bridge config is generated under `.runtime-v2/task-runner/` and must not mutate `cdpa.yaml`.

**Testing**: `uv run pytest tests/hcuw_runtime -q` plus focused legacy CDPA tests for role selection, route closing role, accepted-send provisional URL behavior, dashboard role selector, and CLI `--roles` behavior.

**Target Platform**: Windows fjp workspace at `C:\Users\DuongNH66\Desktop\git\cdpa`; local ports 9227 runtime-v2 API, 9226 runtime-v2 dashboard, 9225 legacy CDPA API, 9224 legacy dashboard, 9222 browser CDP endpoint.

**Project Type**: Internal orchestration/runtime feature spanning runtime-v2 API/dashboard and legacy CDPA task workflow owners.

**Performance Goals**: Runtime-v2 task submission should return queued/submitted status within 10 seconds when legacy worker is available or startable. Focused dashboard polling should refresh every 0.5 seconds only while the dashboard tab is visible and focused.

**Constraints**: No Solo Task creation from runtime-v2. No Independent Agent creation from runtime-v2. No replay after accepted Send. No `cdpa.yaml` mutation. No route/Send ownership in runtime-v2 API/dashboard. Preserve existing dirty/untracked work and existing legacy behaviors outside this feature.

**Scale/Scope**: Single local operator environment with a small number of active Team Tasks and at most a few configured browser endpoints. This feature is a minimal execution path, not a full legacy worker rewrite.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **HCU-ARCH-001 / one concept = one owner**: PASS. Runtime-v2 owns orchestration request validation, bridge config generation, task records, and audit projection. Legacy CDPA remains the owner of Send, route parsing, task manifests, role execution, and recovery controls.
- **HCU-ARCH-002 / one state transition = one owner**: PASS. Runtime-v2 may enqueue legacy task creation but must not mutate legacy task state directly. Accepted-send handling remains inside legacy worker/task ledger owners.
- **HCU-CODE-001 / group functions by responsibility owner**: PASS. Tasks must keep runtime-v2 task submission logic in a named runner/API boundary and accepted-send logic in legacy worker owners.
- **HCU-CODE-002 / small focused functions**: PASS. Tasks include function-map review and length checks before completion.
- **Spec Kit governance bridge**: PASS. Project-wide policy is referenced from `.agents/rules/project.md`; this feature does not create another rulebook.

## Project Structure

### Documentation (this feature)

```text
specs/002-runtime-team-execution/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── runtime-team-api.md
│   ├── workflow-role-contract.md
│   └── accepted-send-contract.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
src/playwright_auto/hcuw_runtime/
├── api_server.py          # Runtime-v2 request/response boundary for /api/tasks and audit endpoints
├── dashboard_server.py    # Runtime-v2 dashboard projection/proxy; no legacy state mutation
├── task_runner.py         # Runtime-v2 Team Task submission and bridge config owner
├── state.py               # Runtime-v2 .runtime-v2 persistence owner
└── audit.py               # Runtime-v2 bounded audit source state owner

src/playwright_auto/
├── cdpa_cli.py            # Legacy CLI task creation flags, including --roles
├── cdpa_store.py          # Legacy task creation, selected roles, active role owner
├── cdpa_worker.py         # Legacy Send/route/recovery owner
├── cdpa_routes.py         # Legacy route parsing and DONE authority checks
├── cdpa_workflow_agents.py# Legacy workflow role validation and closing role helper
└── dashboard_api.py       # Legacy dashboard API create-task validation

tests/
├── hcuw_runtime/
│   └── test_task_runner.py
├── test_cdpa_core.py
├── test_cdpa_pool_integration_edges.py
├── test_dashboard.py
└── focused legacy test files named by implementation tasks
```

**Structure Decision**: Extend existing runtime-v2 and legacy CDPA owner files. Do not introduce a new orchestration subsystem. Runtime-v2 remains a thin operator surface over legacy Team Task execution.

## Complexity Tracking

No constitution violation is approved. Any implementation that adds a new state owner, silently maps Solo/Independent inputs, or replays accepted Send must be rejected before merge.

## Post-Design Constitution Check

- **HCU-ARCH-001**: PASS. Contracts assign each concept to one owner.
- **HCU-ARCH-002**: PASS. Runtime-v2 contracts prohibit direct legacy state mutation.
- **HCU-CODE-001/HCU-CODE-002**: PASS. Tasks require owner grouping, function-map update, and focused tests.
- **Stop before implementation**: PASS for this Spec Kit phase; `tasks.md` is generated unchecked for a DEV agent.
