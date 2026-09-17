# Authoritative CDPA bounded simplification plan

## Decision and scope
Replace the currently WAITING cleanup proposal. Work in `E:\git-project\playwright-auto-multicdp`. Simplify report-quality policing and role-loop policy; add only a narrow safe undo for controller-created unsent probes. Do not rewrite transport recovery, endpoint selection, task orchestration, dashboard behavior, or downstream features.

The source health task `cdpa-idem-9a17c3402e7a2c99f92f0048` stays PAUSED. Its children are not cleanup fixtures. Do not Resume, Retry, Restart, New Chat, or clear them. Existing repository-wide dirty statistics cannot be attributed to this work.

## Hard invariants
- Accepted/ambiguous Send is never replayed; the durable ledger and existing proven non-acceptance rules remain authoritative.
- Persist selected endpoint/page ownership before browser mutation; preserve mutation-boundary checks and page-local Temporary identity.
- Verify expected report identity/path/hash/size and non-empty content before durable handoff use; preserve route schema and role authorization.
- Preserve human, mixed, unknown, stale, or changed composer state, including non-text state.
- Preserve finite contract repair and substantive handoff recovery without replay.
- Use supported APIs only; preserve unrelated dirty/untracked work. No new orchestration component or database. No commit/push/merge/reset/stash/clean/branch switch.

## Policies, not safety proofs
Engineering quality belongs to REVIEW/PLAN. An Evidence heading is a writing convention, not substantive validation. Prompt closure instructions are useful heuristics. Execution limits bound resource use; reaching one does not establish a defect or justify DONE. Contract-repair exhaustion and resource exhaustion may legitimately require operator action even though neither is an irreversible transport failure. Do not redesign PAUSED/BLOCKED semantics in this task.

## PLAN
1. Inspect current code and relevant tests independently. Treat the prior audit as hypotheses, not authority. Start with `src/playwright_auto/cdpa_worker.py`, `src/playwright_auto/chatgpt.py`, and `src/playwright_auto/cdpa_prompts.py`; verify actual paths and call sites.
2. Record an owned-hunk baseline against the current dirty checkout, not Git HEAD alone. Identify shared-file conflicts and coordinate through existing team mechanisms before editing; if attribution cannot be made safely, stop that hunk rather than overwrite it.
3. Produce a deletion map covering substantive validation/continuation, progress facts, cycle detection, alternating rotation, self-route enforcement, related control eligibility, persisted fields, prompts, and tests. Distinguish safety code from deleted policy callers.
4. Identify an existing general execution budget to reuse. If none exists, implement the single policy below in the current worker and durable task state/history. State the concrete default and exact durable counting/reset mapping before DEV. Prefer 12 autonomous role executions per explicitly authorized budget epoch; adjust only with documented workflow evidence.
5. Map compatibility for existing paused/blocked tasks and legacy route events. No automatic resume, state rewriting, or reliance on deleted error classes. Old policy-block codes must remain inspectable and recoverable through supported controls.
6. Identify the probe creation and undo primitives. If safe full-state comparison and mutation isolation cannot be established with a small change, omit automatic undo, preserve the guard, and report a separately scoped follow-up. Do not introduce a generic provenance framework.

## DEV: minimal report contract
Remove semantic Evidence parsing, placeholder/typed-field checks, substantive continuations, and progress normalization. Accept only an expected non-empty integrity-verified report and valid existing route envelope. Do not infer engineering quality from Markdown. Keep report/route contract repair bounded and preserve locked decisions and substantive handoffs where currently needed. Avoid broad repair-path consolidation.

## DEV: one autonomous execution budget
Use a durable monotonic count of newly allocated autonomous role executions within an operator-authorized epoch, including self-routes, ordinary handoffs, and model repair executions. Charge allocation once using durable hop identity; recovery/reconciliation of the same request must neither charge twice nor replay it. Existing finite transport/contract repair limits remain distinct bounds on different operations.

At budget exhaustion, accept and preserve the completed valid report/decision, then enter an existing operator-action-required nonterminal state before allocating another execution. Do not force DONE, inject an extra PLAN hop, rotate context, or auto-renew the budget. A valid authorized terminal decision may complete without another execution. Normal role changes, evidence edits, restart, context replacement, reconciliation, and service restart do not reset the budget. Only a documented explicit operator renewal through supported controls creates a new epoch. Automatic controls cannot renew it.

This replaces self-route, alternating-route, and evidence-aware cycle policies. REVIEW routes concrete defects to DEV; PLAN decides completion under ordinary routing while budget remains. Compatibility must preserve existing histories and bound old tasks conservatively without auto-starting them.

## DEV: exact unsent probe undo
Prefer avoiding mutating availability probes. When a probe must mutate the composer, create it only from a verified clean full composer on the owned page, with no active or unresolved Send. Under the existing mutation guard, record intent in existing durable storage before mutation and the verified resulting full state afterward. Bind the record to task/hop, endpoint, page/conversation and document generation, and the exact non-text probe identity.

Undo is permitted only when the current full state exactly matches the completed provenance record, ownership and proven pre-Send state still hold, and the operation can target only that artifact. Protect against intervening input using the existing mutation boundary and immediate state revalidation; if the primitive cannot enforce this, fail closed. Use no Send, broad force-clear, refresh, navigation, or New Chat as cleanup. Verify the full clean baseline afterward and invalidate the record. Missing/incomplete records, crash windows, edits, attachments, stale identity, ambiguous Send, or uncertain postconditions fail closed. Repeated successful cleanup is a no-op only when clean state is verified. Never retroactively identify the legacy `mcp-thinkbook` pill as owned merely by label or report narrative.

## Verification and REVIEW
Retain focused regressions for accepted/ambiguous Send across recovery, durable ownership, Temporary relocation/identity, report tampering/path mismatch, and bounded contract repair. Add behavior-level coverage for: clean DEV ΓåÆ REVIEW ΓåÆ PLAN ΓåÆ DONE; a concrete REVIEW defect returning to DEV; finite execution for self/pair/three-role churn; durable budget exhaustion and explicit renewal without reset through restart or repair; safe handling of legacy policy-blocked states.

For undo, test exact owned success without Send, non-text-only state, unknown/human/mixed state refusal, stale ownership, crash/incomplete provenance, intervening input, and uncertain Send refusal. These may be parameterized scenarios rather than one test per internal branch. If undo is deferred for lack of safe primitives, test continued refusal and document the limitation instead of weakening acceptance.

Run focused suites and the existing worker/transport safety bundle relevant to changed code. Prior results (11 focused passes; 98 worker passes and 1 deselection) are baselines, not current verification; explain the deselection. Perform at most one authorized bounded disposable live workflow for changed routing behavior, optionally including a reversible probe on its own clean page. Automated transport tests prove prompt bytes. Never use the paused source or child pages. If live execution is unavailable, report that limitation and obtain operator acceptance rather than claim live proof.

REVIEW challenges every retained policy, checks no automatic budget-renewal loop exists, verifies compatibility and negative composer cases, and rejects lost safety or dirty-work overwrites. Preserve justified complexity.

## Acceptance and handoff
- No arbitrary prose-based engineering-progress classifier or substantive-evidence continuation remains.
- One restart-stable autonomous execution budget replaces overlapping role-cycle enforcement; exhausted work never becomes DONE solely because of the budget.
- Safety/integrity and bounded repair regressions pass; clean closure and defect routing remain possible.
- Probe undo is either proven narrowly safe for newly recorded artifacts or explicitly deferred with protection intact. Legacy automatic cleanup is not an acceptance condition.
- Report deleted/retained mechanisms, compatibility behavior, exact scoped production LOC before/after, tests, live limitations, and remaining risks. Target net-negative production LOC, but neither additions needed for safety nor a LOC target determine correctness.
- Health acceptance is representative safe controller/runtime operation and truthful unresolved blockers, not Release/MCP completion. Leave source and children unchanged pending separate operator decisions.
