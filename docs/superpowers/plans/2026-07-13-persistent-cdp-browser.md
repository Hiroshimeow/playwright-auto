# Persistent CDP Browser and KasmVNC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent Playwright-controlled Chromium on local CDP port 9222 with a live, interactive KasmVNC view on Tailscale port 9223.

**Architecture:** Python owns browser lifecycle and CDP tools. GUI mode runs Chromium on a dedicated KasmVNC display; headless mode starts only Chromium. Runtime profile and process data live under ignored `.runtime/`, while PM2 provides durable service entry points.

**Tech Stack:** Python 3.11+, uv, Playwright Python, pytest, Chromium/Chrome, KasmVNC, PM2.

## Global Constraints

- CDP binds only to `127.0.0.1:9222`.
- KasmVNC binds to `0.0.0.0:9223`, has no application password, and is intended only for the private Tailscale network.
- KasmVNC is an interactive viewer only; Playwright automation never depends on port 9223.
- Persistent Chrome profile is `.runtime/main-profile/`.
- GUI and headless modes never use the profile simultaneously.
- Streaming is replaceable by Selkies/WebRTC without changing browser or CDP interfaces.
- Do not modify `tampermonkey-auto` or `agent-mcp-gateway`.

---

### Task 1: Repository foundation and browser lifecycle

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/playwright_auto/__init__.py`, `src/playwright_auto/config.py`, `src/playwright_auto/browser.py`, `src/playwright_auto/cli.py`
- Create: `tests/test_browser.py`

**Interfaces:**
- Produces: `BrowserConfig`, `build_chromium_command()`, and CLI `start|stop|restart|status`.

- [ ] Write tests for ports, repo-local ignored profile, GUI default, headless flag, process state, and single-profile protection.
- [ ] Run `uv run pytest tests/test_browser.py -q` and confirm failure.
- [ ] Implement the minimal lifecycle and CLI.
- [ ] Run the focused tests and confirm success.
- [ ] Commit `feat: add persistent CDP browser lifecycle`.

### Task 2: CDP inspection and screenshots

**Files:**
- Create: `src/playwright_auto/connection.py`, `src/playwright_auto/inspect.py`, `src/playwright_auto/screenshot.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- Consumes: `BrowserConfig.cdp_url`.
- Produces: CDP connection, page inspection, locator candidates, and viewport/full-page/element screenshots.

- [ ] Write tests for endpoint validation, page selection, locator output, and screenshot options.
- [ ] Confirm focused tests fail.
- [ ] Implement tools with role/label/text/test-id locator priority.
- [ ] Run focused and full tests.
- [ ] Commit `feat: add CDP inspection and screenshots`.

### Task 3: KasmVNC GUI streaming boundary

**Files:**
- Create: `scripts/kasmvnc-start.sh`, `scripts/kasmvnc-stop.sh`, `ecosystem.config.cjs`
- Modify: `src/playwright_auto/config.py`, `src/playwright_auto/browser.py`
- Create: `tests/test_streaming.py`

**Interfaces:**
- Produces: dedicated display `:100`, web listener `0.0.0.0:9223`, and PM2 services independent from CDP clients.

- [ ] Write tests for display/port configuration and GUI/headless service selection.
- [ ] Confirm focused tests fail.
- [ ] Implement KasmVNC scripts and PM2 definitions with no password prompt.
- [ ] Run focused and full tests.
- [ ] Commit `feat: add interactive KasmVNC browser stream`.

### Task 4: Host installation and end-to-end verification

**Files:**
- Create: `README.md`, `scripts/smoke.py`
- Update: `uv.lock`

- [ ] Install Python dependencies, browser support, and KasmVNC packages available for the host.
- [ ] Start GUI mode and verify CDP `http://127.0.0.1:9222/json/version`.
- [ ] Verify interactive web response on `http://127.0.0.1:9223/`.
- [ ] Connect Playwright, navigate, interact, and capture a screenshot while streaming remains reachable.
- [ ] Restart headless and verify port 9223 is absent while CDP remains healthy.
- [ ] Run `uv run pytest -q`, `git diff --check`, and confirm ignored runtime data is not tracked.
- [ ] Commit `docs: add setup and runtime verification`.
- [ ] Push `main` to `origin`.
