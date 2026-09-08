# ChatGPT DOM automation contract

Verified against the live ChatGPT web UI on 2026-07-13 through Chromium 150 and CDP.

## Scope

This contract is for deterministic browser automation without an LLM agent. It uses fixed selectors plus explicit state conditions. The DOM is not a public API, so every release must be probed before relying on it in production.

> **CDPA runtime note (2026-09-09):** this document is a low-level browser/DOM reference, not the current CDPA orchestration design. Current workflow roles use the shared operational `RoleController` with DOM + passive Listen observation. Full-conversation/history retrieval is optional information/recovery lookup and must not become authority for normal Send, blocker handling, result admission, Resume, or routing.

## Recommended architecture

There are three viable approaches:

1. **Playwright-only DOM adapter**
   - Playwright reads and mutates the live DOM directly.
   - Lowest setup cost.
   - Most exposed to frontend selector changes.

2. **Tampermonkey-only userscript**
   - A userscript watches the page, computes a normalized state, and executes local commands.
   - Easy to inspect manually.
   - Weak for browser lifecycle, tab orchestration, retries, screenshots, and external process control.

3. **Hybrid adapter — recommended**
   - Tampermonkey normalizes the unstable ChatGPT DOM into stable attributes/events.
   - Playwright owns tabs, session URLs, role routing, waits, retries, screenshots, and process lifecycle.
   - More components, but selector breakage is isolated to one userscript.

The example userscript is `examples/chatgpt-playwright-adapter.user.js`.

## Stable selector priority

Use selectors in this order:

1. `data-testid`, fixed element IDs, and semantic `data-*` attributes.
2. `role` plus `aria-label`.
3. Visible text only as a fallback.
4. Never depend on generated CSS/Tailwind class names.

## Core selectors

| Purpose | Selector | Notes |
| --- | --- | --- |
| Composer | `[contenteditable="true"][role="textbox"]` | It is not a `<textarea>`. Read `innerText.trim()`. |
| Submit element | `#composer-submit-button` | Same element changes state. |
| Send state | `button[data-testid="send-button"]` | Normally visible only when a sendable draft exists. |
| Stop state | `button[data-testid="stop-button"]` | Visible while the response is streaming. |
| New chat | `[data-testid="create-new-chat-button"]` | Prefer this over a shortcut. |
| Login required | `[data-testid="login-button"]` | Visible on the logged-out ChatGPT page. |
| Message | `[data-message-author-role][data-message-id]` | Gives role and message identity. |
| Turn container | `section[data-turn-id][data-testid^="conversation-turn-"]` | Groups messages into a user or assistant turn. |
| Retry error | `[data-testid="regenerate-thread-error-button"]` | Strong error signal. |

The current turn container also exposes:

- `data-turn-id`
- `data-turn-id-container`
- `data-turn="user"` or `data-turn="assistant"`
- `data-testid="conversation-turn-<index>"`

A message node exposes:

- `data-message-author-role`
- `data-message-id`
- `data-message-model-slug`
- `data-turn-start-message`

## Composer rules

Do not determine emptiness from child count. A visually empty contenteditable may still contain `<p><br></p>` or editor scaffolding. Runtime testing also showed that `locator.fill("")` did not clear this controlled ProseMirror editor; `Ctrl+A` followed by `Backspace` did.

Use:

```javascript
const composer = document.querySelector('[contenteditable="true"][role="textbox"]');
const composerText = (composer?.innerText || "").trim();
const composerEmpty = composerText.length === 0;
```

Expected transitions:

| Condition | Composer text | Send | Stop |
| --- | ---: | ---: | ---: |
| New chat, empty | empty | absent | absent |
| Draft | non-empty | present | absent |
| Submitting transition | usually empty | absent | may not yet be present |
| Responding | usually empty/editable | absent | present |
| Waiting for next prompt | empty | absent | absent |

## State machine

Use the following priority order:

1. `error`
   - Auth callback error, visible retry button, or a visible error alert.
2. `responding`
   - Visible `stop-button`.
3. `draft`
   - Composer text is non-empty.
4. `auth_required`
   - `auth.openai.com`, or visible `login-button`, when no stronger interaction state is active.
5. `submitting`
   - Messages exist, latest visible message role is `user`, and Stop has not appeared yet.
6. `waiting_prompt`
   - Messages exist, latest visible message is not `user`, Stop is absent, composer is available.
7. `new_chat`
   - Composer exists, no messages, no draft.
8. `unknown`
   - None of the above.

Important: the first send from `/` can trigger a route change. Do not keep stale JS execution handles across that navigation. Reacquire the page DOM after URL change.

Persistent-browser lifecycle rule: after `connect_over_cdp`, never call `browser.close()`. It sends a close command to the shared Chromium process. Stop the local Playwright client (`playwright.stop()`) or use `connected_browser(...)` instead.

## Send, Stop, and waiting conditions

Prefer explicit DOM waits over sleep:

```python
await page.locator('button[data-testid="send-button"]').click()
await page.locator('button[data-testid="stop-button"]').wait_for(state="visible")
await page.locator('button[data-testid="stop-button"]').wait_for(state="detached")
```

A safer completion condition is:

- Stop is absent.
- Composer is editable.
- The last assistant message text has stopped changing for a small stability window.

The last condition prevents a false completion during route transitions or UI remounts.

## Message actions

Confirmed from the live frontend bundle:

- `copy-turn-action-button`
- `good-response-turn-action-button`
- `bad-response-turn-action-button`
- `variants-turn-action-button`
- `share-prompt-link-turn-action-button`
- `download-files-turn-action-button`
- `report-message-turn-action-button`
- `project-save-turn-action-button`
- `custom-dictionary-turn-action-button`
- `good-image-turn-action-button`
- `bad-image-turn-action-button`
- `regenerate-thread-error-button`

Not every action is rendered for every message. Some only appear on hover, only for specific content types, or only for the latest turn. Query inside the target message or turn instead of globally.

Example:

```python
message = page.locator(
    '[data-message-author-role="assistant"][data-message-id="<message-id>"]'
)
copy_button = message.locator('[data-testid="copy-turn-action-button"]')
```

## Get the N latest responses

For visible assistant message nodes:

```python
responses = page.locator(
    '[data-message-author-role="assistant"][data-message-id]'
)
count = await responses.count()
latest_n = [responses.nth(i) for i in range(max(0, count - n), count)]
```

For logical turns, group by the closest `[data-turn-id]`. A turn may contain more than one message node because of tool output, hidden/suppressed content, reasoning/final channels, images, or widgets. Deduplicate by `data-turn-id` when the requirement is “N responses” rather than “N message nodes.” The Python helper is `recent_assistant_turns(...)`; the userscript method `recentResponses(n)` follows the same rule.

## Session URL

A normal conversation URL contains `/c/<session-id>`:

- `https://chatgpt.com/c/<session-id>`
- GPT-specific routes can contain `/g/<gizmo-id>/c/<session-id>`

Treat the full URL as the canonical session locator and separately parse the segment following `/c/` as `session_id`.

Do not use page title as identity. Titles change after generation and may collide.

## Multi-tab roles

Multiple tabs can share one logged-in Chrome profile and still hold different ChatGPT conversations.

Do not assign roles by tab order. Tab indices change when tabs open, close, or move.

Use per-tab `sessionStorage`:

```javascript
sessionStorage.setItem("playwright-auto:role", "PLAN");
sessionStorage.setItem("playwright-auto:page-id", crypto.randomUUID());
```

This was verified with three tabs (`PLAN`, `DEV`, `REVIEW`): each tab kept a unique `page_id` and its role after reload.

Recommended tab identity tuple:

```text
(page_id, role, conversation_url, session_id)
```

- `page_id`: stable for the tab across reload.
- `role`: PLAN/DEV/REVIEW or another application role.
- `conversation_url`: current ChatGPT session.
- `session_id`: parsed `/c/<id>` value.

Persist an external registry if roles must survive closing and reopening the entire browser. `sessionStorage` survives reload, not tab destruction.

## Keyboard shortcuts

Read directly from the live shortcut panel. ChatGPT allows shortcuts to be changed, so these are defaults, not a permanent API.

| Action | Default shortcut |
| --- | --- |
| Send message or stop answering | `Enter` |
| Toggle dictation | `Ctrl+Shift+D` |
| Add photos | `Ctrl+U` |
| Open new chat | `Ctrl+Shift+O` |
| Show shortcuts | `Ctrl+/` |
| Toggle dev mode | `Ctrl+.` |
| Toggle sidebar | `Ctrl+Shift+S` |
| Set custom instructions | `Ctrl+Shift+I` |
| Copy last code block | `Ctrl+Shift+;` |
| Delete chat | `Ctrl+Shift+Backspace` |

`F5` is a browser refresh, not a ChatGPT shortcut. In Playwright, use `page.reload()` so completion and errors can be awaited explicitly.

For automation, direct button selectors are safer than shortcuts because users can remap shortcuts and focus can be inside overlays, dialogs, or the composer.

## Tampermonkey bridge

The provided userscript logic was executed successfully through Playwright on the live page. The persistent Chrome profile currently has no Tampermonkey extension installed, so extension-level installation is not yet verified. ChatGPT CSP blocks ordinary inline `<script>` injection; use the userscript manager or Playwright evaluation/init-script mechanisms rather than `page.add_script_tag(content=...)`.

The DOM attributes/events are the primary bridge. The page-global helper below is a convenience when the userscript is running in the page JavaScript world.

The provided userscript exports:

```javascript
window.__PLAYWRIGHT_AUTO__.snapshot()
window.__PLAYWRIGHT_AUTO__.setRole("PLAN")
window.__PLAYWRIGHT_AUTO__.recentResponses(3)
window.__PLAYWRIGHT_AUTO__.runCommand("send")
window.__PLAYWRIGHT_AUTO__.runCommand("stop")
```

It also writes normalized attributes to `<html>`:

- `data-playwright-auto-state`
- `data-playwright-auto-role`
- `data-playwright-auto-page-id`
- `data-playwright-auto-session-id`
- `data-playwright-auto-composer-empty`

Playwright can then wait on a stable adapter attribute:

```python
await page.locator('html[data-playwright-auto-state="waiting_prompt"]').wait_for()
```

It emits `playwright-auto:state` events and listens for `playwright-auto:command` events.

## Probe usage

Inspect every current browser tab:

```bash
uv run python scripts/chatgpt_probe.py
```

Assign a role to page index 0 and inspect again:

```bash
uv run python scripts/chatgpt_probe.py --set-role 0=PLAN
```

The role, page ID, and current task ID are stored in that tab's `sessionStorage` and mirrored into a namespaced `window.name` binding. The mirror preserves the visible `⟦ROLE⟧` title and `ROLE · TASK-ID · page-id` badge across document and cross-origin authentication redirects.

## Historical runtime boundary (2026-07-13)

At that verification point the persistent profile was not logged in. A completed guest response and a full sequential PLAN → DEV → REVIEW → PLAN context-transfer workflow were exercised successfully. Repeated guest requests later exhausted the anonymous runtime: fresh role tabs remained active for 90–120 seconds and then redirected to OpenAI authentication. Durable recovery correctly classified the lost post-send transcript as `sent_marker_missing` and refused to resend.

Structural browser stress passed for 2, 5, and 10 visible roles over three cycles per stage. Durable orchestration stress passed 200 tasks, 2,844 role executions, 44 fresh-process resumes, and up to six parallel role executions per task. Authenticated parallel response completion still requires a valid logged-in profile.

Before production use, repeat the probe after a valid login and capture these states on a real conversation:

- new chat
- draft
- first route transition to `/c/<id>`
- streaming
- stopped manually
- completed
- network error/retry
- regenerated branch/variants
- image/tool/widget response


## Runtime corrections verified on 2026-07-13

- `page.keyboard.press("F5")` did not reload the document. Real X11 `F5` and `page.reload()` did. Automation must use `page.reload()`.
- `page.keyboard.press("Control+Shift+O")` did not trigger New chat, while the real X11 shortcut did. Do not use Playwright keyboard dispatch as a substitute for browser-level shortcuts.
- Two visible `create-new-chat-button` nodes exist. A normal Playwright pointer click was intercepted by layout overlays. Selecting the candidate with non-zero geometry and `pointer-events != none`, then calling DOM `click()`, cleared the composer successfully. Direct navigation to `https://chatgpt.com/` is the fallback.
- Send changed to Stop on the live page. Completed guest responses and exact request provenance were verified before anonymous quota exhaustion.
- A sequential live team workflow passed with exact outputs `PLAN_TOKEN_ALPHA`, `DEV_RECEIVED_PLAN`, `REVIEW_RECEIVED_DEV`, and `FINAL_TEAM_OK`; each downstream prompt contained the upstream role output.
- Re-running the same task resumed every round without adding messages or changing the durable ledger/checkpoint.
- Ten role tabs retained independent role, task, and page IDs after repeated New Chat, composer mutation, clear, and reload cycles.
- Cross-origin navigation retained the visible role/task title and badge; auth pages raise `AuthenticationRequiredError` and are never automated.
