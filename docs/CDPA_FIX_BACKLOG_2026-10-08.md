# CDPA Fix Backlog — 2026-10-08

## Mục tiêu

Sau cleanup 2026-10-08, sửa CDPA theo nguyên tắc: một lỗi một lần, không vừa chạy production task vừa vá control-plane, ưu tiên giảm BLOCK giả trước khi thêm chức năng mới.

Baseline sau cleanup:
- Source checkpoint: `a54ff56`.
- Runtime task history cũ đã archive vào `.runtime/cleanup-backup-20261008-095554/`.
- Workflow task cũ: 0 đang chạy.
- Worker error log sau restart: sạch tại thời điểm kiểm tra.
- Hai independent agent nền có thể được runtime dựng lại ở WAITING; không tính là zombie workflow task.
- Full suite gần nhất trước cleanup: 1731 passed, 45 skipped, 15 failed.

## Bản đồ lỗi dễ hiểu

```text
USER / DASHBOARD
      |
      v
 CREATE / CONTROL
      |
      +--> [A] version/CAS race --------------------+
      |                                             |
      v                                             |
   SEND --------------------------------------------+
      |
      +--> [B] receipt / accepted identity
      |         |
      |         +--> URL đổi / mất page
      |                  |
      |                  v
      |             BLOCK giả  <---- P0
      |
      v
 WAIT RESPONSE
      |
      +--> [C] LISTEN / DOM freshness
      |         |
      |         +--> stale streaming / timeout
      |                  |
      |                  v
      |             repair/reload thừa
      |
      v
 RESULT / ROUTE
      |
      +--> [D] report đã có nhưng terminal JSON miss
      |                  |
      |                  v
      |          model route-repair thừa <---- P0
      |
      v
 PLAN / NEXT ROLE
      |
      +--> [E] prompt local != packaged
                |
                v
          hành vi route không đồng nhất <---- P0
```

---

## P0 — Phải sửa trước

### CDPA-001 — Accepted Send nhưng không resolve được conversation identity

**Triệu chứng**
- `accepted_conversation_identity_unresolved`.
- Resume/reopen có thể rơi về `https://chatgpt.com/`.
- Task BLOCK dù Send đã được chứng minh accepted.

**Evidence gần nhất**
- `mcp-gateway-email-approval-spec-20261006-r2` bị BLOCKED ở AUDIT.
- Command history có nhiều recovery_required cùng reason.
- Clear Team cũng thất bại khi exact-page preflight gặp root URL.

**Nguyên nhân thiết kế**
URL/page hiện đang gánh quá nhiều vai trò như identity. Trong thực tế URL có thể đi:
`chatgpt.com -> /c/... -> /g/.../c/...`, hoặc tạm thời quay về root/challenge.

**Hướng sửa**
- Identity chính: request + accepted user message + page lineage/conversation id.
- URL chỉ là navigation evidence.
- Sau accepted Send: read-only reconcile trong bounded settle window.
- Chỉ BLOCK khi có conflict/divergence thật hoặc hết deadline.

**Done khi**
- URL thay đổi hợp lệ không tạo BLOCK.
- Không replay Send.
- Conflict thật vẫn fail-closed.

---

### CDPA-002 — Substantive work xong nhưng control-plane đòi route-repair

**Triệu chứng**
- Report đúng path đã tồn tại.
- Agent đã hoàn tất substantive work.
- Control-plane vẫn sinh prompt kiểu `repairing the route response` / `accepted execution produced no terminal result`.

**Evidence**
- 3–4 ngày gần đây: pattern xuất hiện trên nhiều report/task.
- Đây là nguồn tạo turn thừa, token thừa và thêm cơ hội BLOCK.

**Nguyên nhân**
Terminal route đang phụ thuộc quá nhiều vào final UI/model output thay vì durable artifact.

**Hướng sửa**
- Nếu report exact path tồn tại, non-empty, và route có thể xác nhận deterministic từ durable state: local reconciliation.
- Model repair chỉ là fallback cuối.
- Tuyệt đối không replay substantive work.

**Done khi**
- Mất terminal JSON nhưng report hợp lệ => CDPA tự terminalize/route.
- Không spawn model turn chỉ để sửa bookkeeping.

---

### CDPA-003 — Prompt contract drift giữa local và packaged assets

**Triệu chứng**
Full suite còn nhiều fail:
- local prompt != packaged prompt;
- PLAN thiếu/khác PAUSE contract;
- PLAN self-route/wait contract lệch;
- role prompt thiếu shared contract;
- learning/final PLAN contract lệch.

**Evidence**
15 full-suite failures gần nhất; phần lớn thuộc prompt/route contract.

**Hướng sửa**
- Một canonical prompt source.
- Build/package mirror được generated hoặc verified byte-for-byte.
- Không maintain hai bản thủ công.

**Done khi**
- Toàn bộ prompt contract tests xanh.
- Không thể commit khi local/package drift.

---

### CDPA-004 — Legacy/invalid manifest có thể làm worker spam vô hạn

**Triệu chứng trước cleanup**
- Worker error log ~40 MB.
- Hơn 77k dòng `nonterminal task must have a configured active role`.
- Chủ yếu từ legacy `mcp-device-110-integration` và `final-stress`.

**Nguyên nhân**
Worker scan lại manifest invalid mỗi vòng mà không quarantine/backoff.

**Hướng sửa**
- Invalid legacy manifest => quarantine once + one structured incident.
- Exponential/backoff hoặc fingerprint suppression.
- Không log cùng một immutable error mỗi poll.

**Done khi**
- Một manifest hỏng không thể tăng log vô hạn.
- Worker vẫn xử lý task khỏe bình thường.

---

## P1 — Sửa ngay sau P0

### CDPA-005 — Clear Team quá phụ thuộc browser/page state

**Triệu chứng**
Cleanup task BLOCKED thất bại:
`RoleOwnershipError: cannot inspect a supported ChatGPT page ... https://chatgpt.com/`.

**Vấn đề**
Một operation mang tính cleanup không nên bị kẹt chỉ vì tab đã mất exact conversation URL.

**Hướng sửa**
- Cleanup durable state và browser cleanup tách thành 2 phase.
- Exact-owned tab thì đóng; tab không chứng minh được owner thì không đụng.
- Vẫn cho task đạt terminal cleanup state mà không cần mutate tab không chắc chắn.

---

### CDPA-006 — Dependency guard chặn Clear Team cả khi operator muốn dọn

**Triệu chứng**
`This browser-capacity control cannot bypass unfinished dependencies.`

Ngay cả sau Stop, Clear Team vẫn bị guard dependency cũ từ chối trong cleanup vừa rồi.

**Hướng sửa**
- Dependency phải chặn execution, không chặn destructive cleanup đã confirm.
- `Stop/Clear Team` phải có semantics cleanup riêng.

---

### CDPA-007 — Independent agent delete cần reset thủ công khi có active event

**Triệu chứng**
Delete trả:
`independent agent has an active event`.

Phải gọi `reset` rồi `delete`.

**Hướng sửa**
- UI/API có một operator cleanup action atomic: cancel active event -> delete.
- Hoặc delete confirmed tự thực hiện bounded reset trước.

---

### CDPA-008 — Stale task version/CAS race gây control failure

**Evidence**
Nhiều `stale task version: expected X, current Y` trong control commands gần đây.

**Hướng sửa**
- Với action idempotent/safe: refresh state + retry CAS đúng một lần.
- Với action destructive hoặc semantic mutation: báo conflict rõ ràng, không tự retry mù.

---

### CDPA-009 — Catalog discovery degradation đang có blast radius quá rộng

**Triệu chứng**
- Create task bị chặn vì worker catalog degraded.
- Reload catalog từng fail 3 lần với 17 discovery errors, lần sau mới tự hồi.

**Hướng sửa**
- Per-provider/per-agent health.
- Chỉ block task nếu capability nó cần thật sự unavailable.
- Partial catalog vẫn usable khi phần cần dùng healthy.

---

### CDPA-010 — LISTEN/DOM freshness và response observer còn noise

**Evidence**
- `response_body_decode TimeoutError`.
- stale `IS_STREAMING` từng tham gia timeout/recovery.
- State repair/refresh vẫn xuất hiện.

**Hướng sửa**
- DOM terminal result có authority cao hơn stale LISTEN.
- Signal phải có epoch/sequence/watermark.
- Decode timeout là degraded observation, không tự động thành task failure.

---

## P2 — Debt/ergonomics

### CDPA-011 — Full-suite baseline chưa xanh

Ngoài prompt contract còn một UI cache-buster assertion cũ.

**Hướng sửa**
- Chốt một baseline xanh trước khi bắt đầu series fix tiếp theo.
- Không tiếp tục gọi failure là “baseline debt” vô thời hạn.

---

### CDPA-012 — Runtime observability đang nhiều event nhưng khó thấy root cause

Có hàng nghìn live event nhưng operator phải đọc raw JSON để biết:
- block bắt đầu ở đâu;
- automatic recovery đã thử gì;
- vì sao vẫn cần người.

**Hướng sửa**
Mỗi task có một compact incident timeline:
`healthy -> uncertainty -> reconcile attempts -> terminal reason -> next legal action`.

---

## Thứ tự sửa đề xuất

```text
Phase 0  Baseline xanh + test harness ổn
   |
   +-- 001 Identity lineage
   |
   +-- 002 Local route/report reconciliation
   |
   +-- 003 Canonical prompt source
   |
   +-- 004 Manifest quarantine/log suppression
   |
Phase 1
   +-- 005/006 Cleanup semantics
   +-- 007 Independent cleanup
   +-- 008 CAS retry policy
   +-- 009 Catalog isolation
   +-- 010 Signal freshness
   |
Phase 2
   +-- 011 Full-suite debt
   +-- 012 Operator incident view
```

## Quy tắc thực hiện từ đây

1. Một issue/branch hoặc một commit series nhỏ tại một thời điểm.
2. Trước mỗi fix: reproduction test đỏ.
3. Fix nhỏ nhất có thể; không thêm state/service nếu owner hiện tại đủ dùng.
4. Focused tests xanh rồi mới broader regression.
5. Không chạy live Send acceptance nếu unit/integration contract chưa xanh.
6. Sau mỗi P0: chạy soak thật trên Temporary + persistent và đo BLOCK rate.
7. Không merge thêm feature mới cho đến khi P0 hoàn tất.

## Chỉ số mục tiêu

- Control command clean-path >= 95%.
- BLOCK do navigation/transient identity: 0 trong happy-path soak.
- Model route-repair cho artifact hợp lệ: 0.
- Repeated immutable worker error: tối đa 1 incident/fingerprint/window.
- Full suite: 0 known unexplained failures.
