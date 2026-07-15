#!/usr/bin/env bash
set -euo pipefail
SELKIES_ROOT="${SELKIES_ROOT:-$HOME/.local/opt/selkies-gstreamer}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
WEB_ROOT="$REPO_ROOT/.runtime/selkies-web-video-only"
PYTHON_ROOT="$REPO_ROOT/.runtime/selkies-python-video-only"
RTC_CONFIG="$REPO_ROOT/config/selkies-rtc-tailscale.json"

SELKIES_PYTHON_PACKAGE="${SELKIES_PYTHON_PACKAGE:-}"
if [[ -z "$SELKIES_PYTHON_PACKAGE" ]]; then
  mapfile -t SELKIES_PACKAGE_CANDIDATES < <(
    find "$SELKIES_ROOT/lib" \
      -type d \
      -path '*/site-packages/selkies_gstreamer' \
      -print 2>/dev/null | sort
  )
  if (( ${#SELKIES_PACKAGE_CANDIDATES[@]} == 0 )); then
    printf 'Selkies Python package was not found under %s/lib\n' "$SELKIES_ROOT" >&2
    exit 1
  fi
  SELKIES_PYTHON_PACKAGE="${SELKIES_PACKAGE_CANDIDATES[${#SELKIES_PACKAGE_CANDIDATES[@]}-1]}"
fi

python3 "$SCRIPT_DIR/prepare_selkies_web.py" \
  --source "$SELKIES_ROOT/share/selkies-web" \
  --output "$WEB_ROOT" >/dev/null
python3 "$SCRIPT_DIR/prepare_selkies_python.py" \
  --source "$SELKIES_PYTHON_PACKAGE" \
  --output "$PYTHON_ROOT" >/dev/null

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
LAN_IP="$(hostname -I 2>/dev/null | tr ' ' '
' | grep -Ev '^(127\.|100\.|172\.)' | head -n 1 || true)"
ICE_ADDRESSES="127.0.0.1"
if [[ -n "$TAILSCALE_IP" ]]; then ICE_ADDRESSES+=",$TAILSCALE_IP"; fi
if [[ -n "$LAN_IP" ]]; then ICE_ADDRESSES+=",$LAN_IP"; fi

export DISPLAY=:100
export ENABLE_XVFB=false
export ENABLE_PULSEAUDIO=false
export SELKIES_DISABLE_AUDIO=true
export SELKIES_SIGNAL_RETRY_SECONDS=0.25
export SELKIES_ICE_UDP_ONLY=true
export SELKIES_ALLOWED_ICE_ADDRESSES="$ICE_ADDRESSES"
export PYTHONPATH="$PYTHON_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$SELKIES_ROOT/selkies-gstreamer-run" \
  --addr=0.0.0.0 \
  --port=9223 \
  --web_root="$WEB_ROOT" \
  --rtc_config_json="$RTC_CONFIG" \
  --enable_https=false \
  --enable_basic_auth=false \
  --enable_resize=true \
  --encoder=x264enc \
  --framerate=15 \
  --video_bitrate=1200 \
  --audio_bitrate=16000
