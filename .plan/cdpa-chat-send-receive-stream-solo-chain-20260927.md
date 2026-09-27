# CDPA Feature Plan â€” Exact Send / Bound Response / Scoped Stream

Date: 2026-09-27
Repository: `E:\git-project\playwright-auto-multicdp`
Reviewed starting HEAD: `940eb6b916dd94ad7d96362575d1a0c7cbef5b9d`
Reference repo: `E:\git-project\codex-chatgpt-web-reference` @ `2d73f626290a5062825bb595dabb89aaf88d16c5`
Execution host: g6 only. Browser endpoint: 9202. Do not use g8. Do not push.

This is a new feature slice. The plan/review checkpoint must be committed before implementation begins. Preserve all unrelated dirty/untracked work and stage only task-owned files.

## Mandatory Solo-chain contract

Báº®T BUá»˜C má»i Solo agent trong chuá»—i nÃ y pháº£i tá»± spawn Solo task role tiáº¿p theo trÆ°á»›c khi káº¿t thÃºc, vÃ  tiáº¿p tá»¥c chuá»—i Solo REVIEW/DEV/TEST cho Ä‘áº¿n khi cÃ´ng viá»‡c thá»±c sá»± hoÃ n thÃ nh vÃ  cÃ³ báº±ng chá»©ng Ä‘á»™c láº­p. KhÃ´ng dÃ¹ng Team Task vÃ  khÃ´ng dá»±a vÃ o route worker Ä‘á»ƒ chuyá»ƒn role.

The chain is sequential. Each agent spawns exactly one next Solo role unless it is the final independent closure REVIEW and all acceptance evidence is complete.

Expected shape:

```
REVIEW(plan) -> DEV -> REVIEW(code/evidence) -> DEV(fix) ... -> TEST(runtime) -> REVIEW(closure)
```

Every child Goal must carry this plan path, previous task/report/commit evidence, current HEAD, endpoint 9202, repository path, no push/no g8, preserve unrelated dirty work, and the mandatory sentence above.

## Product invariant

Role URLs are long-lived. The same `/c/...` conversation can be reused days later and the human may chat directly in it between automation runs.

Therefore:
- old history is context, never current-operation identity;
- every automation Send starts from a fresh logical-turn baseline;
- an accepted or ambiguous Send is never replayed;
- the automation user turn must have exact identity;
- the assistant response is bound once to that operation and later human turns cannot steal it;
- if a human user turn arrives before the operation assistant can be bound, fail explicitly rather than selecting a later assistant;
- reload/remount may change DOM placement but not logical ownership.

## Independent review findings

### Current CDPA defects

1. `ChatGPTPage._wait_send_acceptance()` currently accepts `assistant_message`, `response_activity_turn`, `assistant_activity`, and Stop transition without exact automation-user identity. Those are progress signals, not exact Send identity.
2. `wait_for_response()` defaults to `role_runtime.response.current_assistant()`, which deliberately selects the newest assistant after the newest rendered user. That contradicts this feature's long-lived/manual-interleaving requirement.
3. `all_assistant_turns_for_receipt()` includes assistants after later user turns; it is not an exact Uâ†’A binding primitive.
4. Live transcript projection in `role_runtime.signals` is based on latest probe/LISTEN state and is not tied to one receipt-bound assistant.
5. The existing frontend observer already extracts the exact user message ID from `POST /backend-api/f/conversation`. Reuse that owner; do not add another request-identity subsystem.
6. Existing durable Send handling already fails closed after a possibly-dispatched click. A new `NOT_DISPATCHED/DISPATCHED/ACCEPTED` durable state machine would duplicate the ledger and is rejected.
7. Existing `LEARNING.md` advice that later operator turns should drive result admission to the latest rendered user/assistant chain conflicts with the explicit feature requirement. Update that lesson only after the new behavior is verified.

### Reference mechanisms safe to adopt

From `docs/architecture.md`, `src/chatgpt-session.ts`, and `browser-worker.ts`:
- baseline logical turn identity rather than display index;
- include persistent turn containers/group keys so virtualization/remount does not create false â€œnewâ€ turns;
- exact assistant-turn binding followed by scoped observation of that bound turn;
- fail explicitly on duplicate/missing logical identities or foreign user-turn interference;
- terminal evidence from the bound response DOM: response exists, generation no longer running, usable content exists, completed-turn action exists, then content remains stable for the existing settle interval;
- MutationObserver as a wake mechanism, not a second semantic state machine.

### Reference assumptions that do NOT fit CDPA

Do not copy:
- â€œone new turn since baseline is oursâ€ as a sufficient rule; the human can race a manual Send on the same long-lived role URL;
- retained WebContentsView/launcher lease assumptions that exclude unrelated manual conversation activity;
- MCP tool-batch fences, external-progress completion fencing, Codex-specific trace/Markdown serializers, or the large completion/DOM-health class stack;
- a separate response reader or a second orchestration state machine.

## Smallest safe implementation

### 1. Exact Send confirmation â€” reuse existing ledger and frontend identity

Do not add durable Send states.

Around the one permitted click:
- capture a logical-turn baseline from the existing page inspection path;
- arm/reuse the existing frontend conversation observer;
- capture the exact outgoing automation `user_message_id` from the conversation request;
- treat broad assistant/activity/Stop signals only as wake/diagnostic evidence.

A Send may return an accepted `SendReceipt` only when the operation can be tied to the exact automation user turn:
- preferred: the exact rendered user/message identity matches the observed outgoing `user_message_id`;
- acceptable if proven by the existing frontend conversation response: the same exact request/user identity receives successful conversation acceptance;
- fallback without exact network identity: only one new logical user turn after baseline and its rendered content matches the expected prompt; otherwise fail ambiguous.

If the click may have escaped but exact acceptance cannot be proved, raise the existing recovery/ambiguity path and **do not click again**. False-negative acceptance is allowed; replay is not.

Keep legacy `accepted_via` values readable for old receipts, but stop emitting broad acceptance values for new Sends once tests prove the replacement.

### 2. Bind one assistant to the exact receipt user

Use the existing receipt plus existing durable hop `wait` state; do not create a second state store.

Binding rule:
1. locate the exact receipt user identity;
2. inspect only messages/logical turns after it;
3. the first assistant belonging to that user becomes the bound assistant;
4. if another user turn appears before an assistant is bound, return an explicit interruption/ambiguity result;
5. persist the bound assistant turn/message identity in the existing `wait` state so restart/reload keeps ownership;
6. once bound, later human U/A turns are ignored for this operation.

For current and new ChatGPT renderers, prefer stable logical identity:
- legacy `data-turn-id`;
- grouped renderer `data-turn-key` projected as a stable user/assistant logical pair;
- persistent `data-turn-id-container` where needed to prevent virtualized-history remounts from looking new.

Do not use `current_assistant()` for an accepted operation after binding exists.

### 3. Scope the existing wait probe to the bound assistant

Do **not** add a new `ResponseTurnSnapshot` subsystem.

Extend the existing sparse/full wait observation only as much as needed to:
- resolve/read the bound assistant turn;
- return its visible text/fingerprint;
- expose whether the bound response is currently generating;
- expose one completed-turn action boolean if the current `MessageSnapshot.actions` cannot already prove it;
- detect duplicate/missing exact turn structure.

MutationObserver remains the wake source. Existing bounded fallback observation remains the resilience path. No new 200â€“300 ms semantic poll loop.

### 4. Streaming and terminal authority use the same bound turn

For the bound assistant:
- each rendered text change emits an upsert under one stable logical assistant key;
- `complete=false` while the bound turn is generating/incomplete;
- later unrelated conversation turns never change that key or source text;
- LISTEN/CDP may update the operation only when its message/turn identity correlates to the same bound assistant; uncorrelated LISTEN remains diagnostics only.

Terminal candidate requires the same small DOM contract:
- bound response exists;
- usable content exists;
- bound/current generation UI is not active;
- completed-turn action is present;
- content fingerprint remains stable for the existing configured settle interval/samples.

A stale global/LISTEN `IS_STREAMING` must not veto a DOM-proven terminal bound response forever.

Do not introduce a new completion class unless the existing wait-state fields cannot express the settle check directly.

### 5. Recovery and drift

On reload/remount:
- reconcile by exact persisted user/assistant logical identity;
- persistent container/group identity may remount without changing ownership;
- missing identity is â€œnot currently renderedâ€ until bounded reconciliation proves absence;
- duplicate exact identities, incompatible replacement, or foreign user interleaving before bind fail explicitly;
- no drift path authorizes Send replay.

## Required tests

Keep the regression set compact but cover every invariant:

1. old conversation + manual history before Send does not affect new Send identity;
2. exact outgoing user ID is accepted; broad assistant/activity/Stop alone is insufficient;
3. click/acceptance ambiguity never causes a second click;
4. exact Uâ†’A normal binding;
5. human U appears before automation A: explicit interruption, no later-assistant theft;
6. human U/A after A is already bound: bound A remains owned;
7. bound assistant remounts with same logical identity: tracking continues;
8. streaming updates one stable assistant key and later turns cannot redirect it;
9. completed bound DOM can finish despite stale unrelated LISTEN `IS_STREAMING`;
10. duplicate/missing logical turn structure fails explicitly;
11. existing attachment/composer ownership and recovery tests remain green.

After code review is clean, runtime TEST must run:
- fresh Solo smoke;
- a second Solo smoke reusing the same conversation URL;
- exactly one automation Send in each operation;
- trace/evidence shows exact user identity, exact assistant binding, stable streaming key, and no stale-LISTEN wedge.

## DEV implementation order

One DEV should implement the smallest complete slice, not create temporary parallel authorities:

1. add characterization tests first;
2. make exact Send identity authoritative and stop emitting broad new-send acceptance;
3. implement/persist exact assistant binding in the existing wait state;
4. scope existing sparse/full wait observation and transcript emission to the bound assistant;
5. make terminal decision use bound DOM + existing stability timing, with LISTEN correlated/supplemental;
6. delete or demote only the superseded broad authority branches proven unnecessary;
7. update the conflicting `LEARNING.md` lesson after behavior is verified;
8. run focused tests, relevant broader suites, import/compile/lint checks, and `git diff --check`;
9. commit only task-owned changes;
10. spawn exactly one Solo REVIEW before DEV exits.

## Non-goals

Do not redesign orchestration, route/report protocol, endpoint capacity, dashboard layout, task scheduling, bootstrap policy, or unrelated recovery machinery. Do not add a generic event bus, new durable store, new service, new dependency, second DOM reader, generalized state-machine framework, or copy the reference implementation wholesale.

## Stop condition

The feature is complete only when independent REVIEW/TEST evidence proves:
- exact Send identity for new Sends;
- accepted/ambiguous Send cannot replay;
- exact assistant binding survives long-lived conversation reuse;
- later human turns cannot steal a bound response;
- streaming and completion are scoped to that bound response;
- stale uncorrelated LISTEN cannot wedge completion;
- runtime smoke passes twice, including same-URL reuse;
- task-owned changes are committed and independently reviewed.
