# playwright-auto - HCUW Project Profile

This file stores factual project identity and declared engineering intent. It is
not the rulebook. Durable normative policy belongs in `.hcu/rules.md`.

## Identity

- Project: playwright-auto
- Stack: Python
- Existing repository instructions: AGENTS.md present
- README: present

## Declared engineering intent

Keep CDPA small and direct. One concept has one owner and one live path. Do not add duplicate states, gates, recovery paths, functions, services, or control layers. Preserve exactly-once send and ambiguous-side-effect safety. Prefer deletion and consolidation over parallel mechanisms.

## Spec persistence

Completed `.hcu/specs/<change>/` artifacts are historical records by default.
Treat a spec as a living contract only when the repository or user explicitly
says so.

## Verification hints discovered at install

Tests:
- uv run pytest -q

Build:
- None detected

Other checks:
- None detected

These are hints only. Repository-local instructions and actual configuration are authoritative.

## Maintenance

Update this profile when project identity, platform, explicit engineering
intent, or spec-persistence policy changes. Do not copy task-specific facts or
temporary incidents here.
