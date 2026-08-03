#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
NOVNC_ROOT="${NOVNC_ROOT:-$REPO_ROOT/.runtime/noVNC-1.7.0}"
WEBSOCKIFY_BIN="${WEBSOCKIFY_BIN:-$REPO_ROOT/.runtime/novnc-venv/bin/websockify}"
NGINX_BIN="${NGINX_BIN:-$(command -v nginx || true)}"
VNC_PORT="${PLAYWRIGHT_VNC_PORT:-5901}"
WEBSOCKET_PORT="${PLAYWRIGHT_WEBSOCKET_PORT:-9226}"
WEB_PORT="${PLAYWRIGHT_VIEWER_PORT:-9223}"
NGINX_CONFIG="$REPO_ROOT/.runtime/novnc-nginx.conf"
NGINX_PID_FILE="$REPO_ROOT/.runtime/novnc-nginx.pid"

if [[ ! -f "$NOVNC_ROOT/vnc.html" ]]; then
  echo "noVNC assets missing at $NOVNC_ROOT" >&2
  exit 1
fi
if [[ ! -x "$WEBSOCKIFY_BIN" ]]; then
  echo "websockify executable missing at $WEBSOCKIFY_BIN" >&2
  exit 1
fi
if [[ -z "$NGINX_BIN" || ! -x "$NGINX_BIN" ]]; then
  echo "nginx executable not found" >&2
  exit 1
fi

ln -sfn vnc.html "$NOVNC_ROOT/index.html"
cat > "$NOVNC_ROOT/mandatory.json" <<'JSON'
{
  "autoconnect": true,
  "reconnect": true,
  "reconnect_delay": 3000,
  "resize": "scale",
  "shared": true,
  "view_only": false,
  "path": "websockify",
  "quality": 6,
  "compression": 2
}
JSON

cat > "$NGINX_CONFIG" <<NGINX
worker_processes 1;
pid $NGINX_PID_FILE;
error_log stderr warn;

events {
  worker_connections 1024;
}

http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log off;
  sendfile on;

  server {
    listen 0.0.0.0:$WEB_PORT;
    server_name _;
    root $NOVNC_ROOT;
    index index.html;

    location = /websockify {
      proxy_pass http://127.0.0.1:$WEBSOCKET_PORT;
      proxy_http_version 1.1;
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 86400;
      proxy_send_timeout 86400;
    }

    location / {
      try_files \$uri \$uri/ =404;
    }
  }
}
NGINX

for _ in $(seq 1 60); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$VNC_PORT") 2>/dev/null; then
    break
  fi
  sleep 1
done
if ! (exec 3<>"/dev/tcp/127.0.0.1/$VNC_PORT") 2>/dev/null; then
  echo "VNC backend 127.0.0.1:$VNC_PORT did not become ready" >&2
  exit 1
fi

"$NGINX_BIN" -t -p "$REPO_ROOT/" -c "$NGINX_CONFIG"
"$WEBSOCKIFY_BIN" --heartbeat 30 "127.0.0.1:$WEBSOCKET_PORT" "127.0.0.1:$VNC_PORT" &
WEBSOCKIFY_PID=$!

cleanup() {
  if [[ -n "${WEBSOCKIFY_PID:-}" ]]; then
    kill "$WEBSOCKIFY_PID" 2>/dev/null || true
    wait "$WEBSOCKIFY_PID" 2>/dev/null || true
  fi
  if [[ -n "${NGINX_PID:-}" ]]; then
    kill "$NGINX_PID" 2>/dev/null || true
    wait "$NGINX_PID" 2>/dev/null || true
  fi
}
NGINX_PID=""
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$WEBSOCKET_PORT") 2>/dev/null; then
    break
  fi
  if ! kill -0 "$WEBSOCKIFY_PID" 2>/dev/null; then
    wait "$WEBSOCKIFY_PID"
    exit $?
  fi
  sleep 0.2
done

"$NGINX_BIN" -p "$REPO_ROOT/" -c "$NGINX_CONFIG" -g 'daemon off;' &
NGINX_PID=$!

wait -n "$WEBSOCKIFY_PID" "$NGINX_PID"
STATUS=$?
exit "$STATUS"
