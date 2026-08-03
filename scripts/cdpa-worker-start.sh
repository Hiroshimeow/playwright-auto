#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m playwright_auto.cdpa_worker \
  --repository "$PWD" \
  --config "${CDPA_CONFIG:-cdpa.yaml}"
