# Persistent CDP Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Python Playwright CLI controlling a persistent Chromium profile over local CDP port 9222.

**Architecture:** A lifecycle module owns Chromium process discovery and startup. Separate Playwright clients attach over CDP for inspection and screenshots, keeping browser lifetime independent from each automation command.

**Tech Stack:** Python 3.11+, uv, Playwright Python, pytest, Chromium.

## Global Constraints

- CDP endpoint is `http://127.0.0.1:9222`.
- Persistent profile is `~/.local/share/playwright-auto/main-profile`.
- GUI is the default mode.
- GUI and headless must never use the profile simultaneously.
- CDP must never bind publicly.
- Do not modify tampermonkey-auto or agent-mcp-gateway.

---

### Task 1: Project and lifecycle CLI

**Files:**
- Create: `pyproject.toml`
- Create: `src/playwright_auto/config.py`
- Create: `src/playwright_auto/browser.py`
- Create: `src/playwright_auto/cli.py`
- Test: `tests/test_browser.py`

**Interfaces:**
- Produces: `BrowserConfig`, `build_chromium_command(config, headless)`, and CLI commands `start|stop|restart|status`.

- [ ] Write failing tests for fixed loopback port, persistent profile, GUI default, headless flag, and single-profile protection.
- [ ] Run `uv run pytest tests/test_browser.py -q` and confirm failure.
- [ ] Implement minimal lifecycle code and CLI.
- [ ] Run the focused test and confirm it passes.
- [ ] Commit as `feat: add persistent CDP browser lifecycle`.

### Task 2: CDP inspection and screenshots

**Files:**
- Create: `src/playwright_auto/connection.py`
- Create: `src/playwright_auto/inspect.py`
- Create: `src/playwright_auto/screenshot.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `BrowserConfig.cdp_url`.
- Produces: async CDP connection, page inspection output, and screenshot commands.

- [ ] Write failing tests for endpoint validation, page selection, locator output, and screenshot option mapping.
- [ ] Run `uv run pytest tests/test_tools.py -q` and confirm failure.
- [ ] Implement Playwright CDP connection, inspection, and screenshot behavior.
- [ ] Run focused and full tests.
- [ ] Commit as `feat: add CDP inspection and screenshots`.

### Task 3: Runtime setup and verification

**Files:**
- Create: `README.md`
- Create: `.gitignore`
- Create: `scripts/smoke.py`

**Interfaces:**
- Consumes: lifecycle and CDP tools.
- Produces: documented setup and executable end-to-end smoke check.

- [ ] Install locked dependencies with `uv sync` and Chromium support required by the host.
- [ ] Verify `start --headless`, `status`, CDP connection, navigation, screenshot, and `stop`.
- [ ] Verify GUI startup when `DISPLAY` is available.
- [ ] Run `uv run pytest -q`.
- [ ] Commit as `docs: add setup and smoke verification`.

### Task 4: Publish

**Files:** none.

- [ ] Add remote `origin` as `https://github.com/Hiroshimeow/playwright-auto.git`.
- [ ] Confirm `git status --short` is clean and branch is `main`.
- [ ] Push with `git push -u origin main`.
