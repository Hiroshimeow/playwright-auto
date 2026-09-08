# CDPA operational role controller refactor

## Authority and baseline

Operator-directed implementation by the current assistant, not a CDPA coding team. Baseline checkpoint: `6e68acb58eb20940d8045788e2d02df16c881d04`. This preserves existing dashboard/projection work and the unsuccessful snapshot-presence patch. No push, task recreation or original-prompt replay.

## Operational contract

CDPA sends once, watches the current conversation bound to the team/role, resolves operational interruptions, consumes the current valid result and routes. Later operator Retry/injected messages are normal continuation, not a provenance violation.

- G1: filter relevant ChatGPT conversation traffic, not every resource.
- G2: retain explicit, cached conversation/history information retrieval. It is NOT required to run, resume, approve or route, and failure cannot block these operations. No periodic graph GET.
- G4: remove original accepted-user-ID admission from response and permission observation/consumption. Send receipts remain to prevent replay after restart or ambiguous Send.
- Validate route JSON/report, not the semantic correctness of agent work.
- DOM = self-reinstalling MutationObserver plus five-second safety check. Hybrid adds passive network signals to the same consumer. DOM-only leaves listeners installed but does not use their evidence.
- Hidden/visible Allow or live Allow action: wait five seconds, prefer conversation-scoped approval, fall back to plain Allow. After dispatch wait five seconds, then F5 once if Stop/response activity has not progressed. Both waits are intentional.
- Retry/generation failure: FORMAT-REPAIR continuation, never click Retry/Regenerate or resend the task.
- Stable malformed result: F5 once, re-read, then FORMAT-REPAIR if unchanged. Changed results are re-evaluated, not rejected as foreign.
- Same operational state with no progress for ten minutes: F5, never resend. Existing overall response budget and bounded repair attempts stay configurable.
- Auth/session expiry and genuinely blocking dialogs need operator action. Retry is not a fatal/authentication page error.
- Manual drafts/attachments block destructive mutations, not read-only result observation.
- One serialized action owner per page. No new scheduler, database, generic bus or plugin framework.

## Confirmed baseline defects

1. `WaitProbe` sees hidden permission nodes but `ChatGPTSnapshot` discards the counters; the patch queried an absent field.
2. Exact Listen filters original POST user identity and graph validity; the active owner does not read ambient evidence.
3. Whole `response.body()` processing is not incremental SSE observation.
4. Retry becomes generic ERROR: repair is queued but binding/send rejects it.
5. Wait, backend-primary completion, terminal reconciliation and Resume duplicate inconsistent policy.
6. Tests stopped at repair queueing or used synthetic snapshot fields absent in production.

## Layout

`role_runtime/` becomes the single operational policy group. `controller.py` coordinates observations/decisions. Each real interruption has one file: `allow.py`, `retry.py`, `ui_error.py`, `malformed.py`, `timeout.py`, `stalled.py`, `auth.py`, `dialog.py`, `draft.py`. `response.py` selects the current result. Handlers use an explicit order, not a registry framework.

`page_observer.py` owns one page-lifetime DOM/network observation state and coalesced wake signals, without original task-request admission. `listen.py` decodes observed conversation messages, permissions and status independently of full graph reconstruction. Incremental CDP observation uses existing browser traffic; whole-response parsing remains a bounded fallback.

`cdpa_worker.py` keeps durable scheduling/persistence, routing, bootstrap and operator controls. Accepted-wait and Resume use the same role controller. Remove obsolete backend-primary/settle and duplicate permission paths after reference audit. Do not discard independent-agent/dependency features merely to reduce line count.

`chatgpt.py` stays the browser adapter. Preserve actionable fields through full/probe snapshots. Align Retry semantics across snapshots, binding and send. Separate irreversible Send acceptance proof from result selection.

## Implementation sequence and evidence

1. Checkpoint committed.
2. Add failing reproductions using real snapshot types: hidden counters, missing original user, Retry still editable, requestless Listen continuation.
3. Implement the explicit handler group and decision matrix tests.
4. Replace production waiting/Resume; delete dead paths rather than layer bypass flags.
5. Consolidate page-lifetime observation and informational G2; add bounded stream parsing and diagnostics.
6. Browser integration: hidden Allow snapshot -> controller -> handler dispatch; Retry -> queue -> acquire/bind -> actual continuation send; operator Retry/injection -> current result; reload/restart and no original-send duplication.
7. Focused plus full regression in bounded batches. Revise obsolete tests to the operator contract, never suppress real failures.
8. Commit, restart only worker, reconcile existing affected tasks through durable Resume, and verify runtime/actions. No new CDPA teams.

## Acceptance

No original-user, graph completeness, recurring status or request-scope revision prerequisite in response/permission admission. G2 remains available but non-authoritative. Interruption cases converge on one executor. Pre/post Allow 5s and ten-minute stall recovery remain. Retry repair actually sends on the same conversation. Preserve drafts, accepted receipts, dependencies, reports and operator Pause/Stop. Never close persistent browser or push remote. Record exact tests, deleted code paths, remaining limitations and live outcome.
