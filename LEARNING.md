# LEARNING.md

Reusable lessons for CDPA teams. Read before starting a task. Add only evidence-backed lessons that are likely to prevent mistakes in future tasks; do not turn this into a chronological activity log.

## Durable transport

- Persist state before and after every send boundary. On restart or CDP reconnect, distinguish `pre_send`, `sending`, `sent`, `responded`, and `routed`; never infer that an ambiguous send is safe to repeat. Browser/transport disconnect exceptions must escape per-task blocking logic so the reconnect supervisor can attach again without changing hop, request, report, or control identity.
- Once send acceptance is persisted, consume only an assistant message newer than the pre-send baseline. Persist the accepted user message/turn identity and match either non-empty identity after restart or F5 because DOM message IDs may change while turn IDs survive. When durable identity exists, never fall back to rendered text; long prompts may be collapsed behind `Show more`.
- Structurally incomplete output with exact provenance is resumable waiting, not proof of failure. Unproven or stale output remains blocked.
- Rendered ChatGPT DOM may expose route JSON without Markdown fences. Parse the terminal JSON object, validate duplicate keys and all fields, and fail closed.
- A malformed route should trigger a bounded guide-only repair in the same tab. Repair input is compact identity plus validation error, shared guide, and generic naming rule; it must not resend the original task, handoff, constructor, or worker-computed report path.
- Resume is identity continuation, not task creation. Separate new-team-base normalization from exact existing-team validation: never truncate, lowercase, sanitize, or strip an allocated exact identifier, because its numeric suffix is durable identity. Inspect every raw catalog entry associated with that exact team by key, declared team, or path; malformed structures and duplicate path claims fail before selection. Compare every canonical catalog field—path, task, team, suffix, status, and update timestamp—exactly to the manifest before selection and again inside the manifest lock. Revalidate the selected candidate, then queue the shared durable resume control without changing task/hop/request/turn/report identity. Never reconcile away or silently rewrite corrupt catalog metadata.

## Browser and tab ownership

- Use the unique physical-role + team + task tuple as durable operating ownership. Treat `page_id` as replaceable runtime metadata during pre-send reconciliation, but never transfer an accepted in-flight receipt to another page. Fail closed when more than one exact owner exists.
- Tabs with no role are user-owned free tabs. Never allocate or clean them.
- Preserve the persistent Chromium process and profile. Attach/detach through CDP; never call `browser.close()`.
- Stop is an urgent action and must not receive artificial human delay. Visible non-urgent actions may use the shared delay policy.
- Controlled reopen or restart must fail closed when the composer contains manual text or attachments; recovery must never discard user input to reclaim a role tab.

## Response waiting

- Send-button visibility is not sufficient to prove response completion because the user may type while a response is active.
- Response-state conditions belong in editable policy, not embedded flow branches. The baseline responding signal is visible Stop plus empty composer, with stable-response/provenance checks deciding completion.
- While no valid response has been accepted, refresh after 20 minutes without assistant/transport activity when the composer is empty and no manual input is pending; Stop may be visible or hidden by timeout/error UI. Total wait budget is 120 minutes. Refresh count, activity signature, and timestamps must persist across worker restart.
- Persist assistant IDs, turn IDs, and content fingerprints before F5. Union that recovery baseline across refreshes/restarts and reject matching pre-refresh output. Automatic recovery is F5-only; never click Retry or Regenerate.
- A stuck Stop button after content completion is an observed DOM edge case; use stable content and provenance evidence rather than Stop disappearance alone.
- Finality is a validated route/report observed unchanged across at least two samples, not merely Stop disappearance or a transport banner. Changing assistant content, connection-interrupted text, timeout UI, or recent DOM activity remains active work; only a clean, inactive, stable malformed candidate may trigger route repair. Immediately before a timeout block, make one final bounded read using the same provenance, stability, route, and report validator so a completed output at the deadline is accepted exactly once.
- Base recovery refresh on 20 minutes without assistant/transport activity, not continuous Stop visibility. Persist the latest assistant signature, length, last-change timestamp, message/turn IDs, and content fingerprints before F5 so a timeout banner that hides Stop can recover without Retry, Regenerate, or resend.

## Reports and state

- Large role reports belong in independent Markdown files. Cross-role handoffs reference the exact source report path, while the worker keeps the exact next output path internal and exposes only the generic naming rule. Invalid route JSON is repaired in the same role at most three times before blocking.
- Build agent prompts from one explicit physical-role allowlist: title, task ID, team, role, source role, turn, workspace, allowed logical routes, goal, and handoff. Do not serialize team rosters, manifests, or transport records; they leak redundant state and browser/controller metadata.
- Prompt contract tests must inspect the final composer/send payload and persisted receipt, not only `PromptBuilder` output; a shared durable layer can otherwise append forbidden transport markers after prompt construction.
- Machine state and human reports serve different purposes. Keep the atomic task manifest separate from role-authored Markdown reports.
- Only the worker/dashboard mutates task state. Concurrent agents must never edit the manifest directly.

## Dashboard and operations

- Apply a Resume control and its immediate active-hop recheck inside one per-task transaction. Finalize ownership or other non-CDP failures as one durable `reblocked` result, let CDP disconnects escape for reconnect supervision, and do not append duplicate errors when the active block identity is unchanged.
- Incremental updates must preserve text selection and scroll context; do not reload the full page during polling.
- Dashboard controls must request durable state transitions. Resume must report the rechecked result and Retry must require a structured retryable block. Clear Team preflight must fail on any supported-page inspection uncertainty, then persist `stop_pending`/`close_pending`/`closing`/`verify_pending`; once cleanup starts, nonterminal work is STOPPED with no active hop. Count only observed closures and require a clean post-close zero-match verification before `CLEARED`; otherwise preserve the phase and concrete error for restart.
- Apply the same strict primary-manifest validator to direct load, discovery, exact-team resume, catalog handling, and pre-replacement writes. For exact-team operations, raw catalog validation must not skip non-mapping or incomplete entries merely because a valid filesystem manifest also exists; key/path/team/task identity, required suffix/status/timestamp types, duplicate path claims, and full canonical catalog-to-manifest equality are checked before mutation and during locked revalidation. Malformed JSON in canonical `<team>/<task-id>/<file>.json` layout must remain a diagnostic and reserve its directory task ID/team suffix even when the catalog is absent; scope the reservation so unrelated team creation continues. Schema-shaped evidence, full copies under a second filename, partial manifests, `requests.json`, and poisoned catalog metadata are never executable tasks.
- Keep active work, fully offline recoverable work, and terminal History/Reports as separate projections of the same manifest. When CDP is disconnected, availability is unknown rather than offline. `CLEARED` is not sufficient to hide a task: while an exact assigned-role tab is observed, show a cleanup warning and permit Clear Team to re-verify and close it; History requires connected zero-match evidence.
- Discover only canonical primary task manifests. Arbitrary evidence JSON and request ledgers inside task directories must never enter worker or dashboard task loops.
- Normal client disconnects such as `BrokenPipeError` and `ConnectionResetError` are not operational failures and should not pollute logs.
- A non-DONE task is never automatically cleaned after one hour. DONE or STOPPED teams may be cleaned after one hour idle; active, paused, stalled, or blocked teams require explicit user action.

## Complexity control

- Reuse `ChatGPTPage`, workspace ownership, durable ledger, and current response reader. Do not create a duplicate DOM reader or parallel orchestration stack. Retired coordinator code, scripts, docs, tests, and dashboard state bridges should be deleted rather than retained as compatibility surfaces when the canonical manifest worker fully replaces them.
- Separate components by responsibility, but avoid one-implementation interfaces and speculative plugin systems. A component boundary is justified when it can be tested and replaced independently.
- Parallel worktrees help only when modules are genuinely independent and start from a reproducible baseline. Do not split a large uncommitted diff merely to appear parallel.
