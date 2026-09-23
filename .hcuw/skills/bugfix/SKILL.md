---
name: bugfix
description: Use when implementation violates existing intended behavior, an invariant, or a previously working path and the task is primarily to fix the defect rather than add a new capability.
---

# Bug Fix

1. Reproduce or establish concrete evidence of the failure before editing when practical.
2. Trace the failure to the smallest owning boundary; do not patch symptoms in multiple layers.
3. State the root cause in terms of violated behavior/invariant.
4. Add the narrowest stable regression test when practical.
5. Make the smallest complete fix.
6. Run focused verification, then repository-wide required checks.
7. Check whether the bug exposed stale/duplicated guidance or an unnecessary recovery branch; simplify rather than accumulating another special case.

If the fix requires new durable state, a new gate, a long-lived process/service, or changed conceptual ownership, reclassify it as an architecture/capability change and require a spec.
