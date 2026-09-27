# REVIEW report — CDPA Send / Receive / Stream feature plan

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
5. `all_assistant_turns_for_receipt()` can cross later user turns and therefore is not a strict U→A binding primitive.
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

BẮT BUỘC mọi Solo agent trong chuỗi này phải tự spawn Solo task role tiếp theo trước khi kết thúc, và tiếp tục chuỗi Solo REVIEW/DEV/TEST cho đến khi công việc thực sự hoàn thành và có bằng chứng độc lập. Không dùng Team Task và không dựa vào route worker để chuyển role.

## Child DEV

Created and confirmed by the local API:
- task_id: `cdpa-idem-5eacc0a62cb909cae874f34d`
- team: `cdpa-chat-sendrecv-stream-dev-20260927`
- role: `DEV`
- workflow: Solo
- endpoint: 9202 / `56b44af5-6790-430f-afe3-4f78766d58be`
- plan/review checkpoint: `715d96955b0fd227298435dcfc290fe24d9c8023`

Creation command reached API state `applied`, and the task projection showed RUNNING on the requested endpoint.

Recovery note: an idempotency-confirmation mistake caused hop 1 to receive the placeholder goal `test`. Because that Send was already accepted, it was not replayed or overwritten. The same task now has a durable goal revision applying from hop 2 with the full implementation goal, including the plan/report/checkpoint, g6/9202/no-push/no-g8 constraints, preservation of unrelated dirty work, required implementation/verification/commit work, and the mandatory self-propagating Solo-chain sentence. A Resume/reconcile control was queued/applied for the accepted hop rather than creating a second DEV task. Exactly one child Solo DEV was created.
