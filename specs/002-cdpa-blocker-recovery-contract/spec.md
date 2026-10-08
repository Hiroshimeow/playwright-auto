# Feature Specification: CDPA Blocker Recovery Contracts

**Feature Directory**: `specs/002-cdpa-blocker-recovery-contract`  
**Created**: 2026-10-04  
**Status**: Draft for independent spec review after blocker-ledger reconciliation  
**Scope**: Specification and review only. Production implementation is explicitly out of scope for this cycle.

## Context

The external blocker ledger at `.hcu/specs/cdpa-blocker-spec-loop-B.md` is append-only evidence for this feature. Every recorded incident must be represented by an explicit contract or acceptance test before this feature can be called implementation-ready.

The ledger currently establishes these blocker classes:

1. Accepted persistent Send can have exact accepted-user/page evidence while the URL is still provisional, then become canonical on the same page shortly after `accepted_conversation_identity_unresolved` is raised.
2. A legal pristine initial endpoint placement can collide with generic `browser_endpoint_id` immutability; later endpoint migration must remain illegal.
3. Dependency WAIT can hide the primary parent blocker unless parent cause remains projected.
4. Windows `cdpa.cmd` can corrupt multiline task text and leak CLI option tokens into the admitted task.
5. A complete, exact-bound, valid route can be missed because completion action buttons are treated as mandatory and stale LISTEN `IS_STREAMING` evidence survives long enough to cause timeout/reload/route repair.
6. Post-acceptance admission corruption cannot be repaired by rewriting the accepted prompt or by Change Goal; the existing replacement owner has strict eligibility and Team/Solo semantics that the contract must preserve.
7. Later substantive role hops currently omit Goal and role constructor by design. This compact-prompt optimization can drop mandatory task constraints unless the hop carries a verified durable governing-contract reference.
8. Accepted Temporary tab/document loss can create a distinct `execution_recovery` child after the irreversible Send boundary. Recovery must inherit the accepted parent hop's prompt-authority contract: normal substantive work receives governing semantics, while locked route/report repair remains locked and non-substantive.
9. Bounded full-Goal replay is not a legal compatibility fallback for later turns; the repository compact-prompt rule applies regardless of Goal size.
10. A hash-valid bounded payload is insufficient unless its authority is complete by construction: caller-authored governing JSON owns cross-hop task policy, and the workflow-agent continuation contract owns cross-hop role policy.
11. Cloudflare/CDP/session transitions are observed environmental conditions only. No current evidence proves Cloudflare is the root cause of the blocker classes above.

The feature must preserve exactly-once Send, exact execution identity, Temporary Fresh semantics, one workflow home endpoint, compact later-turn prompts, and one authoritative owner per state transition.

### Ledger aliases

The external ledger currently contains two entries labelled B-002. This spec does not rewrite that history. For traceability only:

- **B-002-terminal** = valid terminal response missed / stale LISTEN.
- **B-002-receipt** = immutable receipt divergence versus accepted-send reconciliation.

## Clarifications and Assumptions

### Session 2026-10-04

- **Cloudflare causality**: not assumed. Challenge/cache/session observations stay correlation evidence unless a causal boundary is proven.
- **Accepted/ambiguous Send replay**: forbidden.
- **Workflow endpoint ownership**: a Team workflow has one home endpoint for its lifetime after explicit or legal one-time placement. Recovery never spills accepted work to another endpoint.
- **Task ingress canonicalization**: file/stdin decode is strict UTF-8; canonicalization changes only CRLF and lone CR to LF; every other code point and leading/trailing whitespace is preserved. Team and Solo use the same rule.
- **Accepted identity settle anchor**: the bounded settle window starts only at the first durable exact provisional-identity decision point—the first moment the worker has unchanged accepted SENT provenance, exact accepted user/page evidence, and a still-provisional persistent conversation that would otherwise be classified unresolved. It does **not** start at Send/accepted time.
- **Accepted identity bound**: persist a single 5.0-second wall-clock deadline from that first decision point, capped by the existing response-wait deadline. Restart, Resume, remount, or repeated polling never resets or extends it.
- **Unresolved identity after the bound**: becomes the existing BLOCKED `accepted_conversation_identity_unresolved` state. No reconciliation child is created automatically. Only explicit operator Route PLAN may invoke the existing accepted-send reconciliation owner.
- **Receipt divergence**: is provenance-integrity failure, not unresolved identity. It never enters accepted-send reconciliation. The existing worker block owner records non-retryable `accepted_receipt_provenance_divergence`; while the divergence persists, Resume/Retry/Restart role/New chat/Route PLAN are not legal continuation controls. Explicit operator Stop is the only in-task exit, after which separately authorized replacement may preserve the forensic records.
- **Post-acceptance malformed admission**: first fail closed into a BLOCKED admission-integrity incident while preserving the accepted request. An operator must explicitly Stop that malformed task before replacement. Only then may the existing replacement transaction create a replacement; this makes the current STOPPED eligibility and optional exact-team reuse explicit rather than bypassed.
- **Replacement executable authority**: corrected canonical task/goal fields are authoritative. Retained reports and prior outcome are non-executable context and must not be folded back into executable text if the prior text is known corrupt.
- **Continuation prompts**: repository policy forbids repeating the full Goal and generic constructor on later turns. Team create and Team Goal-change therefore require a bounded caller-authored `governing_objective` plus an explicitly supplied ordered `governing_directives` list. Their canonical JSON payload is the complete authoritative cross-hop task contract; freeform task/Goal text is turn-1 context and is not a fallback semantic source. Later substantive hops use the existing three-payload wire format: governing semantic contract <=4096 code points, role continuation <=2048, facts/ref <=4096. Fixed ASCII framing remains exactly 103 characters, so the maximum serialized governing block remains 10,343 code points. Temporary generation recovery inherits the accepted parent's prompt-authority class: normal substantive parents receive the governing payloads, while locked route/report repair remains locked and non-substantive. The recovery snapshot is supplemental provenance only. No role may be expected to read a manifest or use an optional connector to discover semantics. Format-repair/report-repair prompts remain narrowly locked and are not substantive hops.
- **Terminal response authority**: stable, valid, exact-bound DOM output with current-page generation inactive is sufficient terminal authority. Copy/feedback/regenerate controls are corroboration only. LISTEN/network state is supplemental and may never veto that DOM terminal result.

## Incident Coverage

| Ledger incident | Required contract coverage |
|---|---|
| B-001 | FR-001..FR-005, SC-001 |
| B-002-terminal | FR-030..FR-033, SC-009 |
| B-002-receipt | FR-006, FR-027, SC-003 |
| B-003 | FR-007..FR-011, FR-026, SC-002 |
| B-004 | FR-001..FR-003, SC-001 |
| B-005 | FR-004..FR-005, SC-004 |
| B-006 | Reviewer checklist CHK019/CHK020 must remain unchecked until a fresh independent review is clean |
| B-007 | FR-030..FR-033, SC-009 |
| B-008 | FR-034..FR-038, SC-010 |
| B-009 | FR-002, FR-015..FR-018, SC-001, SC-005 |
| B-010 | FR-034..FR-038, SC-010, SC-013 |
| B-011 | FR-032..FR-033, SC-009, SC-014 |
| B-012 | FR-027, SC-003, SC-015 |
| B-013 | FR-012, FR-034..FR-038, SC-010, SC-016, T011, T062 |
| B-014 | FR-034, FR-037..FR-038, SC-010, SC-013, T043..T045, T064, T066 |
| B-015 | FR-034..FR-036, SC-013, T043..T049, T064..T066 |

## User Scenarios & Testing

### User Story 1 — Preserve an accepted Send while canonical identity settles (Priority P1)

A persistent workflow has crossed the accepted-user boundary exactly once. The exact owned page/user are still proven, but its conversation URL is provisional. CDPA observes the same page for a short persisted interval at the point where it would otherwise block.

**Acceptance Scenarios**

1. Given unchanged accepted SENT provenance, exact accepted user/page, and a provisional URL, when the first unresolved decision occurs, CDPA persists one settle start/deadline in the existing hop wait, remains RUNNING/read-only, and does not replay or mutate the browser.
2. Replaying the observed incident timeline—accepted user roughly 34 seconds before the unresolved decision, then canonical URL roughly 92 ms after that decision—must allow canonical enrichment because the 5-second interval starts at the decision, not at Send.
3. Restart/Resume/remount during that interval reuses the same persisted deadline.
4. Same page/user becoming canonical before the deadline enriches the same receipt and continues the original response wait without extending its deadline.
5. Page/user loss, canonical mismatch, or settle expiry with the receipt unchanged blocks as `accepted_conversation_identity_unresolved`. The hop remains non-replayable.
6. Only an explicit operator Route PLAN from that exact blocked state may create the existing accepted-send reconciliation child.
7. Immutable hop-versus-ledger receipt divergence fails closed before backend/DOM/reopen activity and is not eligible for Route PLAN reconciliation.
8. Temporary workflows never enter saved-conversation identity settle/reopen behavior.

### User Story 2 — Place endpoints without mutating workflow identity (Priority P1)

A workflow is either explicitly pinned at creation or intentionally unpinned for one scheduler placement before Send.

**Acceptance Scenarios**

1. Explicitly pinned 9202/9212 tasks remain on that home endpoint across roles and legal controls, even when the requested endpoint equals the repository default.
2. Admission persists whether browser endpoint, required endpoint, conversation mode/Temporary intent, team, repository, and context policy were explicitly supplied or omitted; equality of a final value with a default is not proof that an option survived transport.
3. A pristine unpinned Team task may perform exactly one atomic initial placement before Send and then becomes pinned.
4. Initial placement may change only `browser_endpoint_id`, `endpoint_pinned: false->true`, implicit `conversation_mode` derived from the selected endpoint when mode was omitted, and omitted/no-bootstrap `context_policy: null->fresh`, plus the existing active role/hop endpoint binding. Explicit Temporary/persistent mode, explicit fresh/bootstrap policy, bootstrap state, required endpoint, task identity, and accepted-request fields are immutable during placement.
5. If an implicit mode/bootstrap combination cannot be preserved on a candidate endpoint, scheduler placement waits/fails closed instead of rewriting bootstrap to Fresh.
6. Ordinary persistence still rejects later task-level endpoint mutation.
7. Resume, Retry, Restart role, New chat, role transitions, recovery, and capacity pressure never migrate a pinned Team workflow.
8. A Temporary workflow pinned to 9202/9212 never spills to 9222.

### User Story 3 — Admit complete instructions losslessly (Priority P1)

A caller submits multiline task instructions through Windows or another CLI/API ingress.

**Acceptance Scenarios**

1. CRLF/lone-CR become LF; all other Unicode code points, quotes, blank lines, option-like text, and leading/trailing whitespace round-trip exactly.
2. Strict UTF-8 decoding errors fail before task creation/browser work.
3. Parser options such as `--config`, `--browser`, `--team`, repository, and Temporary mode can never become task text.
4. Persisted canonical text/hash **and caller intent/provenance** must match the admission envelope before Send: explicit endpoint equal to default remains explicit/pinned; omitted endpoint remains scheduler-placeable; explicit Temporary equal to an endpoint default remains explicit; explicit persistent remains persistent; required endpoint, requested team, repository, and context/bootstrap intent retain their supplied/omitted status.
5. If corruption is discovered only after acceptance, no prompt/request/history is rewritten and no duplicate Send occurs.

### User Story 4 — Recover post-acceptance admission corruption without falsifying history (Priority P1)

An accepted request is later proven to have been admitted from corrupted task/goal text.

**Acceptance Scenarios**

1. Detection records a distinct admission-integrity incident and leaves the accepted request non-replayable.
2. The malformed task becomes BLOCKED through the existing worker block owner; replacement is not legal yet.
3. Explicit operator Stop terminalizes the malformed task without replaying Send or rewriting the accepted prompt.
4. Only then may `TaskStore.replace_task_and_rewire` create a replacement using the trusted corrected canonical task/goal as executable authority.
5. Replacement preserves Team versus Solo workflow mode, role set, endpoint/conversation policy, dependencies, and applicable Solo goal semantics.
6. Retained reports/incident references are context only. Known-corrupt target task/effective goal is never copied into executable replacement instructions.
7. Existing replacement idempotency/linkage and optional atomic child rewiring remain the only replacement graph owner.
8. Change Goal remains future-hop-only and cannot masquerade as accepted-prompt repair.

### User Story 5 — Accept the exact completed route without stale transport veto (Priority P1)

The exact accepted user turn has a stable, valid assistant route in DOM; the page is no longer generating, but action buttons may render late and LISTEN may still contain stale `IS_STREAMING`.

**Acceptance Scenarios**

1. Stable valid exact-bound DOM output is accepted when `stop_visible=false` and the bound assistant turn is not the current response-activity turn.
2. Missing copy/feedback/regenerate controls do not prevent acceptance.
3. A stale LISTEN status does not postpone DOM acceptance, re-arm timeout, trigger reload, or queue route repair.
4. LISTEN terminal content may supplement only the already-bound assistant identity; it cannot bind a different request/turn.
5. Full/sparse snapshot caching cannot hide any signal that terminal arbitration actually requires.
6. A genuinely ambiguous/incomplete persistent response may retain the existing bounded recovery/reload policy; Temporary workflows still never auto-reload.

### User Story 6 — Preserve the governing task contract on later hops (Priority P1)

A Team workflow routes PLAN → REVIEW → PLAN or otherwise re-enters a role after turn 1. The prompt remains compact but cannot lose mandatory Goal/role constraints.

**Acceptance Scenarios**

1. Turn 1 may carry the full Goal/role constructor under existing rules.
2. Team create and Team Goal-change public ingress require both `governing_objective` and `governing_directives`. `governing_objective` is nonblank; `governing_directives` is an explicitly supplied ordered list of nonblank strings and may be empty only when the caller asserts there are no additional cross-hop directives. These fields are the complete authoritative cross-hop task contract; the caller is responsible for placing every constraint that must survive a later substantive hop into this contract. Freeform task/Goal text may provide turn-1 detail but is not later-hop policy, is not a fallback, and is never semantically compared, summarized, or inferred into the contract. The canonical semantic payload is compact JSON with fixed key order `objective`,`directives`, UTF-8, `ensure_ascii=false`, compact separators, no trailing newline, and CRLF/lone-CR normalized to LF inside strings without Unicode normalization. It must be <=4096 code points. Missing/invalid/oversize governing fields fail before durable/browser mutation with `governing_contract_required`. Each accepted original Goal revision owns its matching canonical governing payload.
3. Every later **substantive** Team hop carries three payloads in this exact wire grammar: `GOVERNING_SEMANTIC_SOURCE NNNNN\n<payload1>\nROLE_CONTINUATION_CONTRACT NNNNN\n<payload2>\nGOVERNING_EXECUTION_FACTS_REF NNNNN\n<payload3>`, where each `NNNNN` is a zero-padded five-digit Unicode-code-point length of the following payload. Payload maxima are 4096/2048/4096 code points respectively. The three fixed ASCII header lines plus the two separator newlines consume exactly 103 characters; there is no trailing newline after payload3. Therefore simultaneous payload maxima produce exactly 10,343 code points. Payload contents are never altered to fit framing. The role needs no filesystem or connector access to obtain these semantics.
4. Before Send, CDPA resolves the applicable Goal revision/semantic payload, exact canonical manifest path, and target role continuation contract from durable task/workflow-agent ownership, verifies hashes/source identity, and refuses to send on missing/mismatched source.
5. Workflow-agent definitions add bounded `continuation_contract`. For every role, that continuation contract is the complete authoritative cross-hop role contract. Built-in system roles use repository-owned compact continuation assets from the existing default-prompt owner. New/updated custom workflow agents must supply it and the custom-agent author must place every role constraint that must survive later substantive hops into it. A custom `system_prompt` is first-turn context only and is never a later-hop fallback or semantic source. Catalog/task snapshots persist the continuation contract. Legacy system-role tasks without the field resolve the repository system default. Legacy custom task snapshots without it block `governing_contract_unavailable` before a later substantive Send; after explicit Stop, recovery is **new Team task creation only** using the updated catalog definition. `replace_task_and_rewire` is not legal for this migration because its compatibility contract preserves the old task workflow-agent snapshot.
6. The later prompt delivers the canonical semantic payload verbatim in-band; handoff/report context is supplementary, never the sole policy source.
7. PLAN→REVIEW→PLAN, Restart role, New chat, execution relocation before the irreversible boundary, accepted-send reconciliation, and post-accepted Temporary generation-loss recovery preserve the parent hop's prompt-authority contract. Recovery of normal substantive work receives the governing semantic/role/facts payloads. Recovery of `route_repair` or `report_repair` remains the same locked non-substantive repair and receives no substantive governing payload. Recovery of accepted-send reconciliation preserves its no-replay/lineage restrictions. In every case the original accepted request stays immutable/non-replayable and the recovery child remains on the same workflow home endpoint.
8. Goal revision changes both Goal identity and its semantic payload only from the declared applicable hop onward.
9. Route-repair/report-repair prompts remain narrow output-repair operations and do not reintroduce the substantive Goal/semantic payload or authorize repeated work.
10. Full Goals and full constructor bodies are never duplicated inline on later turns. Budget is over canonical payload code points: governing semantic contract <=4096, role continuation <=2048, facts/ref <=4096; deterministic framing is exactly 103 ASCII characters; serialized governing block <=10,343 code points. Existing persisted Team tasks created before this feature that lack the authoritative governing payload block `governing_contract_unavailable` before their next substantive Send. The only compatibility recovery is explicit Stop followed by normal creation of a new Team task with caller-authored governing fields; there is no exact-text fallback or in-place migration escape hatch.

### User Story 7 — Surface dependency stalls as secondary state (Priority P2)

A child waits behind a blocked or unsuccessful parent.

**Acceptance Scenarios**

1. Waiting child consumes zero browser capacity and performs no browser mutation.
2. Projection exposes exact parent task id/status and non-sensitive parent blocker code without copying the blocker as the child's own root cause.
3. Parent DONE releases the child without endpoint rebinding.
4. Parent STOPPED/failed/missing remains an explicit dependency terminal reason instead of a generic scheduler stall.

## Functional Requirements

### Admission and replacement

- **FR-001**: Team and Solo task ingress MUST share one task-text canonicalizer: strict UTF-8 for file/stdin, CRLF/lone-CR to LF only, preserve every other code point and leading/trailing whitespace, reject only canonical text whose `.strip()` is empty.
- **FR-002**: Admission equality/integrity MUST hash UTF-8 bytes of canonical text; parser options MUST remain structurally separate from task text. The same task manifest MUST durably preserve normalized values **and supplied/omitted provenance** for requested team, repository, browser endpoint, required endpoint, conversation mode (`temporary|persistent|omitted`), context policy (`fresh|bootstrap|omitted`), bootstrap presence, and the derived endpoint-pin intent. Before admission completes, those values/provenance plus persisted task text MUST match the submitted envelope exactly; an explicit value equal to a default is still explicit, and option loss is an admission failure rather than a scheduler default.
- **FR-003**: Admission mismatch detected before Send MUST fail before browser mutation.
- **FR-004**: Admission corruption detected after accepted/ambiguous Send MUST preserve the accepted prompt/request/history, block the malformed task through the existing worker block owner, and require explicit operator Stop before replacement. No replay or in-place rewrite is legal.
- **FR-005**: Replacement for this incident MUST use existing `TaskStore.replace_task_and_rewire` only after STOPPED eligibility. It MUST preserve workflow mode and validated placement/context/dependency semantics; trusted corrected canonical task/goal fields are executable authority; retained reports/incidents are non-executable context; Change Goal remains future-hop-only.

### Durable accepted identity

- **FR-006**: RequestLedger SENT state, attempts, accepted timestamp, immutable SendReceipt fields, exact accepted-user identity, and exact page binding remain authoritative after acceptance.
- **FR-007**: A persistent accepted request MAY enter bounded identity observation only when the unchanged SENT receipt, accepted user, exact page, and provisional conversation are all proven. On the first exact provisional decision that would otherwise block, persist `accepted_identity_started_at=now` once and `accepted_identity_deadline_at=min(start+5.0s, existing wait.deadline_at)`. Do not use `sent_at` as the settle start.
- **FR-008**: If the existing response deadline is already reached when that first decision occurs, do not grant a new settle budget; block immediately. Otherwise identity observation is read-only: no Send, composer refill, upload replay, reload, goto, reopen, New chat, endpoint migration, or deadline extension.
- **FR-009**: Restart, Resume, repeated polling, page remount, or source drift MUST reuse the persisted settle start/deadline and never reset/extend it.
- **FR-010**: Canonical enrichment MUST require the same request, unchanged immutable receipt fields, accepted user identity, exact page binding, and canonical UUID/URL agreement; success continues the original response wait with its existing deadline and attempts unchanged.
- **FR-011**: With an unchanged receipt, page/user loss, canonical conflict, or settle expiry MUST end in BLOCKED `accepted_conversation_identity_unresolved`. The system MUST NOT automatically create the reconciliation child; explicit operator Route PLAN is the sole existing authorization for `_route_unresolved_accepted_to_plan`.
- **FR-012**: Temporary workflows MUST preserve Fresh context and MUST NOT require/synthesize saved-conversation identity, reopen URLs, or automatic reload.

### Endpoint placement

- **FR-013**: Workflow home endpoint and role execution/page binding MUST be distinct concepts.
- **FR-014**: Explicitly pinned Team tasks MUST keep one home endpoint for workflow lifetime.
- **FR-015**: An intentionally unpinned Team task MAY perform exactly one atomic initial placement only while pristine pre-Send, receiptless/message-identity-free, role-unbound, and admission provenance proves browser endpoint was omitted and no required endpoint pins placement.
- **FR-016**: The legal initial-placement owner MAY mutate only: `browser_endpoint_id`; `endpoint_pinned` from false to true; `conversation_mode` only when admission marked mode omitted, deriving exactly the selected endpoint default; `context_policy` only when admission marked it omitted **and** no bootstrap exists, deriving `fresh`; and existing active role/hop endpoint-binding fields needed to materialize that same placement. Explicit Temporary/persistent mode, explicit fresh/bootstrap policy, bootstrap data, required endpoint, task identity, request/receipt/message identity, and all accepted-work fields MUST remain unchanged. Generic persistence MUST continue to reject home-endpoint mutation.
- **FR-017**: Placement candidate selection MUST satisfy the preserved mode/context/bootstrap contract. If no eligible endpoint can preserve it, the task waits/fails closed; placement MUST NOT convert bootstrap to Fresh or explicit persistent/Temporary mode to an endpoint default. Resume, Retry, Restart role, New chat, role transitions, source drift, and accepted-work recovery MUST NOT change Team home endpoint.
- **FR-018**: Required-endpoint release remains a separate explicit pre-boundary policy and MUST NOT authorize relocation of accepted work. Admission tests MUST distinguish omitted endpoint+implicit mode, explicit endpoint equal to default, explicit Temporary equal to endpoint default, explicit persistent, required endpoint, and unpinned bootstrap context.

### Dependency and observability

- **FR-019**: Dependency WAIT MUST identify exact parent ids/statuses and distinguish waiting, stopped/failed, missing, and team-busy causes.
- **FR-020**: Dependency-waiting tasks MUST consume zero browser execution capacity and perform no browser mutation.
- **FR-021**: Parent primary blocker codes MUST remain visible in projection/timeline without becoming a duplicate child blocker owner.
- **FR-022**: Operational projection MUST expose non-sensitive request-boundary, endpoint-placement, accepted-identity timing/outcome, governing-contract reference status, response-terminal arbitration, LISTEN freshness, reconciliation authorization, and dependency cause.
- **FR-023**: Cloudflare/challenge/cache/session evidence MUST remain environmental evidence unless causal proof ties it to a product transition. Anti-bot bypass and active-workflow cache clearing are out of scope.
- **FR-024**: Existing manual draft/attachment ownership and exactly-once upload/Send protections MUST be preserved.
- **FR-025**: No requirement may add a second request ledger, conversation-identity owner, endpoint registry, replacement graph, prompt-policy owner, response state machine, or dependency state machine.
- **FR-026**: Recovery MUST remain deterministic across worker restart/page remount/source drift; durable request/task/hop evidence wins over transient absence, and no restart may mint a fresh identity-settle or prompt-contract budget.
- **FR-027**: Immutable hop-versus-ledger SendReceipt divergence (except permitted one-way conversation-id enrichment) is provenance-integrity failure. The existing worker block owner MUST classify it durably as non-retryable `accepted_receipt_provenance_divergence` before backend/DOM/reopen work, preserve both records/attempts, and keep the accepted request non-replayable. While the divergence persists, Resume/Retry/Restart role/New chat/Route PLAN MUST NOT continue or reconcile the request. The only legal in-task exit is explicit operator Stop; if work must continue, a separately authorized replacement may start from the STOPPED task while preserving both divergent records as forensic evidence.
- **FR-028**: Every new/fixed transition MUST have deterministic focused positive/negative tests plus future end-to-end Temporary acceptance on the explicitly selected endpoint.
- **FR-029**: This cycle MUST stop before production implementation. Spec readiness is not runtime-fix evidence.

### Terminal response arbitration

- **FR-030**: For an exact accepted-user-bound assistant, stable valid DOM output is terminal authority when the current page shows no active generation for that bound turn. Current-page DOM generation evidence outranks cached transport diagnostics.
- **FR-031**: Copy/feedback/regenerate action controls MAY corroborate completion but MUST NOT be mandatory for terminal acceptance.
- **FR-032**: LISTEN/network status is supplemental. Cached `IS_STREAMING` MUST NOT veto a terminal DOM candidate. PageObserver MUST expose a unique `observer_instance_id`, current epoch, monotonically increasing `signal_seq`, and UTC observation time. The existing hop wait/controller evidence owns the last-consumed LISTEN watermark. A LISTEN status is eligible to influence a nonterminal decision only when observer instance, epoch, page, conversation, and bound-response scope match; its observation is no older than 5.0 seconds; and any timeout/progress re-arm uses a sequence strictly newer than the last sequence already used for that purpose. Re-reading the same sequence never extends progress/deadlines. Observer recreation/worker restart invalidates all prior-instance LISTEN evidence until a fresh current-scope signal arrives.
- **FR-033**: Once a terminal DOM candidate is proven, timeout logic MUST NOT reload, query/re-arm from stale stream state, or enqueue state/route repair for that request. Snapshot caching MUST either expose every signal used by terminal arbitration in the sparse probe or force a full refresh when such a signal changes. Temporary no-reload policy remains absolute.

### Governing contract continuity

- **FR-034**: Team-create ingress (CLI, dashboard/API command normalization, worker command payload, and direct TaskStore test seam) and Team Goal-change ingress MUST require both `governing_objective` and `governing_directives`. `governing_objective` MUST be a nonblank string. `governing_directives` MUST be explicitly present as an ordered list of nonblank strings and MAY be empty only as an explicit caller assertion that no additional cross-hop directives exist. Together they are the complete authoritative cross-hop task contract; callers MUST place every constraint that must survive later substantive hops into it. Freeform task/Goal text is not a compatibility/fallback semantic source, and CDPA MUST NOT infer, summarize, or semantically compare missing policy from that text. The caller-authored Team create/Goal-change semantic envelope (`task`/new `goal`, governing objective/directives, and explicit control provenance) MUST traverse one lossless structured file/stdin/API-equivalent admission carrier rather than an unsafe Windows `.cmd` multiline argv boundary. Admission MUST compare the intended canonical governing payload/hash with the persisted payload/hash before browser mutation. Each governing string uses newline canonicalization CRLF/lone-CR -> LF, preserves all other code points/whitespace, and receives no Unicode normalization. The payload MUST serialize exactly as compact JSON with fixed key order `objective`,`directives`, `ensure_ascii=false`, no spaces, no trailing newline; it MUST be <=4096 Unicode code points. SHA-256 is computed over UTF-8 bytes of that canonical payload only, excluding wire framing. Missing, invalid, oversize, or transport-corrupted governing fields fail before durable/browser mutation with `governing_contract_required`.
- **FR-035**: Workflow-agent schema/catalog/API MUST add `continuation_contract`: canonical non-empty text <=2048 Unicode code points. It is the complete authoritative cross-hop role contract. New/updated custom workflow-agent authors MUST place every role constraint intended to survive later substantive role turns into it; their full custom `system_prompt` remains first-turn context only and MUST NOT be used as a later-hop fallback. `normalize_workflow_definition`, catalog create/update/read/resolve, workflow snapshots, dashboard/API workflow-agent create/update, TaskStore wrappers, and worker command application all preserve it. Built-in system roles obtain complete compact continuation contracts from repository-owned default continuation assets co-located with the existing CDPA prompt defaults. The governing reference MUST bind task id, hop id, exact canonical `manifest_path`, applicable Goal revision/effective-goal SHA-256, governing semantic SHA-256, target role, and continuation-contract SHA-256. `manifest_path` is the exact absolute canonical string already owned by the task manifest: `str(Path(state["manifest_path"]).expanduser().resolve())`; before Send it MUST equal the persisted `state["manifest_path"]`, resolve inside the configured plans root, and identify the actual manifest being executed. Resolution precedence for continuation semantics is task snapshot when it contains the field; otherwise repository system-role default only when the role is a built-in system role. Legacy custom snapshots without it have no fallback.
- **FR-036**: The substantive prompt MUST serialize the three verified payloads with the exact length-prefixed wire grammar from User Story 6. Payload lengths are Unicode code-point counts after canonicalization, not UTF-8 byte counts; payload hashes are SHA-256 over canonical UTF-8 payload bytes and exclude framing. `ROLE_CONTINUATION_CONTRACT` uses newline canonicalization only, no Unicode normalization/trailing synthetic newline, nonblank, <=2048 code points. `GOVERNING_EXECUTION_FACTS_REF` is compact canonical JSON with fixed key order `task_id`,`hop_id`,`manifest_path`,`repository`,`workflow_mode`,`home_endpoint`,`required_endpoint`,`conversation_mode`,`context_policy`,`goal_revision`,`effective_goal_sha256`,`semantic_sha256`,`target_role`,`continuation_sha256`, `ensure_ascii=false`, compact separators, no trailing newline, <=4096 code points. Fixed framing is exactly 103 ASCII characters, so total serialized governing block MUST be <=10,343 code points and simultaneous payload maxima are legal. Missing governing contract/section, missing or altered admitted directive, custom-role continuation gap, manifest-path mismatch/noncanonical/outside-plans-root evidence, source/hash mismatch, length-prefix mismatch, or any payload/total bound violation MUST fail closed as non-retryable `governing_contract_unavailable` before Send/substantive browser mutation. Handoff/report content is supplementary context only. Workflow-agent catalog updates after task creation do not rewrite task snapshots.
- **FR-037**: PLAN→REVIEW→PLAN, Restart role, New chat, accepted-send reconciliation, legal pre-boundary relocation, and post-accepted Temporary generation-loss recovery MUST preserve/regenerate policy from the same authoritative task/hop/revision sources **and inherit the accepted parent hop's prompt-authority contract**. The recovery child MAY keep `kind="execution_recovery"` for lineage, but durable recovery metadata MUST retain the parent hop kind/lock fields needed by the existing prompt owner. Recovery of a normal substantive parent verifies and receives the governing semantic/role/facts payloads. Recovery of `route_repair` or `report_repair` MUST render the corresponding locked repair prompt, preserve validation/locked-route/report fields, receive no substantive governing payload, and MUST NOT authorize repeated substantive work. Recovery of accepted-send reconciliation remains substantive but preserves its original no-replay/reconciliation lineage restrictions. All recovery preserves the existing no-replay, home-endpoint, Temporary, and Fresh-context invariants. Recovery snapshot/report/prior chat is supplemental only. Goal revisions affect both Goal identity and the authoritative governing payload only for eligible future hops. An accepted prompt is never rewritten because a later source drifts. Already-persisted Team tasks missing an authoritative governing payload MUST block `governing_contract_unavailable` before their next substantive Send; there is no exact-task/Goal fallback. Recovery is explicit Stop followed by normal creation of a new Team task with caller-authored governing fields. Legacy built-in roles may use repository default continuation assets. Legacy custom snapshots missing `continuation_contract` also require explicit Stop then **new task creation after the custom catalog definition is updated**; replacement is prohibited for this migration because replacement must preserve the old snapshot.
- **FR-038**: Route-repair/report-repair are non-substantive output correction paths and MUST retain their locked minimal prompts without re-injecting the full Goal/governing semantic payload. Later normal prompts MUST remain compact by bounded semantic payload and MUST NOT duplicate the full Goal or full built-in/custom constructor bodies.

## Key Entities

- **Task Admission Envelope**: canonical task text plus optional separately canonicalized Solo goal, parser/config metadata, explicit supplied/omitted control provenance, governing semantic payload for Team work, integrity hashes, and admitted task id.
- **Workflow Home Endpoint**: one durable Team endpoint after explicit or legal initial placement.
- **Role Execution Binding**: endpoint/page/conversation facts for one role execution; never overrides workflow home.
- **Durable Request Boundary**: RequestLedger + SendReceipt exactly owning irreversible Send provenance.
- **Conversation Identity Observation**: additive timing/evidence fields in the existing hop wait; not a second state machine.
- **Accepted-Send Reconciliation**: existing operator-authorized PLAN child for unchanged accepted SENT provenance only.
- **Provenance Integrity Failure**: non-retryable `accepted_receipt_provenance_divergence` block owned by the existing worker; no continuation control is legal while divergence persists, only explicit operator Stop.
- **Admission Corruption Incident**: blocked/then explicitly stopped malformed task that may become an existing-owner replacement target.
- **Terminal Response Evidence**: exact user→assistant binding plus stable valid DOM candidate and current-page generation state.
- **LISTEN Freshness Evidence**: PageObserver instance id + epoch + additive signal sequence/time, compared with a watermark in existing hop wait; never independent route authority.
- **Governing Semantic Contract**: required caller-authored canonical JSON `{"objective":...,"directives":[...]}` <=4096 Unicode code points. It is the complete authoritative cross-hop task contract; freeform task/Goal text, prior chat, reports, and optional tools are never fallback semantic sources.
- **Role Continuation Contract**: exact workflow-agent compact continuation semantics, <=2048.
- **Governing Execution Facts + Reference**: canonical compact JSON durable facts and hashes, <=4096 Unicode code points. Together the three payloads are <=10240 code points; exact wire framing adds 103 ASCII characters, so the serialized governing block is <=10,343 code points.
- **Dependency Gate**: derived parent-readiness state; not a separate scheduler.

## Non-Goals

- Bypassing Cloudflare, anti-bot controls, or account protections.
- Moving accepted workflow ownership between endpoints/accounts.
- Rewriting all ChatGPT/runtime state handling or introducing a new orchestration framework.
- Turning Temporary chats into saved persistent conversations.
- Repeating huge Goals/constructors in every later prompt.
- Making LISTEN an independent terminal-routing authority.
- Automatic replacement of accepted corrupted work without explicit Stop/authorization.
- Production implementation, deployment, worker restart, cache clearing, reset/stash/clean/branch switch, or unrelated worktree mutation in this cycle.

## Success Criteria

- **SC-001**: Team/Solo ingress fixtures round-trip canonical text exactly, including CR/LF variants, blank edges, Unicode, quotes, option-like lines, and strict UTF-8 failure; zero parser tokens leak into task text. Deterministic admission cases prove supplied/omitted provenance survives even when values equal defaults: omitted endpoint+implicit mode, explicit endpoint equal to default, explicit Temporary equal to endpoint default, explicit persistent, required endpoint, requested team/repository, and context/bootstrap intent.
- **SC-002**: A deterministic fixture reproduces the observed ~34-second accepted-to-decision delay plus ~92-ms decision-to-canonical transition and proves the settle starts at first exact unresolved decision, survives restart, never extends the original response deadline, and performs zero browser mutations.
- **SC-003**: Unchanged-receipt settle failure becomes BLOCKED and requires operator Route PLAN; immutable receipt divergence becomes non-retryable `accepted_receipt_provenance_divergence` before backend/DOM/reopen, never enters that owner, rejects continuation controls while divergence persists, and can leave the task only through explicit operator Stop; attempts remain unchanged.
- **SC-004**: Post-acceptance admission corruption proves BLOCKED → explicit Stop → existing replacement, no replay/rewrite, exact corrected executable authority, Team/Solo preservation, and idempotent replacement linkage.
- **SC-005**: Endpoint tests prove one legal pristine initial placement, exact placement mutation whitelist, preservation of explicit Temporary/persistent and fresh/bootstrap intent, fail-closed unpinned bootstrap incompatibility, and rejection of all later home-endpoint mutation; Temporary 9202/9212 workflows never spill to 9222.
- **SC-006**: Dependency tests prove zero-slot waiting and exact parent cause projection/release.
- **SC-007**: Existing exactly-once Send, accepted-user ownership, manual draft/attachment, source-drift, Temporary no-reload, and replacement-graph regressions remain green.
- **SC-008**: Cloudflare remains evidence-only unless a future independent causal proof changes scope.
- **SC-009**: Deterministic response tests accept a stable exact-bound valid DOM route with delayed/missing completion buttons and stale `IS_STREAMING` without reload/status/repair, while still rejecting active/incomplete/wrong-bound output.
- **SC-010**: PLAN→REVIEW→PLAN and other substantive continuation tests prove semantic payload <=4096 code points, continuation payload <=2048, facts/ref payload <=4096, exact 103-character framing, and total governing block <=10,343; carry mandatory semantics without filesystem/connector/prior-chat dependence; preserve applicable Goal revision/task snapshot immutability; block before Send when semantics cannot be obtained; and remain compact without any full-Goal/constructor duplication on later turns.
- **SC-011**: Spec analysis/review reports zero unresolved CRITICAL/HIGH defects; CHK019 and CHK020 are checked only by a fresh independent REVIEW.
- **SC-012**: No production-code file is modified by this spec-only cycle.
- **SC-013**: Supported Team create and Goal-change ingress tests cover required governing fields and canonical JSON equality across intended structured carrier, CLI/API normalization, and store; Windows launcher/file/stdin acceptance includes multiline objective/directives, embedded CR/LF, quotes, backslashes, option/header-like lines, Unicode, reordered input-map keys, and directive-order preservation. Tests cover missing objective, missing directives field, invalid directive entries, semantic payload at 4095/4096/4097, intended-vs-persisted governing hash mismatch fail-before-browser, pre-feature task without a governing payload fail-closed before the next substantive Send, and simultaneous payload maxima 4096/2048/4096 producing exactly 10,343 serialized code points. A B-013-style fixture proves caller-policy directives such as device affinity and SPEC-KIT/no-implementation survive in the semantic payload, while machine-owned endpoint/Temporary/Fresh constraints survive in the facts/ref; dropping or transport-corrupting an admitted required directive causes admission or pre-Send verification failure.
- **SC-014**: LISTEN freshness tests explicitly cover B-011: same-sequence no-rearm, fresh sequence advance, ≤5s age boundary, observer-instance restart invalidation, scope mismatch, and terminal-DOM precedence.
- **SC-015**: Receipt-divergence tests explicitly cover B-012: durable `accepted_receipt_provenance_divergence`, no backend/DOM/reopen/attempt increment, rejection of Resume/Retry/Restart/New chat/Route PLAN, and explicit Stop-only exit.
- **SC-016**: Deterministic Temporary generation-loss fixtures prove prompt-authority inheritance. For a normal substantive parent, at most one same-home-endpoint `execution_recovery` child is created, the original request remains non-replayable with attempts unchanged, and the child receives verified governing semantics before Send. For accepted `route_repair` and `report_repair` parents, recovery stays locked non-substantive, preserves the parent repair lock/validation fields, receives no substantive governing payload, and cannot repeat substantive work. Accepted-send reconciliation recovery preserves its lineage/no-replay restrictions. Missing/mismatched required authority metadata or governing semantics fails closed without reconstructing policy from handoff, prior chat, or an absent report.
