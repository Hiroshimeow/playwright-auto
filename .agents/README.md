# Agent Authority Map

`.agents/` is the canonical agent-facing project root.

- `rules/project.md`: single project-wide normative engineering rulebook.
- `skills/`: reusable procedures; procedures reference rules instead of redefining them.
- `knowledge/`: canonical home for new agent-facing learned evidence and patterns; non-normative until promoted into `rules/project.md`. Legacy runtime-owned `LEARNING.md` and `.learning/learning_recovery.md` stay in place until a dedicated tested migration changes their consumers.
- `agents/`: role definitions.

Compatibility/tool roots:

- `.hcu/`: HCUW compatibility metadata. It delegates to `.agents/rules/project.md`.
- `.specify/`: canonical Spec Kit workflow/tooling root. Keep its standard scripts/templates/integrations/layout intact; its project constitution bridges to `.agents/rules/project.md` for engineering policy.
- `specs/`: feature-scoped requirements/contracts/plans/tasks only.
- `AGENTS.md`: bootstrap/orientation compatibility surface required by current workflow tests; it may summarize operational invariants but is not the canonical rulebook.
- `README.md`: human/operator guidance.

Load order for repository work:

1. current explicit user/task instruction;
2. `.agents/rules/project.md`;
3. active feature `specs/<feature>/` contract when applicable;
4. smallest relevant `.agents/skills/` procedure;
5. owning source/tests/configuration.

One rule has one canonical owner. References are allowed; copied competing definitions are not.
