# CDPA Agent Company — Architecture Specification

**Status:** Draft v1  
**Architecture style:** Evolutionary extension of existing CDPA control plane  
**Primary constraint:** One durable task graph, one execution controller.

## 1. Architecture decision

Agent Company là một lớp tổ chức nằm trên control plane hiện tại. Không thay `TaskStore`, `CDPAWorker`, request ledger, hop routing, endpoint allocator, browser ownership hoặc recovery semantics.

Bổ sung đúng các capability còn thiếu:

- Agent Registry;
- durable Mailbox;
- Agent ↔ Conversation binding;
- organization policy/skills;
- message-driven Agent Reconciler;
- external Worker Watchdog.

Không tạo orchestrator, scheduler, task graph, database hoặc workflow engine thứ hai.

## 2. Logical architecture

```text
Desired Goals
    |
Managers + Agent Skills
    |
ASSIGN / ASK / REVIEW
    |
Durable Mailbox
    |
Agent Registry
    |
Existing Durable Task Graph / Queue / Dependencies
    |
CDPAWorker
    |
9202 / 9212 / 9222
    |
replaceable conversations
```

## 3. Transport responsibilities

- Agent ↔ Agent: durable mailbox trong existing runtime persistence.
- Agent → tools/capabilities: MCP.
- Backend → dashboard: SSE phù hợp cho live projection; polling có thể tồn tại trong migration period.
- Không dùng browser-to-browser direct messaging.
- Không dùng MCP hoặc SSE làm authoritative organizational message bus.

## 4. Core entities

### Agent

```text
agent_id
display_name
role
team
manager_id
capabilities[]
enabled
lifecycle_status
preferred_endpoint
current_task_id
conversation_generation
created_at
updated_at
```

Lifecycle đề xuất:

```text
IDLE
ASSIGNED
RUNNING
WAITING
DEGRADED
PAUSED
DISABLED
```

Agent identity không có trạng thái DONE.

### AgentConversation

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

Logical key: `(agent_id, generation)`.

### AgentMessage

```text
message_id
type
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

Lifecycle:

```text
PENDING -> DELIVERED -> CONSUMED -> COMPLETED
```

`CANCELLED` là terminal alternative.

### Task

Task hiện tại giữ nguyên và tiếp tục là source of truth của executable work. Chỉ bổ sung optional `assigned_agent_id` hoặc metadata tương đương.

## 5. Source-of-truth ownership

| Concern | Source of truth |
|---|---|
| Executable task lifecycle | Existing TaskStore |
| Send provenance | Existing RequestLedger |
| Browser ownership | Existing task/role/page state |
| Agent identity | Agent Registry |
| Agent communication | Mailbox |
| Conversation generation | Agent conversation binding |
| Endpoint capacity | Existing endpoint pool |
| Dependencies | Existing task graph |
| Organizational behavior | Skills/policies |

Một state không được có hai writable authoritative copies.

## 6. Stable identity model

Agent không sở hữu endpoint hoặc URL. Endpoint chỉ là capacity/runtime placement; URL chỉ là locator.

```text
Agent engineering.dev.01
  ├─ Task A
  ├─ Task B
  └─ Conversation generations
       ├─ 17
       ├─ 18
       └─ 19
```

Agent có thể có `preferred_endpoint` hoặc affinity nhưng actual placement vẫn tuân theo existing allocator/recovery invariants.

## 7. Conversation rotation

`/new` hoặc conversation replacement phải dùng flow:

```text
rotation requested
  -> inspect durable request boundary
  -> if accepted/ambiguous: reconcile exact execution first
  -> persist bounded checkpoint/recovery context
  -> increment generation
  -> acquire/bind new conversation
  -> restore bounded context
  -> continue same agent/task
```

Không replay accepted/ambiguous request.

## 8. Mailbox architecture

Write path:

```text
validate message
  -> persist AgentMessage
  -> commit
  -> publish message-available event
```

Read path:

```text
message-available
  -> reconciler checks recipient eligibility
  -> claim oldest eligible message exactly once
  -> trigger/release normal CDPA work
```

Missed events không làm mất work vì PENDING message vẫn durable và được sweep lại.

## 9. ASK / ANSWER

Blocking ASK dùng correlation ID:

```text
DEV --ASK C1--> REVIEWER
DEV task -> WAITING/agent_response
REVIEWER --ANSWER C1--> mailbox
reconciler releases DEV task
normal queue resumes it
```

WAITING vì agent response không được tiêu browser slot nếu không còn execution cần giữ active.

## 10. Management model

### Work Manager

Reconcile desired goals với current owners, dependencies, capacity và review/integration state. Không trực tiếp mở browser tab hoặc tạo execution bypassing queue.

### Operations Manager

Theo dõi worker heartbeat, endpoint health, queue age, stale task, command backlog, lease/recovery/persistence incidents.

### Quality Manager

Theo dõi UI acceptance, report completeness, dashboard invariants, operator controls và product-quality regressions. Quality không được nhập chung với runtime liveness.

## 11. Failure-domain rule

Worker Watchdog phải chạy ngoài failure domain của `CDPAWorker`. Có thể nằm ở dashboard API process hoặc service supervisor hiện có.

Nếu worker chết thì watchdog vẫn phải phát hiện được, dashboard phải chuyển control plane sang DEGRADED/OFFLINE và queued mutation phải thể hiện là chờ worker, không giả vờ đã chạy.

## 12. Dashboard information model

Projection phải phân biệt rõ:

```text
Agent identity
Task / Assignment
Conversation generation
Endpoint
Page
```

Company view có thể nhóm `Team -> Agent`, nhưng chỉ là projection. Business-critical state không được lưu trong UI.

## 13. Control boundaries

Agent không được:

- sửa manifest trực tiếp;
- thao tác page của agent khác;
- bypass dependency;
- override operator Pause/Stop/Delete;
- replay accepted request;
- tăng repair budget tùy ý;
- spawn descendants không giới hạn.

Mọi action ảnh hưởng task phải đi qua existing durable command/control path.

## 14. Scalability decision

Phase đầu reuse existing runtime DB/persistence. Chỉ cân nhắc Postgres/Redis/NATS/Kafka khi có evidence về throughput, multi-host distribution hoặc mailbox contention mà primitives hiện tại không đáp ứng được.

## 15. Architecture invariants

1. Một executable task graph.
2. Một controller execution model.
3. Một authoritative request ledger.
4. Agent identity độc lập browser/session.
5. Conversation là replaceable runtime resource.
6. Endpoint là shared capacity, không phải employee identity.
7. Message phải durable trước delivery.
8. Accepted/ambiguous Send không replay.
9. Operator controls authoritative.
10. Worker watchdog không cùng failure domain với worker.
11. Organization behavior nằm trong skills/policy layer.
12. Không hard-code org chart vào `CDPAWorker`.
