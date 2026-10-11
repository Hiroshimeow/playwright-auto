# MASTER GOAL — CDPA Send, conversation identity and safe recovery (P0.001.1 -> P0.001.2)

**Repository:** `E:\git-project\playwright-auto-multicdp` on g6.
**Baseline implementation:** `44f70d7`, branch `feat/cdpa-multi-cdp-temporary-recovery`.
**Authoritative scope:** `.hcu/specs/p0-001-conversation-identity/plan.md`; P1 evidence: `verification.md`.
**Required reading:** `AGENTS.md`, `.skills/solo-role/SKILL.md`, `.skills/solo-role/references/cdpa-solo-contract.md`, `.agents/skills/hcu-work/SKILL.md`, and the two P0.001 files.

## Global outcome
Run a **sequential, self-spawning CDPA Solo REVIEW/DEV/TEST/REVIEW chain** which independently accepts or repairs P0.001.1, then implements P0.001.2, verifies durable/browser recovery, and ends in a final independent REVIEW. Make a reliable browser-backed agent that can accept Send without HTTP 2xx, keep the right conversation/Project identity, process the response, and recover safely from Chrome/Cloudflare/session failures. Prefer mature existing techniques, KISS/DRY, evidence-based decisions, no extra state machines or API polling.

## Phase 1 — START HERE: independent REVIEW of P0.001.1
1. Independently inspect commit `44f70d7` against its parent, current working tree, current source, exact plan and acceptance matrix. Do not accept earlier assistant's conclusions as proof. `verification.md` reports 284 passed / 11 skipped, but independent review sign-off is **not** complete. Confirm current worker source, commit, tests and runtime health.
2. Review the real Send click/acceptance path: **any single fresh post-click DOM user message, newly visible Stop, or genuinely new ChatGPT assistant response** may mark `SENT`; stale Stop, prior assistant, generic MutationObserver and LISTEN staleness must not. HTTP 2xx should not be Send acceptance evidence. A stale Stop is **never** response-terminal proof. P002 5-second terminal stability remains independent.
3. Review exactly-once ledger semantics and `SendReceipt` when Stop/assistant arrives before `user_message_id`. Ensure manual user steering and DOM virtualization do not cause false blocks or wrong reply attribution; a click alone is not proof.
4. Review validated `conversation_id`, provisional `local-chatgpt` rejection, `/c/<UUID>` -> Project `/g/<project>/c/<same UUID>` redirect, preferential persistence of Project URL, preservation of known-good URL across challenge/root redirects, and refusal of a different canonical UUID. Verify 10-second URL-settlement grace does not block an active/valid response and does not alter P002 timing.
5. Run targeted and sufficiently broad deterministic tests; inspect mocked assertions for false passes. Perform a **safe browser E2E** against a disposable/headless context when possible, without disturbing any user tab. Record exact evidence in a durable review file under this spec directory.
6. If concrete P1 defects remain: spawn exactly one Solo **DEV P1 repair** child, requiring independent REVIEW again afterward. **Do not start P0.001.2 until P1 is independently accepted.** If P1 passes, spawn exactly one Solo **DEV P0.001.2** child.

## Phase 2 — P0.001.2 (only after accepted P1)
Implement only the accepted plan:
- On **positively detected** Cloudflare challenge, `PAUSE BROWSER_CHALLENGE` with precise operator recovery. Model-picker errors alone do not establish Cloudflare. Do not auto-kill/reload another browser or any temporary chat.
- On saved canonical conversation session unavailable, `PAUSE SESSION_UNAVAILABLE`, preserve receipt and URL; operator may Resume. No F5 loops or automatic New Chat.
- After operator restarts the correct persistent Chrome profile, reopen saved conversation using canonical ID (prefer observed Project URL), tolerate same-ID Project redirects and changed tab `page_id` after restart while still proving role/task/team binding; allow 30-second browser/page readiness.
- When confirmed challenge interrupts an already accepted generation **before any canonical ID/URL is saved**, retain the original durable `SENT` receipt and record an abandoned generation. Explicit operator Restart/Resume may authorize at most **ONE** fresh send on a new generation, durably counted across restarts with clear duplicate-risk warning; do not claim the first Send failed or silently rewrite its ledger. If canonical ID exists, reopen the original and do not resend.
- Preserve existing manual drafts/attachments, current response activity, temporary document lifecycle, valid terminal response and P002 stability. Do not add backend polling. Make response arrival/Retry/Stop remain reliable.
- Implement in the existing owners and extend deterministic tests; use a safe disposable E2E for the recovery boundary. No unsanctioned actual Chrome restarts, account changes or unrelated process termination.

## Phase 3 — Independent acceptance and closure
After P0.001.2 DEV commits only verified task-owned changes, spawn exactly one Solo REVIEW. If browser/runtime proof still needed, REVIEW spawns TEST, then independent closure REVIEW. Failures route back via one Solo DEV repair child, then review. Closure requires evidence of: no duplicate Send; no false identity BLOCK; usable PAUSE/Resume after challenge or missing session; once-only operator-authorized replay; correct Project URL; P002 terminal behavior; no unrelated browser/tab mutation.

## Permanent chain/safety rules
- **EVERY NON-FINAL SOLO AGENT MUST SPAWN EXACTLY ONE NEXT SOLO TASK BEFORE FINISHING, AND MUST CONTINUE THE SOLO REVIEW/DEV/TEST CHAIN UNTIL THE WORK IS INDEPENDENTLY VERIFIED COMPLETE. DO NOT USE TEAM TASKS OR ROUTE-WORKER HANDOFFS FOR THIS CHAIN.**
- Use `.skills/solo-role/scripts/spawn_solo_task.py` (idempotent). **After POST acceptance, poll the exact child task ID until it is materialized and visible on `GET /api/tasks/<id>` before parent DONE.** A transient 404 is not permission to spawn another child.
- Pass a self-contained handoff into each child: this master goal file, repository/path/current HEAD, parent task ID and report path, actually verified changes, exact remaining work, acceptance evidence, instructions and chain/materialization rules. Do not depend on chat memory or assume earlier notes are current.
- Next child endpoint: inspect **9212 and 9222** live capacity; pin exactly one available endpoint. Do not default to 9202 or fan out parallel children. Existing user browser tabs remain untouched.
- Commit narrowly when tests pass; preserve unrelated dirty `PROBLEM.md`, existing branches and other tasks. No push, force-clean, merge, deploy or modifying other repos. Do not start another CDPA worker, reset someone else's task, close Thorium, or alter browser endpoints as a side effect.
- Never claim success without evidence; for genuine inaccessible external prerequisites, PAUSE with an exact recovery condition instead of fabricating DONE or looping indefinitely. Release and merge readiness require independent REVIEW sign-off, not only pytest passing.

**Runtime handoff at chain creation (2026-10-11 JST):** operator paused and reset the lone stale RUNNING independent Maintainers job; old BLOCKED tasks preserved. Worker was restarted without closing Chrome; `/health` was `HEALTHY`, `reload_required=false`, worker PID `41216`. First Solo REVIEW must recheck present truth rather than trust these snapshots.
