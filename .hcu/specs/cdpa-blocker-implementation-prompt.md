You are the implementation owner for the reviewed CDPA blocker-recovery specification.

MANDATORY EXECUTION CONTEXT
- Run on md g6 / DESKTOP-B5E4IUE only.
- Repository/workspace: E:\git-project\playwright-auto-multicdp.
- This workflow is admitted as Temporary Chat and pinned to endpoint 9212. Do not migrate to 9202, 9222, or any other endpoint during this task.
- Current branch/workspace contains intentional pre-existing dirty changes and the reviewed spec package. Do NOT reset, stash, clean, discard, or overwrite unrelated work.
- No push, publish, deploy, cache clear, browser/profile reset, or destructive repository operation unless explicitly required and safe.
- The user has now authorized implementation. Do not stop at the pre-implementation gate.

AUTHORITATIVE INPUTS
- Reviewed SPEC-KIT package: E:\git-project\playwright-auto-multicdp\specs\002-cdpa-blocker-recovery-contract
- Implementation tasks: E:\git-project\playwright-auto-multicdp\specs\002-cdpa-blocker-recovery-contract\tasks.md
- External incident ledger: E:\git-project\playwright-auto-multicdp\.hcu\specs\cdpa-blocker-spec-loop-B.md
- Final second-pass review task: cdpa-idem-9ca9cc8720b33efd287bb065
- Final review verdict: CLEAN, CRITICAL=0, HIGH=0, CHK019/CHK020 checked, incident horizon B-001..B-015.

GOAL
Implement the reviewed blocker-recovery contract completely, including the future implementation tasks currently unchecked in tasks.md. Continue through implementation, deterministic tests, integration/no-degradation verification, and independent code review. Do not stop after planning or after an arbitrary phase. Route through DEV/TEST/REVIEW as needed and return DONE only when implementation acceptance is actually proven.

IMPLEMENTATION DISCIPLINE
- Discover and load the relevant skills before acting. In particular use the repository-available ponytail skill at full intensity, test-driven-development, systematic-debugging when failures occur, verification-before-completion, and requesting-code-review/subagent-driven-development when available and appropriate.
- Follow TDD for behavior changes: write the smallest failing test first, observe the expected RED, implement the minimal root-cause change, observe GREEN, then run the relevant regression/full suite.
- Treat the reviewed spec as binding authority. If tasks.md and spec conflict, spec wins; record the smallest ruling needed instead of silently expanding scope.
- Preserve exactly-once Send boundaries, immutable accepted provenance, Team home endpoint, Temporary/Fresh semantics, compact prompt policy, and existing ownership boundaries.
- Do not claim pre-existing dirty source/test changes as evidence that this feature is implemented; verify actual behavior.

ANTI-OVER-ENGINEERING / KISS-DRY-YAGNI
- Before adding any abstraction, ask whether the same requirement can be satisfied by reusing an existing owner/helper or by a smaller local change. Choose the smallest robust root-cause fix.
- Implement only changes necessary to satisfy the reviewed specification and deterministic acceptance tests. Do not add speculative flexibility, scaffolding, extension points, generic frameworks, extra configuration, or "future-proof" layers.
- Prefer deletion/simplification over addition where behavior remains correct. Prefer existing repository patterns, standard library/native mechanisms, and already-installed dependencies over custom machinery.
- No interface with one implementation, no factory for one product, no duplicate registry/ledger/state machine, no second dependency owner, and no wrapper whose only purpose is indirection unless the reviewed spec explicitly requires it.
- Minimize changed files and public surface area. If two solutions satisfy the same invariants, choose the simpler one with fewer states, fewer branches, fewer new concepts, and easier tests.
- DRY applies to real duplicated behavior, not to forcing unrelated cases into a premature abstraction. KISS beats cleverness.
- Complexity must earn its existence with a concrete requirement or failing test. If it cannot, do not add it.
- Do not weaken validation, safety, exactly-once guarantees, or error handling merely to reduce code size.

EXECUTION STRATEGY
- First verify current branch, dirty state, reviewed spec, tasks, and baseline test reality.
- Use the existing workspace as the authoritative implementation workspace. Do not create a new worktree that would omit the reviewed uncommitted spec/evidence. The current feature branch is already isolated from main/master.
- Implement in coherent phases following tasks.md dependencies. Small same-shape edits may be batched when they share one root cause; do not create artificial task fragmentation.
- Prefer specialist subagents for bounded independent review/judgment work if the environment supports them, but do not allow parallel conflicting edits to the same files.
- Maintain a durable implementation progress ledger so compaction/recovery cannot cause completed work to be repeated.
- At useful phase boundaries, run focused tests. Before DONE, run the full relevant suite and a fresh whole-change review.
- Critical/High/Important review findings must be fixed and reverified. Minor speculative cleanup should be deferred unless required by the spec.
- Do not mark tasks complete merely because code exists; require fresh verification evidence.

KNOWN SCOPE TO IMPLEMENT
The reviewed package covers B-001..B-015, including:
- lossless multiline/structured admission and explicit caller-provenance equality;
- accepted conversation identity settling and no-replay reconciliation;
- immutable receipt-divergence fail-closed semantics;
- legal one-time endpoint placement vs immutable Team home endpoint;
- post-acceptance malformed-admission Stop/replacement semantics;
- exact-bound terminal DOM authority and stale LISTEN freshness/watermark behavior;
- authoritative bounded governing task semantics + complete role continuation contract + execution facts/ref;
- no full-Goal/legacy_exact fallback on later substantive turns;
- Temporary generation-loss execution_recovery inheriting parent prompt authority while route/report repair remains locked non-substantive;
- dependency waits as zero-slot derived state with exact parent-cause observability;
- Cloudflare/CDP/session observations as evidence only unless causality is proven.

STOP CONDITION
Return DONE only when:
1. all reviewed implementation requirements are implemented or explicitly proven already satisfied;
2. deterministic tests cover the specified failure modes and pass;
3. the relevant full regression suite passes, with any pre-existing unrelated failures explicitly evidenced and separated;
4. independent code review has no unresolved Critical/High/Important finding;
5. no unnecessary architecture or speculative abstraction was introduced;
6. no push/publish/deploy was performed.

If a runtime/orchestrator blocker prevents safe progress, preserve exact evidence and route the smallest recovery path without replaying an accepted Send.