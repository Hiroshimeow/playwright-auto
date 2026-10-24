# Contract: Direct Chat

Direct Chat is a dashboard control surface over ProviderSession/ConversationBinding, not a task model and not a second send implementation. ConversationBinding may have nullable `task_id` for standalone Direct Chat; ProviderSession remains the control owner.

## Send API

`POST /api/sessions/{session_id}/messages`

A request contains idempotency key, text, optional MaterialRefs, and optional bound task context.

## Exactly-once boundary

Direct Chat reuses the existing command-ledger send owner. The logical SendAttempt projection supports:

`intent_recorded -> submitted -> accepted | unconfirmed -> reconciled_accepted | reconciled_absent`; ambiguous intent_recorded/submitted recovers through reconciliation/unconfirmed before retry

Before Send, capture baseline last-user-message id and user-turn count. Accepted means a new user turn after that baseline exactly matches the sent text. `unconfirmed` is reconciliation-only and never resends. Only proven `reconciled_absent` may create a new attempt id.

## Single writer

Writer ownership is global by writer identity key, not per binding. Every ordinary TeamTask/adopted/direct-chat browser writer uses the same ConversationWriterLease owner. A canonical key or provisional physical-page key has one owner at one `lease_epoch`; during async WAIT it may also be held in formal state `reserved_async_wait`, which blocks other writers but is not an active browser/page lease. Successful continuation wake transitions `reserved_async_wait -> owned`; cancellation/timeout/failure transitions it to released or quarantined/sealed evidence. Direct Chat is blocked while a TeamTask turn owns the lease. Lease takeover increments `lease_epoch`. Send-intent creation must atomically verify current owner+epoch before any composer mutation, so a stale pre-intent owner fails with `WRITER_LEASE_LOST`. Takeover remains forbidden while the existing logical browser turn is in flight, including non-terminal SendAttempt, assistant generation, or route-repair activity. If reconciliation cannot resolve, operator stop/abandon may release runtime process resources only after durable seal evidence is written. It never authorizes replay. If the unresolved attempt is associated with a canonical conversation, that canonical writer key remains quarantined/non-reusable for runtime writes; a later runtime session MUST NOT re-adopt the same conversation as a fresh writable binding.

Lease uniqueness is not scoped only to endpoint_id or explicit Direct Chat bindings. Under Gate C, every browser-backed TeamTask role turn that can Send MUST hold/project a binding into the same writer namespace before Send. If canonical conversation identity is knowable before Send, canonical `(provider_id, canonical_conversation_id)` lease is mandatory before Send intent. A provisional `(provider_id, physical_browser_id, page_binding_id)` lease is allowed only after positive verification of a genuine provider new-conversation surface; unresolved identity never qualifies. When canonical id appears, claim canonical key atomically while retaining provisional ownership until the current SendAttempt is terminal. Collision quarantines the canonical key, so both the key winner and losers are blocked from new Sends until conflicting attempts are terminal; losers reconcile only and then seal. After quarantine clears, the surviving owner captures a fresh baseline before its next Send. Unreconciled operator-abandon leaves the canonical key non-reusable. Bind-time endpoint_id, normalized cdp_url, and physical_browser_id are revalidated before Send; drift blocks and requires rebind.

## Browser stream

`GET /api/sessions/{session_id}/dom-stream`

For ChatGPT Web/browser-backed providers, stream nodes come from fresh DOM projection only: user_message, assistant_text, visible_thinking, tool_activity, progress, error_banner, terminal.

Direct Chat messages are never parsed as TeamTask route JSON unless they are explicitly executing a bound TeamTask role turn.

## Stream cursor behavior

- During an active ProviderSession, published DOM events use a monotonic session-local `event_id` and stable per-node keys when the provider exposes stable DOM identity.
- This feature does not add a second durable event journal. After worker/session restart, the server may emit a reset marker followed by a fresh full DOM projection; clients must not assume a pre-restart cursor can be resumed losslessly.

## Turn and endpoint-binding lifecycle

A browser writer lease is held through the complete logical message/role turn: Send intent, accepted/unconfirmed reconciliation, assistant terminal DOM state, and any route-repair Sends, until a valid route/turn commit, block, or task terminal state. Send acceptance alone never releases the lease.

When a role routes away after a valid commit, its binding may become `idle` and releases the writer lease but remains a live endpoint reference for route-back. Re-entry must reacquire the canonical writer key; if another runtime writer owns it, the task blocks with retryable `WRITER_LEASE_BUSY`. Temporary quarantine blocks with retryable `WRITER_LEASE_QUARANTINED`; permanent quarantine after unresolved abandon uses the same code but non-retryable. Stale epoch continuation blocks with `WRITER_LEASE_LOST`.

For task-bound bindings there is no ad-hoc role-unbind during normal routing: idle bindings remain live references so adopted URL route-back remains valid. Endpoint removal may stay draining until the task reaches DONE/STOPPED (automatic release) or the operator stops/replaces that task. Standalone Direct Chat may explicitly close its idle session. `released|sealed` bindings no longer block endpoint finalization.
