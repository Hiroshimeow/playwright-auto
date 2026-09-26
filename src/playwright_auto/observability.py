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

from .file_lock import atomic_replace, exclusive_file_lock, open_shared_read

_ACTION_EVENT_LOG = Path(
    os.environ.get("PLAYWRIGHT_AUTO_ACTION_LOG", ".runtime/action-events.jsonl")
)
_LIVE_EVENT_LOG = Path(
    os.environ.get("PLAYWRIGHT_AUTO_LIVE_LOG", ".runtime/live-events.jsonl")
)
_LIVE_EVENT_MAX_BYTES = 4 * 1024 * 1024
_ACTION_EVENT_MAX_BYTES = 4 * 1024 * 1024
_LIVE_EVENT_ENABLED = bool(os.environ.get("PLAYWRIGHT_AUTO_LIVE_LOG"))
_LIVE_EVENT_SIGNATURES: dict[tuple[Any, ...], str] = {}


class _AllowSummaryCorruptionError(RuntimeError):
    pass


class _AllowSummaryMissingError(_AllowSummaryCorruptionError):
    pass


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
    _LIVE_EVENT_SIGNATURES.clear()
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
        event_source = payload["source"]
        if event_source in {"LISTEN", "CTRL", "TRANSCRIPT"}:
            transcript_identity = None
            if event_source == "TRANSCRIPT":
                transcript_identity = (
                    payload["values"].get("author"),
                    payload["values"].get("turn_id") or payload["values"].get("message_id"),
                )
            identity = (
                event_source,
                payload["kind"],
                payload["page_url"],
                payload["page_id"],
                payload["role"],
                payload["task_id"],
                payload["team"],
                payload["request_id"],
                payload["generation"],
                transcript_identity,
            )
            signature = json.dumps(
                [payload["changes"], payload["values"], payload["detail"]],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if _LIVE_EVENT_SIGNATURES.get(identity) == signature:
                return payload
            _LIVE_EVENT_SIGNATURES[identity] = signature
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
    expected_task = str(task_id or "").strip()
    expected_role = str(role or "").strip().upper()
    events: list[dict[str, Any]] = []
    chunk_size = 64 * 1024
    try:
        with open_shared_read(target) as handle:
            handle.seek(0, os.SEEK_END)
            cursor = handle.tell()
            leading_fragment = b""
            first_chunk = True
            while cursor > 0 and len(events) < limit:
                read_size = min(chunk_size, cursor)
                cursor -= read_size
                handle.seek(cursor)
                chunk = handle.read(read_size)
                data = chunk + leading_fragment
                parts = data.split(b"\n")
                if first_chunk and data and not data.endswith(b"\n"):
                    parts.pop()
                first_chunk = False
                if cursor > 0:
                    leading_fragment = parts.pop(0) if parts else data
                else:
                    leading_fragment = b""
                for raw_line in reversed(parts):
                    if len(events) >= limit:
                        break
                    raw_line = raw_line.rstrip(b"\r")
                    if not raw_line:
                        continue
                    try:
                        value = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    if expected_task and str(value.get("task_id") or "") != expected_task:
                        continue
                    if expected_role:
                        event_role = str(value.get("role") or "").strip().upper()
                        if (
                            event_role != expected_role
                            and not event_role.endswith(f"-{expected_role}")
                        ):
                            continue
                    if not str(value.get("event_id") or "").strip():
                        digest = hashlib.sha256(raw_line).hexdigest()[:24]
                        value["event_id"] = f"legacy-live-{digest}"
                    events.append(value)
    except FileNotFoundError:
        return []
    events.reverse()
    return events


def _empty_allow_summary() -> dict[str, Any]:
    return {"clicks": 0, "by_tool": {}, "by_task": {}, "latest_at": None}


def _allow_summary_path(target: Path) -> Path:
    return target.with_name(f"{target.stem}.summary.json")


def _action_lock_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.lock")


def _apply_allow_event(summary: dict[str, Any], event: dict[str, Any]) -> None:
    page_url = str(event.get("page_url") or "")
    if (
        event.get("action") != "mcp_allow"
        or event.get("phase") != "complete"
        or not page_url.startswith(("https://chatgpt.com/", "https://www.chatgpt.com/"))
    ):
        return
    summary["clicks"] = int(summary.get("clicks") or 0) + 1
    detail = str(event.get("detail") or "")
    match = re.search(r"\bAllow\s+([^\r\n]+?)\s+for this conversation\b", detail, re.I)
    tool = match.group(1).lower() if match else "unknown"
    by_tool = summary.setdefault("by_tool", {})
    by_tool[tool] = int(by_tool.get(tool, 0)) + 1
    task_id = str(event.get("task_id") or "").strip()
    if task_id:
        by_task = summary.setdefault("by_task", {})
        by_task[task_id] = int(by_task.get(task_id, 0)) + 1
    at = str(event.get("at") or "").strip()
    if at:
        summary["latest_at"] = at


def _scan_allow_summary(target: Path) -> dict[str, Any]:
    summary = _empty_allow_summary()
    if not target.exists():
        return summary
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            _apply_allow_event(summary, event)
    return summary


def _write_allow_summary(target: Path, summary: dict[str, Any]) -> None:
    sidecar = _allow_summary_path(target)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "clicks": int(summary.get("clicks") or 0),
        "by_tool": dict(sorted((summary.get("by_tool") or {}).items())),
        "by_task": dict(sorted((summary.get("by_task") or {}).items())),
        "latest_at": summary.get("latest_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = sidecar.with_name(f"{sidecar.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    atomic_replace(temporary, sidecar)


def _load_allow_summary(target: Path) -> dict[str, Any]:
    sidecar = _allow_summary_path(target)
    try:
        with open_shared_read(sidecar) as handle:
            raw = handle.read().decode("utf-8")
    except FileNotFoundError as exc:
        raise _AllowSummaryMissingError(
            f"Auto-Allow aggregate is unavailable: {sidecar}"
        ) from exc
    except OSError as exc:
        raise _AllowSummaryCorruptionError(
            f"Auto-Allow aggregate is unreadable: {sidecar}"
        ) from exc

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported aggregate payload")
        clicks = payload.get("clicks")
        by_tool = payload.get("by_tool")
        by_task = payload.get("by_task")
        latest_at = payload.get("latest_at")
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 0:
            raise ValueError("clicks must be a non-negative integer")
        for name, counts in (("by_tool", by_tool), ("by_task", by_task)):
            if not isinstance(counts, dict):
                raise ValueError(f"{name} must be an object")
            if any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in counts.items()
            ):
                raise ValueError(f"{name} contains an invalid counter")
        if latest_at is not None and not isinstance(latest_at, str):
            raise ValueError("latest_at must be a string or null")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _AllowSummaryCorruptionError(
            f"Auto-Allow aggregate is corrupt: {sidecar}"
        ) from exc

    return {
        "clicks": clicks,
        "by_tool": dict(by_tool),
        "by_task": dict(by_task),
        "latest_at": latest_at,
    }


def _load_or_migrate_allow_summary(
    target: Path, *, allow_migration: bool
) -> dict[str, Any]:
    try:
        return _load_allow_summary(target)
    except _AllowSummaryMissingError:
        if not allow_migration:
            try:
                fresh_log = not target.exists() or target.stat().st_size == 0
            except OSError:
                fresh_log = False
            if not fresh_log:
                raise
    summary = _scan_allow_summary(target)
    _write_allow_summary(target, summary)
    return summary


def _trim_action_log(target: Path, *, max_bytes: int | None = None) -> None:
    max_bytes = _ACTION_EVENT_MAX_BYTES if max_bytes is None else max_bytes
    try:
        if target.stat().st_size <= max_bytes:
            return
        data = target.read_bytes()
    except FileNotFoundError:
        return
    keep = data[-(max_bytes // 2):]
    newline = keep.find(b"\n")
    if newline >= 0:
        keep = keep[newline + 1:]
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(keep)
    atomic_replace(temporary, target)


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
    target = _ACTION_EVENT_LOG
    lock_path = _action_lock_path(target)
    allow_migration = not lock_path.exists()
    with exclusive_file_lock(lock_path):
        try:
            summary = _load_or_migrate_allow_summary(
                target, allow_migration=allow_migration
            )
        except _AllowSummaryCorruptionError:
            # Aggregate truth stays unavailable, but recent evidence must remain bounded.
            _append_jsonl(target, payload)
            _trim_action_log(target)
            return payload
        _apply_allow_event(summary, payload)
        _write_allow_summary(target, summary)
        _append_jsonl(target, payload)
        _trim_action_log(target)
    return payload


def mcp_allow_click_summary(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else _ACTION_EVENT_LOG
    try:
        summary = _load_allow_summary(target)
    except _AllowSummaryCorruptionError as exc:
        return {
            "clicks": None,
            "by_tool": {},
            "by_task": {},
            "latest_at": None,
            "error": str(exc),
        }
    summary["by_tool"] = dict(sorted((summary.get("by_tool") or {}).items()))
    summary["by_task"] = dict(sorted((summary.get("by_task") or {}).items()))
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
    with open_shared_read(target) as handle:
        all_lines = handle.read().decode("utf-8", errors="replace").splitlines()
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
