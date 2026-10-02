<!--
Sync Impact Report
- Version change: template -> 1.0.0
- Added principles: Single Owner and Single Live Path; Exactly-Once Side Effects; Exact Execution Identity and User Authority; Regression Evidence Before Repair; Minimal Scope and Workspace Preservation
- Added sections: Runtime and State Constraints; Development Workflow and Quality Gates
- Removed sections: none
- Deferred TODOs: none
-->

# Playwright Auto CDPA Constitution

## Core Principles

### I. Single Owner and Single Live Path
Each domain concept MUST have one authoritative owner and one live implementation path. Repairs MUST extend, simplify, or replace the existing owner rather than add parallel validators, state machines, recovery layers, or durable representations. New abstractions require a demonstrated responsibility that cannot be handled cleanly by the current owner.

Rationale: CDPA correctness depends on reducing duplicated policy and contradictory state.

### II. Exactly-Once Side Effects
Send and other irreversible browser actions MUST distinguish not-applied, confirmed-applied, and ambiguous outcomes. A request that may have crossed an irreversible boundary MUST NOT be blindly replayed. Durable request identity and acceptance evidence MUST be persisted and reconciled before retry or recovery.

Rationale: duplicate prompts and duplicate external actions are more damaging than waiting for exact evidence.

### III. Exact Execution Identity and User Authority
Normal Send, Resume, Wait, Route, and Solo conversation reuse MUST operate on the exact endpoint, page, task, team, and conversation identity that owns the work. A user-supplied valid ChatGPT conversation URL is authoritative for navigation; canonical conversation identity MAY be derived for comparison, but MUST NOT rewrite away a valid user navigation URL. A responding page is live work to observe, not by itself an error condition.

Rationale: provider URL shape and transient response state must not override explicit user intent or exact runtime ownership.

### IV. Regression Evidence Before Repair
Stable defects MUST be reproduced or represented by focused regression tests before or with the repair. Tests MUST cover both the failing path and the safety invariant that prevents over-broad recovery. Narrow proving tests run first; broader suites run when the changed boundary can affect adjacent behavior.

Rationale: orchestration regressions are state-dependent and unit tests that cover only one transition are insufficient.

### V. Minimal Scope and Workspace Preservation
Changes MUST be root-cause focused and MUST preserve unrelated dirty or untracked work. Agents MUST NOT reset, stash, clean, mass-revert, switch branches, push, or overwrite unrelated files without explicit authority. Prefer deletion or consolidation over new state, gates, services, dependencies, or compatibility paths.

Rationale: this repository is frequently worked on concurrently and correctness includes preserving unrelated work.

## Runtime and State Constraints

- Persistent workflows MAY continue only their own durable conversation; temporary workflows use fresh context and MUST NOT rely on reload for recovery.
- Conversation URL parsing MUST have one canonical identity owner shared by API, store, worker, and browser actions.
- Existing-conversation reuse and same-task continuation are distinct concepts and MUST NOT be conflated.
- A Solo task's current Goal is authoritative for that task. Reusing an older conversation MUST NOT suppress the new task Goal.
- Endpoint capacity, ownership, and exact-page binding remain fail-closed for true concurrent ownership conflicts.
- Historical or terminal records MUST NOT be treated as live ownership without current execution evidence.

## Development Workflow and Quality Gates

1. Inspect repository truth, current diff, owning source, and focused tests before editing.
2. For non-trivial behavior or recovery changes, use Spec Kit artifacts as the feature contract: specification, clarification, plan, checklist, tasks, analysis, implementation, then convergence.
3. Write tests for reproduced invariants before production changes where deterministic coverage is practical.
4. Execute tasks in dependency order and mark completed tasks in the feature tasks file.
5. Run focused tests, then relevant broader tests, then `git diff --check`.
6. Review the final diff for duplicate ownership, extra state, and accidental unrelated changes.
7. Commit only when the active workflow explicitly authorizes a checkpoint; never infer push or merge authority.

## Governance

This constitution governs Spec Kit feature artifacts and implementation work in this repository. Existing repository instructions in `AGENTS.md`, `.hcu/profile.md`, and `.hcu/rules.md` remain authoritative where they are more specific and do not conflict with these principles. Amendments require an explicit rationale and semantic version bump: MAJOR for incompatible principle changes, MINOR for new or materially expanded principles, PATCH for clarifications. Every implementation review MUST verify constitution compliance, exactly-once safety, exact ownership, focused regression evidence, and minimal scope.

**Version**: 1.0.0 | **Ratified**: 2026-09-30 | **Last Amended**: 2026-09-30
