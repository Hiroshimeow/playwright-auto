# CDPA Agent Company — Requirements Specification

**Status:** Draft v1  
**Target system:** `playwright-auto-multicdp`  
**Purpose:** Nâng CDPA từ task orchestration thành một hệ thống nhiều agent có identity bền vững, có thể phân công, trao đổi và phối hợp tự động mà không thay thế control plane hiện tại.

---

## 1. Mục tiêu

CDPA phải hỗ trợ mô hình trong đó mỗi agent là một thực thể logic tồn tại lâu dài:

- có `agent_id` cố định;
- có role/capability/team/manager;
- có thể nhận nhiều nhiệm vụ nối tiếp nhau;
- có thể thay browser endpoint;
- có thể `/new` hoặc thay conversation;
- có thể crash/recover;
- có thể giao tiếp với agent khác;
- không mất identity khi URL/chat/session thay đổi.

Nguyên tắc nền tảng:

> **Agent identity is durable. Conversation is disposable. Endpoint is replaceable. URL is only a locator.**

## 2. Phạm vi

### 2.1 Trong phạm vi

Hệ thống phải bổ sung:

1. Stable Agent Identity.
2. Agent Registry.
3. Agent ↔ Conversation binding.
4. Conversation generation.
5. Durable agent mailbox.
6. Typed agent-to-agent messages.
7. `ASK / ANSWER`.
8. `ASSIGN / RESULT`.
9. `REVIEW_REQUEST`.
10. Wakeup/reconciliation khi có message mới.
11. Agent manager hierarchy.
12. Capability-based delegation.
13. External liveness watchdog cho CDPA worker.
14. Dashboard projection cho agents/messages/assignments.
15. Skills/workflows cho manager, reviewer, operations và quality agents.

### 2.2 Ngoài phạm vi

Không xây:

- orchestrator thứ hai;
- task graph thứ hai;
- scheduler thứ hai;
- database thứ hai;
- Kafka/RabbitMQ/NATS trong phase đầu;
- generic actor framework;
- generic workflow DSL;
- peer-to-peer direct browser messaging;
- agent tự điều khiển tab của agent khác;
- agent chia sẻ toàn bộ conversation history;
- unrestricted free-form recursive agent spawning.

Mọi execution tiếp tục đi qua:

```text
CDPA TaskStore
    ↓
CDPAWorker
    ↓
existing task/hop/request lifecycle
    ↓
browser endpoint
```

## 3. Các khái niệm

### 3.1 Agent

Agent là employee logic tồn tại lâu dài.

Ví dụ:

```text
agent_id = engineering.backend.01
role = DEV
team = engineering
manager_id = engineering.manager
```

Agent **không đồng nhất** với:

- task;
- hop;
- browser;
- page;
- conversation;
- chat URL.

### 3.2 Conversation

Conversation là runtime context của agent.

Một agent có thể có tuần tự:

```text
generation 17
generation 18
generation 19
```

`/new` tạo generation mới nhưng không tạo agent mới.

### 3.3 Task

Task tiếp tục là đơn vị durable work hiện tại của CDPA.

Không thay đổi ownership invariants của task graph.

Task có thể bổ sung:

```text
assigned_agent_id
```

### 3.4 Assignment

Phase đầu không cần state machine Assignment riêng.

Assignment được biểu diễn bằng:

```text
task_id
assigned_agent_id
role
active_hop
```

Chỉ tách entity `Assignment` riêng nếu tương lai chứng minh cần many-to-many scheduling.

### 3.5 Message

Message là durable communication primitive giữa các agent.

Message không phụ thuộc conversation URL.

## 4. Functional Requirements

### FR-01 — Stable Agent Identity

Mỗi agent phải có một `agent_id` duy nhất và bền vững.

`agent_id` không thay đổi khi:

- `/new`;
- conversation reset;
- browser restart;
- endpoint migration;
- worker restart;
- agent nhận task mới.

### FR-02 — Agent Registry

CDPA phải lưu được tối thiểu:

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
current_conversation_generation
created_at
updated_at
```

Registry phải durable qua restart.

### FR-03 — Conversation Binding

CDPA phải ánh xạ:

```text
agent_id
    ↓
conversation_generation
    ↓
conversation_id / URL
    ↓
endpoint_id
    ↓
page_id
```

URL/page chỉ là runtime binding.

### FR-04 — Conversation Rotation

Agent phải có thể yêu cầu conversation mới khi:

- context đầy;
- conversation lỗi;
- UI/runtime corruption;
- operator yêu cầu New Chat;
- policy yêu cầu fresh context.

Rotation phải:

1. checkpoint durable state;
2. tăng generation;
3. tạo/bind conversation mới;
4. inject bounded recovery context;
5. giữ nguyên agent identity;
6. không replay accepted/ambiguous Send.

### FR-05 — Durable Mailbox

Message giữa agents phải được lưu durable trước khi được coi là gửi thành công.

Restart worker không được làm mất message.

### FR-06 — Typed Messages

Phase đầu hỗ trợ:

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

Mọi message phải có:

```text
message_id
type
from_agent_id
to_agent_id
payload
correlation_id
idempotency_key
created_at
accepted_at
completed_at
```

### FR-07 — ASK / ANSWER

Agent đang xử lý task phải có thể gửi câu hỏi đến một agent khác.

Task có thể chuyển sang:

```text
WAITING/agent_response
```

nếu câu trả lời là prerequisite.

WAITING này không được tiêu browser execution slot nếu agent không cần giữ execution active.

### FR-08 — Delegation

Manager hoặc agent được phép phải có thể yêu cầu:

```text
capability = X
task = Y
```

thay vì buộc phải biết endpoint hoặc conversation URL.

Router chọn agent phù hợp dựa trên:

- capability;
- availability;
- current load;
- team;
- endpoint constraints;
- dependency;
- mutable worktree ownership.

### FR-09 — Manager Hierarchy

Agent có thể có `manager_id`.

Mức tối thiểu:

```text
Work Manager
Operations Manager
Quality Manager
```

Không bắt buộc một agent phải có đúng một manager trong tương lai, nhưng phase đầu dùng một `manager_id`.

### FR-10 — Work Manager

Work Manager chịu trách nhiệm:

- desired goal → task/workstream;
- assignment;
- dependency;
- capacity refill;
- review/test routing;
- completion reconciliation.

Work Manager không sửa product code trực tiếp nếu policy hiện tại cấm.

### FR-11 — Operations Manager

Operations Manager phải theo dõi:

- worker heartbeat;
- stalled execution;
- endpoint offline;
- stale RUNNING task;
- command queue không drain;
- lease expiry;
- conversation loss;
- persistence failure;
- repeated recovery failure.

### FR-12 — Quality Manager

Quality Manager chịu trách nhiệm phát hiện chất lượng sản phẩm ngoài runtime health:

- UI regression;
- empty/missing report;
- unusable controls;
- poor information hierarchy;
- dashboard inconsistency;
- acceptance criteria regression.

Quality inspection không được gộp vào runtime watchdog.

### FR-13 — External Worker Watchdog

Phải có một liveness mechanism **không nằm cùng failure domain với CDPA worker**.

Nếu worker stale/dead:

- dashboard phải báo control plane degraded/offline;
- mutating commands không được tạo cảm giác đã thực thi;
- queued command phải được đánh dấu waiting-for-worker;
- recovery/restart policy được kích hoạt theo cấu hình.

### FR-14 — Existing Safety Invariants

Company layer không được phá các invariant hiện có:

- accepted Send không replay;
- ambiguous Send không replay;
- exact durable ownership;
- `(endpoint_id, page_id)` là physical page identity;
- dependency global;
- endpoint chỉ là shared capacity;
- free user tabs không được chiếm dụng;
- operator Pause/Stop/Delete có quyền ưu tiên;
- manifest chỉ được worker/dashboard mutation path cập nhật.

## 5. Non-Functional Requirements

### NFR-01 — Durability

Các dữ liệu sau phải sống qua worker restart:

- agent identity;
- mailbox;
- conversation generation;
- assignment;
- correlation;
- unresolved ASK;
- manager relationship.

### NFR-02 — Idempotency

Mọi message/action có khả năng retry phải có idempotency key.

Duplicate delivery không được gây:

- duplicate task;
- duplicate Send;
- duplicate repair;
- duplicate review request.

### NFR-03 — Auditability

Phải truy được:

```text
who
asked whom
for what
for which task
when
what was answered
what decision followed
```

### NFR-04 — Bounded Autonomy

Không được recursive spawning vô hạn.

Mọi delegation/repair/escalation phải có:

- provenance;
- depth/budget;
- dedupe;
- terminal escalation path.

### NFR-05 — Backward Compatibility

Workflow CDPA hiện tại vẫn phải chạy khi không sử dụng company features.

Agent company là additive layer.

### NFR-06 — Incremental Migration

Không yêu cầu migrate tất cả role/task hiện hữu thành agent ngay lập tức.

Legacy task có thể:

```text
assigned_agent_id = null
```

## 6. Acceptance Criteria

Hệ thống được coi là đạt phase đầu khi:

1. Agent `engineering.dev.01` tồn tại qua worker restart.
2. Agent đó đổi từ 9202 → 9222 mà không đổi identity.
3. Agent `/new` và tiếp tục cùng assignment trên generation mới.
4. Manager gửi `ASK` đến reviewer; reviewer trả `ANSWER`; original task tự tiếp tục.
5. Message không mất khi worker chết giữa send và consume.
6. Duplicate message không gây duplicate action.
7. Agent không biết URL của agent khác vẫn giao tiếp được.
8. Dashboard hiển thị Agent → Task → Conversation → Endpoint rõ ràng.
9. Worker chết được phát hiện dù worker không còn chạy.
10. Không phá bất kỳ accepted-send/recovery invariant hiện hữu nào.
11. Existing workflow không dùng agent company vẫn pass regression.
12. Không xuất hiện orchestrator, scheduler hoặc database thứ hai.

## 7. Definition of Done

Phase Agent Company Core hoàn tất khi:

```text
stable identity
+ conversation generations
+ durable mailbox
+ typed communication
+ message-driven wakeup
+ manager skills
+ external worker liveness
```

được tích hợp vào cùng TaskStore/CDPAWorker hiện tại, có focused tests, restart tests, failure-injection tests và live acceptance trên multi-endpoint topology.
