[Reading 65 lines from start (total: 65 lines, 0 remaining)]

# Reviewer Checklist: CDPA Blocker Recovery Contract

This checklist is the independent-review gate for the current SPEC-KIT ONLY cycle. Author-side items are checked only where the refreshed artifacts now state the required contract. CHK019 and CHK020 are reserved for the next independent REVIEW verdict and remain unchecked until that review is clean.

## Constitution / ownership

- [x] CHK001 Is every new responsibility assigned to an existing authoritative owner, with no second ledger, endpoint registry, identity parser, replacement graph, response state machine, prompt-policy owner, or dependency state machine?
- [x] CHK002 Does every accepted/ambiguous Send path remain non-replayable, including admission corruption, identity settling, receipt divergence, response recovery, and reconciliation?
- [x] CHK003 Are workflow home endpoint and role execution/page binding separated clearly enough to prevent implicit endpoint migration?
- [x] CHK004 Does the spec preserve Temporary Fresh context and forbid saved-conversation reopen/reload assumptions?

## Accepted identity / provenance

- [x] CHK005 Is the identity-settle timer anchored to the first exact provisional unresolved-decision point, rather than Send/accepted time, with a persisted fixed deadline capped by the existing response deadline?
- [x] CHK006 Are restart/Resume/remount semantics deterministic, with no reset/extension of the settle interval or response deadline?
- [x] CHK007 Does unchanged-receipt settle failure become BLOCKED first, with explicit operator Route PLAN as the only accepted-send reconciliation authorization?
- [x] CHK008 Is immutable receipt divergence explicitly excluded from accepted-send reconciliation and fail-closed before backend/DOM/reopen work?

## Admission / replacement / compatibility

- [x] CHK009 Does multiline ingress cover cmd.exe/PowerShell corruption and option-token leakage without relying on fragile quoting fixes, and does admission prove submitted team/Temporary mode/repository/endpoint metadata instead of accepting silent defaults?
- [x] CHK010 Is canonicalization exact and shared by Team/Solo: strict UTF-8 file/stdin, CRLF/CR→LF only, no blanket trim or Unicode normalization?
- [x] CHK011 Is pre-Send admission mismatch rejected before browser mutation?
- [x] CHK012 Does post-acceptance corruption preserve accepted history, use the existing block/Stop owners, and require explicit Stop before replacement?
- [x] CHK013 Does replacement preserve Team/Solo mode, roles, endpoint/context/dependency semantics, and Solo Goal while keeping known-corrupt prior text non-executable?
- [x] CHK014 Are current dirty endpoint-placement changes treated only as a candidate, with generic endpoint immutability and legacy fail-closed behavior preserved?

## Response terminality / LISTEN

- [x] CHK015 Is stable valid exact-bound inactive DOM output sufficient terminal authority even when completion action buttons are absent or late?
- [x] CHK016 Is LISTEN supplemental only, with stale IS_STREAMING forbidden from vetoing a terminal DOM result or re-arming timeout recovery?
- [x] CHK017 Is LISTEN freshness owned by existing PageObserver + hop-wait/controller evidence with observer-instance/epoch/sequence/time watermark, explicit 5s age rule, and restart invalidation rather than a second transport state owner?
- [x] CHK018 Does snapshot-cache policy expose every signal actually required for terminal arbitration without restoring action buttons as a hidden completion gate?

## Governing ingress / workflow-agent migration

- [x] CHK027 Are Team create and Goal-change governing semantic fields required and mapped to real CLI/API/worker/store ingress, with missing/invalid/oversize contract rejected before mutation?
- [x] CHK028 Do already-persisted pre-feature Team tasks without an authoritative governing payload fail closed before the next substantive Send, with Stop -> new Team task as the recovery path?
- [x] CHK029 Does workflow-agent schema/catalog/API/snapshot ownership define `continuation_contract` as the complete authoritative cross-hop role contract, with full custom `system_prompt` first-turn-only, repository defaults only for built-in roles, and no implicit custom-role fallback?
- [x] CHK030 Are B-009 through B-015 explicitly mapped to requirements/success criteria/tasks without rewriting ledger history?

## Governing budget / legacy-custom recovery

- [x] CHK031 Are governing prompt budgets mathematically complete: payloads <=4096/2048/4096, framing exactly 103 characters, simultaneous maxima total exactly 10,343?
- [x] CHK032 Do governing payload boundary tests cover 4095/4096/4097 and simultaneous maxima without reintroducing full-Goal replay or exact-text fallback?
- [x] CHK033 Is legacy custom-agent recovery consistent with immutable snapshots: Stop -> normal new Team task after catalog update, with replacement explicitly prohibited for this migration?
- [x] CHK034 Has the undefined operator-authorized in-place governing migration path been removed entirely?

## Governing reference identity

- [x] CHK035 Does canonical facts/ref JSON include exact canonical absolute `manifest_path`, and do tests require mismatch/noncanonical/outside-plans-root failure before Send?

## Review verdict — independent reviewer only

- [x] CHK019 No CRITICAL or HIGH specification inconsistency remains after replaying every blocker-ledger incident against the refreshed artifacts.
- [x] CHK020 The feature is implementation-ready at the pre-implementation gate, while production implementation has not started in this cycle.

## Governing contract / dependency coverage

- [x] CHK021 Do later substantive Team hops receive mandatory semantics in-band through the required authoritative bounded semantic payload plus verified ref, with exact canonical manifest binding and no Goal/chat/filesystem/connector fallback?
- [x] CHK022 Does the governing contract preserve the repository compact-prompt rule by never repeating the full Goal/constructor on later turns?
- [x] CHK023 Do Goal revision, Restart role, New chat, legal pre-boundary relocation, PLAN re-entry, accepted-send reconciliation, and Temporary generation recovery preserve the correct parent prompt-authority class, including locked route/report repair?
- [x] CHK024 Are route-repair/report-repair explicitly non-substantive so they do not re-authorize repeated work?
- [x] CHK025 Do dependency waits remain zero-slot secondary state with exact parent cause projection and no competing child blocker owner?
- [x] CHK026 Does the scope keep Cloudflare/CDP/session observations evidence-only unless causal proof exists?