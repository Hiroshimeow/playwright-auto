# PROBLEM.md

Persistent root-cause backlog for small operational defects discovered while CDPA tasks run.

This file is not a chronological incident log. Track one entry per stable root cause, update the existing entry when evidence or ownership changes, and keep resolved operational rules in `LEARNING.md` instead.

## Update rules

- Add only evidence-backed defects that remain unresolved, partially verified, or unowned.
- Do not add an intentional operator Pause, Stop, Restart role, New Chat, or Clear Team as a problem. Track only a system defect that mishandles that intent.
- Record the affected task/team, durable evidence, current owner or repair task, and the next verification needed.
- Use `OPEN`, `IN_PROGRESS`, `BLOCKED_EXTERNAL`, or `RESOLVED`.
- Mark `RESOLVED` only after the responsible task or repair is `DONE` and the required regression/live evidence passes.
- Deduplicate by root cause, not by incident ID, timestamp, team, or repeated symptom.

## Active problems

### P-001 — Operator-originated controls can be mistaken for recovery events

- **Status:** `IN_PROGRESS`
- **Root cause:** recovery selection must use durable control provenance rather than status labels alone. Legacy controls may have incomplete origin metadata.
- **Current architecture:** canonical recovery extraction excludes operator Pause, Stop, Clear Team, Restart role, and New Chat. Independent-agent controls use the distinct `independent_agent` origin plus immutable source task/event identity.
- **Current regression evidence:** trigger tests prove operator actions create no recovery event; direct-control tests prove an independent agent cannot target work outside its active canonical event.
- **Impact if regressed:** a recovery agent could reverse operator intent or create duplicate work.
- **Owner:** `independent-agent-runtime`.
- **Next verification:** controlled live operator Pause/Stop/Clear/Restart/New Chat must produce no Maintainers activation, report, or recovery control before this item is marked resolved.

### P-002 — Recovery actions have historically left a hidden manual Resume step

- **Status:** `IN_PROGRESS`
- **Root cause:** a recovery primitive could be recorded as applied after opening/resetting a role without atomically proving that the same task/hop/request left the matching block and resumed at the safe boundary.
- **Architecture applicability:** still valid with the new API. Exact-tab recovery is worker/browser behavior; frontend and API separation does not close this invariant.
- **Evidence:** historical controls reopened roles with `recovered: true` but still required a separate manual Resume. Current source contains stricter `OPEN_ROLE_TAB` postconditions and same-hop recovery for `pre_send` and accepted `waiting`, but the responsible task stopped before final independent acceptance.
- **Impact:** the user can remain the hidden final recovery mechanism while Maintainers reports success too early.
- **Owner:** replacement `cdpa-idem-003d1dfeb644f2673f7f7adc` / `maintainer-update` now owns final acceptance. Source recovery preserves the same hop/request, accepted receipt, exact conversation URL, and recorded page identity without a separate Resume control.
- **Current regression evidence:** pre-send and accepted-waiting automatic recovery tests pass, including exact-URL recovery that restores a drifted accepted `page_id` without changing the hop/request/receipt or creating an incident. The Maintainers `OPEN_ROLE_TAB` fallback now reuses the same recovery contract: `pre_send` retains clean readiness, accepted `waiting` skips it, exact-tab page-ID drift is restored, wrong-conversation outcomes remain explicitly `ineffective`, and the full worker suite passes 264 tests.
- **Next verification:** controlled live pre-send and accepted-waiting recovery must preserve hop/request/receipt, perform zero resend, create no incident for recovered transients, and require no later operator Resume.

### P-003 — Failed attachment upload is treated as an irreversible in-flight send

- **Status:** `OPEN`
- **Root cause:** hop state `sending` is included in the generic `IN_FLIGHT` guard even when upload failed before ChatGPT accepted a message and the hop has no receipt, message identity, rendered prompt hash, or response.
- **Evidence:** tasks `cdpa-20260724-162733-73d86cc1` and `cdpa-20260724-164821-73f7419c` blocked with `attachment_upload_failed`. Maintainers selected a clean New Chat because no send-boundary evidence existed, but worker controls were rejected with `cannot reset a role across an in-flight send boundary`.
- **Impact:** a recoverable durable upload request cannot use New Chat/restart recovery and must be manually stopped or repaired outside the normal recovery contract.
- **Owner:** no dedicated repair task or waiting repair dependency was found. `cdpa-20260724-170528-d68a7cc3` is `DONE` and proves the final upload happy path, but it does not cover recovery from a failed pre-acceptance upload.
- **Next verification:** distinguish pre-acceptance upload/sending from accepted send using receipt/ledger/message evidence; safely replay the same immutable attachment snapshot and request without a new hop or duplicate send; add focused regression and controlled live failure/recovery acceptance.

### P-004 — Suspended MCP/tooling incidents still need a complete automatic restoration path

- **Status:** `IN_PROGRESS`
- **Root cause:** fail-closed tooling suspension needs a narrow worker-owned capability probe and deterministic restoration of the exact suspended task/hop/request.
- **Architecture applicability:** still valid with the new API because tooling readiness and durable recovery are worker/Maintainers concerns, not dashboard read-path behavior.
- **Evidence:** earlier TEST reproduced an available capability endpoint while the incident remained `SUSPENDED`. Later `maintainer-update` turns added allowlisted MCP preflight work, but final TEST turn 15 was interrupted and the task was operator-stopped.
- **Impact:** after bounded tooling failures, a task can remain suspended indefinitely and require external intervention.
- **Owner:** replacement `cdpa-idem-003d1dfeb644f2673f7f7adc` / `maintainer-update` retains the existing bounded suspension/restoration implementation without architectural changes.
- **Current regression evidence:** the complete Maintainers/action/prompt contract suite passes 393 tests, including bounded capability preflight and preserved incident identity.
- **Next verification:** TEST must run the controlled unavailable→available loopback MCP restoration acceptance and prove the exact suspended task/hop/request resumes automatically.

### P-005 — Legacy worker-parsed PROBLEM.md update contract

- **Status:** `RESOLVED`
- **Resolution:** the special maintenance decision parser and worker-owned model-to-file translation were retired. Independent agents act through explicit MCP/API controls and return plain Markdown; the worker no longer parses a structured PROBLEM.md mutation from model output.
- **Evidence:** active source contains no maintenance decision parser, recovery array dispatcher, or `create_repair_task` control action. Repository problem updates remain ordinary evidence-backed file work rather than hidden worker translation.
- **Do not reintroduce:** no model response schema, automatic lesson/problem append path, or coordinator-specific persistence layer.

### P-006 — Dashboard read path repeatedly rebuilds global task state

- **Status:** `IN_PROGRESS`
- **Root cause:** this was the legacy dashboard architecture: every `/api/tasks` poll discovered and revalidated manifests, rebuilt dependency/index state, and amplified filesystem work at one-second cadence.
- **Architecture applicability:** the root cause is no longer present in the current dirty lightweight branch. Frontend is static, API reads SQLite projections/mailbox only, and the worker is the sole `.plan`/TaskStore owner. Keep this entry open until the replacement architecture is accepted and committed.
- **Current evidence:** live `/api/tasks` is approximately `34,056 B` and returned in roughly `1–3 ms` during the 23-task dataset. Current API source has no TaskStore/CDP discovery path.
- **Impact if regressed:** overlapping request threads, filesystem lock contention, high CPU, and multi-client amplification.
- **Owner:** `cdpa-20260725-205201-efc11447` / `dashboard-mobile-runtime-fix`, currently `RUNNING`; functional/API gates pass but current TEST retains a memory-stability blocker.
- **Next verification:** after task `DONE`, confirm production 9224/9225 uses only the lightweight FE/API boundary, unchanged polls cause no `.plan` scan/write, and 8/30/100-task API latency/payload remain bounded.

### P-007 — Runtime state has no single internal API/snapshot ownership boundary

- **Status:** `IN_PROGRESS`
- **Root cause:** the legacy implementation coupled dashboard serving, filesystem discovery, task projection, CDP inspection, and controls without one revisioned runtime owner.
- **Architecture applicability:** current source implements the intended split: worker owns `.plan`, TaskStore, dependency graph, CDP and mutations; API owns SQLite reads plus command mailbox; frontend is a static consumer. The issue is now deployment/final-acceptance risk rather than missing design.
- **Current evidence:** `/api/tasks`, task detail, `/api/state`, and command polling are served from `.runtime/cdpa-control.sqlite3`; browser/task projections are published by the worker. Frontend/API no longer import direct TaskStore/CDP ownership.
- **Impact if regressed:** inconsistent snapshots, duplicate writers, difficult caching, and unsafe alternate frontends.
- **Owner:** `cdpa-20260725-205201-efc11447` / `dashboard-mobile-runtime-fix` plus the parent lightweight refactor on `feat/cdpa-independent-runtime`.
- **Next verification:** final full regression, production process identity, crash/restart behavior, optimistic command/version checks, and proof that two read clients still leave exactly one manifest/CDP mutation owner.

### P-008 — Worker state transitions and invariants are distributed across large mutable-dict branches

- **Status:** `OPEN`
- **Root cause:** `_apply_control()` is a large action dispatcher that directly mutates status, kanban, block, pause, hop, role, and timeline fields. Core invariant resets such as `block_code`, `block_retryable`, and `block_reason` are duplicated across many branches and other worker paths.
- **Evidence:** `_apply_control()` begins near `cdpa_worker.py:895` and contains the control-specific transition logic inline. The same block-clear mutation cluster currently appears at multiple locations including lines near `528`, `952`, `974`, `993`, `1039`, `1160`, `1554`, and `2217`.
- **Impact:** a repair can update one transition but miss another, leaving contradictory status/column/block/hop state; review and regression attribution become difficult, especially around recovery and accepted-send boundaries.
- **Owner:** none. Do not combine this with schema replacement or a full state-machine rewrite.
- **Next verification:** first add characterization tests for every control/postcondition, then introduce a small set of invariant helpers and action handlers while preserving manifest schema, persistence boundaries, timestamps, hop identity, and control results. Require focused recovery and no-duplicate-send acceptance before completion.

### P-009 — Browser automation behavior is embedded and duplicated as large JavaScript strings

- **Status:** `OPEN`
- **Root cause:** send, composer inspection, visibility checks, text extraction, and attachment detection are implemented as large JavaScript bodies embedded in Python, with overlapping helper logic across `chatgpt.py` and `upload.py`.
- **Evidence:** `click_send_button()` begins near `chatgpt.py:1033` and contains a large browser-side script; `wait_for_response()` begins near `3138`. Similar DOM helper and attachment-inspection logic exists in multiple scripts and upload paths rather than one tested contract.
- **Impact:** selector or ownership fixes must be repeated, subtle semantic divergence is easy, Python review obscures browser-side behavior, and ChatGPT DOM changes are harder to isolate from local regressions.
- **Owner:** none. This is lower priority than P-006/P-007 and should not be attempted during active runtime feature stabilization.
- **Next verification:** extract shared scripts without changing call semantics, retain exact argument/result contracts, add fixture-based browser-script characterization tests, and prove upload ownership, composer conflict handling, accepted-send detection, and duplicate-send guards remain unchanged.

### P-010 — `/api/tasks` duplicates complete task objects across response sections

- **Status:** `IN_PROGRESS`
- **Root cause:** legacy `/api/tasks` returned full task objects repeatedly across `tasks`, `active`, `offline_recoverable`, and `history`, including large timeline/report fields.
- **Architecture applicability:** current lightweight API uses compact summaries and lazy task detail, so the old duplication is no longer observed. Keep open until the active migration task reaches `DONE`.
- **Current evidence:** live board response is about `34 KB`; full task text/prompt/input/report details are absent from summaries; detail is fetched separately and ETag/304 is supported.
- **Impact if regressed:** repeated JSON allocation, browser parsing, bandwidth, and board rerender cost.
- **Owner:** `cdpa-20260725-205201-efc11447` / `dashboard-mobile-runtime-fix`, currently `RUNNING`.
- **Next verification:** board remains `<100 KiB`, task detail remains bounded/lazy, multiple clients do not trigger full-object duplication, and history pagination does not grow the board payload.

### P-011 — Long-running CDPA worker has abnormal resident-memory growth

- **Status:** `IN_PROGRESS`
- **Root cause:** not yet isolated. The former production worker once retained extreme RSS, and the current lightweight real-response acceptance still shows monotonic PSS growth despite the CPU hot loop being fixed.
- **Architecture applicability:** the old `~14.8 GB RSS` observation belongs to the legacy/hot runtime and must not be treated as the current steady-state baseline. The current issue is narrower: retained memory during authenticated active ChatGPT response observation.
- **Current evidence:** `dashboard-mobile-runtime-fix` TEST turn 3 measured worker PSS `+20.39 MiB` and current-source role-UI service PSS `+7.07 MiB` over 300 seconds, exceeding the warmed `<5 MiB per-process` gate. CPU passed (`0.403%` mean, `3.000%` p95). Current worker after restart is about `240 MiB RSS`, which proves the old 14.8 GB state is gone but does not close the five-minute slope.
- **Impact:** continued growth across long responses can accumulate, cause swapping, and invalidate the lightweight-runtime claim.
- **Owner:** `cdpa-20260725-205201-efc11447` / `dashboard-mobile-runtime-fix`, active DEV after the TEST memory blocker.
- **Next verification:** isolate Python versus Playwright-driver retention, warm before measurement, hold exact active response identity, require `<5 MiB` PSS growth per process over 300 seconds, stable FDs/threads/files, and repeat once after GC/task transition.

### P-012 — Task creation conflates the CDPA control-plane repository with the execution workspace

- **Status:** `OPEN`
- **Root cause:** the single task field `repository` currently represents both the local repository that owns the CDPA task store/configuration and the workspace in which an agent is expected to execute code. The dashboard is bound to one `CDPATaskStore`, resolves the submitted value as a local filesystem path, and rejects task creation unless it exactly equals `task_store.config.repository_root`. Worker prompts, route/repair validation, commands, maintenance, and workspace metadata also assume that the same value is a locally available `Path`.
- **Evidence:** `dashboard.py` rejects `/api/tasks` and `/api/tasks/resume` with `task repository must match the dashboard CDPA repository` when the submitted path differs from the dashboard root. The create dialog labels the field `Repository / workspace`, while `CDPATaskStore.create_task()` stores the resolved path as the task repository. Worker code later uses `state["repository"]` both as the agent workspace and as a local `repository_root` for repository-bound operations.
- **Impact:** a central/cloud CDPA control plane cannot create and manage a task whose code workspace lives on another machine and is accessible through a published MCP server. Absolute paths are host-specific, so even identical Git repositories may legitimately have different paths. Simply removing the equality check would be unsafe: it would permit arbitrary server-local paths while downstream code would still incorrectly attempt local filesystem operations against a remote workspace.
- **Owner:** none. This is related to the single-runtime-owner/API boundary in P-007, but requires a separate execution-target data model and migration.
- **Next verification:** split the current field into (1) a local control-plane/task-store root owned exclusively by CDPA and (2) an explicit registered execution target such as `{executor_id, mcp_alias, workspace, repository_identity, capabilities}`. Task creation must select an allowlisted target rather than submit an arbitrary server path. Preserve manifests, queue, dependencies, reports, and controls centrally; route code/file/shell operations through the selected MCP target; keep control-plane files local; use stable executor/repository identity instead of absolute-path equality; add target health/capability checks, backward-compatible migration for existing local tasks, and live acceptance proving one CDPA server can run separate tasks against at least two machines without cross-target writes or path confusion.

### P-013 — Manual user turns can disconnect an active role from routing

- **Status:** `FIXED IN CURRENT SOURCE — retain until independent acceptance`
- **Severity:** `CRITICAL`; a normal operator message must never deadlock a running task.
- **Root cause:** `assistant_turns_for_receipt()` stopped at the first later user turn. The tab and role were still online, but the worker ignored the assistant response following the operator message and kept polling the obsolete response segment.
- **Required recovery loop:**
  1. Check the exact role tab and ownership.
  2. If the tab is offline, reopen the last known exact conversation URL, restore the same role binding, then repeat step 1.
  3. If online, read the current DOM/API transcript.
  4. If the latest assistant response is still streaming, keep waiting.
  5. If the latest transcript item is a user turn, wait for the assistant response after that turn; never reuse an older assistant response.
  6. If the latest assistant response is complete, parse and apply its route.
  7. If the response is complete but does not satisfy the route contract, send the existing route-repair instruction in the same role tab and repeat from step 3.
- **Block boundary:** block only when the exact conversation cannot be restored, login/manual authentication is required, or task/role/page ownership is ambiguous. A later user turn by itself is not an error and does not invalidate the active hop.
- **Implementation:** response selection now continues across later user turns and uses only assistant responses after the latest user turn. Exact previously persisted assistant identity recovery remains supported. No new task, hop, or duplicate Send is created merely because the operator intervened.
- **Live evidence:** `dashboard-mobile-runtime-fix` REVIEW hop `17` had a manual operator turn followed by a valid `DEV` route and existing report, while the manifest remained `waiting`. After the fix and worker restart, the worker consumed the visible response, materialized REVIEW report turn 3, routed hop `17` to `DEV`, and created DEV hop `18` without JSON intervention or duplicate Send.
- **Regression evidence:** operator user turn followed by assistant routes correctly; a later user turn with no assistant does not reuse the previous assistant; exact expected assistant identity before a later turn remains recoverable.
- **Owner:** replacement `maintainer-update` task `cdpa-idem-003d1dfeb644f2673f7f7adc` for final independent acceptance and the offline exact-URL reopen path shared with P-015.
- **Next verification:** live online steer, multiple user turns, streaming response, malformed route repair, worker restart, exact URL reopen, and proof of no duplicate Send or false timeout.

### P-014 — Dashboard can remain RUNNING after a complete visible response

- **Status:** `FIXED FOR THE USER-TURN FAILURE CLASS — retain until independent acceptance`
- **Root cause:** the worker treated process/tab liveness as progress even when its response selector could no longer reach the latest visible assistant response.
- **Impact:** completed work and a valid route remained visible in the owned tab while the task stayed `RUNNING / wait_response`, blocking dependencies and exact-team ownership.
- **Correction:** every waiting cycle must execute the P-013 recovery loop. `RUNNING / wait_response` is valid only while the exact owned tab is responding or awaiting the assistant after the latest user turn. A complete response must proceed immediately to route validation; an invalid complete response must enter route repair rather than wait for the generic timeout.
- **Live evidence:** hop `17` advanced from `wait_response` to `validate_route`, then routed to DEV hop `18`; worker remained online and browser ownership remained intact.
- **Next verification:** no complete visible response may remain in `wait_response` for more than one bounded polling cycle, including after operator messages, F5, worker restart, or exact-tab reopen.


### P-015 — Active-role F5/network loss blocks before deterministic exact-tab recovery

- **Status:** `IN_PROGRESS`
- **Root cause:** `_owned_or_block()` previously moved the current active role directly to `BLOCKED` when its exact tab was absent or on the wrong conversation URL. DEV now invokes one bounded exact-URL reopen before blocking, limited to the active hop in `pre_send`, `sending`, `sent`, or `waiting`.
- **Architecture applicability:** valid under both legacy and current API designs. This is worker/CDP ownership behavior; API transport does not fix it.
- **Required narrow scope:** apply only to a nonterminal task's `active_role` whose active hop is `pre_send`, `sending`, `sent`, or `waiting`. Ignore uncalled/idle roles.
- **Required sequence:** recheck exact role/team/task/page identity with a short bounded grace; if online verify exact recorded conversation URL; if URL is wrong or tab is absent, reopen the saved `/c/<conversation-id>`, restore the same `page_id`/role/team/task, and continue the same hop/request without resend.
- **Block boundary:** enter `BLOCKED` only when the exact URL cannot be restored, redirects away, authentication/manual intervention is required, ownership is duplicated/ambiguous, or the accepted-send `page_id` cannot be re-established.
- **Current source evidence:** automatic recovery verifies the saved `/c/...` identity, repairs URL drift on the existing exact-owned tab or reopens a missing tab, restores role/task/team/page identity even when an already-correct surviving tab has a drifted `page_id`, skips clean-composer readiness for accepted waiting, and leaves unused roles untouched. The verified `OPEN_ROLE_TAB` fallback now invokes that same worker recovery path rather than duplicating locate/open logic. The full worker suite passes 264 tests; action/offline-recovery suites pass 32 tests.
- **Next verification:** TEST must perform controlled F5, tab close, brief CDP/network unavailability, and URL drift against the active role only; prove no new hop/request, duplicate send, operator control, or Maintainers incident for recovered transients.

### P-016 — Choice-prompt detector can classify the injected CDPA role badge as a ChatGPT choice

- **Status:** `IN_PROGRESS`
- **Root cause:** the detector scanned every visible button and used broad substring markers including `run`. DEV now excludes the complete current/legacy CDPA badge and role-control subtrees, requires a ChatGPT main/dialog/modal owner, and uses bounded word/phrase semantics.
- **Architecture applicability:** valid under both legacy and current API designs. It is shared browser automation code.
- **Evidence:** `dashboard-mobile-runtime-fix` blocked with `choice_prompt_blocked`; the captured label was the CDPA badge ending in `Automation role indicator and control`, not a ChatGPT choice prompt.
- **Impact:** a healthy active response can be marked non-retryable `BLOCKED` after F5/transient composer disappearance.
- **Required correction:** exclude the complete CDPA overlay/control subtree by stable IDs/data attributes before choice classification; require ChatGPT-owned prompt/dialog context and bounded whole-label semantics rather than generic substring `run`.
- **Current regression evidence:** full and sparse snapshot classifiers plus safe-choice click use the same text/ARIA candidate rule, ignore metadata-only `data-testid="run-action"`, `runtime`, nested `runner-start-approve`, and overlay `Allow` labels, detect and click one genuine dialog `Continue`, and fail closed without clicking when both `Continue` and `Allow` are eligible; the full browser-safety suite passes 110 tests.
- **Next verification:** TEST must confirm the production role overlay cannot create `choice_prompt_blocked` after F5 while a genuine ChatGPT `Continue/Allow/Proceed` prompt remains fail-closed and resolvable.

### P-017 — Canonical recovery activation must survive restart without duplicate or lost claims

- **Status:** `IN_PROGRESS`
- **Root cause:** recovery triggers are derived from canonical task state and claimed onto the one WAITING independent task. A claim must be durable before send, completion, or successor creation, and operator transitions must not be reclassified as recovery.
- **Current architecture:** one enabled agent may own the exclusive recovery trigger. The worker selects the oldest eligible event, persists `active_event` plus watermarks on the independent task, and reuses the durable request ledger after restart. No second incident store or supervisor exists.
- **Current regression evidence:** focused tests cover oldest-first claim, exclusive ownership, operator/self exclusions, restart-safe claim, queued completion, deterministic successor creation, and Monitor-to-Maintainers deduplication.
- **Impact if regressed:** an eligible block may be skipped, duplicated, or acted on after it becomes stale.
- **Owner:** `independent-agent-runtime`.
- **Next verification:** controlled live A/B blocked ordering with a worker restart between claim/send/completion must prove one claim, zero duplicate send, one successor, and then activation of B.

### P-018 — Runtime benchmark can stop the production worker and leave stale RUNNING projections

- **Status:** `OPEN`
- **Root cause:** the acceptance script `dev3-artifacts/run_benchmark.sh` executes `pm2 stop playwright-cdpa-worker` before launching an isolated fixture. Its EXIT cleanup attempts a restart, but a benchmark failure or PM2 race can leave the production worker stopped while API/SQLite continues serving its last projection.
- **Architecture applicability:** introduced during the current lightweight runtime task; unrelated to the legacy dashboard API defect.
- **Evidence:** the dev3 isolated benchmark failed with `isolated Playwright driver missing`, leaked PM2 process `dev3-exact-wait`, and left production stopped. The defect repeated during dev5: `run_real_wait_acceptance.sh` deliberately stopped production for its full warm-up and 300-second sample, leaving API commands queued and both active tasks frozen until EXIT cleanup restarted the worker. The dev5 cleanup succeeded, but normal control-plane availability was still removed for several minutes by a test harness.
- **Impact:** false RUNNING state, no command application, no response observation/routing, no Maintainers, and possible accidental operational downtime from a test harness.
- **Required correction:** benchmark must be isolated without stopping production. If any acceptance ever manipulates a production service, restoration must be a fail-closed verified postcondition: PM2 online, fresh heartbeat, CDP connected, exact task/hop identities preserved, and no leaked fixture process.
- **Next verification:** benchmark runs without stopping production; forced isolated failure leaves production PID/service and heartbeat healthy; active tasks and mailbox commands continue advancing; no fixture/worker/driver process remains.

### P-019 — Mailbox reports task control applied before a WAITING task applies it

- **Status:** `OPEN`
- **Root cause:** the API command loop marks a `task_control` mailbox command `applied` after `TaskStore.request_control()` durably appends a requested control. Actual task transition occurs later in `advance()`. A task held in `WAITING` on a non-ready dependency is not scheduled through the control-application path, so the requested operator action can remain pending indefinitely while the public command is already terminal `applied`.
- **Architecture applicability:** specific to the new API/mailbox split. The API correctly avoids direct manifest mutation, but the command/result contract currently conflates command delivery with control postcondition.
- **Evidence:** Stop commands for the six superseded legacy WAITING tasks returned mailbox status `applied`, while each task remained `WAITING`, each control remained `status: requested`, and no stop result/applied timestamp existed. The first task remained unchanged for more than 60 seconds and across normal worker cycles. Replacement tasks could still reuse the exact teams because WAITING is not an active-team ownership barrier, but the old tasks remain visible as nonterminal history.
- **Impact:** dashboard/API can claim success when nothing operational happened; Stop/Pause/other controls on dependency-held tasks may never apply; automation cannot safely wait on command completion; stale nonterminal tasks remain visible and can complicate eligibility or operator reasoning.
- **Required correction:** separate mailbox delivery from task-control completion, or keep the mailbox command nonterminal until the worker observes the action-specific postcondition. Requested controls must make their task immediately due regardless of dependency/queue waiting. Preserve optimistic task version, idempotency, operator provenance, and one mutation owner.
- **Next verification:** a WAITING dependency task receives Stop through API, mailbox remains queued/running until the task becomes `STOPPED`, control becomes applied with result, and no browser tab/send is created; repeat for Pause where valid and for stale-version/idempotent replay.

### P-020 — Queued `independent_complete` is not finalized after the assistant response arrives

- **Status:** `OPEN`
- **Root cause:** when `independent_complete` is submitted before the current independent-agent assistant response has been durably observed, the worker records `completion_request` and returns `queued_until_response`. After that same hop later becomes `responded`, the normal advance path does not re-evaluate the pending completion request, call `complete_independent_task()`, or create the deterministic WAITING successor. The task remains `RUNNING` with `active_action: await_completion` indefinitely.
- **Architecture applicability:** specific to the independent-agent mailbox/response reconciliation boundary. The API correctly queues completion before response ownership is proven, but completion must be finalized by the worker immediately after the exact response is accepted.
- **Evidence:** monitor task `agent-9c247954cbd59ff60e4132b5-g1` submitted command `cmd-aaf0fabb-a0a3-41ea-bbce-44e646fcb006` for DEV event `role-complete:cdpa-idem-bce9fa8eaeeef92b369d749d:2:DEV`. The command was accepted while hop 1 was still waiting; the hop later became `responded` at `2026-07-29T22:36:24Z`, but the task stayed `RUNNING / await_completion` with the original `completion_request`, no `completed_at`, and no `successor_task_id`. Consequently the REVIEW event at `2026-07-29T22:37:15Z` and task-DONE event at `2026-07-29T22:40:13Z` were never claimed. Their Telegram reports had to be sent manually, then the stuck monitor was explicitly stopped.
- **Impact:** one valid early completion request can permanently consume the independent agent's sole nonterminal task, preventing successor creation and silently losing all later eligible events. For scoped reporters this means missing operational notifications; for recovery agents it could suppress later incidents while the UI still shows a RUNNING agent.
- **Required correction:** after an independent hop transitions to `responded`, atomically check for a pending `completion_request`; validate it against the same task, active event, hop, and accepted response identity; complete exactly once; publish the completed task; create exactly one deterministic WAITING successor; and advance event watermarks without replaying the accepted send. Reprocessing the same mailbox command or restarting the worker must be idempotent.
- **Owner:** none; belongs to `independent-agent-runtime` worker completion reconciliation.
- **Next verification:** submit `independent_complete` while the hop is still `waiting`, then allow the exact assistant response to arrive. Prove the original task becomes terminal, one successor becomes WAITING, no duplicate send or successor is created across worker restart/idempotent command replay, and subsequent REVIEW plus task-DONE events are each claimed once in order.

## Audited incidents and terminal tasks

| Evidence | Current assessment |
|---|---|
| Historical role-offline incidents on `unstopable3` | Operationally recovered and parent task is `DONE`; systemic atomic-recovery work remains tracked by P-002. |
| STOPPED parent `cdpa-20260724-162023-b059f49a` | Resolved by replacement `cdpa-20260724-162136-431a15c7`, which is `DONE`; no active problem. |
| STOPPED replacement fixtures `cdpa-20260724-160701-d8d8f160` and `cdpa-20260724-161638-558e7806` | Intentional disposable history with no active children found; do not create recovery work solely because they remain `STOPPED`. |
| Upload fixture `cdpa-20260724-163442-3527f3ba` | Resume succeeded before later manual Stop; operator history is not a Maintainers problem. |
| Upload fixtures `cdpa-20260724-162733-73d86cc1` and `cdpa-20260724-164821-73f7419c` | Manual Stop is authoritative; underlying failed-upload recovery defect remains P-003. |
| Final upload fixture `cdpa-20260724-170528-d68a7cc3` | `DONE`; proves normal upload/inline-report flow only, not failed-upload recovery. |
| Composer conflict on `maintainer-update` REVIEW hop 6 | Operationally recovered historically, but the task is now operator-`STOPPED`; hidden follow-up Resume remains part of P-002 acceptance for the replacement task. |
| Current API-v2 `WAITING` task chain | Replacement root `cdpa-idem-003d1dfeb644f2673f7f7adc` reuses exact team `maintainer-update` and waits for `dashboard-mobile-runtime-fix`; six API-v2 replacements follow it through `cdpa-idem-259c176a13b4bcb877b91398`. Legacy waiters have operator Stop requested but remain `WAITING` because of P-019. |
