#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KASM_ROOT="${KASM_ROOT:-$HOME/.local/opt/kasmvnc}"
exec "$KASM_ROOT/usr/bin/Xkasmvnc" :100   -geometry "${KASM_GEOMETRY:-1440x900}" -depth 24   -interface 0.0.0.0 -websocketPort 9223   -SecurityTypes None -DisableBasicAuth 1 -sslOnly 0   -httpd "$KASM_ROOT/usr/share/kasmvnc/www"   -fp /usr/share/fonts/X11/misc,/usr/share/fonts/truetype
