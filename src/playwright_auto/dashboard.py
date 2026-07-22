from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

from .cdpa_config import CDPAConfigError, load_cdpa_config
from .cdpa_store import TaskStore
from .cdpa_team import normalize_team_base
from .chatgpt import ChatGPTPage
from .connection import connect, validate_cdp_url
from .observability import read_recent_action_events

DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")
SUPPORTED_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com", "auth.openai.com"})
_SAFE_TERMINAL_TAB_STATES = frozenset({"waiting_prompt", "new_chat"})

TASK_CONTROLS = [
    "pause",
    "resume",
    "retry",
    "stop",
    "restart_role",
    "open_tab",
    "new_chat",
    "route_plan",
    "clear_team",
]

def _supported_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in SUPPORTED_HOSTS
    except ValueError:
        return False


def dashboard_has_error(raw: Mapping[str, Any]) -> bool:
    if raw.get("retry_visible"):
        return True
    markers = (
        "something went wrong",
        "try again",
        "failed",
        "error",
        "unable to",
        "too many requests",
        "temporarily limited",
    )
    return any(
        marker in str(text).strip().casefold()
        for text in raw.get("error_texts") or []
        for marker in markers
        if str(text).strip()
    )


def _state_from_raw(raw: Mapping[str, Any]) -> str:
    if dashboard_has_error(raw):
        return "error"
    if raw.get("buttons", {}).get("stop", {}).get("visible"):
        return "responding"
    if str(raw.get("composer_text") or "").strip():
        return "draft"
    if raw.get("requires_login"):
        return "auth_required"
    last_role = str(raw.get("last_message_role") or "")
    if last_role == "user":
        return "submitting"
    if last_role == "assistant":
        return "waiting_prompt"
    if raw.get("composer_present"):
        return "new_chat"
    return "unknown"


def build_dashboard_page(raw: Mapping[str, Any]) -> dict[str, Any]:
    role = str(raw.get("role") or "") or None
    page_id = str(raw.get("page_id") or "") or None
    short_page_id = page_id if page_id and len(page_id) <= 12 else (page_id or "no-page-id")[:8]
    normalized_buttons: dict[str, dict[str, bool]] = {}
    for name, value in dict(raw.get("buttons") or {}).items():
        value = value if isinstance(value, Mapping) else {}
        normalized_buttons[str(name)] = {
            "visible": bool(value.get("visible")),
            "enabled": bool(value.get("enabled")),
        }
    composer_text = str(raw.get("composer_text") or "")
    return {
        "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""),
        "page_id": page_id,
        "role": role,
        "team": str(raw.get("team") or "") or None,
        "task_id": str(raw.get("task_id") or "") or None,
        "identity": f"{role or 'UNASSIGNED'} · {short_page_id}",
        "state": _state_from_raw(raw),
        "composer_editable": bool(raw.get("composer_editable")),
        "composer_preview": composer_text[:180],
        "composer_length": len(composer_text),
        "message_count": int(raw.get("message_count") or 0),
        "assistant_count": int(raw.get("assistant_count") or 0),
        "user_count": int(raw.get("user_count") or 0),
        "dialogs": [str(item)[:260] for item in raw.get("dialogs") or []],
        "buttons": normalized_buttons,
    }



def dashboard_payload(
    *,
    connected: bool,
    cdp_url: str,
    pages: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    error: str | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        (dict(page) for page in pages),
        key=lambda page: (str(page.get("role") or "~"), str(page.get("page_id") or "~")),
    )
    return {
        "connected": bool(connected),
        "cdp_url": cdp_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(ordered),
        "pages": ordered,
        "events": [dict(event) for event in events],
        "error": error,
    }


def _column(raw: Mapping[str, Any]) -> str:
    status = str(raw.get("status") or "INBOX").upper()
    if status in {"DONE", "STOPPED"}:
        return "DONE_STOPPED"
    if status in {"PAUSED", "BLOCKED"}:
        return status
    value = str(raw.get("kanban_column") or status).upper().replace("/", "_")
    return value if value in {"INBOX", "PLANNING", "WORKING", "VERIFYING", "PAUSED", "BLOCKED", "DONE_STOPPED"} else "WORKING"


def _task_surface(
    raw: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    *,
    connected: bool,
) -> tuple[str, str]:
    status = str(raw.get("status") or "INBOX").upper()
    cleanup = raw.get("cleanup") if isinstance(raw.get("cleanup"), Mapping) else {}
    roles = raw.get("roles") if isinstance(raw.get("roles"), Mapping) else {}
    exact_live = any(
        str(page.get("team") or "") == str(raw.get("team") or "")
        and str(page.get("task_id") or "") == str(raw.get("task_id") or "")
        and any(
            isinstance(record, Mapping)
            and str(record.get("physical_role") or "") == str(page.get("role") or "")
            for record in roles.values()
        )
        for page in pages
        if isinstance(page, Mapping)
    )
    terminal_or_cleared = status in {"DONE", "STOPPED"} or cleanup.get("state") == "CLEARED"
    if terminal_or_cleared:
        if not connected:
            return "offline_recoverable", "unknown"
        if exact_live:
            availability = (
                "cleared_tab_present"
                if cleanup.get("state") == "CLEARED"
                else "terminal_tab_present"
            )
            return "offline_recoverable", availability
        return "history", "terminal"
    if not connected:
        return "active", "unknown"
    if exact_live:
        return "active", "online"
    return "offline_recoverable", "offline"


def build_task_payload(
    raw: Mapping[str, Any],
    *,
    pages: Sequence[Mapping[str, Any]] = (),
    connected: bool = False,
) -> dict[str, Any]:
    roles_raw = raw.get("roles") if isinstance(raw.get("roles"), Mapping) else {}
    roles = [
        {
            "logical_role": str(role),
            "physical_role": str(value.get("physical_role") or role),
            "status": str(value.get("status") or "unknown"),
            "turn": int(value.get("turn") or 0),
            "page_id": value.get("page_id"),
            "page_url": value.get("page_url"),
            "online": bool(value.get("online")),
            "conversation_generation": int(value.get("conversation_generation") or 0),
            "constructor_sent_generation": value.get("constructor_sent_generation"),
            "reset_requested": bool(value.get("reset_requested")),
            "last_error": value.get("last_error"),
            "last_activity_at": value.get("last_activity_at"),
        }
        for role, value in roles_raw.items()
        if isinstance(value, Mapping)
    ]
    hops_raw = raw.get("hops") if isinstance(raw.get("hops"), list) else []
    active_hop_id = raw.get("active_hop_id")
    active_hop = next((dict(hop) for hop in hops_raw if isinstance(hop, Mapping) and hop.get("hop_id") == active_hop_id), None)
    reports = []
    for index, report in enumerate(raw.get("reports") or [], start=1):
        if not isinstance(report, Mapping):
            continue
        reports.append({
            **dict(report),
            "report_id": str(report.get("report_id") or index),
            "url": f"/api/reports/{raw.get('task_id')}/{report.get('report_id') or index}",
        })
    surface, availability = _task_surface(raw, pages, connected=connected)
    return {
        "task_id": str(raw.get("task_id") or ""),
        "task_title": str(raw.get("task_text") or raw.get("task_title") or "").splitlines()[0],
        "task_text": str(raw.get("task_text") or ""),
        "task_slug": str(raw.get("task_slug") or ""),
        "repository": str(raw.get("repository") or ""),
        "manifest_path": str(raw.get("manifest_path") or ""),
        "team": str(raw.get("team") or ""),
        "team_suffix": int(raw.get("team_suffix") or 1),
        "status": str(raw.get("status") or "INBOX"),
        "column": _column(raw),
        "surface": surface,
        "availability": availability,
        "surface_warning": (
            "Cleanup is marked CLEARED but an assigned role tab is still present; run Clear Team again."
            if availability == "cleared_tab_present"
            else "Terminal task still has an assigned role tab; clear the team to close it."
            if availability == "terminal_tab_present"
            else None
        ),
        "terminal_state": raw.get("terminal_state"),
        "active_role": raw.get("active_role"),
        "active_hop_id": active_hop_id,
        "active_hop": active_hop,
        "active_action": raw.get("active_action"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "started_at": raw.get("started_at"),
        "completed_at": raw.get("completed_at"),
        "stopped_at": raw.get("stopped_at"),
        "last_role_activity_at": raw.get("last_role_activity_at"),
        "pause_reason": raw.get("pause_reason"),
        "block_code": raw.get("block_code"),
        "block_retryable": bool(raw.get("block_retryable")),
        "block_reason": raw.get("block_reason"),
        "stop_reason": raw.get("stop_reason"),
        "cleanup": dict(raw.get("cleanup") or {}),
        "roles": roles,
        "reports": reports,
        "route_timeline": [dict(item) for item in raw.get("route_timeline") or [] if isinstance(item, Mapping)],
        "errors": [dict(item) if isinstance(item, Mapping) else str(item) for item in raw.get("errors") or []],
        "control_results": [dict(item) for item in raw.get("controls") or [] if isinstance(item, Mapping)],
        "controls": list(TASK_CONTROLS),
        "options": dict(raw.get("options") or {}),
    }


class DashboardStore:
    def __init__(self, cdp_url: str) -> None:
        self._lock = threading.RLock()
        self._payload = dashboard_payload(
            connected=False,
            cdp_url=cdp_url,
            pages=[],
            events=[],
            error="Connecting to CDP",
        )

    def replace(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._payload = dict(payload)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._payload, ensure_ascii=False))


class DashboardMonitor:
    def __init__(
        self,
        store: DashboardStore,
        *,
        cdp_url: str,
        event_log: Path,
        poll_seconds: float = 0.5,
        reconnect_seconds: float = 2.0,
    ) -> None:
        if poll_seconds <= 0 or reconnect_seconds <= 0:
            raise ValueError("dashboard timing values must be positive")
        self.store = store
        self.cdp_url = validate_cdp_url(cdp_url)
        self.event_log = event_log
        self.poll_seconds = poll_seconds
        self.reconnect_seconds = reconnect_seconds

    async def inspect_pages(self, browser: Any) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for context in browser.contexts:
            for page in context.pages:
                if page.is_closed() or not _supported_url(page.url):
                    continue
                try:
                    snapshot = await ChatGPTPage(page).snapshot()
                    title = await page.title()
                except Exception:
                    continue
                messages = snapshot.messages
                raw = {
                    "url": snapshot.url,
                    "title": title,
                    "page_id": snapshot.page_id,
                    "role": snapshot.page_role,
                    "team": snapshot.page_team,
                    "task_id": snapshot.page_task_id,
                    "composer_present": snapshot.composer_present,
                    "composer_editable": snapshot.composer_editable,
                    "composer_text": snapshot.composer_text,
                    "requires_login": snapshot.requires_login,
                    "retry_visible": bool(snapshot.error_texts),
                    "error_texts": snapshot.error_texts,
                    "last_message_role": messages[-1].role if messages else "",
                    "message_count": len(messages),
                    "assistant_count": sum(item.role == "assistant" for item in messages),
                    "user_count": sum(item.role == "user" for item in messages),
                    "dialogs": snapshot.blocking_dialogs,
                    "buttons": {
                        "stop": {
                            "visible": snapshot.stop_visible,
                            "enabled": snapshot.stop_visible,
                        },
                        "send": {
                            "visible": snapshot.send_visible,
                            "enabled": snapshot.send_enabled,
                        },
                    },
                }
                pages.append(build_dashboard_page(raw))
        return pages

    async def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            playwright = None
            try:
                playwright, browser = await connect(self.cdp_url)
                while browser.is_connected() and not stop_event.is_set():
                    pages = await self.inspect_pages(browser)
                    events = read_recent_action_events(self.event_log, limit=160)
                    self.store.replace(
                        dashboard_payload(
                            connected=True,
                            cdp_url=self.cdp_url,
                            pages=pages,
                            events=events,
                            error=None,
                        )
                    )
                    await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.replace(
                    dashboard_payload(
                        connected=False,
                        cdp_url=self.cdp_url,
                        pages=[],
                        events=read_recent_action_events(self.event_log, limit=160),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                if playwright is not None:
                    await playwright.stop()
            if not stop_event.is_set():
                await asyncio.sleep(self.reconnect_seconds)


def _busy_role_suffixes(
    task_store: TaskStore,
    pages: Sequence[Mapping[str, Any]],
    *,
    team_base: str | None = None,
    connected: bool = True,
) -> tuple[int, ...]:
    if not str(team_base or "").strip():
        return ()
    base = normalize_team_base(str(team_base))
    role_pattern = re.compile(
        rf"^{re.escape(base)}-(?:plan|dev|review|test|audit)(\d*)$",
        re.IGNORECASE,
    )
    tasks, _errors = task_store.discover_with_errors()
    terminal_teams = {
        (str(task.get("team") or ""), str(task.get("task_id") or ""))
        for task in tasks
        if str(task.get("status") or "").upper() in {"DONE", "STOPPED"}
    }
    busy: set[int] = set()
    if not connected:
        for task in tasks:
            if str(task.get("team_base") or "") != base:
                continue
            if str(task.get("status") or "").upper() not in {"DONE", "STOPPED"}:
                continue
            if task.get("cleanup", {}).get("cleared_at"):
                continue
            roles = task.get("roles") if isinstance(task.get("roles"), Mapping) else {}
            if any(
                isinstance(record, Mapping) and record.get("page_id")
                for record in roles.values()
            ):
                busy.add(int(task.get("team_suffix") or 1))
    for page in pages:
        match = role_pattern.fullmatch(str(page.get("role") or ""))
        if match is None:
            continue
        suffix = int(match.group(1) or 1)
        identity = (
            str(page.get("team") or ""),
            str(page.get("task_id") or ""),
        )
        state = str(page.get("state") or "unknown")
        if identity not in terminal_teams or state not in _SAFE_TERMINAL_TAB_STATES:
            busy.add(suffix)
    return tuple(sorted(busy))


def _validated_report_bytes(
    task_store: TaskStore,
    task: Mapping[str, Any],
    report: Mapping[str, Any],
) -> bytes:
    stored = Path(str(report.get("path") or "")).expanduser()
    candidate = stored if stored.is_absolute() else task_store.config.repository_root / stored
    if candidate.is_symlink():
        raise ValueError("report provenance mismatch: symlink is not allowed")
    resolved = candidate.resolve(strict=True)
    team_root = (task_store.config.plans_root / str(task["team"])).resolve()
    resolved.relative_to(team_root)
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    body = resolved.read_bytes()
    expected_sha = str(report.get("sha256") or "")
    expected_size = int(report.get("size") or -1)
    if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha:
        raise ValueError("report provenance mismatch: content changed after validation")
    return body


def _find_task(task_store: TaskStore, task_id: str) -> dict[str, Any]:
    tasks, _errors = task_store.discover_with_errors()
    matches = [task for task in tasks if task.get("task_id") == task_id]
    if not matches:
        raise KeyError(task_id)
    if len(matches) > 1:
        raise ValueError(f"duplicate task ID {task_id!r}")
    return matches[0]


def _handler(
    store: DashboardStore,
    html: bytes,
    *,
    task_store: TaskStore | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                "application/json; charset=utf-8",
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
            )

        def _read_json(self) -> Mapping[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length <= 0 or length > 1_048_576:
                raise ValueError("request body must be between 1 byte and 1 MiB")
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("request JSON must be an object")
            return value

        def do_GET(self) -> None:  # noqa: N802
            path = unquote(self.path.split("?", 1)[0])
            if path == "/":
                self._send(200, "text/html; charset=utf-8", html)
                return
            if path == "/api/state":
                self._json(200, store.snapshot())
                return
            if path == "/api/tasks":
                if task_store is None:
                    self._json(503, {"error": "CDPA task store unavailable"})
                    return
                raw_tasks, manifest_errors = task_store.discover_with_errors()
                live = store.snapshot()
                pages = [page for page in live.get("pages") or [] if isinstance(page, Mapping)]
                connected = bool(live.get("connected"))
                tasks = [
                    build_task_payload(task, pages=pages, connected=connected)
                    for task in raw_tasks
                ]
                tasks.sort(key=lambda task: str(task.get("updated_at") or ""), reverse=True)
                self._json(200, {
                    "tasks": tasks,
                    "active": [task for task in tasks if task["surface"] == "active"],
                    "offline_recoverable": [task for task in tasks if task["surface"] == "offline_recoverable"],
                    "history": [task for task in tasks if task["surface"] == "history"],
                    "errors": manifest_errors,
                    "availability": "known" if connected else "unknown",
                    "repository": str(task_store.config.repository_root),
                })
                return
            if path.startswith("/api/tasks/"):
                if task_store is None:
                    self._json(503, {"error": "CDPA task store unavailable"})
                    return
                task_id = path.removeprefix("/api/tasks/")
                try:
                    live = store.snapshot()
                    self._json(
                        200,
                        build_task_payload(
                            _find_task(task_store, task_id),
                            pages=[page for page in live.get("pages") or [] if isinstance(page, Mapping)],
                            connected=bool(live.get("connected")),
                        ),
                    )
                except KeyError:
                    self._json(404, {"error": "task not found"})
                except Exception as exc:
                    self._json(409, {"error": f"{type(exc).__name__}: {exc}"})
                return
            if path.startswith("/api/reports/"):
                if task_store is None:
                    self._json(503, {"error": "CDPA task store unavailable"})
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    self._json(404, {"error": "report not found"})
                    return
                _, _, task_id, report_id = parts
                try:
                    task = _find_task(task_store, task_id)
                    reports = [item for item in task.get("reports") or [] if isinstance(item, Mapping)]
                    report = next(
                        item for index, item in enumerate(reports, start=1)
                        if str(item.get("report_id") or index) == report_id
                    )
                    body = _validated_report_bytes(task_store, task, report)
                    self._send(200, "text/markdown; charset=utf-8", body)
                except ValueError as exc:
                    self._json(409, {"error": str(exc)})
                except (KeyError, StopIteration, FileNotFoundError):
                    self._json(404, {"error": "report not found"})
                return
            if path == "/health":
                payload = store.snapshot()
                task_store_ready = task_store is not None
                self._json(
                    200 if task_store_ready else 503,
                    {
                        "ok": task_store_ready,
                        "task_store_ready": task_store_ready,
                        "cdp_connected": payload["connected"],
                    },
                )
                return
            self._send(404, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self) -> None:  # noqa: N802
            path = unquote(self.path.split("?", 1)[0])
            if task_store is None:
                self._json(503, {"error": "CDPA task store unavailable"})
                return
            try:
                body = self._read_json()
                if path in {"/api/tasks", "/api/tasks/resume"}:
                    requested_repository = Path(
                        str(body.get("repository") or task_store.config.repository_root)
                    ).expanduser().resolve()
                    if requested_repository != task_store.config.repository_root:
                        raise ValueError(
                            "task repository must match the dashboard CDPA repository"
                        )
                    if path == "/api/tasks/resume":
                        if body.get("new_roles") or body.get("new_all"):
                            raise ValueError("new_roles and new_all are invalid when resuming")
                        team = body.get("team")
                        if not isinstance(team, str) or not team:
                            raise ValueError("resume requires an exact team string")
                        task = task_store.resume_team(team, reason="resume requested")
                        self._json(202, build_task_payload(task))
                        return
                    live_snapshot = store.snapshot()
                    live_pages = live_snapshot.get("pages") or []
                    task = task_store.create_task(
                        str(body.get("task") or ""),
                        requested_team=str(body.get("team") or "").strip() or None,
                        new_roles=tuple(body.get("new_roles") or ()),
                        new_all=bool(body.get("new_all")),
                        repository=requested_repository,
                        reserved_team_suffixes=_busy_role_suffixes(
                            task_store,
                            [page for page in live_pages if isinstance(page, Mapping)],
                            team_base=str(body.get("team") or "").strip() or None,
                            connected=bool(live_snapshot.get("connected")),
                        ),
                    )
                    self._json(201, build_task_payload(task))
                    return
                if path.startswith("/api/tasks/") and path.endswith("/controls"):
                    task_id = path[len("/api/tasks/") : -len("/controls")].strip("/")
                    task = _find_task(task_store, task_id)
                    updated = task_store.request_control(
                        task["manifest_path"],
                        str(body.get("action") or ""),
                        role=str(body.get("role") or "").strip() or None,
                        reason=str(body.get("reason") or "").strip() or None,
                        confirmed=bool(body.get("confirmed")),
                    )
                    self._json(202, updated)
                    return
                self._json(404, {"error": "not found"})
            except KeyError:
                self._json(404, {"error": "task not found"})
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def _dashboard_html(poll_seconds: float) -> bytes:
    if poll_seconds <= 0:
        raise ValueError("dashboard poll interval must be positive")
    marker = "setInterval(refreshDashboard, 1000);"
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    if html.count(marker) != 1:
        raise RuntimeError("dashboard polling marker is missing or duplicated")
    return html.replace(
        marker,
        f"setInterval(refreshDashboard, {max(1, round(poll_seconds * 1000))});",
    ).encode("utf-8")


def serve_dashboard(
    *,
    host: str,
    port: int,
    cdp_url: str,
    event_log: Path,
    poll_seconds: float,
    reconnect_seconds: float,
    task_store: TaskStore | None = None,
) -> None:
    html = _dashboard_html(poll_seconds)
    store = DashboardStore(cdp_url)
    stop_event = threading.Event()
    monitor = DashboardMonitor(
        store,
        cdp_url=cdp_url,
        event_log=event_log,
        poll_seconds=poll_seconds,
        reconnect_seconds=reconnect_seconds,
    )
    monitor_thread = threading.Thread(
        target=lambda: asyncio.run(monitor.run(stop_event)),
        name="playwright-dashboard-monitor",
        daemon=True,
    )
    monitor_thread.start()
    server = ThreadingHTTPServer((host, port), _handler(store, html, task_store=task_store))
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        monitor_thread.join(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description="CDPA Kanban and persistent ChatGPT tab observer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--cdp", default=None)
    parser.add_argument("--event-log", default=".runtime/action-events.jsonl")
    parser.add_argument("--poll", type=float, default=None)
    parser.add_argument("--reconnect", type=float, default=2.0)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    try:
        config = load_cdpa_config(args.config, repository_root=Path.cwd())
    except CDPAConfigError as exc:
        print(f"dashboard: CDPAConfigError: {exc}", file=sys.stderr)
        return 2
    task_store = TaskStore(config)
    serve_dashboard(
        host=args.host,
        port=args.port if args.port is not None else config.dashboard_port,
        cdp_url=args.cdp if args.cdp is not None else config.cdp_url,
        event_log=Path(args.event_log),
        poll_seconds=(
            args.poll if args.poll is not None else config.dashboard_poll_seconds
        ),
        reconnect_seconds=args.reconnect,
        task_store=task_store,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
