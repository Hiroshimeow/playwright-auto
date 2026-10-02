# Feature Specification: Solo Existing-Conversation Correctness

**Feature Branch**: `001-fix-solo-reuse`

**Created**: 2026-09-30

**Status**: Draft

**Input**: User description: "Fix Solo Task existing-conversation correctness: accept ChatGPT project-scoped conversation URLs, preserve user URL navigation identity, treat an already responding exact conversation as work to wait for rather than a block, and always deliver the new Solo task Goal on the first hop even when reusing an existing conversation."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - New Goal Is Always Delivered (Priority: P1)

A user creates a new Solo Task with a Goal and points it at an existing ChatGPT conversation. The system must treat the Goal as the authority for this new task and deliver it to the agent even though the conversation contains prior context.

**Why this priority**: If the new Goal is omitted, the agent cannot know what work the user actually requested and may continue unrelated prior work.

**Independent Test**: Create a new Solo Task with a distinctive Goal and an existing idle conversation URL. Verify the first task prompt sent to that conversation contains the Goal and current Solo task identity exactly once.

**Acceptance Scenarios**:

1. **Given** an existing idle conversation and a new Solo Task with Goal X, **When** the task starts, **Then** the first task prompt includes Goal X and the current role/task context.
2. **Given** an existing conversation with old task context, **When** a new Solo Task with Goal X reuses it, **Then** old context may remain available but MUST NOT replace or suppress Goal X.
3. **Given** a same-task repair or later continuation after the Goal was already delivered by that task, **When** the task sends a compact continuation prompt, **Then** the system may omit the repeated full Goal while preserving same-task semantics.

---

### User Story 2 - Responding Conversation Waits Instead of Blocking (Priority: P1)

A user creates or resumes a Solo Task on an exact conversation that is already generating a response. The system waits for that response to finish rather than treating the normal responding state as an unsafe error.

**Why this priority**: Blocking on a normal response makes existing-conversation Solo Tasks unreliable and can encourage unsafe duplicate sends.

**Independent Test**: Start a Solo Task against a conversation that transitions from idle to responding immediately before the new task's send boundary. Verify no new send occurs while the existing response is active, the task enters a waiting state, and it continues safely after the response finishes.

**Acceptance Scenarios**:

1. **Given** an exact reused conversation is already responding before the new Solo prompt is sent, **When** the worker observes it, **Then** the task remains running in a wait/observe state and is not blocked merely because the page is responding.
2. **Given** the conversation becomes responding during the transition from preparation to sending, **When** durable evidence proves the new task has not crossed its send boundary, **Then** the task enters receiptless waiting with zero send attempts.
3. **Given** durable evidence indicates the new task may already have crossed the send boundary, **When** a responding state is observed, **Then** the system does not classify it as a pristine existing response and instead follows existing exactly-once reconciliation rules.
4. **Given** the pre-existing response is visible while the new Solo Task is receiptlessly observing it, **When** that prior assistant answer completes, **Then** that answer MUST NOT be accepted, routed, or repaired as the response of the new Solo Task.
5. **Given** the pre-existing response finishes, **When** the reused conversation becomes safe for the new task, **Then** an unprepared current request resumes at pre-send, an already-prepared pristine zero-attempt request resumes at sending without rebuild/rebind, and the new Solo Task remains able to deliver its Goal without duplicate send.

---

### User Story 3 - Project-Scoped ChatGPT URLs Are Reusable (Priority: P2)

A user supplies a valid ChatGPT conversation URL nested under a ChatGPT Project path. The system accepts and opens the exact URL rather than rejecting it because it is not the short `/c/<id>` shape.

**Why this priority**: Project-scoped conversations are valid user-owned ChatGPT conversations and are common for long-running engineering work.

**Independent Test**: Create a Solo Task using a valid project-scoped conversation URL ending in `/c/<conversation-id>`. Verify task creation succeeds, navigation uses the user-supplied URL, and identity comparison recognizes the same conversation as its short-form URL.

**Acceptance Scenarios**:

1. **Given** a valid HTTPS ChatGPT URL whose path ends in `/c/<conversation-id>`, including a project prefix, **When** the user creates a Solo Task, **Then** the URL is accepted.
2. **Given** a project-scoped URL and a short-form URL that end in the same conversation identifier, **When** the system compares conversation identity, **Then** they represent the same conversation.
3. **Given** a valid project-scoped URL supplied by the user, **When** the system navigates to the conversation, **Then** it uses the supplied URL rather than rewriting it to a different route.

---

### User Story 4 - True Concurrent Ownership Still Fails Closed (Priority: P2)

A reused conversation may have historical task records or a genuinely active owner. The system must distinguish stale history from live concurrent ownership.

**Why this priority**: Fixing false blocks must not introduce two workers controlling the same live conversation.

**Independent Test**: Verify a terminal/stale historical task record does not block explicit reuse, while a currently live conflicting owner still prevents concurrent control.

**Acceptance Scenarios**:

1. **Given** only terminal or stale historical records reference the conversation, **When** a new Solo Task explicitly reuses it, **Then** those records alone do not block adoption.
2. **Given** another live execution currently owns the same conversation, **When** a new Solo Task attempts to reuse it, **Then** the conflict remains fail-closed before a new send.

### Edge Cases

- The user supplies a ChatGPT URL on an unsupported host, without a terminal conversation identifier, or with an invalid scheme.
- The conversation redirects to a different conversation after navigation.
- The page becomes responding between readiness observation and the atomic send boundary.
- The durable request ledger exists but has zero attempts versus one or more attempts.
- A conversation contains prior manual draft/attachments while a new Solo Goal is pending.
- A historical task has a page identifier recorded but is already terminal or otherwise no longer executing.
- The same new Goal is retried through a format-repair path after the substantive task prompt has already been delivered.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept valid HTTPS ChatGPT conversation URLs on supported ChatGPT hosts when their path terminates in `/c/<conversation-id>`, including URLs with project-scoped prefixes.
- **FR-002**: The system MUST preserve the exact user-supplied valid conversation URL for navigation.
- **FR-003**: The system MUST derive a canonical conversation identity from supported URLs for comparison and ownership checks without replacing the navigation URL.
- **FR-004**: A new Solo Task's first substantive task prompt MUST include the task's current Goal even when an existing conversation URL is reused.
- **FR-005**: Existing-conversation reuse MUST NOT by itself classify the first hop of a new Solo Task as a same-task continuation.
- **FR-006**: Same-task compact continuation MAY omit the full Goal only after the current task has already delivered its substantive Goal or when performing a narrowly scoped response-format repair.
- **FR-007**: An exact reused conversation that is already responding before the new task crosses its send boundary MUST be treated as live work to observe/wait for, not as a terminal block condition.
- **FR-008**: If a responding state is observed after the hop enters a sending phase but durable request evidence proves zero send attempts and no accepted-send evidence, the system MUST transition to waiting without sending.
- **FR-009**: If durable evidence indicates a send attempt may already have occurred, the system MUST preserve exactly-once semantics and use existing accepted/ambiguous-send reconciliation rather than receiptless waiting.
- **FR-010**: Waiting on a pre-existing response MUST NOT create a duplicate task prompt or increment a send attempt.
- **FR-011**: Receiptless observation MUST NOT accept, route, or repair the pre-existing assistant answer as the new Solo Task result. After that prior activity completes, an unprepared current request MUST resume at pre-send, while an already-prepared request MUST resume at sending only when its durable evidence remains pristine and zero-attempt; otherwise the system MUST fail closed through existing recovery semantics. The pending Goal MUST remain deliverable exactly once.
- **FR-012**: Historical or terminal task records MUST NOT be treated as live concurrent ownership solely because they retain conversation/page metadata.
- **FR-013**: A genuinely live conflicting owner of the same exact conversation MUST continue to block concurrent control before a new send.
- **FR-014**: API validation, durable task storage, worker logic, and browser actions MUST apply one consistent definition of conversation identity.
- **FR-015**: Existing safety behavior for manual drafts, attachments, redirects, endpoint ownership, and ambiguous accepted sends MUST remain fail-closed.

### Key Entities

- **Navigation URL**: The exact valid ChatGPT URL provided by the user and used to open the conversation.
- **Conversation Identity**: The canonical identifier used to determine whether different supported URLs refer to the same conversation.
- **Solo Task Goal**: The authoritative instruction for the current Solo Task.
- **Durable Send Evidence**: Persisted request state indicating whether a send has not occurred, is confirmed, or is ambiguous.
- **Conversation Ownership**: Evidence that a current live execution controls a conversation, distinct from historical metadata.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of new Solo Tasks created with a non-empty Goal and a reused valid conversation URL deliver that Goal in their first substantive task prompt.
- **SC-002**: In deterministic tests of the preparation-to-sending race, a pre-existing responding conversation produces zero new send attempts and transitions to waiting when durable evidence proves the send boundary was not crossed.
- **SC-003**: Deterministic tests prove that requests with one or more send attempts are never reclassified as pristine receiptless waits.
- **SC-004**: Valid project-scoped and short-form ChatGPT URLs for the same conversation are both accepted and compare as the same conversation identity.
- **SC-005**: Navigation tests prove the exact valid URL supplied by the user is preserved when opening the conversation.
- **SC-006**: Historical terminal ownership metadata does not cause a false conflict, while a live conflicting owner remains blocked.
- **SC-007**: All focused Solo URL/prompt/responding/ownership regression tests pass with zero regressions in the relevant recovery and exactly-once test suites.

## Assumptions

- Supported ChatGPT hosts remain `chatgpt.com` and `www.chatgpt.com`.
- A valid reusable conversation URL is HTTPS and has a path whose final conversation segment is `/c/<non-empty-id>`.
- Query strings and fragments are not required for the user scenarios in this feature unless already supported by the canonical URL policy.
- Existing exactly-once durable request semantics remain authoritative after any possible send attempt.
- No new durable state, recovery service, or parallel ownership model is required; the repair should use existing task, wait, ownership, and send-evidence concepts.
