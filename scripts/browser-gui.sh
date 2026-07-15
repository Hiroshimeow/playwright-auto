#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/.runtime/main-profile"

CHROMIUM_BIN="${CHROMIUM_BIN:-}"
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
if [[ -z "$CHROMIUM_BIN" ]]; then
  echo "Chromium was not found. Set CHROMIUM_BIN to an executable path." >&2
  exit 1
fi

export DISPLAY="${DISPLAY:-:100}"
exec "$CHROMIUM_BIN" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$ROOT/.runtime/main-profile" \
  --no-first-run \
  --no-default-browser-check \
  --disable-dev-shm-usage \
  about:blank
