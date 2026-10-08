# Quickstart / Acceptance Matrix: CDPA Blocker Recovery Contracts

This file describes future deterministic acceptance. It does not authorize implementation in the current spec-only cycle.

## Scenario A — Windows multiline admission

Input contains:
- leading/trailing blank lines;
- CRLF and lone CR;
- Unicode;
- quotes;
- a literal line beginning `--config`;
- separate real CLI options after the task transport.

Expected:
- canonical task text changes only CR variants to LF;
- literal option-like task lines remain task text;
- real parser options remain metadata;
- submitted team, Temporary/conversation mode, repository, selected endpoint, and required endpoint (when supplied) exactly match the admitted manifest rather than silently falling back to defaults;
- strict UTF-8 or metadata-integrity failure rejects before task/browser work;
- Team and Solo use the same canonicalizer.

## Scenario B — Verified accepted-identity race

Replay durable timeline:
- Send complete around 00:39:14.585Z;
- accepted user + provisional page around 00:39:15.878Z;
- first exact unresolved decision around 00:39:49.846Z;
- same page canonical URL around 00:39:49.938Z.

Expected:
- no settle timer starts at Send/accepted time;
- at ~00:39:49.846Z persist one start and deadline ≤ start+5s and ≤ original wait deadline;
- same page canonicalizes ~92ms later;
- same request receipt gains canonical identity;
- attempts unchanged;
- no reload/goto/reopen/Send;
- original response deadline is unchanged.

Restart variant:
- restart after settle start;
- persisted start/deadline reused exactly;
- no second 5-second budget.

## Scenario C — Unresolved identity and explicit reconciliation

Given unchanged accepted receipt but page/user loss or settle expiry:
- task becomes BLOCKED `accepted_conversation_identity_unresolved`;
- no automatic child;
- browser remains unmutated.

Non-operator Route PLAN:
- rejected.

Explicit operator Route PLAN:
- re-proves exact unchanged SENT record;
- old hop becomes non-replayable abandoned evidence;
- exactly one PLAN `accepted_send_reconciliation` child is created.

Receipt-divergence variant:
- fails before backend/DOM/reopen;
- no accepted-send reconciliation;
- attempts unchanged.

## Scenario D — Post-acceptance malformed admission

Given accepted malformed task/goal:
1. record admission-integrity incident;
2. BLOCK through existing worker owner;
3. do not replay/rewrite;
4. operator Stop -> STOPPED;
5. call existing replacement owner.

Expected replacement:
- corrected canonical task/goal is executable authority;
- known-corrupt target goal is not executable context;
- Team stays Team; Solo stays Solo;
- roles/workflow agents, endpoint/context policy, dependencies preserved;
- prior reports/incident references are non-executable handoff/context;
- replacement linkage and rewiring remain idempotent.

## Scenario E — Legal endpoint placement

Pinned task:
- any attempted later home-endpoint mutation fails.

Unpinned pristine Team:
- one pre-Send placement succeeds;
- endpoint becomes pinned;
- later mutation fails.

Temporary:
- role transitions/Resume/Retry/New chat/restart never spill to 9222.

## Scenario F — Stable DOM route with delayed buttons and stale LISTEN

Given:
- exact accepted user/assistant binding;
- assistant text is stable for configured interval;
- route JSON/report validates;
- `stop_visible=false`;
- bound turn is not current `response_activity_turn_id`;
- copy/feedback/regenerate controls absent or render later;
- LISTEN cache says `IS_STREAMING` from same/older observer history.

Expected:
- ACCEPT through DOM response owner;
- no wait for action buttons;
- LISTEN cannot veto;
- no timeout reload;
- no one-shot stream re-arm because terminal result already exists;
- no `state_repair`/route repair.

Negative variants:
- bound turn still active -> WAIT;
- unstable/incomplete output -> WAIT;
- wrong bound user/assistant -> reject/block per existing exact-binding contract;
- current Retry/error -> existing recovery path.

## Scenario G — PageObserver freshness

Within one page observer:
- observer instance id is unique for that observer lifetime;
- signal sequence increases on accepted network signal/status transitions;
- epoch changes on real conversation/document lifecycle transitions;
- hop wait records the last sequence consumed for progress/rearm;
- repeated same sequence and signals older than 5 seconds are diagnostic only;
- worker/observer restart invalidates the prior observer instance until a fresh current-scope signal arrives.

Expected:
- terminal DOM is never overridden by old LISTEN;
- LISTEN terminal content can supplement only already-bound assistant identity;
- no second route/request identity is created from network evidence.

## Scenario H — PLAN → REVIEW → PLAN governing contract

Ingress/migration setup:
- new Team create/Goal-change requires `governing_objective` + explicitly supplied ordered `governing_directives`;
- their canonical JSON is the complete authoritative cross-hop task contract and must be <=4096 code points;
- missing/invalid/oversize governing fields reject before mutation with `governing_contract_required`;
- pre-feature persisted Team tasks without an authoritative governing payload block before the next substantive Send and recover only through Stop -> normal new Team task creation.

Turn 1:
- full Goal/role behavior remains per current policy.

Later REVIEW and PLAN re-entry:
- prompt does not duplicate the full Goal at all;
- prompt contains the exact length-prefixed governing wire format; semantic payload <=4096, target-role continuation <=2048, canonical facts/ref <=4096; framing exactly 103 ASCII characters; total governing block <=10,343 code points;
- ref task/hop/manifest is exact; `manifest_path` equals the persisted canonical absolute task manifest path, resolves under the configured plans root, and mismatch/noncanonical/outside-root variants fail before Send;
- effective Goal hash matches `task_goal_for_hop` for that hop;
- role contract hash matches the target role workflow definition;
- applicable goal revision identity is exact;
- CDPA verifies ref before Send;
- prompt says governing Goal/role contract must be resolved before substantive work;
- handoff alone is not authoritative.

Canonicalization checks:
- governing objective/directives serialize as compact JSON with fixed key order `objective`,`directives`, ensure_ascii=false, no spaces/trailing newline;
- missing objective or missing directives field is invalid; an explicitly supplied empty directives list is valid only as the caller's assertion that no additional cross-hop directives exist;
- semantic SHA-256 hashes UTF-8 payload bytes only, excluding headers/length prefixes;
- embedded CR/LF, quotes, backslashes, header-like text, Unicode, reordered input keys, and directive order are deterministic;
- simultaneous payload maxima 4096/2048/4096 serialize to exactly 10,343 code points.

Variants:
- future Change Goal updates only future-hop Goal/capsule;
- required create/change-goal governing fields round-trip through CLI/API/direct store seams;
- new/updated custom workflow agent supplies a bounded `continuation_contract` that is explicitly the complete cross-hop role contract; a mandatory custom role directive survives later re-entry/recovery, while full custom `system_prompt` remains first-turn-only; task snapshot freezes the continuation contract;
- built-in system role uses repository compact default continuation asset;
- legacy custom snapshot without continuation contract blocks `governing_contract_unavailable`; after Stop, recovery is normal creation of a new Team task using the updated custom catalog definition. Replacement is not used because it preserves the old snapshot;
- source/hash mismatch before Send fails closed;
- Restart role/New chat/reconciliation child retains a valid ref/capsule;
- accepted Temporary document/page loss creates at most one same-home-endpoint recovery child with the accepted parent's prompt-authority class preserved; normal substantive recovery receives verified governing semantics, while route/report repair recovery retains its lock and receives no substantive governing payload;
- accepted-send reconciliation recovery retains its no-replay/reconciliation lineage restrictions;
- the recovery snapshot, prior chat, or an absent prior report cannot substitute for governing semantics or repair authority;
- route/report repair stays minimal and does not reintroduce Goal/capsule.

## Scenario H2 — Receipt divergence manual exit

Given immutable hop-versus-ledger receipt divergence:
- block code is exactly `accepted_receipt_provenance_divergence` and retryable=false;
- backend/DOM/reopen/Send are untouched; attempts remain unchanged;
- Resume, Retry, Restart role, New chat, and Route PLAN are rejected while divergence persists;
- explicit operator Stop is the only legal in-task exit;
- any later replacement is separately authorized and preserves both forensic receipts.

## Scenario I — Dependency secondary stall

Parent BLOCKED:
- child remains WAITING;
- slot use=0;
- projection includes parent task id/status/block code;
- child does not copy parent blocker as its own root cause.

Parent DONE:
- gate releases without endpoint rebinding.

Parent STOPPED/missing:
- explicit dependency terminal/missing reason remains visible.

## Scenario J — Cloudflare/session correlation

If challenge/session/browser transitions are present:
- record them as environment evidence;
- do not infer cause without boundary proof;
- do not add anti-bot bypass or cache-clearing mutation to this feature.

## Spec-only gate

Before any future implementation:
- fresh independent REVIEW finds no CRITICAL/HIGH contract defect;
- reviewer checks CHK019 and CHK020;
- all implementation tasks T001–T067 remain unchecked until implementation is separately authorized.
