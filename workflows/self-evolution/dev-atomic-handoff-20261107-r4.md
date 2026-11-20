# G6 DEV r4 — Atomic successor creation and crash reconciliation

- Task: `g6-evolution-atomic-handoff-dev-20261107-r4`
- Parent: `cdpa-idem-e4bcd5eeef67218835ac8d70`
- Repository: `E:\git-project\cdpa`, branch `rebuild/runtime-v2-r11`
- Scope: bounded offline source/test changes only; **NOT_ACCEPTED** for full live campaign.

## Entry, root cause and owner

Read `workflows/self-evolution/GOAL_AND_RULES.md` completely before actions, plus `AGENTS.md`, HCUW project rules, skills and prior r3 DEV/TEST evidence. Existing r3 work supplied deterministic parent-scoped create idempotency and parent manifests, but Pi did not reserve an exact transition identity ahead of submission, its heartbeats could overwrite evidence, its recovery consulted only task inventory/runner text (not CDPA's existing command ledger), and restart could not automatically validate a committed child. A fake `PI_ACTION` or newly observed task is not authoritative.

## Changes (all on G6)

1. `spawn_cdpa.py`: before calling the existing CLI to create a parented Solo task, durably reserve parent, team, role, deterministic task ID, unchanged parent-scoped idempotency key and SHA-256 of full actual `--goal` in the Pi runner checkpoint. A missing or conflicting owner/reservation fails before subprocess submission.
2. `run_pi.py`: serialize and fsync atomic state writes across the runner and canonical constructor using the existing single-owner runner state. Heartbeats preserve the transition. Reconcile the exact persisted creation key **read-only** after crash/restart; require the matching applied CDPA command, parent/team/role, goal hash, manifest, actual first-turn full policy prompt, bounded instruction, matching receipt/user-turn identity, one request attempt and creation-command provenance in `applied_command_ids`. Missing, queued, contradictory or ambiguous evidence stays in a durable runner `SAFETY_HOLD` with dispatch disabled; no task submit/Send replay. Once verified, clear the active reservation and retain the prior verified transition as evidence, without a browser action.
3. `dashboard_api.py`: add a read-only GET at `/api/commands/by-idempotency/<key>` served by **the existing** CDPA RuntimeDB command ledger; return only status, identity/provenance and goal hash, not the complete prompt or sensitive payload. No parallel queue/store/scheduler.
4. `tests/test_self_evolution_handoff_recovery.py`: new offline fault-injection cases for pre-submit durable reservation and heartbeat preservation; absent, pending and conflicted commands; accepted-but-lost-ack; post-commit/pre-checkpoint verification; duplicate prevention/conflicting transition; command ID mismatch; prompt/receipt/request tampering; actual constructor intent-before-submit ordering; Pi restart reconciliation without redispatch; and read-only API lookup/no goal leakage. Existing `tests/test_self_evolution_atomic_handoff.py` fixture now contains realistic request prompt/receipt and accepted user identity to satisfy stricter verification.

## Evidence (fresh commands on G6)

- Initial RED: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_handoff_recovery.py` — **5 failures** before implementation (missing intent/ledger lookup). A subsequent intermediate RED identified heartbeat loss and missing ledger prompt validation.
- Focused GREEN: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_handoff_recovery.py tests/test_self_evolution_atomic_handoff.py tests/test_self_evolution_runner.py tests/test_self_evolution_propagation.py tests/test_self_evolution_core.py` — **37 passed; exit 0**.
- Additional relevant regression: `.venv\Scripts\python.exe -m pytest -q tests/test_cdpa_solo_task.py tests/test_dashboard_api.py tests/test_cdpa_runtime_db.py` — **75 passed, 1 skipped; exit 0**.
- `git diff --check` — **exit 0**; Git emitted only existing LF/CRLF normalization warnings.
- Real existing G6 request ledger inspection was **read-only**; its hop receipt and request receipt share user message ID and conversation ID, both prompt fields matched, and attempts = 1. No live task-create or Send performed during this DEV.

## Remaining release limitations / Pi-only handoff

**Not a live accepted successor handoff.** This repair has not been deployed/reloaded into live CDPA; no actual Pi process kill during a real HTTP task-create, real controlled browser Send/receipt, full suite, independent G6 Solo REVIEW, FJP Opus review or end-to-end continuity/noninterference gate was executed in this task. The runner's durable safety hold is authoritative only for its **own Pi dispatch gate**, not a claimed new CDPA control-plane task hold. Automatic read-only reconciliation can unblock on evidence of a fully applied and accepted task; an absent/ambiguous command or irreversible Send remains an explicit safety hold for Pi/operator reconciliation. Pi alone owns any future task dispatch and runtime recovery.

Before attempting any live action, Pi must reconcile parent `cdpa-idem-e4bcd5eeef67218835ac8d70`, that task's accepted request `cdpa-idem-e4bcd5eeef67218835ac8d70-hop1`, and the existing command ledger. Arrange independent reviews and a live isolated crash-at-commit test without replaying accepted/ambiguous requests. Preserve unrelated dirty/untracked files. No restart of CDPA runtime or Chrome; no 9212/FJP browser operations; no commit/push/deploy.

**DEV bounded conclusion:** offline implementation and focused regression PASS; campaign acceptance remains **NOT_ACCEPTED** pending Pi reconciliation and independent live gates.
