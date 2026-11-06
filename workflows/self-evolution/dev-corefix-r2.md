# G6 DEV bounded core fix — g6-evolution-corefix-20261107-r2

Date 2026-11-07. Task `cdpa-idem-40233f9939ea9672f79ccfe4`; parent `cdpa-idem-3205a33826a7fe89ccac9a92`. Owner G6 `DESKTOP-B5E4IUE`, repository `E:\git-project\cdpa`, branch `rebuild/runtime-v2-r11`.

**Bounded fix: TESTED. Entire CDPA self-evolution release: NOT_ACCEPTED.**

## Intake and source ownership

Read complete `workflows/self-evolution/GOAL_AND_RULES.md`, `AGENTS.md`, project HCU profile/rules, relevant skills, `PROMPT_ROOTFIX_20261107.md`, `OPUS_REVIEW_20261107.md` and G6 REVIEW r3. Checked actual manifest `.plan/g6-evolution-corefix-20261107-r2/cdpa-idem-40233f9939ea9672f79ccfe4/cdpa-self-evolution-dev-g6-evolution-corefix-20261107-r2.json`: full canonical policy appears under real initial `goal`, in `hops[0].prompt`, along with parent and bounded DEV instruction. Receipt shows one accepted Send (`...-hop1`, attempts=1, user_message_id `af1d56bc-2962-4587-97e9-d70add0a4192`). No separate browser Send, task Resume, Chrome restart or runtime reload was performed.

The working tree had extensive pre-existing dirty/untracked product files. They were preserved. This slice edits only the self-evolution operator code and targeted tests listed below; none of the core CDPA product/source Send paths were changed.

## Root cause and minimum changes

1. `workflows/self-evolution/core.py`: removed unused duplicate `core.sqlite3` queue, `connect/enqueue/claim/handoff/events`, and misleading monitor/CLI loop. Production Pi never consumed this state; CDPA's existing manifest/request ledger remains sole authoritative task/Send owner. Retained canonical full-policy loading, bounded prompt assembly and read-only health helper. Added exact absolute canonical policy path in generated Solo prompt, without changing `spawn_cdpa.py`'s already-correct `--goal` propagation. Old `.runtime/self-evolution/core.sqlite3` was not deleted or migrated (historical evidence, no active consumer).
2. `workflows/self-evolution/run_pi.py`: retained single-instance Windows byte-range owner lock; corrected lock-first ordering. Bound the owner to a native Windows job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so a hard parent crash reaps descendants. Each Pi cycle now runs in a small job-owning Python child that reaps its Pi and grandchildren when that cycle exits or fails. `run_cycle` handles every exit/exception with `finally` cleanup. Added restart reconciliation: if prior `RUNNING` Pi PID may still be alive, startup fails closed with `SAFETY_HOLD` instead of dispatching a competing operator. No new scheduler, ledger, queue, database, or product-code interface introduced.
3. `tests/test_self_evolution_core.py`, `tests/test_self_evolution_propagation.py`, `tests/test_self_evolution_runner.py`: replaced SQLite-mock 50-generation proof with 50 generations of the actual `spawn_cdpa.command` → `PromptBuilder.build_solo` first-turn path. Added Windows disposable-process crash/singleton/restart/orphan/exception tests. Tests only run inert Python subprocesses and never invoke Pi with a browser or CDPA Send.

## Verification performed on G6

- Baseline: initial targeted pytest **6 failed, 5 passed**, exposing the prior missing invariants.
- After repair: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_core.py tests/test_self_evolution_propagation.py tests/test_self_evolution_runner.py tests/test_cdpa_repair.py tests/test_cdpa_solo_task.py` → **57 passed**, exit 0. Includes Windows process-parent crash, grandchild lifecycle, exclusive lock, simulated state-write exception and legacy Pi PID hold.
- `.venv\Scripts\python.exe -m py_compile workflows/self-evolution/core.py workflows/self-evolution/run_pi.py workflows/self-evolution/spawn_cdpa.py` → exit 0.
- `.venv\Scripts\python.exe workflows/self-evolution/spawn_cdpa.py --role REVIEW --team evolution-dryrun-verify --instruction "verify generation prompt" --parent cdpa-idem-40233f9939ea9672f79ccfe4 --dry-run` → `READY ... embedded_contract_bytes=9086`. No task created.
- `git diff --check` → exit 0, only unrelated existing LF→CRLF warnings. Note: self-evolution directory and tests are currently **untracked**, so Git's tracked diff alone does not show their contents. Actual source/byte contents were read/written directly; do not mistake this for a committed patch.
- No real Pi owner process kill, task dispatch, live browser E2E, full repository regression or release restart was carried out as part of this bounded DEV fix.

## Single-instance launch and safe restart

Existing authorized launch: `workflows\self-evolution\start-on-login.cmd` (one user logon launcher) or `.venv\Scripts\pythonw.exe workflows\self-evolution\run_pi.py` once from G6. Do not run a second launcher if a previous owner exists. Existing `.runtime\self-evolution\pi-runner.lock` owns exclusivity; `pi-runner.json` exposes status/PIDs; `pi-runner.log` retains cycle output.

For cutover from the **old running runner**, first inspect its exact PID, current Pi cycle, task ledger and accepted Sends. Stop the *known old Pi coordinator owner* only during an authorized quiescent cycle, then verify its child/descendants exited; never terminate CDPA browser/worker or use an indiscriminate `taskkill`. If prior Pi remains alive, the new runner refuses launch as a safety hold. After reconciliation, start the existing launcher once and verify an advancing heartbeat and one owner. Do not infer successful deployment from these unit tests.

## Remaining risks / Pi handoff

- Current canonical policy prose still says embed into `--task`; this conflicts with verified `--goal` CLI semantics. Its correction is a separate narrowly owned follow-up (immutability/authority review), not part of this core-fix slice. Constructor's actual behavior remains correct.
- Runner startup assumes Windows job assignment is permitted for the deployed process tree; if Windows refuses nested assignment or PID evidence cannot be reconciled, it fails closed rather than dispatching.
- Long-lived runner *was not hot-reloaded* because that would require live owner/Send reconciliation and an authorized cutover. No claims of new code currently operating in the old runner.
- Successor should obtain independent G6 CDPA REVIEW of this exact patch and source, verify FJP Opus completion/evidence, then perform controlled Pi ownership cutover, live G6 task continuation, restart, Send-idempotency, noninterference and broad regression gates. Full campaign remains **NOT_ACCEPTED**.

**Result:** bounded DEV code fix and targeted regression complete; release remains unaccepted, and active Pi operational ownership must be reconciled before cutover.