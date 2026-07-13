#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/.runtime/main-profile"
export DISPLAY=:100
exec /snap/bin/chromium   --remote-debugging-address=127.0.0.1   --remote-debugging-port=9222   --user-data-dir="$ROOT/.runtime/main-profile"   --no-first-run --no-default-browser-check --disable-dev-shm-usage about:blank
