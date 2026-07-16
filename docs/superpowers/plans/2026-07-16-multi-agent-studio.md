# Multi-Agent Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a cross-platform Tkinter control studio for existing ChatGPT tabs, ordered collaborative execution, live status, response viewing, prompt editing, and safe intervention.

**Architecture:** A deterministic model layer feeds a background asyncio controller that owns CDP and ChatGPTPage objects. Tkinter runs only on the main thread and communicates with the controller using thread-safe commands and an event queue. Existing browser safety and durable send APIs remain authoritative.

**Tech Stack:** Python 3.11+, tkinter/ttk, asyncio, threading, Playwright CDP, existing playwright-auto ChatGPTPage APIs, pytest.

## Global Constraints

- Do not call `browser.close()` on the persistent CDP browser.
- Do not delete drafts, attachments, dialogs, conversations, or unrelated tabs automatically.
- Use no new runtime dependency beyond existing Playwright.
- Keep all runtime layouts, events, and run records under `.runtime/studio/`.
- Windows and Linux must both import, test, and launch the application.

---

### Task 1: Deterministic studio domain model

**Files:**
- Create: `src/playwright_auto/studio/__init__.py`
- Create: `src/playwright_auto/studio/models.py`
- Test: `tests/test_studio_models.py`

**Interfaces:**
- Produces: `WorkerStatus`, `RunStatus`, `WorkerModel`, `StudioSettings`, `StudioState`, `StudioEvent`, `render_worker_prompt(...)`, `reorder_pending_workers(...)`, `slug_task_id(...)`.

- [ ] Write failing tests for status serialization, prompt rendering, duplicate-role detection, pending-only reorder, response history, and JSON persistence.
- [ ] Run `uv run pytest -q tests/test_studio_models.py` and verify failure because the module is absent.
- [ ] Implement immutable validation helpers and mutable runtime models with explicit `to_dict/from_dict` methods.
- [ ] Run the model tests and verify pass.

### Task 2: Browser backend and controller

**Files:**
- Create: `src/playwright_auto/studio/controller.py`
- Modify: `src/playwright_auto/chatgpt.py` only if a focused release-role helper is required.
- Test: `tests/test_studio_controller.py`

**Interfaces:**
- Consumes: model interfaces from Task 1 and existing `connect`, `ChatGPTPage`, `ensure_role_indicator`.
- Produces: `StudioController.start()`, `shutdown()`, `connect()`, `refresh_tabs()`, `assign_role()`, `release_role()`, `reorder()`, `update_prompt()`, `start_run()`, `pause_run()`, `resume_run()`, `stop_worker()`, `stop_run()`, `poll_events()`.

- [ ] Write fake-client tests for discovery, role mutation, sequential response propagation, pause-after-current, stop, queued prompt edit, active edit-and-retry, duplicate-role blocking, and disconnect handling.
- [ ] Run controller tests and verify failure.
- [ ] Implement one background event loop and thread-safe command submission.
- [ ] Implement browser discovery without opening or closing tabs.
- [ ] Implement collaborative execution with exact worker order and existing `send/wait_for_response` APIs.
- [ ] Persist layout, events, and run records atomically.
- [ ] Run controller and regression tests.

### Task 3: Tkinter widgets and visual design

**Files:**
- Create: `src/playwright_auto/studio/theme.py`
- Create: `src/playwright_auto/studio/widgets.py`
- Create: `src/playwright_auto/studio/app.py`
- Test: `tests/test_studio_ui.py`

**Interfaces:**
- Consumes: controller methods and event/view models.
- Produces: `StudioApp(root, controller)`, `WorkerCard`, `ScrollableWorkerList`, `LogNotebook`, `ResponseDialog`, `PromptDialog`.

- [ ] Write a hidden-root smoke test and widget behavior tests for card state, drag reorder callback, dynamic log tabs, and bounded log content.
- [ ] Implement dark compact ttk theme with responsive three-column layout.
- [ ] Implement draggable cards, pulsing active border, status chips, elapsed timer, and action buttons.
- [ ] Implement prompt editor with queued save and active `Stop & Retry` option.
- [ ] Implement response/history dialog and per-worker logs.
- [ ] Run UI tests.

### Task 4: CLI entry point, docs, and persistence UX

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Create: `scripts/studio_smoke.py`
- Test: `tests/test_studio_cli.py`

**Interfaces:**
- Produces: `playwright-studio` console command and `python -m playwright_auto.studio.app` fallback.

- [ ] Add failing CLI/help and layout-path tests.
- [ ] Add the console entry point.
- [ ] Document Windows and Linux launch, controls, intervention semantics, and runtime files.
- [ ] Add a no-send CDP smoke script.
- [ ] Run CLI tests and build package.

### Task 5: Verification and live operational test

**Files:**
- Modify only defects found by verification.

- [ ] Run `uv run pytest -q` on Windows.
- [ ] Run compile, JS syntax, `git diff --check`, and `uv build`.
- [ ] Run `uv run playwright-studio --help`.
- [ ] Run the no-send CDP smoke against port 9222 and verify current tabs are discovered without role or URL mutation.
- [ ] Launch the Tk app, capture a screenshot, and inspect the visual layout.
- [ ] Perform a controlled authenticated two-worker run with a fresh task ID, verify response propagation, pause/stop behavior, and no duplicate send.
- [ ] Re-run full tests after any fixes.
- [ ] Commit and push the feature branch after verification.
