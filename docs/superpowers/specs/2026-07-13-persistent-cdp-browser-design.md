# Persistent CDP Browser Design

## Goal

Create a standalone Python Playwright automation repository that controls a reusable Chromium profile through CDP on `127.0.0.1:9222`.

## Architecture

A browser lifecycle CLI starts Chromium with a persistent profile and a fixed local-only CDP port. GUI mode is the default for manual login and daily use; headless mode reuses the same profile only after the GUI instance has stopped. Automation clients attach with Playwright over CDP rather than owning the browser process.

## Safety and lifecycle constraints

- Bind CDP only to `127.0.0.1:9222`; never expose it directly to the public Internet.
- Store the reusable profile outside the repository at `~/.local/share/playwright-auto/main-profile`.
- Permit only one Chromium process to use the profile at a time.
- Support `start --gui`, `start --headless`, `stop`, `restart`, and `status`.
- Preserve login state across restarts.
- Keep Tampermonkey and agent-mcp-gateway out of scope.

## Automation tools

Provide commands to inspect pages and generate robust Playwright locator candidates, take viewport/full-page/element screenshots, and connect to the current browser. Prefer role, label, text, and test-id locators before CSS or XPath.

## Verification

Tests cover command construction, profile locking, mode selection, status detection, and local CDP validation. A smoke check must start Chromium on port 9222, connect through Playwright, open a page, and capture a screenshot in both supported modes when a display is available.
