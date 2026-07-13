# Persistent CDP Browser Design

## Goal

Create a standalone Python Playwright automation repository that controls a reusable Chromium profile through CDP on `127.0.0.1:9222`.

## Architecture

A browser lifecycle CLI starts Chromium with a persistent profile and fixed local-only CDP endpoint at `127.0.0.1:9222`. GUI mode runs Chromium on a dedicated virtual display (`DISPLAY=:100`) so KasmVNC can expose that display at `http://<tailscale-ip>:9223/`. This lets the agent drive Chromium through CDP while the user watches and can intervene with mouse and keyboard in real time.

Headless mode reuses the same profile only after the GUI instance and its display stream have stopped. Automation clients attach with Playwright over CDP rather than owning the browser process.

The streaming boundary is independent of the browser and automation layers: a future Selkies/WebRTC service may consume the same virtual display without changing the CDP endpoint, persistent profile, or Playwright clients.

## Safety and lifecycle constraints

- Bind CDP only to `127.0.0.1:9222`; never expose it directly to Tailscale or the public Internet.
- Bind KasmVNC to `0.0.0.0:9223` for access through `http://<tailscale-ip>:9223/`.
- Do not configure a KasmVNC password; access control is provided by the private Tailscale network.
- Do not expose port 9223 using Tailscale Funnel or another public tunnel.
- Store the reusable profile inside the repository at `.runtime/main-profile/` and exclude `.runtime/` from Git.
- Permit only one Chromium process to use the profile at a time.
- Support `start --gui`, `start --headless`, `stop`, `restart`, and `status`.
- GUI start owns the dedicated display and KasmVNC stream; headless start does not run streaming services.
- Preserve login state across restarts.
- Keep Tampermonkey and agent-mcp-gateway out of scope.

## Automation tools

Provide commands to inspect pages and generate robust Playwright locator candidates, take viewport/full-page/element screenshots, and connect to the current browser. Prefer role, label, text, and test-id locators before CSS or XPath.

## Verification

Tests cover command construction, profile locking, mode selection, status detection, and local CDP validation. A smoke check must start Chromium on port 9222, connect through Playwright, open a page, and capture a screenshot in both supported modes when a display is available.
