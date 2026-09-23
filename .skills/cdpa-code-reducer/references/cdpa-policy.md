# CDPA deterministic safety invariants

Simplification must preserve these code-enforced guarantees.

## Send boundary
- An accepted or ambiguous Send is not repeated.
- Exact durable request identity survives recovery.
- Recovery reconciles the original execution before replacement.
- Mutable source or rendered DOM text never becomes replay authority after acceptance.

## Ownership
- Preserve task/team/role ownership and required endpoint/page binding.
- Accepted in-flight work remains tied to persisted execution provenance.
- Browser endpoints provide capacity without erasing durable ownership.

## Persistence and idempotency
- Keep authoritative read/compare/write mutation atomic at its owning application boundary.
- Repeated controls and recovery remain idempotent or fail closed.
- Do not create parallel state stores for the same workflow fact.

## Restart reconciliation
- Recover from durable evidence rather than assuming a clean slate.
- Preserve request, hop, report, control, and conversation provenance needed for safe continuation.
- Missing evidence becomes explicit unknown/blocking state, not permission to duplicate work.

## Browser capacity and locking
- Preserve capacity accounting and ownership-lock semantics.
- Dependency waits do not consume execution capacity unless the scheduler contract requires it.
- Keep global locks away from slow browser, network, and archive work.
- Placement simplification must not weaken exact ownership or cooldown/rate-limit isolation.

## Policy versus code
Judgment-heavy reversible heuristics may move to agent policy. Deterministic code continues to own durable identity, concurrency safety, persistence integrity, irreversible boundaries, and admission/ownership checks.
