# PROBLEM.md

nếu có task nào được chạy từ problem thì xóa dòng đấy đi.

Persistent root-cause backlog for small operational defects discovered while CDPA tasks run.

This file contains only active/unresolved root causes. Track one entry per stable root cause and update that entry when evidence or ownership changes. Once a problem is independently verified resolved, move its permanent trace to `DONE.md`; keep reusable operating rules in `LEARNING.md`.

## Update rules

- Add only evidence-backed defects that remain unresolved, partially verified, or unowned.
- Do not add an intentional operator Pause, Stop, Restart role, New Chat, or Clear Team as a problem. Track only a system defect that mishandles that intent.
- Record the affected task/team, durable evidence, current owner or repair task, and the next verification needed.
- Use `OPEN`, `IN_PROGRESS`, `BLOCKED_EXTERNAL`, or `RESOLVED`.
- Mark `RESOLVED` only after the responsible task or repair is `DONE` and the required regression/live evidence passes.
- Deduplicate by root cause, not by incident ID, timestamp, team, or repeated symptom.

## Active problems

### P-045 — Pre-send format-repair hop blocked by stale ChatGPT delivery-timeout banner

- **Status:** `OPEN`
- **Priority:** `HIGH`
- **Affected task:** `cdpa-idem-52db86a2e2c44b7f811a8d4d`, DEV hop 2, pinned endpoint `56b44af5-6790-430f-afe3-4f78766d58be`, conversation `6ac78a4c-2470-83e8-bf27-378def9b43aa`.
- **Concrete evidence:** durable manifest records `unexpected_error` / `UnsafePageStateError: page is in error state: ['Message delivery timed out. Please try again. Retry']`; hop 2 RequestLedger remains `new`, `attempts=0`, `accepted_at=null`, `binding=null`, `receipt=null`. Hop 1 is `completed`, accepted via exact user message and has a captured terminal response. Hop 2 is only JSON-format repair of hop 1's malformed route, not substantive work.
- **Impact:** a recoverable-looking UI error banner prevents a zero-attempt format-repair turn from beginning and strands the active task. The banner's provenance has not been proven; do not click Retry, restart, rebind, or replay the completed hop based on the string alone.
- **Owner:** unresolved; no independent lifecycle control or repair-task creation capability was exposed in this session. Preserve pinned endpoint, exact request/branch ownership, and explicit operator decisions.
- **Next verification:** read-only inspect the pinned tab/current composer, error-banner provenance, controller runtime and exact request ledger. If hop 2 is still provably pre-send and tab ownership matches, use one authorized `independent_task_control` release. Then verify the task advances without resending hop 1. If the banner handling recurs, create one deduplicated PLAN/DEV/REVIEW repair task using `independent_create_repair` to classify prior-turn transport errors separately from the new hop's send admission.


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
- **Impact:** no current evidence shows wrong-host edits, failed routing, inability to create/manage the workflow, task loss, or cross-target writes caused by the missing typed remote execution identity. Previously demonstrated remote report/capacity defects are already resolved and removed from this active backlog; do not duplicate those fixes here.
- **Owner:** none. No production fix task is justified under current evidence.
- **Revisit trigger:** reconsider only when a concrete normal operation fails because task text plus explicit MCP authority is insufficient to identify the execution host/workspace safely. At that point, fix the smallest demonstrated boundary; do not pre-build a target registry, capability framework, migration layer, or remote execution abstraction without such evidence.

### P-043 — Stable malformed Solo PAUSE response is misclassified as generic state repair

- **Status:** `OPEN`
- **Priority:** `HIGH`
- **Root cause:** the role controller records a stable assistant candidate and its route-validation error, but a malformed terminal route does not transition promptly to `malformed_result`. The controller keeps waiting until the ten-minute timeout path returns `state_repair`, even though the rendered response has been unchanged for many samples.
- **Concrete evidence:** Solo DEV task `cdpa-idem-ae812dce02fca9d8b2fa57ac` rendered an intended `PAUSE` response whose `handoff` contained unescaped embedded quotes around `We couldn't connect your account`, making the JSON invalid. Hop 1 retained that exact assistant candidate for 33 samples; hops 2 and 3 repeated the same class of malformed `PAUSE` for 35 and 38 samples. None was accepted as a route; each eventually became `state_repair` after the timeout path.
- **Required correction:** when a terminal assistant candidate is stable and route validation still fails, classify it immediately as a malformed-result/format-repair condition. Do not wait for the generic response timeout and do not reinterpret malformed JSON as a valid `PAUSE`.
- **Relevant source:** `src/playwright_auto/role_runtime/response.py` records `validation_error` for a candidate; `src/playwright_auto/role_runtime/controller.py` decides whether a candidate is accepted/repaired; `src/playwright_auto/role_runtime/timeout.py` currently falls through to `state_repair` after the long timeout. The older `src/playwright_auto/chatgpt.py::wait_for_response()` path already has `StableMalformedResponseError` and is useful behavioral reference, not a second implementation target.
- **Relevant tests:** extend `tests/test_role_runtime_browser.py` for stable malformed candidate classification and `tests/test_cdpa_solo_task.py` for end-to-end Solo malformed `PAUSE` handling. Keep valid Solo `PAUSE`/`DONE` coverage in the same file as regression guard.
- **Checklist / verification:**
  - [ ] Reproduce a stable Solo `PAUSE` with invalid JSON quoting.
  - [ ] Prove the controller surfaces `malformed_result` after the normal stability/grace window, not after the ten-minute timeout.
  - [ ] Preserve fail-closed route parsing: malformed JSON must never become an accepted PAUSE/DONE decision.
  - [ ] Prove valid PAUSE and DONE responses still route normally.

### P-044 — Route repair prompt omits the concrete validation failure and lets malformed JSON repeat

- **Status:** `OPEN`
- **Priority:** `HIGH`
- **Root cause:** `_repair_route()` correctly persists `validation_error`, and the normal team workflow already feeds that error through `PromptBuilder.repair()`. The defect is specific to `workflow_mode=solo`: `_pre_send()` handles every non-task Solo hop with `build_solo(... continuation=True)`, so a Solo `route_repair` bypasses the existing repair prompt builder and loses the concrete parser error. The same malformed structure can therefore be regenerated repeatedly.
- **Concrete evidence:** in `cdpa-idem-ae812dce02fca9d8b2fa57ac`, three consecutive `DEV → DEV` route-repair hops were created. Each repair prompt was essentially `Continue from the current state ... Return exactly one valid JSON object`, while the actual defect was an unescaped `"` inside the `handoff` string. The role repeated malformed PAUSE output until `route_validation_exhausted` blocked the task.
- **Required correction:** give Solo `route_repair` an explicit minimal format-repair prompt carrying the exact `validation_error` and instructing the role to preserve its decision/content while returning only corrected JSON. Reuse the existing repair semantics where practical; do not redesign the team report-repair path, which already includes `Validation error:`. Do not redo task work. Keep retries bounded and never cross an already accepted Send boundary for the same hop.
- **Relevant source:** `src/playwright_auto/cdpa_worker.py::_pre_send()` has the early Solo branch that currently sends every continuation through `PromptBuilder.build_solo()`; `_repair_route()` in the same file stores `validation_error`. `src/playwright_auto/cdpa_prompts.py::build_solo()` currently has only generic continuation text, while `PromptBuilder.repair()` demonstrates the existing team repair contract with the exact validation error.
- **Relevant tests:** extend `tests/test_cdpa_solo_task.py` to assert a Solo `route_repair` prompt contains the concrete parser error and does not repeat the substantive goal. Use `tests/test_cdpa_repair_prompt_contract.py` and `tests/test_cdpa_worker.py::test_malformed_response_creates_same_turn_guide_only_repair` as existing team-path regression guards; they should continue passing unchanged.
- **Checklist / verification:**
  - [ ] Repair prompt includes the exact parser/contract error.
  - [ ] Repair prompt says to fix only the route format, not repeat repository work.
  - [ ] One malformed quoted handoff is corrected to valid JSON on the next repair in a focused regression.
  - [ ] Repeated invalid repair remains bounded and ends in one clear block without uncontrolled resend loops.
  - [ ] Exactly-once Send/no-resend invariants remain unchanged for the original work hop.

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
