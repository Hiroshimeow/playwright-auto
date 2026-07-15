#!/usr/bin/env bash
set -euo pipefail
XVFB_BIN="${XVFB_BIN:-$(command -v Xvfb || true)}"
if [[ -z "$XVFB_BIN" ]]; then
  echo "Xvfb was not found. Set XVFB_BIN to an executable path." >&2
  exit 1
fi
DISPLAY_NUMBER="${DISPLAY_NUMBER:-100}"
DISPLAY=":${DISPLAY_NUMBER}"
rm -f "/tmp/.X${DISPLAY_NUMBER}-lock" "/tmp/.X11-unix/X${DISPLAY_NUMBER}"
exec "$XVFB_BIN" "$DISPLAY" \
  -screen 0 1920x1080x24 \
  +extension COMPOSITE \
  +extension DAMAGE \
  +extension GLX \
  +extension RANDR \
  +extension RENDER \
  +extension MIT-SHM \
  +extension XFIXES \
  +extension XTEST \
  +iglx +render -nolisten tcp -ac -noreset
