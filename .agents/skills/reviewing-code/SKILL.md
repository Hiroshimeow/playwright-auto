---
name: reviewing-code
description: Use when independently reviewing completed or in-progress code changes for correctness, requirement and test coverage, unnecessary complexity, or merge readiness.
---

# Reviewing Code

## Purpose

Review evidence, not author intent. Optimize for the smallest complete and correct solution. Do not edit source, tests, configuration, documentation, or durable state.

## Authority

Apply review criteria in this order:

1. The caller's explicit scope and requirements.
2. Repository instructions such as `AGENTS.md` or `CLAUDE.md`.
3. `.agents/rules/project.md` when present.
4. Accepted specs, plans, tests, and public contracts relevant to the change.
5. The fallback rules below.

Do not invent missing requirements. Report ambiguity when it materially prevents a verdict. Never create or modify `.agents/rules/project.md` during review.

## Review Procedure

1. Establish the exact review target: git range, working-tree diff, named files, or supplied patch. Preserve unrelated dirty work.
2. Read the requirements and only the surrounding code needed to understand the owning path and invariants.
3. Inspect the actual diff before evaluating it. Trace affected callers, state transitions, failures, and tests where they can change the verdict.
4. Evaluate independently on three axes:
   - **Correctness:** required behavior, invariants, error paths, concurrency, recovery, compatibility, security, and regression risk.
   - **Coverage:** every requirement mapped to implementation and evidence; important branches, boundaries, negative cases, integration points, and realistic tests.
   - **Overengineering:** duplicate owners or live paths, speculative states/gates/layers/configuration, premature generalization, unnecessary compatibility paths, and change beyond scope.
5. Run the narrowest useful repository-native tests, lint, typecheck, or build commands. Broaden only when the changed boundary warrants it. Running verification is allowed; installing dependencies or changing configuration is not.
6. Re-check each candidate finding against the code. Omit style preferences and claims without a concrete failure mode, unmet requirement, or measurable maintenance cost.
7. Give a clear merge-readiness verdict and disclose verification limits.

## Fallback Rules When HCU Is Absent

- Preserve required observable behavior and established contracts.
- One concept should have one authoritative owner and one live implementation path.
- Prefer repairing or extending the existing owner over adding another wrapper, state store, service, registry, retry layer, or abstraction.
- A new abstraction needs demonstrated repetition, a concrete shared invariant, or an independently replaceable boundary.
- Treat external or irreversible side effects conservatively: distinguish not applied, applied, and ambiguous; never authorize blind replay after an ambiguous outcome.
- Validate untrusted input at boundaries and handle errors at the smallest layer able to decide correctly.
- Tests should prove behavior and important failure paths, not merely mock call counts or implementation details.
- Do not demand speculative extensibility, unrelated refactors, or exhaustive low-value tests.
- Prefer deletion, consolidation, and direct control flow when they preserve completeness and safety.

## Severity

- **Critical:** likely data loss, security breach, irreversible duplicate side effect, or fundamentally broken core behavior. Must fix.
- **Important:** real bug, unmet requirement, meaningful regression risk, critical coverage gap, or unjustified complexity that materially harms ownership/reliability. Should fix before merge.
- **Minor:** bounded maintainability or evidence weakness with low immediate risk. Nice to fix.

## Output

Lead with findings, highest severity first. For every finding include:

- severity and axis (`Correctness`, `Coverage`, or `Overengineering`);
- `file:line` or the narrowest available location;
- concrete triggering scenario or missing requirement;
- impact;
- smallest reasonable correction.

Then report:

- **Requirement coverage:** covered, missing, and unverified requirements.
- **Verification:** exact commands run and their outcomes; commands not run and why.
- **Verdict:** `Ready`, `Ready with minor concerns`, or `Not ready`.

If no findings survive verification, say so explicitly, but still state residual risks and test limitations.

## Prohibited Actions

Do not edit files, apply fixes, update snapshots, install dependencies, commit, push, reset, clean, stash, switch branches, launch destructive services, or mutate external systems. Test-created disposable caches/artifacts are acceptable only when normal for the repository; report them if they remain visible.
