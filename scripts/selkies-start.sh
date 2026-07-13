#!/usr/bin/env bash
set -euo pipefail
SELKIES_ROOT="${SELKIES_ROOT:-$HOME/.local/opt/selkies-gstreamer}"
export DISPLAY=:100
export ENABLE_XVFB=false
export ENABLE_PULSEAUDIO=false
exec "$SELKIES_ROOT/selkies-gstreamer-run" --addr=0.0.0.0 --port=9223 --enable_https=false --enable_basic_auth=false --enable_resize=true --encoder=x264enc --framerate=30
