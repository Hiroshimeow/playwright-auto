# Contract: Solo Task Existing-Conversation Reuse

## Create Request

A Solo Task create request that reuses an existing conversation includes:

- `workflow_mode = "solo"`
- exactly one logical `role`
- a concrete `browser_endpoint_id`
- optional non-empty `goal`
- `conversation_url`: user-supplied existing ChatGPT conversation URL

## Conversation URL Contract

A reusable URL is accepted when:

1. scheme is `https`;
2. host is `chatgpt.com` or `www.chatgpt.com`;
3. its path terminates in `/c/<non-empty-conversation-id>`.

Valid examples include:

```text
https://chatgpt.com/c/6ab94cae-dac8-83e8-a362-48356451f6f9
https://chatgpt.com/g/g-p-project-slug/c/6ab94cae-dac8-83e8-a362-48356451f6f9
```

The exact accepted user URL is stored/preserved for navigation. Equality and ownership compare the canonical identity:

```text
/c/6ab94cae-dac8-83e8-a362-48356451f6f9
```

## First-Hop Prompt Contract

For a newly created Solo Task where the active hop is the task hop:

- Reusing an existing conversation does not make the hop a same-task continuation.
- If the stored Goal is non-empty, the first substantive prompt MUST contain that Goal.
- Prior conversation content is contextual history only; it does not replace the new Goal.
- The first substantive prompt MUST retain the Solo DONE/PAUSE route contract.

## Existing Response Contract

If the exact reused conversation is currently responding before the new task has crossed its Send boundary:

- the task remains RUNNING;
- the hop enters receiptless waiting/observation;
- no new Send attempt occurs;
- the existing assistant response is not accepted as the new task response;
- when prior activity ends, the pending new task resumes from its safe pre-boundary state.

If durable request evidence shows a Send attempt or accepted/ambiguous boundary evidence, the receiptless-existing-response rule does not apply; existing exactly-once reconciliation owns the outcome.

## Ownership Contract

- A non-terminal foreign task durably claiming the same canonical conversation identity remains a conflict.
- Terminal historical metadata alone is not a live ownership conflict.
- Explicit user conversation reuse does not bypass true concurrent ownership safety.

## Observable Failure Categories

Invalid or unsafe inputs/states remain typed rather than silently rewritten:

- invalid/unsupported conversation URL;
- redirected/mismatched conversation identity;
- live foreign conversation ownership;
- ambiguous or accepted Send boundary requiring existing reconciliation;
- manual draft/attachment conflict where existing safety policy applies.
