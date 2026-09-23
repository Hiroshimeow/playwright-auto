# Change Classification

Classify before implementation.

## Bug fix

A declared behavior/invariant is wrong in implementation.

Requirements:
- establish root cause;
- reproduce or prove the failure;
- add a narrow regression when practical;
- fix at the owning boundary;
- avoid unrelated redesign.

Use `skills/bugfix/SKILL.md`.

## Refactor

Behavior should remain materially unchanged while structure becomes simpler/clearer.

Requirements:
- name preserved behavior/invariants;
- state what duplicate/dead/confusing mechanism is removed;
- verify before/after behavior;
- do not smuggle new product capability into a refactor.

Use `skills/refactor/SKILL.md`.

## Capability change

The system gains or changes operator/user-visible behavior.

A repository-owned spec is normally required when the capability is non-trivial.

Define:
- intended outcome;
- acceptance;
- non-goals;
- ownership/state/gates;
- recovery;
- evidence.

## Architecture change

Conceptual ownership, durable state, process/service, dependency, provider boundary, or authoritative representation changes.

Requires an accepted spec/design decision before implementation.

Use `skills/architecture-change/SKILL.md`.

## Legacy modernization

Working legacy behavior is being moved/simplified.

Preserve useful capability and safety invariants, not file/class/state-machine parity.

Use `skills/legacy-modernization/SKILL.md`.
