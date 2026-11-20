# CDPA Self-Evolution DEV — Bootstrap / Exclusive-owner recovery (2026-11-08)

- Task: `g6-evolution-bootstrap-recovery-20261108-r1`
- Actual CDPA task ID: `cdpa-idem-d53a661e313b06a7b8a70b5d`
- Parent: `cdpa-idem-e46ff47af39e11cecf1a493a`
- Device: G6 `DESKTOP-B5E4IUE`, repo `E:\git-project\cdpa`, branch `rebuild/runtime-v2-r11`.
- Campaign authority: `workflows/self-evolution/GOAL_AND_RULES.md` (read in full at intake); `AGENTS.md`, HCUW and CDPA self-repair skills read.
- Scope: **DEV slice completed; full self-evolution campaign NOT_ACCEPTED**.

## Reproduced defects and repair

1. **Stale RUNNING checkpoint**. Archived checkpoint `.runtime/self-evolution/20261108-prebootstrap-runner-state.json` had iteration 344 / owner PID 32200 / child PID 35124, both dead, no transition, but `unreconciled_state()` retained it indefinitely. Added process-creation FILETIME validation (including reused PID and exited-but-queryable PID), and `reconcile_stale_running()` in the existing `run_pi.py` owner. Transitionless stale state is marked RECOVERED only if both processes are proven non-owning and CDPA campaign/task/command reconciliation is authoritative. Historical applied commands are allowed only when their exact created child is terminal with matching parent/team; missing, queued or mismatching commands hold. PAUSED/BLOCKED campaign tasks are not considered safe to replace. No new state store, queue, scheduler or supervisor.
2. **Windows checkpoint Access denied**. Existing atomic state replacement used one fixed `.json.tmp` draft and immediately called `os.replace`; runner log recorded `PI_OPERATOR_ERROR=PermissionError(13, 'Access is denied')` in cycles 324 and 344. Replacement now uses a unique fsynced draft and five bounded attempts across a Windows temporary share violation, cleans its draft after success/failure, and retains the existing `state_guard` and `single_owner` locks. A permanent denial fails closed without corrupting the previous checkpoint.
3. **Real atomic handoff stuck despite applied child**. Read-only operator state at iteration 345 carried the correct constructor hash and child ID, but the live CLI-normalized manifest goal lacks the constructor's trailing newline and its command projection reports role null. The old reconciler demanded the wrong hash and a non-null role. Reconciliation now checks actual persisted normalized goal against exact command hash, allows the one-LF legacy constructor hash, and verifies role via manifest start role / first hop if command role absent. The live Send receipt has accepted user identity but null `conversation_id`; it is accepted only when the exact persistent conversation URL, physical role, page ID, manifest binding and request receipt all match. Wrong binding still fails closed.

## Files touched by this DEV

- `workflows/self-evolution/run_pi.py` — minimal changes in existing coordinator checkpoint, PID and handoff owners, on top of prior dirty work (not exclusive ownership of prior diff).
- `tests/test_self_evolution_bootstrap_recovery.py` — added regressions for stale/live/reused PIDs, historical commands, PAUSED/BLOCKED ownership, fault injection for checkpoint replace and PID access.
- `tests/test_self_evolution_handoff_recovery.py` — two targeted regressions added to pre-existing untracked file (goal hash/role and exact owned page fallback).
- This report.

Preserved all unrelated dirty/untracked files, including `=` and all existing `src/playwright_auto/*` changes. No git reset/stash/clean, branch switching, push or runtime deployment. No child dispatch, browser restart, CDPA request resend or other irreversible browser action.

## Verification evidence

- Failing-first `tests/test_self_evolution_bootstrap_recovery.py`: **6 failed / 1 passed** before repair; later four additional Opus-derived failures, all subsequently green.
- Targeted handoff tests reproduced CLI normalization / missing role and missing conversation identity failures before code changes.
- Final complete repository self-evolution test family on G6:
  `$testFiles = @(Get-ChildItem -Path tests -Filter 'test_self_evolution_*.py' | ForEach-Object { $_.FullName }); .venv\Scripts\python.exe -m pytest -q $testFiles --tb=short`
  **50 passed in 5.08s**, exit code 0.
- `.venv\Scripts\python.exe -m compileall -q workflows\self-evolution\run_pi.py tests\test_self_evolution_bootstrap_recovery.py tests\test_self_evolution_handoff_recovery.py`: exit code 0.
- `git diff --check`: exit code 0 (line-ending warnings only).
- Exact child manifest: `.plan/g6-evolution-bootstrap-recovery-20261108-r1/cdpa-idem-d53a661e313b06a7b8a70b5d/cdpa-self-evolution-dev-g6-evolution-bootstrap-recovery-20261108-r1.json`. It retains parent/team identity, full canonical policy in `hops[0].prompt`, bounded instruction, canonical spawn command, mandatory browser recovery instructions, first-hop accepted user receipt, one attempt, and applied creation command.
- Read-only live `run_pi.reconcile_transition(read_state()['transition'])`: `('DISPATCH_VERIFIED', 'cdpa-idem-d53a661e313b06a7b8a70b5d')`. This proves current child creation only, **not task terminal success**. Live state was left intact as `INTENT_PENDING` with `dispatch_allowed=false`.

## Independent M365 Opus review (FJP)

- M365 doctor ready. Opus turn `533cec14-m365-worker-20261108-092033-3f6a691c` **completed**, result at `C:\Users\DuongNH66\AppData\Local\m365-copilot2dom\jobs\533cec14-m365-worker-20261108-092033-3f6a691c\response.md`.
- Review result: **REVISE** against the earlier patch. Opus falsified permanent hold on historical applied commands and unsafe PAUSED/BLOCKED classification, requested PID-access fault injection. Those points were repaired and independently retested locally on G6. Opus did not review the final delta after these corrections; do not claim final approval.
- Opus additionally noted pending-path exception handling/host-denied PID and persistent failed `os.replace` are still operational risks, and historical permanent hold exit requires recovery design. Treat as remaining acceptance gaps rather than interpreting passing unit tests as release approval.

## Exact remaining gates / next safe action

1. **Pi coordinator** alone should reconcile current `INTENT_PENDING` child through the existing authoritative ledger, retaining one owner and never rerunning its accepted create_task/Send. Once this DEV task has an exact owned assistant terminal route, move to fresh canonical G6 Solo REVIEW of these files and evidence. Any successor must reread `GOAL_AND_RULES.md`, carry full current goal, exact parent, bounded instruction and canonical spawn/recovery commands in the actual `--goal`.
2. Independent G6 Solo REVIEW **not performed** in this DEV task; no independent final approval from Opus after last delta.
3. Real long-lived Pi restart after crash, lock reacquisition and stale-state recovery, with exact child/parent continuation and no Send replay, **not performed**. Permanent Windows checkpoint denial still cannot durably write a fresh hold; retain previous fsynced state and explicit error; operator restart procedure needs separate E2E proof. The pre-loop `previous_pi_still_running` fail-closed exception may exit instead of a visible hold on denied PID and needs operational assessment.
4. Live CDPA runtime may be source-drifted by pre-existing dirty source changes; CDPA-only reload, post-reload identity/receipt and heartbeat evidence **not performed**. No Chrome restart.
5. End-to-end noninterference, 9202 allowed challenge/restart, comprehensive repository regression and complete independent release acceptance **not performed**. Campaign status stays **NOT_ACCEPTED**.

**Handoff:** DEV implementation/test slice ready for Pi reconciliation and independent review, no new dispatch authorized to this DEV.
