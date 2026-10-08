You are task A2 in the second autonomous SPEC-KIT ONLY diagnostic/review loop for the CDPA multi-endpoint project.

MANDATORY EXECUTION CONTEXT
- Run on md g6 / DESKTOP-B5E4IUE only.
- Repository: E:\git-project\playwright-auto-multicdp.
- This task is admitted directly through the loopback API as Temporary Chat and pinned to endpoint 9212. Do not migrate to, request, or use 9202, 9222, or any other endpoint during this task.
- SPEC/REVIEW ONLY. Do not implement production fixes. Do not edit src/ or production tests as part of the blocker fixes. Stop at the final pre-implementation review gate.
- Existing dirty working-tree changes are intentional and pre-existing. Do not reset, stash, clean, discard, or overwrite unrelated work.

INPUTS
- External blocker ledger: E:\git-project\playwright-auto-multicdp\.hcu\specs\cdpa-blocker-spec-loop-B.md. Read all incidents, currently B-001 through B-012, including the two historical B-002 entries. Do not delete or rewrite incident history.
- Reviewed spec-kit package: E:\git-project\playwright-auto-multicdp\specs\002-cdpa-blocker-recovery-contract.
- Prior clean pass: task cdpa-idem-773db9d9bd12297c3fb10b1b on endpoint 9202 reached DONE at the pre-implementation gate after independent REVIEW.

GOAL
Perform a fresh adversarial second-pass SPEC-KIT review using endpoint 9212. Re-open repository truth, current spec artifacts, durable runtime/report evidence, and the blocker ledger. Verify that every B incident is represented by an explicit owner, invariant, transition, acceptance criterion, and deterministic future test. Look specifically for contradictions, missing owners, unsafe implicit recovery, accidental Send replay, endpoint migration, Temporary/persistent drift, governing-contract loss, stale LISTEN precedence, accepted-identity ambiguity, replacement semantics, dependency stalls, and any gap exposed merely by running this second pass on a different allowed temporary endpoint.

WORKFLOW
- If a specification defect is found, correct SPEC-KIT artifacts only and route through PLAN/REVIEW until independently clean. Do not route into production implementation merely because a concrete bug exists.
- If the CDPA runtime/orchestrator itself BLOCKS, times out, corrupts prompt/identity/endpoint/mode, loses the governing contract, reloads Temporary work, or otherwise behaves unexpectedly, preserve exact durable evidence in the role report. The external controller will diagnose it, append a new B incident, and resume/restart safely. Never replay an accepted Send.
- Cloudflare/CDP/session observations are evidence only unless causality is independently proven.
- Do not mark implementation tasks complete. Production behavior remains NOT_IMPLEMENTED/NOT_MEASURED until the user separately authorizes implementation.

STOP CONDITION
Return DONE only after a fresh independent spec review finds no unresolved CRITICAL/HIGH issue and all known B incidents are explicitly covered, while all production implementation remains unstarted. If a runtime blocker prevents safe progress, stop without improvising around exactly-once boundaries and leave precise evidence for the external monitor.