---
hcu_baseline: "0.5"
derived_at: "2026-10-12"
evidence_fingerprint: "sha256:5c0f0169a24037d8476df879eecfefed18c3308c35dd8277a0bff385cb94af26"
---

# CDPA Project Rules

- [user] Keep CDPA small and direct: one concept has one owner and one live path. Do not add duplicate states, gates, recovery paths, functions, services, or control layers. Preserve exactly-once Send and ambiguous-side-effect safety. Prefer deletion and consolidation over parallel mechanisms.
- [user] Separate stable policy from per-step execution guidance. `.hcu/rules.md` owns durable engineering policy, `README.md` owns human/operator guidance, and `AGENTS.md` is bootstrap/orientation. Normal role prompts stay compact: turn 1 carries identity + workspace + goal + handoff + exact report/route/response contract; later turns of the same role carry identity + previous handoff/report + exact report/route/response contract without repeating the full goal or generic bootstrap. Do not serialize repository skill catalogs or built-in role-constructor bodies into normal workflow prompts; a custom workflow agent may receive its own custom system prompt on its first role turn. Use SPEC artifacts only when behavior, ownership, lifecycle, recovery, or dependency ordering genuinely benefits from them.
- [user] Local checkpoint commits are authorized by default. After each verified change gate and before role handoff, commit all task-owned changes so work is never orphaned and each gate can be reverted independently. REVIEW/TEST/AUDIT create no empty commits. Final PLAN requires all task-owned repository changes committed and the worktree clean except explicitly allowed generated/runtime files. Checkpoint authority never implies push, merge, release, reset, stash, clean, branch switch, or unrelated-file mutation.
- [evidence: AGENTS.md] Repository source plus durable runtime state are authoritative. Preserve unrelated dirty/untracked work and never use reset, stash, clean, mass-revert, branch switching, or unrelated overwrite as a repair technique.
- [evidence: AGENTS.md] One workflow team owns one browser endpoint/account for its lifetime. Roles do not independently migrate endpoints; if the pinned endpoint is unavailable, continuation waits rather than spilling onto another endpoint.
- [evidence: AGENTS.md] Normal Send, Resume, Wait, and Route are exact-execution-identity operations. Do not globally scan unrelated ChatGPT tabs on normal hot paths; reserve global strict scanning for destructive cleanup that requires global certainty.
- [evidence: AGENTS.md] Exactly-once Send is a product invariant. Persist request identity before the irreversible boundary, persist accepted provenance after it, and never resolve an accepted or ambiguous Send by blindly replaying it. Manual drafts and attachments remain operator-owned evidence.
- [evidence: AGENTS.md] Temporary workflows use Fresh context and never auto-reload because reload can destroy temporary document identity. Persistent roles may continue only their own durable conversation.
- [evidence: AGENTS.md] Prefer the existing owner and remove duplicate paths rather than adding another state store, gate, recovery layer, or abstraction. Establish root cause before repair and verify with the narrowest proving checks first.
- [evidence: pyproject.toml] The project targets Python 3.11+ and uses pytest with `src` on the Python path; use repository-native pytest commands for deterministic verification.
