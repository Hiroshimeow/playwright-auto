# Contract: Adopt Existing Conversation URL

Example:

```json
{
  "roles": ["REVIEW"],
  "start_role": "REVIEW",
  "closing_role": "REVIEW",
  "provider_id": "chatgpt_web",
  "conversation_url": "https://chatgpt.com/c/...",
  "depends_on_task_ids": []
}
```

If REVIEW semantic route data includes `defect -> DEV`, reachable_roles may include DEV even though initial roles contains only REVIEW. DEV is lazy-materialized in the same TeamTask when needed.

## Binding rules

- Provider must explicitly support adopted browser URLs.
- ChatGPT Web opens/reuses the exact URL and verifies exact conversation identity before Send.
- Wrong URL/provider/endpoint/account or unresolved identity blocks with `CONVERSATION_IDENTITY_MISMATCH` without Send or new-chat fallback. In this feature, inability to prove canonical identity uses the same code as a proven mismatch.
- `depends_on_task_ids` contains TeamTask ids only.
- The adopted binding belongs to the start-role slot.
- A newly materialized different role gets its own provider conversation and receives handoff through MaterialRef.
- Routing back to the adopting role resumes the same adopted URL.
- Sharing one conversation across different role slots is out of scope.

Gate C must acquire the global canonical ConversationWriterLease before adopted URL Send, using the same lease_epoch fencing and command-ledger ambiguous-send reconciliation as every browser writer.
