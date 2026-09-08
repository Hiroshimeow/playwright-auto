from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_ACTION_EVENT_LOG = Path(
    os.environ.get("PLAYWRIGHT_AUTO_ACTION_LOG", ".runtime/action-events.jsonl")
)
_LIVE_EVENT_LOG = Path(
    os.environ.get("PLAYWRIGHT_AUTO_LIVE_LOG", ".runtime/live-events.jsonl")
)
_LIVE_EVENT_MAX_BYTES = 4 * 1024 * 1024
_LIVE_EVENT_ENABLED = bool(os.environ.get("PLAYWRIGHT_AUTO_LIVE_LOG"))


def configure_action_event_log(path: str | Path) -> Path:
    global _ACTION_EVENT_LOG
    _ACTION_EVENT_LOG = Path(path)
    return _ACTION_EVENT_LOG


def action_event_log_path() -> Path:
    return _ACTION_EVENT_LOG


def configure_live_event_log(path: str | Path, *, enabled: bool = True) -> Path:
    global _LIVE_EVENT_LOG, _LIVE_EVENT_ENABLED
    _LIVE_EVENT_LOG = Path(path)
    _LIVE_EVENT_ENABLED = bool(enabled)
    return _LIVE_EVENT_LOG


def live_event_log_path() -> Path:
    return _LIVE_EVENT_LOG


def live_event_logging_enabled() -> bool:
    return _LIVE_EVENT_ENABLED


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, encoded)
    finally:
        os.close(descriptor)


def _trim_live_log(path: Path, *, max_bytes: int = _LIVE_EVENT_MAX_BYTES) -> None:
    try:
        if path.stat().st_size <= max_bytes:
            return
        data = path.read_bytes()
    except FileNotFoundError:
        return
    keep = data[-(max_bytes // 2) :]
    newline = keep.find(b"\n")
    if newline >= 0:
        keep = keep[newline + 1 :]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(keep)
    os.replace(temporary, path)


def append_live_event(
    source: str,
    kind: str,
    *,
    page_url: str = "",
    page_id: str | None = None,
    role: str | None = None,
    task_id: str | None = None,
    team: str | None = None,
    request_id: str | None = None,
    generation: int | None = None,
    changes: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    payload: dict[str, Any] = {
        "event_id": f"live-{uuid4().hex}",
        "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "at_epoch": now,
        "pid": os.getpid(),
        "source": str(source).upper(),
        "kind": str(kind),
        "page_url": str(page_url or ""),
        "page_id": page_id,
        "role": role,
        "task_id": task_id,
        "team": team,
        "request_id": request_id,
        "generation": generation,
        "changes": dict(changes or {}),
        "values": dict(values or {}),
        "detail": detail,
    }
    if _LIVE_EVENT_ENABLED:
        _append_jsonl(_LIVE_EVENT_LOG, payload)
        _trim_live_log(_LIVE_EVENT_LOG)
    return payload


def read_recent_live_events(
    path: str | Path | None = None,
    *,
    limit: int = 200,
    task_id: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    target = Path(path) if path is not None else _LIVE_EVENT_LOG
    if not target.exists():
        return []
    expected_task = str(task_id or "").strip()
    expected_role = str(role or "").strip().upper()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        target.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if expected_task and str(value.get("task_id") or "") != expected_task:
            continue
        if expected_role:
            event_role = str(value.get("role") or "").strip().upper()
            if event_role != expected_role and not event_role.endswith(f"-{expected_role}"):
                continue
        if not str(value.get("event_id") or "").strip():
            value["event_id"] = (
                f"legacy-live-{line_number}-{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}"
            )
        events.append(value)
    return events[-limit:]


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
        "event_id": f"event-{uuid4().hex}",
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
    _append_jsonl(_ACTION_EVENT_LOG, payload)
    return payload


def mcp_allow_click_summary(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else _ACTION_EVENT_LOG
    summary: dict[str, Any] = {
        "clicks": 0,
        "by_tool": {},
        "by_task": {},
        "latest_at": None,
    }
    if not target.exists():
        return summary
    tool_pattern = re.compile(r"\bAllow\s+(mcp-[A-Za-z0-9._-]+)\s+for this conversation\b", re.I)
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        page_url = str(event.get("page_url") or "")
        if (
            event.get("action") != "mcp_allow"
            or event.get("phase") != "complete"
            or not page_url.startswith(("https://chatgpt.com/", "https://www.chatgpt.com/"))
        ):
            continue
        summary["clicks"] += 1
        detail = str(event.get("detail") or "")
        match = tool_pattern.search(detail)
        tool = match.group(1).lower() if match else "unknown"
        summary["by_tool"][tool] = int(summary["by_tool"].get(tool, 0)) + 1
        task_id = str(event.get("task_id") or "").strip()
        if task_id:
            summary["by_task"][task_id] = int(summary["by_task"].get(task_id, 0)) + 1
        at = str(event.get("at") or "").strip()
        if at:
            summary["latest_at"] = at
    summary["by_tool"] = dict(sorted(summary["by_tool"].items()))
    summary["by_task"] = dict(sorted(summary["by_task"].items()))
    return summary


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
    all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, len(all_lines) - limit)
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(all_lines[start:], start=start + 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            if not str(value.get("event_id") or "").strip():
                value["event_id"] = (
                    f"legacy-{line_number}-{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}"
                )
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
              team: sessionStorage.getItem('playwright-auto:team'),
            })"""
        )
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    page_url = str(getattr(page, "url", "") or "")
    event = append_action_event(
        action,
        phase,
        page_url=page_url,
        page_id=metadata.get("page_id"),
        role=metadata.get("role"),
        task_id=metadata.get("task_id"),
        delay_seconds=delay_seconds,
        detail=detail,
    )
    append_live_event(
        "ACTION",
        f"{action}:{phase}",
        page_url=page_url,
        page_id=metadata.get("page_id"),
        role=metadata.get("role"),
        task_id=metadata.get("task_id"),
        team=metadata.get("team"),
        values={
            "action": str(action),
            "phase": str(phase),
            **({"delay_seconds": delay_seconds} if delay_seconds is not None else {}),
        },
        detail=detail,
    )
    return event
