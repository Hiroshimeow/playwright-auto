You are task A in an autonomous SPEC-KIT ONLY diagnostic loop for the CDPA multi-endpoint project.

MANDATORY EXECUTION CONTEXT
- Run on md g6 / DESKTOP-B5E4IUE only.
- Repository: E:\git-project\playwright-auto-multicdp.
- This task is spawned as Temporary Chat and pinned to browser endpoint 9202 or 9212. Do not migrate to, request, or use endpoint 9222 or any other endpoint.
- Do not implement production fixes. Stop at the final pre-implementation spec/review gate.
- Existing working tree changes are intentional; do not discard, reset, or overwrite unrelated work.

GOAL
Run one complete spec-kit cycle for the currently observed CDPA blocker classes, reaching reviewed, implementation-ready specifications but stopping before implementation. The current blocker classes include at minimum:
1. accepted_conversation_identity_unresolved after an accepted-user boundary, including recovery/reconciliation without replaying Send.
2. ValueError: task browser_endpoint_id is immutable when creating/restarting/reassigning tasks.
3. Secondary dependency stalls caused by the above blockers.
4. Browser/CDP/Cloudflare/session transitions only where evidence shows they are triggers or contributing conditions; do not assume Cloudflare is the root cause.

SPEC-KIT WORK
- Inspect current code, tests, .hcu/specs, .specify/specs, durable reports, runtime contracts, and recent relevant git diff before writing specs.
- Use the repository's spec-kit conventions and artifacts. Produce/refresh specification, clarification/assumption record, plan/design, task breakdown, and a rigorous spec review as appropriate for this repository.
- Explicitly model: accepted boundary ledger; canonical conversation identity recovery; durable reconciliation; no duplicate Send; task identity vs execution/browser placement; immutable vs mutable fields; temporary endpoint lifecycle; retries/resume/restart-role/new-chat; dependency semantics; observable terminalization; source-drift/reload behavior; compatibility/no-degradation.
- Define invariants, state transitions, failure modes, acceptance criteria, deterministic tests, migration/compatibility expectations, observability fields, and rollback boundaries.
- Challenge over-broad fixes. Prefer minimal explicit contracts over ad-hoc recovery branches.
- Use specialist subagents for independent review if available, but remain in spec/review only.

BLOCKER LEDGER
The orchestration layer maintains E:\git-project\playwright-auto-multicdp\.hcu\specs\cdpa-blocker-spec-loop-B.md. Treat it as external evidence. Read it at the beginning and incorporate every recorded incident into the spec-kit artifacts. Do not delete or rewrite incident history.

STOP CONDITION
The task is DONE only when the spec-kit artifacts have completed review and are implementation-ready, with all discovered incidents addressed by explicit spec requirements/tests. Do not enter implementation, do not modify production code for the fixes, and do not claim runtime bugs are fixed. Return DONE at the pre-implementation gate.