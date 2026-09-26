# CDPA Agent Workflow & Prompt Guide — Nghiên cứu và thiết kế tham khảo

> **Trạng thái:** Tài liệu nghiên cứu và thiết kế tham khảo.
>
> Đây **không phải** policy hiện hành, runtime contract đã được chấp nhận, hay bộ prompt mà CDPA đang sử dụng. Không file prompt active nào được thay đổi bởi tài liệu này. Một phần chỉ trở thành authoritative khi user hoặc một task/spec hiện hành chấp nhận rõ phần đó.
>
> **Ngôn ngữ:** Tiếng Việt trước. Các thuật ngữ kỹ thuật và trích dẫn nguồn giữ tiếng Anh khi cần độ chính xác.
>
> **Ngày khảo sát:** 2026-09-26.

---

## Mục lục

1. [Mục tiêu và phạm vi](#1-mục-tiêu-và-phạm-vi)
2. [Tóm tắt điều hành](#2-tóm-tắt-điều-hành)
3. [Hiện trạng CDPA và vấn đề cần giải quyết](#3-hiện-trạng-cdpa-và-vấn-đề-cần-giải-quyết)
4. [Tiêu chí của một workflow/prompt phù hợp với CDPA](#4-tiêu-chí-của-một-workflowprompt-phù-hợp-với-cdpa)
5. [Bản đồ authority đề xuất](#5-bản-đồ-authority-đề-xuất)
6. [Mô hình identity và capability](#6-mô-hình-identity-và-capability)
7. [Phân lớp prompt](#7-phân-lớp-prompt)
8. [Routing graph và trách nhiệm role](#8-routing-graph-và-trách-nhiệm-role)
9. [Failure, retry, fallback và recovery](#9-failure-retry-fallback-và-recovery)
10. [PAUSE, WAITING, BLOCKED, FAILED và DONE](#10-pause-waiting-blocked-failed-và-done)
11. [Focused verification và acceptance gates](#11-focused-verification-và-acceptance-gates)
12. [Những gì runtime phải enforce ngoài prompt](#12-những-gì-runtime-phải-enforce-ngoài-prompt)
13. [Research: coding-agent repositories](#13-research-coding-agent-repositories)
14. [Research: multi-agent orchestration](#14-research-multi-agent-orchestration)
15. [Research: durable workflows và incident response](#15-research-durable-workflows-và-incident-response)
16. [Research: capability và tool routing](#16-research-capability-và-tool-routing)
17. [Ma trận pattern được chọn](#17-ma-trận-pattern-được-chọn)
18. [Các pattern bị loại](#18-các-pattern-bị-loại)
19. [Full prompt catalog — bản tham khảo tiếng Việt](#19-full-prompt-catalog--bản-tham-khảo-tiếng-việt)
20. [Correction prompt catalog](#20-correction-prompt-catalog)
21. [Dynamic envelopes và report templates](#21-dynamic-envelopes-và-report-templates)
22. [Ví dụ đúng và sai](#22-ví-dụ-đúng-và-sai)
23. [Migration checklist](#23-migration-checklist)
24. [Câu hỏi còn mở](#24-câu-hỏi-còn-mở)
25. [Source index](#25-source-index)

---

# 1. Mục tiêu và phạm vi

Tài liệu này phục vụ bốn mục tiêu:

1. Nghiên cứu workflow và prompt contract công khai từ các repository/framework đáng tin cậy.
2. Đánh giá khách quan pattern nào phù hợp với CDPA và pattern nào không nên sao chép.
3. Đề xuất một mô hình prompt/workflow nhỏ, bền vững, capability-aware và giữ exactly-once safety.
4. Cung cấp một bộ prompt tiếng Việt đầy đủ để tham khảo trước khi quyết định chỉnh các file active như:
   - `prompts/cdpa/BASE_CONTEXT.md`;
   - `PLAN.md`;
   - `DEV.md`;
   - `REVIEW.md`;
   - `TEST.md`;
   - `AUDIT.md`;
   - `INDEPENDENT_RULE.md`.

## Ngoài phạm vi

Tài liệu này không:

- sửa prompt active;
- sửa worker/runtime;
- tạo capability registry mới;
- thay manifest hiện tại bằng LangGraph, Temporal hoặc framework khác;
- quyết định migration production;
- chứng minh mọi đề xuất đã phù hợp với live ChatGPT UI;
- cấp authority cho agent dùng tool, endpoint hoặc external service nào.

---

# 2. Tóm tắt điều hành

## 2.1 Kết luận chính

Thiết kế phù hợp nhất cho CDPA không phải “agent tự tổ chức như công ty”, không phải free-form group chat, và cũng không phải generic graph framework. Nó là:

- một durable control plane nhỏ;
- topology/allowed transitions do worker sở hữu;
- role contract hẹp;
- prompt projection tối thiểu;
- runtime capability được quảng bá theo turn;
- exact execution identity;
- typed repair/correction;
- independent evidence cho REVIEW/TEST/AUDIT;
- bounded recovery không replay side effect.

## 2.2 Năm lớp cần tách

Một prompt tốt phải tách:

1. **Durable policy** — invariant ổn định.
2. **Role contract** — role làm gì, không làm gì.
3. **Current task/turn context** — goal, handoff, expected report, routes.
4. **Runtime capability truth** — tool/capability/target hiện hành.
5. **Output contract** — report và typed route được runtime validate.

## 2.3 Root cause tổng quát của lỗi legacy `mcp-thinkbook`

Lỗi không chỉ là connector cũ không ổn định. Root cause khái niệm là:

- tên provider/tool cụ thể nằm lâu dài trong task prose;
- regex nâng literal cũ thành authority hiện hành;
- capability, provider, runtime tool và exact target bị nhập làm một;
- path/OS từng được dùng để suy tool authority;
- discovery có thể mutate UI/composer;
- stale tool failure không có typed capability-resolution path;
- role retry cùng connector rồi PAUSE thay vì resolve capability hiện hành.

Hướng đúng:

- runtime discovery read-only;
- capability và exact target là đầu vào typed;
- provider/tool chỉ là implementation candidate;
- historical tool name không có authority;
- fallback phải policy-filtered và target-preserving;
- accepted/ambiguous mutation khóa fallback và replay.

---

# 3. Hiện trạng CDPA và vấn đề cần giải quyết

## 3.1 Những điểm hiện tại đang đúng

- Role prompt ngắn.
- PLAN/DEV/REVIEW/TEST/AUDIT có trách nhiệm tương đối rõ.
- DONE thuộc PLAN.
- PAUSE được coi là exceptional.
- Report path và route JSON rõ.
- Worker sở hữu machine state.
- Exactly-once Send và ambiguous-side-effect safety đã là invariant trung tâm.
- Prompt mới tránh serialize full roster/manifest/transport state.
- Current direction ưu tiên một owner, một live path, deletion/consolidation.

## 3.2 Những điểm đang trộn lẫn

### Tool literal và authority

Một literal như `@mcp-thinkbook` có thể chỉ là:

- current explicit requirement;
- preferred provider;
- historical hint;
- negative example;
- legacy report text;
- connector cần thay thế.

Regex không phân biệt được các trường hợp trên.

### Target và provider

`ThinkBook` có thể là exact physical target, trong khi `mcp-thinkbook` chỉ là provider alias. Target có thể vẫn đúng dù provider đổi thành MCP Device hoặc worker/API path khác.

### Capability và tool

`filesystem.read_write` là capability. `mcp-thinkbook.write_file` là một runtime implementation. Capability không nên chết chỉ vì một tool name biến mất.

### Pause và capability failure

“Tool X không có” không tự động là external/manual prerequisite. Trước PAUSE cần xác định:

- capability nào thực sự bắt buộc;
- exact target có bắt buộc không;
- candidate nào hiện được authorize;
- failure là discovery stale, provider unavailable, auth required, target mismatch hay ambiguous execution.

## 3.3 Xung đột tài liệu liên quan

Các audit trước đã chỉ ra những loại drift cần tránh:

- prompt source và packaged prompt không mirror tuyệt đối;
- historical plan/research trông giống current policy;
- `.hcu/rules.md` được tuyên bố là durable policy owner nhưng review precedence có thể xếp `AGENTS.md` lên trước;
- standalone workflow docs có wording trái exactly-once policy;
- `LEARNING.md` có thể giữ guidance của kiến trúc đã tạm disable.

Tài liệu này vì vậy phải tự ghi rõ trạng thái “reference only”.

---

# 4. Tiêu chí của một workflow/prompt phù hợp với CDPA

Một workflow/prompt phù hợp phải đạt các tiêu chí sau.

## 4.1 Correctness

- Giữ exactly-once logical request.
- Phân biệt `not_applied`, `confirmed_applied`, `ambiguous`.
- Không retry external mutation chỉ vì timeout/disconnect.
- Exact target đã bind thắng provider preference.
- Resume tiếp tục exact execution; không tạo execution mới rồi gọi đó là resume.

## 4.2 Ownership

- Một concept có một owner.
- Worker sở hữu machine state và route application.
- Role sở hữu report/reasoning và task-owned changes theo authority.
- PLAN không đồng thời là implementation worker.
- REVIEW/TEST/AUDIT không âm thầm sửa source.

## 4.3 Capability awareness

- Tool inventory đến từ runtime hiện tại.
- Role không suy host/provider từ path hoặc OS.
- Model chỉ chọn trong candidate set đã policy-filter.
- Tool/provider substitution không được tự ý đổi target hoặc credential scope.

## 4.4 Durability

- Handoff mang exact identity và report path.
- Retry/recovery counters thuộc worker state, không thuộc prose.
- Accepted provenance sống qua restart.
- Progress metadata không thay thế request identity.

## 4.5 Bounded execution

- Semantic completion trước.
- No-progress detection.
- Bounded correction/retry.
- Hard cap chỉ là safety net, không chứng minh success.

## 4.6 Prompt economy

- Stable policy gửi một lần mỗi conversation generation.
- Turn sau chỉ gửi delta/handoff cần thiết.
- Không lặp full manifest, skill catalog, team roster hoặc old transcript.

---

# 5. Bản đồ authority đề xuất

Thứ tự tham khảo:

1. Explicit current user/operator instruction.
2. Typed current task và capability state.
3. Accepted current spec/contract được task chỉ rõ.
4. `.hcu/rules.md` — durable engineering policy.
5. `AGENTS.md` — bootstrap/orientation và repository safety entry point.
6. Current role contract.
7. Current handoff/report evidence.
8. README — operator usage.
9. Historical plans/reviews/reports — evidence only.
10. Legacy task prose/tool literals — non-authoritative nếu chưa được current task tái xác nhận.

## Quy tắc xung đột

- Current explicit requirement thắng historical hint.
- Typed runtime state thắng path heuristic.
- Exact accepted execution thắng placement preference.
- Durable receipt thắng hop label.
- Current source/tests thắng stale handoff claim.
- Machine state không được thay đổi chỉ từ free-form report text.

---

# 6. Mô hình identity và capability

## 6.1 Các identity cần tách

```text
logical_task_id        task/team identity
logical_request_id     durable request identity
execution_attempt_id   attempt/runtime identity
endpoint_id            browser/account/capability endpoint
page_id                replaceable runtime page metadata
conversation_id        external conversation identity
capability_id          semantic ability required
provider_id            MCP/local/worker provider
runtime_tool_id        exact advertised tool for this turn
target_id              exact workspace/page/account/resource
credential_scope       authorization audience/scope
invocation_id          one exact tool/action call
report_identity        expected path + hash/size/provenance
```

## 6.2 Bốn khái niệm không được nhập làm một

### Required capability

Ví dụ:

- repository.read;
- repository.write;
- shell.execute;
- browser.observe;
- browser.mutate;
- test.execute.

### Preferred provider

Lựa chọn mềm trước binding. Có thể đổi nếu policy cho phép và capability/target giữ nguyên.

### Exact target

Ví dụ:

- exact Windows workspace;
- exact browser endpoint/account;
- exact page/conversation;
- exact durable request.

Exact target không phải preference.

### Historical hint

Ví dụ:

- `@mcp-thinkbook` từng được dùng;
- endpoint từng chạy role này;
- previous speaker;
- path từng map vào host nào đó.

Historical hint chỉ hỗ trợ ranking; không cấp authority.

## 6.3 Capability binding tham khảo

```yaml
resolution_id: capres-...
capability_id: filesystem.read_write
provider_id: <provider-id>
runtime_tool_id: mcp-device.write_file
target_id: E:\git-project\playwright-auto-multicdp
credential_scope: device-session-...
catalog_generation: 42
authorization_grant_id: grant-...
operation_class: mutating
revalidate_on:
  - catalog_change
  - endpoint_reconnect
  - target_change
```

---

# 7. Phân lớp prompt

## 7.1 Stable durable policy

Owner: `.hcu/rules.md`.

Nội dung:

- exactly-once;
- ownership;
- workspace/Git safety;
- complexity budget;
- verification discipline;
- external side-effect rules.

Không copy nguyên vào mọi role prompt.

## 7.2 Shared base context

Owner: `BASE_CONTEXT.md` hoặc prompt projection tương đương.

Chỉ chứa:

- source-of-truth hierarchy;
- role/workflow boundary chung;
- capability/target distinction;
- PAUSE rule chung;
- no replay/no arbitrary substitution;
- report/route requirement chung.

## 7.3 Role contract

Owner: `PLAN.md`, `DEV.md`, v.v.

Mỗi role chỉ trả lời:

- tôi sở hữu quyết định nào;
- tôi không sở hữu quyết định nào;
- input authoritative nào;
- output evidence nào;
- route hợp lệ nào.

## 7.4 Dynamic turn envelope

Owner: worker/control plane.

Mang:

- exact identity;
- current goal hoặc handoff delta;
- workspace;
- capability snapshot/delta;
- expected report path;
- allowed routes;
- current exact blocker nếu có.

## 7.5 Output contract

Runtime validate:

- route schema;
- destination allowlist;
- exact report path;
- artifact exists/non-empty;
- role authority;
- DONE authority;
- checkpoint requirements.

---

# 8. Routing graph và trách nhiệm role

## 8.1 Graph mặc định

```text
PLAN -> DEV -> REVIEW -> PLAN -> DONE
```

TEST hoặc AUDIT chỉ thêm khi có boundary cụ thể.

Graph tham khảo mở rộng:

```text
PLAN
  -> DEV
      -> REVIEW
          -> DEV              khi có defect
          -> TEST             khi cần executable acceptance
          -> PLAN             khi clean và đủ evidence
      -> TEST                 khi boundary độc lập đã rõ

TEST
  -> DEV                      khi defect tái hiện
  -> PLAN                     khi pass
  -> PAUSE/BLOCKED            khi exact external prerequisite

AUDIT
  -> DEV                      implementation defect
  -> PLAN                     clean evidence
  -> BLOCKED/RECOVERY_REQUIRED provenance không đủ

PLAN
  -> DONE                     acceptance đầy đủ
  -> DEV                      còn work cụ thể
  -> PAUSE                    external/manual prerequisite
```

## 8.2 Vai trò

| Role | Sở hữu | Không sở hữu |
|---|---|---|
| PLAN | scope, dependencies, acceptance, next route, DONE | source mutation, independent approval |
| DEV | implementation, focused self-check | final independent review |
| REVIEW | correctness/coverage/overengineering review | source mutation |
| TEST | executable verification | architecture redesign, silent fix |
| AUDIT | provenance/cross-cutting safety | implementation và manifest mutation |
| Recovery | exact execution reconciliation | replay original side effect, scope expansion |

---

# 9. Failure, retry, fallback và recovery

## 9.1 Failure taxonomy

```text
DISCOVERY_STALE
CANDIDATE_UNAVAILABLE
AUTH_REQUIRED
CAPABILITY_MISSING
TARGET_MISMATCH
IDENTITY_MISMATCH
EXECUTION_NOT_APPLIED
EXECUTION_CONFIRMED_APPLIED
EXECUTION_AMBIGUOUS
CONTRACT_INVALID
ENVIRONMENT_FAILURE
IMPLEMENTATION_DEFECT
NO_PROGRESS
BUDGET_EXHAUSTED
```

## 9.2 Capability resolution

1. Chuẩn hóa current requirement thành capability + target + operation class.
2. Lấy runtime catalog read-only.
3. Filter theo authorization, endpoint, target, credential scope, operation class.
4. Chứng minh target equivalence nếu có alternate provider.
5. Rank candidate đã được phép.
6. Bind exact candidate.
7. Execute.
8. Classify outcome.
9. Fallback chỉ khi policy và boundary state cho phép.
10. Hết candidate mới WAIT/BLOCK/PAUSE.

## 9.3 Fallback gate

Fallback chỉ hợp lệ nếu:

- failure class cho phép;
- previous attempt proven not applied hoặc operation idempotent tại owner;
- target equivalence được chứng minh;
- alternate provider được authorize;
- credential scope phù hợp;
- chưa có accepted/ambiguous provenance khóa execution cũ.

## 9.4 Recovery loop

```text
Bind exact identity
  -> Read durable state
  -> Observe exact target read-only
  -> Classify outcome
  -> Choose at most one safe action
  -> Verify postcondition
  -> Persist result
  -> Stop or return control
```

Không tạo recovery architecture song song.

## 9.5 Timeout taxonomy

Phải phân biệt:

- admission/capacity timeout;
- schedule/start timeout;
- attempt execution timeout;
- operational stall timeout;
- overall response budget;
- recovery budget.

Một timeout không nói action đã áp dụng hay chưa.

---

# 10. PAUSE, WAITING, BLOCKED, FAILED và DONE

| State/outcome | Ý nghĩa | Resume | Replay side effect |
|---|---|---|---|
| DONE | Acceptance đầy đủ | Không | Không |
| WAITING | Dependency/capability transient chưa sẵn sàng | Tự động/cùng identity | Không nếu accepted/ambiguous |
| PAUSED | Chờ exact operator/external prerequisite | Khi predicate cụ thể đúng | Không |
| BLOCKED | Không có safe automatic progress path | Cần control/human action | Không |
| RETRYABLE_FAILURE | Safe retry đã được chứng minh | Bounded retry | Chỉ khi not-applied/idempotent |
| FAILED | Terminal failure | Không tự động | Không |
| CHANGES_REQUIRED | Independent evidence trả work về owner | Explicit route | Không replay old turn |

## PAUSE hợp lệ phải có

- `block_code`;
- exact missing prerequisite;
- attempts/candidates đã kiểm tra;
- lý do alternate path không hợp lệ;
- `resume_condition` có thể quan sát được;
- exact identity phải giữ.

## PAUSE không hợp lệ

- tool literal cũ không tồn tại nhưng capability khác vẫn có;
- test chưa chạy vì role chưa thử current authorized path;
- “cần thêm thời gian”;
- “không chắc” nhưng vẫn có read-only evidence path;
- provider preference không khả dụng trước binding nhưng có equivalent authorized provider.

---

# 11. Focused verification và acceptance gates

## 11.1 Focused verification

Trình tự:

1. exact regression/symptom;
2. focused subsystem suite;
3. boundary integration tests;
4. broad/full suite khi impact path biện minh.

Focused suite không thay full suite; nó trả lời câu hỏi hẹp hơn và tăng tốc diagnosis.

## 11.2 Gate tốt phải trả lời

1. Invariant nào được bảo vệ?
2. Exact target nào được kiểm tra?
3. Evidence pass/fail là gì?
4. Failure có chặn downstream không?
5. Ai hoặc predicate nào giải phóng gate?

## 11.3 Gate tham khảo

- Send acceptance gate.
- Report artifact gate.
- Checkpoint gate.
- Recovery reconciliation gate.
- Done/original-transition gate.

Không dùng “all green” hoặc endpoint HEALTHY chung chung làm gate.

---

# 12. Những gì runtime phải enforce ngoài prompt

Prompt không đủ cho:

- tool allowlist;
- read-only role enforcement;
- route destination validation;
- report path validation;
- checkpoint/commit authority;
- exact request receipt;
- accepted/ambiguous non-replay;
- endpoint/account binding;
- capability admission;
- credential scope;
- retry count/backoff;
- stuck detection;
- human approval identity;
- destructive cleanup postcondition.

Nguyên tắc:

> Prompt giải thích responsibility. Runtime enforce authority và irreversible safety.

---

# 13. Research: coding-agent repositories

Mỗi mục dùng ba nhãn:

- **[SOURCE]**: behavior/tài liệu của repo nguồn.
- **[CDPA FIT]**: mức phù hợp.
- **[PROPOSAL]**: bài học đề xuất, chưa là policy.

## 13.1 OpenHands

- Repo: https://github.com/All-Hands-AI/OpenHands
- Commit agent-core công khai khảo sát: `d39f7ae0e66174c50bdc714304fed4078b5e3b72`
- Files:
  - `openhands/agenthub/codeact_agent/prompts/system_prompt.j2`
  - `openhands/agenthub/readonly_agent/prompts/system_prompt.j2`
  - `openhands/controller/agent_controller.py`
  - `openhands/controller/stuck.py`

**[SOURCE]** ReadOnlyAgent công bố rõ allowed tools và restrictions. Controller sở hữu lifecycle/pending action/stuck detection. Tool không đăng ký trả typed error.

**[CDPA FIT]** Phù hợp cao cho REVIEW/AUDIT read-only và controller-level loop detection. Không phù hợp để copy prompt lớn hoặc reset/retry generic sau browser side effect.

**[PROPOSAL]** REVIEW/AUDIT phải bị hạn chế tools ở runtime. Wrong tool trả correction hẹp. Worker phát hiện repeated action/error/route fingerprints.

## 13.2 SWE-agent

- Repo: https://github.com/SWE-agent/SWE-agent
- Commit: `3ea751c087f32b16e039a2233dd6eefecef325d5`
- Files:
  - `config/default.yaml`
  - `sweagent/agent/agents.py`
  - `sweagent/agent/reviewer.py`
  - `sweagent/tools/tools.py`

**[SOURCE]** Tách system/task/observation/error templates. Malformed format, blocked action, syntax error, timeout có correction riêng và `max_requeries`.

**[CDPA FIT]** Rất phù hợp cho route/report/capability repair. Whole-attempt hard reset không phù hợp sau accepted/ambiguous Send.

**[PROPOSAL]** Mỗi repair prompt chỉ mang exact validation error và contract cần sửa; không resend goal gốc.

## 13.3 Aider

- Repo: https://github.com/Aider-AI/aider
- Commit: `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`
- Files:
  - `aider/coders/architect_prompts.py`
  - `aider/coders/architect_coder.py`
  - `aider/coders/base_prompts.py`

**[SOURCE]** Architect tạo instruction; Editor sửa. Current file content được đánh dấu authoritative. Lint/test output quay lại bounded reflection.

**[CDPA FIT]** Phù hợp cao cho PLAN → DEV separation. Auto model fallback và auto-commit không nên copy nguyên.

**[PROPOSAL]** PLAN nêu owner/files/invariants/tests/stop condition; DEV reread current source và không coi handoff là file truth.

## 13.4 OpenAI Codex CLI

- Repo: https://github.com/openai/codex
- Commit: `e72da2b53805894878023d01949a25a082e0a5cb`
- Files:
  - `codex-rs/core/src/agent/role.rs`
  - `codex-rs/core/src/agent/child_config.rs`
  - `codex-rs/core/src/context/world_state/tools.rs`
  - `codex-rs/core/assets/agent/builtins/awaiter.toml`

**[SOURCE]** Role có thể giảm capability nhưng không thay parent authority. Child refresh live provider/cwd/sandbox/approval. Deferred tool namespaces gửi snapshot/delta. Unknown role fail explicit.

**[CDPA FIT]** Một trong các nguồn phù hợp nhất.

**[PROPOSAL]** Role authority là giao của task authority, endpoint capability và role contract. Tool state phải là live turn state, không phải persisted prose.

## 13.5 Claude Code public plugins/examples

- Repo: https://github.com/anthropics/claude-code
- Commit: `7779afb12e3635f46f56ec823979d68350ae000b`
- Files:
  - `plugins/feature-dev/agents/code-explorer.md`
  - `plugins/feature-dev/agents/code-architect.md`
  - `plugins/feature-dev/agents/code-reviewer.md`
  - `plugins/pr-review-toolkit/agents/pr-test-analyzer.md`
  - `examples/hooks/bash_command_validator_example.py`

**[SOURCE]** Agent markdown có name/trigger/tools/model/contract. Hooks có thể block wrong tool và trả actionable feedback.

**[CDPA FIT]** Phù hợp cho role metadata, confidence threshold và enforcement ngoài prompt. Core runtime không public đầy đủ nên không suy diễn quá mức.

**[PROPOSAL]** Review finding cần threshold/evidence. Tool mismatch nên bị runtime correction trước side effect.

## 13.6 Cline

- Repo: https://github.com/cline/cline
- Commit: `29896ec7fa8e2dd98805b56c2dae987d59fc9baa`
- Files:
  - `sdk/packages/core/src/extensions/tools/team/subagent-prompts.ts`
  - `.../multi-agent.ts`
  - `.../team-tools.ts`
  - `.../projections.ts`

**[SOURCE]** Durable-ish task/run states, block reason, retry count/backoff, dependency, mission log và recovery.

**[CDPA FIT]** Hữu ích cho typed blocked/interrupted state. Requeue original message không an toàn cho browser Send.

**[PROPOSAL]** Retry/backoff thuộc worker state. Recovery prompt phải tách khỏi original task khi request đã accepted/ambiguous.

---

# 14. Research: multi-agent orchestration

## 14.1 LangGraph

- Repo: https://github.com/langchain-ai/langgraph
- Commit: `7daa3ab49d678a5da75edb08baa87db4a2be52c3`
- Files:
  - `libs/checkpoint/README.md`
  - `libs/langgraph/langgraph/types.py`
  - `libs/langgraph/langgraph/_internal/_retry.py`

**[SOURCE]** Explicit graph, checkpoint/thread identity, interrupt ID, pending writes, retry classification.

**[CDPA FIT]** Rất phù hợp về nguyên tắc; không nên thêm graph/checkpointer runtime thứ hai.

**[PROPOSAL]** Typed transitions, exact resume identity, retry theo failure class, không rerun work đã hoàn tất.

## 14.2 Microsoft AutoGen

- Repo: https://github.com/microsoft/autogen
- Commit: `027ecf0a379bcc1d09956d46d12d44a3ad9cee14`
- Files:
  - selector, swarm, round-robin group chat implementations;
  - termination conditions;
  - state schemas.

**[SOURCE]** Có deterministic routing, selector và handoff modes; save/load team state; termination composition; max attempts.

**[CDPA FIT]** Phù hợp để phân biệt routing modes. Fallback chọn previous/first speaker không an toàn cho CDPA.

**[PROPOSAL]** Deterministic edge mặc định; model choice chỉ trong allowlist và không silent fallback.

## 14.3 OpenAI Agents SDK

- Repo: https://github.com/openai/openai-agents-python
- Commit: `588826c5be27cad21a3067463e21972ffea38561`
- Docs:
  - `docs/running_agents.md`
  - `docs/handoffs.md`
  - `docs/sessions/index.md`
  - `docs/human_in_the_loop.md`

**[SOURCE]** Typed handoff destination, context filters, application state tách metadata, exact interruption identity, serializable RunState, fail-closed approval.

**[CDPA FIT]** Rất phù hợp cho handoff và PAUSE/Resume semantics.

**[PROPOSAL]** Một handoff per allowed destination; route payload nhỏ; durable identity không do model sinh; resume same state/session.

## 14.4 CrewAI

- Repo: https://github.com/crewAIInc/crewAI
- Commit: `4ed2abc7bbf504a634d3b733f2a97e0fbe8d44ec`

**[SOURCE]** Sequential/hierarchical process; manager không đồng thời là worker; bounded iterations/retries; replay identity validation.

**[CDPA FIT]** Manager-worker separation hữu ích. Persona/backstory dài và general replay không phù hợp.

**[PROPOSAL]** PLAN không làm DEV; identity validation trước recovery/replay.

## 14.5 MetaGPT

- Repo: https://github.com/FoundationAgents/MetaGPT
- Commit: `11cdf466d042aece04fc6cfd13b28e1a70341b1f`

**[SOURCE]** SDLC roles và artifact/action subscriptions; budget/round caps; recovery state.

**[CDPA FIT]** Hữu ích cho context slicing. Engineer self-review không thay independent REVIEW.

## 14.6 ChatDev/DevAll

- Repo: https://github.com/OpenBMB/ChatDev
- Commits: `31fd994...` và `4fb2db0...`

**[SOURCE]** Phase graph, bounded cycles, reviewer comment → programmer modification, graph engine ở bản mới.

**[CDPA FIT]** Phase separation có ích; prompt duplication, marker text completion và arbitrary graph complexity không phù hợp.

## 14.7 OpenAI Swarm

- Repo: https://github.com/openai/swarm
- Commit: `6af0b4caf37dca4526dfd98e9fbd8ce36e7eeb22`

**[SOURCE]** Agent + handoff primitives, stateless, educational, superseded.

**[CDPA FIT]** Chỉ hữu ích như lời nhắc giữ machinery nhỏ; không dùng production.

---

# 15. Research: durable workflows và incident response

## 15.1 Temporal

- Repo docs: https://github.com/temporalio/documentation
- Relevant docs:
  - `docs/encyclopedia/activities/activity-definition.mdx`
  - `activity-execution.mdx`
  - retry policies;
  - Python activity timeouts;
  - workflow/run identity.

**[SOURCE]** Workflow tách Activity side effect; activities có thể execute nhiều lần; timeout taxonomy; heartbeat; idempotency tại target service; workflow ID khác run ID.

**[CDPA FIT]** Nguồn mạnh nhất cho exactly-once reality và timeout semantics.

**[PROPOSAL]** Không có receipt không đồng nghĩa not-applied. Timeout phải ghi phase. Business identity khác attempt identity.

## 15.2 Prefect

- Repo: https://github.com/PrefectHQ/prefect
- Relevant docs: states, retry flow runs, transactions.

**[SOURCE]** Failed vs Crashed vs Paused/Suspended; manual retry giữ logical run; local rollback không undo external side effect.

**[CDPA FIT]** Hữu ích cho failure ownership và pause semantics.

## 15.3 Dagster

- Repo: https://github.com/dagster-io/dagster
- Relevant docs: sensors, run keys/cursors, asset checks, backfills.

**[SOURCE]** Run key chống duplicate; cursor chỉ progress; focused checks; blocking/non-blocking; exact partition repair.

**[CDPA FIT]** Rất phù hợp cho request identity vs progress cursor và focused verification.

## 15.4 Kubernetes

- Repo docs: https://github.com/kubernetes/website
- Relevant docs: controller pattern, Jobs, finalizers.

**[SOURCE]** Desired vs current reconciliation; scope-aware failures; finalizer postcondition; suspend semantics.

**[CDPA FIT]** Rất phù hợp cho recovery loop, nhưng không nên thêm CRD/operator abstraction.

## 15.5 GitHub Actions

- Repo docs: https://github.com/github/docs
- Relevant docs: workflow syntax, concurrency, environments.

**[SOURCE]** Explicit dependency, scoped continue-on-error, timeout scopes, human approval, concurrency replacement risks.

**[CDPA FIT]** Hữu ích cho explicit gates và supersession safety.

## 15.6 Google SRE

- https://sre.google/sre-book/managing-incidents/
- https://sre.google/workbook/incident-response/

**[SOURCE]** Incident Commander, Operations, Communications, live state document, explicit handoff, mitigation vs durable repair.

**[CDPA FIT]** PLAN tương ứng commander; DEV resolver; reports/manifest tách machine/human evidence.

## 15.7 PagerDuty Incident Response

- Repo: https://github.com/PagerDuty/incident-response-docs
- Relevant docs: roles, during incident, anti-patterns, postmortems.

**[SOURCE]** Commander không phải resolver; SMEs không freelance; scribe/timeline; tránh heroics/tunnel vision.

**[CDPA FIT]** Rất phù hợp cho role boundaries và incident report discipline.

---

# 16. Research: capability và tool routing

## 16.1 Model Context Protocol

- Repo: https://github.com/modelcontextprotocol/modelcontextprotocol
- Commit: `ab3a39c13bd23be691c2760e1c6c5c15a64582e1`
- Docs: tools, resources, client best practices, security/authorization.

**[SOURCE]** `tools/list`, optional `list_changed`, dynamic inventory, protocol vs execution errors, resources URI, client confirmation, OAuth audience binding.

**[CDPA FIT]** Ủng hộ runtime discovery và target/tool separation. MCP không định nghĩa semantic fallback.

## 16.2 OpenAI Agents SDK tool identity

- Commit: `588826c...`
- Files:
  - `src/agents/_tool_identity.py`
  - `src/agents/run_internal/tool_execution.py`
  - `.agents/references/tool-identity.md`

**[SOURCE]** Public, qualified, lookup, approval, call identities tách biệt; tools enabled per run.

**[CDPA FIT]** Nguồn mạnh nhất cho identity layering.

## 16.3 Semantic Kernel

- Repo: https://github.com/microsoft/semantic-kernel
- Commit: `ca40aa7226531d28a721d0ca0e451d0aaf86dafc`

**[SOURCE]** Auto/Required/None function choice; plugin/function filters; qualified identity; missing function raises.

**[CDPA FIT]** Runtime-filtered candidate set hữu ích. Bare-name first-match không an toàn.

## 16.4 Google ADK

- Repo: https://github.com/google/adk-python
- Commit: `044a1ec3f434cf2e3f7acfdbd52305b60d16f6e5`

**[SOURCE]** Toolsets resolve per invocation; filters/prefix; failed toolset có thể isolated; tool-not-found trả current list; auth/confirmation.

**[CDPA FIT]** Mẫu tốt cho “toolset fail không pause toàn agent”, nhưng required capability không được âm thầm drop.

## 16.5 AWS Strands Agents

- Commit khảo sát: `15da9dca2cd6f870684fdc27a2498bebbd00b62d`

**[SOURCE]** Registry/provider, dynamic MCP refresh, agent-facing alias tách server tool name, collision checks, explicit continue-on-error/retry hooks.

**[CDPA FIT]** Alias/remote identity separation hữu ích. Generic retry hook không dùng cho ambiguous mutation.

## 16.6 LangChain fallbacks

- Repo: https://github.com/langchain-ai/langchain
- Commit: `80b74090515a594cc8421fb2d82e98f4e256c29f`
- Files: `runnables/fallbacks.py`, configurable alternatives.

**[SOURCE]** Explicit ordered alternatives và exception filter.

**[CDPA FIT]** Provider fallback phải preconfigured. Generic `Exception` fallback quá rộng.

---

# 17. Ma trận pattern được chọn

| Pattern | Nguồn chính | Áp dụng CDPA |
|---|---|---|
| Role chỉ giảm authority | Codex CLI | Role contract/runtime tool scope |
| Typed handoff, exact interruption | OpenAI Agents SDK | Route/PAUSE/Resume |
| Explicit transitions/checkpoint identity | LangGraph | Worker state, không thêm framework |
| Side effect vs workflow decision | Temporal | PLAN/DEV/worker boundary |
| Dynamic tool listing | MCP, ADK, Strands | Runtime capability truth |
| Bounded correction prompt | SWE-agent | Route/report/tool repair |
| Architect/editor separation | Aider | PLAN/DEV |
| Read-only role enforcement | OpenHands, Claude plugins | REVIEW/TEST/AUDIT |
| Durable blocked/interrupted reasons | Cline, Prefect | Worker state |
| Focused check/gate | Dagster | TEST/acceptance |
| Reconcile desired/current | Kubernetes | Recovery |
| Commander != resolver | Google SRE, PagerDuty | PLAN/DEV |
| Run key != progress cursor | Dagster | Request vs hop/progress |

---

# 18. Các pattern bị loại

- Whole-attempt hard reset/retry sau external side effect.
- Free-form group chat làm control plane.
- Arbitrary next-agent selection.
- Silent fallback role/model/provider/tool.
- Tool name hardcoded theo OS/path.
- Probe availability bằng composer mutation.
- Persona/backstory dài thay cho contract.
- Marker text như `TERMINATE` làm state authority.
- Fixed N review cycles bất kể semantic state.
- Full transcript gửi cho mọi role.
- Developer self-review được coi là independent approval.
- Generic transaction/rollback claim cho external side effects.
- Graph/checkpointer/service thứ hai khi manifest/worker hiện tại đủ.
- Automatic dependency install nếu task/runtime chưa authorize.

---

# 19. Minimal production role contracts — bản tham khảo tiếng Việt

> Các candidate dưới đây chỉ định danh vai trò, yêu cầu dùng các skill liên quan và quy tắc route. Phương pháp planning, coding, review, testing và audit thuộc skill được load tại runtime cùng `.hcu/rules.md`; không lặp lại trong role prompt.
>
> Tên MCP/provider không nằm trong role contract. User cung cấp authority cần thiết trong Goal của turn 1. Turn sau tiếp tục từ report/handoff đã validate.

## 19.1 Candidate `BASE_CONTEXT.md`

```markdown
# CDPA base context

Current task instructions, durable runtime state, and current repository evidence are authoritative. Historical plans, reports, paths, OS hints, and tool names are evidence only unless the current task explicitly renews them.

Preserve exact execution identity and never replay an accepted or ambiguous irreversible action. Worker owns machine state and route application.
```

## 19.2 Candidate `PLAN.md`

```markdown
# PLAN — scope and workflow decision owner

Own scope, acceptance, dependencies, and next-role choice; do not implement. Discover, load, and use the relevant skills for this responsibility.

Route concrete remaining work to the appropriate allowed role. PAUSE only for an unresolved external/manual prerequisite; only PLAN may route DONE.
```

## 19.3 Candidate `DEV.md`

```markdown
# DEV — implementation owner

Own the task-scoped implementation and focused self-checks. Discover, load, and use the relevant skills for this responsibility.

Route complete work to REVIEW, independently unproven runtime boundaries to TEST, and unresolved external/manual prerequisites to PAUSE. DONE remains PLAN-only.
```

## 19.4 Candidate `REVIEW.md`

```markdown
# REVIEW — independent reviewer

Review the assigned change without editing implementation files. Discover, load, and use the relevant skills for this responsibility.

Route actionable defects to DEV, clean work to PLAN, and independently unproven execution to TEST. PAUSE only for an unresolved external/manual prerequisite; DONE remains PLAN-only.
```

## 19.5 Candidate `TEST.md`

```markdown
# TEST — independent verification owner

Produce executable or observable evidence for the assigned acceptance boundary without editing implementation files. Discover, load, and use the relevant skills for this responsibility.

Route fixable failures to DEV and verified results to PLAN. PAUSE only for an unresolved external/manual prerequisite; DONE remains PLAN-only.
```

## 19.6 Candidate `AUDIT.md`

```markdown
# AUDIT — independent auditor

Audit only the named cross-cutting boundary without implementation or recovery mutation. Discover, load, and use the relevant skills for this responsibility.

Route confirmed source defects to DEV and clean evidence to PLAN. PAUSE only for an unresolved external/manual prerequisite; never route DONE.
```

## 19.7 Candidate `INDEPENDENT_RULE.md`

```markdown
You are the sole agent for one active independent job. Worker/control-plane state owns identity, trigger, lifecycle, and completion controls. Do not return normal workflow routing responses.

Discover, load, and use the relevant skills. Use only authority supplied by the current task/session. Continue or complete only the same active job and target; never replay an accepted or ambiguous external action.
```

## 19.8 Turn projection

Turn 1:

```text
ROLE / TASK / TURN
WORKSPACE
GOAL (bao gồm MCP/tool authority do user cung cấp nếu có)
HANDOFF
MINIMAL ROLE CONTRACT
EXPECTED REPORT
ALLOWED ROUTES
```

Turn 2+:

```text
ROLE / TASK / TURN
VALIDATED PREVIOUS REPORT OR HANDOFF
CHANGED RECOVERY/BLOCKER FACTS, nếu có
EXPECTED REPORT
ALLOWED ROUTES
```

Không lặp goal, role contract, base context, skill catalog hoặc tool guidance trong turn 2+.

# 20. Correction prompt catalog

## 20.1 Malformed route correction

```markdown
ROUTE_REPAIR

Previous response did not satisfy the route contract.
Validation error: <exact bounded error>
Allowed routes: <allowlist>
Expected report path: <exact path>

Do not repeat the original task, implementation, or external action. Ensure the existing report artifact is valid, then return exactly one valid route object.
```

## 20.2 Invalid report correction

```markdown
REPORT_REPAIR

The route decision is locked: <route>.
The handoff path is locked: <exact path>.
Report validation error: <exact bounded error>.

Repair only the report artifact. Do not change implementation, route, target, request, conversation or external state. Then return the locked route object.
```

## 20.3 Stale tool correction

```markdown
CAPABILITY_CORRECTION — STALE TOOL

The requested historical tool name is not present in the current authorized runtime catalog.
Historical name: <name>
Required capability: <capability>
Exact target: <target>
Current authorized candidates: <list or none>

Historical tool names are non-authoritative. Select only a current candidate that preserves exact target, credential scope and side-effect semantics. If none exists, report CAPABILITY_MISSING; do not invent or probe a provider through UI mutation.
```

## 20.4 Capability unavailable

```markdown
CAPABILITY_UNAVAILABLE

Required capability: <capability>
Exact target: <target>
Discovery generation: <generation>
Candidates checked: <list>
Failure classes: <typed list>

Continue only work that does not require this capability. Do not silently replace it with a weaker capability or different target. If completion requires it, report the exact blocker and control-plane/operator action needed.
```

## 20.5 Exact target mismatch

```markdown
TARGET_MISMATCH

The available provider/tool does not prove access to the exact authorized target.
Required target: <target>
Observed target: <observed>

Do not proceed, translate paths heuristically, or substitute another host/account/workspace. Preserve current identity and report the evidence required to establish target equivalence.
```

## 20.6 Timeout/stall correction

```markdown
TIMEOUT_CLASSIFICATION

Classify the timeout before choosing an action:
- admission/capacity;
- attempt execution;
- operational stall;
- overall result budget;
- recovery budget.

State whether the external/irreversible boundary began and whether outcome is not_applied, confirmed_applied or ambiguous. Do not retry an ambiguous action. Use only the bounded observation/recovery action authorized for this timeout class.
```

## 20.7 Ambiguous side effect

```markdown
AMBIGUOUS_EXTERNAL_ACTION

The action may have been applied. Preserve exact request and target identity.

Forbidden:
- replay/resend;
- Retry/Regenerate;
- provider/endpoint substitution;
- replacement execution presented as resume.

Required:
- read durable request evidence;
- observe exact target read-only;
- reconcile accepted provenance;
- report HUMAN_REQUIRED/RECOVERY_REQUIRED if neither applied nor not-applied can be proven.
```

---

# 21. Dynamic envelopes và report templates

## 21.1 Dynamic turn envelope tham khảo

```yaml
TURN_CONTEXT:
  task_id: cdpa-idem-...
  team: ...
  logical_role: DEV
  physical_role: DEV
  turn: 2
  workspace: E:\git-project\...
  source_report: .plan/.../plan_turn1_....md
  expected_report: .plan/.../dev_turn2_....md
  allowed_routes: [REVIEW, TEST, PLAN, PAUSE]
  goal_delta: "Implement exact accepted plan delta"

CAPABILITY_CONTEXT:
  catalog_generation: 42
  required:
    - capability: repository.read_write
      exact_target: E:\git-project\...
      candidates:
        - runtime_tool_id: <provider-id>.write_file
          provider_id: <provider-id>
          status: available
  forbidden_substitution:
    - endpoint
    - account
    - workspace
    - credential_scope

BOUNDARY_CONTEXT:
  irreversible_action_status: not_started
  accepted_request_id: null
  ambiguous: false
```

## 21.2 Handoff template

```markdown
# Handoff — <ROLE> turn <N>

## Identity
- Task/team/hop/request:
- Workspace/exact target:
- Source report:

## Outcome
- What was completed:
- What was not completed:

## Evidence
- Source/diff:
- Commands/results:
- Runtime observations:

## Boundary status
- External/irreversible actions:
- Accepted/ambiguous evidence:
- Ownership/binding:

## Remaining work
- Exact next owner:
- Exact next action/decision:
- Required capability:
- Stop/block condition:

## Risks/unknowns
- ...
```

## 21.3 PAUSE report template

```markdown
## PAUSE
- block_code: <typed code>
- blocked_operation: <exact operation>
- required_capability: <capability>
- exact_target: <target>
- candidates_attempted: <list>
- why_alternates_invalid: <reason>
- side_effect_status: not_applied|applied|ambiguous
- identity_preserved: <evidence>
- resume_condition: <observable predicate>
```

## 21.4 REVIEW finding template

```markdown
### <Severity> — <Axis> — <Title>
- Location: `file:line`
- Trigger:
- Actual behavior:
- Expected contract:
- Impact:
- Evidence:
- Smallest complete correction:
```

---

# 22. Ví dụ đúng và sai

## 22.1 Tool unavailable

### Sai

```text
Workspace là Windows, nên dùng @mcp-thinkbook. Tool không có; PAUSE.
```

### Đúng

```text
Required capability là repository.read_write trên exact workspace E:\...
Historical provider @mcp-thinkbook không có trong catalog hiện tại.
Runtime resolver kiểm tra authorized candidates. Nếu <provider-id> được chứng minh cùng exact target và credential scope, dùng binding đó; nếu không, report CAPABILITY_MISSING với resume condition.
```

## 22.2 Timeout sau Send

### Sai

```text
Không thấy receipt, nên gửi lại.
```

### Đúng

```text
Timeout xảy ra sau external boundary; outcome ambiguous. Giữ logical request ID, quan sát exact conversation, adopt accepted provenance nếu tìm thấy; không resend.
```

## 22.3 REVIEW phát hiện lỗi

### Sai

```text
Tôi đã sửa luôn cho nhanh.
```

### Đúng

```text
REVIEW ghi finding với file:line, trigger, impact và route DEV. Runtime tool surface của REVIEW không có edit authority.
```

## 22.4 PLAN thiếu capability

### Sai

```text
Không có tool nên đổi endpoint/account khác.
```

### Đúng

```text
PLAN ghi required capability và exact target. Control plane resolve candidate. Sau binding/accepted execution, placement preference không còn quyền đổi target.
```

## 22.5 Broad tests

### Sai

```text
Chạy full suite trước; timeout; không biết defect ở đâu.
```

### Đúng

```text
Reproduce exact failing invariant, chạy focused subsystem suite, sau đó broad suite theo impact boundary.
```

---

# 23. Migration checklist

## Phase 0 — Chốt contract trước code

- [ ] Chốt authority order.
- [ ] Chốt định nghĩa capability/provider/tool/target.
- [ ] Chốt exact-target và fallback semantics.
- [ ] Chốt PAUSE/WAITING/BLOCKED semantics.
- [ ] Chốt role boundaries.
- [ ] Chốt historical-document status convention.

## Phase 1 — Documentation cleanup

- [ ] Sửa exactly-once contradiction trong standalone workflow docs.
- [ ] Đồng bộ source/package prompts.
- [ ] Scope legacy bootstrap guidance.
- [ ] Làm rõ `.hcu/rules.md` vs `AGENTS.md` ownership.
- [ ] Đánh dấu old plans/research historical/superseded.

## Phase 2 — Prompt-only bounded changes

- [ ] Thay provider-specific wording bằng capability wording.
- [ ] Bổ sung “historical tool names are non-authoritative”.
- [ ] Bổ sung capability resolution trước PAUSE.
- [ ] Làm rõ read-only role contracts.
- [ ] Thêm bounded correction prompts.

## Phase 3 — Runtime enforcement

- [ ] Tool allowlist per role.
- [ ] Runtime capability snapshot/delta.
- [ ] Typed candidate filtering.
- [ ] Stale-tool correction result.
- [ ] Exact-target binding.
- [ ] Retry/fallback gate.
- [ ] Stuck detection.

## Phase 4 — Tests

- [ ] Legacy `mcp-thinkbook` mention không trở thành hard authority.
- [ ] Current explicit provider requirement vẫn được giữ.
- [ ] Negative mention không bị regex nâng thành requirement.
- [ ] Provider unavailable nhưng equivalent capability valid → continue.
- [ ] Alternate target không proven equivalent → block.
- [ ] Accepted/ambiguous mutation → no fallback/replay.
- [ ] REVIEW/AUDIT cannot edit.
- [ ] Route/report repair không resend goal.

## Phase 5 — Live acceptance

- [ ] Read-only discovery không mutate composer.
- [ ] Runtime provider rename/removal được refresh.
- [ ] Exact workspace bytes/target equivalence được chứng minh.
- [ ] No duplicate Send/recovery side effect.
- [ ] Operator-visible blocker/resume condition chính xác.

---

# 24. Câu hỏi còn mở

1. Capability state nên persist ở endpoint record, task record hay chỉ runtime projection?
2. Có cần semantic `capability_id` ổn định, hay bounded mapping hiện hữu đủ?
3. Exact target equivalence được chứng minh bằng path, mount ID, repository fingerprint hay control-plane binding?
4. Historical task text được sanitize/annotate ở create-time hay projection-time?
5. Current explicit tool requirement được biểu diễn bằng typed field nào?
6. WAITING và PAUSE có cần tách durable state hay dùng status + typed blocker?
7. Role tool allowlist có thể enforce ngay bằng current browser/model environment không?
8. Capability discovery có thể hoàn toàn read-only với current ChatGPT/MCP UI không?
9. Provider alias migration cần version/expiry hay có thể xóa legacy alias hoàn toàn?
10. Prompt compactness budget cho turn đầu và continuation nên là bao nhiêu?

---

# 25. Source index

## Coding agents

- OpenHands: https://github.com/All-Hands-AI/OpenHands/tree/d39f7ae0e66174c50bdc714304fed4078b5e3b72
- SWE-agent: https://github.com/SWE-agent/SWE-agent/tree/3ea751c087f32b16e039a2233dd6eefecef325d5
- Aider: https://github.com/Aider-AI/aider/tree/5dc9490bb35f9729ef2c95d00a19ccd30c26339c
- OpenAI Codex: https://github.com/openai/codex/tree/e72da2b53805894878023d01949a25a082e0a5cb
- Claude Code public repo: https://github.com/anthropics/claude-code/tree/7779afb12e3635f46f56ec823979d68350ae000b
- Cline: https://github.com/cline/cline/tree/29896ec7fa8e2dd98805b56c2dae987d59fc9baa

## Multi-agent orchestration

- LangGraph: https://github.com/langchain-ai/langgraph/tree/7daa3ab49d678a5da75edb08baa87db4a2be52c3
- AutoGen: https://github.com/microsoft/autogen/tree/027ecf0a379bcc1d09956d46d12d44a3ad9cee14
- CrewAI: https://github.com/crewAIInc/crewAI/tree/4ed2abc7bbf504a634d3b733f2a97e0fbe8d44ec
- MetaGPT: https://github.com/FoundationAgents/MetaGPT/tree/11cdf466d042aece04fc6cfd13b28e1a70341b1f
- ChatDev 1.0: https://github.com/OpenBMB/ChatDev/tree/31fd994416a251ecdeb1f0a73c329271743bfb56
- ChatDev/DevAll: https://github.com/OpenBMB/ChatDev/tree/4fb2db0ea90375ce1059f44fe03ffbd191a7a169
- OpenAI Swarm: https://github.com/openai/swarm/tree/6af0b4caf37dca4526dfd98e9fbd8ce36e7eeb22
- OpenAI Agents SDK: https://github.com/openai/openai-agents-python/tree/588826c5be27cad21a3067463e21972ffea38561

## Durable workflow và incident response

- Temporal docs: https://github.com/temporalio/documentation
- Prefect: https://github.com/PrefectHQ/prefect
- Dagster: https://github.com/dagster-io/dagster
- Kubernetes docs: https://github.com/kubernetes/website
- GitHub Actions docs: https://github.com/github/docs
- Google SRE Incident Management: https://sre.google/sre-book/managing-incidents/
- Google SRE Incident Response: https://sre.google/workbook/incident-response/
- PagerDuty Incident Response: https://github.com/PagerDuty/incident-response-docs

## Capability/tool routing

- MCP: https://github.com/modelcontextprotocol/modelcontextprotocol/tree/ab3a39c13bd23be691c2760e1c6c5c15a64582e1
- Semantic Kernel: https://github.com/microsoft/semantic-kernel/tree/ca40aa7226531d28a721d0ca0e451d0aaf86dafc
- Google ADK: https://github.com/google/adk-python/tree/044a1ec3f434cf2e3f7acfdbd52305b60d16f6e5
- LangChain: https://github.com/langchain-ai/langchain/tree/80b74090515a594cc8421fb2d82e98f4e256c29f
- OpenAI Agents SDK: https://github.com/openai/openai-agents-python/tree/588826c5be27cad21a3067463e21972ffea38561

---

## Kết luận

CDPA không cần prompt dài hơn ở runtime. CDPA cần:

- prompt hẹp hơn;
- authority rõ hơn;
- capability hiện hành thay vì tool name lịch sử;
- typed transitions;
- correction prompt riêng;
- exact execution identity;
- runtime enforcement cho những gì prompt không thể bảo đảm.

Câu hỏi mặc định của agent phải là:

> Postcondition cần đạt là gì, exact target nào thực sự bắt buộc, capability hiện hành nào được authorize để đạt nó, và action trước đã ở trạng thái not-applied, applied hay ambiguous?

Không phải:

> Task từng nhắc tool nào, và khi tool đó lỗi thì tôi PAUSE thế nào?
