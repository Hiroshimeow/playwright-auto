# Data Model: CDPA Blocker Recovery Contracts

This feature extends existing owners only. Shapes below are contracts for future implementation; they are not authorization to add parallel stores.

## TaskAdmissionEnvelope

Authoritative boundary record for task/goal text before a browser mutation.

Fields:
- `intended_task_text`: caller-provided Unicode task instructions.
- `canonical_task_text`: strict newline-canonical form.
- `task_text_sha256`: SHA-256 of UTF-8 bytes of canonical task text.
- `canonical_goal` / `goal_sha256`: optional separately canonicalized Solo Goal when that field is supplied independently.
- `transport_kind`: `argv | structured_file | structured_stdin | api`; multiline Team create/Goal-change envelopes use only the lossless structured forms/API, while simple compatible one-line argv may remain only where it cannot split caller-authored semantic fields.
- `parser_options`: config/team/browser/repository/temp and other options, structurally separate from task/goal text.
- `normalized_team`, `normalized_repository`, `normalized_browser_endpoint_id`, `normalized_required_browser_endpoint_id`, `normalized_conversation_mode`, `normalized_context_policy`: normalized control values.
- `team_explicit`, `repository_explicit`, `browser_endpoint_explicit`, `required_endpoint_explicit`, `conversation_mode_explicit`, `context_policy_explicit`, `bootstrap_present`: caller supplied/omitted provenance. Explicit values remain explicit even when equal to defaults.
- `endpoint_pin_intent`: derived only from explicit endpoint/required-endpoint/solo/exact-team semantics; omitted endpoint for a normal Team task remains intentionally unpinned.
- `governing_objective`, `governing_directives`: required caller-authored Team semantics persisted under the existing task/goal owner; never model-inferred. Objective is mandatory/nonblank. Directives must be explicitly supplied as an ordered list of nonblank strings and may be empty only as an explicit assertion that no additional cross-hop directives exist. The intended canonical governing payload/hash are computed from the structured carrier before parser/store mutation and must equal the persisted payload/hash.
- `semantic_payload`: exact canonical compact JSON with fixed key order `objective`,`directives`, UTF-8, ensure_ascii=false, no spaces, no trailing newline. This payload is the complete authoritative cross-hop task contract; freeform task/Goal text is not a fallback semantic source.
- `semantic_sha256`: SHA-256 over UTF-8 bytes of `semantic_payload` only.
- Payload length is measured in Unicode code points and must be <=4096; missing/invalid/oversize governing fields are rejected before mutation with `governing_contract_required`.
- `admitted_task_id`: populated only after persisted task/goal hashes, governing semantic payload, normalized control values, and supplied/omitted provenance match the created manifest.

Canonicalization:
1. file/stdin decode with strict UTF-8;
2. CRLF → LF;
3. lone CR → LF;
4. preserve all other code points and all leading/trailing whitespace;
5. no Unicode normalization, blanket trim, or BOM removal;
6. reject only if canonical text `.strip()` is empty.

## WorkflowHomeEndpoint

Fields:
- `endpoint_id`
- `pinned`
- `placement_source`
- `placed_at`

Transitions:
- `UNPLACED -> PINNED`: once only for pristine pre-Send Team workflow whose admission provenance says endpoint was omitted and no required endpoint pins placement.
- During that transition, only `browser_endpoint_id`, `endpoint_pinned`, implicit `conversation_mode`, omitted/no-bootstrap `context_policy`, and the matching active role/hop endpoint binding may change.
- Explicit conversation mode, explicit context policy, bootstrap data, required endpoint, task identity, request/receipt/message identity, and accepted-work fields are immutable during placement.
- If the selected endpoint cannot preserve explicit mode/bootstrap/context semantics, placement waits/fails closed.
- `PINNED -> different endpoint`: illegal.
- Required-endpoint release: separate control; never rewrites accepted ownership.

Legacy rule:
- missing `endpoint_pinned` defaults fail-closed/pinned.

## RoleExecutionBinding

Fields:
- logical/physical role;
- endpoint id;
- page id/url;
- conversation generation;
- Temporary document id when applicable.

Rules:
- Team role endpoint equals WorkflowHomeEndpoint after placement.
- Page/conversation changes obey request-boundary controls.
- Temporary document identity is not a saved-conversation identity.

## DurableRequestBoundary

Existing owner: RequestLedger + SendReceipt.

Relevant fields:
- request id/status;
- attempts;
- accepted_at;
- prompt/render hashes;
- exact binding;
- accepted user message/turn;
- optional conversation id.

Rules:
- attempts never increment during identity observation/reconciliation;
- conversation identity may only enrich the same accepted provenance;
- accepted/ambiguous state prohibits blind replay.

## ConversationIdentityObservation

Additive evidence under the existing active hop `wait` object; not a second state machine.

Fields:
- `accepted_identity_started_at`: persisted once at the first exact provisional-identity decision that would otherwise block.
- `accepted_identity_deadline_at`: `min(start + 5.0s, existing wait.deadline_at)`.
- `accepted_identity_finished_at`: optional audit timestamp.
- `accepted_identity_outcome`: optional `canonicalized | blocked_unresolved`.
- `accepted_identity_page_id`: exact page id observed at start.

Entry preconditions:
- persistent workflow;
- RequestLedger record is unchanged accepted SENT;
- durable SendReceipt agrees with ledger;
- accepted user identity is exact;
- exact owned page is present;
- conversation URL/receipt still provisional.

Transitions:
- `NONE -> OBSERVING`: persist start/deadline once.
- `OBSERVING -> CANONICALIZED`: same request/user/page proves canonical UUID/URL; enrich existing receipt.
- `OBSERVING -> BLOCKED_UNRESOLVED`: deadline, page/user loss, or canonical conflict with unchanged receipt.
- receipt divergence at any point exits to ProvenanceIntegrityFailure, not reconciliation.

Restart rule:
- restart/Resume/remount reuses persisted timing; no new interval.

Deadline rule:
- if existing wait deadline is already reached at first exact provisional decision, do not create a fresh five-second budget.

## AcceptedSendReconciliation

Existing owner: `CDPAWorker._route_unresolved_accepted_to_plan`.

Eligibility:
- Team task;
- task status BLOCKED;
- block code `accepted_conversation_identity_unresolved`;
- active hop still waiting;
- accepted user identity exists;
- immutable receipt exactly matches accepted SENT ledger;
- explicit control origin=operator and action=Route PLAN.

Effect:
- old accepted hop becomes abandoned/reconciled evidence;
- exactly one PLAN child of kind `accepted_send_reconciliation` is created;
- old prompt is never replayed;
- reconciliation child uses durable evidence/reports only.

## ProvenanceIntegrityFailure

Meaning: immutable hop-versus-ledger receipt divergence excluding legal one-way conversation-id enrichment.

Rules:
- existing worker block owner sets non-retryable `accepted_receipt_provenance_divergence`;
- no backend read, DOM scan, reopen, rebind, Send, or accepted-send reconciliation;
- preserve both records and attempts;
- Resume/Retry/Restart role/New chat/Route PLAN are illegal while divergence persists;
- explicit operator Stop is the only legal in-task exit; separately authorized replacement may follow from STOPPED while preserving both records as forensic evidence.

## AdmissionCorruptionIncident

Existing state owners: worker block/stop controls plus TaskStore replacement graph.

Flow:
1. `RUNNING/WAITING accepted malformed task -> BLOCKED` with an admission-integrity incident id/code; no replay.
2. `BLOCKED -> STOPPED` only through explicit operator Stop.
3. `STOPPED -> replacement creation` through `replace_task_and_rewire`.
4. Original remains immutable history.

Replacement authoritative fields:
- corrected `canonical_task_text`;
- corrected `canonical_goal` when applicable;
- original `workflow_mode`;
- original workflow-agent/role snapshot;
- validated endpoint/required-endpoint/conversation/context policy;
- dependencies;
- `replaces_task_id`;
- `replacement_incident_id`.

Retained prior reports/incidents are context metadata/handoff only, not executable task text.

## TerminalResponseEvidence

Fields/inputs owned by existing response controller:
- accepted user receipt identity;
- bound assistant message/turn identity;
- assistant fingerprint/text;
- route validation result;
- stability timestamp/samples;
- current DOM `stop_visible`;
- current DOM `response_activity_turn_id`.

Terminal rule:
- candidate is exact-bound;
- candidate is stable;
- route validation succeeds;
- `stop_visible=false`;
- current response-activity turn is absent or differs from the bound turn.

Completion action controls are optional corroboration only.

## ListenFreshnessEvidence

Existing owner: PageObserver/RoleSignals.

Fields:
- `observer_instance_id`: unique id for one PageObserver lifetime; changes on observer recreation/worker restart.
- `observer_epoch`: existing page/conversation epoch within that instance.
- `signal_seq`: monotonic integer incremented at every accepted network signal/status transition in the current epoch.
- `observed_at`: UTC observation timestamp.
- `conversation_id`, page identity, optional bound response/message identity.
- hop-wait consumer watermark: last consumed `observer_instance_id/observer_epoch/signal_seq` plus the purpose for which it was consumed (for example timeout-progress rearm).

Rules:
- LISTEN never overrides TerminalResponseEvidence;
- scope must match current page, conversation, and bound response;
- a nonterminal status is usable only while `now-observed_at <= 5.0s`;
- the same sequence may be observed repeatedly but may not extend progress/deadlines more than once;
- timeout/progress rearm requires a sequence strictly newer than the watermark used for that purpose;
- observer recreation/worker restart invalidates all prior-instance LISTEN evidence until a fresh current-scope signal arrives.

## GoverningSemanticPayload

Caller-authored bounded semantics persisted under the existing task/goal owner for new Team work and every Goal revision. This entity stores payload semantics only; prompt framing is deterministic and external to the payload/hash.

Fields:
- `governing_objective`: concise non-empty authoritative cross-hop objective.
- `governing_directives`: explicitly supplied ordered list of authoritative cross-hop directives; list may be empty only by explicit caller assertion.
- structured facts derived from the task owner at render time: repository, workflow mode, home/required endpoint, conversation/context policy.
- target-role `continuation_contract`: canonical non-empty text <=2048 characters and the complete authoritative cross-hop role contract. Custom-agent authors must include every role constraint intended to survive later role turns; the custom `system_prompt` is first-turn context only and is never a fallback. Task workflow-agent snapshot is authoritative when the field exists; built-in system roles alone may fall back to repository-owned complete compact continuation assets; legacy custom snapshots have no fallback.

Rules:
- objective/directives are caller-authored/canonicalized and form the complete authoritative cross-hop task contract; they are never summarized or inferred by a model;
- Team create/change-goal without both governing fields rejects before mutation with `governing_contract_required`;
- persisted pre-feature Team tasks lacking this authoritative payload block `governing_contract_unavailable` before the next substantive Send and recover only through explicit Stop followed by normal new Team task creation with caller-authored governing fields;
- in-band governing content uses exact wire framing: `GOVERNING_SEMANTIC_SOURCE NNNNN\n<payload1>\nROLE_CONTINUATION_CONTRACT NNNNN\n<payload2>\nGOVERNING_EXECUTION_FACTS_REF NNNNN\n<payload3>`. Each NNNNN is a zero-padded five-digit Unicode-code-point payload length. Payload maxima are 4096/2048/4096; fixed framing is exactly 103 ASCII characters; total serialized governing block max is 10,343 code points; no trailing newline after payload3;
- custom workflow-agent snapshots without an explicit compact continuation contract have no implicit fallback;
- legacy tasks missing a governing semantic payload block `governing_contract_unavailable` before the next substantive hop.

## WorkflowAgentContinuationContract

Existing owner: workflow-agent definition/catalog/snapshot.

Schema extension:
- `continuation_contract`: canonical non-empty string, maximum 2048 characters, and the complete authoritative cross-hop role contract. Custom-agent authors own completeness; the full custom `system_prompt` is first-turn-only context and is not consulted as a later-hop fallback.
- New/updated custom workflow agents MUST supply it through the same dashboard/API -> command -> TaskStore -> WorkflowAgentCatalog path that already owns `name/system_prompt`.
- Built-in system roles load compact repository-owned continuation assets from the existing CDPA default-prompt owner.
- Task workflow-agent snapshots persist the continuation contract and are immutable for that task; later catalog edits/deletes do not rewrite the snapshot.
- Legacy built-in task snapshots missing the field may resolve the repository built-in default.
- Legacy custom snapshots missing the field have no implicit fallback and block `governing_contract_unavailable` before the next substantive Send. Recovery is explicit Stop followed by normal creation of a new Team task after the custom catalog definition has been updated; replacement is not legal for this migration because replacement preserves the old task snapshot.

## GoverningContractRef

Integrity binding for the exact in-band capsule.

Fields:
- `task_id`, `hop_id`, `manifest_path`, `target_role`;
- applicable `goal_revision_id`/index and `effective_goal_sha256`;
- `semantic_sha256`;
- `continuation_sha256`.

`manifest_path` is the exact absolute canonical string already persisted by the task manifest (`str(Path(state["manifest_path"]).expanduser().resolve())`), must resolve inside the configured plans root, and must identify the actual manifest being executed.

The facts/ref payload is compact canonical JSON with fixed key order `task_id`,`hop_id`,`manifest_path`,`repository`,`workflow_mode`,`home_endpoint`,`required_endpoint`,`conversation_mode`,`context_policy`,`goal_revision`,`effective_goal_sha256`,`semantic_sha256`,`target_role`,`continuation_sha256`, ensure_ascii=false, compact separators, no trailing newline; <=4096 Unicode code points.

Resolution:
- effective Goal/revision uses existing `task_goal_for_hop` ownership;
- semantic payload uses the matching original/revision record;
- manifest path must equal the task's persisted canonical absolute manifest path and actual execution manifest;
- role continuation contract uses the deterministic source rule above;
- CDPA verifies all hashes/source identity before Send, then delivers the verified semantic payload/reference in-band.

Special paths:
- route-repair/report-repair are non-substantive and do not receive the substantive semantic payload;
- accepted-send reconciliation is substantive and carries the applicable semantic payload/ref;
- Temporary generation recovery stores `recovered_parent_kind` plus the parent lock/validation fields required by the existing prompt owner. A normal substantive parent recovers with governing payloads; route/report-repair parents recover with the same locked non-substantive repair contract; accepted-send reconciliation preserves its substantive no-replay lineage contract;
- every recovery stays on the same workflow home endpoint and preserves the prior accepted request unchanged;
- accepted already-sent prompt is never rewritten after later source drift.

## DependencyGate

Fields:
- child task id;
- parent ids;
- parent statuses;
- parent non-sensitive block codes;
- waiting kind;
- slot consumption=0.

Rules:
- child references parent cause; it does not own/copy the parent blocker;
- DONE releases;
- unsuccessful terminal/missing parent remains explicit.

## GoalRevision

A revision applies only from its declared future hop onward and records external command provenance.

Rules:
- never repairs already accepted prompt;
- GoverningContractRef binds the effective revision for each future hop;
- earlier hop references remain immutable evidence.
