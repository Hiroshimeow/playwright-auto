# Specification Quality Checklist: CDPA Blocker Recovery Contracts

**Purpose**: Validate specification completeness and internal quality before independent review.  
**Created**: 2026-10-04  
**Feature**: ../spec.md

## Scope and content quality

- [x] The cycle is explicitly specification/review only and does not claim any runtime bug is fixed.
- [x] Every incident currently recorded in the external blocker ledger is mapped to explicit requirements and future acceptance evidence.
- [x] No unresolved NEEDS CLARIFICATION, TBD, TODO, or template placeholder is intentionally left in the feature contract.
- [x] Non-goals exclude anti-bot bypass, endpoint migration of accepted work, broad runtime rewrites, and production implementation.

## Requirement completeness

- [x] Accepted/ambiguous Send non-replay and exact accepted provenance are explicit.
- [x] Provisional identity settling has exact entry evidence, a corrected first-unresolved-decision timing anchor, persisted deadline, restart semantics, response-deadline cap, success, and fail-closed outcomes.
- [x] Unchanged-receipt unresolved identity is separated from immutable receipt divergence, which has explicit non-retryable block classification, illegal continuation controls, and explicit Stop as the only in-task exit.
- [x] Accepted-send reconciliation preserves its existing explicit operator Route PLAN gate.
- [x] Temporary-chat behavior remains separate from persistent saved-conversation identity behavior.
- [x] Workflow home endpoint, role execution binding, legal one-time placement, generic immutability, and legacy fail-closed semantics are distinct.
- [x] Windows multiline admission, option leakage, exact canonicalization, Team/Solo parity, and pre-Send integrity failure are specified, including supplied/omitted provenance so explicit values equal to defaults cannot be mistaken for defaults and initial placement has a strict mutation whitelist.
- [x] Post-acceptance admission corruption defines BLOCKED → explicit operator Stop → existing replacement and preserves accepted history.
- [x] Replacement executable authority, Team/Solo semantics, dependency/context preservation, and non-executable retained history are unambiguous.
- [x] Exact DOM terminal authority, optional completion controls, stale LISTEN non-veto semantics, and LISTEN freshness ownership are specified, including observer-instance restart invalidation, 5-second age bound, and one-consumption watermark semantics.
- [x] Snapshot-cache requirements are limited to signals actually required for terminal arbitration.
- [x] Later substantive Team hops receive mandatory semantics in-band through a required authoritative bounded semantic payload plus verified ref; create/change-goal ingress, exact canonical manifest binding, fail-closed missing/invalid/oversize behavior, and no full-Goal fallback are specified without model inference.
- [x] Goal revision and repair-prompt boundaries are consistent with the governing-reference contract.
- [x] Dependency waiting, parent attribution, zero-slot behavior, release, and unsuccessful-parent outcomes are covered.
- [x] Cloudflare/challenge/session conditions remain evidence-only rather than assumed root cause.
- [x] Observability, rollback, compatibility/no-degradation, and no-duplicate-owner constraints are defined.

## Acceptance quality

- [x] Each P1 blocker has deterministic positive and negative future acceptance scenarios.
- [x] The accepted-identity acceptance case reproduces the observed ~34-second accepted-to-decision and ~92-ms decision-to-canonical timeline.
- [x] Terminal-response acceptance covers delayed/missing action buttons plus stale IS_STREAMING and proves no reload/status/state-repair after terminal DOM.
- [x] Governing-contract acceptance covers PLAN→REVIEW→PLAN, revisions, restart/new-chat/reconciliation, Temporary recovery authority inheritance (including locked repair), pre-Send source mismatch, and compactness.
- [x] Replacement acceptance proves explicit Stop eligibility and Team/Solo preservation.
- [x] Requirement-to-task coverage is represented by the future T001–T067 task breakdown.
- [x] Existing exactly-once, manual-input, Temporary no-reload, endpoint, source-drift, and replacement compatibility requirements remain preserved.
- [x] PASS, INCOMPLETE, and NOT_MEASURED evidence classes are not conflated.
- [x] Public ingress/migration is complete: Team create and Goal change require governing fields with exact canonical JSON/hash rules; missing objective/directives and explicit-empty-directives behavior is defined; pre-feature tasks without the authoritative payload fail closed before unsafe continuation with no exact-text fallback.
- [x] Workflow-agent continuation semantics are the complete authoritative cross-hop role contract, with explicit author responsibility, full custom system prompt first-turn-only, and deterministic built-in/legacy-custom compatibility rules.
- [x] Governing prompt size composition is deterministic with explicit canonical payloads, exact 103-character framing, independent section caps, total 10,343 bound, and exact max-boundary tests.
- [x] Legacy custom snapshots recover only through Stop -> new Team task after catalog update; replacement remains snapshot-preserving and is not misrepresented as a migration repair.
- [x] No undefined operator-authorized in-place governing migration path remains.
- [x] B-009 through B-015 are explicitly traceable in the incident matrix and future tasks/tests.
- [x] Independent REVIEW remains mandatory before PLAN may close this spec-only task as DONE.
