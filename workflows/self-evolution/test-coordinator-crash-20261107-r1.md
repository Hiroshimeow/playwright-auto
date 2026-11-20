# G6 TEST — Coordinator crash / handoff gate (2026-11-07, r1)

**Task:** `cdpa-idem-de15d32234531e7e0f01c19f` · team `g6-evolution-coordinator-crash-test-20261107-r1` · parent `cdpa-idem-8a09e7d357e1fd22c6e965d2` · G6 `DESKTOP-B5E4IUE`, `E:\git-project\cdpa`.

**Verdict:** **BOUNDED TEST COMPLETED; CAMPAIGN NOT_ACCEPTED.** Disposable Windows process/lock tests and complete first-turn contract propagation PASS. **Atomic task-successor-or-safety-hold handoff across coordinator crash is NOT PROVEN and has a reproducible enforcement gap.** No live process was terminated, no browser was opened/focused, and no CDPA control, Send, Resume, Retry, Stop, Clear, or source/runtime restart was performed.

## Mandatory intake and scope

Read `workflows/self-evolution/GOAL_AND_RULES.md` **in full before work**; then `AGENTS.md`, `.agents/skills/hcu-work/SKILL.md`, `.hcu/profile.md`, `.hcu/rules.md`, `.agents/rules/project.md`, `LEARNING.md`; consulted the verification-before-completion skill, `run_pi.py`, `spawn_cdpa.py`, `core.py`, `tests/test_self_evolution_{runner,propagation,core}.py`, and prior independent `review-pi-cycle-r1.md` and `review-g6-r3.md`. No independent source or release review is inferred from these reports. `hcuw fingerprint .` produced `sha256:f3dabe91f3fc750298446bcae8e6eee3cf49a8d71c7b6a5ac7ab03a9eed2c4ca`; the existing project rulebook records `sha256:b55af76f7c107843707a096f835f5a8c3ca29da6455e5319da3e2bdeaf0f3cb4`. This is policy-fingerprint drift; no policy files were modified by TEST.

## Exact commands and measured results

1. **Focused offline checks (G6, Windows):**
   ```powershell
   .venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_self_evolution_runner.py tests/test_self_evolution_propagation.py tests/test_self_evolution_core.py
   ```
   **Exit 0; 15 passed in 2.42s** (retained process output: `............... [100%]`). Tests exercise disposable kill-on-close job/descendant teardown, second-owner file-lock refusal and crash release, stale child fail-closed, prompt construction, 50 generations and basic policy parsing. They **do not** exercise a real CDPA successor transaction.

2. **Read-only live Pi observations:** `Get-Content .runtime/self-evolution/pi-runner.json`, `Get-Process -Id 32200,45348 -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime`, and `Get-CimInstance Win32_Process -Filter 'ProcessId=32200 OR ProcessId=45348' | Select-Object ProcessId,ParentProcessId,Name,CreationDate`.
   - First observation: `status=RUNNING`, iteration 2, owner PID **32200**, Pi wrapper PID **45348**, `no_output_seconds=311`, health `ready=false, worker=SOURCE_DRIFT`.
   - Both PIDs existed at observation; Win32 parent of 45348 was 32200. Later observation: `status=NEXT_CYCLE`, iteration 2, `last_exit=0`, worker still `SOURCE_DRIFT`; `pi-runner.log` ends in `PI_ACTION: SAFETY_HOLD` and `PI_EXIT_CODE=0`.
   - This is evidence of **one observed runner chain making a cycle transition**, not proof that no second runner exists, indefinite liveness, or transactional recovery after a real crash. No live lock contention or destructive crash injection was attempted.

3. **Offline falsification of cycle-action admission (no real Pi/CDPA):** invoked `run_pi.run_cycle(707)` with disposable `Popen` stand-in returning exit 0, `build_pi_command` replaced by inert command, `health` fixed to ready, and `write_state` collected in memory. Output:
   ```text
   INERT_PI_CYCLE_EXIT 0 STATES ['RUNNING']
   ```
   **Exit 0.** An inert Pi which performs **no dispatch and no safety hold** is treated as a successful cycle by `run_cycle`. Real `run()` then writes `NEXT_CYCLE` on return code 0. The runner does **not parse or validate** `PI_ACTION`, require an authoritative CDPA successor/hold receipt, or atomically commit the coordinator's handoff with CDPA task state. The currently observed `PI_ACTION: SAFETY_HOLD` is a separately written operator checkpoint, not that missing transactional guarantee. This is a **negative gate finding**, not a live crash demonstration.

4. **Exact real first-turn/request verification (read-only):** queried the current task manifest and `requests.json` through local Python JSON parsing; stdout:
   ```text
   task_id=cdpa-idem-de15d32234531e7e0f01c19f; status=RUNNING;
   ledger=sent; attempts=1; prompt_bytes=11077; canonical_bytes=8899;
   full_contract=True; path_in_prompt=True; parent_in_prompt=True;
   instruction_in_prompt=True; spawn_in_prompt=True;
   match_receipt=True; match_request=True
   ```
   Underlying sources:
   - `.plan/g6-evolution-coordinator-crash-test-20261107-r1/cdpa-idem-de15d32234531e7e0f01c19f/cdpa-self-evolution-test-g6-evolution-coordinator-crash-test-20261107-r1.json` (`hops[0].prompt`, `hops[0].receipt.prompt`)
   - Same directory `requests.json` (`cdpa-idem-de15d32234531e7e0f01c19f-hop1`, `status=sent`, `attempts=1`, accepted via exact user-message identity; owned 9202 page `97689a40-d751-496e-86e4-5099c4dab4dc`).
   The actual delivered first turn, its receipt and request ledger **match exactly**. This proves this task's full-text propagation and one accepted Send, not its assistant terminal route or future generations' real dispatch.

5. **Existing operator hold and drift evidence:** `.runtime/self-evolution/operator-checkpoint-20261107-cycle-pi-coding.json` records `pi_action=SAFETY_HOLD`, exact task/request, `attempts=1`, `response_terminal=false`, worker `SOURCE_DRIFT`, and next reconciliation action. `workflows/self-evolution/INCIDENT_SOURCE_DRIFT_20261107.md` records changes in `src/playwright_auto/cdpa_routes.py` and `src/playwright_auto/cdpa_worker.py` while the request awaited response. Neither proves an **atomic** handoff with the CDPA ledger. This TEST did not create that checkpoint or modify those files.

## Gate matrix

| Gate | Result | Evidence / limitation |
| --- | --- | --- |
| Windows singleton / disposable crash child cleanup | **PASS locally** | Runner focused pytest; no live forced crash |
| No overlapping operator after real runner crash | **NOT_ACCEPTED** | Observed parent chain only; not a tested deployed takeover |
| Full contract in actual Solo first turn and receipt | **PASS for current task** | Exact manifest, receipt and request; 11,077-byte prompt |
| Atomic successor **or durable safety hold** across crash | **NOT_ACCEPTED** | Inert cycle exit 0 admitted without either action; no atomic CDPA ledger handshake |
| Accepted Send not replayed | **PASS for observation only** | Current request ledger attempts=1; recovery replay not tested |
| Live CDPA/worker health and source-drift reload | **NOT_ACCEPTED** | Worker SOURCE_DRIFT; accepted request pending; no restart authorized here |
| Broad regression / full mission E2E / 9212-FJP noninterference | **NOT_TESTED** | Deliberately outside this offline TEST slice |

## Narrowest falsification/repair handoff to Pi

1. **First reconcile this exact accepted TEST response** (`...-hop1`) and source-drift safety hold through existing CDPA read-only/authorized channels. Do not re-Send, reload, or replace its conversation on assumption of timeout. `9202` has an accepted request; do not use 9212/FJP.
2. Assign a **single bounded G6 DEV successor through Pi's canonical constructor only after exact ownership is clear**. At existing `run_pi.py`/authoritative CDPA control boundary, require a structured committed successor receipt **or** authoritative durable safety hold, correlated by exact parent/task/request, **before** a Pi cycle may transition to `NEXT_CYCLE`. A plain log entry or `PI_ACTION` text is insufficient. Do **not** create another queue/scheduler or duplicate the CDPA request ledger.
3. In a disposable offline **CDPA ledger fixture**, fault-inject pre-submit, submitted-but-unacknowledged, post-commit/pre-Pi-checkpoint and runner/child termination windows. Prove one and only one successor or durable hold, no loss of parent mission, single active owner, full first-turn policy and `attempts=1` for any accepted/ambiguous Send. Verify a later live controlled restart only after ledger/browsers can be safely reconciled.
4. Obtain required independent FJP M365 Opus and G6 Solo REVIEW for the eventual substantial fix; follow with relevant broader regression and real E2E. **Campaign release remains NOT_ACCEPTED.**

**Repository safety:** no product source, CDPA runtime, browsers, task control or accepted Send changed by this TEST. Two pre-existing modified product files were observed at intake; later `tests/test_cdpa_solo_task.py` and `INCIDENT_SOURCE_DRIFT_20261107.md` appeared from other concurrent activity; left untouched. The only task-owned artifact intended from this TEST is this Markdown report.
