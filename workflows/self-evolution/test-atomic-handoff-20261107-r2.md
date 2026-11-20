# G6 TEST — Atomic handoff offline gate (2026-11-07 r2)

**Scope:** CDPA Self-Evolution TEST `g6-evolution-atomic-handoff-test-20261107-r2`. Exact task `cdpa-idem-2110b3b4b9ccf260155dfcaa`, parent `cdpa-idem-de15d32234531e7e0f01c19f`. Owner G6 `DESKTOP-B5E4IUE`, `E:\git-project\cdpa` (`rebuild/runtime-v2-r11`).

**Verdict: TEST SLICE COMPLETE WITH NEGATIVE GATE — ATOMIC HANDOFF NOT_ACCEPTED; full campaign NOT_ACCEPTED.** No browser Send, Resume, spawn, Chrome action, 9212 or FJP operation, actual coordinator kill, or live CDPA task mutation performed.

## Mandatory intake

Read full `workflows/self-evolution/GOAL_AND_RULES.md` first, `AGENTS.md`, HCUW repository guidance and skill, G6 self-repair skill, prior r1 crash TEST and r2 core-fix report, live current implementation `run_pi.py`, `spawn_cdpa.py`, actual CDPA CLI, `TaskStore.create_task`, worker command application, and focused tests. Loaded verification-before-completion and systematic-debugging skills. `hcuw fingerprint .` returned `sha256:f3dabe91f3fc750298446bcae8e6eee3cf49a8d71c7b6a5ac7ab03a9eed2c4ca`; existing `.agents/rules/project.md` fingerprint `sha256:b55af76f7c107843707a096f835f5a8c3ca29da6455e5319da3e2bdeaf0f3cb4` differs; not modified.

## Evidence

- `workflows/self-evolution/atomic_handoff_r2_offline_probe.py`: disposable `TemporaryDirectory` manifest/request-ledger fault probes using real `spawn_cdpa.command`, real `PromptBuilder.build_solo`, real `run_pi.campaign_tasks`/ `classify_cycle`, and `cdpa_cli.submit_task` with `_post` intercepted **before any networking**. No actual CDPA control-plane mutation.
- `workflows/self-evolution/atomic-handoff-r2-probe-output.json`: complete machine-readable results; diagnostic exited **2 by design**, 1 PASS and 5 NOT_ACCEPTED.
- `workflows/self-evolution/atomic-handoff-r2-focused-pytest.txt`: actual existing regression suite `20 passed in 2.32s`, exit **0**.
- `python -m py_compile workflows/self-evolution/atomic_handoff_r2_offline_probe.py`: exit **0**; `git diff --check`: exit **0** (only existing LF/CRLF warnings).
- Existing current real TEST task manifest `.plan/g6-evolution-atomic-handoff-test-20261107-r2/cdpa-idem-2110b3b4b9ccf260155dfcaa/cdpa-self-evolution-test-g6-evolution-atomic-handoff-test-20261107-r2.json` observed read-only: RUNNING, `hops[0].prompt` 10,982 bytes, request `...-hop1` status sent / attempts **1**, matching receipt. This observation does not prove terminal success or crash replay safety.

## Fault matrix

| Offline scenario | Result | Reproducible fact |
|---|---|---|
| Canonical constructor + renderer | PASS (offline only) | Entire policy, absolute path, parent, bounded instruction, canonical constructor and one recorded request attempt survive first-turn rendering. |
| Crash before submit | NOT_ACCEPTED | `classify_cycle` yields RECOVERY_PENDING; **zero** committed successors and **zero** authoritative durable holds. |
| Submit accepted but response/ack lost | NOT_ACCEPTED | Two calls to the actual `cdpa_cli.submit_task` without an explicit key generated **different idempotency keys**; `spawn_cdpa.py` supplies no stable CLI key. The probe did not send either request. |
| Child committed before Pi checkpoint | NOT_ACCEPTED | `classify_cycle` returns **DISPATCH_VERIFIED** on task-ID delta and matching `PI_ACTION` text even when the offline child has **wrong parent**, **missing full policy**, and **attempts=2**. |
| Coordinator/child termination and subsequent cycle | NOT_ACCEPTED | First cycle RECOVERY_PENDING; second cycle DISPATCH_VERIFIED a second successor for the **same parent** (2 committed children in disposable ledger). |
| Durable safety hold | NOT_ACCEPTED | A log-only `PI_ACTION: SAFETY_HOLD task ... blocked` is accepted as safety hold with **no durable hold receipt** bound to exact parent/task/request. |

**Important limitation:** These are *offline falsifications* of the current production classification and CLI submission identity, not real OS process-termination tests or evidence that an actual browser Send was replayed. The temporary ledger uses minimal JSON records, not a complete production CDPA store. In particular the probe proves the present runner **does not enforce** required atomic/durable handoff; it does not assert the underlying CDPA store inherently lacks all atomic operations.

## Narrow repair request to Pi / G6 DEV

At the **existing** CDPA command/task ledger and `run_pi.py` admission boundary, use one **persisted, stable idempotency identity per parent transition** before calling the canonical constructor. On restart, reconcile that same command/task/request identity, never re-submit an ambiguous command with a newly generated key. Confirm successor **parent linkage**, full first-turn policy/receipt, exact owned request ID, and accepted/ambiguous Send attempts=1 *from authoritative records* before success; or durably persist a safety hold correlated to those identities. A log line or runner-local `pi-runner.json` status alone must not count as a committed transition. Do not add another store/scheduler and do not bypass the sole Pi dispatch owner.

For DEV's subsequent proving test, fault-inject at the actual durable task/command boundary: pre-submit, persisted intent before network submit, HTTP accepted but ack lost, CDPA child committed before Pi checkpoint, Pi crash and child termination. Reconcile one stable key and assert **exactly one successor or authoritative durable hold**, no second active owner, exact parent/request, full first turn, attempts=1. Follow with independent G6 REVIEW + FJP Opus, broad regression, safe live recovery and E2E before release acceptance.

**Changed only task-owned probe and evidence files:** `atomic_handoff_r2_offline_probe.py`, `atomic-handoff-r2-probe-output.json`, `atomic-handoff-r2-focused-pytest.txt`, and this report. Pre-existing tracked dirty files `run_pi.py` / `tests/test_self_evolution_runner.py` were inspected but not edited by this TEST. Pi is solely responsible for successor dispatch.
