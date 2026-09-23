---
name: refactor
description: Use for behavior-preserving simplification, code reduction, decomposition of tangled modules, removal of duplicate paths, or cleanup intended to make ownership/control flow easier to understand.
---

# Refactor

Before editing, state:
- behavior/invariants that must remain unchanged;
- duplicated/dead/confusing mechanism being removed;
- evidence that currently protects the behavior.

Prefer:
- one authority instead of synchronized representations;
- direct control flow instead of dispatcher layers;
- cohesive modules instead of giant workers/controllers;
- explicit domain types instead of mutable dictionaries;
- deletion of obsolete paths instead of compatibility forever.

Do not introduce new product behavior under a refactor label.

Verify behavior before/after and review the final diff for net conceptual simplification, not merely file movement.
