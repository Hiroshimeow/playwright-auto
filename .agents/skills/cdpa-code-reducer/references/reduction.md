# System-complexity reduction

## Conserve behavior first
Write the externally required behaviors and safety properties before changing mechanism. If behavior is intentionally changing, separate that decision from simplification.

## Account for the whole mechanism
Count all machinery required to make the behavior true:
- state and representations;
- transitions and branches;
- adapters, wrappers, queues, registries, and schedulers;
- persistence and recovery paths;
- compatibility layers;
- tests and operational controls needed to keep parallel implementations coherent.

A local simplification that pushes complexity elsewhere is not a reduction.

## Derive the first-principles minimum
Ask what must exist if the system were rebuilt from the invariant upward. Then compare the current mechanism with that minimum.

Prefer, in order:
1. delete obsolete behavior or unreachable mechanism;
2. unify duplicated sources of truth and duplicated decisions;
3. reduce state space and transition count;
4. reduce decision space by making one layer authoritative;
5. remove pass-through abstractions with no independent invariant;
6. move safe heuristic guidance from code into agent policy when deterministic enforcement is unnecessary.

Do not add a generic framework to remove a specific duplication.

## Two independent gates

### Behavior gate
Pass only when required behavior, safety boundaries, restart behavior, and operator semantics remain correct.

### Mechanism gate
Pass only when the implementation materially reduces at least one of:
- authoritative states;
- representations;
- branches/transitions;
- duplicated policy decisions;
- layers;
- legacy implementations;
- synchronization burden.

Passing the behavior gate alone proves refactoring safety, not simplification.

Source inspiration: citypaul reduce-system-complexity
https://github.com/citypaul/.dotfiles/blob/main/claude/.claude/skills/reduce-system-complexity/SKILL.md
