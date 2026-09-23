---
name: side-effect-safety
description: Use when code performs an external or irreversible side effect that may be unsafe to repeat, such as sending a message, clicking submit, charging, deploying, committing/pushing, or mutating remote state.
---

# Side-Effect Safety

Before the side effect:
- establish stable action/request identity;
- persist intent before crossing the irreversible boundary when durability is required;
- define what evidence proves not-applied, applied, or ambiguous.

After execution:
- CONFIRMED/APPLIED: never repeat merely because later observation failed.
- NOT_APPLIED/failed-before-action: retry only when evidence proves the side effect did not occur.
- AMBIGUOUS: stop equivalent replay and reconcile external state first.

Timeout/transport/process loss after action start is ambiguous unless the adapter can prove otherwise.

Put the gate at the side-effect boundary, not globally around unrelated work.

Tests should demonstrate that ambiguous outcomes cannot cause duplicate external mutation.
