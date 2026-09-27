# REVIEW report â€” CDPA Send / Receive / Stream feature plan

Task: `cdpa-idem-bbb63a71b92e95ad0b221bc4`
Team: `cdpa-chat-sendrecv-stream-plan-review-20260927`
Role: REVIEW
Repository: `E:\git-project\playwright-auto-multicdp`
Reviewed starting HEAD: `940eb6b916dd94ad7d96362575d1a0c7cbef5b9d`
Reference: `E:\git-project\codex-chatgpt-web-reference` @ `2d73f626290a5062825bb595dabb89aaf88d16c5`

## Scope

Plan-only review. No production source was modified. Existing unrelated dirty/untracked work was preserved.

## Source reviewed

Current CDPA:
- `src/playwright_auto/chatgpt.py`: Send acceptance, frontend identity observer, wait snapshots, `wait_for_response`.
- `src/playwright_auto/chatgpt_models.py`: `SendReceipt`, receipt/user matching, assistant selection helpers, response activity.
- `src/playwright_auto/role_runtime/response.py`: latest-user/current-assistant result selection.
- `src/playwright_auto/role_runtime/controller.py`: LISTEN priority and DOM fallback.
- `src/playwright_auto/role_runtime/signals.py`: DOM/LISTEN transcript projection.
- `src/playwright_auto/role_runtime/page_observer.py`: MutationObserver/network observation.
- `src/playwright_auto/durable_blocks.py`: durable Send ledger and passive frontend identity enrichment.

Reference repo:
- `docs/architecture.md`
- `src/chatgpt-session.ts`
- `src/adapters/chatgpt-web/browser-worker.ts`

## Findings

1. The original plan over-designed Send with an extra durable state machine. CDPA already has the correct non-replay boundary in the durable ledger and Send recovery path. Adding another durable owner would violate the repo's one-owner rule.
2. Current `_wait_send_acceptance()` emits broad acceptance from assistant/activity/Stop signals. These signals prove progress but not the exact automation user turn.
3. CDPA already observes the exact outgoing `user_message_id` from `POST /backend-api/f/conversation`. The feature should promote/reuse that existing identity path rather than invent another tracker.
4. Current `wait_for_response()` defaults to `current_assistant()`, which selects the assistant after the newest rendered user. That is incompatible with long-lived role URLs when a human chats manually after or during automation.
5. `all_assistant_turns_for_receipt()` can cross later user turns and therefore is not a strict Uâ†’A binding primitive.
6. Live transcript and LISTEN acceptance are currently latest/global oriented rather than receipt-bound.
7. Reference repo mechanisms worth adopting: logical turn identities, persistent turn containers/group keys across virtualization, bind-once assistant ownership, bound-turn scoped DOM reads, explicit DOM-drift failure, completion action + no generation + stable content.
8. Reference assumptions that do not fit: one-new-turn-since-baseline alone, retained isolated WebContents assumptions, Codex MCP tool-batch fences, trace/Markdown serialization stack, and its larger completion-health class hierarchy.

## Revised design

The plan was rewritten in place to one narrow pipeline:

`exact Send user identity -> exact assistant binding -> same bound turn for stream + completion`

No new durable Send state machine, no second DOM reader, no generalized event/state framework.

Key required behavior:
- new Sends require exact automation user identity; broad assistant/activity/Stop become wake/diagnostic only;
- ambiguous post-click outcome remains fail-closed with no resend;
- assistant is bound to the exact receipt user and stored in existing hop `wait` state;
- a manual user turn before assistant bind causes explicit interruption instead of stealing a later answer;
- later manual turns after binding cannot rebind the operation;
- MutationObserver remains wake-only;
- LISTEN may affect the operation only when correlated to the bound assistant;
- bound DOM completion overrides stale unrelated `IS_STREAMING` once completion evidence is stable.

Revised plan:
`.plan\cdpa-chat-send-receive-stream-solo-chain-20260927.md`

## Verification of review artifact

- Current repository HEAD matched the requested `940eb6b916dd94ad7d96362575d1a0c7cbef5b9d` before this feature-plan checkpoint.
- Reference repository HEAD matched `2d73f626290a5062825bb595dabb89aaf88d16c5`.
- No production implementation file was edited.
- Existing unrelated dirty/untracked files remain untouched.

## Solo-chain requirement

Báº®T BUá»˜C má»i Solo agent trong chuá»—i nÃ y pháº£i tá»± spawn Solo task role tiáº¿p theo trÆ°á»›c khi káº¿t thÃºc, vÃ  tiáº¿p tá»¥c chuá»—i Solo REVIEW/DEV/TEST cho Ä‘áº¿n khi cÃ´ng viá»‡c thá»±c sá»± hoÃ n thÃ nh vÃ  cÃ³ báº±ng chá»©ng Ä‘á»™c láº­p. KhÃ´ng dÃ¹ng Team Task vÃ  khÃ´ng dá»±a vÃ o route worker Ä‘á»ƒ chuyá»ƒn role.

## Child DEV

Pending creation after this report and revised plan are checkpoint-committed. The child must be Solo DEV on endpoint 9202, implement the smallest complete safe slice, verify and commit task-owned changes, then spawn exactly one Solo REVIEW before it exits.
