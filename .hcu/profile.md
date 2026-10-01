# playwright-auto - HCUW Compatibility Profile

This file stores HCUW-facing project identity and compatibility metadata. It is not the rulebook.

## Identity

- Project: playwright-auto / CDPA
- Stack: Python
- Canonical agent root: `.agents/`
- Canonical project rulebook: `.agents/rules/project.md`
- Project skill root: `.agents/skills/`
- Feature specification root: `specs/`
- Human/operator guide: `README.md`

## Compatibility

HCUW tools may continue to inspect `.hcu/profile.md` and `.hcu/rules.md`. `.hcu/rules.md` delegates to `.agents/rules/project.md`; project-wide policy must not be duplicated under `.hcu/`.

## Verification hints

Tests:
- `uv run pytest -q`

These are hints only. Repository-local instructions and actual configuration are authoritative.
