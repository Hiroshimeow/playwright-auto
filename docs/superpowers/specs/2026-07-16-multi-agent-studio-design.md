# Multi-Agent Studio Desktop UI Design

## Goal

Add a compact Tkinter desktop application that controls the existing persistent ChatGPT tabs on CDP port 9222. The application is the primary operational surface for assigning roles, ordering workers, starting collaborative tasks, observing per-tab output, and intervening safely while a task is running.

## Scope

The first complete version provides:

- Connect/disconnect and refresh for CDP `http://127.0.0.1:9222`.
- Discovery of existing ChatGPT tabs without opening or closing unrelated tabs.
- Role assignment and release on an existing physical tab.
- Draggable worker cards whose order defines the collaboration sequence.
- Per-worker prompt templates with a global goal and previous worker outputs.
- Sequential multi-worker execution using the existing `ChatGPTPage` safety API.
- Start, pause-after-current, resume, stop-all, and stop-one-worker controls.
- Animated green border for the active worker.
- Per-worker status, elapsed time, latest response, and response history.
- A right-side log notebook containing All, System, and one tab per worker.
- Editing a queued worker prompt immediately. Editing an active worker stores the new template for the next attempt; `Stop & Retry` stops the current response and requeues that worker with the edited prompt.
- Durable run state and event logs under `.runtime/studio/`.
- Windows and Linux support using only the Python standard library plus the existing Playwright dependency.

## Non-goals

- No embedded browser rendering; the existing browser/viewer remains separate.
- No arbitrary graph editor in this version. Worker order is a sequential pipeline.
- No hidden overwrite of manual composer text, attachments, or dialogs.
- No automatic rerouting when the user changes a leased role during execution.
- No third-party Tkinter theme dependency.

## Architecture

### Desktop layer

`playwright_auto.studio.app.StudioApp` owns Tkinter widgets and never calls Playwright directly. It consumes immutable view models and posts commands to a controller. All UI mutations happen on the Tk main thread through a polling event queue.

### Controller layer

`playwright_auto.studio.controller.StudioController` owns one background asyncio thread, the persistent CDP connection, tab registry, run state, event queue, and cancellation flags. It exposes thread-safe commands for discovery, role mutation, reorder, prompt editing, start/pause/resume/stop, and response retrieval.

### Domain layer

`playwright_auto.studio.models` defines worker state, run status, event types, prompt rendering, reorder validation, and serialization. These functions are deterministic and covered by unit tests without Tk or a browser.

### Browser operations

Each discovered ChatGPT page is wrapped with `ChatGPTPage`. A role mutation uses the existing role indicator API. A collaborative run prepares one task ID across all selected workers, then sends one prompt at a time in the current worker order. The prompt contains:

1. Global goal.
2. Worker role and ordinal.
3. Worker-specific prompt template.
4. Truncated outputs from prior workers.
5. A request to return a concrete result for the next worker.

The controller uses `send()` and `wait_for_response()` so prompt provenance, page ownership, manual-input protection, and send recovery remain fail-closed.

## UI layout

### Top bar

- Connection state and CDP endpoint.
- Refresh tabs.
- Global goal entry.
- Start, Pause/Resume, and Stop buttons.
- Compact run status and elapsed time.

### Left column: role library and run settings

- Preset roles: PLAN, DEV, REVIEW, TEST, plus Add custom role.
- Task ID, rounds, response timeout, and context character limit.
- Save/load workspace layout.

### Center column: ordered workers

- Scrollable draggable worker cards.
- Drag handle, order number, role combobox, page title, page ID, state chip, prompt preview, and action buttons.
- Buttons: Edit Prompt, View Response, Stop, Release.
- Active card uses a pulsing green border; errors use red; waiting uses amber.

### Right column: logs and system status

- Notebook tabs: All, System, then one tab per worker.
- Text widgets are read-only, append-only, timestamped, and capped to prevent unbounded UI growth.
- System panel shows connection, discovered tabs, duplicate roles, active task, active worker, and durable run path.

## State and persistence

`.runtime/studio/layout.json` stores worker order, prompt templates, settings, and last global goal. `.runtime/studio/runs/<task-id>.json` stores run metadata, worker statuses, prompt text actually sent, responses, errors, and timestamps. `.runtime/studio/events.jsonl` stores append-only operational events.

Runtime state is never committed.

## Intervention semantics

- **Pause:** prevents the next worker from starting; it does not interrupt an accepted request.
- **Resume:** continues from the next queued worker.
- **Stop worker:** if that worker is actively responding, calls `ChatGPTPage.stop()`; otherwise removes it from the current run queue only.
- **Stop all:** stops the active response when possible and cancels remaining queued workers.
- **Edit queued prompt:** applies immediately.
- **Edit active prompt:** updates the template for the next attempt. `Stop & Retry` explicitly stops the active response and requeues that worker.
- **Reorder during a run:** updates only workers that have not started. The active and completed prefix remains fixed.

## Error handling

- Duplicate non-empty roles block Start.
- Unassigned, disconnected, login-required, manual-draft, attachment, dialog, and ownership-drift states are surfaced on the worker card and in logs.
- No automatic draft deletion, attachment removal, unknown-dialog dismissal, or tab close.
- CDP disconnect changes the run to blocked and preserves state for reconnect.
- Rate-limit errors are logged and leave the worker retryable; the studio does not bypass existing durable safeguards.

## Testing

- Unit tests for models, prompt rendering, reorder behavior, state transitions, persistence, and log truncation.
- Controller tests with fake browser clients for discovery, role assignment, sequential collaboration, pause/stop, edit-and-retry, and error propagation.
- Tk smoke test creates and destroys the app under a hidden root without entering `mainloop`.
- Live CDP smoke test discovers current tabs, assigns no roles, and performs no sends.
- Manual visual test verifies drag/drop, glow animation, dialogs, response viewer, and log tabs.

## Success criteria

- The app starts with `uv run playwright-studio` on Windows and Linux.
- Existing ChatGPT tabs appear within two seconds after Connect.
- Role changes made in the app are visible in the browser badge and CLI list.
- Dragging cards changes the next execution order and persists it.
- A two-worker authenticated run completes sequentially and the second prompt contains the first response.
- Stop, pause, prompt edit, response viewer, and per-worker logs operate without closing the persistent browser or losing unrelated tabs.
