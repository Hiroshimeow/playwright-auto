# Research: HCUW Runtime V2 Skeleton

**Feature**: HCUW Runtime V2 Skeleton
**Date**: 2026-09-30
**Status**: Draft - review before implementation

## Decision 1: Use an isolated runtime-v2 package

**Decision**: Put new code under `src/playwright_auto/hcuw_runtime/` instead of adding more logic to existing large CDPA modules.

**Rationale**: The refactor goal is to make responsibilities easy to locate. A new package creates a clear review boundary while legacy 9225 remains untouched.

**Alternatives considered**:

- Extend `cdpa_worker.py`: rejected because it would continue the current large-owner problem.
- Extend `dashboard_api.py`: rejected because the first skeleton needs isolated API/dashboard behavior and should not mix old and new route authority.
- Separate repository: rejected because this is explicitly a worktree of `Hiroshimeow/playwright-auto` and should share existing tests/tooling.

## Decision 2: API 9227 and dashboard 9226 run separately

**Decision**: API listens on 9227; dashboard listens on 9226.

**Rationale**: Separate processes/ports make failures and ownership clear. API owns state. Dashboard owns display. Neither shares the legacy 9225 mutation path.

**Alternatives considered**:

- Single combined server: rejected for now because it blurs API/dashboard boundaries during the refactor.
- Extend legacy dashboard: rejected because it could accidentally mix old and new task state.
- Use dynamic ports: rejected because operator review requires fixed known ports.

## Decision 3: Dashboard uses `/api/*` proxy to API 9227

**Decision**: Dashboard 9226 serves a static local page and proxies `/api/*` to API 9227.

**Rationale**: The dashboard browser code can call same-origin `/api/health`, `/api/config`, `/api/runs`, and `/api/endpoints`. This avoids CORS setup and keeps the first web UI minimal.

**Alternatives considered**:

- Browser directly fetches `http://127.0.0.1:9227/api/*`: rejected because it adds CORS behavior to the first skeleton.
- Embed API data into HTML at render time only: rejected because manual refresh should call the API without reloading the whole server.

## Decision 4: Prefer Python standard library for the first skeleton

**Decision**: Do not add a web framework dependency unless implementation evidence proves it is needed.

**Rationale**: The existing project already uses standard library HTTP servers in legacy dashboard code. The skeleton only needs a few local JSON/static routes.

**Alternatives considered**:

- FastAPI/Starlette: rejected for this skeleton because it adds dependency and framework conventions before proving need.
- Flask: rejected for the same reason.
- Node/Vite dashboard: rejected because it adds another toolchain to a Python runtime refactor.

## Decision 5: Endpoint inventory is read-only

**Decision**: Runtime-v2 may read endpoint configuration and perform bounded reachability summaries, but may not inspect tabs, claim pages, write ownership, or send prompts.

**Rationale**: The user wants to reuse online CDP endpoints later, but the first milestone must not compete with legacy CDPA.

**Alternatives considered**:

- Adopt existing tabs immediately: rejected because response authority and manual steering rules are not implemented yet.
- Probe all ChatGPT tabs: rejected because global scanning is explicitly not normal hot-path behavior.

## Decision 6: Feature owner map applies canonical ownership rules

**Decision**: Apply `HCU-ARCH-002` from `.agents/rules/project.md` through the concrete owner assignments in `contracts/code-organization-contract.md`.

**Rationale**: The feature needs explicit owner mapping for fault localization, but the project-wide ownership rule must have one canonical definition.

**Alternatives considered**:

- Free functions organized only by filename: rejected because the user requested grouped/class-like functional ownership and condition consolidation.
- One monolithic runtime manager: rejected because it would recreate the current large-function debugging problem.

## Decision 7: Stop before implementation for final review

**Decision**: This pass updates Spec Kit artifacts only and does not create or modify runtime source code.

**Rationale**: The operator requested final review before implementation.

**Alternatives considered**:

- Build the web/API immediately: rejected because it conflicts with the explicit stop-before-implementation request.
