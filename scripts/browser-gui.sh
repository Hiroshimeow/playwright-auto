#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROFILE_DIR="${PLAYWRIGHT_PROFILE_DIR:-$HOME/Workspace/playwright-profile}"
DEFAULT_URL="${PLAYWRIGHT_DEFAULT_URL:-https://chatgpt.com/}"
CHROMIUM_BIN="${CHROMIUM_BIN:-}"
# Keep system/Tailscale routing untouched. Only Chromium uses the local WARP
# SOCKS proxy; sensitive/working destinations remain direct.
PROXY_SERVER="${PLAYWRIGHT_PROXY_SERVER:-socks5://127.0.0.1:40000}"
PROXY_BYPASS_LIST="${PLAYWRIGHT_PROXY_BYPASS_LIST:-localhost;127.0.0.1;[::1];100.64.0.0/10;192.168.0.0/16;*.ts.net;*.hcu-lab.me;chatgpt.com;*.chatgpt.com;openai.com;*.openai.com;*.oaistatic.com;*.oaiusercontent.com;ashbyhq.com;*.ashbyhq.com;google.com;*.google.com;*.googleusercontent.com;*.gstatic.com}"

if [[ -z "$CHROMIUM_BIN" ]]; then
  for candidate in chromium chromium-browser google-chrome google-chrome-stable; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CHROMIUM_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$CHROMIUM_BIN" && -x /snap/bin/chromium ]]; then
  CHROMIUM_BIN=/snap/bin/chromium
fi
if [[ -z "$CHROMIUM_BIN" || ! -x "$CHROMIUM_BIN" ]]; then
  echo "Chromium was not found. Set CHROMIUM_BIN to an executable path." >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"
chmod 700 "$PROFILE_DIR"
export DISPLAY="${DISPLAY:-:100}"
export GTK_THEME="${GTK_THEME:-Adwaita:dark}"

# Starting ChatGPT as Chromium's command-line URL can break native X11/VNC
# keyboard input in this display. Start neutral, then open ChatGPT through CDP.
(
  for _ in $(seq 1 60); do
    if TARGETS="$(curl -fsS http://127.0.0.1:9222/json/list 2>/dev/null)"; then
      BLANK_ID="$(python3 -c 'import json,sys; pages=json.load(sys.stdin); print(next((p["id"] for p in pages if p.get("type")=="page" and p.get("url")=="about:blank"), ""))' <<<"$TARGETS")"
      curl -fsS -X PUT "http://127.0.0.1:9222/json/new?$DEFAULT_URL" >/dev/null
      sleep 0.2
      if [[ -n "$BLANK_ID" ]]; then
        curl -fsS -X PUT "http://127.0.0.1:9222/json/close/$BLANK_ID" >/dev/null 2>&1 || true
      fi
      exit 0
    fi
    sleep 0.2
  done
) &

exec "$CHROMIUM_BIN" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-dev-shm-usage \
  --proxy-server="$PROXY_SERVER" \
  --proxy-bypass-list="$PROXY_BYPASS_LIST" \
  --force-dark-mode \
  --blink-settings=preferredColorScheme=0 \
  about:blank
