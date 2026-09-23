---
name: review
description: Use for independent review of a code or architecture change against its requested behavior, repository rules, accepted spec, invariants, and deterministic evidence.
---

# Independent Review

Review the actual diff/repository state, not the implementer's narrative.

Check:
1. acceptance behavior;
2. invariant preservation under failure/restart/concurrency;
3. ownership and source-of-truth clarity;
4. unnecessary new states/gates/processes/dependencies;
5. duplicate/retired paths left behind;
6. provider/tool details leaking into global domain logic;
7. deterministic evidence quality;
8. docs/spec/current-state consistency.

Findings must be concrete, actionable, and reproducible.

Do not fail a change for generic preferences that are not tied to correctness, maintainability, or repository rules.
