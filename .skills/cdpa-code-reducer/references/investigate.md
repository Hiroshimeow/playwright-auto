# Evidence-led investigation

Use this before proposing structural reduction.

## ORIENT
- Find the concrete entrypoint, state owner, tests, configuration, and runtime boundary.
- Prefer the thinnest vertical slice that reaches the behavior under review.
- Record what is known versus assumed.

## TRACE
Trace one real path end to end: input -> decision -> mutation -> persistence -> external effect -> recovery/observation.

For each important claim, classify evidence:
- **SOURCE** — directly proven by current code or configuration.
- **RUNTIME** — directly observed from tests, logs, or execution.
- **INFERRED** — conclusion supported by evidence but not directly observed.
- **UNKNOWN** — evidence is insufficient.
- **CONTRADICTED** — current evidence disproves the claim.

## IMPACT
Before deleting or unifying anything, identify:
- callers and downstream consumers;
- durable state or schema dependencies;
- restart/recovery behavior;
- exactly-once or irreversible boundaries;
- operator controls and diagnostics;
- tests that encode the current contract.

Do not treat "unused in the happy path" as dead code until recovery and compatibility paths are checked.

## VERIFY
Turn assumptions into falsifiable checks:
- prove the path is reachable or unreachable;
- prove duplicate representations agree or identify divergence;
- prove a layer adds or does not add an invariant;
- prove a proposed deletion preserves required behavior.

A reduction proposal is ready only when its causal model explains the current mechanism better than a code-shape guess.

Source inspiration: Data-System-School investigate-codebase
https://github.com/Data-System-School/agent-skills/blob/main/investigate-codebase/SKILL.md
