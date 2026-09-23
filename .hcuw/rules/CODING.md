# Coding Rules

These rules protect architecture clarity, not formatting preferences.

## Cohesion

Keep modules aligned with one responsibility or ownership boundary. A module that simultaneously owns persistence, orchestration, provider/browser interaction, recovery, reporting, and operator controls is becoming a giant worker and should be split.

## Explicit domain concepts

Prefer explicit enums/dataclasses/value objects for durable concepts over large mutable dictionaries and stringly typed state.

Do not promote one provider/UI substep or one historical incident into a global domain state unless it represents a durable semantic distinction.

## Direct control flow

Prefer readable normal-path control flow over dispatch tables, hidden callbacks, and policy spread across layers.

Put a safety check at the smallest boundary that owns the failure it prevents. Do not duplicate the same gate in caller, worker, provider, watchdog, and UI.

## Abstractions

Add an abstraction only when it:
- removes demonstrated duplication;
- centralizes a real invariant;
- isolates a replaceable external boundary.

Do not add frameworks for hypothetical future scale.

## Compatibility

Compatibility code must have:
- a concrete consumer;
- a removal/revisit condition;
- tests for the behavior it preserves.

Do not keep retired pathways merely because they once existed.

## Provider/tool boundaries

External quirks belong inside the adapter that owns them. Provider DOM details, CLI output quirks, transport retry semantics, or API-specific state should not leak into global task/domain state unless they represent a demonstrated cross-provider invariant.
