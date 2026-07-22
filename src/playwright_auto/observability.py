from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ACTION_EVENT_LOG = Path(
    os.environ.get("PLAYWRIGHT_AUTO_ACTION_LOG", ".runtime/action-events.jsonl")
)


def configure_action_event_log(path: str | Path) -> Path:
    global _ACTION_EVENT_LOG
    _ACTION_EVENT_LOG = Path(path)
    return _ACTION_EVENT_LOG


def action_event_log_path() -> Path:
    return _ACTION_EVENT_LOG


def append_action_event(
    action: str,
    phase: str,
    *,
    page_url: str = "",
    page_id: str | None = None,
    role: str | None = None,
    task_id: str | None = None,
    delay_seconds: float | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    payload: dict[str, Any] = {
        "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "at_epoch": now,
        "pid": os.getpid(),
        "action": str(action),
        "phase": str(phase),
        "page_url": str(page_url or ""),
        "page_id": page_id,
        "role": role,
        "task_id": task_id,
        "delay_seconds": delay_seconds,
        "detail": detail,
    }
    path = _ACTION_EVENT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)
    return payload


def read_recent_action_events(
    path: str | Path | None = None,
    *,
    limit: int = 120,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    target = Path(path) if path is not None else _ACTION_EVENT_LOG
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def record_page_action(
    page: Any,
    action: str,
    phase: str,
    *,
    delay_seconds: float | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        metadata = await page.evaluate(
            """() => ({
              page_id: sessionStorage.getItem('playwright-auto:page-id'),
              role: sessionStorage.getItem('playwright-auto:role'),
              task_id: sessionStorage.getItem('playwright-auto:task-id'),
            })"""
        )
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return append_action_event(
        action,
        phase,
        page_url=str(getattr(page, "url", "") or ""),
        page_id=metadata.get("page_id"),
        role=metadata.get("role"),
        task_id=metadata.get("task_id"),
        delay_seconds=delay_seconds,
        detail=detail,
    )
