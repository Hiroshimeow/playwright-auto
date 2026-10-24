# Playwright Auto / CDPA

CDPA is a durable multi-agent workflow runner built on ChatGPT browser sessions. The control plane stores task state, exact Send provenance, role ownership, reports, dependencies, and browser-endpoint placement.

## Runtime

Default local services:

| Service | Address | Purpose |
|---|---|---|
| Dashboard | `http://127.0.0.1:9224` | Kanban, task detail, controls |
| API | `http://127.0.0.1:9225` | Durable task/command API |
| Browser endpoints | registry-managed | Shared ChatGPT execution capacity |

Start or reopen the local runtime:

```powershell
cdpa start
cdpa ui
```

The browser endpoint registry is the source of truth for CDP addresses, capacity, rate-limit scope, and `temporary_default`.

## Create a task

The normal command is intentionally small:

```powershell
cdpa "Repository/worktree: E:\git-project\my-project. Implement X. Preserve dirty work. Verify Y."
```

For most tasks, stop there. CDPA allocates the team identity, chooses browser capacity, starts Fresh context, and persists the workflow.

The task text should contain:

- target repository/worktree;
- required outcome;
- constraints that materially affect execution;
- concrete acceptance evidence.

### Optional overrides

Use flags only when they represent real requirements:

```powershell
cdpa "<task>" --team my-feature
cdpa "<task>" --depends-on <parent-task-id>
cdpa "<task>" --browser 9212
cdpa "<task>" --upload design.md
cdpa "<task>" --temp
```

`--browser` pins the entire workflow team to that endpoint/account. It is not a per-role preference.

`--temp` forces Temporary Chat. Normally it is unnecessary when the selected endpoint already has `temporary_default=true`.

Bootstrap selection is temporarily disabled. New workflow tasks always use Fresh context.

### Dependencies

```powershell
cdpa "Build parent" --team parent
cdpa "Build child" --team child --depends-on <parent-task-id>
```

Dependency waits consume no browser slot.

### Resume

Resume the one nonterminal durable task for an exact team:

```powershell
cdpa --team <exact-existing-team>
```

This preserves task ID, endpoint, active hop, request ledger, conversation ownership, and report history.

There is no public `--reuse-team` flow.

## Browser placement

### One workflow team, one endpoint

Normal pool placement works in two stages:

1. If no `--browser` is supplied, the pool chooses a usable endpoint once at first placement.
2. That endpoint becomes the durable home of the whole team.

PLAN, DEV, REVIEW, TEST, and AUDIT do not independently move between browser endpoints.

If the team endpoint later becomes full, cooling down, or unavailable, the task waits instead of moving another role to a different account/profile.

An explicit `--browser` pins the team immediately.

### Temporary defaults

Endpoint configuration can set:

```json
{
  "temporary_default": true
}
```

If an unpinned task is first placed on that endpoint, the task inherits Temporary mode unless the request explicitly forced another supported mode.

Temporary workflows:

- use Fresh context;
- do not use bootstrap;
- never perform automatic F5/reload recovery;
- preserve the exact live temporary document or wait for recovery/operator action;
- are not grouped into persistent ChatGPT Projects.

## Ownership model

Each workflow role receives an immutable `binding_id` when the task is created.

Durable execution identity is centered on:

```text
team endpoint
    +
exact page_id
    +
role binding_id
    +
task_id / physical_role audit metadata
```

Normal Send and Resume inspect only the exact role candidate. They do not full-scan every ChatGPT tab on the endpoint.

This is deliberate: an unrelated ordinary ChatGPT tab may be stale or slow without affecting another task.

Strict global tab inspection is reserved for destructive cleanup such as Clear Team, where uncertainty must fail closed.

## Exactly-once Send

CDPA preserves the accepted-Send boundary with a durable request ledger.

Conceptually:

```text
exact role page
  -> ownership check
  -> composer / attachment / transcript check
  -> durable request
  -> exact draft
  -> Send once
  -> accepted user-message identity
  -> response
  -> route/report
```

A timeout on an unrelated page is not Send ambiguity.

If an accepted Send is actually ambiguous, recovery must reconcile durable/backend evidence rather than replay the prompt.

## Context

New workflow tasks and newly allocated workflow roles use Fresh context.

Bootstrap donor branching, bootstrap source URLs, and bootstrap prewarm are temporarily disabled for normal task execution. Historical bootstrap metadata may remain readable so old durable state is not corrupted.

The same role may continue its own durable conversation during the same task.

## ChatGPT Projects

Persistent workflow conversations are grouped automatically by execution repository.

For each endpoint/account CDPA:

1. looks for an existing Project with the repository name;
2. creates it when absent;
3. attaches the conversation after accepted Send.

Project grouping is organizational metadata and never blocks substantive execution.

Temporary chats are excluded.

## Controls

Use the dashboard/API controls instead of editing runtime manifests:

- Pause
- Resume
- Retry hop when retryable
- Restart role
- New chat
- Open tab
- Route PLAN
- Change goal
- Stop
- Clear Team

Controls are processed by the durable worker state machine.

## Workflow and HCUW

The default localized workflow is:

```text
PLAN -> DEV -> REVIEW -> PLAN -> DONE
```

TEST or AUDIT are opt-in when a runtime, restart, browser, destructive, environment-specific, cross-cutting, or otherwise unproven boundary needs independent evidence. PLAN alone may route `DONE`.

A Team Task may select a role subset with `--roles` (for example `--roles DEV,REVIEW`). The first selected role in canonical order (`PLAN, DEV, TEST, REVIEW, AUDIT`) starts; a team without PLAN runs `DEV -> REVIEW -> DONE`, and REVIEW is then the only role that may route `DONE`. A team must include PLAN or REVIEW.

Repository work uses HCUW as the durable engineering frame:

1. Load `AGENTS.md`, `.agents/README.md`, and `.agents/rules/project.md` once; use `.hcu/profile.md` only when HCUW compatibility metadata is needed.
2. Resolve rule drift narrowly with `hcu-rules`; do not regenerate the rulebook wholesale.
3. Use DIRECT for mechanical/local changes, BOUNDED for ordinary fixes/refactors/features, and SPEC only for non-obvious capability, durable lifecycle/state, ownership, recovery, public-contract, concurrency, irreversible-side-effect, or dependency-ordered work.
4. For SPEC work, create only useful `specs/<change>/spec.md`, `plan.md`, or `tasks.md`. Clarification/checklist/analyze steps are gates only when a concrete ambiguity or dependency justifies them.
5. Debugging follows evidence -> root cause -> scoped fix -> focused validation. Do not convert incident workarounds into permanent architecture.

Stable project policy lives in `.agents/rules/project.md`; this README explains operator usage; `AGENTS.md` only bootstraps agents; `.hcu/` is HCUW compatibility metadata and `.specify/` remains the canonical Spec Kit workflow/tooling root.

Normal workflow composer payloads are intentionally compact. A role's first turn includes its identity, workspace, current goal, handoff, exact report destination, route contract, and response instruction. Later turns of that same role omit the full goal and generic bootstrap; they continue from the previous handoff/report plus the exact report/route contract. Repository skill catalogs and built-in role-constructor bodies are not serialized into normal workflow prompts; agents load repository guidance through AGENTS/HCUW and the available skill mechanism. A custom workflow agent still receives its own custom system prompt on its first role turn.

Reports are file-backed under:

```text
.plan/<team>/...
```

### Checkpoint commits

When the active task explicitly authorizes repository checkpointing, every role that changes task-owned repository files commits only those task-owned changes before handoff. REVIEW/TEST/AUDIT make no empty commits. Final PLAN requires task-owned repository changes committed and a clean worktree except explicitly allowed generated/runtime files.

Checkpoint authority does not authorize push, merge, release, reset, stash, clean, branch switching, or touching unrelated work.

PLAN owns final closure and the bounded learning pass.

## Windows

Install the CLI if needed:

```powershell
uv tool install --force "git+https://github.com/Hiroshimeow/playwright-auto.git@develop"
uv tool update-shell
```

From the control repository, the local wrapper uses the local source/runtime:

```powershell
cd E:\git-project\playwright-auto-multicdp
cdpa "Repository/worktree: E:\git-project\target. Implement and verify the requested behavior."
```

`--repository` means the task target repository only. It is not the CDPA control repository.

For routine task creation, prefer putting the repository path in the task text and omit `--repository`.

## Direct runtime commands

For development/debugging of the control plane:

```powershell
uv run playwright-dashboard-api --repository . --host 127.0.0.1 --port 9225 --config cdpa.yaml
uv run cdpa-worker --repository . --config cdpa.yaml
```

Do not launch duplicate API/worker instances on the same runtime database.

## Development verification

At minimum after control-plane changes:

```powershell
python -m py_compile <changed Python modules>
python -m pytest -q <focused tests>
git diff --check
```

For ownership/placement changes, include regressions proving:

- unrelated tabs cannot block normal Send/Resume;
- destructive cleanup remains strict;
- one team remains on one endpoint;
- unpinned first placement becomes durably pinned;
- `temporary_default` is inherited at first placement;
- Temporary workflows do not refresh;
- exact accepted Send is never replayed;
- Project auto-grouping reuses or creates the endpoint-local Project and attaches the conversation.

## Design rule

Prefer fewer gates.

A gate belongs on the smallest identity/boundary whose failure it protects:

- exact role ownership -> exact role;
- Send provenance -> exact request;
- endpoint capacity -> team endpoint;
- destructive cleanup -> global strict scan.

Do not make unrelated tabs, unrelated roles, or another endpoint prerequisites for ordinary work.
