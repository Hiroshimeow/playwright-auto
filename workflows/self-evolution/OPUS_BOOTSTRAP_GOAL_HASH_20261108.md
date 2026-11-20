# M365 Opus review - goal-hash normalization 2026-11-08

Exact FJP worker turn: 1b197046-m365-worker-20261108-091631-cc5374d6, COMPLETED, model Opus, 4859 response characters. Reviewer analyzed submitted evidence; did not run G6 tests or edit G6. Full campaign goal supplied. Its conclusion: normalize Solo goal before hashing/sending, test failing-first, retain existing request/command ledger, and reconcile pending already-applied task without duplicate dispatch. See original response below.

---

[Reading 52 lines from start (total: 52 lines, 0 remaining)]

Reasoned for 1s
Review độc lập (M365 Opus, FJP): CDPA G6, lỗi băm --goal khi chuyển giao Pi

Đây là phân tích chỉ dựa trên đoạn mã và bằng chứng anh/chị gửi. Tôi không đọc được repo G6 và chưa chạy test nào. Mọi nhận định về mã thật bên dưới đều được đánh dấu là cần xác minh.

Kết luận: Đồng ý với hướng "chuẩn hoá một lần trước khi băm và gửi". Có hai điểm cần sửa so với đề xuất ban đầu:

Sửa spawn_cdpa.py chỉ chữa được các lần dispatch sau. Intent đang treo của cdpa-idem-d53a661e313b06a7b8a70b5d vẫn sẽ bị SAFETY_HOLD sau khi restart.
Có một rủi ro gửi trùng (duplicate Send) nếu idempotency key được tính từ nội dung goal. Phải kiểm tra điểm này trước khi restart.
OBSERVED (bằng chứng anh/chị cung cấp)
task_prompt trả về chuỗi kết thúc bằng \n (dài 10964 ký tự). record_transition_intent băm chuỗi gốc này và ghi hash 1130f0f3….
submit_task gửi đi str(goal).strip() (dài 10963 ký tự). Cả manifest lẫn API /api/commands/by-idempotency/<key> đều ra hash 047b12dd…. 10963 ký tự đầu của hai chuỗi giống hệt nhau.
Trong ledger chỉ có đúng một lệnh đã APPLIED và một task. Không có Send nào bị phát lại.
Pi chưa được restart.
ROOT CAUSE

Có hai nơi cùng định nghĩa "goal chuẩn" mà không thống nhất với nhau. Intent băm chuỗi trước khi chuẩn hoá, còn CLI gửi chuỗi sau khi chuẩn hoá. Lỗi không nằm ở phép so sánh SHA trong reconcile_transition, nên SAFETY_HOLD lúc này là hành vi fail-closed đúng thiết kế.

MINIMUM FIX (cho G6 CDPA DEV)
Gom về một chỗ. Tách biểu thức str(goal or "").strip() trong cdpa_cli.py thành một hàm một dòng, ví dụ normalize_goal. submit_task gọi hàm này, và spawn_cdpa.py cũng gọi nó trước khi tạo cmd. Như vậy argv, hash của intent và payload HTTP là cùng một chuỗi. Vì strip là idempotent nên lần strip thứ hai trong CLI không làm thay đổi gì.
Chặn ngay trong record_transition_intent. Nếu goal != normalize_goal(goal) thì raise lỗi, không âm thầm chuẩn hoá. Cách này bắt được mọi caller khác ngoài spawn_cdpa.
Không làm những việc sau: so sánh SHA "mềm", lưu hai hash, thêm nhánh legacy vĩnh viễn trong reconcile_transition, hay tạo ledger hoặc gate mới. Tất cả đều là over-engineering.

Intent đang treo (d53a…): Dùng đúng cơ chế đã có. Cứ để Pi vào SAFETY_HOLD, sau đó giải hold bằng giao diện run_pi.write_state (giao diện operator đã dùng). Bản ghi giải hold cần có: hai SHA, độ dài 10964/10963, bằng chứng chỉ có một command APPLIED, và quyết định nhận d53a làm task đang sở hữu. Không spawn task mới, vì sẽ thành task trùng. Đây là thao tác trên state của Pi, không phải sửa ledger của CDPA.

TESTS

Có thể chạy offline trên G6 (không cần trình duyệt, không Send):

Test hồi quy viết trước (failing-first): dùng goal nhiều dòng có \n ở cuối, so sánh SHA của intent, SHA của payload submit_task (mock HTTP) và SHA mà API trả về trên instance test. Test phải FAIL trên HEAD c2e87f2 và PASS sau khi sửa.
record_transition_intent phải raise khi nhận goal chưa chuẩn hoá.
reconcile_transition dùng API giả: SHA khớp thì tiếp tục, SHA lệch thật thì vẫn SAFETY_HOLD (không được làm yếu kiểm tra).
Ổn định idempotency key: cùng một intent trước và sau bản sửa phải ra cùng key. Retry phải dùng key đã lưu trong intent, không tính lại.
CRLF: campaign file dùng \r\n vẫn cho SHA nhất quán. Ký tự không phải ASCII cũng phải round-trip đúng qua argv trên Windows.
50 thế hệ: mỗi thế hệ đều chứa nguyên văn contract và chuỗi spawn. Độ dài không tăng tích luỹ. Phép kiểm tra "contract nằm trong prompt" phải so với bản đã strip.

Chỉ chứng minh được khi chạy thật: hops[0].prompt và receipt của lần dispatch đầu tiên sau bản sửa, cùng tình huống crash/mất ack mà vẫn không phát lại Send. Việc này chỉ làm với successor kế tiếp, tuyệt đối không gửi lại d53a.

RISKS
Duplicate Send (cao): Nếu key 1ff76327… (dạng uuid5) được tính từ goal hoặc hash, việc đổi cách chuẩn hoá sẽ đổi key khi retry, dẫn tới tạo task thứ hai. Cần xác minh các đầu vào dùng để tính key.
Retry dựng lại prompt: Nếu nhánh 404 gọi lại constructor mà campaign file đã đổi, SHA sẽ lệch. Retry phải dùng goal hoặc hash đã lưu trong intent. Cần xác minh.
Giới hạn argv của Windows (32.767 ký tự): hiện tại khoảng 11k nên còn an toàn, nhưng contract còn dài thêm thì sẽ chạm trần. Bây giờ chỉ cần ghi nhận, chưa cần sửa.
Có thể có kiểm tra "nguyên văn contract" ở chỗ khác đang giả định có \n ở cuối.
G6 DEV có nên áp dụng trước khi restart Pi?

Có. Bản sửa và test offline nên được merge trước khi restart, để các lần dispatch sau không lặp lại lỗi. Cần lưu ý ba điểm:

Restart xong thì intent d53a vẫn vào SAFETY_HOLD. Đây là hành vi đúng và an toàn, xử lý như mục "Intent đang treo" ở trên.
Nếu d53a có phạm vi được giao (bounded scope) không bao gồm bản sửa này, Pi chỉ giao cho một successor duy nhất sau khi đã giải hold và reconcile xong parent và command.
Phải xác minh rủi ro idempotency key trước khi restart.

Toàn bộ campaign vẫn ở trạng thái NOT_ACCEPTED.