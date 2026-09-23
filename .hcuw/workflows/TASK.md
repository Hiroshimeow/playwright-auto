# Task Workflow

Use this sequence for non-trivial engineering work.

1. Establish the requested outcome, constraints, and acceptance evidence.
2. Read repository-specific instructions and the current implementation path.
3. Classify the change using `CHANGE.md`.
4. Identify the authoritative owner/state/path affected.
5. Load only the relevant skill(s).
6. If architecture-affecting, locate/create an accepted repository-owned spec before code.
7. Reproduce or establish evidence before editing when fixing a defect.
8. Implement the smallest complete mechanism.
9. Run focused checks, then the repository's full deterministic verification.
10. Review the actual diff and behavior, not the agent's self-report.
11. Remove/retire superseded paths when safe.
12. Update current-state docs/spec status only after verification.

Stop rather than guessing when a side effect outcome is ambiguous or when required authority/evidence is missing.
