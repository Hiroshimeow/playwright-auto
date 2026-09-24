---
hcu_baseline: "0.5"
derived_at: "2026-09-25"
evidence_fingerprint: "sha256:6265ab4dd2dbb5541a9cad3256301dc1a66bee627802337439459e77fa78b0a0"
---

# CDPA Project Rules

- [user] Keep CDPA small and direct: one concept has one owner and one live path. Do not add duplicate states, gates, recovery paths, functions, services, or control layers. Preserve exactly-once Send and ambiguous-side-effect safety. Prefer deletion and consolidation over parallel mechanisms.
- [evidence: AGENTS.md] Repository source and durable runtime state are authoritative; preserve unrelated dirty/untracked work and do not use reset, stash, clean, mass-revert, or unrelated overwrite as a repair technique.
- [evidence: AGENTS.md] One workflow team owns one browser endpoint/account for its lifetime. Roles in that team do not independently migrate endpoints; if the pinned endpoint is unavailable, the task waits rather than spilling to another endpoint.
- [evidence: AGENTS.md] Normal Send, Resume, Wait, and Route are exact-execution-identity operations. Do not globally scan unrelated ChatGPT tabs on normal hot paths; reserve global strict scanning for destructive cleanup that actually needs global certainty.
- [evidence: AGENTS.md] Exactly-once Send is a product invariant. Persist request/accepted-turn identity around the irreversible Send boundary and never resolve ambiguous accepted-Send state by blindly sending again.
- [evidence: AGENTS.md] Manual drafts and attachments are operator-owned evidence and must not be overwritten. Destructive cleanup stays fail-closed.
- [evidence: AGENTS.md] Temporary workflows do not use automatic F5/reload recovery because reload may destroy temporary document identity; recover from exact durable role/page/conversation state or wait for operator action.
- [evidence: AGENTS.md] Prefer removing duplicate gates/state machines over adding another recovery layer; keep non-destructive hot paths local to the exact execution identity.
- [evidence: AGENTS.md] Current verified code plus AGENTS.md outrank stale historical documentation; when a repair changes verified behavior, update stale current-state documentation in the same change.
- [evidence: pyproject.toml] The project targets Python 3.11+ and uses pytest with src on the Python path; use repository-native pytest commands for deterministic verification.
