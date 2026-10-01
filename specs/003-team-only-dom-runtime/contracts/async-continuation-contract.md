# Contract: AsyncContinuation

## Phase 1.5 scope

Phase 1.5 implements the minimal script/command timeout callback path for existing TeamTask role conversations: confirmed spawn, route WAIT, callback, bounded result MaterialRefs, and same-TeamTask/same-role wake. It does not implement Direct Chat, adopted URL, dynamic endpoint UI, or generalized global writer lease wake-back; those remain Gate C.


## Purpose

Allow a role turn to launch long external work, return `WAIT`, and stop the ChatGPT Web response without polling until the external provider finishes.

## Lifecycle

1. Runtime registers AsyncContinuation with task id, role key, route contract id, parent conversation binding, `wake_policy=same_role`, optional `failure_policy`, and idempotency keys.
2. Runtime spawns external provider/script and records confirmed provider job identity.
3. RouteContract may expose `WAIT` for that turn.
4. Role returns `WAIT` with continuation_id.
5. Task becomes `WAITING` with `waiting_code=async_callback`.
6. External job calls callback API/CLI with result refs.
7. CDPA records ProviderResult/MaterialRefs and schedules wake for the same TeamTask/role or configured wake target.
8. Wake uses the parent conversation binding/adopted URL and normal writer-lease/SendAttempt reconciliation.

## Idempotency

- Spawn confirmation is idempotent by continuation_id + spawn_idempotency_key.
- Callback completion is idempotent by continuation_id + callback_idempotency_key.
- Duplicate callback does not create a second wake Send.
- Conflicting callback payload for the same key fails closed with `ASYNC_CALLBACK_CONFLICT`.

## Wake-back writer reservation

The parent canonical writer key is held in formal ConversationWriterLease state `reserved_async_wait` while waiting. On successful callback, wake transitions `reserved_async_wait -> owned` for the same role, captures a fresh Send baseline, and then sends. Cancel/timeout/failure must transition `reserved_async_wait` durably to released or quarantined/sealed evidence before any other writer may proceed.

## WAIT versus PAUSE

`WAIT` means runtime-owned asynchronous wait. It is resumable by callback and visible in dashboard as async waiting. `PAUSE` means operator intervention is required and must not be used for normal async provider completion.

## Wake-back safety

Wake-back must not silently open a new chat. It verifies the parent task state, route_contract_id, conversation identity, endpoint/profile binding, writer lease, and dependency readiness. Failure blocks with a specific code such as `ASYNC_WAKE_IDENTITY_MISMATCH`, `ASYNC_CALLBACK_FAILED`, or dependency waiting.
