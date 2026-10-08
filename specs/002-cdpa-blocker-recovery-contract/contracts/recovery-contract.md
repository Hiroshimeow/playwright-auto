# Recovery Contract: CDPA Blocker Classes

## 1. Exactly-once accepted boundary

### Preconditions
A request has durable request id and rendered-prompt evidence before Send.

### Accepted transition
When accepted user identity is proven:
- RequestLedger becomes SENT with accepted provenance;
- attempts are fixed for that Send;
- user message/turn and exact page binding become ownership evidence;
- no ambiguous/accepted recovery may replay Send.

### Illegal transitions
- accepted -> refill/re-Send;
- accepted -> guessed conversation reopen;
- accepted -> endpoint migration;
- accepted -> retroactive Change Goal.

## 2. Persistent provisional conversation identity

### Entry

Only if all are true:
- persistent workflow;
- exact accepted SENT ledger/receipt agree;
- accepted user identity is present;
- exact page still owns the request;
- canonical conversation id remains unresolved.

At the **first** point where current logic would otherwise emit `accepted_conversation_identity_unresolved`:
- if existing response deadline is already reached, go directly to `BLOCKED_UNRESOLVED`;
- otherwise persist `accepted_identity_started_at` once;
- persist `accepted_identity_deadline_at=min(start+5.0s, existing_response_deadline)`;
- remain RUNNING/read-only.

The accepted Send timestamp is not the settle start.

### Observation

Allowed:
- read exact page URL/identity;
- read exact accepted user identity;
- consume already-captured same-request identity evidence.

Forbidden:
- Send;
- composer refill;
- upload replay;
- reload/goto;
- reopen;
- New chat;
- endpoint change;
- response-deadline extension.

### Success

Before deadline, same request/user/page proves canonical UUID/URL:
- enrich the same receipt/ledger through the existing enrichment owner;
- attempts unchanged;
- persist outcome=canonicalized;
- continue original response wait with original deadline.

### Unchanged-receipt failure

On deadline, page/user loss, or canonical conflict:
- task becomes BLOCKED;
- block code remains `accepted_conversation_identity_unresolved`;
- current hop remains non-replayable evidence;
- no reconciliation child is automatic.

Only explicit operator Route PLAN may invoke existing accepted-send reconciliation. It must re-prove unchanged SENT ledger/receipt before abandoning the old hop and creating the PLAN reconciliation child.

### Receipt divergence

Immutable receipt mismatch is not this failure path:
- existing worker block owner sets non-retryable `accepted_receipt_provenance_divergence` before backend/DOM/reopen;
- preserve both records/attempts and never replay;
- Resume/Retry/Restart role/New chat/Route PLAN are illegal while divergence persists;
- explicit operator Stop is the only legal in-task exit; separately authorized replacement may follow from STOPPED while retaining both forensic records.

## 3. Temporary workflow lifecycle

Temporary means Fresh:
- no saved-conversation reopen identity;
- no automatic F5/reload;
- no persistent identity settle;
- no endpoint spill;
- page/document loss follows existing fail-closed/new-generation policy only where the request boundary permits it.

## 4. Workflow endpoint placement

Admission provenance is part of this contract: explicit browser endpoint, required endpoint, conversation mode/Temporary intent, team, repository, context policy, and bootstrap presence remain distinguishable from omitted/defaulted values even when final values coincide with defaults.

### Explicitly pinned
Home endpoint is immutable for workflow lifetime.

### Intentionally unpinned
One atomic initial placement is legal only if:
- Team workflow;
- `endpoint_pinned=false`;
- active hop is pristine pre-Send;
- no receipt/message identity;
- active role has no page binding.

Placement ends with `endpoint_pinned=true`. The placement owner may change only home endpoint, false->true pin, implicit mode derived from the selected endpoint when mode was omitted, omitted/no-bootstrap context policy to Fresh, and matching active role/hop endpoint binding. Explicit mode/context/bootstrap/required endpoint and request identity are immutable; incompatible placement waits/fails closed.

### Never implied
Resume, Retry, Restart role, New chat, recovery, source drift, role transition, dependency release, or capacity pressure never make `browser_endpoint_id` mutable.

## 5. Lossless admission

Canonicalizer:
- strict UTF-8 file/stdin;
- CRLF/CR -> LF;
- preserve all other Unicode + whitespace;
- reject canonical-whitespace-only;
- no Unicode normalization or BOM stripping.

For multiline Team create/Goal-change, use one lossless structured file/stdin/API-equivalent carrier for the whole caller-authored semantic envelope: task/new Goal, `governing_objective`, ordered `governing_directives`, and explicit control provenance. Windows `.cmd %*` multiline argv is not an allowed carrier for these fields.

Before parser/store mutation, compute the intended canonical task/goal hashes and governing semantic payload/hash from that carrier. Before browser mutation, persisted values must match those intended values exactly. Any transport or equality mismatch fails closed.

Acceptance must exercise multiline governing objective/directives with quotes, Unicode, option/header-like lines, and directive ordering through the Windows launcher path.

## 6. Accepted admission corruption and replacement

If admission corruption is discovered after accepted/ambiguous Send:

1. preserve the accepted prompt, request, receipt, reports, and attempts;
2. record admission-integrity incident;
3. block through the existing worker block owner;
4. require explicit operator Stop;
5. only after STOPPED eligibility, invoke existing `replace_task_and_rewire`;
6. replacement executable task/goal is the trusted corrected canonical payload;
7. known-corrupt target effective goal is never embedded in executable replacement instructions;
8. prior reports/incidents are context/handoff metadata only;
9. preserve target Team/Solo mode, workflow agents/roles, placement/context policy, and dependencies;
10. replacement linkage/child rewiring stays atomic/idempotent under the existing owner.

Change Goal is not a repair step for already accepted corruption.

## 7. Terminal response arbitration

Terminal acceptance has one authority order:

1. exact accepted user -> bound assistant identity;
2. current DOM content and generation state;
3. stable valid route;
4. LISTEN/network diagnostics as supplemental evidence only.

A bound DOM candidate is terminal when:
- exact-bound to accepted receipt;
- unchanged for configured response stability;
- route validation succeeds;
- `stop_visible=false`;
- bound turn is not the current DOM response-activity turn.

Copy/feedback/regenerate controls are optional.

When terminal DOM is true:
- accept immediately through the existing response owner;
- stale `IS_STREAMING` cannot veto;
- do not timeout-reload;
- do not query/re-arm stream status;
- do not queue `state_repair`/route repair for absence of completion controls.

When terminal DOM is false:
- existing bounded persistent recovery may apply;
- Temporary no-reload remains absolute.

LISTEN freshness:
- PageObserver exposes unique observer-instance id, existing epoch, monotonic `signal_seq`, and observation time;
- hop wait/controller evidence stores the last-consumed watermark; same sequence cannot re-arm progress twice;
- nonterminal LISTEN older than 5 seconds is diagnostic only; observer restart invalidates prior-instance evidence until a fresh current-scope signal;
- LISTEN terminal text may supplement only the already-bound assistant identity;
- no transport source can bind a different request by itself.

Snapshot-cache rule:
- every signal required for terminality must be present in the sparse probe or force full-snapshot invalidation;
- late action buttons need not invalidate terminal authority because actions are not a terminal prerequisite.

## 8. Governing contract continuity

### Turn 1
Existing full Goal/role-constructor behavior remains legal.

### Later substantive Team hops
Team create and Team Goal-change require caller-authored `governing_objective` plus an explicitly supplied ordered `governing_directives` list. Their bounded canonical JSON is the complete authoritative cross-hop task contract; missing/invalid/oversize fields fail before mutation, and pre-feature Team tasks without that payload fail closed before the next substantive Send.

Each role's bounded `continuation_contract` is the complete authoritative cross-hop role contract. Custom-agent authors must place every role constraint intended to survive later substantive turns into it. A custom full `system_prompt` is turn-1 context only and is never a later-hop fallback. Built-in roles use repository-owned complete compact continuation contracts; legacy custom snapshots without one fail closed before a later substantive Send.

Prompt uses exact wire grammar `GOVERNING_SEMANTIC_SOURCE NNNNN\n<payload1>\nROLE_CONTINUATION_CONTRACT NNNNN\n<payload2>\nGOVERNING_EXECUTION_FACTS_REF NNNNN\n<payload3>`. NNNNN is zero-padded Unicode-code-point payload length. Payload caps are 4096/2048/4096; framing is exactly 103 ASCII characters; total <=10,343 code points. The semantic payload is required caller-authored canonical compact JSON with fixed `objective`,`directives` key order and is the complete authoritative cross-hop task contract. Freeform task/Goal text is not repeated or used as a fallback. Payload hashes cover UTF-8 payload bytes only, never framing.

The ref binds:
- task id;
- hop id;
- exact canonical absolute `manifest_path` of the actual task manifest;
- effective Goal hash/revision;
- governing semantic payload hash;
- target role;
- authoritative continuation-contract hash.

Before Send, CDPA re-resolves durable sources, proves `manifest_path` equals the persisted canonical absolute task manifest inside plans root and the actual manifest being executed, then verifies hashes. Missing/mismatch fails closed before browser mutation. CDPA does not infer governing policy from freeform task/Goal text; caller-declared governing fields are the semantic authority.

The in-band governing sections already carry the verified semantic payload + role continuation semantics; the role does not need to resolve external files/tools. Handoff/report state is supplementary context only.

The substantive governing contract applies to:
- PLAN -> REVIEW -> PLAN;
- later substantive role re-entry;
- Restart role when it creates substantive work;
- New chat when it creates substantive work;
- legal pre-boundary substantive relocation/rebuild;
- accepted-send reconciliation, subject to its existing no-replay/lineage restrictions.

Temporary generation recovery inherits the accepted parent hop's prompt-authority class; the child kind alone never grants authority:
- normal substantive parent -> verify and deliver governing semantic/role/facts payloads;
- `route_repair` parent -> preserve repair validation/lock metadata and render the same locked route-repair prompt with no substantive governing payload;
- `report_repair` parent -> preserve locked route/handoff/validation metadata and render the same locked report-repair prompt with no substantive governing payload;
- accepted-send reconciliation parent -> preserve reconciliation/no-replay lineage and its substantive governing contract.

Recovery metadata must preserve the parent kind plus required lock/lineage fields. Missing or mismatched authority metadata fails closed before Send. Every recovery stays on the same workflow home endpoint, preserves Temporary/Fresh policy, and leaves the prior accepted request immutable/non-replayable. The recovery snapshot is provenance only.

Goal revision affects only hops at/after its applicability boundary.

Acceptance must prove one mandatory custom-role directive survives later role re-entry and normal Temporary recovery without replaying the full custom `system_prompt`.

### Non-substantive repair
Route-repair/report-repair prompts remain locked/minimal, including after Temporary generation loss:
- do not reintroduce full Goal or substantive governing payload;
- do not authorize repeating substantive work;
- preserve the existing repair lock/validation fields;
- repair only route/report format from existing durable workspace/report state.

## 9. Dependencies

Dependency WAIT:
- zero browser slot;
- zero browser mutation;
- exact parent status and non-sensitive block code visible;
- parent DONE releases;
- parent unsuccessful terminal/missing stays explicit.

No second dependency machine.

## 10. Observability

Future implementation must make these inspectable without sensitive payload leakage:
- request id/status/attempts/accepted timestamp;
- exact accepted user/page identity identifiers;
- accepted-identity start/deadline/outcome;
- workflow home endpoint + pin state;
- role execution endpoint/page;
- PageObserver epoch/sequence/time;
- bound assistant identity;
- terminal-decision source/reason;
- governing-contract ref hashes/resolution status;
- reconciliation operator control provenance;
- dependency parent ids/status/block codes.

Cloudflare/challenge observations remain environment evidence, not a causal state.

## 11. Rollback boundaries

- Ingress transport/canonicalizer may roll back independently if no persisted schema migration is required.
- Identity settle metadata is additive under hop wait; rollback must not rewrite accepted history.
- Terminal arbitration/listen freshness changes must not alter RequestLedger.
- Governing-contract refs are additive prompt/hop metadata; rollback may restore prior compact behavior only before new accepted prompts depend on the new reference contract.
- Endpoint initial-placement support requires no migration of already pinned/legacy tasks.
- Replacement trusted-authority extension may roll back only without rewriting existing replacement/original histories.
- Dependency projection is additive and does not rewrite graph history.
