# CDPA Agent Company — Solution Specification

**Status:** Draft v1  
**Implementation strategy:** Incremental, backward-compatible extension.  
**Migration style:** No big-bang rewrite.

## 1. Principles

Reuse existing `TaskStore`, runtime DB/persistence, durable command queue, dependency graph, endpoint pool, Independent Agent infrastructure, RequestLedger, worker lifecycle, recovery framework and dashboard API.

Do not introduce a parallel ownership model, second scheduler, second task graph, second database, generic agent framework or external message broker in the first phase.

## 2. Phase 0 — Stabilize control plane

Before Agent Company rollout, current operational defects must be stabilized rather than hidden by a new layer:

- durable queue/dispatch;
- stale RUNNING reconciliation;
- worker liveness outside worker failure domain;
- Windows persistence/atomic-replace failure;
- endpoint-loss recovery;
- self-heal trigger framework.

## 3. Phase 1 — Generalize Independent Agent into Agent Registry

Independent Agent already provides long-lived identity, manifest-backed state, persistent-conversation option, triggers and enable/disable lifecycle. Extend/generalize this model instead of creating an Agent runtime from scratch.

Introduce a narrow `AgentRegistry` abstraction backed by existing persistence.

Suggested fields:

```text
agent_id
display_name
role
team
manager_id
capabilities
enabled
status
preferred_endpoint
current_task_id
conversation_generation
created_at
updated_at
```

Existing independent-agent identities must remain valid during migration.

## 4. Phase 2 — Conversation generations

Persist Agent ↔ Conversation binding with at least:

```text
agent_id
generation
conversation_id
conversation_url
endpoint_id
page_id
mode
status
opened_at
closed_at
```

`(agent_id, generation)` is the durable logical identity of a conversation generation.

`/new` means rotate conversation generation, not replace the agent.

Rotation sequence:

1. inspect current durable request boundary;
2. if accepted/ambiguous, reconcile exact execution before rotation;
3. persist bounded checkpoint/recovery context;
4. increment generation;
5. acquire new conversation;
6. bind new endpoint/page metadata;
7. bootstrap bounded agent/task/inbox context;
8. resume normal work without replay.

## 5. Phase 3 — Durable mailbox

Add durable message storage to existing runtime persistence.

Minimum fields:

```text
message_id
message_type
from_agent_id
to_agent_id
task_id
correlation_id
causation_id
idempotency_key
payload
state
created_at
delivered_at
consumed_at
completed_at
```

Initial message types:

```text
ASSIGN
RESULT
ASK
ANSWER
HANDOFF
BLOCKER
REVIEW_REQUEST
REVIEW_RESULT
ESCALATE
CANCEL
```

Message must be committed before send is acknowledged.

Recommended internal interface:

```text
send_message(...)
claim_next_message(...)
complete_message(...)
cancel_message(...)
list_inbox(...)
list_outbox(...)
```

All payloads are bounded. Do not copy complete conversation histories into messages; use artifact/evidence references.

## 6. Message-driven wakeup

Mailbox does not introduce a scheduler.

Flow:

```text
message persisted
  -> existing event/reconciliation mechanism wakes recipient
  -> eligibility/capacity checked
  -> existing dispatcher grants slot
  -> normal CDPA execution starts or resumes
```

If an event is lost, a durable PENDING message remains discoverable by the normal reconciliation sweep.

## 7. ASK / ANSWER

A blocking ASK records a correlation ID and may move the requester task to:

```text
WAITING/agent_response
```

That wait must not consume a browser slot unless an existing irreversible execution requires exact ownership to remain.

When a valid ANSWER with matching correlation arrives:

1. validate sender/recipient/correlation;
2. complete the ASK relationship;
3. release the waiting task;
4. re-enter the existing durable admission queue;
5. continue through normal controller semantics.

No mailbox code may directly invoke browser Send.

## 8. Assignment strategy

Do not create a separate Assignment state machine in v1.

Represent assignment using existing task/hop state plus optional:

```text
assigned_agent_id
```

Manager chooses capability; Agent Registry resolves a suitable employee; existing task creation/queue/dispatch does the execution.

Only introduce a dedicated Assignment entity if later requirements prove many-to-many assignment cannot be represented safely.

## 9. Manager skills

Organizational behavior belongs in skills/policies, not hard-coded controller branches.

### Work Manager

Responsibilities:

- decompose desired goal;
- find existing owner before spawning;
- assign by capability;
- preserve one mutable-worktree DEV owner;
- manage dependencies;
- request review/test;
- reconcile completion;
- refill safe capacity.

### Operations Manager

Responsibilities:

- stale worker heartbeat;
- stale RUNNING tasks;
- endpoint offline;
- command queue not draining;
- queue starvation;
- lease/recovery failure;
- persistence incidents;
- RUNNING state without an active executor.

### Quality Manager

Responsibilities:

- UI acceptance;
- empty/missing reports;
- dashboard projection correctness;
- operator-control behavior;
- product-quality regressions.

Runtime liveness and product quality remain separate concerns.

## 10. External worker watchdog

Preferred first implementation: reuse a runtime already outside `CDPAWorker`, such as dashboard API process or existing OS/service supervisor.

Watchdog observes durable worker heartbeat/process liveness.

When stale/dead:

```text
control_plane_status = DEGRADED/OFFLINE
```

Dashboard must make this visible.

Mutating commands may remain durably queued, but their status must clearly mean `WAITING_FOR_WORKER`; UI/API must not imply execution is in progress.

If automatic worker restart exists, verification of recovered heartbeat is required before triggering normal IA/repair mechanisms.

## 11. Events

Minimum new semantic events:

```text
AGENT_CREATED
AGENT_STATUS_CHANGED
AGENT_ASSIGNED
CONVERSATION_ROTATED
MESSAGE_CREATED
MESSAGE_CONSUMED
MESSAGE_COMPLETED
AGENT_WAITING_FOR_RESPONSE
AGENT_RESPONSE_RECEIVED
WORKER_STALE
WORKER_RECOVERED
```

Events serve wakeup/projection/audit. Durable state remains authoritative.

## 12. Dashboard

Add an Agent/Company projection without replacing the existing task board.

Agent card should expose at minimum:

```text
Agent identity
Role / Team / Manager
Current assignment
Agent status
Unread inbox
Conversation generation
Preferred/requested endpoint
Actual execution endpoint
Last activity
```

Company view may show:

```text
Engineering Manager
  -> Dev agents
  -> Reviewer agents
Operations Manager
  -> Recovery/monitoring agents
Quality Manager
  -> UI/quality agents
```

Org chart is a projection only.

## 13. Migration

### Existing workflow tasks

Tasks without `assigned_agent_id` continue unchanged.

### Existing independent agents

Map existing independent task identity into Agent Registry without silently replacing task IDs, conversations or durable ownership.

Migration must be additive and restart-safe.

## 14. Testing strategy

### Unit

- stable agent identity persistence;
- generation increment;
- mailbox CRUD/state transitions;
- typed-message validation;
- idempotency;
- correlation;
- manager relationship validation.

### Restart

- agent identity survives worker restart;
- pending message survives restart;
- unresolved ASK survives restart;
- current conversation generation survives restart.

### Concurrency

- duplicate ASK/action deduped;
- one message claimed exactly once;
- two managers cannot allocate the same exclusive mutable work;
- two agents cannot both acquire one exclusive assignment.

### Recovery

- endpoint loss;
- conversation loss;
- worker loss;
- accepted/ambiguous send non-replay;
- conversation rotation after safe recovery.

### Live acceptance on 9202/9212/9222

1. manager assigns a DEV agent;
2. DEV executes on one endpoint;
3. DEV sends ASK to reviewer on another endpoint;
4. DEV becomes WAITING without wasting capacity;
5. reviewer ANSWER releases DEV;
6. DEV resumes through queue;
7. DEV rotates `/new`;
8. generation changes, agent ID does not;
9. endpoint can change where policy allows;
10. task completes through normal PLAN/review lifecycle.

## 15. Rollout

- Release A: Agent Registry / stable identity.
- Release B: Conversation generations and rotation.
- Release C: Durable mailbox + ASK/ANSWER.
- Release D: Capability assignment + Work Manager skill.
- Release E: Operations/Quality manager skills and external liveness integration.
- Release F: Company dashboard projection.

Each release must be independently testable and rollbackable.

## 16. Anti-patterns

Do not introduce:

```text
AgentManagerV2
SecondTaskStore
AgentScheduler
MessageBrokerService
AgentWorkflowEngine
parallel agent state machine duplicating CDPA
```

Do not implement agent communication as “open agent X's chat URL and type a message”. URL/session transport is replaceable and must never be the durable communication contract.

## 17. Final target

```text
User / Desired Goal
        ↓
Company Managers + Skills
        ↓
Durable Agent Communication
        ↓
Stable Agent Identities
        ↓
Existing CDPA Task Graph
        ↓
Existing Queue / Controller
        ↓
Shared Browser Capacity
```

The target is a durable multi-agent organization whose execution core remains the existing CDPA control plane.
