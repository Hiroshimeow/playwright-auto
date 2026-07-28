# Maintainers self-learning design

## Scope

This upgrade keeps Maintainers as the exclusive operational recovery owner on the shared independent-agent engine. It improves one existing persistent artifact, repository-root `LEARNING.md`, only after a recovery outcome is verified. It does not add an improvement agent, sidecar, coordinator, evaluator service, scheduler, queue, database, approval workflow, or model-to-worker action protocol.

Research was bounded to primary papers and their official project material. The selected design is evidence-gated, incremental, section-local, and explicitly allowed to produce no lesson.

## Primary-source comparison

| Approach | Useful mechanism | Risk or rejected complexity | CDPA rule selected |
|---|---|---|---|
| Reflexion — Shinn et al., [arXiv:2303.11366](https://arxiv.org/abs/2303.11366) | Converts task feedback and trajectories into verbal reflection held in episodic memory. | Reflection can preserve a bad causal explanation when feedback or attribution is wrong. | Learn only from an observed outcome with a verified causal account; do not persist an unverified narrative. |
| Self-Refine — Madaan et al., [arXiv:2303.17651](https://arxiv.org/abs/2303.17651) | Alternates actionable feedback and refinement with a stopping condition. | Recursive refinement can regress when self-feedback is wrong and adds unnecessary cycles. | Run exactly one bounded learning pass after operational verification, with an explicit skip result. |
| ExpeL — Zhao et al., [arXiv:2308.10144](https://arxiv.org/abs/2308.10144) | Extracts reusable insights from successful and failed experiences and updates accumulated knowledge. | Raw reflections may hallucinate; append-only accumulation produces duplicates and conflict. | Compare retained incidents, update a matching lesson in place, and revise or skip when evidence conflicts. |
| Voyager — Wang et al., [arXiv:2305.16291](https://arxiv.org/abs/2305.16291) | Uses environment feedback, execution errors, and self-verification before retaining reusable skills. | An executable skill library is unnecessary for short CDPA operating rules. | Do not write a lesson before the operational postcondition is verified. |
| Generative Agents — Park et al., [arXiv:2304.03442](https://arxiv.org/abs/2304.03442) | Synthesizes higher-level reflections from accumulated observations and links them to supporting memories. | Full memory streams and retrieval scoring duplicate retained CDPA reports. | Put supporting evidence in the Maintainers report; keep only the distilled rule in `LEARNING.md`. |
| Agent Workflow Memory — Wang et al., [arXiv:2409.07429](https://arxiv.org/abs/2409.07429) | Induces reusable routines from evaluated trajectories and measures cross-task transfer. | Copying a task trajectory as memory overfits to one workflow. | A lesson must express a reusable condition and action/check, not task-specific steps. |
| Agentic Context Engineering — Zhang et al., [arXiv:2510.04618](https://arxiv.org/abs/2510.04618) | Applies incremental item-level context updates, deduplication, and preservation of unrelated knowledge. | Generator/Reflector/Curator role separation would recreate prohibited improvement architecture. | Mutate one exact section or bullet, reject stale target content, preserve unrelated edits, and validate after write. |
| Knowledge-Centric Self-Improvement — Wang et al., [arXiv:2607.19592](https://arxiv.org/abs/2607.19592) | Improves an inspectable shared knowledge artifact while keeping agents generic. | Forums, adjudicating agents, and a typed knowledge database are excessive here. | Improve the existing shared file rather than Maintainers architecture; require evidence and transferability. |
| OEP — Wang et al., [arXiv:2605.18930](https://arxiv.org/abs/2605.18930) | Demonstrates that locally correct experiences can become harmful non-transferable persistent rules. | Severe hypothetical consequences can bias reflection toward over-generalized policy. | A one-off locally correct recovery is insufficient; require recurrence or a deterministic cross-task invariant. |
| Promptbreeder — Fernando et al., [arXiv:2309.16797](https://arxiv.org/abs/2309.16797) | Evolves prompt populations against a benchmark fitness function. | CDPA has no stable Maintainers fitness set; mutation populations and evaluators violate the bounded task. | Reject autonomous prompt evolution. Maintain a human-readable prompt contract and evidence-backed shared lessons. |

## Selected policy

### When evidence justifies a lesson

A lesson may be added or revised only after the recovery result is verified and all five checks pass:

1. **Observed facts:** retained evidence identifies the failure, action, and postcondition.
2. **Causal support:** evidence explains why the failure occurred or why the rule works; plausibility alone is insufficient.
3. **Transferability:** either the same root cause appears in retained prior evidence, or a deterministic invariant/regression proves cross-task applicability from one incident.
4. **Actionability:** the rule states its applicability condition and a concrete action or check.
5. **Consistency:** the rule preserves operator intent, exact ownership, accepted-send non-replay, idempotency, dependencies, and current repository policy.

The default for a one-off transient dependency outage, temporary machine condition, or similar symptom without causal/transfer evidence is `SKIPPED — insufficient reusable evidence`.

### Noise, recurrence, and systemic defects

Recurrence means the same root cause or violated invariant, not merely a similar visible symptom. Task IDs, team names, page/request IDs, timestamps, raw paths, machine-local values, and one temporary outage belong in incident evidence, not in the shared rule.

A systemic, recurring, or symptomatically recovered source defect requires a normal repair task even when a useful lesson is recorded. Learning and repair are independent decisions:

- `CONTINUE_IN_PARALLEL` when the recovered task is stable and continuing cannot cross an integrity boundary.
- `HOLD_FOR_REPAIR` when continuing risks ownership, accepted-send, durable-state, dependency, or idempotency integrity.

A lesson never substitutes for code repair and never claims that a pending repair succeeded.

### Pollution and contradiction controls

The Maintainers report separates **Facts**, **Inference**, and **Proposed reusable rule**. Before mutation, Maintainers searches current `LEARNING.md` for equivalent or conflicting guidance.

- Equivalent guidance: `SKIPPED` with no mutation.
- Matching but incomplete guidance: `REVISED` in place.
- Matching guidance disproved or stale under its stated condition: revise that exact bullet; use a narrowly adjacent `SUPERSEDED` note only when retaining the old text prevents ambiguity.
- Contradictory or insufficient evidence: `SKIPPED`; do not append a second rule.

Lesson text is concise and operational. It excludes secrets, credentials, raw paths, transient IDs, timestamps, incident chronology, task-specific procedures, speculative consequences, and generic warnings. `LEARNING.md` is a reusable playbook, not an incident log.

### Shared-file mutation protocol

PLAN and Maintainers use one repository-owned operation rather than raw generic MCP file mutation:

```bash
uv run python -m playwright_auto.cdpa_learning --repository <repository-root>
```

The operation accepts exactly one JSON object on stdin with `disposition`, `old_text`, and `new_text`; it is a narrow file mutation boundary, not a storage subsystem or worker action parser.

1. Resolve repository-root `LEARNING.md`, reject symlinks/escapes, and use one repository-scoped lock file.
2. Under that lock, read the current file as strict UTF-8 immediately before mutation.
3. Require the old span to occur exactly once as a complete byte-exact Markdown bullet/section; whitespace-fuzzy fallback is forbidden.
4. Apply one bounded replacement/addition and validate canonical Markdown title/headings, sanitization, and normalized duplicate lessons.
5. Write a same-directory temporary file, preserve the target mode, fsync it, atomically replace the target, and fsync the parent directory.
6. Read back before releasing the lock and verify exact bytes plus unchanged prefix/suffix, so compliant overlapping writers serialize and preserve unrelated edits.
7. Return validated JSON containing `ADDED`, `REVISED`, or `SUPERSEDED`; a stale/conflicting request fails without mutation. `SKIPPED` is recorded by the agent when no mutation is justified.

This learning path can mutate only repository-root `LEARNING.md`. It cannot edit manifests, SQLite, request ledgers, source, tests, configuration, role reports, or task deliverables.

## Controlled acceptance scenarios

| Scenario | Evidence | Learning result | Repair result |
|---|---|---|---|
| One-off dependency outage | One transient failure; direct recovery and stability verified; no general invariant | `SKIPPED — insufficient reusable evidence`; no file mutation | None |
| Recurring exact-ownership failure | Same root cause in retained incidents or deterministic ownership regression | One concise `ADDED` rule | Create/reuse repair if source defect remains |
| Matching lesson misses a verified condition | Existing semantic match plus new supported applicability condition/check | Exact bullet `REVISED`; no duplicate | Separate decision |
| Existing lesson is stale or harmful | Current incident plus contrary retained/regression evidence | Exact bullet `REVISED`, or narrowly `SUPERSEDED`; unrelated history untouched | Repair if implementation defect exists |
| Plausible but unverified inference | No causal proof or transfer evidence | `SKIPPED`; no mutation | None unless concrete defect evidence independently exists |
| Concurrent target edit | Exact old target no longer exists | Stale mutation rejected; re-read and reassess | None |
| Concurrent unrelated edit | Two compliant writers overlap while targeting different exact spans | Shared lock serializes both edits; neither unrelated change is lost | None |
| Secret/path/task-ID evidence | Incident evidence contains credentials, absolute paths, or transient identities | Sanitized generalized rule, or `SKIPPED` if sanitization removes meaning | Separate decision |
| Systemic defect with safe continuation | Verified broad defect; current task stable | Learning gate evaluated separately | `CONTINUE_IN_PARALLEL` |
| Systemic defect unsafe to continue | Continuing risks an irreversible/integrity boundary | Learning gate evaluated separately | `HOLD_FOR_REPAIR` |
| Normal recovery preservation | Exact task/hop/request/receipt retained and postcondition verified | One bounded pass, then completion | Existing direct controls only |

### Exact-edit examples

**Stale target rejection**

- Initial target bullet: `- Reopen the saved conversation before resuming.`
- Another writer changes that exact bullet before Maintainers edits.
- Exact-content replacement finds no match and must fail without writing.
- Maintainers re-reads the file and either revises its proposal against the new evidence or records `SKIPPED`.

**Unrelated concurrent edit preservation**

- PLAN and Maintainers invoke the same helper while targeting different exact bullets.
- The first writer holds the repository lock through atomic replace and read-back.
- The second writer waits, then reads the first writer's result under the same lock and applies only its own exact span.
- The final file contains both changes; raw generic file edits are prohibited because they do not participate in this lock.

**Sanitization**

- Evidence may cite an absolute repository path, a task ID, page ID, timestamp, or credential while proving the incident.
- The report retains only the minimum sanitized evidence needed for review.
- The lesson states the general invariant and action; no raw identity or secret enters `LEARNING.md`.

## Rejected complexity

Three design options were evaluated:

1. **Prompt + one locked exact-edit helper + focused tests — selected.** It reuses the independent-agent lifecycle, direct worker commands, `exclusive_file_lock`, atomic replace/fsync primitives, retained reports, and the existing settings command.
2. **Raw generic MCP edit tools — rejected.** Their full-file snapshot/rename path has no shared repository lock or exact-span boundary and can lose an overlapping unrelated write or apply whitespace-fuzzy matches.
3. **Improvement agent, structured lesson database, evaluator/approval layer, or prompt evolution — rejected.** These duplicate existing state and directly violate the one-engine, single-operator, bounded-learning constraints.

The selected design is the smallest complete solution: recovery remains operationally authoritative; learning starts only after verification; bad or weak evidence produces no mutation; repair and learning stay separate; and concurrent shared-file edits fail closed or preserve unrelated work.
