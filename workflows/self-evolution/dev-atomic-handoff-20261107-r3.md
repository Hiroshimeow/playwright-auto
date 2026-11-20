# G6 DEV r3 — Atomic successor handoff repair

Task: `g6-evolution-atomic-handoff-dev-20261107-r3`
Claimed parent: `cdpa-idem-2110b3b4b9ccf260155dfcaa`
Host/repository: `DESKTOP-B5E4IUE`, `E:\git-project\cdpa`, branch `rebuild/runtime-v2-r11`
Policy: `workflows/self-evolution/GOAL_AND_RULES.md` read in full before work; `AGENTS.md`, HCU project rules and self-repair skill inspected.

## Cause and changes

The r3 TEST evidence showed missing durable parent lineage, random task-create idempotency on every invocation, and false positive `DISPATCH_VERIFIED` / `SAFETY_HOLD` classifications based on Pi log text.

1. `spawn_cdpa.py`: parent transition derives one deterministic idempotency key via UUIDv5; includes `--parent-task-id` and `--idempotency-key`. A changed second submission with the same key is rejected by CDPA's existing command ledger (no second command/task). Full current canonical goal still goes into actual Solo `--goal`.
2. `cdpa_cli.py`, `dashboard_api.py`, `cdpa_worker.py`, `cdpa_store.py`, `cdpa_projection.py`: propagate, validate and persist explicit `parent_task_id` in the existing Solo task manifest and projected status. Parentage is provenance, not a scheduling dependency; the parent must exist during creation. Reuse the existing API command-idempotency and task storage; no new task ledger, scheduler or browser writer.
3. `run_pi.py`: confirm newly created successor through its exact on-disk CDPA manifest, parent, full initial prompt, matching `hops[0]` receipt and `requests.json` request ID with attempts=1. Never mark a log-only safety hold or unverified child as progress. Before a Pi cycle, atomically checkpoint `INTENT_PENDING`; after it, atomically checkpoint a verified result or `SAFETY_HOLD` with relevant task IDs, possible new Send request IDs, reason and `dispatch_allowed=false`. Interrupted `INTENT_PENDING`/`RUNNING` or a safety hold is blocked at restart rather than spawning a second child. Preserve pre-submit task identity through RUNNING heartbeats.
4. New `tests/test_self_evolution_atomic_handoff.py` tests stable key, duplicate-command prevention and payload conflicts in actual disposable CDPA RuntimeDB, parent manifest, exact prompt/request/receipt, missing/stale/duplicate attempts, pre-submit failure and post-commit failure, and fail-closed restart. Updated existing runner expectations that previously permitted unproven claims.

## Executed checks (G6 only)

- Initial red run: 5 failing regressions reproduced.
- Focused after repair: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_atomic_handoff.py tests/test_self_evolution_runner.py tests/test_self_evolution_propagation.py tests/test_self_evolution_core.py` — **30 passed, exit 0**.
- Solo/API regression: `.venv\Scripts\python.exe -m pytest -q tests/test_cdpa_solo_task.py tests/test_dashboard_api.py` — **70 passed, 1 skipped, exit 0** in a retained process. An earlier shell attempt timed out; its result was *not* counted as a passing suite.
- Python compileall of seven edited Python modules — exit 0.
- `git diff --check` — exit 0.

## Explicit limitations — NOT_ACCEPTED release gate

This is an **offline code repair**, not full live atomic handoff acceptance. A runner-local `.runtime/self-evolution/pi-runner.json` safety hold is durably written, but **has not been proven equivalent to an authoritative CDPA task/command-level hold transaction**. In particular, a new child may exist before its initial Send receipt; Pi conservatively blocks further dispatch until exact reconciliation, and no production-grade automated resolution path has yet been demonstrated. A real Pi kill/restart, actual G6 successor dispatch and exact browser Send, continued task completion, E2E restart and noninterference, broad repository suite and independent G6 Solo REVIEW + FJP Opus review remain unproven. The existing runtime was not reloaded to load modified source.

No new task was spawned, no live API mutation/Send performed, no accepted or ambiguous request replayed, and no Chrome/runtime/9212/FJP action occurred. Existing unrelated dirty and untracked files were preserved; this DEV did not push, deploy, or create a release.

## Pi-only next action

Reconcile exact parent `cdpa-idem-2110b3b4b9ccf260155dfcaa`, this child identity, existing CDPA command ledger and accepted Send before any live dispatch or runtime reload. Arrange independent G6 REVIEW and FJP Opus critique of minimal owner and the authoritative hold boundary; then test a **genuine task-create command commit followed by Pi kill before checkpoint** with same idempotency key. Confirm exactly one child, durable parent, full prompt, receipt, request attempts=1 and no duplicate; verify safe unblocking of holds. If safety hold resolution remains manual or ambiguous, keep campaign NOT_ACCEPTED and Pi dispatch blocked.
