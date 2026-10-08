# Browser mutation evidence — bounded implementation plan

## Goal and limits

Audit frequent Cloudflare/ChatGPT blocks using evidence, then implement the smallest proven improvements. No causal Cloudflare root cause is established. No stealth, spoofing, bypass, Desktop edits, live browser/runtime mutation, lifecycle redesign, commits, push, or package publication.

Preserve exactly-once Send, accepted/ambiguous no-replay, exact ownership, manual composer/attachments, operator Retry, terminal response admission and Temporary no-refresh.

## Verified evidence

- Actual model registry (`pi --list-models sol`) contains `cliproxy-2080/gpt-6.1-sol`; initial REVIEW + PLAN and separate DEV use it.
- Normal manifest Send (`cdpa_worker.py` around 6520) explicitly uses `wait_for_response=False`, `max_attempts=1`, `recovery_reload=False`. Waiting (`_waiting`, around 7224) uses `RoleController`.
- `team_blocks.py` default `active_reload_after_ms=120000` reaches the retained legacy wait loop, not normal manifest waiting. Do not repair the production controller by changing this default.
- Controller timeout policy derives from `response.refresh_after_seconds=600`. Bound-content progress corrections are pre-existing operator dirty work, including fingerprint/history and virtualization regressions added during audit; preserve them.
- `chatgpt.refresh_page()` directly reloads without action lifecycle evidence. Controller reason is not passed to `client.refresh()`. Goto, new-page, acquisition cleanup and verified close outcomes are incompletely observable.
- Existing `observability.py` is the logging owner. Delay/ready events are scheduling observations, not proof of mutation execution.
- Read-only reviewer reports retained action window 2026-09-14 19:46:32 UTC through 2026-10-02 01:51:46 UTC, 8526 records (bounded log): composer_fill3658/send3525/close_tab814/mcp_allow496/new_chat24/stop5/open_tab4. Close records are 410delay+404ready, not814 closures. Counts are not causal evidence or reload counts.
- New chat readiness observation broadly catches exceptions then falls back to homepage goto; this can conflate readiness timeout with disconnect/lifecycle failure. Only one retained New chat completion is navigation, so frequent fallback churn is not proven.
- Accepted provisional identity can block before the same exact page later exposes canonical `/c/<id>`. This is a separate timing/liveness issue, not proof of Cloudflare/reload/duplicate Send. Current receipt/ledger validation remains fail-closed; correction deferred from this batch.
- Parent independently read all three Desktop launchers: persistent Thorium profiles and local CDP match supplied evidence. No evidence that remote-debugging flags cause blocking. Cleaner clears cache and cache_storage/service_workers for ChatGPT/Cloudflare. Desktop `cdp-cache-clean.log` reports successful execution on all three ports on 2026-09-29 21:46:43–44 (local timestamps); execution is corroborated by that log, but challenge causality remains unproven. Do not edit Desktop.
- Parent independently reread action events during ongoing runtime: 8532 retained records through 2026-10-02T13:29:19 UTC; mcp_allow now502 while other action counts unchanged. Window is moving. Parent also verified exact provisional acceptance/block/canonical close observations for page e322172c-e6ff-4549-b6dc-11038423f6ce in live-events lines3305–3311.

## Design alternatives and chosen scope

1. **Chosen:** extend existing action evidence at current mutation owners plus local New chat fallback hardening. It resolves a confirmed observability gap without changing ownership or lifecycle policy.
2. Change refresh defaults/tab retention: rejected for this batch; production ownership and logs do not justify it, and operator progress fixes already address demonstrated clock defects.
3. Add accepted-identity recovery behavior: separate later bounded observation-only fix after dedicated regression; not combined with telemetry.

## DEV steps

1. Independently inspect current dirty truth and snapshot compatibility. Latest operator snapshot is outside repo at `C:/Users/admin/AppData/Local/Temp/cdpa-audit-baseline-c3juhmp6/latest-operator`; preserve all baseline changes.
2. TDD: extend `observability.append_action_event` / `record_page_action` with optional reason, team and correlated action ID; retain event IDs/readers/Allow summary. Capture known identity before mutation; preserve it for terminal events if renderer disappears. Never log secrets/payloads or invent ownership.
3. TDD: instrument actual reload in `chatgpt.refresh_page`, propagate caller reason through `ChatGPTPage.refresh` and `RoleController.browser_action`, classify retained direct recovery calls. Preserve committed reload timeout reconciliation and never retry Send.
4. TDD: instrument actual navigation/allocation/cleanup in `workspace.open_role`, `cdpa_actions` fresh/reopen/close and New chat fallback. Report generic known cleanup reason if caller cause is unavailable; do not mislabel it. Keep existing owners; no new mutation framework or durable state.
5. TDD: New chat DOM success must not navigate; missing-target fallback remains supported; readiness timeout must recheck safe achieved postcondition; non-timeout/disconnect errors must propagate without fallback. If local hardening cannot preserve composer safety, defer behavior change and disclose.
6. Run narrow then impacted safety suites, review patch, `git diff --check`.
7. Parent independently inspects actual diff and reruns tests; independent review evaluates requirements, correctness, coverage and complexity. Repair Important findings before closure.

## Acceptance

- Executed in-scope mutations have reasoned correlated start and terminal evidence with available exact identity.
- Long progressing bound response remains protected by operator progress patch; true stale/UI recovery still works.
- No duplicate/replayed accepted Send or weakened owner/composer/Retry/terminal/Temporary invariant.
- No broad refactor/new recovery stack or unrelated dirty loss.
- Focused regression evidence green; full-suite limitations disclosed.

## Verification baseline

Use `./.venv/Scripts/python.exe -m pytest`; `uv run` tried replacing a locked `cdpa-worker.exe` and failed Access denied. Do not stop worker or alter dependencies.

Parent latest combined suite: **194 passed** (runtime/browser-runtime/recovery/composer/actions/refresh/late-send/presend/response/temporary/rate-limit). Selected worker and operator safety: **78 passed,3 skipped,126 deselected**. Reviewer ran **158 passed** and **134 passed** broader targeted groups; counts overlap and must not be summed.

Pre-implementation full suite timed out120sec with existing failures; confirmed `tests/test_cdpa_learning.py:247` missing prompt text. Do not claim full suite green or fix unrelated prompt/bootstrap tests. Historical cached failures are not fresh evidence.

## Remaining questions

- Cloudflare causal attribution needs future reasoned mutation/challenge evidence, not bypass.
- Accepted canonicalization timing needs its own bounded observation-only regression/fix.
- Exact historical cleanup causes cannot be recovered from delay/ready counts.
- Ordinary overall response budget enforcement was not demonstrated by audit; do not silently redesign it.
- No live deployment acceptance or worker restart is authorized by this task.
