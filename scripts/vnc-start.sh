#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NAME="${PLAYWRIGHT_DISPLAY:-:100}"
VNC_PORT="${PLAYWRIGHT_VNC_PORT:-5901}"
X11VNC_BIN="${X11VNC_BIN:-$(command -v x11vnc || true)}"

if [[ -z "$X11VNC_BIN" || ! -x "$X11VNC_BIN" ]]; then
  echo "x11vnc executable not found" >&2
  exit 1
fi

for _ in $(seq 1 60); do
  if DISPLAY="$DISPLAY_NAME" xdpyinfo >/dev/null 2>&1; then
    exec "$X11VNC_BIN" \
      -display "$DISPLAY_NAME" \
      -rfbport "$VNC_PORT" \
      -localhost \
      -forever \
      -shared \
      -nopw \
      -xkb
  fi
  sleep 1
done

echo "display $DISPLAY_NAME did not become ready" >&2
exit 1
