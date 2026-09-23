---
name: cdpa-code-reducer
description: Use when simplifying, reviewing, or refactoring CDPA implementation complexity without weakening transport, ownership, persistence, recovery, or idempotency guarantees.
---

# CDPA Code Reducer

Reduce CDPA complexity only after proving current behavior and safety boundaries.

## Core workflow

1. **Investigate first.** Use ORIENT / TRACE / IMPACT / VERIFY from [references/investigate.md](references/investigate.md). Build claims from source and runtime evidence; label uncertainty.
2. **Account for the whole mechanism.** Identify every state owner, recovery path, policy decision, compatibility path, and irreversible boundary involved.
3. **Define conserved behavior.** Write the behaviors and invariants that must survive before proposing deletion or unification.
4. **Derive the minimum mechanism.** Prefer deleting obsolete paths, unifying duplicated decisions, reducing representations, state space, and decision space, and removing pass-through layers.
5. **Apply separate gates.**
   - **Behavior gate:** required behavior and safety invariants still hold.
   - **Mechanism gate:** the implementation has genuinely less state, branching, duplication, or machinery.
6. **Record the reduction.** Use [templates/reduction-ledger.md](templates/reduction-ledger.md) for non-trivial changes.

## Required CDPA boundary

Before reducing orchestration, read [references/cdpa-policy.md](references/cdpa-policy.md). Never trade away accepted/ambiguous Send non-replay, durable request identity, exact endpoint/page ownership, atomic persistence/idempotency, restart reconciliation, or browser-capacity/locking correctness.

## Reduction targets

Prioritize:
- duplicated policy or recovery decisions;
- dead legacy paths and obsolete workflow implementations;
- redundant durable/in-memory representations;
- pass-through wrappers without an independent invariant;
- supervisory heuristics that can safely become agent policy instead of code;
- branches made unnecessary by one authoritative state model.

Do not optimize for line count alone. The target is the smallest mechanism that remains complete, observable, and recoverable.

## References

This skill is an original synthesis informed by:
- Data-System-School, **investigate-codebase**: https://github.com/Data-System-School/agent-skills/blob/main/investigate-codebase/SKILL.md
- citypaul, **reduce-system-complexity**: https://github.com/citypaul/.dotfiles/blob/main/claude/.claude/skills/reduce-system-complexity/SKILL.md
- citypaul source notes: https://github.com/citypaul/.dotfiles/blob/main/claude/.claude/skills/reduce-system-complexity/references/source-notes.md
