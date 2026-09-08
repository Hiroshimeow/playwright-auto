# PROBLEM.md

nếu có task nào được chạy từ problem thì xóa dòng đấy đi.

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

### P-042 — Resume rejects a proven fresh generation-1 preboundary role after operator New Chat

- **Status:** `RESOLVED`
- **Priority:** `HIGH`
- **Root cause:** `_recover_pristine_preboundary_sending()` permitted a lost donorless role only when `conversation_generation == 0`. A role that was provably fresh because the durable operator `new_chat` control created generation 1 before any durable Send evidence was therefore rejected as `preboundary_context_unrecoverable`, even when the exact RequestLedger was empty or pristine `NEW` with zero attempts and no binding, baseline, receipt, acceptance, or response evidence.
- **Concrete evidence:** `screens-pi-sdk-migration` / `cdpa-idem-a7d0363b3887a5e0e3008113` reached PLAN hop 1 / request `cdpa-idem-a7d0363b3887a5e0e3008113-hop1` with an empty exact ledger, generation 1, no receipt/conversation identity, and an applied operator `new_chat` control whose immutable snapshot proves generation 0 / `pre_send` / no prior page or conversation identity for the same hop, request, turn, role, and handoff. The original page disappeared before Send, and the old donorless generation guard dead-ended Resume.
- **Owner:** `cdpa-preboundary-newchat-recovery` / `cdpa-idem-9de92ce2ae014a4d81383805`.
- **Correction:** the existing Resume recovery owner now admits only that narrow durable operator-New-Chat provenance, rejects mismatched/stale provenance and attachment-owned transfer, and keeps the RequestLedger pristine/crossed boundary classifier authoritative. A proven fresh context is reacquired through the existing targeted fresh-role primitive rather than bootstrap branching, without incrementing `conversation_generation`, changing task/hop/request/turn/prompt identity, or crossing Send inside the Resume control. Legacy generation-zero and recorded-bootstrap-donor paths remain unchanged.
- **Verification:** focused generation-1 tests cover empty and pristine `NEW` ledgers plus unproven/mismatched provenance, crossed durable Send evidence, and attachments; the full Resume suite and durable/worker control suites are green. Live Resume control 10 on the original incident returned `continued` / `ownership_reacquired_before_send` with the same hop/request/turn, generation 1, and null receipt hash; normal worker continuation then crossed Send exactly once as the same request. Independent REVIEW passed the implementation boundary, and TEST reloaded the final source into the PM2 worker, proved healthy heartbeat/browser connectivity, reran the focused recovery set successfully, and confirmed the original ledger remained `COMPLETED` with `attempts=1` and no replay.

### P-041 — Resume UI bootstrap can erase known rate-limit classification before Send

- **Status:** `RESOLVED`
- **Priority:** `HIGH`
- **Root cause:** `_branch_from_bootstrap_ui()` reaches donor-turn UI actions before the bound `ChatGPTPage` clean-ready checks. If a known request/conversation-history throttle blocks an early action such as `hover()`, the generic exception path closes the page and wraps the raw browser failure as `BootstrapUIBranchError`; `_recover_pristine_preboundary_sending()` then receives `preboundary_context_unrecoverable` instead of the existing `RateLimitBlockedError` cooldown path.
- **Concrete evidence:** Pet MVP `cdpa-idem-55c14d0070ddc344654e11da` is blocked on TEST hop 3, exact request `cdpa-idem-55c14d0070ddc344654e11da-hop3`, before Send. Its exact durable record remains `NEW` with `attempts=0` and null binding, baseline, receipt, and accepted evidence. The retained failure is a `BootstrapUIBranchError` wrapping `Locator.hover` timeout where `modal-conversation-history-rate-limit` intercepted pointer events.
- **Correction:** this repair adds one read-only canonical known-rate-limit check at the generic UI-bootstrap exception seam while the failed page is still inspectable. Only raw/untyped UI failures are eligible for that late reclassification: typed `ChatGPTAutomationError` safety failures plus bootstrap semantic failures keep their stronger existing classification and fail closed. A proven raw throttle propagates `RateLimitBlockedError` to the existing global cooldown owner; failed/negative inspection preserves the original `BootstrapUIBranchError` path. The canonical lightweight detector also recognizes the exact known conversation-history rate-limit modal testid; no worker-local selector/parser or second recovery engine is added.
- **Owner:** `cdpa-resume-bootstrap-rate-limit-regression` / `cdpa-idem-b710df7ccc729ee1cd820f26`.
- **Verification:** DEV focused RED reproduced both the known-modal early-hover regression and its non-rate-limit twin. REVIEW turn 1 found a semantic `BranchBootstrapError` collision; DEV turn 2 fixed it. REVIEW turn 2 then reproduced the same taxonomy flaw for typed `PageOwnershipError`; DEV turn 3 fixed it with the existing `ChatGPTAutomationError` base-class boundary. Independent REVIEW turn 3 passed the positive raw-hover throttle case, raw non-rate-limit twin, semantic and typed-ownership negatives, manual-composer/unresolved-branch safety, and exact-once continuation. Final PLAN fresh verification passed the same focused gate: 13 targeted tests, 21 Resume/bootstrap tests, 16 worker bootstrap/rate-limit tests, and 23 actions branch/rate-limit tests; compileall and `git diff --check` also exited 0.

### P-040 — Resume can declare empty-ledger preboundary `sending` ready without recovering dead page ownership

- **Status:** `RESOLVED`
- **Priority:** `HIGH`
- **Root cause:** `_recover_resume_sending()` treated an existing exact request ledger with no matching request record as `await_durable_send / send_not_started` and returned without verifying or reacquiring the role page. `_sending()` acquires the owned page before `DurableSendBlock.run()` creates the request record, so a page that disappears after `_pre_send()` but before durable `begin()` leaves a legitimate preboundary `sending` hop that immediately falls back to the same dead binding after Resume.
- **Concrete evidence:** `continuous-job-parttime-sourcing-r45` / `cdpa-idem-71c84455b628c6103824e8b7` was `BLOCKED / role_offline` on hop 1 turn 1 with `receipt=null`; its exact `requests.json` existed with zero records. Three pre-fix operator Resume attempts, including `cmd-746a95aa-627e-4e6c-862d-cdf5dbfd25cc` and `cmd-4a5b3a5c-8650-4c78-b794-d061ed907cef`, returned `continued / await_durable_send / send_not_started` without replacing the missing page and then returned to `role_offline`. In contrast, the existing pristine-preboundary ownership path reacquires the exact recorded bootstrap donor without crossing Send.
- **Correction:** route the exact valid empty-ledger preboundary case through the existing `_recover_pristine_preboundary_sending()` ownership boundary instead of declaring unverified send readiness. Keep crossed/ambiguous SENDING recovery unchanged and do not create a request record from Resume.
- **Regression evidence:** focused TDD reproduced both the live-owner and dead-owner gaps before the source change; after the minimal control-flow correction, the empty-ledger dead-page case reacquires the recorded donor without Send, preserves task/team/hop/request/turn/prompt/hash/conversation generation, leaves the ledger empty until normal continuation, then accepts exactly one Send with `attempts=1`. Relevant pristine restart, crossed-record, cooldown, and Open-tab pre-send checks also pass. Bounded production recovery reloaded only `playwright-cdpa-worker` (browser PID unchanged) and issued exactly one new Resume, `cmd-be2f4c0c-3598-499d-a409-c49a0fdfd39d`: its durable result was `continued / reacquire_preboundary_role / ownership_reacquired_before_send` with `receipt_sha256=null`, replacing dead page `f63f5331-b36f-40cb-ac03-5d2135e73957` by `0e9c5b75-560b-420e-8f20-634e13f375dd` while preserving conversation generation 1. Normal continuation then produced one `SENT` ledger record with `attempts=1`; r45 is `RUNNING / waiting`, the replacement page is the sole live owned r45 page, and `role_offline` is cleared.
- **Owner:** `cdpa-preboundary-dead-page-recovery-regression` / `cdpa-idem-9e34976a115eb53c61455d61`.
- **Verification:** independent REVIEW re-read the focused diff plus durable control/ledger/runtime evidence, reran the focused recovery suite, and found no blocker. The empty-ledger branch cannot bypass preboundary ownership recovery, crossed/ambiguous Send guards remain unchanged, and bounded r45 recovery accepted the original request exactly once.

### P-008 — Worker state transitions and invariants are distributed across large mutable-dict branches

- **Status:** `OPEN`
- **Priority:** `LOW`
- **Root cause:** `_apply_control()` is a large action dispatcher that directly mutates status, kanban, block, pause, hop, role, and timeline fields. Core invariant resets such as `block_code`, `block_retryable`, and `block_reason` are duplicated across many branches and other worker paths.
- **Evidence:** `_apply_control()` begins near `cdpa_worker.py:895` and contains the control-specific transition logic inline. The same block-clear mutation cluster currently appears at multiple locations including lines near `528`, `952`, `974`, `993`, `1039`, `1160`, `1554`, and `2217`.
- **Impact:** a repair can update one transition but miss another, leaving contradictory status/column/block/hop state; review and regression attribution become difficult, especially around recovery and accepted-send boundaries.
- **Owner:** none. Do not combine this with schema replacement or a full state-machine rewrite.
- **Next verification:** first add characterization tests for every control/postcondition, then introduce a small set of invariant helpers and action handlers while preserving manifest schema, persistence boundaries, timestamps, hop identity, and control results. Require focused recovery and no-duplicate-send acceptance before completion.

### P-009 — Browser automation behavior is embedded and duplicated as large JavaScript strings

- **Status:** `OPEN`
- **Priority:** `LOW`
- **Root cause:** send, composer inspection, visibility checks, text extraction, and attachment detection are implemented as large JavaScript bodies embedded in Python, with overlapping helper logic across `chatgpt.py` and `upload.py`.
- **Evidence:** `click_send_button()` begins near `chatgpt.py:1033` and contains a large browser-side script; `wait_for_response()` begins near `3138`. Similar DOM helper and attachment-inspection logic exists in multiple scripts and upload paths rather than one tested contract.
- **Impact:** selector or ownership fixes must be repeated, subtle semantic divergence is easy, Python review obscures browser-side behavior, and ChatGPT DOM changes are harder to isolate from local regressions.
- **Owner:** none. This is lower priority than P-006/P-007 and should not be attempted during active runtime feature stabilization.
- **Next verification:** extract shared scripts without changing call semantics, retain exact argument/result contracts, add fixture-based browser-script characterization tests, and prove upload ownership, composer conflict handling, accepted-send detection, and duplicate-send guards remain unchanged.

### P-012 — Remote execution target identity is implicit rather than durable task state

- **Status:** `OPEN`
- **Priority:** `LOW`
- **Disposition:** `DEFERRED_LOW_IMPACT`
- **Current classification:** `B — REAL BUT LOW-IMPACT / TOO NARROW`
- **Current root cause:** same-host control-plane and execution repositories are now operationally separated: create accepts repositories inside configured allowed roots, task state persists that execution repository, workflow prompts use it as `workspace`, and report roots follow it. The remaining gap is limited to true cross-host execution: durable task state still has no typed executor/host/workspace identity, so remote repository and MCP authority are carried in task text while the task `repository` remains a local Linux path.
- **Current evidence:** focused current-code verification passes for repository inference, persisted cross-repository creation, file-only cross-workspace routing, cross-workspace report hydration, and the explicit Windows-remote fallback. A live FPT workflow uses `/home/ayumi/Workspace/fpt/0808-format-worktrees/excel` as durable execution repository while the control plane remains `/home/ayumi/Workspace/git_project/playwright-auto`, and its role reports are available. Two recent ThinkBook workflows completed `DONE` even though their durable `repository`/prompt `workspace` remained the Linux control repository and the actual Windows repository plus `@mcp-thinkbook` authority lived in task text.
- **Impact:** no current evidence shows wrong-host edits, failed routing, inability to create/manage the workflow, task loss, or cross-target writes caused by the missing typed remote execution identity. The demonstrated remote operational defect is report-byte availability, tracked separately by P-039; do not duplicate that fix here.
- **Owner:** none. No production fix task is justified under current evidence.
- **Revisit trigger:** reconsider only when a concrete normal operation fails because task text plus explicit MCP authority is insufficient to identify the execution host/workspace safely. At that point, fix the smallest demonstrated boundary; do not pre-build a target registry, capability framework, migration layer, or remote execution abstraction without such evidence.

### P-022 — CHECK_ALL independent jobs cannot bind the single incident they select

- **Status:** `OPEN`
- **Priority:** `MEDIUM`
- **Root cause:** a canonical `check_all` event is activated with `target_task_id: null`, but `_queue_independent_task_control()` requires the requested target to equal the active event target and `_create_independent_repair_command()` rejects an active event without a target. The Resumer contract requires scanning all tasks, selecting exactly one incident, then using those two commands; the runtime provides no durable one-time claim/bind transition for that selection.
- **Concrete evidence:** Resumer event `check-all:resumer:991949` started at `2026-07-31T14:30:00Z` with no target. It selected task `cdpa-idem-593e2ebe2e10a6f96d24d9a0`, which is `BLOCKED` on PLAN hop 60 with `accepted_user_provenance_ambiguous`. The owned conversation contains a newer explicit operator steering turn and a complete assistant route to DEV, while the accepted hop-60 user turn is absent from the current branch. Resume/retry would re-enter the obsolete receipt boundary, and the only independent control/repair APIs reject the selected task before any action because the active CHECK_ALL event remains targetless.
- **Impact:** a periodic whole-runtime watchdog can detect and diagnose the highest-priority incident but cannot safely recover it or create the required deduplicated repair task. The job must escalate to the operator even when the evidence and desired target are unambiguous.
- **Required correction:** add one idempotent, durable, same-event operation that binds an active targetless `check_all`/interval job to exactly one currently eligible non-independent task before control or repair. Preserve event identity, record the selected task/hop, reject rebinding, revalidate canonical eligibility, and reuse the existing command mailbox/TaskStore—no second queue or recovery path.
- **Next verification:** activate a targetless CHECK_ALL job, select one blocked task, bind it once, apply an exact-target safe control or create/reuse one repair, reject a second target and stale/replayed commands, restart the worker between claim and action, and prove zero accepted-send replay or ownership drift.

### P-023 — Manual Run completion disables a recurring Recovery agent

- **Status:** `OPEN`
- **Priority:** `MEDIUM`
- **Root cause:** `_release_independent_job()` decides whether to preserve `independent.enabled` from the active event `trigger_type` only. Completing a `manual` Run-now event therefore sets `enabled=false` even when the same long-lived identity is configured as the global recurring Recovery owner. The trigger settings remain `recovery=true`, but status becomes `PAUSED` and canonical Recovery events can no longer be claimed.
- **Concrete evidence:** Maintainers `agent-7fa62f0a9c22c1b921092493-g4` remained `RUNNING / await_completion` with hop 2 already `responded` and a durable `REPAIR_REQUIRED` completion request from 2026-07-31. On 2026-08-01 Resumer replayed only that identical completion control; the job released, but source changed the agent to `PAUSED`, `enabled=false`, `pause_reason=independent agent disabled`. Resumer restored the exact pre-incident setting with `enabled=true`, after which API and manifest both showed `WAITING / waiting_trigger` with `recovery=true`. No work prompt or accepted send was replayed.
- **Contract mismatch:** the independent runtime specification says direct/manual one-shot agents auto-pause, while recurring Interval/Recovery agents remain enabled and return to waiting. A manual Run-now operation on an already recurring agent must not silently destroy its recurrence.
- **Required correction:** completion should preserve enabled state when the identity has a recurring trigger configured (`recovery` or interval/check-all recurrence), regardless of a one-off manual Run-now event. A truly manual-only identity should still auto-pause. Keep the same manifest, event history, exactly-once receipt and no successor generation.
- **Next verification:** configure a Recovery agent, invoke one manual Run-now job, complete it, and prove it returns to enabled `WAITING`; then prove a manual-only agent still pauses. Repeat for interval/check-all agents and verify no duplicate event claim after worker restart.

### P-038 — Shared-profile concurrency policy: maximum 2 workflow teams RUNNING

- **Status:** `OPERATOR POLICY ACTIVE — USE EXISTING DEPENDENCIES/QUEUE, NO NEW SCHEDULER`
- **Confirmed operational invariant (2026-08-11):** allow at most **2 workflow task teams in `RUNNING`** on the shared ChatGPT profile. Three workflow teams are materially more likely to hit rate limits, especially when one workflow creates additional verification/fix work.
- **Unit:** workflow team, not role count. `WAITING`, `BLOCKED`, `PAUSED`, `DONE`, and `STOPPED` do not consume a RUNNING slot.
- **Operating method:** use the existing dependency/queue machinery to serialize new verification and fix work. Do not create another scheduler, queue, sidecar, or generalized concurrency subsystem.
- **Fix-task rule:** every fix task created from a problem verification must have an explicit dependency and must commit its own production fix so the change can be traced or reverted.
- **Verification-task rule:** verify the defect against current source/runtime first. If the old failure no longer exists, or the impact is operationally negligible/too narrow, update `PROBLEM.md` and do not create a fix task.

## Candidate design investigations — GitHub Trending 2026-08-07

These are **not accepted CDPA requirements and not evidence-backed defects**. They are design candidates captured for later operator review. Do not create implementation work from this section unless the operator explicitly selects an item. Prefer copying a small useful primitive into the current CDPA architecture over integrating another orchestration framework.

### C-002 — Layer General Team Bootstrap and reusable knowledge instead of injecting all context

- **Source:** `TencentCloud/TencentDB-Agent-Memory` — https://github.com/TencentCloud/TencentDB-Agent-Memory
- **Why it is relevant:** its useful idea for CDPA is not the database product itself, but layered memory and selective context loading. The project separates raw conversation/facts/scenario knowledge/stable context and avoids injecting the entire memory corpus into every agent turn.
- **Concept worth borrowing:** separate stable bootstrap context, reusable project knowledge, and task-specific context, then load detailed knowledge on demand.
- **Potential CDPA shape:**

  ```text
  Stable bootstrap
   |- operating principles
   |- repository / server environment
   `- routing conventions

  Reusable knowledge
   |- LEARNING.md
   |- known incidents
   |- accepted patterns
   `- role-specific knowledge

  Task context
   |- goal
   |- handoff
   `- evidence
  ```

- **Desired behavior:** a bootstrap conversation receives only stable reusable context. PLAN/DEV/REVIEW branches receive task context plus a minimal role loadout. Detailed lessons, incidents, or repository knowledge are retrieved only when the current task actually needs them.
- **Expected benefit:** smaller branch prompts, less stale duplicated context, lower bootstrap renewal cost, clearer distinction between stable invariants and task-local evidence, and better scaling as `LEARNING.md` grows.
- **Risk:** a full memory platform would add MemoryCore/knowledge/proxy/LLM pipelines and create another subsystem before CDPA has demonstrated the need.
- **Boundary:** borrow the taxonomy and retrieval principle only. Do **not** install or integrate TencentDB Agent Memory unless a later measured context/retrieval problem justifies it.
- **Evaluation before build:** measure current bootstrap/task prompt size and identify context that is repeatedly injected but rarely used. A first implementation should be static/minimal and reuse current files or existing retrieval capability rather than adding a new database.

### C-004 — `agent-skills` / `superpowers`: mine individual skills, do not add another workflow layer

- **Sources:** `addyosmani/agent-skills` and `obra/superpowers`.
- **Current overlap:** CDPA already uses skill discovery and Superpowers-style smallest-relevant-skill selection plus PLAN/DEV/REVIEW, verification-before-completion, and report/route discipline.
- **Potential value:** periodically inspect for a specific high-value skill/checklist that is missing locally, especially around code review, debugging, verification, or planning.
- **Why not integrate wholesale:** marginal architecture value is currently low; another process layer would duplicate conventions CDPA already owns.
- **Boundary:** cherry-pick individual reusable practices only when they close a demonstrated gap. Do not make these repositories another mandatory orchestration dependency.

### C-005 — `cloudflare/computer`: monitor, but no current integration

- **Source:** `cloudflare/computer`.
- **Relevant concept:** standardized sandbox/workspace execution runtime for agents.
- **Current CDPA position:** execution is already provided through G8/ThinkBook/MCP surfaces, and current architecture work is trying to make execution targets explicit without adding unnecessary runtime layers.
- **Why defer:** the project is preview-oriented and its API/runtime contract may change; adding it now would not solve a demonstrated CDPA operational bottleneck and would create another execution abstraction.
- **Future trigger to revisit:** only reconsider if CDPA needs disposable remote sandboxes, stronger per-task isolation, or portable cloud execution that existing MCP executors cannot provide cleanly.

### Candidate priority / recommended order

| Candidate | Potential CDPA value | Initial effort | Recommended action |
|---|---:|---:|---|
| C-002 layered bootstrap/memory | High | Low if concept-only | **Bootstrap roadmap candidate** |
| C-004 agent-skills/superpowers | Low–medium marginal value | Low | **Mine individual ideas only** |
| C-005 cloudflare/computer | Low today | Medium/high | **Defer** |

**Recommended sequence after the selected C-003 and C-001 evaluations:** `C-002 bootstrap layering`.

The governing constraint for all candidates is simplification: do not integrate three new frameworks. Prefer a small primitive that replaces existing CDPA complexity. If a candidate cannot delete/consolidate current logic or produce measurable operational value, do not build it.
