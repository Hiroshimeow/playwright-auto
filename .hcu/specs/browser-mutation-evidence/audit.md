# Recovery/navigation audit

## Scope and method

Read-only REVIEW + PLAN subagent used registry-verified `cliproxy-2080/gpt-6.1-sol`; parent independently inspected owning source, dirty baseline, Desktop files, action counts and accepted-identity event sequence. Separate DEV follows the validated scoped plan. No live browser mutations or Cloudflare experiments performed. Lines refer to pre-implementation source; locate symbols if shifted.

## Production mutation paths

| Boundary | Call chain / evidence | Meaning |
|---|---|---|
| Fresh allocation | worker `_pre_send` → `_acquire_workflow_role` → actions `fresh_chat` → workspace `open_role` → new_page/goto(homepage)/bind (`workspace.py:112–141`) | Intentional new role page; failed allocation closes its own created page |
| Saved conversation reopen | `_waiting` → `_owned_or_block` → recovery → actions `reopen` (`cdpa_actions.py:781–841`) | Creates page only if exact owner unavailable; navigates only if saved exact URL differs; re-proves/restores exact binding |
| Response refresh | `_waiting` (`cdpa_worker.py:7195–7224`) → `RoleController.run` → `browser_action` (`controller.py:448`) → client `refresh` (`chatgpt.py:3926`) → `refresh_page` (`:766`) | Production automatic recovery, not legacy team timer |
| Pre-send error refresh | controller `decide(phase=pre_send)` ui_error hash → same refresh owner | One bounded attempt per unchanged error; ownership/composer guards retained |
| Normal Send | `_sending` durable block around6520 | wait_for_response=False, max_attempts=1, recovery_reload=False; no Send recovery reload on normal task |
| New chat UI | client `prepare_task`/`new_chat` → `open_new_chat` (`chatgpt.py:722`) | DOM click; broad observation exception fallback originally goto homepage; draft/attachments/active guards precede mutation |
| Cleanup | worker terminal cleanup/Clear Team → preflight → actions `close_team` (`cdpa_actions.py:1356`) | Existing assigned-owned pages only; strict verification. Explicit operator close/new-chat/restart and independent idle closure are separate causes |
| Bootstrap branch | actions branch goto around589 | Exists but disabled for new normal workflow; not normal root cause |
| Endpoint registration | explicit unknown-auth capability probe | May new_page/goto/close; periodic inventory does not allocate solely to check login |

## Recovery trigger distinctions

- Production `RoleController` policy timeout derives from `response.refresh_after_seconds` (600). `controller_progress_at` owns stall timing. Pre-existing operator dirty changes now advance it on changed bound DOM content and preserve progress across virtualization.
- Current pre-send UI error refresh is keyed once per error hash; same error then waits.
- Temporary workflow suppresses automatic refresh; no weakening allowed.
- Legacy `DurableTeamRoleExecutor` supplies active_reload_after_ms=120000 into legacy wait_for_response. That path was able to interrupt progressing output before the operator patch. It is retained/public, but not normal manifest waiting. Do not change its default to claim production fix.
- Retained legacy Send recovery may reload when enabled; production normal Send explicitly disables it.
- Explicit ownership recovery reopens a saved canonical conversation. No accepted provisional URL can authorize reopening/rebinding/resend.

## Observability contract gap

`refresh_page` has no lifecycle action evidence. Controller semantic decision logs cannot prove reload execution. Action `close_tab` delay/ready does not prove closure. Allocation/goto/acquisition cleanup lack correlated start and outcome. Action metadata omits team even though page reader obtains it.

Smallest contract: existing action log owner, action + reason + correlated action_id + phase(start/complete/error) + known task/team/role/page_id and ownership/URL evidence. Capture pre-action identity for terminal attribution after renderer loss. Existing event_id/timestamp/PID remain. Do not log credentials, prompts, cookies or speculative ownership. Timeout may be ambiguous/committed: distinguish attempted browser call failure from verified successful refresh postcondition.

## Runtime counts verified

Reviewer window contained8526 records ending2026-10-02T01:51:46 UTC. Parent later independently read8532 records ending13:29:19 UTC; oldest record2026-09-14T19:46:32 UTC. Log is bounded/moving, not lifetime evidence.

Parent counts: composer_fill3658, send3525, close_tab814, mcp_allow502, new_chat24, stop5, open_tab4. Close phases410delay +404ready; reviewer404ready records cover378 page IDs/179 task IDs. New-chat completion5dom/1navigate. Counts include phases; never compare them as physical operation totals. Reload absence in old log is instrumentation absence, not zero reloads.

Intentional cleanup paths are proven by source. Historical initiating causes are not recoverable from these phase counts; unnecessary churn not proven. Instrument before changing retention/allocation.

## Accepted identity failure is separate

Parent verified `.runtime/live-events.jsonl` lines3305–3311 for exact page e322172c-e6ff-4549-b6dc-11038423f6ce:

- 00:39:14.585 UTC Send completion observed at homepage.
- 00:39:15.878 DOM accepted user identity on provisional local-chatgpt route.
- 00:39:49.846 blocked accepted_conversation_identity_unresolved.
- 00:39:49.938 same page ID observed with canonical /c/6abefd21-fd2c-83e8-a4cd-051dff5663b8 during close delay.

`_discover_accepted_conversation_identity` (worker around6908) performs exact receipt/SENT-ledger/accepted user/page/canonical checks and enrichment, without reload/goto. `_owned_or_block` blocks when identity remains provisional. Blocked tasks do not automatically continue normal ticks. This establishes premature block relative to later canonical observation, not why canonicalization was delayed, not Cloudflare, not duplicate Send. Any future fix must be bounded observation-only with original request, attempts1 and fail-closed wrong/missing identity. Deferred from telemetry batch.

## Desktop evidence

Parent read9202_PLUSHCU.bat/9212_SUSAN.bat/9222_BusiHCU.bat: same Thorium executable, localhost CDP address, respective ports/origins, distinct persistent profiles, no-first-run/no-default-browser-check/new-window homepage. No evidence remote-debugging-port causes challenge.

Parent read cleaner: Network.clearBrowserCache and Storage.clearDataForOrigin(cache_storage,service_workers) for challenges.cloudflare.com and chatgpt.com, on all3ports. `cdp-cache-clean.log` reports successful execution2026-09-29 21:46:43–44 local. It does not explicitly clear cookies/login storage, but changes profile cache/service workers outside workflow ownership. Risk factor, not established block cause. Recommend only explicit troubleshooting while workflows idle with before/after evidence, not periodic active-workflow cleaning; no Desktop edits made.

## Classification

Confirmed: missing mutation telemetry, original progress-clock defects (operator fixes preserved), broad New-chat fallback catch, accepted canonical timing block, cache-cleaner state mutation and execution reported by log.

Plausible: redundant navigation/cache disruption/allocations contribute to challenges. Unconfirmed until correlated evidence.

Unsupported: CDP flags as root cause,814confirmed closes, zero reloads from missing events, accepted identity failure equals Cloudflare, stealth/spoofing or safety weakening as appropriate remedy.

## Fix batch and test gate

See plan.md. Implement reasoned lifecycle logging and locally provable New-chat fallback narrowing only. Do not change tab cleanup policy, timers, accepted identity reconciliation or operator progress work.

Required proofs: progress over long response does not trigger elapsed-only recovery, true stale/UI recovery remains, reasoned logging, exactly-once/accepted no-replay, ownership, manual input/attachments, Retry, terminal and Temporary no-refresh.

Known residual: ordinary overall response-budget enforcement was not demonstrated; deadline_at exists but ordinary controller consumption was not established. No redesign in this batch.

No causal live Cloudflare acceptance, worker restart/deployment, Desktop modification or full-suite green claim is supported. Baseline focused suites green; full suite has unrelated failures and timed out before implementation.
