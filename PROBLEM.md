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

### P-028 — Dashboard role transcript still depends on browser/worker observation instead of the completion graph already fetched for routing

- **Status:** `OPEN`
- **Priority:** `LOW`
- **Problem:** CDPA exposes role/tab availability and durable workflow state but does not preserve a lightweight role conversation transcript for the dashboard. The operator still has to open the real browser tab or add separate observation work to inspect completed role content.
- **Current capability:** stream-status-primary completion already performs exactly one authenticated `GET /backend-api/conversation/<conversation_id>` after `stream_status=COMPLETE`. That response contains the full conversation object, `mapping`, `parent`/`children`, message metadata, and `current_node`; the same graph is already required to resolve the exact accepted turn before routing.
- **Desired future feature:** reuse that exact completion graph read as the dashboard data source. When the worker successfully GETs the graph for routing, retain one deduplicated snapshot/reference keyed by `conversation_id` and expose an on-demand transcript projection per role. Do not poll the full graph every second and do not perform a second graph GET merely for dashboard display. While a response is still streaming, status/progress can remain lightweight; completed transcript data becomes available from the graph already captured at the terminal transition.
- **Storage boundary:** do not copy a 200 KiB+ raw graph into every hop, task manifest, timeline row, or normal SQLite dashboard projection. Prefer one latest snapshot per conversation (or an equivalent deduplicated runtime artifact) plus a bounded transcript projection/reference for the UI. Preserve task/hop/report/control state in the existing CDPA store; the ChatGPT graph is conversation content, not a replacement workflow database.
- **Architecture boundary:** the CDPA worker remains the only backend/browser owner. Dashboard reads must not Send, Stop, click, type, change page ownership, create a second poller, or bypass the existing command mailbox. A one-shot screenshot may remain a separate diagnostic action for popup/layout cases, but completed transcript rendering should not require DOM scraping or tab focus.
- **Impact/opportunity:** one backend read can serve both completion routing and role transcript display, allowing completed source-role tabs to be backgrounded or closed after durable receipt/response handling and potentially deleting substantial DOM/message-display observation code without adding a new service.
- **Owner:** none. The P-030 completion-graph regression is already repaired; this remains a low-priority dashboard simplification and should be built only if it still removes meaningful browser/DOM observation work.
- **Next verification:** only if this low-priority simplification is selected later, complete real PLAN/DEV/REVIEW turns, prove each successful terminal graph GET is reused without any extra graph polling, close a completed source tab, and verify the dashboard can still render the retained transcript while routing, task ownership, CPU, and no-duplicate-send invariants remain correct.

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
