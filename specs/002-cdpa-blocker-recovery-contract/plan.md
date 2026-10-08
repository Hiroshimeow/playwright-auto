# Implementation Plan: CDPA Blocker Recovery Contracts

**Lifecycle**: SPEC-KIT ONLY in the current task. Future implementation is gated on a clean independent REVIEW.  
**Feature**: `specs/002-cdpa-blocker-recovery-contract/spec.md`  
**Architecture**: extend current owners; zero new services/registries/state machines.

## Constitution Check

- Single Owner / Single Live Path: PASS by design.
- Exactly-Once Side Effects: PASS by design; no accepted/ambiguous Send replay.
- Exact Execution Identity: PASS by design; endpoint/page/user/request binding stays explicit.
- Regression Evidence Before Repair: future implementation tasks start with failing focused tests.
- Minimal Scope / Workspace Preservation: current unrelated dirty/untracked work remains untouched.
- Repository compact-prompt rule: PRESERVED by governing reference rather than repeated full Goal.

## Repository Truth and Requirement Classification

| Area | Class | Current owner / truth | Future contract |
|---|---|---|---|
| RequestLedger/SendReceipt accepted boundary | PRESERVE | `durable.py` + worker exact accepted-user flow | Attempts/immutable accepted provenance never replayed |
| Canonical identity validation primitive | COMPLETE | `_discover_accepted_conversation_identity` validates exact evidence | Reuse as-is; lifecycle around it changes |
| Provisional accepted identity lifecycle | FIX | `_owned_or_block` blocks immediately after one discovery attempt | Persist 5s settle from first unresolved decision, capped existing deadline |
| Accepted-send reconciliation | PRESERVE | operator Route PLAN + unchanged receipt only | Explicit BLOCKED -> operator Route PLAN remains mandatory |
| Receipt divergence | PRESERVE safety / COMPLETE observability | current generic resume failure is safe but not deterministic enough | non-retryable `accepted_receipt_provenance_divergence`; no continuation controls; explicit Stop only |
| Generic home-endpoint immutability | PRESERVE | `TaskStore._mutate_manifest` | Ordinary writes stay immutable |
| One-time pristine placement | COMPLETE candidate / acceptance INCOMPLETE | dirty `update_initial_placement` + worker placement path | Legal only pre-Send/unbound/unpinned, then pin |
| Multiline ingress | FIX | `cdpa.cmd`/CLI argv boundary can corrupt text/control options | lossless canonical admission plus explicit supplied/omitted provenance for endpoint/mode/team/repository/context/bootstrap |
| Post-acceptance admission corruption | FIX | replacement graph exists but target eligibility + executable-text seam are incomplete | BLOCK -> explicit Stop -> existing replacement; trusted corrected executable authority |
| Team/Solo replacement semantics | FIX | replacement defaults `workflow_mode=team` and does not pass Solo goal | preserve target workflow mode/roles/goal |
| Dependency waiting | PRESERVE + projection completion | store/worker own dependency state | zero slot + exact parent attribution |
| DOM terminal response | FIX | `response.handle` requires completion actions | stable valid exact-bound inactive DOM is sufficient |
| LISTEN freshness | FIX within existing owner | PageObserver state is process-local and has no consumer watermark | expose observer-instance/epoch/seq/time + hop-wait watermark; same sequence cannot re-arm; prior instance invalid after restart |
| Wait full-snapshot cache | FIX only as needed | `ChatGPTPage._wait_full_reason` owns invalidation | all terminal-required signals sparse/invalidation-visible |
| Later prompt policy and recovery authority | FIX while preserving compactness | later normal hops omit Goal/constructor; Temporary recovery currently collapses parent prompt class into generic `execution_recovery` | authoritative task/role payloads for substantive parents; parent prompt-authority/repair lock inherited across recovery; no full Goal/constructor fallback |
| Route/report repair prompt | PRESERVE | locked minimal repair owner | remains non-substantive; no Goal reinjection |
| Cloudflare/session observations | PRESERVE evidence-only | observability boundary | no causal state/bypass |

## File / Boundary Map for Future Implementation

### Admission
- `cdpa.cmd`
- `src/playwright_auto/cdpa_cli.py`
- one lossless structured Team create/Goal-change carrier for task/new-goal + governing fields + explicit control provenance; do not add parallel per-field multiline argv paths
- smallest shared canonicalizer at existing CLI/store boundary; do not create a service
- `tests/test_cdpa_cli_runtime.py` and Windows launcher fixture for multiline governing fields

### Accepted identity and reconciliation
- `src/playwright_auto/cdpa_worker.py`: `_owned_or_block`, existing discovery/reconciliation control paths
- `src/playwright_auto/durable.py`: existing receipt enrichment only if needed
- focused tests: pool integration edges, execution recovery, backend conversation identity

### Endpoint placement
- `src/playwright_auto/cdpa_store.py`: explicit initial placement + generic immutability
- `src/playwright_auto/cdpa_worker.py`: execution endpoint preparation/persistence
- tests: endpoint/core/capacity suites

### Admission-corruption replacement
- `src/playwright_auto/cdpa_store.py::replace_task_and_rewire` and current stop/block control owners
- narrow extension to preserve target workflow mode/Solo goal and separate executable corrected payload from non-executable retained context
- no new replacement API family
- tests: core replacement graph + operator control + Solo compatibility

### Terminal arbitration and LISTEN freshness
- `src/playwright_auto/role_runtime/response.py`: terminal acceptance rule
- `src/playwright_auto/role_runtime/controller.py` / `timeout.py`: no timeout recovery after terminal DOM
- `src/playwright_auto/role_runtime/page_observer.py`: existing epoch + new signal sequence/time exposure
- `src/playwright_auto/role_runtime/signals.py`: carry/log freshness, never elevate it above DOM
- `src/playwright_auto/chatgpt.py::_wait_full_reason` only if a terminal-required signal is still hidden by caching
- tests: role runtime, page observer, ChatGPT recovery, deterministic incident fixture

### Governing contract continuity
- `src/playwright_auto/cdpa_cli.py`: required Team create governing fields with fail-closed validation.
- `src/playwright_auto/dashboard_api.py`: create/change-goal schema plus workflow-agent continuation-contract create/update validation.
- `src/playwright_auto/cdpa_store.py`: persist original/revision authoritative governing semantic payload and workflow-agent snapshot fields; no exact-text Goal migration/fallback.
- `src/playwright_auto/cdpa_workflow_agents.py`: `continuation_contract` normalization/catalog/read/resolve/snapshot compatibility and system-role default continuation assets.
- `src/playwright_auto/cdpa_worker.py`: command propagation, migration/fail-closed behavior, `_prepare_cross_endpoint_recovery` preservation of parent prompt-authority/repair lock, and compute/verify ref before every substantive Send.
- `src/playwright_auto/cdpa_prompts.py::PromptBuilder.build`: render verified in-band semantic payload / continuation / facts-ref wire block.
- tests: CLI/API create, goal change, workflow-agent catalog/API/snapshot, compact prompt P0, recovery/restart/reconciliation.

### Dependencies
- existing TaskStore/worker dependency state
- `src/playwright_auto/cdpa_projection.py` for additive parent-cause exposure
- existing dependency/projection tests

## Required Invariants

1. Accepted/ambiguous Send is never blindly replayed.
2. Attempts remain unchanged through identity settle/reconciliation/provenance failure.
3. First identity-settle timestamp is the first exact unresolved decision, not accepted `sent_at`.
4. Settle timing is persisted once and cannot reset on restart/Resume/remount.
5. Explicit operator Route PLAN remains the only accepted-send reconciliation authorization.
6. Receipt divergence never uses accepted-send reconciliation.
7. Temporary Fresh never relies on reload/saved-conversation identity.
8. Team home endpoint is immutable after legal placement.
9. Admission parser tokens never become task/goal text.
10. Accepted malformed prompt/history is immutable; replacement follows BLOCK -> Stop -> existing owner.
11. Replacement preserves Team/Solo semantics and uses corrected canonical executable authority.
12. Stable valid exact-bound inactive DOM output is terminal regardless of delayed/missing action buttons or stale LISTEN.
13. LISTEN freshness is owned by PageObserver and cannot become independent route authority.
14. Later substantive Team hops never rely solely on prior chat/handoff/recovery snapshot for Goal/role policy; Temporary recovery inherits the accepted parent's prompt-authority class instead of deriving authority from `kind="execution_recovery"`.
15. Later prompt compactness is preserved via verified durable reference, not full Goal duplication.
16. Route/report repair remains a narrow non-substantive operation.
17. Dependency WAIT consumes zero browser slots.
18. No second durable owner/state machine is introduced.
19. Cloudflare is not asserted causal without evidence.
20. Current unrelated dirty work is not reset/overwritten.

## Acceptance Evidence Required for Future Implementation

### Admission
- CRLF/CR/LF, Unicode/BOM code point, quotes, blank edges, option-like tokens, strict UTF-8 failure, Team/Solo.
- persisted hashes equal canonical values.
- malformed pre-Send fails before browser mutation.

### Accepted identity
- deterministic actual incident timing: ~34s accepted-to-decision + ~92ms decision-to-canonical.
- start time equals first unresolved decision.
- restart/Resume reuses same deadline.
- no browser mutation and attempts unchanged.
- failure -> BLOCKED; only explicit operator Route PLAN creates reconciliation.
- receipt divergence negative path before backend/DOM/reopen.

### Replacement
- accepted corruption blocks with incident id and does not replay.
- explicit Stop is required before replacement.
- replacement task_text/goal are corrected canonical authority.
- no corrupt target goal in executable replacement text.
- Team and Solo preserve workflow mode/roles/policy.
- existing idempotency/rewiring remains deterministic.

### Endpoint
- pinned immutability;
- legal unpinned first placement;
- no later relocation;
- Temporary 9202/9212 no 9222 spill.

### Terminal arbitration
- delayed/missing completion buttons;
- stale `IS_STREAMING` after stable exact DOM route;
- wrong-bound/active/incomplete negatives;
- no reload/status/state-repair after terminal DOM;
- Temporary no-reload;
- PageObserver epoch/sequence freshness.

### Governing contract
- PLAN -> REVIEW -> PLAN.
- post-accepted Temporary document/page loss -> same-home-endpoint recovery, with original Send non-replayable and parent prompt-authority/repair locks preserved.
- goal revision applies only at future hop boundary.
- Restart role/New chat/reconciliation preserve verified ref.
- manifest/goal/role-contract hash mismatch fails before Send.
- route/report repair remains minimal.
- long Goal absent inline on later hop while ref is resolvable and verified.

### Dependency / compatibility
- zero-slot waiting + exact parent projection.
- existing exactly-once, manual draft/attachment, source-drift, endpoint capacity, accepted user binding, and replacement graph suites remain green.
- future live Temporary acceptance pinned to the task-selected allowed endpoint only.

## Non-Goals

No production implementation in this cycle. No deployment/restart/cache clearing/Cloudflare bypass. No endpoint 9222 spill. No reset/stash/clean/branch switch. No broad ChatGPT rewrite.

## Rollback Boundaries

- Admission transport/canonicalization: independent if schema-free.
- Identity settle: additive hop-wait metadata only.
- LISTEN freshness: additive PageObserver observation metadata.
- Terminal arbitration: controller/response policy; no ledger schema.
- Governing reference: additive prompt/hop metadata; no accepted-history rewrite.
- Endpoint initial placement: no migration; legacy missing pin defaults pinned.
- Dependency projection: additive.
- Replacement extension: no rewrite of original/replacement histories already persisted.

## Review Gate

A fresh independent REVIEW must:
1. replay all ledger incidents against the requirements/tasks;
2. challenge the corrected identity timing anchor;
3. challenge explicit Stop/replacement legality and Team/Solo semantics;
4. verify DOM terminal authority and LISTEN freshness do not create false acceptance;
5. verify governing-contract continuity satisfies B-008/B-010/B-013 without violating compact-prompt repository policy;
6. verify every FR maps to deterministic future tasks/tests;
7. leave implementation unchecked;
8. mark reviewer-only CHK019/CHK020 only if no CRITICAL/HIGH specification defect remains.

Only a subsequent PLAN may return DONE at the pre-implementation gate after that clean review.
