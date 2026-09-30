# Contract: Accepted Send, Provisional Conversation, and Reconciliation

## Owner boundary

Legacy worker and request ledger own accepted-send provenance, canonical conversation enrichment, blocking, and reconciliation. Runtime-v2 audit may display these states but must not mutate them.

## Provisional accepted-send handling

When a hop has accepted Send and the conversation URL is `https://chatgpt.com/c/local-chatgpt%3A...`:

1. If the exact owned page is still present and shows the accepted user turn, the worker must continue observing that page.
2. The worker must not reopen, resend, restart role, or create a new chat while the accepted user boundary is already crossed.
3. The worker must retry canonicalization on later observation ticks.
4. When the exact page URL becomes `/c/<canonical-id>`, the worker must enrich the receipt and request ledger with that canonical id.

## Safe block conditions

The worker must block instead of observing when any of the following is true:

- exact page binding is missing;
- page id does not match the accepted receipt binding;
- accepted user message is no longer visible or cannot be proven;
- canonical URL conflicts with durable request evidence;
- multiple possible canonical identities exist.

## Reconciliation path

If the accepted send cannot be recovered from exact page evidence:

- retry is not allowed when the block is non-retryable;
- restart/new chat is not allowed after accepted Send;
- route reconciliation must create a new hop of kind `accepted_send_reconciliation`;
- reconciliation handoff must instruct the closing role to use durable reports, request ledger, and existing evidence only;
- reconciliation must not replay external/business side effects.

## Audit requirements

Runtime-v2 audit should surface:
- `accepted_conversation_identity_unresolved` block code;
- whether the blocked task is old or current verification work;
- latest known page URL type: root, provisional local-chatgpt, or canonical `/c/<id>`;
- recent send/acceptance/canonicalization events when available.
