# G6 TEST r3 — Parent propagation and atomic handoff

**Task:** `cdpa-idem-e4bcd5eeef67218835ac8d70` (team `g6-evolution-parent-atomic-handoff-20261107-r3`), claimed parent `cdpa-idem-2110b3b4b9ccf260155dfcaa`.

**Verdict: PAUSE — atomic successor-or-authoritative-safety-hold NOT_ACCEPTED.** Parent prompt propagation passes; durable parent ownership and crash-proof atomic handoff do not. This report is TEST evidence, **not** an authoritative CDPA control-plane hold transaction. Pi must reconcile the recorded task/parent/request; only Pi can dispatch a successor.

## Exact verification

- Canonical policy `GOAL_AND_RULES.md` and `AGENTS.md` read on G6; HCUW/repository rules and self-repair skill inspected. Repo `E:\git-project\cdpa`, branch `rebuild/runtime-v2-r11`.
- Focused deterministic suite: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_core.py tests/test_self_evolution_propagation.py tests/test_self_evolution_runner.py` → **20 passed in 2.28s; exit 0**.
- Offline fault probe rerun: `.venv\Scripts\python.exe workflows\self-evolution\atomic_handoff_r2_offline_probe.py` → **1 PASS, 5 NOT_ACCEPTED**. Fault cases: pre-submit crash, accepted-but-unacknowledged task submission, post-commit/pre-checkpoint incorrect verification, Pi crash/restart duplicate child, and log-only safety hold. Probe deliberately returns nonzero when negative gates remain. Shell wrapper showed exit 1. No real HTTP task mutation or browser Send.
- Real created task manifest and `/api/tasks` projected status **RUNNING** at inspection. Manifest `hops[0].prompt` contains exact complete current policy, real parent ID, bounded TEST instruction, absolute policy path, canonical constructor and 9202 recovery commands. Hop `receipt.prompt` is likewise full text. The exact request `cdpa-idem-e4bcd5eeef67218835ac8d70-hop1` is `sent`, **attempts 1**, with receipt `accepted_via=user_message_identity` and matching page binding; **no terminal route** yet during observation.
- Critical lineage gap: parent ID exists **only inside free-text goal/prompt**; the child manifest has no `parent_task_id` and `depends_on_task_ids=[]`; request ledger has no parent field and API projection has neither parent ID nor dependency field. This fails persisted parent identity requirements despite correct first-turn propagation.
- Source check: `spawn_cdpa.py` produces a task title and full `--goal` but does not supply a stable creation key or persisted parent linkage. In `cdpa_cli.submit_task`, Solo's request omits dependency/parent lineage and defaults to a new UUID idempotency key. `run_pi.classify_cycle` can mark `DISPATCH_VERIFIED` from new task ID plus `PI_ACTION` text without parent/receipt/requests validation and can classify a safety hold from uncommitted log text.
- `git diff --check` exited 0 (existing LF/CRLF warnings); unrelated dirty tracked/untracked files preserved.

## Reconciliation and required next action

**Do not replay** this accepted Send or a possibly accepted task-submission command. Reconcile exact parent `cdpa-idem-2110b3b4b9ccf260155dfcaa`, current task `cdpa-idem-e4bcd5eeef67218835ac8d70`, owned request `cdpa-idem-e4bcd5eeef67218835ac8d70-hop1`, and the existing command ledger before any new dispatch.

G6 DEV should repair the **existing** task/command-ledger ownership boundary, using one durable stable idempotency identity per parent transition and durable parent linkage. Pi must not treat a log-only hold or `DISPATCH_VERIFIED` text as a committed transition. Then fault-inject before submit, after durable intent, accepted-with-lost-ack, child-commit-before-Pi-checkpoint and Pi restart, checking exactly one valid child or an authoritative durable hold, full prompt/receipt, attempts=1 and no competing owner. Require G6 independent REVIEW, FJP Opus advisory review for significant change, broad regression and live E2E before release acceptance.

**Evidence:** `workflows/self-evolution/test-parent-atomic-handoff-20261107-r3-evidence.json`; prior `workflows/self-evolution/atomic-handoff-r2-probe-output.json`; live manifest and `requests.json` under `.plan/g6-evolution-parent-atomic-handoff-20261107-r3/cdpa-idem-e4bcd5eeef67218835ac8d70/`. No browser, 9212, FJP, runtime, or accepted Send was mutated.
