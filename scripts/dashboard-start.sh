#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

args=(
  --host "${DASHBOARD_HOST:-0.0.0.0}"
  --event-log "${DASHBOARD_EVENT_LOG:-.runtime/action-events.jsonl}"
  --config "${CDPA_CONFIG:-cdpa.yaml}"
)
[[ -n "${DASHBOARD_PORT:-}" ]] && args+=(--port "$DASHBOARD_PORT")
[[ -n "${CDP_URL:-}" ]] && args+=(--cdp "$CDP_URL")
[[ -n "${DASHBOARD_POLL:-}" ]] && args+=(--poll "$DASHBOARD_POLL")

exec .venv/bin/python -m playwright_auto.dashboard "${args[@]}"
