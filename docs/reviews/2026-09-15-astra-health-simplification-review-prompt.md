You are an independent senior architecture/code-review judge. Evaluate the proposed CDPA simplification objectively from first principles. Do not assume the prior audit is correct, and do not assume simplification is automatically desirable. Preserve complexity when it protects a real irreversible boundary; remove it when it mainly compensates for prompt/model behavior or duplicates another guard.

IMPORTANT EXECUTION RULES:
- All material context needed for this review is embedded below.
- Do NOT inspect the filesystem, run shell commands, search the repo, or call tools. This is intentionally a one-shot review to minimize API/tool calls.
- Return only the JSON object required by the supplied output schema.
- Be concise but decisive. Prefer a smaller coherent plan over a broad rewrite.
- You may choose PASS, REVISE, or REJECT.
- PASS means the current WAITING cleanup team/task can start as written.
- REVISE means the direction is valid but task/audit/plan should change. Supply a complete replacement plan and task text. You may also supply revised_audit_markdown; if non-null it will replace the prior audit.
- REJECT means the current cleanup proposal is materially wrong. Supply a safer alternative plan/task text anyway.
- For REVISE or REJECT, normally choose current_waiting_team_action=STOP_AND_REPLACE unless there is a strong reason to keep it waiting.
- Decide independently whether the original health task should stay paused, stop, or resume later.
- Do not propose a second controller, scheduler, DB, queue, sidecar, or orchestration engine.
- Do not require commit/push/merge/reset/stash/clean/branch switch.
- The replacement plan must be executable by PLAN/DEV/REVIEW and must explicitly distinguish hard safety invariants from policy/heuristic gates.
- Target net-negative production LOC when safe, but do not use LOC reduction as a correctness criterion.
- Avoid one-test-per-internal-branch if the branch itself should disappear; retain focused regression coverage for externally meaningful safety invariants.

RAW OBSERVATIONS (not interpretation):
SOURCE HEALTH TASK CURRENT RAW STATE:
status=PAUSED
active_hop_id=65
active_role=DEV
active_action=paused
hops=65
route_events=70
created_at=2026-09-14T12:12:06.297164+00:00
updated_at=2026-09-14T20:22:58.996723+00:00

CURRENT WAITING CLEANUP TEAM RAW STATE:
task_id=cdpa-idem-a6585f80ee35f316628e44d5
team=cdpa-health-simplification-20260915
status=WAITING
active_hop_id=1
active_role=PLAN
active_action=waiting_dependency
waiting_code=dependency
depends_on=cdpa-idem-9a17c3402e7a2c99f92f0048
request_attempts=0
The source health task accumulated 61 role reports. Its original scope included operational snapshot, prompt/controller loop fixes, durable endpoint ownership, recovery of multiple product/dependency tasks (endpoint repair, Temporary Chat, Agent Company releases, MCP tasks), PAUSED semantics, dashboard truth, three live canaries, broad suite, runtime restart, final requirement table and learning pass. It is now PAUSED because a child REVIEW context/tool-picker probe left an unsent mcp-thinkbook plugin pill; fail-closed composer provenance then refused restart.

The control checkout is heavily dirty from concurrent work: last observed git diff stat was 74 tracked files changed, +5644/-465, plus health-related untracked tests. Those numbers MUST NOT be attributed wholesale to the health task because multiple teams changed the same checkout concurrently.

Current known regression evidence from the latest review boundary: compact/prompt/recovery transport bundle 11 passed; worker suite 98 passed, 1 deselected; Release F reached terminal DONE with exactly-once attempts on its final hops. These are evidence, not proof that every proposed guard is necessary.

CURRENT WAITING CLEANUP TASK PROPOSAL (candidate, not authoritative):
---BEGIN TASK---
Repository/worktree: E:\git-project\playwright-auto-multicdp

CLEANUP TASK - remove CDPA over-gating and over-engineering introduced/exposed during System Health Recovery. This task is intentionally held in WAITING for operator review. Do not execute any implementation until the operator explicitly approves/resumes this cleanup task and the temporary approval dependency is removed through the supported API.

PRIMARY AUDIT INPUT:
- Relative: docs/reviews/2026-09-15-cdpa-system-health-recovery-objective-audit.md
- Absolute: E:\git-project\playwright-auto-multicdp\docs\reviews\2026-09-15-cdpa-system-health-recovery-objective-audit.md

SOURCE TASK FOR CONTEXT ONLY:
- cdpa-idem-9a17c3402e7a2c99f92f0048 / cdpa-system-health-recovery
- The source task is currently PAUSED. Do not Resume/Retry/Restart/New Chat it or any child as part of this cleanup unless the operator separately asks.

OBJECTIVE:
Make the CDPA control plane materially smaller and easier to reason about by deleting/merging unnecessary gates, semantic parsers, duplicated recovery policies, and speculative abstractions. Prefer net-negative production LOC. Preserve hard correctness/safety boundaries; do not trade exactly-once or ownership safety for convenience.

KEEP / DO NOT WEAKEN without equivalent simpler proof:
1. accepted or ambiguous Send is never replayed;
2. durable request ledger remains authoritative for Send provenance;
3. selected endpoint/page ownership is durable before browser mutation;
4. Temporary Chat document identity remains page-local and fail-safe;
5. expected report path/hash/size integrity remains enforced;
6. true human unsent drafts are preserved;
7. no direct runtime manifest/SQLite mutation to force state;
8. no second scheduler, queue, DB, controller, sidecar, or orchestration engine.

PRIMARY SIMPLIFICATION TARGETS:
1. Remove or radically shrink semantic Markdown evidence parsing. Controller should validate deterministic report artifact contract; REVIEW/PLAN should judge engineering quality.
2. Delete/merge overlapping route-loop mechanisms (self-route streak, alternating rotation, evidence continuation accounting, three-role no-progress parser) into one small deterministic route-cycle policy. Prefer routing to PLAN/closure over inventing new BLOCK classes.
3. Eliminate `_report_progress_facts`-style parsing of arbitrary command/path/URL prose if a simpler deterministic policy can replace it.
4. Reduce prompt-contract gates that require repeated live feature chains to prove byte ordering already covered by exact send-payload/receipt tests. Keep one bounded live canary where behavior, not bytes, needs validation.
5. Fix provenance handling for controller-created UNSENT tool-picker/plugin artifacts: exact controller-owned probe artifacts must have a safe undo/clear path; genuine human drafts remain fail-closed. The present `mcp-thinkbook` plugin-pill manual gate is a concrete motivating case.
6. Decouple System Health completion from downstream product feature completion. Health proves representative controller/runtime operation; Release F/E/D/MCP feature tasks remain separate work.
7. Remove duplicated helpers/branches/tests that exist only to support gates deleted above. Do not replace deleted complexity with a new abstraction layer.

WORK METHOD:
- Read the primary audit first, then independently inspect current source/diff/tests; the audit is evidence and analysis, not authority.
- Attribute dirty changes carefully because this checkout contains concurrent work. Preserve unrelated dirty/untracked work exactly.
- First PLAN should produce a deletion/simplification map with concrete files/functions and expected net LOC direction before DEV changes.
- DEV should use focused TDD for behavior that must remain, but avoid one-test-per-internal-branch if the branch itself should be deleted.
- REVIEW must specifically challenge whether each remaining gate is required by an irreversible safety boundary or merely compensates for prompt/model behavior.
- No feature expansion while doing this task.

ACCEPTANCE:
- production control-plane code for the targeted over-gating subsystem is materially smaller; report exact production LOC before/after for owned hunks;
- semantic evidence parser is removed or demonstrably much smaller and no longer parses arbitrary command/path/URL contents to infer engineering progress;
- route-cycle handling is one coherent policy rather than overlapping special cases;
- a clean DEV -> REVIEW -> PLAN flow can terminate without extra controller repair;
- a real REVIEW defect still routes to DEV;
- genuine human unsent draft remains protected;
- exact controller-created unsent connector/tool-picker probe can be safely cleared/replaced without human DOM surgery and without Send;
- exactly-once Send, endpoint ownership, Temporary identity, and report integrity regressions remain green;
- no new controller/DB/queue/scheduler/sidecar;
- no commit/push/merge/reset/stash/clean/branch switch;
- final report must explicitly list what was deleted, what remained, why each remaining gate is necessary, test/live evidence, and net production LOC delta.

APPROVAL HOLD:
This task is intentionally WAITING on temporary dependency `cdpa-idem-9a17c3402e7a2c99f92f0048` only to prevent execution before operator review. That dependency is an approval latch, not a semantic prerequisite. After the operator approves/resumes this cleanup task, remove this temporary dependency using the supported dependency API, then allow normal PLAN execution. Do not wait for the source task to reach DONE.
---END TASK---

PRIOR OBJECTIVE AUDIT (candidate evidence/analysis, not authority):
---BEGIN AUDIT---
# CDPA System Health Recovery — objective audit after ~8h runtime

Date: 2026-09-15

Repository: `E:\git-project\playwright-auto-multicdp`

Source task: `cdpa-idem-9a17c3402e7a2c99f92f0048` / `cdpa-system-health-recovery`

Current source-task state at audit time: `PAUSED`, active hop `65`, active role `DEV`.

## Executive assessment

The task produced substantial real fixes. It was not seven-plus hours of no-op work. The strongest improvements are around accepted-Send non-replay, durable endpoint ownership, Temporary Chat relocation, prompt compaction, report integrity, and several concrete controller recovery defects.

However, the task also became materially over-scoped and over-gated. A health-recovery task that began as a P0 operational repair expanded into a dependency orchestrator for endpoint repair, Temporary Chat, Agent Company Release F, Release E, later Release D/core-bridge work, MCP OAuth/scalable/catalog work, final canaries, and learning/final-report gates. That expansion made completion depend on feature work and on the controller validating increasingly detailed properties of free-form role reports.

The clearest architectural smell is the no-progress/evidence subsystem. Multiple review/fix iterations were spent making the controller parse human-authored Markdown evidence strongly enough to distinguish genuine progress from volatile `turn`/`hop_id` metadata without corrupting commands, URLs, paths, quoted values, semicolon-delimited commands, whitespace-delimited fields, or bare target names. This is expensive complexity created by the gate itself.

Recommended direction: preserve hard safety invariants at irreversible boundaries, but move subjective work-quality judgment back to REVIEW/PLAN. Replace overlapping semantic gates with a small number of deterministic controller rules. Split feature/dependency completion from “system health” completion.

## Source and attribution method

This checkout contains extensive concurrent dirty work from multiple projects/tasks. Current repository-wide diff is approximately:

- `74` tracked files changed
- `5,644` insertions
- `465` deletions
- additional untracked Agent Company plans, prompt/test files, and dashboard files

Those numbers MUST NOT be attributed wholesale to System Health Recovery.

This audit attributes changes only when supported by one or more of:

1. the 61 role reports under `.plan/cdpa-system-health-recovery/`;
2. child-task reports explicitly driven by the health task;
3. current source/tests inspected during REVIEW turns;
4. durable task/ledger/runtime evidence recorded in those reports.

Everything else in the dirty checkout is treated as concurrent/unattributed.

## Runtime and workflow volume

Source manifest:

- created: `2026-09-14T12:12:06.297164+00:00`
- last source-task update before this audit: `2026-09-14T20:22:58.996723+00:00`
- elapsed creation -> current pause: about `8h 10m 53s`
- hops: `65`
- route-timeline events: `70`
- role reports: `61`
  - PLAN: `7`
  - AUDIT: `2`
  - DEV: `28`
  - REVIEW: `24`

The number of reports/hops is itself evidence that the recovery policy did not converge efficiently.

## Original task intent

The original task had legitimate P0 goals:

- restore operational health before feature work;
- preserve dirty work and never mutate runtime DB/task manifests directly;
- never replay accepted or ambiguous Send;
- prevent report-only/self-route/review-ping-pong loops;
- make selected endpoint ownership durable across restart;
- preserve hard 9212 Temporary Chat placement;
- make PAUSE mean a true external/manual prerequisite;
- verify dashboard/runtime truth and endpoint capacity;
- run final canaries on 9202/9212/9222.

The original task also explicitly listed a long dependency chain of existing feature/infrastructure tasks. In practice this turned one recovery task into a supervisory mega-task.

## Attributed changes and outcomes

### 1. Durable endpoint ownership and relocation

Implemented/verified changes in the shared worker path so selected execution endpoint ownership is persisted consistently across role/hop/runtime state before browser mutation.

Observed fixes included:

- active hop receives the selected `browser_endpoint_id` consistently;
- pristine pre-Send relocation is distinguished from accepted/ambiguous Send state;
- relocation can rebuild prompt/context without replaying an accepted request;
- endpoint affinity/restart behavior was tightened;
- hard-required endpoint recovery was exercised live.

Outcome:

- required-endpoint child `cdpa-idem-5c9ac11362fba87f9dca71d2` reached terminal `DONE` on hard endpoint 9222;
- exactly-once request provenance was repeatedly checked.

Assessment: **high-value, justified complexity**, though code should still be simplified where possible.

### 2. Temporary Chat / 9212 recovery

The task recovered `cdpa-quick-temp-composer-9212-20260914` without blind replay.

Key changes/evidence:

- missing-ledger/pre-boundary relocation was handled;
- persisted endpoint moved from default 9202 to required 9212;
- Temporary Chat remained Temporary;
- relocation invalidated stale page-local document identity;
- a pristine Temporary request with no valid `temporary_document_id` was forced through a fresh Temporary page rather than adopting an unproven target page.

Outcome: child reached `DONE` and the reviewed ownership hole was fixed.

Assessment: **necessary safety work**. Temporary document identity is a real correctness boundary, not an over-gate.

### 3. Prompt compaction and human-first ordering

The task changed the normal workflow prompt contract so later turns do not repeatedly inject the full original task/constructor/base context.

Attributed behavior includes:

- compact role/mission + bounded handoff context;
- constructor/base context scoped to conversation generation;
- route schema emitted once;
- work-before-report directive moved late in the prompt;
- bounded recovery snapshot instead of unbounded recovery context;
- PLAN-specific closure instruction added late/final so clean REVIEW + satisfied acceptance can terminate instead of reflexively routing DEV.

Fresh focused prompt/transport tests later passed, including actual send-payload == persisted receipt/rendered prompt invariants.

Assessment: **net positive**. Prompt compaction directly addressed the original 13,861-character REVIEW prompt failure.

### 4. Report/evidence gate

A server-side substantive-evidence gate was introduced and then repeatedly refined.

The intended rule became roughly:

- normal role report needs an evidence section;
- non-PAUSE completion requires typed action/target plus typed outcome;
- PAUSE requires `block_code` plus `resume_condition`;
- placeholders such as `none`, `unknown`, `done`, `complete` do not qualify;
- report integrity failures use bounded report repair;
- weak substantive evidence causes continuation rather than immediate completion.

This gate generated many successive review failures:

- colon/dotted-token false positives;
- arbitrary inline-code false positives;
- bare transition-arrow false positives;
- arbitrary `key=value`/number false positives;
- placeholder-value false positives;
- blocker evidence accepted on non-PAUSE routes;
- later coupling to no-progress evidence normalization.

Assessment: **partly useful, materially over-engineered**. Verifying report existence/hash/expected path is deterministic and valuable. Parsing free-form Markdown deeply enough to decide whether engineering work is “substantive” should not be a core controller responsibility.

### 5. Self-route, alternating-route, and three-role no-progress guards

The controller accumulated several loop controls:

- consecutive same-role/self-route guard;
- inclusion of substantive-evidence continuation in the same self-route guard;
- DEV/REVIEW alternating-route context rotation;
- evidence-aware three-role `DEV -> REVIEW -> PLAN -> DEV` no-progress detection;
- `no_progress_route_cycle` block and special control eligibility;
- PLAN closure prompt to prevent clean PLAN from re-opening DEV work.

The three-role guard then required a custom `_report_progress_facts` normalizer to ignore volatile workflow metadata while preserving real changes.

That normalizer went through a long sequence of fixes for:

- `turn17` / `-hop17`;
- `turn=17` / `hop_id=2`;
- metadata appearing after other fields;
- command/path/URL target values that happen to contain those strings;
- quoted command values;
- semicolon/comma delimiters;
- whitespace-separated fields;
- bare filenames such as `artifact-turn17.json` and `request-hop17.json`.

Assessment: **primary over-engineering hotspot**. Multiple overlapping route guards plus semantic normalization of Markdown are symptoms of the controller trying to infer engineering progress from prose.

Recommended simplification:

- keep one bounded route-cycle policy;
- do not semantically parse report bodies to determine progress;
- after a small route-cycle budget, route to PLAN or require explicit REVIEW finding, rather than block on a derived prose signature;
- let REVIEW/PLAN judge whether evidence is materially new.

### 6. Report repair / restart-role recovery

The task made useful corrections around repair and restart:

- artifact-integrity/read failures use report repair rather than normal substantive continuation;
- report repair remains contract-only;
- restart from nested report-repair hops preserves nearest substantive handoff;
- accepted Send is not replayed;
- exactly-once provenance survives role/context replacement.

Assessment: **mostly justified**. The number of separate repair paths should still be audited for consolidation, but the underlying durability invariants are sound.

### 7. Release F implementation and live closure

The health task went beyond controller health and directly drove Agent Company Release F in a child worktree.

Attributed Release F work included:

- `company_agents` projection;
- task summary/detail differences;
- identity/role/manager/inbox/generation/assignment/status/preferred/requested/actual endpoint/last activity visibility;
- board/detail/API projection behavior;
- fixes for summary bloat and incorrect activity fallback.

Release F eventually reached `DONE` at hop 70 with latest relevant requests at `attempts=1`.

Assessment:

- product work itself appears real and reviewed;
- **but including Release F terminalization as a hard completion gate for “System Health Recovery” expanded scope substantially**.

### 8. PLAN closure behavior

Live recovery showed a clean REVIEW still did not cause PLAN to terminate. A `PLAN_CLOSURE` instruction was added and strengthened so PLAN:

- accepts handoff and accepted role reports as closure evidence;
- routes `DONE` when acceptance is satisfied and no concrete defect remains;
- does not route DEV merely because PLAN itself did not rerun tests;
- must name a concrete unmet criterion if not choosing DONE.

Current source/tests prove the final ordering. Release F hop70 reached DONE using an earlier cue ordering; therefore later REVIEW correctly noted that hop70 is not a live proof of the literal final-source ordering.

Assessment: **reasonable prompt fix**, but requiring yet another live terminal canary solely for exact prompt-order wording is borderline over-gating if transport tests already prove the byte-level payload.

### 9. Release E / Operations & Quality recovery

PLAN advanced Release E after Release F.

The child recovered from prior report-validation exhaustion and produced a real DEV report. It added:

- `E:\git-project\playwright-auto-multicdp-ops-quality-20260914\src\playwright_auto\cdpa_supervisor_policies.py`

Current parent DEV report explicitly does **not** accept Release E as complete: integration references and focused coverage remained unproven.

The latest blocker was created while probing the REVIEW page's connector availability:

- tool picker showed installed `mcp-thinkbook`;
- selecting it created an unsent atomic plugin pill in the composer;
- normal editor cleanup did not remove the pill;
- supported Restart Role then failed closed with `ComposerConflictError` because the draft no longer matched persisted automated prompt provenance;
- child remained BLOCKED at REVIEW hop17;
- parent health task routed PAUSE at hop65.

Assessment: **clear over-gating/provenance UX defect**. Fail-closed behavior is correct for an accepted/ambiguous Send, but a controller-created, unsent connector-selection artifact should have explicit controller-owned provenance and a safe cleanup path. Requiring manual human deletion of that pill is not a healthy terminal design.

## What objectively worked

Keep these invariants unless a replacement is demonstrably simpler and equally safe:

1. Accepted/ambiguous Send is never replayed.
2. Durable request ledger is authoritative for Send provenance.
3. Exact endpoint ownership is persisted before browser mutation.
4. Temporary Chat document/page identity is treated as page-local and non-transferable.
5. Report file identity/path/hash/size is validated before using it as a durable handoff.
6. Direct task-manifest/SQLite edits are not used to force state.
7. Destructive browser/context replacement is bounded by ownership/provenance.
8. Prompt constructor/base context is generation-scoped rather than repeated every turn.

These are safety/correctness controls, not merely process ceremony.

## What should be challenged or removed

### A. Semantic Markdown evidence parser

Candidate: `_validate_substantive_report_evidence` + `_report_progress_facts` complexity.

Problem: the controller is acting as a weak programming-language parser for human evidence prose. This caused a large fraction of the 24 REVIEW turns.

Simpler target:

- require expected non-empty report + `## Evidence` section;
- preserve report integrity/path/hash checks;
- REVIEW/PLAN judges evidence quality;
- controller does not interpret arbitrary commands/URLs/path tokens to infer progress.

### B. Multiple overlapping loop guards

Current concepts overlap:

- self-route streak;
- alternating pair rotation;
- evidence continuation streak;
- three-role no-progress cycle;
- PLAN closure prompt.

Simpler target:

- one route-cycle budget with one recovery action;
- after bounded churn, force PLAN with preserved handoff rather than inventing additional block classes;
- only hard BLOCK for an actual unsafe/irreconcilable transport or ownership condition.

### C. Prompt byte-order as repeated live gate

Prompt send-payload/receipt equality deserves automated integration coverage. Requiring repeated real feature chains to prove exact last-line prompt ordering is expensive unless a previous live result demonstrated a model-behavior regression.

Use automated transport test as primary proof; one bounded live canary is enough after behavior changes.

### D. System-health task owning feature completion

Release F and Release E were pulled into the health task because the original plan listed them as recovery dependencies.

This conflates:

- “controller/runtime is healthy enough to execute work”
with
- “all previously blocked product features are now complete”.

Recommended boundary:

System Health is DONE when the controller can safely execute/resume/route representative workflows on 9202/9212/9222 and the known controller-induced blockers are eliminated. Product tasks remain separate downstream tasks.

### E. Controller-created unsent draft treated as foreign/manual draft

Current blocker demonstrates missing provenance granularity.

Desired rule:

- accepted/ambiguous user-message boundary => immutable/no replay;
- true human-authored unsent draft => preserve/fail closed;
- exact controller-created unsent probe/draft artifact => controller may safely undo/clear it using its own provenance.

This should remove the present manual plugin-pill gate without weakening Send safety.

## Objective risk assessment

| Area | Value | Complexity cost | Recommendation |
| --- | --- | --- | --- |
| exactly-once Send ledger | very high | medium | KEEP |
| endpoint ownership durability | very high | medium | KEEP |
| Temporary document identity | high | medium | KEEP |
| compact/generation-scoped prompts | high | low/medium | KEEP |
| report path/hash integrity | high | low | KEEP |
| typed semantic evidence gate | medium | high | SHRINK heavily |
| `_report_progress_facts` parser | low/medium | very high | DELETE/replace |
| multiple route-loop guards | medium | high | MERGE into one policy |
| repeated live prompt-order canaries | low/medium | high operational cost | REDUCE |
| health task as feature dependency orchestrator | low for health objective | very high | SPLIT |
| fail-closed foreign manual draft | high | medium | KEEP |
| fail-closed controller-created unsent connector pill | negative | high friction | FIX provenance/cleanup |

## Recommended simplification target

The cleanup task should aim for the following minimal controller model:

1. **Transport safety gate**
   - pre-Send: mutable/recoverable;
   - accepted/ambiguous Send: exact execution immutable, reconcile only;
   - completed response: durable handoff.

2. **Ownership gate**
   - one durable endpoint/page owner;
   - Temporary document identity enforced;
   - no second scheduler/DB/controller.

3. **Minimal report contract**
   - exact expected report exists and is non-empty;
   - report hash/size/path stable;
   - has an Evidence section;
   - no semantic parser for arbitrary engineering prose.

4. **One loop policy**
   - bounded route churn counter across role transitions;
   - on threshold, preserve evidence and route to PLAN/review closure;
   - do not maintain separate semantic self/alternation/three-role parsers unless a concrete case proves one is necessary.

5. **PAUSE policy**
   - external/manual prerequisite only;
   - controller-owned unsent artifacts must have safe undo provenance;
   - system-checkable/tooling problems remain recoverable/BLOCKED, not PAUSED.

6. **Scope boundary**
   - health acceptance proves representative execution and truthful runtime state;
   - downstream product releases are not health DONE gates.

## Suggested acceptance for the cleanup task

- Reduce controller gate/normalizer code rather than adding another guard.
- Remove or radically shrink semantic report parsing.
- Consolidate route-cycle handling into one deterministic policy.
- Add explicit safe cleanup provenance for controller-created unsent connector/tool-picker artifacts.
- Preserve exactly-once Send tests and endpoint/Temporary ownership tests unchanged or stronger.
- Demonstrate that a clean `DEV -> REVIEW -> PLAN` flow can terminate without extra controller intervention.
- Demonstrate a real implementation defect still routes back to DEV.
- Demonstrate a genuine human draft is preserved.
- Demonstrate a controller-created unsent probe can be safely cleared/replaced.
- No new DB, scheduler, queue, controller, sidecar, or generic abstraction layer.
- Prefer net negative production LOC for the over-gating subsystem.

## Current exact blocker at source-task pause

Parent:

- task: `cdpa-idem-9a17c3402e7a2c99f92f0048`
- state: `PAUSED`
- hop: `65`
- role: `DEV`

Release E child:

- task: `cdpa-idem-1a4e2f622390f515a73898e2`
- state: `BLOCKED`
- active REVIEW hop: `17`
- block: `report_validation_exhausted`
- accepted requests remain one-attempt each in the inspected range.

Parent DEV28 declared:

- `block_code=review_connector_probe_draft_requires_manual_clear`
- resume condition requires manually removing the unsent `mcp-thinkbook` plugin pill from the exact 9222-plus REVIEW conversation, then Restart Role REVIEW.

This audit considers that manual gate a design smell suitable for the cleanup task.

## Important evidence paths

Primary source reports:

- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-audit_turn2_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn14_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn15_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn16_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn21_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn18_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn19_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn20_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn21_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn22_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn26_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn27_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-review_turn24_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-plan_turn7_cdpa-idem-9a17c3402e7a2c99f92f0048.md`
- `.plan/cdpa-system-health-recovery/cdpa-system-health-recovery-dev_turn28_cdpa-idem-9a17c3402e7a2c99f92f0048.md`

## Bottom line

The recovery task made real correctness gains, particularly around Send durability, endpoint ownership, Temporary Chat, and compact prompts. The weak point is not “too much safety” in irreversible operations; it is **putting subjective progress/evidence semantics and feature-completion policy into the controller**.

The next cleanup should delete policy and parser complexity, not add another recovery layer. The target is a smaller controller with strong irreversible-boundary safety and weaker opinions about how engineers write evidence.
---END AUDIT---

SELECTED CURRENT SOURCE EXCERPTS. These are included so you can evaluate the main controversial gates without repository reads.

[cdpa_worker.py constants / error classes]
  150: from .connection import is_cdp_disconnect, is_local_cdp_endpoint, normalize_cdp_endpoint
  151: from .durable import (
  152:     DurableRecoveryState,
  153:     DurableRequestError,
  154:     RequestLedger,
  155:     RequestStatus,
  156:     build_idempotency_key,
  157:     classify_recovery_state,
  158: )
  159: from .durable_blocks import DurableSendBlock
  160: from .upload import UploadIdentityChangedError, UploadReceipt, collect_file_identities
  161: from .workflow import WorkflowContext
  162: from .workspace import ChatGPTWorkspace
  163: 
  164: TERMINAL = frozenset({"DONE", "STOPPED"})
  165: IN_FLIGHT = frozenset({"sending", "sent", "waiting"})
  166: _STREAM_STATUS_RECOVERY_SECONDS = 30.0
  167: _CONSECUTIVE_SELF_ROUTE_LIMIT = 3
  168: _CONSECUTIVE_SELF_ROUTE_BLOCK_CODE = "consecutive_self_route_limit"
  169: _RECOVERY_SNAPSHOT_MAX_CHARS = 12_000
  170: _RECOVERY_GOAL_MAX_CHARS = 1_000
  171: _ALTERNATING_ROUTE_ROTATION_LIMIT = 6
  172: _ALTERNATING_ROUTE_ROTATION_KIND = "alternating_route_context_rotation"
  173: _NO_PROGRESS_ROUTE_CYCLE_BLOCK_CODE = "no_progress_route_cycle"
  174: _STATUS_RECOVERY_GRAPH_SECONDS = 300.0
  175: _TERMINAL_GRAPH_RETRY_SECONDS = 120.0
  176: _TERMINAL_GRAPH_MAX_ATTEMPTS = 3
  177: _DOM_FALLBACK_SETTLE_SECONDS = 30.0
  178: _POST_REFRESH_RESPONSE_PROBE_SECONDS = 60.0
  179: _POST_REFRESH_FINAL_RECONCILE_SECONDS = 120.0
  180: _STREAM_STATUS_POLL_MIN_SECONDS = 30.0
  181: _STREAM_STATUS_POLL_MAX_SECONDS = 35.0
  182: _DOM_WAIT_PROBE_MS = 5_000
  183: _MCP_ALLOW_STABLE_SECONDS = 5.0
  184: _AMBIENT_DOM_FALLBACK_SECONDS = 5.0
  185: _MCP_ALLOW_POST_CLICK_SECONDS = 5.0
  186: _CONTROLLER_STALL_REFRESH_SECONDS = 600.0
  187: _AUTOMATED_SEND_SPACING_SECONDS = 10.0
  188: _SENDING_CONTINUATION_STARTED = "bounded continuation started after proven atomic non-acceptance"
  189: _PROVEN_ATOMIC_NONACCEPTANCE_ERRORS = (
  190:     "ComposerConflictError: composer changed or became unavailable inside the atomic send boundary",
  191:     "ComposerConflictError: attachment identity changed inside the atomic send boundary",
  192:     "PageOwnershipError: page ownership changed inside the atomic send boundary:",
  193:     "UnsafePageStateError: page state changed inside the atomic send boundary:",
  194: )
  195: _RATE_LIMIT_BLOCK_MESSAGE = (
  196:     "Too many requests; shared browser-profile cooldown is active"
  197: )
  198: 
  199: 
  200: class IneffectiveControlError(RuntimeError):
  201:     """The primitive ran, but its required operational postcondition did not hold."""
  202: 
  203: 
  204: class _SubstantiveEvidenceError(RouteContractError):
  205:     """The report artifact is valid, but its evidence content is insufficient."""

[cdpa_worker.py route/evidence progress helpers]
 5000:             handoff=next_handoff,
 5001:             kind=kind,
 5002:         )
 5003:         state.setdefault("route_timeline", []).append(
 5004:             {
 5005:                 "at": utc_now(),
 5006:                 "hop_id": old_hop_id,
 5007:                 "source_role": source_role,
 5008:                 "route": "PLAN",
 5009:                 "kind": kind,
 5010:                 "new_hop_id": new_hop["hop_id"],
 5011:                 "reason": reason,
 5012:             }
 5013:         )
 5014:         return {"old_hop_id": old_hop_id, "new_hop_id": int(new_hop["hop_id"])}
 5015: 
 5016:     def _continue_substantive_work(
 5017:         self,
 5018:         state: dict[str, Any],
 5019:         hop: dict[str, Any],
 5020:         error: Exception,
 5021:         *,
 5022:         decision: RouteDecision,
 5023:         report_path: str,
 5024:         report_sha256: str,
 5025:         report_size: int,
 5026:     ) -> None:
 5027:         self._complete_request_response(hop)
 5028:         validation_error = sanitize_text(error, max_chars=2000)
 5029:         role = str(hop["target_role"])
 5030:         hop["validation_error"] = validation_error
 5031:         hop["validation_route"] = decision.route
 5032:         hop["validation_handoff"] = decision.handoff
 5033:         hop["report_path"] = report_path
 5034:         hop["report_sha256"] = report_sha256
 5035:         hop["report_size"] = report_size
 5036:         hop["state"] = "routed"
 5037:         hop["route"] = role
 5038:         hop["timestamps"]["routed_at"] = utc_now()
 5039:         state.setdefault("route_timeline", []).append(
 5040:             {
 5041:                 "at": utc_now(),
 5042:                 "hop_id": hop["hop_id"],
 5043:                 "source_role": role,
 5044:                 "route": role,
 5045:                 "kind": "substantive_evidence_continuation",
 5046:                 "proposed_route": decision.route,
 5047:                 "error": validation_error,
 5048:             }
 5049:         )
 5050:         streak_role, streak = self._consecutive_self_route_streak(state)
 5051:         if streak_role == role.strip().upper() and streak >= _CONSECUTIVE_SELF_ROUTE_LIMIT:
 5052:             self._block(
 5053:                 state,
 5054:                 (
 5055:                     f"Consecutive self-route limit reached for role {streak_role}: "
 5056:                     f"streak {streak}; explicit operator Resume is required to continue."
 5057:                 ),
 5058:                 code=_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE,
 5059:                 retryable=False,
 5060:             )
 5061:             return
 5062:         continuation = self._append_hop(
 5063:             state,
 5064:             source_role=role,
 5065:             target_role=role,
 5066:             handoff=decision.handoff,
 5067:             kind="handoff",
 5068:             turn=int(hop["turn"]) + 1,
 5069:             validation_error=validation_error,
 5070:         )
 5071:         continuation["substantive_evidence_required"] = True
 5072: 
 5073:     def _repair_report(
 5074:         self,
 5075:         state: dict[str, Any],
 5076:         hop: dict[str, Any],
 5077:         error: Exception,
 5078:         *,
 5079:         decision: RouteDecision,
 5080:     ) -> None:
 5081:         self._complete_request_response(hop)
 5082:         validation_error = sanitize_text(error, max_chars=2000)
 5083:         hop["validation_error"] = validation_error
 5084:         hop["validation_route"] = decision.route
 5085:         hop["validation_handoff"] = decision.handoff
 5086:         attempt = int(hop.get("repair_attempt") or 0) + 1
 5087:         if attempt > self.config.route_repair_attempts:
 5088:             self._block(
 5089:                 state,
 5090:                 f"report repair exhausted: {error}",
 5091:                 code="report_validation_exhausted",
 5092:                 retryable=True,
 5093:             )
 5094:             return
 5095:         role = str(hop["target_role"])
 5096:         hop["state"] = "routed"
 5097:         hop["route"] = decision.route
 5098:         hop["timestamps"]["routed_at"] = utc_now()
 5099:         state.setdefault("route_timeline", []).append(
 5100:             {
 5101:                 "at": utc_now(),
 5102:                 "hop_id": hop["hop_id"],
 5103:                 "source_role": role,
 5104:                 "route": decision.route,
 5105:                 "kind": "report_repair",
 5106:                 "error": validation_error,
 5107:             }
 5108:         )
 5109:         repair = self._append_hop(
 5110:             state,
 5111:             source_role=role,
 5112:             target_role=role,
 5113:             handoff="report repair",
 5114:             kind="report_repair",
 5115:             turn=int(hop["turn"]),
 5116:             repair_attempt=attempt,
 5117:             validation_error=validation_error,
 5118:         )
 5119:         repair["locked_route"] = decision.route
 5120:         repair["locked_handoff"] = decision.handoff
 5121:         repair["substantive_evidence_required"] = bool(
 5122:             hop.get("substantive_evidence_required")
 5123:         )
 5124: 
 5125:     def _repair_route(
 5126:         self,
 5127:         state: dict[str, Any],
 5128:         hop: dict[str, Any],
 5129:         error: Exception,
 5130:         *,
 5131:         decision: Any = None,
 5132:     ) -> None:
 5133:         self._complete_request_response(hop)
 5134:         validation_error = sanitize_text(error, max_chars=2000)
 5135:         hop["validation_error"] = validation_error
 5136:         if decision is not None:
 5137:             hop["validation_route"] = decision.route
 5138:             hop["validation_handoff"] = decision.handoff
 5139:         attempt = int(hop.get("repair_attempt") or 0) + 1
 5140:         if attempt > self.config.route_repair_attempts:
 5141:             self._block(
 5142:                 state,
 5143:                 f"route repair exhausted: {error}",
 5144:                 code="route_validation_exhausted",
 5145:                 retryable=True,
 5146:             )
 5147:             return
 5148:         role = str(hop["target_role"])
 5149:         hop["state"] = "routed"
 5150:         hop["route"] = role
 5151:         hop["timestamps"]["routed_at"] = utc_now()
 5152:         state.setdefault("route_timeline", []).append(
 5153:             {
 5154:                 "at": utc_now(),
 5155:                 "hop_id": hop["hop_id"],
 5156:                 "source_role": role,
 5157:                 "route": role,
 5158:                 "kind": "route_repair",
 5159:                 "error": validation_error,
 5160:             }
 5161:         )
 5162:         repair = self._append_hop(
 5163:             state,
 5164:             source_role=role,
 5165:             target_role=role,
 5166:             handoff="route repair",
 5167:             kind="route_repair",
 5168:             turn=int(hop["turn"]),
 5169:             repair_attempt=attempt,
 5170:             validation_error=validation_error,
 5171:         )
 5172:         repair["substantive_evidence_required"] = bool(
 5173:             hop.get("substantive_evidence_required")
 5174:         )
 5175: 
 5176:     @staticmethod
 5177:     def _self_route_reset_hop_id(state: Mapping[str, Any]) -> int:
 5178:         boundary = 1
 5179:         for revision in state.get("goal_revisions") or ():
 5180:             if not isinstance(revision, Mapping):
 5181:                 continue
 5182:             applies_from = revision.get("applies_from_hop_id")
 5183:             if isinstance(applies_from, int) and not isinstance(applies_from, bool):
 5184:                 boundary = max(boundary, applies_from)
 5185:         for control in state.get("controls") or ():
 5186:             if not isinstance(control, Mapping) or control.get("action") != "resume":
 5187:                 continue
 5188:             origin = str(control.get("origin") or "operator").strip().lower()
 5189:             if origin != "operator":
 5190:                 continue
 5191:             result = control.get("result")
 5192:             if not isinstance(result, Mapping):
 5193:                 continue
 5194:             before = result.get("before")
 5195:             after = result.get("after")
 5196:             outcome = str(result.get("outcome") or "").strip().lower()
 5197:             status = str(control.get("status") or "").strip().lower()
 5198:             if (
 5199:                 isinstance(before, Mapping)
 5200:                 and str(before.get("status") or "").upper() == "PAUSED"
 5201:                 and status == "applied"
 5202:                 and outcome in {"continued", "applied"}
 5203:             ):
 5204:                 hop_id = before.get("active_hop_id")
 5205:                 if isinstance(hop_id, int) and not isinstance(hop_id, bool):
 5206:                     boundary = max(boundary, hop_id)
 5207:             if (
 5208:                 isinstance(before, Mapping)
 5209:                 and str(before.get("block_code") or "")
 5210:                 == _CONSECUTIVE_SELF_ROUTE_BLOCK_CODE
 5211:                 and status == "applied"
 5212:                 and outcome == "continued"
 5213:                 and isinstance(after, Mapping)
 5214:             ):
 5215:                 hop_id = after.get("active_hop_id")
 5216:                 if isinstance(hop_id, int) and not isinstance(hop_id, bool):
 5217:                     boundary = max(boundary, hop_id)
 5218:         return boundary
 5219: 
 5220:     @staticmethod
 5221:     def _alternating_route_streak(
 5222:         state: Mapping[str, Any]
 5223:     ) -> tuple[tuple[str, str] | None, int]:
 5224:         pair: tuple[str, str] | None = None
 5225:         previous: tuple[str, str] | None = None
 5226:         streak = 0
 5227:         for event in state.get("route_timeline") or ():
 5228:             if not isinstance(event, Mapping):
 5229:                 continue
 5230:             kind = str(event.get("kind") or "").strip()
 5231:             if kind == _ALTERNATING_ROUTE_ROTATION_KIND:
 5232:                 pair = None
 5233:                 previous = None
 5234:                 streak = 0
 5235:                 continue
 5236:             if kind:
 5237:                 continue
 5238:             source = str(event.get("source_role") or "").strip().upper()
 5239:             route = str(event.get("route") or "").strip().upper()
 5240:             if not source or not route or source == route or route in {"DONE", "PAUSE"}:
 5241:                 pair = None
 5242:                 previous = None
 5243:                 streak = 0
 5244:                 continue
 5245:             current_pair = tuple(sorted((source, route)))
 5246:             if (
 5247:                 pair == current_pair
 5248:                 and previous is not None
 5249:                 and previous == (route, source)
 5250:             ):
 5251:                 streak += 1
 5252:             else:
 5253:                 pair = current_pair
 5254:                 streak = 1
 5255:             previous = (source, route)
 5256:         return pair, streak
 5257: 
 5258:     def _report_progress_facts(
 5259:         self,
 5260:         state: Mapping[str, Any],
 5261:         hop: Mapping[str, Any],
 5262:     ) -> frozenset[str]:
 5263:         report_path = str(hop.get("report_path") or "").strip()
 5264:         expected_sha = str(hop.get("report_sha256") or "").strip().lower()
 5265:         expected_size = hop.get("report_size")
 5266:         if (
 5267:             not report_path
 5268:             or re.fullmatch(r"[a-f0-9]{64}", expected_sha) is None
 5269:             or isinstance(expected_size, bool)
 5270:             or not isinstance(expected_size, int)
 5271:             or expected_size < 0
 5272:         ):
 5273:             return frozenset()
 5274:         report_repository, _report_plans_root = _workflow_report_roots(self.config, state)
 5275:         path = Path(report_path)
 5276:         if not path.is_absolute():
 5277:             path = report_repository / path
 5278:         try:
 5279:             data = path.read_bytes()
 5280:         except OSError:
 5281:             return frozenset()
 5282:         if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha:
 5283:             return frozenset()
 5284:         text = data.decode("utf-8-sig", errors="replace")
 5285:         match = re.search(r"(?im)^##\s+(?:Evidence|Kiểm chứng)\s*$", text)
 5286:         if match is None:
 5287:             return frozenset()
 5288:         section = text[match.end():]
 5289:         next_heading = re.search(r"(?m)^##\s+", section)
 5290:         if next_heading is not None:
 5291:             section = section[:next_heading.start()]
 5292:         normalized = section.casefold()
 5293:         for root in (str(report_repository), str(state.get("repository") or "")):
 5294:             if not root:
 5295:                 continue
 5296:             normalized = normalized.replace(root.casefold(), "<repo>")
 5297:             normalized = normalized.replace(root.replace("\\", "/").casefold(), "<repo>")
 5298:         normalized = re.sub(
 5299:             r"(?i)[^\s`\"']*\.plan(?:[\\/]+)[^\s`\"']+?\.md",
 5300:             "<report>",
 5301:             normalized,
 5302:         )
 5303:         task_id = str(state.get("task_id") or "").strip().casefold()
 5304:         if task_id:
 5305:             normalized = normalized.replace(task_id, "<task>")
 5306:         def normalize_identity_metadata(line: str) -> str:
 5307:             normalized_line: list[str] = []
 5308:             index = 0
 5309:             quote: str | None = None
 5310:             escaped = False
 5311:             while index < len(line):
 5312:                 char = line[index]
 5313:                 if quote is not None:
 5314:                     normalized_line.append(char)
 5315:                     if escaped:
 5316:                         escaped = False
 5317:                     elif char == "\\":
 5318:                         escaped = True
 5319:                     elif char == quote:
 5320:                         quote = None
 5321:                     index += 1
 5322:                     continue
 5323:                 if char in {"'", '"', "`"}:
 5324:                     quote = char
 5325:                     normalized_line.append(char)
 5326:                     index += 1
 5327:                     continue
 5328:                 boundary = index == 0 or line[index - 1].isspace() or line[index - 1] in ";,"
 5329:                 if boundary:
 5330:                     match = re.match(
 5331:                         r"(turn|(?:active_)?hop_id)(\s*=\s*)\d+(?=$|[\s;,])",
 5332:                         line[index:],
 5333:                         re.IGNORECASE,
 5334:                     )
 5335:                     if match is not None:
 5336:                         normalized_line.append(
 5337:                             f"{match.group(1).casefold()}{match.group(2)}<n>"
 5338:                         )
 5339:                         index += match.end()
 5340:                         continue
 5341:                 normalized_line.append(char)
 5342:                 index += 1
 5343:             return "".join(normalized_line)
 5344: 
 5345:         normalized = "\n".join(
 5346:             normalize_identity_metadata(line) for line in normalized.splitlines()
 5347:         )
 5348:         normalized = re.sub(
 5349:             r"\b20\d{2}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?\b",
 5350:             "<timestamp>",
 5351:             normalized,
 5352:         )
 5353:         return frozenset(
 5354:             re.sub(r"\s+", " ", line).strip()
 5355:             for line in normalized.splitlines()
 5356:             if re.sub(r"\s+", " ", line).strip()
 5357:         )
 5358: 
 5359:     def _no_progress_review_plan_cycle(
 5360:         self, state: Mapping[str, Any]
 5361:     ) -> tuple[str, str] | None:
 5362:         boundary = self._self_route_reset_hop_id(state)
 5363:         hops = {
 5364:             hop.get("hop_id"): hop
 5365:             for hop in state.get("hops") or ()
 5366:             if isinstance(hop, Mapping)
 5367:         }
 5368:         normal_events: list[tuple[Mapping[str, Any], str, str]] = []
 5369:         for event in state.get("route_timeline") or ():
 5370:             if not isinstance(event, Mapping):
 5371:                 continue
 5372:             kind = str(event.get("kind") or "").strip()
 5373:             if kind:
 5374:                 if kind in {
 5375:                     "role_restart",
 5376:                     "accepted_send_reconciliation",
 5377:                     _ALTERNATING_ROUTE_ROTATION_KIND,
 5378:                 }:
 5379:                     normal_events.clear()
 5380:                 continue
 5381:             hop_id = event.get("hop_id")
 5382:             if (
 5383:                 isinstance(hop_id, bool)
 5384:                 or not isinstance(hop_id, int)
 5385:                 or hop_id < boundary
 5386:             ):
 5387:                 continue
 5388:             source = str(event.get("source_role") or "").strip().upper()
 5389:             route = str(event.get("route") or "").strip().upper()
 5390:             if not source or not route or source == route or route in {"DONE", "PAUSE"}:
 5391:                 normal_events.clear()
 5392:                 continue
 5393:             normal_events.append((event, source, route))
 5394:         if len(normal_events) < 6:
 5395:             return None
 5396:         first, second, third, fourth, fifth, sixth = normal_events[-6:]
 5397:         work_role = first[1]
 5398:         if work_role in {"PLAN", "REVIEW"}:
 5399:             return None
 5400:         if not (
 5401:             first[2] == "REVIEW"
 5402:             and second[1:] == ("REVIEW", "PLAN")
 5403:             and third[1:] == ("PLAN", work_role)
 5404:             and fourth[1:] == (work_role, "REVIEW")
 5405:             and fifth[1:] == ("REVIEW", "PLAN")
 5406:             and sixth[1:] == ("PLAN", work_role)
 5407:         ):
 5408:             return None
 5409:         first_hop = hops.get(first[0].get("hop_id"))
 5410:         fourth_hop = hops.get(fourth[0].get("hop_id"))
 5411:         if not isinstance(first_hop, Mapping) or not isinstance(fourth_hop, Mapping):
 5412:             return None
 5413:         first_facts = self._report_progress_facts(state, first_hop)
 5414:         fourth_facts = self._report_progress_facts(state, fourth_hop)
 5415:         if first_facts and fourth_facts and fourth_facts.issubset(first_facts):
 5416:             fourth_signature = hashlib.sha256(
 5417:                 "\n".join(sorted(fourth_facts)).encode("utf-8")
 5418:             ).hexdigest()
 5419:             return work_role, fourth_signature
 5420:         return None
 5421: 
 5422:     @classmethod
 5423:     def _consecutive_self_route_streak(
 5424:         cls, state: Mapping[str, Any]
 5425:     ) -> tuple[str | None, int]:
 5426:         boundary = cls._self_route_reset_hop_id(state)
 5427:         hops = {
 5428:             hop.get("hop_id"): hop
 5429:             for hop in state.get("hops") or ()
 5430:             if isinstance(hop, Mapping)
 5431:         }
 5432:         streak_role: str | None = None
 5433:         streak = 0
 5434:         for event in state.get("route_timeline") or ():
 5435:             if not isinstance(event, Mapping):
 5436:                 continue
 5437:             event_kind = str(event.get("kind") or "").strip()
 5438:             if event_kind not in {"", "substantive_evidence_continuation"}:
 5439:                 continue
 5440:             hop_id = event.get("hop_id")
 5441:             if (
 5442:                 isinstance(hop_id, bool)
 5443:                 or not isinstance(hop_id, int)
 5444:                 or hop_id < boundary
 5445:             ):
 5446:                 continue
 5447:             source_hop = hops.get(hop_id)
 5448:             if not isinstance(source_hop, Mapping) or str(source_hop.get("kind") or "") not in {
 5449:                 "task",
 5450:                 "handoff",
 5451:                 "report_repair",
 5452:             }:
 5453:                 continue
 5454:             source_role = str(event.get("source_role") or "").strip().upper()
 5455:             route = str(event.get("route") or "").strip().upper()
 5456:             if not source_role or not route:
 5457:                 continue
 5458:             if route == "DONE":
 5459:                 return None, 0
 5460:             if source_role != route:
 5461:                 streak_role = None
 5462:                 streak = 0
 5463:                 continue
 5464:             if streak_role == source_role:
 5465:                 streak += 1
 5466:             else:
 5467:                 streak_role = source_role
 5468:                 streak = 1
 5469:         return streak_role, streak
 5470: 
 5471:     @staticmethod
 5472:     def _validate_substantive_report_evidence(
 5473:         report_path: str,
 5474:         *,
 5475:         repository_root: Path,
 5476:         expected_sha256: str,
 5477:         expected_size: int,
 5478:         route: str,
 5479:     ) -> None:
 5480:         path = Path(report_path)
 5481:         if not path.is_absolute():
 5482:             path = repository_root / path
 5483:         data = path.read_bytes()
 5484:         if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
 5485:             raise RouteContractError("report changed before substantive evidence validation")
 5486:         text = data.decode("utf-8", errors="replace")
 5487:         match = re.search(r"(?im)^##\s+(?:Evidence|Kiểm chứng)\s*$", text)
 5488:         if match is None:
 5489:             raise _SubstantiveEvidenceError("normal role report lacks a substantive evidence section")
 5490:         section = text[match.end():]
 5491:         next_heading = re.search(r"(?m)^##\s+", section)
 5492:         if next_heading is not None:
 5493:             section = section[:next_heading.start()]
 5494:         concrete = section.strip()
 5495:         if len(concrete) < 40:
 5496:             raise _SubstantiveEvidenceError("normal role report lacks concrete substantive evidence")
 5497:         evidence_text = concrete
 5498:         placeholders = {"none", "null", "unknown", "n/a", "na", "ok", "done", "complete", "completed"}
 5499:         key_values = {
 5500:             match.group(1).lower(): match.group(2).strip("`'\".,;")
 5501:             for match in re.finditer(
 5502:                 r"(?=\b([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*([^\s`;,]+))",
 5503:                 evidence_text,
 5504:             )
 5505:         }
 5506: 
 5507:         blocker_code = key_values.get("block_code", "").lower()
 5508:         resume_condition = key_values.get("resume_condition", "").lower()
 5509:         blocker_evidence_ok = (
 5510:             blocker_code

[cdpa_worker.py route handling thresholds]
 5790:                 "hop_id": hop["hop_id"],
 5791:                 "source_role": role,
 5792:                 "route": decision.route,
 5793:                 "report_path": report_path,
 5794:             }
 5795:         )
 5796:         state["roles"][role]["status"] = "idle"
 5797:         state["block_code"] = None
 5798:         state["block_retryable"] = False
 5799:         state["block_reason"] = None
 5800:         state["pause_reason"] = None
 5801:         if decision.route == "DONE":
 5802:             state["status"] = "DONE"
 5803:             state["terminal_state"] = "DONE"
 5804:             state["kanban_column"] = "DONE_STOPPED"
 5805:             state["completed_at"] = utc_now()
 5806:             state["last_role_activity_at"] = state["completed_at"]
 5807:             state["active_role"] = None
 5808:             state["active_hop_id"] = None
 5809:             state["active_action"] = "done"
 5810:             return
 5811:         streak_role, streak = self._consecutive_self_route_streak(state)
 5812:         if (
 5813:             str(decision.route).strip().upper() == role.strip().upper()
 5814:             and streak_role == role.strip().upper()
 5815:             and streak >= _CONSECUTIVE_SELF_ROUTE_LIMIT
 5816:         ):
 5817:             self._block(
 5818:                 state,
 5819:                 (
 5820:                     f"Consecutive self-route limit reached for role {streak_role}: "
 5821:                     f"streak {streak}; explicit operator Resume is required to continue."
 5822:                 ),
 5823:                 code=_CONSECUTIVE_SELF_ROUTE_BLOCK_CODE,
 5824:                 retryable=False,
 5825:             )
 5826:             return
 5827:         alternating_pair, alternating_streak = self._alternating_route_streak(state)
 5828:         if (
 5829:             decision.route != role
 5830:             and alternating_pair is not None
 5831:             and role in alternating_pair
 5832:             and decision.route in alternating_pair
 5833:             and alternating_streak >= _ALTERNATING_ROUTE_ROTATION_LIMIT
 5834:         ):
 5835:             target_record = state["roles"][decision.route]
 5836:             target_record["reset_requested"] = True
 5837:             target_record["constructor_sent_generation"] = None
 5838:             state.setdefault("route_timeline", []).append(
 5839:                 {
 5840:                     "at": utc_now(),
 5841:                     "hop_id": hop["hop_id"],
 5842:                     "source_role": role,
 5843:                     "route": decision.route,
 5844:                     "kind": _ALTERNATING_ROUTE_ROTATION_KIND,
 5845:                     "reason": (
 5846:                         f"Detected {alternating_streak} alternating routes between "
 5847:                         f"{alternating_pair[0]} and {alternating_pair[1]}; rotate target context."
 5848:                     ),
 5849:                 }
 5850:             )
 5851:         no_progress_cycle = self._no_progress_review_plan_cycle(state)
 5852:         if no_progress_cycle is not None:
 5853:             work_role, _progress_signature = no_progress_cycle
 5854:             self._block(
 5855:                 state,
 5856:                 (
 5857:                     f"Repeated no-progress route cycle detected for {work_role}: two "
 5858:                     f"{work_role} -> REVIEW -> PLAN -> {work_role} rounds carried materially "
 5859:                     "unchanged evidence. Inspect the accepted evidence and use Restart Role "
 5860:                     "only when a fresh context or materially new work is warranted."
 5861:                 ),
 5862:                 code=_NO_PROGRESS_ROUTE_CYCLE_BLOCK_CODE,
 5863:                 retryable=False,
 5864:             )
 5865:             return

[cdpa_prompts.py compact prompt / PLAN closure]
   60:                 else "DONE"
   61:                 for item in allowed_routes
   62:             )
   63:             guide = guide.replace(
   64:                 "PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE", route_contract
   65:             )
   66:         report_path = (
   67:             str(expected_report_path).strip()
   68:             if expected_report_path is not None
   69:             else self.naming_rule()
   70:         )
   71:         if not report_path:
   72:             raise ValueError("expected_report_path must not be empty")
   73:         return guide.replace(
   74:             ".plan/<team>/<physical-role>_turn<N>_<task-id>.md",
   75:             report_path,
   76:         ).replace(".plan/<team>", f"{self._plans_prefix()}/<team>")
   77: 
   78:     @staticmethod
   79:     def filesystem_scope(workspace: str, report_path: str) -> str:
   80:         path_type = PureWindowsPath if PureWindowsPath(workspace).drive else PurePosixPath
   81:         destination = path_type(workspace) / report_path
   82:         return (
   83:             f"FILESYSTEM_SCOPE: Resolve relative paths under {workspace}, not its parent or Git's top-level directory.\n"
   84:             f"Absolute report destination: {destination}\n"
   85:             "Use this exact working directory for MCP filesystem/tool calls. Keep the route handoff relative as specified."
   86:         )
   87: 
   88:     @staticmethod
   89:     def current_role_execution(
   90:         role: str,
   91:         *,
   92:         physical_role: str,
   93:         source_physical_role: str | None,
   94:     ) -> str:
   95:         normalized = validate_workflow_route_key(role)
   96:         continuation = ""
   97:         if source_physical_role and str(source_physical_role).strip() == str(physical_role).strip():
   98:             continuation = f" Continue the previous {normalized} turn; do not restate it."
   99:         directive = "Work first." + continuation + " Write the report, then return exactly one valid route JSON object."
  100:         if normalized == "PLAN":
  101:             directive += (
  102:                 "\n\nPLAN_CLOSURE: Treat the handoff and accepted role reports as closure evidence. After a clean REVIEW and satisfied task acceptance with no concrete remaining in-scope work, "
  103:                 "route DONE now. Do not route DEV merely because PLAN itself did not run tests, or for continued progression, closure preparation, or redundant validation. "
  104:                 "If not routing DONE, identify a concrete unmet acceptance criterion or fixable defect from the current evidence."
  105:             )
  106:         elif normalized == "REVIEW":
  107:             directive += " Clean work routes to PLAN; route DEV only for a concrete reproducible defect."
  108:         return directive
  109: 
  110:     @staticmethod
  111:     def tool_authority(workspace: str) -> str:
  112:         platform_hint = (
  113:             "This workspace is Windows: use @mcp-thinkbook."
  114:             if PureWindowsPath(workspace).drive
  115:             else "This workspace is Linux: use @mcp-g8 or @mcp-docker as authorized."
  116:         )
  117:         return (
  118:             "Use @mcp-g8 or @mcp-docker on Linux, and @mcp-thinkbook on Windows.\n"
  119:             + platform_hint
  120:             + "\nDo not claim tool or workspace access is unavailable until an actual tool call fails."

[chatgpt.py composer ownership / clear / refresh protections]
 4660:                     and visible_text_matches(existing, expected_existing)
 4661:                 )
 4662:                 if not permitted:
 4663:                     raise ComposerConflictError(
 4664:                         "composer contains unowned/manual input; refusing to overwrite"
 4665:                     )
 4666:             if not visible_text_matches(existing, expected):
 4667:                 await set_composer_text(self.page, expected, timeout_ms=timeout)
 4668:             post = await self._interaction_snapshot(timeout_ms=timeout)
 4669:             if not visible_text_matches(post.composer_text, expected):
 4670:                 raise ComposerConflictError("composer did not contain the exact requested text")
 4671:             self._owned_composer_text = expected
 4672: 
 4673:     async def clear(
 4674:         self,
 4675:         *,
 4676:         force: bool = False,
 4677:         expected_text: str | None = None,
 4678:         timeout_ms: int | None = None,
 4679:     ) -> None:
 4680:         timeout = timeout_ms or self.timeout_ms
 4681:         async with self.mutation_guard():
 4682:             snapshot = await self._interaction_snapshot(timeout_ms=timeout)
 4683:             existing = snapshot.composer_text.strip()
 4684:             if not existing:
 4685:                 self._owned_composer_text = None
 4686:                 return
 4687:             if expected_text is not None and not visible_text_matches(existing, expected_text):
 4688:                 raise ComposerConflictError(
 4689:                     "composer no longer matches the text expected by this workflow"
 4690:                 )
 4691:             if not force and not visible_text_matches(existing, self._owned_composer_text):
 4692:                 raise ComposerConflictError(
 4693:                     "composer contains unowned/manual input; refusing to clear"
 4694:                 )
 4695:             await clear_composer(self.page, timeout_ms=timeout)
 4696:             post = await self.assert_ownership()
 4697:             if not post.composer_empty:
 4698:                 raise ComposerConflictError("composer did not become empty")
 4699:             self._owned_composer_text = None
 4700: 
 4701:     async def new_chat(
 4702:         self,
 4703:         *,
 4704:         discard_draft: bool = False,
 4705:         expected_draft_text: str | None = None,
 4706:         discard_attachments: bool = False,
 4707:         stop_first: bool = False,
 4708:         timeout_ms: int | None = None,
 4709:     ) -> str:
 4710:         timeout = timeout_ms or self.timeout_ms
 4711:         async with self.mutation_guard():
 4712:             snapshot = await self.assert_ownership()
 4713:             if rate_limit_dialogs(snapshot):
 4714:                 await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
 4715:                 snapshot = await self.assert_ownership()
 4716:             if snapshot.stop_visible:
 4717:                 if not stop_first:
 4718:                     raise UnsafePageStateError(
 4719:                         "response is active; stop it explicitly before New chat"
 4720:                     )
 4721:                 await stop_response(self.page, timeout_ms=timeout)
 4722:                 snapshot = await self.assert_ownership()
 4723:                 if rate_limit_dialogs(snapshot):
 4724:                     await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
 4725:                     snapshot = await self.assert_ownership()
 4726:             if snapshot.blocking_dialogs:
 4727:                 limited = rate_limit_dialogs(snapshot)
 4728:                 if limited:
 4729:                     raise RateLimitBlockedError(
 4730:                         f"request rate limit is active: {list(limited)!r}"
 4731:                     )
 4732:                 raise UnsafePageStateError(
 4733:                     f"blocking dialog is open: {list(snapshot.blocking_dialogs)!r}"
 4734:                 )
 4735:             if snapshot.attachment_markers and not discard_attachments:
 4736:                 raise ComposerConflictError(
 4737:                     "New chat would discard manual/unowned attachments; "
 4738:                     "set discard_attachments=True explicitly"
 4739:                 )
 4740:             actual_draft = normalize_visible_text(snapshot.composer_text)
 4741:             if expected_draft_text is not None:
 4742:                 expected_draft = normalize_visible_text(expected_draft_text)
 4743:                 if actual_draft != expected_draft:
 4744:                     raise ComposerConflictError(
 4745:                         "New chat draft does not match the exact automated prompt provenance"
 4746:                     )
 4747:                 discard_draft = True
 4748:             if actual_draft and not discard_draft:
 4749:                 raise ComposerConflictError(
 4750:                     "New chat would discard a draft; set discard_draft=True explicitly"
 4751:                 )
 4752:             method = await open_new_chat(self.page, timeout_ms=timeout)
 4753:             post = await self.wait_until_clean_ready(
 4754:                 timeout_ms=timeout,
 4755:                 poll_ms=100,
 4756:             )
 4757:             if not post.composer_empty:
 4758:                 raise UnsafePageStateError("New chat did not produce an empty composer")
 4759:             self._owned_composer_text = None
 4760:             return method
 4761: 
 4762:     async def open_delete_dialog(self, *, timeout_ms: int | None = None) -> str:
 4763:         timeout = timeout_ms or self.timeout_ms
 4764:         async with self.mutation_guard():
 4765:             snapshot = await self.assert_ownership()
 4766:             if snapshot.stop_visible:
 4767:                 raise UnsafePageStateError("response is active; Stop before deleting chat")
 4768:             if snapshot.composer_text.strip() or snapshot.attachment_markers:
 4769:                 raise ComposerConflictError(
 4770:                     "deleting this chat would discard manual draft or attachments"
 4771:                 )
 4772:             return await open_delete_chat_dialog(self.page, timeout_ms=timeout)
 4773: 
 4774:     async def confirm_delete(self, *, timeout_ms: int | None = None) -> str:
 4775:         timeout = timeout_ms or self.timeout_ms
 4776:         async with self.mutation_guard():
 4777:             await self.assert_ownership()
 4778:             return await confirm_delete_chat(self.page, timeout_ms=timeout)
 4779: 
 4780:     async def delete_chat(self, *, timeout_ms: int | None = None) -> str:
 4781:         timeout = timeout_ms or self.timeout_ms
 4782:         async with self.mutation_guard():
 4783:             snapshot = await self.assert_ownership()
 4784:             if snapshot.stop_visible:
 4785:                 raise UnsafePageStateError("response is active; Stop before deleting chat")
 4786:             if snapshot.composer_text.strip() or snapshot.attachment_markers:
 4787:                 raise ComposerConflictError(
 4788:                     "deleting this chat would discard manual draft or attachments"
 4789:                 )
 4790:             await open_delete_chat_dialog(self.page, timeout_ms=timeout)
 4791:             return await confirm_delete_chat(self.page, timeout_ms=timeout)
 4792: 
 4793:     async def refresh(
 4794:         self,
 4795:         *,
 4796:         allow_responding: bool = True,
 4797:         timeout_ms: int | None = None,
 4798:     ) -> None:
 4799:         timeout = timeout_ms or self.timeout_ms
 4800:         async with self.mutation_guard():
 4801:             before = await self.assert_ownership(require_binding=self.binding is not None)
 4802:             if before.stop_visible and not allow_responding:
 4803:                 raise UnsafePageStateError("refusing to refresh an active response")
 4804:             if before.manual_input_pending:
 4805:                 raise ComposerConflictError("refresh would discard a manual draft or attachments")
 4806:             await refresh_page(self.page, timeout_ms=timeout)
 4807:             await self.assert_ownership(require_binding=self.binding is not None)
 4808:             self._owned_composer_text = None
 4809: 
 4810:     async def _wait_send_acceptance(

DECISION QUESTIONS:
1. Which existing mechanisms are hard safety invariants, which are deterministic workflow policy, and which are heuristic/over-gated model-output policing?
2. Is semantic parsing of free-form Markdown evidence justified in the controller? If not, what is the smallest replacement contract?
3. Are same-role streak, alternating-route rotation, and three-role evidence-aware no-progress detection best represented as separate guards, one guard, or mostly role policy?
4. Should controller-created UNSENT tool-picker/plugin artifacts have provenance-aware cleanup while true human drafts remain protected? Define the narrow safe boundary.
5. Should system-health completion depend on downstream Release/MCP feature completion, or only on representative health canaries plus truthful remaining blockers?
6. Is the current WAITING task safe and sufficiently scoped to start? If not, produce a replacement plan/task that is narrower and safer.
7. What should happen to the original source health task after this cleanup task starts?

OUTPUT EXPECTATION:
- verdict PASS/REVISE/REJECT
- objective assessment, strongest arguments both for keeping and removing current gates
- exact invariants to retain
- exact mechanisms to simplify/remove
- a complete plan_markdown suitable to save as the authoritative cleanup plan
- a complete team_task_text suitable for CDPA task creation, self-contained enough that the new PLAN does not need this conversation
- action for the current WAITING team and original health task
- revised_audit_markdown only if you believe the prior audit materially needs correction; otherwise null
