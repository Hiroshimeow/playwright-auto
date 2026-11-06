# DEV Pi action correction — g6-evolution-action-fix-20261107-r1

Date: 2026-11-07. G6 DESKTOP-B5E4IUE, E:\git-project\cdpa, branch rebuild/runtime-v2-r11. Task cdpa-idem-53d3861aacccfb51da8420be; parent cdpa-idem-0270fec2f31e41ce810e4d5b.

**Bounded implementation: unit-verified. Live dispatch/crash-handoff acceptance: NOT_ACCEPTED.** This report does not authorize concluding the whole campaign.

## Evidence and root cause

- Mandatory GOAL_AND_RULES.md and AGENTS.md read before source action. Read HCUW, project rules, LEARNING.md, self-repair skill, core/spawn/runner and review receipts.
- Pi cycles 1-4 (and cycle 5) in `.runtime/self-evolution/pi-runner.log` produced health/task inventory and checkpoint, with zero dispatch. `run_pi.py::coordinator_prompt()` explicitly instructed Pi to "return an operational checkpoint" after a bounded safe cycle, but did not demand any evidence-backed operation, dispatch, or specific safety hold.
- `/api/tasks` showed current DEV `cdpa-idem-53d3861aacccfb51da8420be` RUNNING on requested endpoint `4bc12276-da9c-4a7a-8ef9-ddcfcca6113f` (9202). A new live dispatch during this DEV execution would create a competing owner. Historical PAUSED tasks and accepted Sends remain distinct from an active writer and do not by themselves prevent a fresh isolated task *after* ownership is released.
- Read-only live Pi runner `pi-runner.json` after cycle 5: `NEXT_CYCLE`, owner PID 25456, exit 0; `/health` ready/HEALTHY. API health is NOT release evidence. Existing M365 FJP Opus advisory is recorded in `OPUS_REVIEW_20261107.md` (prior design; no new response for this patch).

## Focused modifications (only G6)

1. `workflows/self-evolution/run_pi.py`: replaced the permissive cycle checkpoint prompt with an explicit one-action decision: reconcile exact live owner/accepted-or-ambiguous Send; when no active owner and no unreconciled prior submit, select highest-value incomplete gate and dispatch **exactly one** bounded Solo using the existing `spawn_cdpa.py` canonical constructor. Inspect actual resulting first-turn/receipt. Otherwise perform an evidence-backed observation/recovery or document a specific safety hold and falsifiable next action. A timeout/ambiguous submit MUST reconcile same team before any retry. Existing Pi/CDPA owner mechanisms remain unchanged: no new state, scheduler, store or Send path.
2. `tests/test_self_evolution_runner.py`: new failing-first regression asserting the actual Pi invocation includes the full canonical policy, explicit single-dispatch decision, historical PAUSED distinction, safe hold, and 9202/9212/FJP constraints.
3. `.agents/skills/cdpa-self-repair/SKILL.md`: replaced the single contradictory instruction to resurrect the two obsolete Windows scheduled tasks with self-evolution campaign-specific long-lived single-owner Pi guidance. Other skill guidance unchanged.

## Actual execution and verification

- Red: `.venv\Scripts\python.exe -m pytest -q tests\test_self_evolution_runner.py -k pi_operator_must_act_or_record_exact_safety_hold` -> **FAIL** due to missing `dispatch exactly one` (exit 1).
- Green: same command -> **1 passed**, exit 0.
- Focused + surrounding regression: `.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_core.py tests/test_self_evolution_propagation.py tests/test_self_evolution_runner.py tests/test_cdpa_repair.py tests/test_cdpa_solo_task.py` -> **58 passed**, exit 0 (includes disposable parent crash, child/grandchild cleanup, lock/restart safety, and 50-generation `spawn_cdpa.command` to real `PromptBuilder.build_solo` propagation).
- `.venv\Scripts\python.exe -m py_compile workflows\self-evolution\run_pi.py workflows\self-evolution\spawn_cdpa.py` -> exit 0.
- `.venv\Scripts\python.exe workflows\self-evolution\spawn_cdpa.py --role REVIEW --team g6-evolution-action-next-dryrun --instruction "Verify safe 9202 handoff after active DEV terminal" --parent cdpa-idem-53d3861aacccfb51da8420be --dry-run` -> READY, embedded_contract_bytes=9481, exit 0; no task submitted.
- `git diff --check` -> exit 0; changed campaign files are currently untracked, so tracked Git diff does not attest their patch contents. No unrelated dirty files touched, no code commit/push.
- NO forced live Pi crash, NO real new task dispatch, NO real runtime reload, NO browser Send/Restart/F5/Resume/Stop/Clear executed. Whole repository pytest and terminal-route E2E NOT run/proven. A prompt-only behavioral contract is not a deterministic dispatch guarantee.

## Exact next Pi/acceptance action (one successor, not a storm)

1. Wait for **this exact DEV** `cdpa-idem-53d3861aacccfb51da8420be` to reach an owned assistant terminal route and for CDPA to release 9202. Verify `requests.json` exact accepted attempt remains 1 and reconcile any outstanding ambiguous submit. While this DEV is RUNNING, do NOT dispatch another 9202 Solo.
2. Inspect next Pi cycle log and API tasks. If no active 9202 owner, Pi dispatches **one** uniquely named `REVIEW` successor using the canonical `spawn_cdpa.py --role REVIEW --team <fresh-unique> --instruction "<bounded independent review of action prompt, first-turn evidence and safe dispatch>" --parent cdpa-idem-53d3861aacccfb51da8420be`; capture exact child task_id, first-turn hops[0].prompt, receipt, request ledger, and ownership. If runner instead records another read-only checkpoint, the operational fix is still NOT_ACCEPTED; diagnose whether the actual Pi worker loaded the new run_pi prompt and why it refused a dispatch.
3. After that live acceptance, hand off one bounded DEV/TEST successor at a time, ordered by evidence: `CONVERSATION_IDENTITY_MISMATCH` => prove durable conversation identity before reconciliation; `response_terminal_missing` => separate provider silence from DOM/terminal parser and verify exact assistant; `preboundary_acquisition_recovery_required` => prove prior boundary and donor/session provenance before any fresh acquisition; PAUSED/BLOCKED => reconcile exact accepted/ambiguous Send then use safe CDPA Resume if eligible, never re-Send; runtime drift/reload => CDPA-only worker/API restart with task and ledger retained, then genuine E2E continuation. Follow with simulated crash at handoff boundary, atomic successor-or-safe-hold proof, 9212/FJP noninterference, broad regression, independent G6 REVIEW plus fresh FJP Opus challenge.
4. Campaign release remains NOT_ACCEPTED until all mandatory gates independently pass. The current slice is limited to prompt/skill and local tests; it cannot claim working autonomous dispatch without the next live Pi cycle.

## Safety disposition

Reason for bounded PAUSE rather than DONE: active task owns G6 9202 and true live dispatch would compete; the required real post-handoff task dispatch, forced handoff crash and broad E2E are unproven. Pi must continue after this DEV terminal and reconcile exactly one successor or a documented safety hold. Never replay any accepted/ambiguous Send.
