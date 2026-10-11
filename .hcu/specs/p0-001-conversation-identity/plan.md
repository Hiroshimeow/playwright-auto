# P0.001.1 -> P0.001.2: Send evidence, conversation URL, browser recovery

> PLAN ONLY | 2026-10-11 | base HEAD `7f82805` (`feat/cdpa-multi-cdp-temporary-recovery`)
> Execute sequentially: finish, test, independently review and checkpoint P0.001.1 before starting P0.001.2.

## Outcome

CDPA should accept an actual post-click Send without depending on HTTP 2xx; keep one recoverable conversation identity across `/c/<uuid>` -> `/g/<project>/c/<same-uuid>` redirects; never replace a good saved URL with `/c/local-chatgpt:*` or a challenge URL. Cloudflare/session faults have short, explicit operator recovery, not a branching identity state machine.

## Verified baseline and non-goals

- The current code captures pre-click baseline in `chatgpt.py:send`, waits for Send acceptance in `_wait_send_acceptance`, and persists durable `SENDING -> SENT` through `durable_blocks.py`.
- The HTTP `response` listener around `POST /backend-api/f/conversation` is passive, not an API polling loop. HTTP 2xx is removed as acceptance authority per operator request; **do not claim this explains rate limits** or remove unrelated stream observation as part of this change.
- The last session left **uncommitted, partial** changes in `src/playwright_auto/chatgpt.py` and `tests/test_chatgpt_recovery.py`; 17 focused acceptance tests passed, broad integration tests did not finish reliably. Treat this diff as a candidate, NOT completed production behavior.
- `PROBLEM.md` was dirty before these changes: preserve it; never stage or overwrite it as part of this work.
- Current `AGENTS.md` / `.hcu/profile.md` prefer exactly-once Send, small ownership boundaries, no duplicate states. See the deliberately scoped operator Cloudflare exception in P0.001.2 below.
- No new database, durable state machine, graph service, global tab scan, background backend polling, Cloudflare bypass, or auto-killing browser processes.

## P0.001.1 — Confirm Send and persist the right conversation

### A. Send accepted when ANY one **new post-click DOM** signal appears

1. A new user message absent from the pre-click baseline, preferably matching the automated prompt.
2. Stop button newly appears **after** the click (rising edge); an old Stop already present is never a new-send signal.
3. New assistant response/thinking/streaming associated with post-click activity (new assistant turn/message or actual response progress); generic MutationObserver wake, prior assistant text, stale LISTEN status or sidebar changes are insufficient.

These signals prove *Send acceptance*, not *response completion*. Keep P002 terminal validation and response stability unchanged. A stuck Stop button must not prevent terminal admission after valid stable output. Do not use HTTP 2xx for Send acceptance. Use the existing MutationObserver to wake a bounded DOM probe, not as acceptance authority.

**Important compatibility repair:** `SendReceipt` already supports `accepted_via` values `stop_button`, `assistant_message`, `response_activity_turn`, and `assistant_activity`, but these may have no `user_message_id` yet. Ensure `DurableSendBlock`, `_owned_or_block`, `_discover_accepted_conversation_identity`, and `role_runtime/response.py` can proceed via the fresh assistant/DOM evidence without immediately treating missing user ID as foreign ownership. Never invent a user ID and never consume an assistant turn that predates the click.

Manual prompt injections during an active response are legitimate steering, not conversation conflict. Preserve `allow_user_steering=True` behavior and its original immutable Send receipt. No `page_id` or original user message recheck on every response-sample solely to keep a saved URL alive.

### B. One canonical conversation ID, Project URL only as preferred navigation

Read canonical UUID from a validated ChatGPT URL: `https://chatgpt.com/c/<uuid>` or `https://chatgpt.com/g/<project>/c/<uuid>`. Reject `/c/local-chatgpt:*`, `WEB:*`, root/challenge/auth URLs, URLs with `temporary-chat=true`, malformed hosts/paths/IDs. For a Temporary workflow, do not persist a saved reopen URL.

- Store existing `receipt.conversation_id` and reuse `hop.conversation_url` / `role.page_url`; do not introduce another competing identity store.
- `/c/<uuid>` can redirect automatically to `/g/<project>/c/<same-uuid>`: this is the **same conversation**, not conflict, tab hijack or a new generation. Prefer the observed Project URL; otherwise use the canonical `/c/<uuid>` fallback.
- Canonicalizing a receipt must not overwrite an already validated Project URL with a synthetic `/c/<uuid>` URL.
- Never overwrite a valid saved URL with provisional `local-chatgpt`, a Cloudflare URL, or an unrelated URL observed during navigation.
- A truly different canonical UUID is not a prompt-injection symptom: preserve the previous owner and surface the navigation conflict clearly. Avoid silently adopting another conversation.
- Re-check `click_send_button` exact pathname / ownership checks and `cdpa_actions.reopen()` so a `/c/<id>` -> Project redirect of the same ID does not trigger false ownership failure. Do not remove task/team ownership checks before a write.

### C. Provisional URL wait

Replace the independent 5-second canonicalization window with **10 seconds**. The 10 seconds waits only for URL settlement; it is not the P002 5-second stable-response threshold and not the Send-acceptance 60-second evidence budget.

After 10 seconds: if the same owned page still shows **current response activity**, continue observing and allow response completion even if URL remains provisional. A valid response must not be discarded solely because `/c/<uuid>` did not appear. Do not treat arbitrary DOM mutation as activity. If neither canonical URL nor relevant Send/response activity exists, surface a clear pause/recovery reason rather than looping or blocking under a misleading identity error.

### Existing owners / target boundaries

- `src/playwright_auto/chatgpt.py`: `_wait_send_acceptance`, `send`, `click_send_button` and page observation.
- `src/playwright_auto/chatgpt_models.py`: `SendReceipt`, `extract_session_id`, new-message baseline.
- `src/playwright_auto/durable_blocks.py`: `SENDING -> SENT`, accepted receipt without exact user ID.
- `src/playwright_auto/cdpa_worker.py`: `_sending`, `_owned_or_block`, `_canonicalize_receipt_conversation_url`, `_discover_accepted_conversation_identity`, `_active_recovery_context`.
- `src/playwright_auto/cdpa_actions.py`: `_reopenable_conversation_identity`, `reopen`.
- `src/playwright_auto/role_runtime/response.py`: existing manual steering and bound assistant handling; preserve P002.

### P0.001.1 acceptance tests

| Case | Expected |
| --- | --- |
| New user DOM after click | `SENT` |
| Newly visible Stop after click, no user message yet | `SENT`, then safe response/URL continuation |
| Newly observed assistant response before user DOM | `SENT`, route correct assistant |
| Stale Stop present before click | Not newly accepted; no false terminal block |
| Old assistant text, generic mutation, stale LISTEN stream | Not accepted as this Send |
| Multiple manual user injections during response | No identity BLOCK, follow existing steering behavior |
| `/` -> local URL -> `/c/<uuid>` | Canonical ID saved, no premature BLOCK |
| `/c/<uuid>` -> `/g/<project>/c/<same-uuid>` | Same identity, Project navigation preferred |
| Challenge or `local-chatgpt` after known good URL | Good URL remains unchanged |
| 10-second URL delay with actual streaming | Continue observing, no URL-only BLOCK |
| Stable valid terminal response while URL stays provisional | Accept result, flag reopen limitation only when needed |
| UUID really changes to unrelated conversation | Do not adopt/replay or falsely attribute response |

Run focused `tests/test_chatgpt_recovery.py`, `tests/test_durable.py`, `tests/test_cdpa_pool_integration_edges.py`, `tests/test_cdpa_blocker_recovery_contract.py`, `tests/test_role_runtime.py` plus relevant browser/URL checks, then independent review of source/diff. Restore or replace obsolete tests based on real observed behavior; do not merely flip assertions to make tests pass.

**P0.001.1 DONE gate:** all tests above pass; accepted Stop/assistant-without-user-ID does not enter erroneous identity BLOCK; URL preference and redirect work; no Send replay; P002 terminal tests stay passing; independent reviewer signs off; commit only task-owned verified changes.

## P0.001.2 — Cloudflare/session recovery (after P0.001.1)

1. **Confirmed Cloudflare challenge:** PAUSE with `BROWSER_CHALLENGE` and a short operator guide. Model picker load failure / Send failure may be clues, not enough alone to label Cloudflare. Operator closes and reopens only the affected Chrome/CDP profile/endpoint (including its browser windows), then uses Resume; worker must not silently restart an unrelated Chrome.
2. **Valid `/c/<uuid>` but session unavailable:** PAUSE with `SESSION_UNAVAILABLE` and concrete instructions; user waits and resumes. No F5 loop, no automatic New Chat solely because the server temporarily cannot open a session.
3. On reopening a persistent conversation, use saved canonical ID and preferably Project URL, re-prove role/task/team ownership at the new page, and allow **30-second page stabilization/readiness** before proceeding. A changed runtime `page_id` after restart is not itself conversation mismatch. This includes preassigned persistent **Team role URLs** (even before that role's first Send), not only Solo and already-SENT roles: never replace an explicitly attached Team role URL with Fresh Chat during session recovery.
4. **Operator-approved narrow exception:** if Cloudflare is confirmed and *no canonical conversation URL/ID was ever saved*, treat the old browser generation as abandoned for continuation. Preserve the original durable ledger/receipt, even if it already says `SENT`; after the operator explicitly Restarts/Resumes, allow at most **one** fresh send of that goal on a new request/generation, with a durable retry counter and duplicate-send warning. This is a deliberate exception to the repo's default exactly-once invariant, NOT proof the original request never reached ChatGPT. Never silently reset/rewrite a `SENT` receipt or permit repeated replay on subsequent restarts.
5. If there is a canonical ID, resume that conversation first rather than creating a new one. If there are manual drafts/attachments or a known active response, do not discard those on automatic retry. Temporary mode has separate non-reopenable lifecycle and must not be converted into a saved-chat recovery.

### P0.001.2 acceptance tests

- Real challenge evidence yields PAUSE + correct endpoint restart guide; unrelated model-loading error alone does not invent a Cloudflare verdict.
- Real session-unavailable on canonical URL yields PAUSE; Resume reopens saved conversation without wrong-session BLOCK.
- `/c/<uuid>` redirect into Project after Chrome restart remains valid; 30-second stabilization/readiness is honored.
- Cloudflare without canonical ID: single explicitly operator-triggered fresh retry is permitted and logged; worker restart does not reset retry count; no third Send.
- Cloudflare with existing canonical ID: resume original conversation, not New Chat; no unrelated tab is closed.
- Send and result flows remain functional without frontend HTTP acceptance observer or added backend polling.

**P0.001.2 DONE gate:** deterministic recovery tests + live end-to-end demonstration on disposable/safe CDP context, independent review, isolated commit. No automatic browser kill/reload in tests against user-owned tabs.

## Execution discipline / known blockers

1. P0.001.1 first. Review the current dirty Send patch before modifying it further; it is partial and has not passed broad integration. Do not mark P0.001.1 DONE based on its 17 existing narrow passes.
2. Do not stage unrelated preexisting `PROBLEM.md` modifications. Do not reset, stash, clean, switch branch, push or release without explicit authority.
3. The current HCU rules file has an older evidence fingerprint than `hcuw fingerprint .` output; follow repository instructions for narrow policy reconciliation before implementation if the drift is relevant. This is not a reason to expand the feature scope.
4. P0.001.2 starts only after P0.001.1 has verified receipt/URL semantics and an independent reviewer has confirmed the recovery dependency.

**Current state:** decisions agreed; no verified integrated implementation yet. This document is a plan, not an assertion that the incidents have been fixed.
