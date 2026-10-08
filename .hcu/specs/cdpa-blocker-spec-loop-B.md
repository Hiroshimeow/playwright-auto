# CDPA blocker discovery ledger (B)

Scope: spec-only iterative CDPA blocker discovery before implementation.
Host: md g6 (DESKTOP-B5E4IUE).
Allowed browser endpoints for spawned spec tasks: 9202 and 9212 only.
All spawned tasks must use Temporary Chat (`--temp`).
Stop condition: spec-kit review is complete and implementation has NOT started.

## Operating loop
1. Spawn spec task A on 9202 or 9212 with Temporary Chat.
2. Monitor task state, timeline, reports, and live events.
3. On every block/failure/unexpected recovery path, diagnose root cause from durable evidence and append an incident below.
4. Resume task A only after the incident is recorded and a safe recovery path is known.
5. Continue until A reaches spec-review DONE (pre-implementation).
6. Feed the complete ledger back into the next fresh spec task A' on the other allowed endpoint.
7. Repeat until a full spec-kit review completes without discovering a new blocker category.

## Incidents

### B-001 — Windows `cdpa.cmd` mangles multiline `--task` payload before task admission
- Observed: 2026-10-04 15:29 JST while spawning task A on temporary endpoint 9202.
- Evidence: API detail for `cdpa-idem-cdb9957ac70724b6d5a69c70` showed `task_text` / `effective_goal` reduced to the first prompt line plus `--config [PRIVATE_PATH]`, despite the source prompt file containing the full spec-only instructions.
- Trigger: PowerShell invoked `cdpa.cmd --task $prompt ...` where `$prompt` contained embedded newlines. The batch wrapper forwards arguments via `%*` into `python -m playwright_auto.cdpa_cli`; the multiline argument boundary is not preserved safely through the `.cmd` layer.
- Confirmed impact: the durable manifest shows `conversation_mode: persistent`, `conversation_mode_explicit: false`, even though the orchestration requested `--temp`; the requested team name was also lost and the task fell back to its generated idempotent team. Endpoint 9202 remained selected only because it was also the default endpoint, so this run does not prove `--browser 9202` survived argument transport.
- Impact: task can start with silently corrupted goal and silently lose control flags, including the Temporary Chat safety constraint. The API accepts the malformed task rather than rejecting it.
- Required spec coverage: define lossless CLI argument transport for multiline task/goal content, or provide a first-class file/stdin input path; add admission round-trip validation so persisted task text, team, temporary mode, repository, and endpoint selection must match the submitted request before Send; never allow wrapper-injected option tokens such as `--config` to leak into task text.
- Recovery decision for this run: this task cannot be converted safely from persistent to temporary in place. Stop it and replace it with a correctly admitted Temporary task using direct JSON/API transport. Preserve its reports/live evidence as diagnostic input only.

### B-002 — Valid bound terminal response is missed; stale `IS_STREAMING` survives until timeout and forces route-repair
- Observed twice in task `cdpa-idem-cdb9957ac70724b6d5a69c70`: REVIEW hop 2 and PLAN hop 4.
- REVIEW hop 2 evidence: Send accepted at 06:46:09 UTC. At 06:54:13 DOM/full snapshot showed the exact bound assistant turn with the required route JSON, `complete=true`, `stop_visible=false`, and `transport_active=false`. CDPA did not record a response. At 06:56:28 it ran `response_timeout_recheck`; at 06:56:58 it queried status; at 06:57:27 it chose `state_repair`. Durable hop state retained `live_listen_signature={\"status\":\"IS_STREAMING\"}` while `timeout_status` was null.
- PLAN hop 4 evidence: the correct turn-2 route JSON was visible as a complete bound assistant message at 07:06:45 UTC, but the task still timed out, refreshed at 07:11:07, and queued route-repair at 07:12:07. The durable hop again retained stale LISTEN `IS_STREAMING` evidence.
- Code path: `role_runtime/response.py::handle()` refuses a stable bound DOM candidate unless completion action controls (`copy`, `feedback`, or `regenerate`) are present. `chatgpt.py::_wait_full_reason()` refreshes the full snapshot on message identity/text/transport changes but does not refresh solely when completion action controls appear after text has stabilized. A full snapshot taken before those controls render can therefore cache a terminal message without the completion actions indefinitely. Timeout logic later consults stale LISTEN diagnostics and falls into `state_repair` even though DOM already has the exact terminal JSON.
- Required spec coverage: DOM-bound terminal identity + stable valid route must be sufficient terminal authority once current-page generation is inactive; completion action buttons may be corroborating evidence but not a mandatory gate. Sparse/full snapshot invalidation must observe any completion-state signal needed by terminal arbitration. LISTEN `IS_STREAMING` is diagnostic only and must never override a complete bound DOM turn. Add deterministic tests for delayed action-button rendering, stale LISTEN streaming after DOM completion, exact user-turn binding, and no duplicate Send during route-only recovery.
- Recovery: route-repair is safe because the accepted Send is not replayed, but it is unnecessary work and consumes turns. Keep the evidence; replacement Temporary runs must verify this path without relying on refresh.

### B-002 — Receipt-divergence semantics conflict with the existing accepted-send reconciliation owner
- Observed: independent REVIEW of task A, 2026-10-04.
- Root cause: the draft spec sends immutable `SendReceipt` divergence into `_route_unresolved_accepted_to_plan`, but that owner explicitly requires the durable SENT ledger receipt to remain unchanged and rejects divergence.
- Impact: the spec would weaken or bypass an accepted-request invariant merely to reuse an existing recovery child.
- Required spec change: split unchanged-receipt timing/page/canonicalization failures from true durable receipt divergence. True divergence must fail closed into an explicit terminal/manual-evidence contract unless a separately proven reconciliation rule is defined. Never replay Send.

### B-003 — Bounded accepted-identity observation has no durable timing owner
- Observed: independent REVIEW of task A, 2026-10-04.
- Root cause: `PROVISIONAL_EXACT_PAGE -> OBSERVING -> ...` is specified symbolically, but the spec does not define authoritative start/deadline metadata, restart reconstruction, maximum bound, or cleanup.
- Impact: worker restart/page remount can reset the observation interval and make a supposedly bounded wait nondeterministic or unbounded.
- Required spec change: bind timing metadata to an existing durable hop/wait owner; define exact start/deadline, configured maximum, restart/remount behavior, and cleanup after canonicalization/reconciliation. Do not create a second state machine.

### B-004 — Windows multiline ingress normalization/equality contract is ambiguous
- Observed: independent REVIEW confirms B-001 and finds the contract itself underspecified.
- Root cause: the spec simultaneously promises lossless multiline preservation while allowing undefined `documented text normalization`; current CLI uses `.strip()`.
- Impact: integrity hash/equality and fixtures can disagree about leading/trailing blank lines, so admission correctness is not deterministic.
- Required spec change: define one exact normalization algorithm shared by Team/Solo and integrity evidence, or require true text preservation with only an explicitly named newline encoding rule. Add fixtures for leading/trailing blanks, embedded newlines, quotes, and option-like text such as `--config`.

### B-005 — Post-acceptance malformed-goal recovery has no concrete existing owner/state transition
- Observed: current task A has already accepted the malformed prompt; later Change Goal is future-hop-only and `goal_revisions` remains empty for the accepted hop.
- Root cause: draft spec says `explicit reconciliation or replacement provenance` but does not select an existing owner, eligibility rule, terminal status, replacement linkage, or immutable-history boundary.
- Impact: an accepted corrupted prompt could be incorrectly treated as repaired in place or accidentally replayed.
- Required spec change: define whether the malformed task is frozen/terminalized, whether an existing replacement-task path is used, which history remains immutable/transfers, and the exact no-replay invariant. No new replacement subsystem.

### B-006 — Reviewer checklist cannot be marked clean until material findings are reconciled
- Observed: `checklists/recovery-review.md` remains intentionally unchecked after independent REVIEW.
- Required spec change: PLAN must reconcile the checklist after B-002 through B-005 are corrected; only then may another independent REVIEW declare the spec implementation-ready.

### B-007 — Stale LISTEN `IS_STREAMING` delays a valid terminal route and triggers unnecessary reload
- Observed: REVIEW hop 2 returned a complete valid `PLAN` route at 2026-10-04T06:54:13Z; DOM had `stop_visible=false`, a complete assistant message id/turn id, and the exact valid route JSON, but the hop remained `wait_response` for more than two minutes.
- Evidence: the last LISTEN state remained `IS_STREAMING`; at 06:56:28Z the controller chose `response_timeout_recheck` and reloaded the already-completed page, then reported `verified_ownership`.
- Root cause hypothesis to verify in spec/source: terminal arbitration allows a stale LISTEN streaming status to outrank newer authoritative DOM/transcript completion evidence, so valid terminal output is not accepted promptly.
- Impact: needless reloads, delayed routing, and increased chance of identity/session drift after an already-complete accepted turn.
- Required spec change: define freshness/authority ordering for LISTEN vs DOM/transcript signals. A complete bound assistant message with stable valid route and `stop_visible=false` must not be held indefinitely by stale `IS_STREAMING`; stale listener state needs an expiry/sequence/epoch rule. Preserve the one-reload recovery only for genuinely ambiguous/incomplete results.

### B-008 — Continuation handoff prompt drops Goal and role contract
- Observed: after REVIEW successfully routed back to PLAN, hop 4 / PLAN turn 2 was generated with only `Turn`, `Workspace`, `Handoff`, `REPORT`, and `ROUTE`; the persisted prompt omitted the task Goal and the PLAN role constructor entirely.
- Evidence: active hop 4 input had no `Goal:` section and no spec-only constraints; it therefore omitted `md g6`, allowed endpoint set `9202/9212`, Temporary-only execution, and the explicit prohibition on implementation. The malformed continuation was accepted exactly once at 2026-10-04T07:00:43Z, so it must not be replayed or rewritten in place.
- Impact: later turns can silently lose the governing task contract and route into implementation or use disallowed runtime placement even when turn 1 was constrained correctly. Handoff text becomes an accidental sole source of policy.
- Required spec change: every substantive role hop must carry an immutable/effective Goal reference plus the relevant role contract and execution constraints, or a cryptographically/equivalently bound durable reference that the role resolves before acting. Add regression tests across PLAN→REVIEW→PLAN and other multi-turn role cycles. Never rely on prior chat context alone for mandatory constraints.

### B-009 — Admission equality loses caller explicitness when explicit values equal defaults
- Observed: independent REVIEW Turn 1 of corrected Temporary task `cdpa-idem-773db9d9bd12297c3fb10b1b`, 2026-10-04.
- Evidence: B-001's requested endpoint happened to equal default 9202. Current create state materializes concrete endpoint/mode values even when options are omitted, while separate fields such as `endpoint_pinned` and `conversation_mode_explicit` encode caller intent. The one-time placement candidate can also derive endpoint/mode/context fields.
- Root cause: comparing only normalized final values cannot prove that explicit `--browser`, `--temp`, required endpoint, or context intent survived transport; a dropped option can be indistinguishable from a coincidentally equal default.
- Required spec change: admission envelope/equality must include explicitness/provenance, at minimum endpoint requested/pinned intent and conversation-mode/Temporary explicitness. Define omitted/derived versus explicit values and exactly which fields one-time initial placement may derive. Explicit Temporary/persistent and bootstrap/context policy must survive placement unless an existing explicit contract authorizes change. Add deterministic default-equality and unpinned/bootstrap cases.

### B-010 — `GOVERNING_CONTRACT_REF` proves identity but not semantic delivery to the role
- Observed: independent REVIEW Turn 1 of corrected task, 2026-10-04.
- Evidence: proposed later-hop reference contains task/hop/manifest identifiers plus Goal and role-contract hashes. CDPA can verify those hashes before Send, but the model does not learn the Goal/role semantics from a hash/locator. Tool/filesystem access is not guaranteed by repository policy.
- Root cause: B-008 is a semantic-delivery failure, not only an integrity failure. The spec delegates role-side resolution without defining a guaranteed role-accessible resolver or in-band governing content.
- Required spec change: choose one deterministic existing-owner delivery contract: either a guaranteed resolver returning the exact verified effective Goal + target-role contract and fail closed before substantive work if unavailable/mismatched, or a bounded in-band governing capsule containing mandatory semantics. Define deterministic role-contract source precedence (task snapshot when present plus exact legacy fallback). Acceptance must prove no substantive work starts when semantics cannot be obtained.

### B-011 — LISTEN freshness fields lack a deterministic consumer watermark/restart rule
- Observed: independent REVIEW Turn 1 of corrected task, 2026-10-04.
- Evidence: proposed PageObserver epoch/sequence/time metadata remain in-memory; current PageObserver starts `epoch=0`, can retain `IS_STREAMING`, and a worker/PageObserver recreation can restart local counters. The spec does not define what the consumer compares sequence against or when a same-sequence status becomes diagnostic-only.
- Root cause: adding freshness metadata without a comparison/watermark contract does not deterministically retire stale LISTEN evidence, especially across restart.
- Required spec change: keep existing PageObserver/controller owners but define last-consumed observer-instance/epoch/sequence evidence in the existing hop wait/controller state, or explicitly invalidate all pre-restart LISTEN evidence on observer recreation. Define observed-time aging, scope match, sequence advance, restart semantics, and deterministic stale/fresh tests. Terminal DOM precedence remains authoritative.

### B-012 — Immutable receipt divergence lacks an observable terminal/manual-recovery state contract
- Observed: independent REVIEW Turn 1 of corrected task, 2026-10-04.
- Evidence: spec excludes immutable `SendReceipt` divergence from accepted-send reconciliation but only says manual evidence is required. Current generic resume exception can surface `resume_recovery_failed`, leaving classification/control semantics underspecified.
- Root cause: fail-closed/no-replay is defined, but the durable blocker classification, terminal status, legal operator control, re-proof requirements, and no-repair terminal behavior are not.
- Required spec change: select one existing owner/state transition completely: e.g. dedicated provenance-integrity block classification with exact existing manual recovery control + re-proof preconditions, or explicit Stop/terminalization when no safe in-task recovery exists. Preserve accepted provenance, attempts, records, non-replay, and prohibit Route PLAN accepted-send reconciliation for true divergence.

### B-013 — Accepted Temporary generation-loss recovery creates a substantive child without governing semantics
- Observed: second-pass task `cdpa-idem-9ca9cc8720b33efd287bb065` on endpoint 9212, PLAN hop 1 -> execution-recovery hop 2, 2026-10-04 19:55 JST.
- Durable evidence: hop 1 Send was accepted exactly once (`accepted_via=exact_user_message`, attempts=1, bound page `c4050a76-dd46-4405-a225-ff8cc163f2cc`). The page then showed `cloudflare_challenge Retry`; shortly afterward the worker marked hop 1 `abandoned` with reason `Temporary generation lost by tab loss or document reload` and created a fresh Temporary generation on the same endpoint. No causal claim is made that Cloudflare caused the loss.
- Recovery correctness preserved: the original accepted request was not replayed. Hop 2 is a distinct `execution_recovery` child with `recovery_of_request_id` and a compact `RECOVERY_SNAPSHOT` carrying immutable Send provenance.
- New defect: hop 2 is substantive work in a fresh Temporary chat, but its prompt contains only role/team/turn/workspace + recovery snapshot + report/route instructions. It does **not** carry the effective Goal, md-g6/9212-only constraint, spec-only/no-implementation stop condition, or a deterministic in-band governing semantic capsule. Because Temporary context was lost, prior-chat context cannot supply those semantics safely.
- Spec gap: current FR-037 / contract section 8 / T062 enumerate PLAN->REVIEW->PLAN, later role re-entry, Restart role, New chat, legal pre-boundary relocation/rebuild, and accepted-send reconciliation child, but do not explicitly include post-acceptance `execution_recovery` children created after Temporary generation loss.
- Impact: accepted generation recovery can safely preserve exactly-once Send provenance yet continue under an under-scoped prompt, allowing policy/endpoint/spec-only constraints to disappear precisely on the recovery path that needs them most.
- Required spec change: classify `execution_recovery` as a later substantive Team hop and require the same governing semantic source, continuation contract, execution facts/reference, endpoint/mode constraints, and pre-Send verification as every other substantive hop. Recovery snapshot remains supplemental provenance only. Add a deterministic test for accepted Temporary hop -> document/page loss -> same-endpoint fresh generation -> execution-recovery child with governing capsule; prove original attempts remain 1 and no original Send replay occurs.

### B-014 — `legacy_exact` continuation conflicts with the repository compact-prompt invariant
- Observed: independent REVIEW Turn 1 of second-pass task `cdpa-idem-9ca9cc8720b33efd287bb065`, 2026-10-04.
- Evidence: the refreshed design permits later substantive hops to deliver `legacy_exact`, defined as exact canonical task/effective Goal text when it is <=4096 code points. Repository rules and README explicitly preserve the opposite invariant for later turns of the same role: do not repeat the full Goal; current `PromptBuilder.build` intentionally includes `Goal:` only on turn 1.
- Root cause: the draft attempts to solve semantic delivery by using bounded full-Goal replay as a compatibility fallback while simultaneously classifying compact later prompts as PRESERVE. Size-bounding does not remove the policy contradiction.
- Impact: implementation could violate a durable repository prompt contract while still passing the feature's own tests, or preserve compactness and leave B-008/B-010/B-013 unresolved.
- Required spec change: remove exact full task/Goal replay as a later-hop compatibility payload. Define one bounded authoritative continuation contract instead. Legacy/pre-feature work that lacks such a contract must fail closed before a later substantive Send and recover through the already specified explicit Stop -> new Team task path. If full-Goal repetition is intentionally desired, it must be handled as an explicit repository-policy change rather than reported as preserved behavior.

### B-015 — Governing payload can be hash-valid while semantically incomplete
- Observed: independent REVIEW Turn 1 of second-pass task `cdpa-idem-9ca9cc8720b33efd287bb065`, 2026-10-04.
- Evidence: the proposed explicit `governing_objective` + optional `governing_directives` can omit mandatory directives that exist only in the full task/Goal (for example md-g6-only, endpoint-9212-only, Temporary/Fresh-only, SPEC-KIT/no-implementation) while `semantic_sha256`, `effective_goal_sha256`, and execution-facts checks all remain internally valid.
- Root cause: integrity/identity is specified, but no construction-time authority/completeness contract requires the bounded governing payload to be the complete set of cross-hop mandatory semantics. Hashing a caller-supplied subset proves that subset, not semantic completeness.
- Impact: a later role or `execution_recovery` child can receive a cryptographically consistent capsule and still lose exactly the mandatory constraints exposed by B-008/B-013.
- Required spec change: make the structured bounded governing payload the authoritative complete cross-hop contract by construction; task/Goal text outside that payload must not be relied on as mandatory continuation policy. Admission/Goal-change must fail closed when a workflow requiring later substantive hops lacks this complete bounded contract. Add positive fixtures containing the actual device/endpoint/Temporary/spec-only directives and negative fixtures proving a missing required directive is rejected before admission or before the later substantive Send. Do not use full-Goal replay as the completeness fallback.

