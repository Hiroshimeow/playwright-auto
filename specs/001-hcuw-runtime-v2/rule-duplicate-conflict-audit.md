# Rule / Duplicate / Conflict Audit

**Repository**: `C:\Users\DuongNH66\Desktop\git\cdpa`
**Branch**: `refactor/hcuw-runtime-v2`
**Date**: 2026-09-30
**Scope**: agent rules, HCUW adapters, Spec Kit integration, feature specs, legacy learning references, bootstrap contracts. Runtime-v2 implementation is still out of scope.

## Executive result

The repository no longer has competing project-wide rulebooks after this cleanup. `.agents/rules/project.md` is the canonical project engineering policy. `.specify/` remains the canonical Spec Kit workflow/tooling root. `.hcu/rules.md` remains a compatibility pointer because current HCUW/bootstrap contracts still know that path.

Four real topology/precedence conflicts were found during the audit and corrected or contained. One legacy knowledge migration remains intentionally deferred because current source/prompts/tests consume the old path.

## Counts

### Core policy exact duplicates

Audit set:

- `AGENTS.md`
- `README.md`
- `.agents/README.md`
- `.agents/rules/project.md`
- non-Spec-Kit HCU/review/reducer skills
- `.hcu/profile.md`
- `.hcu/rules.md`
- `.specify/memory/constitution.md`

Result:

- Exact duplicate policy groups: **1**
- The only exact duplicate is `evidence_fingerprint` in `.agents/rules/project.md` and `.hcu/rules.md`.
- This duplicate is intentional compatibility metadata, not a second rule definition.

### Spec Kit generated duplication

A normalized exact-line scan found **23 repeated groups** across the installed `speckit-*` skills. The largest groups are the standard hook-dispatch protocol repeated across 10 Spec Kit workflow skills.

Decision: **keep unchanged**. These are generated/upstream workflow instructions and are not repository engineering policy. Editing them for deduplication would risk Spec Kit behavior and would violate the decision to keep `.specify`/Spec Kit integration standard.

### Feature repetition

Keyword occurrence counts across `AGENTS.md`, `README.md`, `.agents`, `.hcu`, `.specify`, and `specs/001-hcuw-runtime-v2`:

- `read-only`: 47 hits / 14 files
- `9225`: 31 hits / 10 files
- `9226`: 48 hits / 11 files
- `9227`: 63 hits / 11 files
- `one owner`: 11 hits / 6 files
- `100 lines`: 4 hits / 3 files
- `clever`: 4 hits / 4 files
- `state transition`: 7 hits / 7 files

These are mostly expected cross-artifact feature traceability: spec -> plan -> contract -> quickstart -> tasks -> checklist. They are not separate project-wide policy owners. The project-wide coding/ownership meaning is now referenced through `HCU-ARCH-*` and `HCU-CODE-*` IDs; feature documents retain concrete acceptance criteria and owner assignments.

## Legacy reference inventory

### `LEARNING.md`

Tracked reference count: **84**.

This is a live legacy operational learning surface with many source/docs/plans consumers. It was not migrated.

### `.learning/learning_recovery.md`

Tracked `.learning/` reference count: **8** total at audit time.

Of those, **5 are live compatibility consumers** outside the topology report:

- `prompts/cdpa/MAINTAINERS.md`
- `src/playwright_auto/cdpa_defaults/prompts/cdpa/MAINTAINERS.md`
- `src/playwright_auto/cdpa_independent.py`
- `tests/test_independent_recovery_learning.py`
- `tests/test_maintainers_prompt_contract.py`

The other references are explanatory history in the topology handoff.

Decision: keep `.learning/learning_recovery.md` in place until a dedicated migration updates source, prompt mirrors, and tests together. Do not duplicate its live contents into `.agents/knowledge/`.

### `.skills/`

Tracked `.skills/` references after migration: **3**, all explanatory references in the topology handoff. No live source/test consumer was found.

Decision: reducer skill migration to `.agents/skills/cdpa-code-reducer/` is safe; old `.skills/` root can stay removed.

### `.hcu/specs`

Tracked references: **0** after cleanup.

All current feature work uses Spec Kit `specs/<feature>/`.

### `.hcu/rules.md`

Tracked references: **12** at audit time, split into:

- 3 active HCUW compatibility references (`hcu-work`, `.hcu/profile.md`, `.hcu/rules.md` itself)
- 5 historical research references in `docs/research/cdpa-agent-workflow-prompt-guide.vi.md`
- 3 explanatory references in the topology handoff
- 1 current bootstrap-contract test reference

Decision: keep `.hcu/rules.md` as a compatibility pointer. Do not put project policy there.

## Real conflicts found

### C1 - HCU SPEC path versus Spec Kit feature path

Old HCU workflow text could direct SPEC artifacts to `.hcu/specs/<change>/`, while the installed Spec Kit workflow and current feature use `specs/<feature>/`.

Resolution: project-local HCU workflow now uses `specs/<change>/`. Audit result: `.hcu/specs` references = 0.

### C2 - Moving `.learning/learning_recovery.md` would break legacy runtime

Initial topology cleanup attempted to move recovery knowledge under `.agents/knowledge/`. Audit of source/prompts/tests showed the old path is a live runtime contract.

Resolution: migration was reverted. `.learning/learning_recovery.md` remains live compatibility/runtime data; `.agents/knowledge/README.md` defines the destination for new agent-facing knowledge without duplicating legacy live content.

### C3 - Over-simplifying `AGENTS.md` would break the current bootstrap contract

`tests/test_cdpa_prompt_bootstrap_contract.py` requires legacy orientation strings such as the HCU skill path, `hcuw fingerprint .`, `.hcu/rules.md`, endpoint ownership, Fresh context, no global tab scan, and bootstrap behavior.

Resolution: `AGENTS.md` remains a compatibility bootstrap/orientation surface while explicitly delegating canonical project policy to `.agents/rules/project.md`.

### C4 - HCU precedence wording could allow feature instructions to silently override project rules

Project-local HCU skills previously said more-specific repository instructions override HCUW rules. That conflicts with the new authority model where feature specs may strengthen/specialize behavior but cannot silently redefine project-wide rules.

Resolution: `hcu-work` and `hcu-rules` now state that explicit current user/task instructions outrank repository policy; feature/local instructions may specialize within scope, but material conflicts with `.agents/rules/project.md` must be surfaced rather than silently overriding it.

## Semantic repetition intentionally retained

Two reusable skill baselines still restate generic ideas similar to `HCU-ARCH-001`:

- `hcu-work` managed generic baseline
- `reviewing-code` fallback review rules

These are not project rule owners. They exist so the reusable skills still work when a repository has no project rulebook. In this repository, `.agents/rules/project.md` has precedence. Do not delete those generic fallbacks merely to reduce textual repetition.

`AGENTS.md` and `README.md` also summarize some runtime invariants because current workflow/tests and human operator documentation require them. They are compatibility/orientation summaries, not canonical policy definitions.

## Safe consolidation completed

- Canonical project rules: `.agents/rules/project.md`
- Agent authority/load-order map: `.agents/README.md`
- Repository reducer skill: `.agents/skills/cdpa-code-reducer/`
- Duplicate reducer safety-policy file removed; reducer references `HCU-*` rule IDs instead
- `.hcu/rules.md`: compatibility pointer only
- `.specify/`: standard Spec Kit structure preserved
- `.specify/memory/constitution.md`: Spec Kit governance bridge, not a second engineering rulebook
- `specs/001-hcuw-runtime-v2/contracts/code-organization-contract.md`: feature owner map referencing canonical rule IDs

## Deferred migration

Do NOT migrate `LEARNING.md` or `.learning/learning_recovery.md` as part of the runtime-v2 skeleton unless the task explicitly includes a legacy learning migration.

A correct future migration must update all source/prompt/test consumers atomically, preserve prompt mirrors, prove existing learning behavior, and keep legacy 9225 operational.

## Verification evidence

- `specify check`: PASS
- `.specify` standard roots preserved: `integrations`, `memory`, `scripts`, `templates`, `workflows`
- `git diff --cached --check`: PASS after whitespace/encoding cleanup
- changed/staged runtime source under `src/`: NONE
- changed/staged tests under `tests/`: NONE
- mojibake scan of changed text: NONE
- compatibility-focused tests: `20 passed`
  - `tests/test_independent_recovery_learning.py`
  - `tests/test_maintainers_prompt_contract.py`
  - `tests/test_cdpa_prompt_bootstrap_contract.py::test_agents_is_bootstrap_orientation_not_duplicate_rulebook`

A broader targeted test command produced `9 failed, 26 passed`; all 9 failures are pre-existing local-versus-packaged role-prompt drift / role-prompt contract failures. No prompt or runtime source file involved in those failures was changed by this topology pass. Do not attribute those 9 failures to the rule consolidation.

## Implementation-agent rule

Before implementing runtime-v2, the agent must treat this audit as a boundary map, not as permission to clean historical documentation or migrate legacy learning paths. Only touch those compatibility surfaces when a dedicated task explicitly includes them and carries tests for their consumers.
