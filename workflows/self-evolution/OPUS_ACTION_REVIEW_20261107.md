# M365 Opus independent action review — 2026-11-07

Source: FJP M365 Worker exact turn `1b197046-m365-worker-20261107-035540-16e0e890`, state COMPLETED. Raw response at `C:\Users\DuongNH66\AppData\Local\m365-copilot2dom\jobs\1b197046-m365-worker-20261107-035540-16e0e890\response.md` (8,064 chars). It reviewed provided source excerpts and evidence; it did not execute G6 tests.

## Recommendation

**REVISE**, campaign still NOT_ACCEPTED. The single first fix is in the existing `workflows/self-evolution/run_pi.py`: verify whether a Pi cycle made actual CDPA task progress, rather than counting a checkpoint-only cycle as success. No new ledger, service, or direct Send owner. The current G6 DEV task `cdpa-idem-53d3861aacccfb51da8420be` has this same scope; reconcile its output, do not spawn a competing DEV while in flight.

- Observed: `coordinator_prompt` tells Pi to return checkpoint after one safe cycle; cycles 1-3 completed without dispatch when 9202 had no campaign writer. `--no-session` forgets earlier cycles. The runner currently verifies log growth and process exit, not accepted CDPA progress.
- Proposed simplest fix: obtain a read-only campaign task inventory before each Pi cycle and give it to the Pi prompt. Require one explicit outcome: `WAIT task_id`, `DISPATCH task_id`, `RECOVER action`, `HOLD blocker/evidence`. Validate against CDPA's existing /api/tasks; classify checkpoint-only cycles NO_PROGRESS. Persist consecutive NO_PROGRESS in the already-existing pi-runner.json and explicitly warn/focus subsequent cycle. Never fake progress on a terminal task.
- Only reconcile nonterminal campaign tasks when deciding dispatch. Old unrelated task errors are historical evidence, not a blanket block on new campaign task creation.
- Crash recovery: reconcile the existing CDPA task ledger upon restart. If an accepted task already exists, WAIT it; do not dispatch a duplicate. Confirm deterministic team and exact request ownership, not only process PID. No second transaction ledger.
- HYPOTHESES requiring evidence: process PID reuse may create false safety hold; check CreationDate and runner startup; CDPA idempotency keys may depend on payload text; verify exact implementation rather than assuming.

## Tests requested by Opus

1. No active campaign task + checkpoint-only Pi output => NO_PROGRESS.
2. WAIT for already terminal task => NO_PROGRESS.
3. DISPATCH exact task ID found in existing CDPA ledger => PROGRESS.
4. Two NO_PROGRESS cycles => next Pi prompt explicitly includes stuck reason and a bounded next action.
5. Simulate crash after accepted dispatch but before Pi outputs outcome; recovery must reconcile the same task with one Send attempt.
6. Live G6: after bounded DEV completes, Pi dispatches next CDPA REVIEW, survives Pi cycle crash, no duplicate accepted Send and no 9212/FJP interference.

## Existing blocked tasks

- `preboundary_acquisition_recovery_required`: only when evidence proves no Send accepted, replace with a unique scoped task rather than blindly Resume.
- `CONVERSATION_IDENTITY_MISMATCH` and `response_terminal_missing`: reconcile accepted/ambiguous requests from ledger; never re-Send them. Reassign remaining unfinished work as a distinct new task only after exact ownership safety.
- Do not auto-retry all historical BLOCKED tasks.

Opus notes that the G6 DEV `53d3861...` was already in-flight when reviewed. It explicitly recommends waiting for that task's terminal evidence before applying a competing change or performing a real Pi cutover. 
