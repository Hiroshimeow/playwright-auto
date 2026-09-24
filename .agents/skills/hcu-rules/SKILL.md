---
name: hcu-rules
description: Maintain and reconcile a repository's persistent HCUW engineering rules. Use for durable policy changes, project-rule drift, or rule conflicts; edit policy only and defer implementation work.
---

# HCU Rules

Maintain `.hcu/rules.md` as concise repository-owned policy. Do not turn this skill into an implementation workflow.

## Scope guard

Classify the request before editing:

- **Policy intent**: durable engineering rules, project character, convention ownership, verification policy, architectural guardrails.
- **Non-policy intent**: feature implementation, bug fixes, refactoring, build/deploy work, data migration, or other product changes.

Perform only policy work in this skill. Do not modify application source or tests as a side effect of a rule request. If the request also contains non-policy work, finish the policy edit and list that work as a separate next action. If an instruction is genuinely ambiguous between policy and implementation, ask for clarification before changing either.

## Inputs and precedence

Read once:

1. repository `AGENTS.md`;
2. `.hcu/profile.md`;
3. `.hcu/rules.md` if present;
4. `hcuw fingerprint .`;
5. only repository evidence needed for the requested policy decision.

Explicit current user instructions and more-specific repository instructions outrank HCUW. Project rules outrank the managed generic baseline, but conflicts should be surfaced rather than silently hidden.

## Provenance

Every project-specific rule must carry one marker:

- `[user]` — explicit durable user intent. Never auto-revise or delete it.
- `[evidence: path]` — supported by named repository evidence.
- `[inferred]` — weak inference; use sparingly.

A rule created or changed because the user explicitly requested it becomes `[user]`.

Do not copy mechanically checkable formatter/linter/typechecker/compiler conventions into the rulebook. Keep those tools and configs authoritative.

## Editing policy

Prefer the smallest coherent edit:

1. Find the existing section and rule that own the policy.
2. Modify or replace that rule instead of appending a near-duplicate.
3. Remove obsolete or conflicting derived wording.
4. Preserve unrelated project-specific rules.
5. Keep rules operational and concise.
6. Do not bulk-regenerate the rulebook.

Avoid rule accumulation. One strong rule is better than several overlapping reminders.

## Drift reconciliation

When `hcu_baseline` or `evidence_fingerprint` differs from `hcuw fingerprint .`:

1. Identify which changed evidence can affect existing `[evidence: ...]` or `[inferred]` rules.
2. Re-read only that evidence.
3. Propose or apply the narrowest necessary changes.
4. Never silently modify a `[user]` rule.
5. Remove an `[inferred]` rule when current evidence no longer supports it.
6. Update the front matter to the current baseline, date, and fingerprint after reconciliation.

A fingerprint mismatch is a staleness signal, not a failure gate. Do not block unrelated work when changed evidence cannot affect the loaded rule.

## Missing rulebook

If `.hcu/rules.md` is missing, follow the first-load bootstrap in `hcu-work`. Prepare a candidate and use `hcuw claim-rules` so concurrent agents cannot overwrite each other.

## Profile versus rules

Use `.hcu/profile.md` for relatively factual identity and declared intent:

- project purpose;
- stack/platform;
- explicit engineering character;
- spec persistence policy;
- stable environment facts.

Use `.hcu/rules.md` only for repository-specific normative policy that is not already owned by deterministic tooling.

For polyglot or monorepo projects, keep the root rulebook cross-cutting. Language/package-specific conventions belong in local repository instructions or the relevant tool configuration unless the user deliberately promotes them to project-wide policy.

## Validation

After an edit:

- check for contradictions and near-duplicates;
- check provenance on every changed rule;
- ensure user rules were not silently altered during reconciliation;
- ensure mechanically enforced style was not copied into policy;
- ensure no application code/test changes were made by this skill;
- show the substantive policy change and any deferred next action concisely.
