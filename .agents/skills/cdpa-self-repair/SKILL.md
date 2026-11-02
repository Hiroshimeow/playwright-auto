---
name: cdpa-self-repair
description: Guide G6 CDPA Solo workers and Pi coordinator through one small code repair, verified handoff, automatic continuation, and safe blocked-task recovery.
---

# CDPA Self Repair on G6

## Startup (mandatory once per new task)
1. Confirm host DESKTOP-B5E4IUE, repo E:\git-project\cdpa and branch rebuild/runtime-v2-r11. Never use FJP.
2. Read AGENTS.md, .agents/skills/hcu-work/SKILL.md, .agents/rules/project.md, docs/REBUILD_CONTRACT.md, docs/CURRENT_VERIFIED_STATE.md and specs/003-team-only-dom-runtime/tasks.md. Run hcuw fingerprint .; reconcile drift via hcu-rules if needed.
3. Read workflows/self-repair/GOAL_CHECKLIST.md, REPAIR_HISTORY.md, MISSION.md, PROGRESS.md and current task slice in workflows/self-repair/slices.json. For any new defect, search REPAIR_HISTORY for the same symptom/root cause before editing; compare exact source, Git diff, earlier test and latest runtime receipts. Append a dated repair entry after a verified fix.
4. Check CDPA /health, /api/tasks, pinned browser endpoint and exact conversation ownership. Never interpret queued as accepted Send.

## Mandatory model gate for EVERY newly spawned Solo
- Target ChatGPT reasoning effort HIGH or EXTRA HIGH (prefer EXTRA HIGH when available). Before any new Send, inspect the actual selected model/effort in the owned browser session, choose an allowed level if needed, then independently verify the displayed choice. Do this BEFORE filling the composer.
- Do not treat `--thinking high`, a model name in the prompt, or a default profile as proof that the ChatGPT UI is in High/Extra High. If the UI cannot prove an allowed effort, fail closed before Send and report `model_verification_blocked` with evidence. Existing accepted/in-flight Send must never be replayed just to change the model.
- After runtime reload, reconcile task state, conversation identity, Send ledger and model evidence before any next task dispatch. Model check applies separately to every new task, not only at campaign startup.
- **Independent model evidence:** `/api/tasks` may not expose selected effort. Read `.runtime/action-events.jsonl`, filter `action=model_gate`, `phase=verified` and the **exact task_id**; verify High or Extra High occurred BEFORE the same task's `send:click` and that task/team/session ownership is unchanged. Read the live *owned* tab's visible `Select ChatGPT model` button when independent UI evidence is required. Never infer model from other tabs or from unit-test events with null task ID. For slice 08 also examine `.runtime/self-repair/08-real-browser-model-attestation.json` and verify it against raw events rather than treating it alone as proof.

## Work contract
- Implement one bounded slice only. No broad rewrite, reset, stash, clean, branch switch, unrelated changes or touching another computer.
- Reuse one CDPA command ledger, provider controller, state store and scheduler. Never duplicate Send, F5 Temporary Chat, steal focus or mutate non-owned tabs.
- Write a JSON receipt at workflows/self-repair/receipts/<slice-id>.json, containing slice_id, task_id, status PASS or BLOCKED, git_before, git_after, changed_files, tests, evidence and next_action.
- Tests must actually run. An unverified change is BLOCKED, not PASS. Do not falsely mark full release accepted.
- **Multi-Send test runtime:** `_run_automated_send` deliberately spaces Send operations by 10 seconds. Four Send calls in one test legitimately take 30+ seconds. Use a retained process and read its terminal exit code, not a short (28-second) shell execution; a tool timeout is UNKNOWN, **not** proof of deadlock. Never remove Send spacing or weaken exactly-once safety to make a test fast.
- A local Pi supervisor owns all scheduling and spawning. Do not spawn another task yourself.
- On blocking ambiguity (lost-ack Send, identity mismatch, auth or challenge) do not blindly retry. Record exact evidence and leave safe recovery to Pi.

## Recovery precedence — explicit G6 operator directive (2026-10-27)
- **Top priority: reload G6 CDPA runtime** when it is stale, has source drift, or a task hangs. Stop the affected Solo via the exact CDPA API if it responds, then reload the CDPA API/dashboard/worker immediately without waiting for ChatGPT tabs. Browser profiles on 9202/9212 are independent; do not restart them as part of a CDPA runtime reload.
- After restart, read `.runtime/self-repair/state.json`, `PROGRESS.md`, every relevant task receipt and the command ledger. Keep accepted Send provenance; never resend the previous turn just because a worker restarted.
- Re-run health/worker/endpoint checks. Resume an eligible same task only when safe; otherwise treat the stopped task as a historical code-only result if receipt-backed, create **one fresh Solo for the next slice** and make it read this skill before working. Release stays NOT_ACCEPTED until real CDP proof.
- If a Solo edits runtime source and creates SOURCE_DRIFT, reload again on G6 before dispatching another Solo. Do not wait two hours for a hanging UI; promptly recover the controller, with minimal changes and without overengineering.
- **Two separate duties, one task owner:** Windows task `CDPA-Pi-SelfRepair-G6` alone schedules/spawns Solo tasks every minute. Windows task `CDPA-G6-HealthWatchdog` checks only `/health` for API/frontend/worker/source drift every minute, and reloads via `workflows/self-repair/reload_cdpa.ps1` even if a long Pi coordinator call holds the Solo supervisor lock. The reload script uses a single exclusive reload.lock; neither watchdog nor Solo tasks are allowed to restart Chrome or replay an accepted Send. Use `Get-ScheduledTask` and `Get-ScheduledTaskInfo` to verify both scheduled jobs are Ready and their last results are 0.

## Independent M365 Opus review for medium/large defects (advisory, not a new gate)

- Fix small independently reproduced defects directly on G6 with failing-first tests; do not delay for a routine review. For medium/large structural or repeated incidents, use M365 Opus on FJP **for review only**; G6 remains the sole code/task writer.
- **Before Round 1:** give Opus a concrete evidence packet, not a short status summary. Include task ID/team/phase, G6 commit/branch/dirty diff, exact relevant source code and call chain, failing test(s) with actual failure/exit code, observed vs expected DOM and ledger events, previous fixes in REPAIR_HISTORY, release constraints, competing hypotheses, and redacted paths/links to further evidence. Never send secrets, passwords, cookies or tokens.
- Require Opus to separate `OBSERVED` from `HYPOTHESIS`, identify one smallest existing code owner, the falsifiable test, regression risk and remaining unknowns. **If evidence is insufficient, Opus must explicitly return `NEED_EVIDENCE` with specific questions / filenames / lines / log intervals; it must not guess or force a recommendation.**
- **Debate:** verify Opus's claims against the G6 repo and run targeted tests. Send its unanswered questions and concrete counterexamples back in Round 2 (or later), preserving the same issue and full previous decision trail. Continue only while material evidence or design disagreement remains. End after one round only if evidence is complete and both sides already converge; otherwise require explicit agreement or an honest unresolved conclusion. No automatic adoption of a lone Opus response, additional auth/Allow gates or duplicate control layers.
- Store dated round summaries, `NEED_EVIDENCE` requests, agreed owner/fix/tests, rejected alternatives and G6 verification in REPAIR_HISTORY (and FJP M365 prompt/response files). Apply/verify the agreed minimal fix on G6 through existing controller only, then reload if worker source changed and check real task/accepted-Send ledgers.

## Final response
Conclude with exact receipt location, changed files, test evidence and route DONE only when accepted. Keep the conversation short; the next task always rereads this skill.

## G6 provider_challenge recovery (2026-10-27)
- Only G6 may recover browsers. 9202 is HCU profile (E:\mauto_playwright\profiles\hcu_chatgpt). 9212 is a distinct Susan profile. Never move an accepted Send between accounts.
- If Solo is BLOCKED by provider_challenge, inspect the accepted-Send ledger first; never blindly resend, reload Temporary Chat, or bypass provider protection.
- Before restarting 9202, save its normal tab URLs and any unsent composer drafts under .runtime/self-repair. Check active ownership and Temporary Chat; if ambiguous, stop for reconciliation. Pause Pi Windows scheduler.
- Close only the owned 9202 Thorium browser gracefully with CDP Browser.close. Relaunch via C:\Users\admin\Desktop\9202_PLUSHCU.bat. Confirm a new ChatGPT tab has a visible composer; an initial HTTP 400 can be transient.
- Restore saved normal URLs as background tabs, without F5 or Send. Preserve draft backup. Validate 9212 separately, using its own profile and endpoint.
- Re-enable scheduler and record restart attempts. The previous Solo remains BLOCKED; use a uniquely identified new recovery Solo only after proving no competing writer. Apply bounded retries, not an infinite restart loop.
