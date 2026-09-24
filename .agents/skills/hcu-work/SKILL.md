---
name: hcu-work
description: Main HCUW engineering workflow. Initializes durable repository rules once, then uses the minimum sufficient process for implementation work.
---

# HCU Work

Use this skill for non-trivial engineering work in a repository that has HCUW installed.

The goal is not to maximize process. The goal is to produce correct, maintainable code with the smallest sufficient design and the smallest sufficient workflow.

## Limits

HCUW provides engineering defaults and makes policy drift visible. Repository rules can become incomplete or stale. When repository evidence conflicts with a derived rule, reconcile the rule before relying on it.

## Load contract

At the start of an agent session:

1. Read the repository root `AGENTS.md`.
2. Read `.hcu/profile.md`.
3. Run `hcuw fingerprint .`.
4. If `.hcu/rules.md` exists, read it once and compare its `hcu_baseline` and `evidence_fingerprint`.
5. If both match, use the loaded policy for the rest of the session unless the files change.
6. If either differs, use `hcu-rules` for narrow reconciliation; do not regenerate the whole rulebook.
7. If `.hcu/rules.md` does not exist, initialize it once using the bootstrap protocol below before substantive implementation.

After rules are loaded, do not reload this skill or regenerate the rules on every turn.

Explicit user instructions and more specific repository instructions override HCUW rules for the current task.

## First-load rule bootstrap

When `.hcu/rules.md` is missing, create it before substantive implementation.

Do not ask the user to author the initial rules. Derive them from:

- `.hcu/profile.md`;
- existing repository instructions;
- README / package or build manifest;
- current source structure;
- repository-owned tests and lint/build configuration;
- the current implementation where it reveals stable conventions.

Keep discovery bounded. Do not crawl old history, every document, or unrelated source merely to make the rule file look comprehensive.

The managed baseline below remains authoritative in this skill. Do not copy generic baseline rules into `.hcu/rules.md`. The project rulebook contains only repository-specific policy supported by explicit user intent or evidence.

Create a candidate with front matter containing `hcu_baseline: "0.5"`, `derived_at`, and the current `evidence_fingerprint` from `hcuw fingerprint .`. Mark each project-specific rule as `[user]`, `[evidence: path]`, or `[inferred]`. Use `[inferred]` sparingly.

Write the candidate to a unique temporary file under `.hcu/`, then run `hcuw claim-rules . <candidate-path>`. If another agent already created the rulebook, discard the candidate and load the existing file; never merge two first-load rulebooks automatically.

If bounded evidence is insufficient for a project-specific rule, omit it rather than inventing one.

### Managed baseline

#### Product and architecture
- Preserve required behavior and user/operator-visible outcomes.
- Prefer the existing architecture and owning boundary unless evidence shows it is the problem.
- One concept should have one authoritative owner and representation.
- Before creating a new owner or implementation path, check whether the repository already has one for the same responsibility.
- Use one bounded search round with the concept's main nouns/verbs and obvious synonyms; inspect only the strongest nearby matches.
- Reuse, extend, merge, or repair the existing owner when its responsibility already matches. If a near-match is rejected, state the concrete responsibility difference.
- Provider, UI, retry, and transport details stay at their boundary unless they represent a demonstrated product invariant.
- Remove obsolete paths after a replacement is verified instead of running old and new designs indefinitely.

#### Code quality
- Prefer simple, direct control flow over framework-style indirection.
- Keep modules, classes, and functions cohesive; split by responsibility only when there is a real boundary.
- Use clear domain names and explicit types/data shapes where they reduce ambiguity.
- Let repository formatters, linters, type checkers, compilers, and nearby code own mechanically checkable conventions; do not duplicate those rules in `.hcu/rules.md`.
- Prefer deletion or local repair over adding another layer around broken ownership.
- Comments explain non-obvious intent or invariants, not what obvious code already says.
- Handle errors at the smallest layer that can make the correct decision; do not duplicate the same guard across many layers.
- Validate external/untrusted inputs at system boundaries.
- Do not log secrets, credentials, private payloads, or unnecessary sensitive data.
- Function or module size alone is not a reason to split. Split only at a named responsibility boundary with a stable contract.
- Prefer a class only when state survives across calls, that state has an invariant between calls, and the type usefully enforces the invariant; otherwise prefer cohesive functions or plain data plus functions.

#### Change workflow
Use the lowest sufficient scope:
- DIRECT: mechanical/local change with clear behavior and verification; no persistent planning artifact.
- BOUNDED: default for ordinary bug fixes, refactors, and small features; keep outcome, evidence, non-goals, owner, and verification in working context only. Completion means the stated outcome is verified, not merely that code compiles.
- SPEC: only for non-obvious capability, durable lifecycle/state, ownership boundary, public contract, concurrency/restart/recovery semantics, irreversible side effects, or truly cross-cutting dependency-ordered work.

Inside BOUNDED or SPEC, if a named uncertainty can materially change correctness, safety, or architecture, resolve that uncertainty before proceeding. Do not create a separate workflow mode just to label uncertainty.

For SPEC work, create only what is useful under `.hcu/specs/<change>/`:
- `spec.md`: behavior, acceptance criteria, non-goals;
- `plan.md`: smallest viable design when design decisions are non-trivial;
- `tasks.md`: only when dependency ordering or multi-session recovery actually benefits from it.

Do not create artifacts only to prove that a process was followed.

#### Complexity budget
Default budget for each change is zero for:
- new durable states;
- new deterministic gates;
- new long-lived services/processes;
- new dependencies;
- new registries/frameworks/coordination layers;

A non-zero item requires:
1. concrete required behavior or demonstrated failure;
2. why the current owner cannot handle it cleanly;
3. the smallest boundary where it belongs;
4. what existing mechanism it replaces, merges, or simplifies.

Historical incidents do not automatically become global domain states.

#### Failure, recovery, and side effects
- Every non-terminal operation needs an explainable future progress path.
- Distinguish not-applied, confirmed-applied, and ambiguous outcomes for irreversible/external actions.
- Never blindly replay an action whose application may already have succeeded.
- Make retry/recovery idempotent where the product boundary requires it.
- Treat timeouts after an external action begins as ambiguous unless evidence proves otherwise.
- Concurrency/exclusivity rules belong at the resource owner, not duplicated across unrelated callers.

#### Tests and verification
- Reproduce or establish evidence for defects before patching when practical.
- Add a focused regression test for stable bugs/invariants when the repository supports deterministic testing.
- Run the narrowest relevant check first, then broader checks only if the changed boundary can affect them.
- Review the actual diff before finishing.
- Use repository-native lint/type/build/test commands; inferred commands are hints, not authority.
- Do not weaken or delete valid tests merely to make a change pass.

#### Git and workspace safety
- Preserve unrelated dirty and untracked work.
- Do not reset, clean, stash, rewrite history, mass-revert, or switch away unrelated work without explicit authority.
- Keep generated/transient files out of commits unless they are required project artifacts.
- A commit, release, deployment, destructive migration, or external side effect requires the authority expected by the surrounding workflow.

#### Documentation and durable learning
- Update current-state documentation only after behavior is verified.
- Durable architecture decisions belong in the repository's architecture/ADR mechanism if it has one.
- Stable engineering policy belongs in `.hcu/rules.md`.
- Task-only facts stay with the task/spec, not in global rules.
- A recurring procedure may become a skill only when it is reusable and non-obvious.

### Project adaptation

Add only repository-specific rules supported by evidence, such as:
- actual test/build/lint/typecheck commands;
- naming or module conventions already used consistently;
- security or data-handling constraints;
- platform/runtime constraints;
- established ownership boundaries;
- explicit user-provided project intent.

Do not invent architecture or conventions merely to fill sections.

Once created, `.hcu/rules.md` is repository-owned and persistent. Future HCUW updates must not overwrite it.

## Work execution

After loading or bootstrapping rules:

1. Understand the requested outcome.
2. Inspect only the context that can materially affect the decision.
3. Identify the existing owner/boundary.
4. Choose DIRECT, BOUNDED, or SPEC.
5. Before adding a new named responsibility, perform the bounded duplicate-responsibility search.
6. Establish root cause/evidence before a bug fix when practical.
7. Implement the smallest complete change.
8. Verify proportionally.
9. Review the actual final diff against the request, project rules, and the simplification questions below.

## Simplification pass

Before completion, check:

- Did the change add a state, gate, service, abstraction, or config surface that is not required?
- Does any added symbol or path duplicate an existing responsibility?
- Does the change leave two live implementations or owners for one concept?
- Did provider-specific state leak into the domain?
- Can new code be deleted or folded into the existing owner?
- Is an obsolete compatibility path now removable?
- Did implementation exceed the user's requested outcome?

Simplify when the answer is yes.

## Finish

Report concisely:
- what changed;
- verification actually run;
- any real remaining risk.

Do not produce a process diary by default.
