#!/usr/bin/env bash
set -euo pipefail

NOVNC_VERSION="${NOVNC_VERSION:-1.7.0}"
WEBSOCKIFY_VERSION="${WEBSOCKIFY_VERSION:-0.13.0}"
PYTHON_VERSION="${NOVNC_PYTHON_VERSION:-3.12}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUNTIME_ROOT="$REPO_ROOT/.runtime"
NOVNC_TARGET="$RUNTIME_ROOT/noVNC-$NOVNC_VERSION"
VENV_TARGET="$RUNTIME_ROOT/novnc-venv"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"

if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  echo "uv executable not found" >&2
  exit 1
fi

mkdir -p "$RUNTIME_ROOT"
TEMP_ROOT="$(mktemp -d "$RUNTIME_ROOT/novnc-install.XXXXXX")"
cleanup() {
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT INT TERM

curl -fsSL --retry 3 --max-time 120 \
  "https://github.com/novnc/noVNC/archive/refs/tags/v$NOVNC_VERSION.tar.gz" \
  -o "$TEMP_ROOT/novnc.tar.gz"
tar -xzf "$TEMP_ROOT/novnc.tar.gz" -C "$TEMP_ROOT"

"$UV_BIN" venv --python "$PYTHON_VERSION" "$TEMP_ROOT/venv"
"$UV_BIN" pip install \
  --python "$TEMP_ROOT/venv/bin/python" \
  "websockify==$WEBSOCKIFY_VERSION"

printf 'noVNC=v%s\nwebsockify=%s\n' \
  "$NOVNC_VERSION" "$WEBSOCKIFY_VERSION" \
  > "$TEMP_ROOT/noVNC-$NOVNC_VERSION/PLAYWRIGHT_AUTO_VERSION"
ln -sfn vnc.html "$TEMP_ROOT/noVNC-$NOVNC_VERSION/index.html"

rm -rf "$NOVNC_TARGET" "$VENV_TARGET"
mv "$TEMP_ROOT/noVNC-$NOVNC_VERSION" "$NOVNC_TARGET"
mv "$TEMP_ROOT/venv" "$VENV_TARGET"

"$VENV_TARGET/bin/python" - <<PY
import importlib.metadata
from pathlib import Path
assert importlib.metadata.version("websockify") == "$WEBSOCKIFY_VERSION"
assert Path("$NOVNC_TARGET/vnc.html").is_file()
print("installed noVNC v$NOVNC_VERSION and websockify $WEBSOCKIFY_VERSION")
PY
