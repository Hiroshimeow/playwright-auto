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