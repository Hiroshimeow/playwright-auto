# Contract: DOM-only Browser Observation

This contract applies to `chatgpt_web` and other browser-backed providers only.

## Authoritative projection

Decisions use a fresh DOM projection containing the visible user/assistant transcript, composer state, stop/retry visibility, visible thinking/progress/tool nodes, visible errors, terminal state, and canonical conversation identity evidence.

### ChatGPT Web activity-node capture

The projection MUST expose ChatGPT Web visible activity/progress nodes even when they are not part of assistant final text. Current production DOM evidence includes activity headers and shimmer spans under selectors/classes such as `.group/activity-header`, `[class*="agent-activity"]`, `[class*="activity-header"]`, `.summary-lK7Lpm`, `.cadencedShimmerSweep-ICUAVH`, and `.cadencedShimmerHighlight-VcX29h`. Each projected node includes bounded text, tag, class/id/testid/aria label when present, viewport visibility/intersection, and a bounded DOM path. These nodes feed dashboard/progress display and stale detection; they do not by themselves create a route or terminal answer.

### Business/Enterprise permission prompts

ChatGPT Web Business/Enterprise role turns commonly render MCP/tool permission prompts with `Allow` controls. The browser provider MUST project permission prompt presence/count and reuse the existing CDPA permission-click handler from the legacy runtime where possible. Clicking `Allow` is permitted only on the exact owned task/team/page with an empty/manual-safe composer and no blocking ownership drift. Permission prompts are not routes, terminal answers, or provider results; after allowed permission clicks, the role remains in the same response-wait turn and continues DOM observation.

## Non-authoritative signals

- LISTEN/network data cannot route, block, close, accept, or resend.
- MutationObserver cannot route, block, close, accept, or resend.
- If retained, either mechanism may only wake a fresh DOM projection.

## Exactly-once rule

Accepted or ambiguous Send is never replayed merely because canonical conversation identity is late or missing. Pre-Send validation must complete before the existing command-ledger Send intent boundary.

Non-browser providers such as Pi/Codex do not use this contract; they report through ProviderResult and MaterialRef.

## Phase 1.5 control decisions

Phase 1.5 uses this same projection for control recovery. `stop_visible=true` with `retry_visible=false` is active response evidence, not a failure. Stable `retry_visible=true` after accepted Send is provider Retry evidence; the runtime may click the provider Retry button once for the exact accepted user turn after ownership/composer checks. Visible assistant terminal JSON and audit transcript text may be used only as reconciliation evidence when the expected report exists and RouteContract validation succeeds.

No new DOM crawler/source owner is introduced for these decisions.
