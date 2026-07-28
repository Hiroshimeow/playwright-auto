#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m playwright_auto.dashboard \
  --host "${DASHBOARD_HOST:-0.0.0.0}" \
  --config "${CDPA_CONFIG:-cdpa.yaml}"
