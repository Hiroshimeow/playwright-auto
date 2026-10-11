# Study case: interrupted response with stale Stop button

Date: 2026-10-11 JST. Investigated on g6 CDP endpoint `9202`.
Conversation (operator supplied): `https://chatgpt.com/c/6aca8431-80fc-83ee-a0b1-728a76357178`.
Page title: `Review build APK`. Source tree HEAD at observation: `38824de`.

## Method and scope

Read-only Playwright `connect_over_cdp` to the *exact existing tab* (one match). No reload/F5, click, typing, Stop, Retry, Send, ChatGPT HTTP polling, browser restart or task control. Inspected DOM; installed one temporary MutationObserver under `main`, configured for `childList`, `characterData`, and selected UI attributes. Sampled the same DOM 7 times at 5-second intervals (30.2 seconds). The observer was disconnected at the end.

Raw local (ignored/runtime, not a tracked product change):
- `.runtime/cdpa_stale_stop_observation_6aca8431.py`
- `.runtime/cdpa_stale_stop_observation_6aca8431.log`
- `.runtime/cdpa_interruption_markup_6aca8431.py`

## Direct browser evidence

- Explicit interruption status: `<div role="status"><span class="text-chatgpt-recovery">Connection interrupted. Waiting for the complete answer</span></div>`.
- Stop button: visible and enabled, `button[aria-label="Stop"]`, all 7 samples.
- No visible Retry button. No visible `[data-streaming-response-status]` marker. `document.readyState=complete`.
- Last rendered assistant body belongs to an **earlier turn**, with later user messages visible in the DOM and no subsequent assistant message for the most recent user turn. Do not misattribute the old answer to the latest user message.
- Last previous assistant text length: **9,627** characters; hash `7c80bf09` throughout.
- `main.innerText` hash: `3a499b9d` throughout.
- MutationObserver across the 30.2-second interval: **44 records**, all `attributes`, all on `SPAN`; **0** `childList`, **0** `characterData`, **0** assistant-subtree mutations, **0** assistant text changes.
- Recorded sample offsets: 0, 5.1, 10.1, 15.1, 20.2, 25.2, 30.2 seconds. Stop present in each; assistant and main hashes unchanged in every sample.

## Conclusion and limits

This is a **verified browser-visible response interruption with a stale Stop UI**, not proof of ongoing generation. Generic MutationObserver wake events are **not** progress; the 44 attribute changes only show UI churn. The UI may theoretically reconnect later; this study does not assert backend generation state or permanent impossibility of recovery.

The user's observation that Stop remains after F5 was **not independently re-tested by reloading** (reload intentionally avoided to preserve the live tab).

## Code correlation (source at observation)

- `src/playwright_auto/chatgpt_models.py` declares `connection interrupted` / `waiting for the complete answer` as transient response markers. `response_transport_ui_active` may treat these notices as activity rather than interruption.
- `src/playwright_auto/role_runtime/signals.py`: `RoleSignals.transport_active` is true whenever `stop_visible` is true, even without content progress.
- `src/playwright_auto/role_runtime/page_observer.py`: main/form DOM mutations wake the role controller; such wake signals must not be equated with new assistant content.
- `src/playwright_auto/role_runtime/controller.py` and timeout handling need a response-interruption classification **before** treating stale Stop as indefinite activity. The existing terminal-response five-second stability requirement remains separate.

## Minimal regression acceptance for subsequent phase

1. Given an accepted Send, last user turn has no assistant continuation, UI contains the exact interruption `role=status` text, Stop stays visible, and MO yields attribute-only churn but no assistant text advancement: classify **interrupted/stalled**, not `responding` indefinitely.
2. Allow a bounded reconnect grace; if no meaningful progress, surface an operator-recoverable pause/recovery without automatic duplicate Send or blind F5.
3. If assistant text/identity genuinely progresses, continue normal response handling. If a correct terminal response already exists for the accepted user turn, preserve normal terminal acceptance.
4. Stop visibility remains allowed as **one post-click Send acceptance signal** when newly visible; it is **never by itself proof of live response progress**.

No product code was changed during this investigation.
