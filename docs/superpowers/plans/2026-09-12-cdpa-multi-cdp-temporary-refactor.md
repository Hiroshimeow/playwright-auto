# CDPA Multi-CDP + Temporary Chat Recovery Refactor Plan

## Mục tiêu

Refactor CDPA để một worker vận hành nhiều browser CDP trên cùng dashboard, cấp endpoint theo từng role/execution và lưu provenance bền vững thay vì gắn task/team vĩnh viễn vào browser; bổ sung Temporary Chat và recovery khi mất generation.

Refactor chia thành hai milestone chính:

1. Multi-CDP hoàn chỉnh và ổn định.
2. Temporary Chat cho workflow + recovery snapshot.

Multi-CDP đi trước để browser identity, ownership và routing ổn định trước khi thay đổi conversation lifecycle.

---

# 0. Baseline và môi trường hiện tại

## Repository

ThinkBook workspace:

```text
E:\git-project\playwright-auto-multicdp
```

Branch:

```text
refactor/cdpa-multi-cdp-temporary
```

Base:

```text
d4f0c41 refactor(cdpa): simplify timeout recovery handling
```

`origin/develop` cũng đang ở `d4f0c41`.

## Browser thật đang có trên ThinkBook

```text
127.0.0.1:9222
127.0.0.1:9202
```

Cả hai endpoint:

- trả `/json/version` hợp lệ;
- kết nối CDP được;
- có ChatGPT conversation page;
- `requires_login=False`;
- composer hiện diện.

Browser process hiện tại:

```text
9222 -> Thorium -> E:\mauto_playwright\profiles\thorium_chatgpt
9202 -> Thorium -> E:\mauto_playwright\profiles\hcu_chatgpt
```

Đây là hai endpoint acceptance chính trong quá trình refactor.

## Test baseline trên Windows

Regression set hiện tại cho kết quả:

```text
164 passed
13 failed
```

Các failure đã quan sát:

- test MCP browser fixture hardcode `/snap/bin/chromium`, không phù hợp Windows;
- test symlink cần privilege Windows;
- một legacy inline-report recovery case khác behavior giữa Windows/Linux.

Các failure này có trước refactor và được ghi nhận như baseline khác biệt nền tảng. Khi cần test code mới trên ThinkBook, ưu tiên các test thuần Python và acceptance qua hai CDP thật. Test fixture mới nên chạy được trên Windows/Linux thay vì tiếp tục hardcode Snap Chromium.

---

# 1. Contract tổng thể sau refactor

Một dashboard và một worker quản lý nhiều browser:

```text
Dashboard :9224
      |
Dashboard API
      |
TaskStore / Runtime DB
      |
1 CDPAWorker
      |
      +---- endpoint A -> browser A
      +---- endpoint B -> browser B
      +---- endpoint C -> browser C
```

CDP endpoints là execution capacity pool, không phải durable team ownership. Scheduler giữ một workflow/task model và cấp endpoint slot theo từng execution/role. Cùng một team có thể chạy PLAN/DEV/TEST/REVIEW trên các endpoint khác nhau; browser affinity chỉ là preference khi an toàn và còn capacity.

```text
team alpha
  PLAN   -> endpoint A
  DEV    -> endpoint B
  TEST   -> endpoint C
  REVIEW -> endpoint A
```

Browser remote chỉ là execution surface qua CDP. Worker, TaskStore, scheduler và dashboard vẫn chạy ở root host.

---

# 2. Browser endpoint model

## Yêu cầu dữ liệu

Browser endpoint cần có identity nội bộ bền vững tách khỏi địa chỉ kết nối.

Thông tin cần quản lý:

```text
endpoint_id
cdp_url / host + port
display label
is_default
temporary_default
max_active_tabs (default 2, configurable per endpoint)
rate_limit_scope
management mode: managed-local | external
profile path nếu là managed-local
health state
auth state
```

`endpoint_id` là identity bền vững cho provenance và role/execution placement; nó không phải permanent task/team ownership.

`cdp_url` chỉ là địa chỉ hiện tại. Hai máy khác nhau có thể cùng dùng port `9222`.

Ví dụ hợp lệ:

```text
127.0.0.1:9222
127.0.0.1:9202
100.80.10.4:9222
100.80.10.8:9222
```

## Backward compatibility

Config cũ hiện có:

```json
"browser": {
  "cdp_url": "http://127.0.0.1:9222"
}
```

Flow load config:

```text
có endpoint registry mới?
  |
  +-- YES -> load registry
  |
  +-- NO  -> synthesize một endpoint legacy từ browser.cdp_url
```

Task/manifest cũ chưa có `browser_endpoint_id` vẫn map về legacy endpoint cho provenance/affinity compatibility. Field này không phải permanent scheduling ownership; role/execution có thể được cấp endpoint khác khi pool cần relocation.

Mục đích là nâng cấp không mất provenance cũ nhưng vẫn cho phép scheduler mới dùng bất kỳ eligible endpoint an toàn.

---

# 3. Parsing endpoint từ UI/CLI

Input đơn giản:

```text
9212
```

được hiểu là:

```text
127.0.0.1:9212
```

Input có host:

```text
100.80.10.4:9222
```

được giữ thành endpoint remote.

Có thể chấp nhận hostname nếu CDP connect được, ví dụ host trong Tailscale/NetBird.

Flow normalize:

```text
input chỉ là số port?
  |
  +-- YES -> localhost:<port>
  |
  +-- NO  -> parse host:port / URL
              |
              +-- format hợp lệ -> probe
              +-- format lỗi    -> trả validation error
```

Không dùng port làm identity nội bộ.

---

# 4. Add Browser flow

## Bước probe

```text
User nhập endpoint
      |
      v
Normalize address
      |
      v
Probe TCP/HTTP CDP
      |
      +-- /json/version hợp lệ?
             |
             +-- YES -> thử connect_over_cdp
             |           |
             |           +-- thành công -> inspect contexts/pages/auth
             |           +-- lỗi       -> báo endpoint không usable
             |
             +-- NO -> phân loại local / remote
```

## Endpoint online

Nếu CDP đã online:

```text
connect thành công
      |
      v
add endpoint record
      |
      v
publish browser health/auth
      |
      v
endpoint xuất hiện trong dashboard
```

Nếu browser không do CDPA tạo thì management mode là `external`.

External browser:

- worker attach/detach qua CDP;
- dashboard không quản PID/profile lifecycle;
- browser vẫn được dùng bình thường cho task nếu health/auth ready.

## Endpoint local nhưng offline

Nếu input trỏ localhost và chưa có CDP:

```text
local endpoint offline
      |
      v
UI cho lựa chọn tạo browser local
      |
      +-- new profile
      +-- existing profile path
      +-- clone profile từ browser local khác
```

Profile mặc định khi tạo mới dùng tên dễ nhận biết theo port, ví dụ:

```text
...\profiles\9212
```

## Endpoint remote nhưng offline

V1 chỉ attach remote browser, không quản process remote.

```text
remote endpoint offline
      |
      v
record/display OFFLINE hoặc hủy add theo UX hiện tại
```

Không cần SSH/remote agent trong refactor này.

---

# 5. Local browser lifecycle

Browser local do dashboard/CDPA tạo cần tách rõ các action:

```text
attach/detach
start/stop process
remove endpoint registry
delete profile
```

Các action không đồng nghĩa với nhau.

## Clone profile

Flow an toàn:

```text
user chọn clone source browser
      |
      v
source có active work / đang ghi profile?
      |
      +-- YES -> chờ/drain hoặc yêu cầu source dừng trước snapshot
      |
      +-- NO  -> copy profile sang folder mới
                   |
                   v
               start browser mới
                   |
                   v
               probe CDP + auth
```

Mục tiêu là tránh copy một Chromium profile đang thay đổi giữa chừng.

Sau clone, user có thể đổi ChatGPT account trên browser mới.

---

# 6. Health và login state

CDP health và ChatGPT authentication là hai state khác nhau.

## CDP state

```text
OFFLINE
CONNECTING
ONLINE
ERROR
```

## ChatGPT state

```text
UNKNOWN
LOGGED_OUT
LOGGED_IN
AUTH_CHALLENGE
```

Detection có thể reuse probe hiện tại trong `chatgpt.py`, ví dụ `requires_login`, composer presence và URL/auth markers.

Flow readiness:

```text
CDP ONLINE?
  |
  +-- NO -> endpoint unavailable
  |
  +-- YES
        |
        v
   ChatGPT authenticated?
        |
        +-- YES -> READY
        +-- NO  -> login/auth state hiển thị trên dashboard
```

Task mới chỉ được schedule vào endpoint có state phù hợp để thao tác ChatGPT.

Endpoint mất kết nối chỉ làm execution trên endpoint đó cần phân loại recovery. Pre-send pristine work có thể relocate; accepted/ambiguous Send phải reconcile provenance trước và chỉ dùng recovery continuation khi không thể consume kết quả cũ.

---

# 7. Default browser

Registry có một endpoint default.

Dashboard cho phép set endpoint hiện tại thành default.

Flow create task khi CLI không chỉ định browser:

```text
create task
   |
   v
resolve default endpoint
   |
   v
snapshot endpoint_id vào manifest
```

Sau khi task đã tạo, đổi default browser chỉ ảnh hưởng task mới.

---

# 8. Durable workflow state + execution placement

`browser_endpoint_id` được giữ cho provenance/initial affinity/backward compatibility, không phải durable team owner. Scheduling unit là active execution/role. Role record có thể ghi endpoint nơi execution đang/chạy gần nhất, và các role của cùng team được phép khác endpoint.

Allocation dùng pool state:

```text
active role/hop cần browser
      |
      v
filter READY + auth usable + not draining + cooldown clear + free capacity
      |
      v
prefer safely reusable same-role live endpoint
      |
      v
least-loaded / stable-order eligible endpoint
```

Capacity tính theo protected/running executions, không theo historical idle tabs. Default `max_active_tabs = 2` nhưng registry cho phép cấu hình per endpoint. Khi hết slot hợp lệ, task WAIT vì resource pressure và được reconsider khi pool đổi state.

## Relocation safety

- pre-send/pristine, chưa accepted và không có attachment ownership conflict -> cùng task/hop/request có thể relocate sang endpoint khác;
- accepted result còn recoverable -> reconcile/consume result, không resend;
- accepted/ambiguous Send + endpoint cũ unusable -> không replay prompt; dùng durable workspace/report/Git/RequestLedger/provenance để tạo recovery continuation trên endpoint eligible khác;
- BLOCK chỉ khi không còn safe automatic progress path.

Conversation/account affinity là optimization. Durable workflow state và workspace/report evidence là continuity boundary.

---

# 9. Physical page ownership trong multi-CDP

Identity vật lý hiện tại dựa mạnh vào `page_id` trong một browser context.

Multi-CDP cần coi physical page key tối thiểu là:

```text
(endpoint_id, page_id)
```

Các boundary cần review và cập nhật:

- role record;
- page binding;
- tab ownership lookup;
- send receipt/request ledger nếu physical browser provenance liên quan recovery;
- browser projection;
- dashboard task/page matching;
- cleanup/reopen validation;
- duplicate ownership detection.

Mục đích là hai browser khác nhau không thể vô tình được coi là cùng physical tab namespace.

---

# 10. Worker topology

Giữ một `CDPAWorker`.

Worker quản một pool endpoint runtime:

```text
endpoint_id -> connection/browser/context/health
```

Startup flow:

```text
load endpoint registry
      |
      v
connect từng endpoint độc lập
      |
      +-- endpoint A online
      +-- endpoint B online
      +-- endpoint C offline
      |
      v
worker vẫn chạy với các endpoint còn usable
```

Một endpoint disconnect không nên làm toàn bộ worker/browser pool trở thành disconnected.

Reconnect loop chạy theo từng endpoint.

Task scheduler giữ nguyên queue/dependency machinery; trước mỗi browser execution, endpoint allocator chọn một eligible slot. Scheduler không tạo queue/worker riêng theo endpoint.

---

# 11. Browser inventory và runtime projection

Browser snapshot hiện tại là một projection đơn.

Sau refactor cần biểu diễn nhiều endpoint, ví dụ về mặt semantics:

```text
browser pool
  endpoint A
    health / auth / cooldown
    active_count / capacity / available_slots
    running_executions
    pages
  endpoint B
    health / auth / cooldown
    active_count / capacity / available_slots
    running_executions
    pages
```

Mỗi page inventory có endpoint identity.

Dashboard/API cần đủ dữ liệu để:

- biết browser nào online;
- biết browser nào đã login;
- biết execution/role nào đang hoặc vừa chạy trên browser nào;
- filter board theo current execution placement mà không ngụ ý permanent ownership;
- hiển thị count/pages theo endpoint;
- không đánh dấu toàn bộ browser system offline khi chỉ một endpoint mất kết nối.

---

# 12. Dashboard

Giữ dashboard/Kanban hiện có trong code hiện tại.

Không lấy UI trong ảnh tham khảo làm target implementation.

Refactor UI hiện có để browser endpoint trở thành scope/board của Kanban.

## Browser/board selector

Cần hỗ trợ:

```text
All
endpoint A
endpoint B
endpoint C
Add browser
Manage browsers
```

Chọn endpoint:

```text
selected endpoint
      |
      v
Kanban hiện execution/task đang có current/last role placement trên endpoint đó
```

`All` là projection tổng hợp.

## Control theo browser

Khi đang chọn một endpoint, UI có chỗ cho:

- health;
- auth/login state;
- set default;
- Temporary toggle;
- active / capacity / available slots;
- cooldown + running task/team/role entries;
- add/manage/remove browser.

Giữ style/component conventions đang có của dashboard.

---

# 13. Remove / drain browser

Drain hoạt động trên execution capacity, không trên historical team ownership.

```text
drain request
      |
      v
endpoint không nhận allocation mới
      |
      +-- running execution còn usable -> cho finish
      +-- endpoint mất -> relocation/recovery theo Send boundary
      +-- hết running execution -> detach/remove registry
```

Removing endpoint không được strand team/task chỉ vì từng chạy ở endpoint đó.

Đối với external browser, remove nghĩa là detach khỏi CDPA registry.

Đối với managed-local browser, stop process và delete profile là action riêng.

---

# 14. Rate limit và shared account scope

Nhiều endpoint có thể đăng nhập cùng một ChatGPT account.

Không mặc định mỗi CDP có rate limit độc lập.

Endpoint model nên có cách biểu diễn group/scope dùng chung cho:

- send pacing;
- cooldown;
- bootstrap/project compatibility nếu các identifier phụ thuộc account.

Nếu `9222` và `9202` dùng cùng account, worker có thể giữ rate-limit gate chung cho chúng.

Nếu browser đăng nhập account khác, có thể cấu hình scope khác.

Scope này là config/operator concept, không cần tự suy luận account identity từ private data.

---

# 15. CLI simplification

Giữ positional task và `--task` explicit form.

Bỏ public `--reuse-team` và cleanup code/tests/help liên quan nếu không còn caller nội bộ cần semantics đó.

Giữ các option hiện tại còn có mục đích riêng:

```text
--task
--new
--new-all
--team
--bootstrap
--fresh
--depends-on
--upload
--repository
--config
```

Thêm `--temp` cho workflow task creation.

Contract Temporary:

```text
có --temp
   -> task dùng Temporary Chat

không có --temp
   -> task inherit Temporary setting của browser endpoint
```

Không cần thêm `--persistent` hoặc tri-state CLI.

Nếu CLI cần chọn browser trực tiếp, dùng một option ngắn rõ nghĩa cho endpoint/board; parser chấp nhận shorthand port và host:port giống Add Browser UI. Tên option nên phù hợp naming hiện có trong CLI sau khi agent review parser, tránh tạo nhiều alias cho cùng một việc.

---

# 16. Bỏ `dom_only`

`dom_only` hiện là runtime switch global giữa DOM-only và hybrid observation.

Hybrid đã là operational behavior được sử dụng ổn định, nên refactor hướng tới một behavior hybrid cố định.

Các điểm cần cleanup:

- runtime settings snapshot;
- dashboard API `/api/runtime/settings` phần `dom_only`;
- dashboard toggle;
- worker `_dom_only_enabled()`;
- controller/page observer branches chỉ tồn tại để switch mode;
- tests của hot mode switch / DOM-only-specific branch.

Không cần bỏ network/DOM evidence hiện có; chỉ bỏ mode toggle và nhánh lựa chọn runtime.

Kết quả mong muốn:

```text
worker observation = hybrid cố định
```

---

# 17. Persistent bootstrap direct-branch + explicit fresh context policy

Bootstrap/fork không nên phụ thuộc menu DOM của ChatGPT nữa. Contract chuẩn cho mọi persistent bootstrap donor là một anchor pair bất biến:

```text
(conversation_id, assistant_message_id)
```

Trong đó:

- `conversation_id` là UUID của source conversation chứa bootstrap/anchor;
- `assistant_message_id` là ID của đúng assistant message nơi branch bắt đầu;
- hai giá trị phải được lưu và dùng như một cặp atomic, không ghép `conversation_id` của donor này với `assistant_message_id` của donor khác.

Bootstrap creation/prewarm tạo donor như sau:

```text
open fresh chat
      |
      v
send bootstrap/prewarm prompt
      |
      v
receipt.conversation_id ---------------------> source conversation_id
terminal assistant response.message_id ------> assistant_message_id
      |
      v
persist donor = (conversation_id, assistant_message_id)
```

Sau khi đã có donor pair, mọi fork dùng trực tiếp URL:

```text
https://chatgpt.com/branch/{conversation_id}/{assistant_message_id}
```

Flow chuẩn:

```text
select donor C/A
      |
      v
open /branch/C/A
      |
      v
observe POST /backend-api/conversation/new_branch
      |
      v
read response.conversation.conversation_id = canonical new conversation id
      |
      v
bind page/role/task ownership
      |
      v
sanitize composer + send actual task prompt
```

Không dùng visible URL làm source of truth trong giai đoạn branch bootstrap. ChatGPT có thể tạm hiển thị một client-local URL dạng:

```text
/c/WEB:<uuid>
```

trước khi history URL ổn định. Canonical branch identity phải lấy từ `new_branch` response khi có, rồi tiếp tục reconcile với send receipt/conversation evidence hiện có. Không gửi dummy message chỉ để ép `/c/WEB:*` đổi thành `/c/<uuid>`.

### Bỏ DOM branching

Xóa bootstrap/recovery dependency vào flow UI kiểu:

```text
More actions
  -> Open new branch
       -> Branch in new Chat
```

và bỏ production path dựa vào selector/menu text như `Branch in new chat`. ChatGPT hiện có nested branch menu và upstream UI có thể đổi tiếp; transport branch không cần phụ thuộc UI khi donor pair đã đủ dữ liệu.

`CDPATabActions.branch_from_anchor(...)` trở thành primitive duy nhất cho persistent bootstrap fork. `_branch_from_bootstrap_ui(...)` và tests chỉ tồn tại để mock/cố định menu DOM nên được xóa sau khi tất cả call site đã chuyển sang direct URL.

### Thống nhất create và Resume

Hiện create bootstrap đã ưu tiên direct `branch_from_anchor`, nhưng một số pre-boundary Resume path phân biệt `exact_donor` với donor resolve từ current/default bootstrap rồi đẩy trường hợp sau sang UI branch. Refactor bỏ divergence này.

Contract mới:

```text
có usable donor pair C/A
      |
      v
branch_from_anchor(C, A)
```

áp dụng giống nhau cho:

- first allocation của task có bootstrap;
- Resume của role có `bootstrap_source_donor` đã persist;
- pre-boundary recovery resolve được donor từ task/bootstrap catalog;
- donor rotation khi donor đầu không còn usable.

Không còn policy kiểu:

```text
not exact_donor -> use UI branch
```

Transport không được thay đổi chỉ vì donor là recorded donor hay current/default donor. Safety quyết định ở việc donor nào được phép chọn; sau khi donor đã được chọn, cách branch luôn là direct URL.

### Donor failure / rotation

Nếu direct branch donor hiện tại fail trước send boundary:

```text
donor #1 fail
   -> donor #2
   -> donor #3
   -> nếu hết donor và bootstrap có prewarm_prompt: regenerate donor
   -> nếu không regenerate được: BLOCK với bootstrap-specific evidence
```

Không fallback sang DOM click.

Nếu lỗi xảy ra sau khi actual task prompt có thể đã được accepted, giữ send-provenance/reconciliation semantics hiện tại; không mở donor khác rồi blind replay.

### Explicit `--fresh` phải giữ semantics riêng

Task được tạo bằng `--fresh` phải persist một durable context policy rõ ràng, ví dụ:

```text
context_policy = fresh
```

hoặc field tương đương phù hợp schema hiện tại. Không được để `bootstrap = None` trở thành trạng thái mơ hồ rồi khi Resume tự rebase sang current default bootstrap.

Pre-boundary recovery của explicit fresh task được phép tạo fresh page mới khi đồng thời thỏa:

```text
context_policy = fresh
no attachments owned by lost page
no accepted user message
no send receipt
no message identity / irreversible send evidence
```

Flow:

```text
lost pristine fresh page
      |
      v
open new fresh ChatGPT page
      |
      v
bind same task/team/role
      |
      v
resume same durable request_id
```

Nếu đã có accepted/ambiguous send evidence thì không fresh-replay tự động; phải đi qua reconciliation/recovery hiện có.

Legacy task thật sự không có context policy có thể giữ compatibility rule riêng, nhưng không được làm mất explicit operator choice `--fresh` của task mới.

### Anchor extensibility

Bootstrap chỉ là một use case của generic branch anchor. Sau này nếu muốn fork từ bất kỳ assistant message đã biết nào, chỉ cần có:

```text
conversation_id
assistant_message_id
```

rồi gọi cùng `branch_from_anchor`. Không cần thêm transport mới hoặc UI automation mới.

### File/code boundaries dự kiến

Chỉ refactor các owner liên quan branch/context recovery:

```text
src/playwright_auto/cdpa_actions.py
- giữ/siết `branch_from_anchor(...)` làm canonical branch primitive
- canonicalize `new_branch` response identity
- không thêm DOM fallback

src/playwright_auto/cdpa_worker.py
- mọi selected donor đi qua `branch_from_anchor(...)`
- bỏ `use_ui_branch` / `_branch_from_bootstrap_ui(...)`
- giữ donor rotation + fail-closed send boundary
- persist/interpret explicit fresh context policy

src/playwright_auto/cdpa_store.py / dashboard create normalization nếu schema cần
- snapshot explicit fresh-vs-bootstrap context policy lúc create
- backward compatibility cho manifest cũ

src/playwright_auto/cdpa_cli.py / dashboard_api.py
- bảo đảm `--fresh`/`bootstrap_id: null` được materialize thành durable fresh policy thay vì chỉ là absence của bootstrap
```

Không mở rộng scope sang multi-CDP/Temporary UI ngoài phần state field cần thiết để giữ context policy.

### Test contract cho branch refactor

Thay mocked DOM contract bằng transport/state contract:

- bootstrap first allocation dùng đúng `/branch/{conversation_id}/{assistant_message_id}`;
- exact-donor Resume dùng direct branch URL;
- current/default-donor pre-boundary recovery cũng dùng direct branch URL, không gọi DOM branch;
- donor #1 fail trước send thì thử donor #2;
- hết donor thì regenerate/block đúng bootstrap policy;
- `new_branch` HTTP response cung cấp canonical conversation ID dù visible page URL còn `/c/WEB:*`;
- explicit `--fresh` persist durable fresh policy;
- explicit fresh + lost pristine pre-send page -> fresh reacquire cùng durable request, không tự lấy default bootstrap;
- accepted/ambiguous request -> không fresh replay;
- legacy manifest không có field mới vẫn load/recover theo compatibility rule đã định;
- xóa assertions/mocks hardcode menu text `Branch in new chat` / `Open new branch`.

Live acceptance nên dùng ít nhất một donor thật (ví dụ bootstrap của ThinkBook/G8) và chứng minh:

```text
C/A -> /branch/C/A -> new_branch 200 -> canonical conversation id -> bind -> send
```

Acceptance không cần click menu DOM.

---

# 18. Temporary setting theo browser

Mỗi endpoint có boolean:

```text
temporary_default = true | false
```

Dashboard toggle gắn vào endpoint đang chọn.

Flow task creation:

```text
CLI có --temp?
   |
   +-- YES -> conversation_mode = temporary
   |
   +-- NO  -> conversation_mode = endpoint.temporary_default
```

Task persist `conversation_mode` ngay lúc create.

Đổi Temporary setting của browser sau đó chỉ ảnh hưởng task mới.

Task đang chạy không reinterpret mode từ dashboard setting.

---

# 19. Temporary conversation lifecycle

Temporary workflow role start:

```text
conversation_mode = temporary
      |
      v
open https://chatgpt.com/?temporary-chat=true
      |
      v
bind role/page ownership
      |
      v
send role prompt
      |
      v
continue trên live tab cùng generation
```

Pattern independent-agent Temporary hiện có là reference chính cho lifecycle cơ bản.

Không coi Temporary conversation là saved `/c/<id>` conversation có thể reopen sau khi tab state mất.

---

# 20. F5 / lost Temporary tab

Temporary Chat có failure mode riêng:

```text
F5 / navigation / tab loss
      |
      v
page về new Temporary Chat
      |
      v
old conversation generation không còn usable
```

Recovery không dựa vào reopen URL.

Flow recovery:

```text
lost temporary generation
      |
      v
classify send/task state
      |
      +-- chưa qua irreversible boundary
      |      -> có thể tạo fresh temporary generation
      |
      +-- đã có accepted/ambiguous work
             -> build recovery snapshot
                    |
                    v
             open new Temporary Chat
                    |
                    v
             send full recovery context
```

Send provenance vẫn được dùng để tránh blind replay.

---

# 21. Recovery Snapshot

Recovery snapshot là state bền vững nằm ngoài browser.

Nó tổng hợp evidence đã có trong CDPA thay vì biến raw browser conversation thành source of truth.

Nội dung cần có ở mức phù hợp:

```text
Task identity
- task id
- team
- repository/worktree
- original goal

Execution state
- logical/physical role
- current hop/turn
- route/handoff state
- endpoint id
- conversation generation

Workspace evidence
- reports/checkpoints hiện có
- git status/diff summary
- files đã thay đổi nếu xác định được
- test/validation evidence đã persist
- relevant task logs

Model/browser evidence
- last accepted assistant response/progress có ích
- send receipt
- request state
- accepted/ambiguous/not-sent classification
```

Workspace/files/report là evidence authoritative hơn lời model nói đã làm gì.

Không cần mirror vô hạn toàn bộ chat transcript nếu task state + workspace đủ để reconstruct continuation.

---

# 22. Recovery prompt semantics

Persistent route repair hiện có thể dùng compact continuation vì conversation cũ còn context.

Lost Temporary generation cần full recovery context hơn.

Recovery context cần cho role mới biết:

- task/role đang tiếp tục;
- original goal;
- current hop;
- checkpoint/report/log hiện có;
- workspace đã có thay đổi;
- validation nào đã biết;
- last observed progress;
- send/request provenance.

Flow agent-side kỳ vọng:

```text
new temporary recovery role
      |
      v
inspect existing workspace
      |
      v
inspect reports/checkpoints/logs/git state
      |
      v
identify completed vs unfinished work
      |
      v
continue unfinished work
```

Mục tiêu là recovery từ durable state thay vì bắt đầu task từ đầu.

---

# 23. Bootstrap + Temporary

Policy đã chốt theo live transport và deterministic regression:

- Persistent + bootstrap dùng direct `/branch/{source_conversation_id}/{assistant_message_id}` và nhận canonical child từ `new_branch`; không dùng DOM menu fallback.
- Temporary luôn là Fresh context, không reopen durable bootstrap donor. F5 được nhận diện bằng document/generation identity, không chỉ URL.
- Store/API tính effective Temporary từ override của task và `temporary_default` của endpoint. Explicit bootstrap ID/snapshot cùng effective Temporary bị reject; Temporary creation không tự lấy default bootstrap.
- Không silently ignore bootstrap selection. Transport kết hợp bootstrap + Temporary chỉ được bổ sung khi có live evidence tương thích; contract hiện tại fail closed.

Live transport trước closeout đã quan sát Temporary không có durable anchor usable và F5 làm mất generation. Kết quả chi tiết và mức độ live coverage được ghi riêng trong phần closeout, không suy diễn từ mocked `/c/` tests.

# 24. SSE / streaming / snapshot reuse

CDPA đã có nhiều primitive:

- `SendReceipt`;
- accepted user/turn identity;
- stream status;
- frontend/network evidence;
- assistant message identity;
- response text/tail;
- task manifest;
- hop state;
- reports và workspace.

Refactor ưu tiên reuse các primitive này cho Recovery Snapshot.

Có thể tham khảo `miuuyy/codex-chatgpt-web` ở ý tưởng canonical state nằm ngoài Temporary browser conversation, nhưng không cần copy nguyên transport/protocol của repo đó.

Nếu current stream evidence đủ để snapshot tiến độ thì tận dụng trực tiếp. Chỉ thêm capture mới khi có một recovery scenario cụ thể cần evidence mà CDPA chưa giữ.

---

# 25. Các module dự kiến chạm

Danh sách để agent bắt đầu code reading; final split có thể điều chỉnh theo cấu trúc hiện tại.

## Config / endpoint registry

```text
src/playwright_auto/cdpa_config.py
src/playwright_auto/cdpa_defaults/cdpa.json
cdpa.yaml
src/playwright_auto/connection.py
```

## Durable task state

```text
src/playwright_auto/cdpa_store.py
src/playwright_auto/cdpa_store_independent.py khi cần compatibility
src/playwright_auto/cdpa_runtime_db.py
```

## Worker / routing

```text
src/playwright_auto/cdpa_worker.py
src/playwright_auto/cdpa_actions.py
src/playwright_auto/workspace.py
src/playwright_auto/chatgpt.py
```

## Ownership / projection

```text
src/playwright_auto/cdpa_browser_projection.py
src/playwright_auto/cdpa_projection.py
```

## Dashboard

```text
src/playwright_auto/dashboard_api.py
src/playwright_auto/dashboard_assets/app.js
src/playwright_auto/dashboard_assets/views/
src/playwright_auto/dashboard.html
```

## CLI

```text
src/playwright_auto/cdpa_cli.py
```

## Role runtime / hybrid cleanup

```text
src/playwright_auto/role_runtime/controller.py
src/playwright_auto/role_runtime/model.py
src/playwright_auto/role_runtime/page_observer.py
src/playwright_auto/role_runtime/timeout.py
```

## Local managed browser lifecycle

```text
scripts/browser-gui.sh
ecosystem.config.cjs
```

Windows/local browser management có thể cần một abstraction khác nếu shell script Linux không phù hợp; external CDP attach không phụ thuộc phần này.

---

# 26. Test strategy

Mỗi behavior mới nên có test trước khi thay implementation tương ứng.

## Unit/config

- shorthand port -> localhost;
- host:port parsing;
- endpoint ID stable/persisted;
- legacy `cdp_url` adapter;
- duplicate endpoint validation;
- default endpoint selection;
- Temporary setting inheritance;
- `--temp` override;
- `--reuse-team` removal/help contract.

## Store/task

- new task snapshots initial/default endpoint as provenance/affinity, not permanent ownership;
- legacy task maps legacy endpoint for compatibility without preventing relocation;
- same-team roles may be placed on different eligible endpoints;
- browser default change does not mutate durable task policy;
- task persists conversation mode independently from execution placement.

## Worker

- allocate active role/execution to an eligible endpoint slot;
- endpoint disconnect isolation;
- reconnect affected endpoint independently while allowing safe relocation elsewhere;
- no composite `(endpoint_id, page_id)` ownership collision;
- per-endpoint active capacity + pool exhaustion waiting;
- one endpoint failure không disconnect pool;
- fixed hybrid observation sau khi bỏ `dom_only`.

## Projection/API/dashboard

- endpoint-scoped page inventory;
- endpoint health/auth projection;
- board filtering;
- default browser update;
- Temporary endpoint toggle;
- add/remove/drain flow.

## Temporary

- fresh temporary workflow role;
- same-generation continuation giữ live tab;
- F5/lost tab tạo new generation;
- recovery snapshot từ durable evidence;
- accepted/ambiguous send không blind replay;
- recovery continuation đọc workspace/report/checkpoint;
- Temporary mode remains durable while endpoint placement may change during recovery.

## Live acceptance trên ThinkBook

Dùng:

```text
9222
9202
```

Scenario cuối nên có ít nhất một task thật trên mỗi browser, sau đó test:

- simultaneous visibility;
- dashboard filter;
- disconnect một browser;
- reconnect;
- Temporary toggle theo browser;
- fresh Temporary task;
- simulated F5/lost generation và recovery.

---

# 27. Live acceptance: Multi-CDP + Temporary

Live acceptance cuối chạy trên ThinkBook sau khi Multi-CDP, bootstrap direct-branch, Temporary mode và Temporary recovery đã cùng có mặt. Dùng hai browser thật:

```text
127.0.0.1:9222
127.0.0.1:9202
```

Các scenario:

- cả hai endpoint được registry/probe thành công;
- dashboard thấy hai browser riêng;
- cả hai báo authenticated;
- default endpoint có thể đổi;
- task mới snapshots initial endpoint/provenance without becoming permanently bound to it;
- endpoint capacity is enforced and least-loaded eligible placement is deterministic;
- roles of the same team can execute on different browsers while preserving handoff/report continuity;
- inventory/page ownership phân biệt endpoint + page;
- tắt một endpoint không làm endpoint còn lại offline;
- pre-send execution on a lost endpoint safely relocates to another eligible endpoint without creating a new task/request;
- accepted/ambiguous Send never blind-replays; recovery continuation uses durable state when the old endpoint is unusable;
- reconnecting an endpoint restores its capacity without disturbing healthy peers;
- remote-style endpoint với host khác có cùng port `9222` được model hỗ trợ;
- legacy config một `cdp_url` vẫn load được.

Bổ sung vào cùng live run:

- persistent workflow chạy được trên cả `9222` và `9202`;
- Temporary workflow chạy được trên cả `9222` và `9202`;
- Temporary default theo từng browser được áp dụng cho task mới;
- `--temp` override browser setting;
- direct bootstrap `/branch/{conversation_id}/{assistant_message_id}` nhận canonical identity từ `new_branch`;
- explicit fresh task giữ fresh context policy qua recovery;
- F5/lost Temporary generation tạo recovery flow từ durable state;
- Recovery Snapshot giúp role mới tiếp tục từ workspace/report/checkpoint hiện có thay vì làm lại từ đầu;
- persistent và Temporary task có thể cùng tồn tại trong browser pool.

Read-only/smoke checks trên CDP có thể chạy sớm trong quá trình refactor. Live acceptance đầy đủ dùng hai CDP thật được gom ở bước cuối để luôn bao gồm Temporary.

---

# 28. Thứ tự triển khai đề xuất

## Phase A - Endpoint/config/store foundation

- [x] Endpoint model + legacy config adapter.
- [x] Endpoint parser/probe.
- [x] Durable endpoint identity for provenance plus role/execution placement evidence; no permanent team ownership.
- [x] Per-endpoint `max_active_tabs` capacity (default 2) and explicit `rate_limit_scope`.
- [x] Default endpoint + Temporary default schema.
- [x] Backward compatibility cho config/task cũ.

Kết quả phase: durable browser identity và task assignment contract đã rõ trước khi worker dùng nhiều connection.

## Phase B - Worker multi-endpoint runtime + ownership

- [x] Worker endpoint pool với per-endpoint reconnect.
- [x] Role/execution slot allocator over existing scheduler/queue machinery.
- [x] Capacity release from durable running-execution state, not historical tabs.
- [x] Safe cross-endpoint relocation classified by Send boundary.
- [x] Composite physical page identity.
- [x] Endpoint-aware send/browser provenance khi recovery cần tới physical ownership.
- [x] Multi-endpoint browser inventory/projection.

Kết quả phase: một worker vận hành nhiều CDP độc lập và một endpoint lỗi không làm sập pool.

## Phase C - Dashboard/browser management

- [x] Browser selector/scope trên dashboard hiện có.
- [x] Add browser flow.
- [x] Health + auth state.
- [x] Set default.
- [x] Managed-local vs external semantics.
- [x] Remove/drain flow.
- [x] Local create/profile/clone flow nếu host là localhost.

Kết quả phase: operator quản browser pool từ dashboard hiện tại.

## Phase D - CLI + observation cleanup

- [x] Bỏ `--reuse-team` public option và contract liên quan.
- [x] Giữ positional task + `--task`.
- [x] Thêm endpoint override theo parser thống nhất nếu CLI cần chọn browser trực tiếp.
- [x] Bỏ `dom_only` setting/toggle/branch.
- [x] Hybrid observation trở thành path duy nhất.

Kết quả phase: public/runtime surface gọn và ổn định trước khi đổi conversation lifecycle.

## Phase E - Persistent bootstrap direct branch + explicit fresh policy

- [x] Persist donor anchor pair `(conversation_id, assistant_message_id)` theo contract hiện tại.
- [x] Dùng direct `/branch/C/A` làm branch transport chung.
- [x] Canonicalize `new_branch` response identity.
- [x] Chuyển first allocation, Resume, pre-boundary recovery và donor rotation sang cùng branch primitive.
- [x] Bỏ dependency production vào DOM branch menu.
- [x] Persist explicit fresh context policy và giữ fresh recovery semantics trước send boundary.

Kết quả phase: bootstrap persistent không còn phụ thuộc DOM branch vừa thay đổi của ChatGPT.

## Phase F - Temporary workflow

- [x] Per-endpoint Temporary default.
- [x] `--temp` override.
- [x] Persist `conversation_mode` lúc task creation.
- [x] Workflow fresh Temporary generation dùng pattern independent agent phù hợp.
- [x] Same-generation continuation giữ live tab.
- [x] Probe bootstrap + Temporary compatibility bằng live evidence và materialize policy rõ ràng.

## Phase G - Temporary recovery

- [x] Detect lost/F5 Temporary generation.
- [x] Define Recovery Snapshot từ task/workspace/send evidence.
- [x] Persist/update snapshot ở các transition phù hợp.
- [x] New Temporary generation nhận recovery context.
- [x] Recovery role inspect workspace/report/log rồi tiếp tục.
- [x] Accepted/ambiguous request giữ non-replay semantics.

## Phase H - Live end-to-end acceptance

- [x] Chạy acceptance trên cả ThinkBook `9222` và `9202`.
- [x] Verify auth detection, capacity selection, same-team cross-endpoint placement, and affinity-as-preference only.
- [x] Verify pre-send relocation, accepted-send non-replay recovery, pool exhaustion waiting, rate-limit-scope isolation, and capacity release.
- [x] Verify disconnect/reconnect isolation.
- [x] Verify persistent workflow trên hai endpoint.
- [x] Verify Temporary default và `--temp` trên hai endpoint.
- [x] Verify direct bootstrap `/branch/C/A` + canonical `new_branch` identity.
- [x] Verify explicit fresh recovery semantics.
- [x] Verify F5/lost Temporary generation + Recovery Snapshot continuation.
- [x] Verify persistent và Temporary task cùng tồn tại trong browser pool.
- [x] Verify legacy single-CDP config/task compatibility.

Read-only probe hoặc smoke check có thể chạy sớm khi cần evidence. Bài live acceptance hoàn chỉnh dùng hai CDP thật nằm ở phase cuối để luôn kiểm tra cả Temporary.

### Closeout evidence - 2026-09-13

- Browser pool live trên `9222` và `9202`: cả hai endpoint `online`, `authenticated`, `chatgpt_ready`; allocator chuyển sang `9202` khi `9222` full, cooldown hoặc draining, và detach không làm chết browser.
- Transport live artifacts `acceptance-20260913/transport-run2/9222-transport.json` và `transport-run3/9202-transport.json` đều PASS: persistent Send, direct `/branch/C/A` nhận `new_branch` 200 với canonical child identity, Temporary Send trên `?temporary-chat=true`, F5 đổi document và mất prior Temporary generation. Probe giữ persistent/branch sống trong lúc tạo Temporary rồi mới cleanup, nên hai mode đã coexist thật trong cùng browser pool. `temporary_default`/`--temp` resolution được khóa bằng contract tests cùng live Temporary transport trên cả hai endpoint.
- Persistent live: pre-Send `NEW` và `PROMPT_SET` relocation giữ nguyên task/hop/request rồi Send đúng một lần; accepted-Send recovery đi tới `DONE` mà không replay original request. `PROMPT_SET` cũng đi tới `DONE` với bytes kết quả đúng; harness chỉ fail hậu kiểm vì hai tab không thuộc probe được mở độc lập trong lúc bài test dài đang chạy.
- Temporary live: F5 làm đổi document identity, endpoint nguồn bị partition, worker tạo `execution_recovery` trên endpoint còn lại và tiếp tục bằng recovery snapshot; bản chạy từ `9202` sang `9222` vượt qua recovery generation, chạm guard ba PLAN liên tiếp, sau durable Resume tiếp tục đúng state và kết thúc `DONE` mà không replay request gốc. Một run đối xứng gặp composer-verification timeout trên UI hiện thời; đây được giữ là live limitation, không dùng để nới non-replay invariant.
- Synthetic ambiguous-Send live cố ý xóa receipt sau một Send đã thật sự accepted và vì vậy va vào fail-closed manual-composer guard. Deterministic recovery tests chứng minh lost ambiguous `SENDING/attempts=1/receipt=None` tạo `execution_recovery` child và không tăng attempt của request gốc; synthetic case không được coi là bằng chứng cho phép replay.
- Regression committed branch: `225 passed`. Full suite: `1097 passed, 11 failed, 1 skipped`; cùng 11 failure tái hiện trên baseline bất biến nên không phải regression của refactor.
- `mcp-thinkbook` account connector vẫn không ổn định từ ChatGPT client; live acceptance dùng Remote Desktop Commander làm filesystem authority và hai CDP thật làm browser transport. Đây là dependency ngoài repo, không đổi Send/recovery semantics.

---

# 29. Commit/review boundaries gợi ý

Refactor lớn nên chia commit theo behavior để dễ bisect/review:

```text
1. endpoint config/registry
2. durable endpoint provenance + conversation-mode snapshot
3. worker multi-endpoint execution pool + slot allocator
4. ownership + browser projection
5. dashboard browser management
6. CLI cleanup + fixed hybrid observation
7. persistent bootstrap direct branch + explicit fresh policy
8. multi-CDP cross-layer stabilization
9. workflow temporary mode
10. temporary recovery snapshot
11. end-to-end acceptance/documentation
```

Nếu một bước cần thay schema/state, migration/backward compatibility nên nằm cùng commit behavior đó thay vì để commit sau sửa state đã phát hành trong branch.

---

# 30. Completion evidence mong đợi

Cuối refactor nên có một report ngắn chứa:

- commit range;
- schema/config migration summary;
- CLI help mới;
- dashboard/browser-management flow đã triển khai;
- unit/regression tests đã chạy;
- Windows baseline exceptions còn tồn tại nếu chưa liên quan scope;
- live acceptance result với ThinkBook `9222` và `9202`;
- Temporary F5/recovery acceptance evidence;
- các compatibility decision cuối cùng cho bootstrap và shared-account rate limit.

Mục tiêu của report là cho thấy flow thực tế đã được kiểm chứng, không chỉ code paths tồn tại.

Final architecture principle:

> CDP endpoints are a capacity pool. Durable workflow state belongs to CDPA/workspace, not to a browser account. Preserve Send safety, then use any available endpoint to keep the task moving toward DONE.

## Astra follow-up resolution evidence — 2026-09-13

- The six findings from the first Astra review are integrated in the current feature worktree: accepted/ambiguous execution reconciliation now distinguishes CDP connectivity from allocation readiness; draining endpoints remain protected by unresolved durable accepted/ambiguous requests; pristine relocation keeps the same request ID while rebuilding prompt/context for the new generation and fails closed when durable proof is missing or corrupt.
- Managed-browser follow-up uses NUL-delimited POSIX argv parsing so profile paths with spaces remain intact, and canonical profile-path locks serialize start/clone operations across endpoint IDs with deterministic source/destination ordering.
- Partial endpoint disconnect is projected as unknown rather than confirmed offline for roles assigned to an unobservable endpoint; task availability becomes unknown when no role is confirmed online but at least one is unobservable.
- Multi-endpoint stability follow-up adds durable independent-agent browser affinity, requested-endpoint board visibility before allocation while actual placement wins afterward, PLAN turn-1 prompt de-duplication, cross-cycle semantic LISTEN/CTRL event de-duplication, terminal/unowned observation stop, immediate advancement after a valid route/report, and Windows Clear Team page-close tolerance.
- Operator documentation/help now states the shared browser pool, global dependency semantics, browser endpoint preference/affinity, Temporary/Persistent behavior, rate-limit scope, and supported controls.
- Focused regression after integration: `322 passed, 5 failed, 2 skipped`; all five failures are in the previously documented baseline set (Windows `SIGALRM`, two legacy projection/symlink cases, symlink report privilege, legacy inline-conflict recovery). Targeted new/changed-behavior gate: `22 passed, 2 skipped`.
- `uv run python -m compileall -q src tests` and `git diff --check` are clean aside from expected Git CRLF conversion warnings. The final full-suite run and single post-fix Astra review are recorded in the task evidence after completion.
