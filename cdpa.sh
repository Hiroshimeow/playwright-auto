#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m playwright_auto.cdpa_cli "$@" --config "$ROOT/cdpa.yaml"
