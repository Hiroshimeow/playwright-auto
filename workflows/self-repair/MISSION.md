# CDPA G6 Self-Repair Campaign

Owner: Pi Coding Agent (cliproxy-g8/gpt-5.6-luna, high); all work on DESKTOP-B5E4IUE, E:\git-project\cdpa. CDPA Solo DEV workers are subordinate execution tasks. Pi schedules and recovers them; only one task may mutate a given ChatGPT conversation at a time.

## Objective
Complete R11 Gate C (T074-T099), pass independent focused and broad regression, verify exactly-once ChatGPT CDP operations, publish evidence-based release verdict and keep 9224/9225 worker healthy. The running G6 runtime is development-only until every gate in docs/REBUILD_CONTRACT.md passes.

Before each new check or Solo dispatch, read `workflows/self-repair/GOAL_CHECKLIST.md` **and `workflows/self-repair/REPAIR_HISTORY.md`** to compare symptoms, known root fixes, regressions and remaining evidence before editing. After every actual fix append a dated, source/test-backed repair entry; update only independently proven checklist boxes. Do not declare DONE or release PASS while any acceptance gate is open.

## Existing evidence
G6 branch rebuild/runtime-v2-r11; 2026-10-25 clean Gate B baseline 1927 passed, 45 skipped. Current Gate C uncommitted changes, 22 focused tests passing. Registered ChatGPT browser 9202 endpoint; 9212 may contain Temporary Chat, do not reload it. The repo is dirty; preserve unrelated changes and user tabs.

## Scheduling and recovery
Supervisor single writer; one active CDPA Solo per campaign. Each task gets immutable slice id, unique team, deterministic idempotency key. Before creation: check API health, worker heartbeat, endpoint and all existing campaign tasks. After creation: reconcile command id to task id and require actual accepted send evidence before saying RUNNING. After terminal DONE: validate receipt against task id plus tests and git diff, then select next smallest slice. BLOCKED, PAUSED or prolonged WAIT -> diagnose with Pi; no blind Send/Resume. If current task is safely inactive, create a scoped recovery Solo; if stale or ambiguous writer ownership remains, Pi repairs locally or stops for explicit reconciliation. Do not invent progress, create duplicate child, or silently bypass a release gate.

## Required progress records
Runtime authoritative: .runtime/self-repair/state.json and incident journal; shared agent-facing projection: workflows/self-repair/PROGRESS.md. Agent-specific receipts: workflows/self-repair/receipts/<slice>.json. New task reads skill, MISSION and PROGRESS every time.

## Verified incident: duplicated ChatGPT main (2026-10-27)
The exact G6 Solo 05 DOM contained two `<main>` surfaces: an offscreen 0x0 stale page followed by the live 1868x911 conversation. `document.querySelector('main')` selected the stale shell and hid terminal assistant text. The existing `chatgpt.py` DOM owner and sparse probe now share the visible positive-size main, also used by response projection/Retry; `test_duplicate_main_uses_active_conversation_for_full_and_wait_projection` was failing-first and passes. 138 focused/integration tests passed, compileall and diff check passed; worker was reloaded from source, preserving Chrome. Never treat an empty assistant shell as DONE. This is a code-fix receipt, not proof that the real browser release gate is accepted.

## Operator override 2026-10-27: RELOAD CDPA FIRST
If CDPA runtime becomes stale, `SOURCE_DRIFT`, deadlocked, or its Solo turn cannot finish, **the highest operational priority is to STOP that CDPA task and RELOAD the G6 CDPA API/dashboard/worker immediately**. Do not wait for ChatGPT's tab to finish. The browser is independent; a CDPA reload must not restart Thorium 9202/9212. Record the exact task/accepted-Send ledger, issue one idempotent official Stop where the API accepts it, and restart only the CDPA process tree through its existing launcher. A worker that cannot process Stop must not prevent its own reload. After startup verify `/health` ready, `SOURCE_DRIFT=false`, endpoint 9202 connected, then reconcile the retained task and receipts and spawn the next smallest fresh Solo. **Never resend an old accepted Send** or invent a DONE receipt. If a reload fails, repeat bounded diagnostics/reload rather than waiting on the ChatGPT UI. Do not modify unrelated source or create new state stores.

## Release acceptance
Compile and focus tests, real browser acceptance, full regression with clean teardown, no duplicate Send on lost ack, no F5 Temporary, no focus stealing, correct identity and session lease, no stale DOM arbitration, stable /health worker heartbeat. If not proven, verdict remains NOT_ACCEPTED.

## G6 provider_challenge recovery (2026-10-27)
- Only G6 may recover browsers. 9202 is HCU profile (E:\mauto_playwright\profiles\hcu_chatgpt). 9212 is a distinct Susan profile. Never move an accepted Send between accounts.
- If Solo is BLOCKED by provider_challenge, inspect the accepted-Send ledger first; never blindly resend, reload Temporary Chat, or bypass provider protection.
- Before restarting 9202, save its normal tab URLs and any unsent composer drafts under .runtime/self-repair. Check active ownership and Temporary Chat; if ambiguous, stop for reconciliation. Pause Pi Windows scheduler.
- Close only the owned 9202 Thorium browser gracefully with CDP Browser.close. Relaunch via C:\Users\admin\Desktop\9202_PLUSHCU.bat. Confirm a new ChatGPT tab has a visible composer; an initial HTTP 400 can be transient.
- Restore saved normal URLs as background tabs, without F5 or Send. Preserve draft backup. Validate 9212 separately, using its own profile and endpoint.
- Re-enable scheduler and record restart attempts. The previous Solo remains BLOCKED; use a uniquely identified new recovery Solo only after proving no competing writer. Apply bounded retries, not an infinite restart loop.
