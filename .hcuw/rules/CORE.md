# Core Engineering Rules

## Preserve the product, not accidental structure

Start from required behavior, operator/user-visible outcomes, incidents, tests, and real constraints. Existing code is evidence, not automatically the architecture.

When modernizing working software, preserve proven capabilities while removing duplicated state, duplicated policy, and incidental coupling.

## One concept, one authority

For each important concept identify:
- its owner;
- authoritative representation;
- legal transitions;
- restart/recovery behavior;
- observable evidence.

Do not create a second queue, scheduler, state store, registry, coordinator, or report-as-authority to solve a local problem.

## Agents decide semantics; deterministic boundaries enforce safety

Use agent judgment for:
- decomposition;
- design alternatives;
- root-cause analysis;
- implementation;
- semantic review.

Use deterministic code/tests for:
- ownership;
- durability;
- idempotency;
- capacity/exclusivity;
- dependency integrity;
- destructive authority;
- accepted/ambiguous side-effect boundaries.

## Progress must be explainable

A nonterminal operation must have an explicit future progress path: active owner, dependency, external prerequisite, retry condition, or recovery/reconciliation path.

Do not hide stalled work behind generic states or prose-only reasons.

## Prefer deletion over parallel evolution

When a new path replaces an old one, retire/remove the obsolete path once evidence permits. Keeping both active indefinitely is architecture debt.
