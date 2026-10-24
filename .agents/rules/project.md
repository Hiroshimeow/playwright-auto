---
hcu_baseline: "0.5"
derived_at: "2026-09-30"
evidence_fingerprint: "sha256:2959575f8a1f060294d2980a005c978e1cb7b41454d3c20c17611b90bde7508e"
---

# CDPA Project Rules

This file is the single repository-specific normative engineering rulebook. Other repository documents, skills, Spec Kit artifacts, and knowledge files may reference these rules but must not copy them as competing project-wide policy.

## Architecture and ownership

- [user][HCU-ARCH-001] Keep CDPA small and direct: one concept has one owner, one authoritative representation, and one live implementation path. Prefer deletion, consolidation, or repair of the existing owner over adding parallel states, gates, recovery paths, services, stores, or control layers. Preserve exactly-once Send and ambiguous-side-effect safety.
- [user][HCU-ARCH-002] One runtime condition or state transition has exactly one decision/mutation owner. Other layers consume the owner's typed result instead of recomputing the condition or mutating the same state independently. A new owner is allowed only when its responsibility is materially distinct and named.

## Code organization and readability

- [user][HCU-CODE-001] Group functions and methods by responsibility owner/module/class. Code should read top-down from public entrypoint or orchestration method to named decision/I/O helpers and then small implementation helpers. Prefer explicit names, typed boundaries, and straightforward control flow over clever shorthand, hidden dynamic dispatch, boolean-mode functions, code golf, or implicit fallback.
- [user][HCU-CODE-002] Keep each function focused on one responsibility and normally below 100 lines. Exceeding 100 lines requires a clear cohesion reason; split only at a named responsibility boundary rather than by arbitrary line count. Comments explain invariants, ownership, or non-obvious intent, not obvious syntax.

## Rule and artifact ownership

- [user][HCU-POLICY-001] Stable repository engineering policy lives only in `.agents/rules/project.md`. `.agents/knowledge/` is the canonical home for new agent-facing non-normative knowledge; `.agents/skills/` stores reusable procedures; `.agents/agents/` stores role definitions; `AGENTS.md` is bootstrap/orientation; `.hcu/` is HCUW compatibility metadata only; `.specify/` is Spec Kit tooling; `specs/` stores feature-scoped requirements/plans/tasks; `README.md` stores human/operator guidance. Legacy runtime-owned learning surfaces (`LEARNING.md` and `.learning/learning_recovery.md`) remain in place until a dedicated tested migration changes their consumers; they are compatibility/runtime data, not project engineering rulebooks. Feature artifacts may strengthen requirements for their feature but must reference canonical rule IDs instead of redefining project-wide rules.
- [user][HCU-WORKFLOW-001] Keep normal role prompts compact: turn 1 carries identity + workspace + goal + handoff + exact report/route/response contract; later turns of the same role carry identity + previous handoff/report + exact report/route/response contract without repeating the full goal or generic bootstrap. Do not serialize repository skill catalogs or built-in role-constructor bodies into normal workflow prompts. Use SPEC artifacts only when behavior, ownership, lifecycle, recovery, public contract, or dependency ordering genuinely benefits from them.
- [user][HCU-GIT-001] Any role that changes task-owned repository files must checkpoint-commit only those task-owned changes before handoff when the active task authorizes repository checkpointing. REVIEW/TEST/AUDIT create no empty commits. Final PLAN requires all task-owned repository changes committed and the worktree clean except explicitly allowed generated/runtime files. Checkpoint authority never implies push, merge, release, reset, stash, clean, branch switch, or unrelated-file mutation.

## Evidence-backed runtime constraints

- [evidence: README.md] Repository source plus durable runtime state are authoritative. Preserve unrelated dirty/untracked work; never use reset, stash, clean, mass-revert, branch switching, or unrelated overwrite as a repair technique.
- [evidence: README.md] One workflow team owns one browser endpoint/account for its lifetime. Roles do not independently migrate endpoints; if the pinned endpoint is unavailable, continuation waits rather than spilling onto another endpoint.
- [evidence: README.md] Normal Send, Resume, Wait, and Route are exact-execution-identity operations. Do not globally scan unrelated ChatGPT tabs on normal hot paths; reserve global strict scanning for destructive cleanup that requires global certainty.
- [evidence: README.md] Exactly-once Send is a product invariant. Persist request identity before the irreversible boundary, persist accepted provenance after it, and never resolve an accepted or ambiguous Send by blindly replaying it. Manual drafts and attachments remain operator-owned evidence.
- [evidence: README.md] Temporary workflows use Fresh context and never auto-reload because reload can destroy temporary document identity. Persistent roles may continue only their own durable conversation.
- [evidence: README.md] Establish root cause before repair and verify with the narrowest proving checks first.
- [evidence: pyproject.toml] The project targets Python 3.11+ and uses pytest with `src` on the Python path; use repository-native pytest commands for deterministic verification.
