# Handoff: Rule Topology Consolidation Before Runtime-V2 Implementation

**Repository**: `C:\Users\DuongNH66\Desktop\git\cdpa`
**Branch**: `refactor/hcuw-runtime-v2`
**Date**: 2026-09-30
**Status**: Rule/spec topology prepared; runtime implementation has NOT started.

## 1. Purpose

The repository previously had overlapping rule/policy/learning surfaces across `AGENTS.md`, `.agents/`, `.hcu/`, `.learning/`, `.skills/`, `.specify/`, and feature `specs/`. The cleanup establishes one explicit owner for each artifact class before runtime-v2 implementation begins.

The key invariant is the same one required for runtime code:

`one concept -> one canonical owner -> references allowed -> duplicate definitions forbidden`

## 2. Canonical repository layout

```text
.agents/
  README.md                 # authority map and load order
  rules/
    project.md              # SINGLE project-wide engineering rulebook
  skills/                   # reusable agent procedures
  knowledge/                # non-normative learned evidence/patterns
  agents/                   # role definitions

.hcu/
  profile.md                # HCUW compatibility metadata only
  rules.md                  # compatibility pointer to .agents/rules/project.md

.specify/                   # CANONICAL Spec Kit workflow/tooling root - KEEP STANDARD LAYOUT
  integrations/
  memory/
  scripts/
  templates/
  workflows/

specs/
  001-hcuw-runtime-v2/      # feature-scoped requirements/contracts/plan/tasks

AGENTS.md                    # legacy-compatible bootstrap/orientation; not rulebook
README.md                    # human/operator guidance
```

### Important Spec Kit rule

Do NOT move, merge, rename, or replace `.specify/`. It is the standard Spec Kit root and must stay compatible with `specify` workflows. `.specify/memory/constitution.md` is retained where Spec Kit expects it; it is now a governance bridge that delegates repository engineering policy to `.agents/rules/project.md` instead of duplicating that policy.

## 3. Canonical authority and precedence

For implementation work, use this order:

1. current explicit user/task instruction;
2. `.agents/rules/project.md` project-wide engineering policy;
3. active feature contract under `specs/<feature>/`;
4. source/tests/tool configuration within their owning boundary;
5. relevant `.agents/skills/` procedure/defaults.

`AGENTS.md`, `.hcu/rules.md`, `.specify/memory/constitution.md`, skills, knowledge, and feature docs MUST NOT become parallel project-wide rulebooks.

## 4. Canonical project rules added/clarified

The implementation agent must read `.agents/rules/project.md` before coding.

Key IDs:

- `HCU-ARCH-001`: one concept, one owner, one authoritative representation, one live path; prefer deletion/consolidation over parallel gates/stores/services.
- `HCU-ARCH-002`: one runtime condition/state transition has one decision/mutation owner; consumers use the owner's typed result rather than recomputing/mutating the same fact.
- `HCU-CODE-001`: group functions/methods by responsibility owner; code reads top-down from public entrypoint/orchestration to named decision/I/O helpers and then small helpers; avoid clever shorthand/hidden dispatch/implicit fallback.
- `HCU-CODE-002`: functions stay focused and normally below 100 lines; split only at a named responsibility boundary; comments explain invariants/ownership/non-obvious intent.
- `HCU-POLICY-001`: `.agents/rules/project.md` is the only project-wide engineering policy owner; `.specify` is Spec Kit tooling, `specs/` is feature-scoped, `.hcu` is compatibility metadata, `.agents/skills` is procedure, `.agents/knowledge` is non-normative evidence.
- `HCU-WORKFLOW-001`: role prompts stay compact; do not serialize rule/skill catalogs into each role turn.
- `HCU-GIT-001`: task-owned checkpoint policy and workspace safety; no implicit push/merge/reset/stash/clean/branch switch authority.

## 5. Migrations performed

### Learning

The audit found that `.learning/learning_recovery.md` is still a live legacy runtime contract: prompts, source code, and tests reference that exact path. Moving it before a dedicated migration would break legacy behavior.

Therefore:

- `.learning/learning_recovery.md` remains in place as a legacy runtime-owned compatibility surface.
- `LEARNING.md` also remains in place because it has many live operational consumers.
- `.agents/knowledge/README.md` defines `.agents/knowledge/` as the canonical home for new agent-facing non-normative knowledge.
- Live legacy learning content is NOT duplicated into `.agents/knowledge/`.
- A later dedicated migration may move those consumers after tests are changed and legacy 9225 compatibility is proven.

A learned lesson becomes project policy only through reviewed promotion into `.agents/rules/project.md`.

### Repository-local reducer skill

Old:

`.skills/cdpa-code-reducer/`

New:

`.agents/skills/cdpa-code-reducer/`

The old copied policy file `references/cdpa-policy.md` was removed. The reducer skill now references canonical `HCU-*` rules instead of maintaining a second safety policy.

The old `.skills/` root was removed after its reducer skill was migrated. `.learning/` was deliberately retained because legacy runtime consumers still use it.

### Agent bootstrap and HCUW adapters

- `AGENTS.md` remains the legacy-compatible bootstrap/orientation surface required by existing tests, while explicitly delegating canonical project policy to `.agents/rules/project.md`.
- `.hcu/profile.md` now describes compatibility metadata and points to `.agents/`.
- `.hcu/rules.md` is a compatibility pointer only; do not add project rules there.
- HCUW/review skills under `.agents/skills/` were updated to load `.agents/rules/project.md` as the rulebook.
- `.agents/README.md` was added as the agent-facing authority map.

### Spec Kit

- `.specify/` standard structure was preserved.
- `.specify/memory/constitution.md` was changed from a duplicate project rulebook into a bridge that tells Spec Kit workflows to apply `.agents/rules/project.md`.
- Spec Kit-installed `.agents/skills/speckit-*` remain where Spec Kit integration created them.

### Runtime-v2 feature docs

`specs/001-hcuw-runtime-v2/` remains feature-scoped.

The feature's code-organization contract now maps concrete owners to canonical `HCU-ARCH-*` / `HCU-CODE-*` rule IDs instead of redefining those project rules.

The spec/plan/runtime API/research/tasks were adjusted so project-wide ownership/readability requirements are referenced by rule ID while feature-specific assignments remain local.

## 6. Runtime-v2 feature boundary remains unchanged

Planned development surfaces:

- API: `127.0.0.1:9227`
- dashboard: `127.0.0.1:9226`
- legacy API: `127.0.0.1:9225` remains untouched
- runtime-v2 writable state: `.runtime-v2`
- shared CDP endpoints: read-only inventory in the first skeleton

First skeleton MUST NOT implement:

- prompt Send;
- tab adoption/ownership;
- response binding;
- route parsing;
- route repair;
- legacy durable-state mutation.

## 7. Planned owner map for implementation

Concrete feature owner assignments are in:

`specs/001-hcuw-runtime-v2/contracts/code-organization-contract.md`

Current planned owners:

- `RuntimeConfig`: ports, paths, limits, legacy-isolation config.
- `RuntimeStateStore`: `.runtime-v2` persistence and state-file validation.
- `RuntimeApiServer`: 9227 HTTP routing and JSON envelopes; it consumes owner results rather than re-deciding them.
- `DashboardServer`: 9226 static UI and same-origin `/api/*` proxy; it does not own API/state/endpoint semantics.
- `EndpointInventory`: read-only endpoint config and bounded reachability summaries.
- observation modules: later feature only; authority must be explicit before implementation.

Every planned public class/function must be recorded in `function-map-template.md` with owner, input, output, side effects, authority, forbidden responsibilities, failure reasons, and tests.

## 8. Required implementation style

The implementing agent must optimize for human readability and fault localization, not minimum line count.

Expected shape:

```text
public entrypoint
  -> owner/orchestration method
  -> named decision or I/O helper
  -> small implementation helper
```

Do not:

- create a generic runtime manager that owns unrelated state;
- duplicate a condition in API/dashboard/store/inventory layers;
- introduce boolean flags that switch one function between unrelated modes;
- use hidden fallback between 9227/9226/9225;
- compress important flow into dense comprehensions/dynamic dispatch/code golf;
- create another rules file because a local requirement is inconvenient.

## 9. Verification performed

Verified on fjp:

- `specify check`: PASS; Specify CLI reports ready, Codex CLI and Claude Code available.
- `.specify/` still contains its standard top-level roots: `integrations`, `memory`, `scripts`, `templates`, `workflows` plus normal metadata files.
- `git diff --check`: PASS after cleanup.
- `git diff --cached --check`: PASS.
- staged changes under `src/` or `tests/`: NONE.
- compatibility-focused tests: `20 passed` for recovery-learning, Maintainers prompt path, and `AGENTS.md` bootstrap compatibility.
- a broader targeted command had `9 failed, 26 passed`; all 9 failures are existing local-vs-packaged role-prompt drift/contract failures in files untouched by this pass.
- runtime-v2 source directory has not been implemented in this policy/spec pass.

Detailed duplicate/conflict statistics and compatibility decisions are in `specs/001-hcuw-runtime-v2/rule-duplicate-conflict-audit.md`.

## 10. Known limitation / follow-up

The `hcuw` executable is not installed/available on fjp, so the HCUW evidence fingerprint could not be regenerated after moving canonical policy to `.agents/rules/project.md`.

This does not block Spec Kit (`specify check` passes), but if a future environment installs the HCUW CLI and relies on native `.hcu/rules.md` claim/fingerprint behavior, verify the compatibility pointer workflow before relying on automatic HCUW rule reconciliation. Do not move canonical policy back out of `.agents` without explicit user review.

## 11. Git state / commit authority

The topology/spec changes are staged in the worktree for review. No commit and no push were performed by this pass.

No runtime source implementation was created.

## 12. Start condition for the implementation agent

This report documents preparation only. It is not, by itself, permission to implement.

When the operator explicitly authorizes implementation, the agent should:

1. read `AGENTS.md`;
2. read `.agents/README.md` and `.agents/rules/project.md`;
3. read this handoff and `specs/001-hcuw-runtime-v2/rule-duplicate-conflict-audit.md`;
4. read `specs/001-hcuw-runtime-v2/spec.md`, `plan.md`, `contracts/runtime-api.md`, `contracts/code-organization-contract.md`, and `tasks.md`;
5. preserve `.specify/` standard Spec Kit structure;
6. fill/maintain the function map as public functions/classes are introduced;
7. start from the first implementation task after the explicit review gate in `tasks.md`;
8. keep legacy 9225 untouched and prove isolation before adding browser behavior.
