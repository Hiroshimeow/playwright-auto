# HCU Workflow

HCU Workflow is generic engineering guidance installed locally in this repository.

It does not replace project-specific architecture or instructions. More specific repository rules, accepted specs, and explicit user constraints take precedence.

## Read this first

For every non-trivial change:

1. Read the repository's own `AGENTS.md`, architecture docs, and current implementation notes.
2. Read:
   - `rules/CORE.md`
   - `rules/CODING.md`
   - `rules/GATES.md`
3. Classify the change using `workflows/CHANGE.md`.
4. Load only the relevant local skill under `skills/`.
5. If the change introduces a capability, durable state, deterministic gate, long-lived process/service, dependency, provider boundary, or conceptual ownership change, create/locate a repository-owned spec before implementation.
6. Implement the smallest complete mechanism.
7. Run repository-owned deterministic checks and review the actual diff/evidence.
8. Update repository-owned current-state documentation when verified behavior changes.

## Skill routing

- architecture or ownership change → `skills/architecture-change/SKILL.md`
- bug/root-cause fix → `skills/bugfix/SKILL.md`
- behavior-preserving simplification → `skills/refactor/SKILL.md`
- modernizing a working legacy system → `skills/legacy-modernization/SKILL.md`
- irreversible/external side effects → `skills/side-effect-safety/SKILL.md`
- independent review → `skills/review/SKILL.md`

## Repository-owned specs

Use this bundle's `specs/TEMPLATE.md` as a pattern, but keep real specs in the repository's own documentation location (for example `docs/specs/`). HCUW updates must never overwrite project specs.

## Managed content

Everything under `.hcuw/**` is managed by the `hcuw` CLI. Do not edit it directly; improve HCU Workflow at its source repository and run `hcuw update`.
