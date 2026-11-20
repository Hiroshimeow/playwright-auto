# M365 Opus Round 2 - Solo role provenance 2026-11-08

Exact FJP worker turn 1b197046-m365-worker-20261108-091940-ec2b3a68, completed, same conversation as round 1. Reviewer did not access G6 nor run tests. Recommendation: preserve command ledger; normalize read-only Solo lookup's role from `roles[0]` only when Solo and exactly one valid role, retaining strict comparisons; do not introduce a duplicate normalized role write path. Full response:

---

[Reading 66 lines from start (total: 66 lines, 0 remaining)]

Reasoned for 1s
Review vòng 2 (M365 Opus, FJP): cách lưu role của command trong CDPA G6

Phân tích này chỉ dựa trên bằng chứng anh/chị gửi. Tôi không đọc được repo G6 và chưa chạy test nào. Những điểm có ghi cần xác minh là suy luận, chưa có bằng chứng.

Kết luận: Nên dùng phương án lấy role ở phía đọc, tức là trong endpoint tra cứu chỉ-đọc đang có. Không nên lưu thêm role thô ở bước chuẩn hoá. Phép so sánh role trong reconcile_transition phải giữ nguyên chế độ strict.

OBSERVED
Command cmd-f880256e… đã APPLIED và chỉ có một task là d53a…. Payload đã lưu có roles=["DEV"] nhưng không có trường role.
Endpoint by-idempotency trả "role": payload.get("role"), nên kết quả là null.
reconcile_transition yêu cầu role bằng "DEV". Vì vậy lệch role là nguyên nhân thứ hai gây SAFETY_HOLD, độc lập với chuyện lệch hash do ký tự xuống dòng cuối goal. Sửa một trong hai thì vẫn bị hold.
run_pi.py đang được child đang chạy sửa. Nếu thêm một bản sửa vào cùng file thì sẽ có hai owner cùng sửa một chỗ.
MINIMAL FIX
Chọn sửa ở phía đọc, trong dashboard_api.py tại GET /api/commands/by-idempotency:
Nếu payload có role thì trả nguyên giá trị đó.
Nếu không có, và đồng thời workflow_mode == "solo" và roles là list có đúng 1 phần tử kiểu chuỗi, thì trả roles[0].
Mọi trường hợp còn lại trả null.
Có thể trả kèm roles thô để phục vụ audit.
Lý do không lưu role thô ở bước chuẩn hoá:
Không chữa được command đã APPLIED, vì ledger không được sửa.
Làm đổi hình dạng payload được lưu. Nếu có kiểm tra xung đột idempotency theo payload hoặc hash, thì một lần retry với cùng key có thể bị 409 hoặc bị coi là lệch. Cần xác minh.
Giữ strict compare trong reconcile_transition:
Không chấp nhận null, không bỏ qua khác biệt chữ hoa/thường, không chấp nhận multi-role.
Nhờ vậy không phải sửa run_pi.py, nên tránh được xung đột với child đang chạy.
Hash của goal, chỉ áp dụng khi intent có lưu toàn văn goal (cần xác minh):
Reconcile có thể so sha256(normalize_goal(intent.goal)) với hash của command. Đây là so khớp chính xác trên cùng một biểu diễn chuẩn, không phải nới lỏng kiểm tra.
Cách này tự gỡ hold cho d53a mà không cần operator can thiệp. Tuy nhiên nó nằm trong run_pi.py, nên chỉ làm sau khi child hiện tại đã bàn giao.
Nếu intent chỉ lưu hash thì d53a vẫn phải gỡ hold một lần qua write_state kèm bằng chứng, như tôi đã đề xuất ở vòng 1.
TESTS

Mọi test đều phải FAIL trên c2e87f2 trước khi sửa. Tất cả chạy offline được.

Positive:

Fixture có đúng hình dạng của ledger thật (workflow_mode=solo, roles=["DEV"], không có role): lookup trả "DEV".
Reconcile với transition DEV và hash đã chuẩn hoá khớp: không vào hold.
Payload legacy có sẵn role:"DEV": lookup vẫn trả "DEV".

Adversarial (đều phải trả role=null hoặc vào SAFETY_HOLD):

roles=["REVIEW"] trong khi transition là DEV.
roles=["DEV","TEST"].
roles=[], thiếu trường roles, hoặc roles không phải list hay chứa phần tử không phải chuỗi.
workflow_mode khác solo nhưng roles=["DEV"].
role:"DEV" mâu thuẫn với roles=["REVIEW"].
"dev" viết thường, nếu quy ước là phân biệt hoa/thường.
Role khớp nhưng hash lệch thật.
Hash khớp nhưng role lệch.

Bất biến:

Gọi lookup không làm thay đổi byte nào của ledger.
Replay cùng key với cùng payload sau khi sửa: vẫn trả command cũ, không tạo task mới, không có Send.
Test kết hợp tái hiện trường hợp d53a (lệch cả role lẫn ký tự xuống dòng): chỉ ra khỏi hold khi cả hai bản sửa đều có.

Chỉ chứng minh được khi chạy thật: reconcile của successor đầu tiên được dispatch sau bản sửa. Tuyệt đối không gửi lại d53a.

RISKS
Có thể có client khác đang dựa vào việc role bằng null. Rủi ro thấp, nhưng cần grep các chỗ đọc trường này.
Tôi chưa chắc workflow_mode có được lưu trong payload hay không. Nếu không có, phải dùng một dấu hiệu Solo khác đã có sẵn trong payload, không thêm trường mới.
Áp dụng dashboard_api.py cần reload API. Theo rule 9, reload không được restart Chrome 9202, và phải kiểm tra lại task hoặc Send đang chạy trước khi reload.
Bản sửa này có lẽ nằm ngoài phạm vi được giao của d53a. Khi đó Pi chỉ giao cho một successor duy nhất, sau khi đã reconcile xong parent và command.

G6 DEV có nên áp dụng trước khi restart Pi? Có, nên áp dụng cả hai bản sửa (role và ký tự xuống dòng cuối goal) trước. Restart khi mới sửa một bản thì vẫn chắc chắn vào hold.

Toàn bộ campaign vẫn ở trạng thái NOT_ACCEPTED.