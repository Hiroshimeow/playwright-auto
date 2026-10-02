# Research: Solo Existing-Conversation Correctness

## Decision 1: Keep navigation URL and canonical identity separate

**Decision**: Extend the existing conversation identity owner in `cdpa_commands.py` with one shared validator/normalizer for user-supplied ChatGPT conversation URLs. A valid navigation URL is HTTPS, uses a supported ChatGPT host, and has a recoverable terminal `/c/<id>` identity. Preserve the stripped user URL for navigation; use `conversation_identity()` only for equality/ownership.

**Rationale**: `conversation_identity()` already recognizes project-scoped paths ending in `/c/<id>`. The defect comes from API/store validators independently requiring exactly two path segments. Reusing the existing identity owner removes duplicate policy without creating a new module.

**Alternatives considered**:
- Add project-path exceptions independently to API and store: rejected because it preserves duplicate validators.
- Rewrite project URLs to short `/c/<id>`: rejected because the user explicitly supplied a valid navigation route and project context may matter.
- Accept arbitrary hosts: rejected because exact ChatGPT host validation remains a security/ownership boundary.

## Decision 2: Existing conversation reuse is not same-task continuation

**Decision**: For Solo prompt construction, a first-hop task (`kind == "task"`) is always a substantive task prompt. Whether it reuses an existing conversation does not set continuation mode. Compact continuation remains for later same-task repair/recovery hops.

**Rationale**: The stored Goal is the new Solo task authority. Reusing prior context cannot suppress the new instruction. The current expression conflates a browser/context decision with a prompt/task semantic decision.

**Alternatives considered**:
- Always include Goal in every continuation: rejected because format repair and same-task continuation intentionally stay compact and must not redo substantive work.
- Add a second Goal copy only when a URL is present: rejected because the first-hop semantic already provides the correct discriminator.

## Decision 3: Reuse existing receiptless observation state for pre-existing responses

**Decision**: Use the existing `observed_existing_response` receiptless-wait marker for both cases:
1. the supplied conversation is already responding during first-hop preparation; and
2. the conversation begins responding after preparation but before the new task crosses the Send boundary.

Do not introduce a new durable state. Retire the unused parallel `adopted_existing_response` marker.

**Rationale**: CDPA already has a durable receiptless waiting representation and reopen handling. The missing piece is correctly entering and exiting that state for the sending race.

**Alternatives considered**:
- Treat `UnsafePageStateError("page is already responding")` as retryable BLOCKED: rejected because responding is normal live work and repeated resume creates operator burden.
- Keep the hop in `sending` and retry every advance: rejected because it repeatedly reaches the Send path while unrelated response activity is still in progress.
- Add a new "existing_response_wait" hop state: rejected by the constitution complexity budget because the existing waiting state plus marker is sufficient.

## Decision 4: Receiptless existing-response wait must never consume the old assistant answer

**Decision**: When `observed_existing_response == true` and there is no Send receipt, waiting observes only whether the pre-existing response is still active. It must not validate or accept that assistant message as the result of the new Solo task.

When the pre-existing response becomes inactive:
- if no new-task prompt/ledger was prepared yet, return the hop to `pre_send`;
- if a prompt and pristine zero-attempt ledger record already exist, return the hop to `sending` without rebuilding or rebinding the request;
- otherwise fail closed on inconsistent durable evidence.

**Rationale**: The ordinary waiting controller with no receipt can select the newest assistant response. That response belongs to the conversation's prior work, not the new Goal. Consuming it would route/repair the wrong task.

**Alternatives considered**:
- Let ordinary receiptless response selection handle it: rejected because it can mistake prior response content for the current task result.
- Discard and recreate a new conversation after the response: rejected because the user explicitly requested the existing URL and its context.

## Decision 5: Guard the sending race with existing pristine-ledger proof

**Decision**: A `sending` hop may convert `page is already responding` to receiptless waiting only when the current request passes `_is_pristine_preboundary_sending_record()`, proving status NEW/PROMPT_SET, zero attempts, no binding/baseline/receipt/acceptance/session/error, and matching request/prompt/source identity.

**Rationale**: This existing predicate is already the repository's authoritative proof that the irreversible Send boundary has not been crossed.

**Alternatives considered**:
- Check only `attempts == 0`: rejected because other durable fields can prove or imply a crossed/ambiguous boundary.
- Convert every responding exception in `sending`: rejected because accepted or ambiguous sends must remain exactly-once and fail closed.

## Decision 6: Terminal historical ownership is not live ownership

**Decision**: The foreign conversation owner scan ignores terminal tasks but continues to fail closed for non-terminal tasks that durably claim the same conversation.

**Rationale**: A completed/stopped task retaining `page_id` metadata is history, not a live controller. Non-terminal records remain conservative ownership evidence.

**Alternatives considered**:
- Ignore all durable owner records when the user supplies a URL: rejected because two workers could control the same conversation.
- Require current browser online evidence before blocking: rejected because a temporarily offline non-terminal task still owns its durable execution identity.

## Verification strategy

Focused RED/GREEN tests must cover:
- project-scoped URL acceptance in both API and store;
- preservation of user navigation URL and canonical identity equivalence;
- first-hop existing-conversation Solo prompt includes Goal;
- already-responding pre-send path enters receiptless wait;
- ready→sending race with pristine ledger enters receiptless wait and records zero sends;
- old response completion resumes the pending new task instead of accepting old assistant content;
- non-pristine/attempted send does not use receiptless wait;
- terminal historical owner does not block while non-terminal owner still does.

Broader regression coverage should include Solo Task, runtime stability/recovery, actions/ownership, and conversation-identity tests.
