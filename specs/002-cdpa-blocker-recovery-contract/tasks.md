# Tasks: CDPA Blocker Recovery Contracts

**Status**: Future implementation backlog only. All tasks remain unchecked in this SPEC-KIT ONLY cycle.  
**Ordering rule**: regression evidence first; then the smallest owner-scoped implementation; then independent acceptance.  
**Forbidden in current cycle**: executing any T### production implementation task.

## Phase 1 — Deterministic blocker regressions

- [ ] T001 [P] Add Windows multiline ingress regression reproducing B-001: preserved CR/LF/Unicode/blank edges, no `--config`/`--browser` option leakage, failure before browser mutation.
- [ ] T002 [P] Add accepted-identity timeline regression reproducing the observed ~34s accepted-to-unresolved-decision delay and ~92ms decision-to-canonical same-page transition.
- [ ] T003 [P] Add restart/Resume/remount regression proving identity settle start/deadline persists once and does not reset or extend the response deadline.
- [ ] T004 [P] Preserve/add immutable receipt-divergence regression proving fail-closed before backend/DOM/reopen and zero attempt increment.
- [ ] T005 [P] Add endpoint/admission-provenance regression covering omitted endpoint+implicit mode, explicit endpoint equal to default, explicit Temporary equal to endpoint default, explicit persistent, required endpoint, unpinned bootstrap context, exact placement mutation whitelist, post-Send immutability, legacy missing-pin fail-closed behavior, and Temporary 9202/9212 no-9222-spill.
- [ ] T006 [P] Add post-acceptance admission-corruption regression proving accepted history remains immutable and replacement is illegal before explicit Stop.
- [ ] T007 [P] Add replacement compatibility regression for Team and Solo, including workflow mode, role set, endpoint/context policy, dependencies, and Solo Goal.
- [ ] T008 [P] Add B-002-terminal/B-007 regression: exact-bound stable valid DOM route + `stop_visible=false` + inactive bound turn, delayed/missing completion buttons, stale LISTEN `IS_STREAMING`; assert ACCEPT without reload/status/state-repair.
- [ ] T009 [P] Add negative response regressions: active bound turn, unstable/incomplete route, wrong user/assistant binding, current-page error/Retry; ensure no false terminal acceptance.
- [ ] T010 [P] Add B-008 PLAN→REVIEW→PLAN prompt regression proving later substantive hops carry a resolvable governing-contract reference while long Goal text is not duplicated inline.
- [ ] T011 [P] Add goal-revision/restart/New-chat/accepted-send-reconciliation plus Temporary generation-loss regressions proving prompt-authority inheritance: normal substantive recovery receives the governing contract; accepted route/report-repair recovery remains locked/non-substantive; reconciliation recovery preserves lineage; same-home-endpoint and original attempts/no-replay invariants hold.
- [ ] T012 [P] Add dependency projection regression proving zero slot consumption and exact parent status/block-code attribution.

## Phase 2 — Lossless task admission

- [ ] T013 Implement one shared task-text canonicalizer at the existing CLI/store admission boundary: strict UTF-8 file/stdin, CRLF/lone-CR→LF, preserve all other code points/whitespace, whitespace-only rejection.
- [ ] T014 Add one first-class structured file/stdin-or-equivalent Team create/Goal-change ingress carrying task/new-goal, governing objective/directives, and explicit control provenance; keep compatible simple one-line argv only where it cannot split semantic fields.
- [ ] T015 Update repository Windows launcher/spawn path to use that structured carrier for multiline Team semantic envelopes instead of relying on cmd.exe `%*` multiline argv quoting.
- [ ] T016 Persist/verify admission task-text hash, governing semantic payload/hash, and optional separately canonicalized Solo Goal hash before browser mutation; parser options remain separate, and normalized submitted team/Temporary mode/repository/selected+required endpoint metadata must equal the created manifest rather than silently falling back to defaults.
- [ ] T017 Ensure Team and Solo admission call the same canonicalization/integrity owner and fail before Send on mismatch.

## Phase 3 — Accepted persistent identity settle

- [ ] T018 Extend the existing hop `wait` shape with accepted-identity start/deadline/page/outcome evidence; no new state store.
- [ ] T019 In `_owned_or_block`, start the settle interval only at the first exact provisional-identity decision that would otherwise block; never anchor it to `sent_at`.
- [ ] T020 Cap settle deadline at the existing response deadline; if the response deadline is already reached, block immediately without granting a new budget.
- [ ] T021 Reuse `_discover_accepted_conversation_identity` for read-only same-request/page/user canonicalization and existing receipt enrichment; zero Send/reload/goto/reopen.
- [ ] T022 Persist canonicalized outcome and continue the original wait without extending attempts/deadline.
- [ ] T023 On unchanged-receipt page/user loss, conflict, or settle expiry, produce BLOCKED `accepted_conversation_identity_unresolved` and no automatic reconciliation child.
- [ ] T024 Preserve explicit operator Route PLAN as the sole call path to `_route_unresolved_accepted_to_plan`; keep unchanged-receipt preconditions.
- [ ] T025 Keep receipt divergence on existing provenance-integrity fail-closed path; never route it through identity reconciliation.

## Phase 4 — Workflow home endpoint placement

- [ ] T026 Audit the dirty `update_initial_placement` candidate against FR-013..FR-018 and delete/simplify any behavior beyond one pristine pre-Send placement.
- [ ] T027 Ensure worker persistence uses the explicit initial-placement owner only for false→true pinning and ordinary persistence stays immutable.
- [ ] T028 Preserve explicit pinned endpoint across Resume/Retry/Restart role/New chat/role transition/source drift/dependency release; no accepted-work migration.
- [ ] T029 Preserve legacy missing `endpoint_pinned` as pinned/fail-closed; require no unsafe manifest migration.

## Phase 5 — Post-acceptance admission corruption and replacement

- [ ] T030 Add the narrow admission-integrity block classification through the existing worker block owner, with incident provenance and no request replay.
- [ ] T031 Reuse existing operator Stop to move the malformed accepted task from BLOCKED to STOPPED; do not add a replacement-specific terminal state.
- [ ] T032 Narrowly extend `replace_task_and_rewire` to preserve target `workflow_mode`, workflow-agent/role snapshot, endpoint/required-endpoint/conversation/context policy, dependencies, and Solo Goal semantics.
- [ ] T033 Add an incident-mode replacement seam where corrected canonical task/goal fields are executable authority instead of `replacement_continuation_text()` reading the known-corrupt target effective Goal.
- [ ] T034 Keep retained reports/incident references as non-executable handoff/context metadata; do not mix known-corrupt target text into replacement executable instructions.
- [ ] T035 Preserve existing replacement idempotency, `replaces_task_id`/`replacement_incident_id` linkage, exact-team reuse rules, and atomic child rewiring.

## Phase 6 — Exact terminal response and LISTEN freshness

- [ ] T036 Extend existing PageObserver observation with unique `observer_instance_id`, current epoch, monotonically increasing `signal_seq`, and observation time; increment sequence only inside the existing signal owner.
- [ ] T037 Carry freshness through RoleSignals and persist the last-consumed observer-instance/epoch/seq watermark in existing hop wait/controller evidence; same sequence cannot re-arm progress and prior-instance evidence is invalid after restart.
- [ ] T038 Change `role_runtime/response.py` so stable valid exact-bound DOM output with inactive current-page generation can ACCEPT without copy/feedback/regenerate controls.
- [ ] T039 Ensure controller authority ordering accepts terminal DOM before LISTEN/timeout recovery; stale `IS_STREAMING` cannot veto, re-arm, reload, or queue state repair.
- [ ] T040 Audit `ChatGPTPage._wait_full_reason`/sparse probe so every terminal-required signal is observable; do not add action-button invalidation merely to preserve an obsolete completion gate.
- [ ] T041 Preserve current persistent bounded recovery only when no terminal DOM result exists and preserve Temporary absolute no-reload behavior.
- [ ] T042 Emit terminal decision source/reason and LISTEN epoch/sequence freshness fields without logging sensitive prompt/response payloads.

## Phase 7 — Governing contract continuity for later substantive hops

- [ ] T043 Extend Team create ingress end-to-end (`cdpa_cli.py`, dashboard/API normalization, worker command payload, TaskStore create/test seam) with required `governing_objective` + explicitly supplied ordered `governing_directives`; consume the same lossless structured carrier as task/control provenance, persist/hash canonical semantic JSON without model inference, and reject missing/invalid/oversize/transport-mismatched contract before mutation.
- [ ] T044 Extend Team Goal-change ingress end-to-end with the same required governing fields, exact canonical JSON/hash rules, and future-hop ownership; no exact-task/Goal fallback is permitted.
- [ ] T045 For pre-feature Team tasks missing the authoritative governing payload, fail closed with `governing_contract_unavailable` before the next substantive Send; recovery is explicit Stop followed by normal new Team task creation with caller-authored governing fields.
- [ ] T046 Extend `cdpa_workflow_agents.py` schema/catalog/snapshot normalization with canonical non-empty `continuation_contract` ≤2048 as the complete authoritative cross-hop role contract; custom full `system_prompt` remains first-turn-only; preserve snapshot immutability and catalog version compatibility.
- [ ] T047 Extend workflow-agent dashboard/API create/update + TaskStore/worker command seams to accept/persist `continuation_contract`; new/updated custom agents require it.
- [ ] T048 Add repository-owned compact continuation assets for built-in system roles under the existing default-prompt owner and deterministic system-role fallback; legacy custom snapshots without the field have no fallback and recover only by Stop -> new Team task after catalog update, never replacement.
- [ ] T049 Resolve effective Goal/semantic payload through existing `task_goal_for_hop`/goal-revision ownership, exact canonical `manifest_path` from the actual task manifest, and continuation semantics from the immutable task workflow snapshot or built-in system default; compute SHA-256 over canonical UTF-8 payload bytes only, excluding framing.
- [ ] T060 Verify governing sources/hashes, exact canonical manifest path/actual manifest identity, and exact wire budgets before every substantive later Team-hop Send: semantic payload <=4096 code points, continuation <=2048, canonical facts/ref <=4096, fixed framing exactly 103 characters, total <=10,343; fail closed pre-Send on missing/mismatch/source drift/manifest mismatch/length-prefix/size violation.
- [ ] T061 Render the exact length-prefixed governing wire grammar with zero-padded five-digit Unicode-code-point lengths, `manifest_path` in the canonical facts/ref key order, and no trailing newline after payload3; no filesystem/connector resolution is required. Missing semantics blocks `governing_contract_unavailable` before Send.
- [ ] T062 Preserve parent prompt-authority across Temporary generation recovery: normal substantive parents receive governing contract; `route_repair`/`report_repair` parents preserve lock/validation fields and render locked non-substantive prompts; accepted-send reconciliation preserves its lineage/no-replay restrictions. Reuse existing recovery/prompt owners.
- [ ] T063 Preserve route-repair/report-repair as non-substantive locked prompts: no full Goal/capsule reinjection and no authorization to repeat completed work.
- [ ] T064 Add deterministic structured-ingress/canonicalization tests for CLI/API/direct-store create and Goal change: semantic payload 4095/4096/4097; missing objective; missing directives field; explicit empty directives list; invalid directive entries; multiline CR/LF, quotes, backslashes, option/header-like text, Unicode; reordered input-map keys; directive-order preservation; intended-vs-persisted governing hash mismatch fail-before-browser; pre-feature task without governing payload fails closed before substantive Send.
- [ ] T065 Add workflow-agent tests proving a mandatory custom role directive is present in the authoritative continuation contract and survives later role re-entry/Temporary recovery; also cover legacy custom missing field, built-in system fallback, catalog update/delete, immutable task snapshot behavior, and legacy-custom Stop -> new-task recovery while proving replacement remains snapshot-preserving and therefore illegal for this migration.
- [ ] T066 Update compact-prompt tests for the exact wire grammar, canonical facts/ref key order including `manifest_path`, manifest mismatch/noncanonical/outside-plans-root fail-closed cases, payload hashes excluding framing, independent bounds, simultaneous maxima 4096/2048/4096 -> exactly 10,343 code points, max-1/max/max+1 semantic payload, length-prefix validation, and no full Goal/constructor duplication on later turns.
- [ ] T067 Strengthen receipt-divergence tests for B-012: exact `accepted_receipt_provenance_divergence` block code, illegal Resume/Retry/Restart/New chat/Route PLAN controls, attempts unchanged, and explicit Stop-only exit.

## Phase 8 — Dependency attribution and public observability

- [ ] T050 Add exact parent task/status/non-sensitive block-code projection under the existing dependency owner; child root blocker remains distinct.
- [ ] T051 Prove dependency wait consumes zero endpoint capacity and parent DONE releases without rebinding home endpoint.
- [ ] T052 Project accepted-identity timing/outcome, endpoint pin/home, terminal-response source, governing-contract resolution, reconciliation control origin, and LISTEN freshness through existing observability surfaces only where needed.
- [ ] T053 Keep Cloudflare/challenge/cache/session data evidence-only; add no Cloudflare-specific domain/recovery state.

## Phase 9 — Compatibility and acceptance

- [ ] T054 Run narrow regression suites for each owner immediately after its implementation task.
- [ ] T055 Run relevant broader exactly-once/manual-draft/attachment/source-drift/endpoint/replacement/prompt/dependency suites; classify unavailable or stale evidence as NOT_MEASURED, never PASS.
- [ ] T056 Run deterministic Windows launcher acceptance using the lossless multiline path.
- [ ] T057 Run future live Temporary acceptance only on the explicitly task-selected allowed endpoint (9202 or 9212 for this program); prove zero 9222 spill.
- [ ] T058 Review final production diff for duplicate ownership, extra states/gates/services, unsafe migrations, and unrelated dirty-file mutation; run `git diff --check`.
- [ ] T059 Perform independent implementation REVIEW/TEST only after all owner-scoped regressions pass; no acceptance claim based solely on unit structure.
