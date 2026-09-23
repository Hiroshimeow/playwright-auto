---
name: architecture-change
description: Use when a change adds or changes conceptual ownership, durable state, a long-lived process/service, a provider boundary, a dependency, or an architectural abstraction.
---

# Architecture Change

Read:
- `../../rules/CORE.md`
- `../../rules/CODING.md`
- `../../rules/GATES.md`
- `../../workflows/CHANGE.md`
- the repository's own architecture/current-state docs.

Before code:
1. Name the concrete missing responsibility or broken ownership.
2. Show why an existing component cannot own it cleanly.
3. Create/locate an accepted repository-owned spec.
4. Enumerate new/changed state, gates, processes, dependencies, and provider boundaries.
5. State restart/recovery behavior.
6. State what existing mechanism becomes simpler, merged, or deleted.
7. Define black-box/invariant acceptance evidence.

Reject speculative frameworks whose only justification is possible future scale.

After implementation, verify that architecture docs describe intended ownership while current-state docs describe actual verified code.
