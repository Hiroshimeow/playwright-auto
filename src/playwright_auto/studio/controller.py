from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from ..chatgpt import (
    PAGE_ID_STORAGE_KEY,
    ROLE_STORAGE_KEY,
    TASK_ID_STORAGE_KEY,
    WINDOW_NAME_PREFIX,
    ChatGPTPage,
    PageBinding,
    validate_page_role,
)
from ..connection import connect as connect_cdp
from ..durable_blocks import DurableSendBlock
from ..file_lock import fsync_parent_directory
from ..role_indicator import ensure_role_indicator
from ..workflow import WorkflowContext
from .models import (
    RunStatus,
    StudioEvent,
    StudioSettings,
    StudioState,
    WorkerModel,
    WorkerStatus,
    duplicate_roles,
    render_worker_prompt,
    reorder_pending_workers,
    slug_task_id,
    utc_now_iso,
)

_SUPPORTED_HOSTS = {"chatgpt.com", "www.chatgpt.com", "auth.openai.com"}


@dataclass(frozen=True)
class DiscoveredTab:
    page_id: str
    title: str
    url: str
    role: str | None
    browser_state: str
    requires_login: bool


class StudioBackend(Protocol):
    async def connect(self, cdp_url: str) -> None: ...

    async def disconnect(self) -> None: ...

    async def discover(self) -> list[DiscoveredTab]: ...

    async def assign_role(self, page_id: str, role: str) -> DiscoveredTab: ...

    async def release_role(self, page_id: str) -> DiscoveredTab: ...

    async def prepare_task(self, page_id: str, task_id: str) -> None: ...

    async def send_and_wait(
        self,
        page_id: str,
        prompt: str,
        timeout_ms: int,
        request_context: dict[str, object],
    ) -> str: ...

    async def stop(self, page_id: str) -> None: ...


class PlaywrightStudioBackend:
    """Persistent CDP backend. Disconnecting never closes the browser."""

    def __init__(
        self,
        *,
        timeout_ms: int = 15_000,
        ledger_path: str | Path = ".runtime/studio/request-ledger.json",
    ) -> None:
        self.timeout_ms = timeout_ms
        self.ledger_path = Path(ledger_path)
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._cdp_url: str | None = None
        self._pages: dict[str, Any] = {}
        self._clients: dict[str, ChatGPTPage] = {}

    def configure_runtime(self, runtime_dir: str | Path) -> None:
        self.ledger_path = Path(runtime_dir) / "request-ledger.json"

    async def connect(self, cdp_url: str) -> None:
        if self._browser is not None and self._browser.is_connected() and self._cdp_url == cdp_url:
            return
        await self.disconnect()
        self._playwright, self._browser = await connect_cdp(cdp_url)
        self._cdp_url = cdp_url

    async def disconnect(self) -> None:
        self._pages.clear()
        self._clients.clear()
        self._browser = None
        self._cdp_url = None
        if self._playwright is not None:
            playwright, self._playwright = self._playwright, None
            await playwright.stop()

    async def _force_unique_page_id(self, page: Any) -> str:
        page_id = await page.evaluate(
            """([roleKey, pageIdKey, taskIdKey, windowNamePrefix]) => {
              const pageId = crypto.randomUUID();
              const role = sessionStorage.getItem(roleKey);
              const taskId = sessionStorage.getItem(taskIdKey);
              sessionStorage.setItem(pageIdKey, pageId);
              window.name = windowNamePrefix + JSON.stringify({
                role: role || null,
                pageId,
                taskId: taskId || null,
              });
              return pageId;
            }""",
            [ROLE_STORAGE_KEY, PAGE_ID_STORAGE_KEY, TASK_ID_STORAGE_KEY, WINDOW_NAME_PREFIX],
        )
        await ensure_role_indicator(page, expected_page_id=page_id)
        return str(page_id)

    async def discover(self) -> list[DiscoveredTab]:
        if self._browser is None or not self._browser.is_connected():
            raise RuntimeError("CDP browser is not connected")
        pages: dict[str, Any] = {}
        clients: dict[str, ChatGPTPage] = {}
        result: list[DiscoveredTab] = []
        for context in self._browser.contexts:
            for page in context.pages:
                if page.is_closed():
                    continue
                hostname = (urlparse(page.url).hostname or "").lower()
                if hostname not in _SUPPORTED_HOSTS:
                    continue
                indicator = await ensure_role_indicator(page)
                client = ChatGPTPage(page, timeout_ms=self.timeout_ms)
                snapshot = await client.snapshot()
                page_id = str(snapshot.page_id or indicator.get("pageId") or "").strip()
                if not page_id:
                    continue
                if page_id in pages:
                    page_id = await self._force_unique_page_id(page)
                    snapshot = await client.snapshot()
                role = str(snapshot.page_role).strip() if snapshot.page_role else None
                if role:
                    role = validate_page_role(role)
                    client.binding = PageBinding(page_id, role)
                pages[page_id] = page
                clients[page_id] = client
                try:
                    title = await page.title()
                except Exception:
                    title = "ChatGPT"
                result.append(
                    DiscoveredTab(
                        page_id=page_id,
                        title=title or "ChatGPT",
                        url=page.url,
                        role=role,
                        browser_state=snapshot.state.value,
                        requires_login=snapshot.requires_login,
                    )
                )
        self._pages = pages
        self._clients = clients
        return result

    def _page(self, page_id: str) -> Any:
        try:
            return self._pages[page_id]
        except KeyError as exc:
            raise KeyError(f"ChatGPT tab {page_id!r} is not discovered") from exc

    def _client(self, page_id: str, *, require_binding: bool = True) -> ChatGPTPage:
        try:
            client = self._clients[page_id]
        except KeyError as exc:
            raise KeyError(f"ChatGPT tab {page_id!r} is not discovered") from exc
        if require_binding and client.binding is None:
            raise RuntimeError(f"ChatGPT tab {page_id!r} has no role")
        return client

    async def _describe(self, page_id: str) -> DiscoveredTab:
        page = self._page(page_id)
        client = self._client(page_id, require_binding=False)
        snapshot = await client.snapshot()
        try:
            title = await page.title()
        except Exception:
            title = "ChatGPT"
        return DiscoveredTab(
            page_id=str(snapshot.page_id or page_id),
            title=title or "ChatGPT",
            url=page.url,
            role=(str(snapshot.page_role) if snapshot.page_role else None),
            browser_state=snapshot.state.value,
            requires_login=snapshot.requires_login,
        )

    async def assign_role(self, page_id: str, role: str) -> DiscoveredTab:
        page = self._page(page_id)
        current = self._client(page_id, require_binding=False)
        assigned = await current.set_role(role, allow_rebind=True)
        actual_id = assigned["page_id"]
        if actual_id != page_id:
            self._pages.pop(page_id, None)
            self._clients.pop(page_id, None)
            page_id = actual_id
            self._pages[page_id] = page
        self._clients[page_id] = current
        return await self._describe(page_id)

    async def release_role(self, page_id: str) -> DiscoveredTab:
        page = self._page(page_id)
        await ensure_role_indicator(page)
        result = await page.evaluate(
            """() => {
              const api = window.__PLAYWRIGHT_AUTO_ROLE_INDICATOR__;
              if (!api) throw new Error('role indicator API is unavailable');
              return api.releaseRole({source: 'studio'});
            }"""
        )
        actual_id = str(result.get("pageId") or page_id)
        client = ChatGPTPage(page, timeout_ms=self.timeout_ms)
        if actual_id != page_id:
            self._pages.pop(page_id, None)
            self._clients.pop(page_id, None)
            page_id = actual_id
            self._pages[page_id] = page
        self._clients[page_id] = client
        return await self._describe(page_id)

    async def prepare_task(self, page_id: str, task_id: str) -> None:
        client = self._client(page_id)
        async with client.workflow_guard():
            await client.prepare_task(task_id)

    async def send_and_wait(
        self,
        page_id: str,
        prompt: str,
        timeout_ms: int,
        request_context: dict[str, object],
    ) -> str:
        client = self._client(page_id)
        local_context = WorkflowContext(client=client, variables={})
        block = DurableSendBlock(
            prompt,
            ledger_path=self.ledger_path,
            source_context=dict(request_context),
            role_prompt_hash=json.dumps(
                request_context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            wait_for_response=True,
            max_attempts=2,
            recovery_reload=True,
            response_timeout_ms=timeout_ms,
            stable_ms=1_000,
            poll_ms=100,
            active_reload_after_ms=min(timeout_ms // 2, 180_000),
            block_id="studio_durable_send",
        )
        async with client.workflow_guard():
            result = await block.run(local_context)
        response = result.get("response")
        if not isinstance(response, dict) or not str(response.get("text") or "").strip():
            raise RuntimeError("durable Studio response is missing text")
        return str(response["text"])

    async def stop(self, page_id: str) -> None:
        # Stop must be able to interrupt send_and_wait while that operation owns
        # the workflow guard. ChatGPTPage.stop() already serializes the real DOM
        # mutation with its mutation guard and verifies page ownership.
        client = self._client(page_id)
        await client.stop(timeout_ms=15_000)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_parent_directory(path)


class StudioEngine:
    def __init__(
        self,
        backend: StudioBackend,
        *,
        runtime_dir: str | Path = ".runtime/studio",
        event_sink: Callable[[StudioEvent, dict[str, Any]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.runtime_dir = Path(runtime_dir)
        configure_runtime = getattr(self.backend, "configure_runtime", None)
        if callable(configure_runtime):
            configure_runtime(self.runtime_dir)
        self.layout_path = self.runtime_dir / "layout.json"
        self.events_path = self.runtime_dir / "events.jsonl"
        self.runs_dir = self.runtime_dir / "runs"
        self.event_sink = event_sink
        self.state = self._load_layout()
        self.connected = False
        self._run_task: asyncio.Task[None] | None = None
        self._active_operation: asyncio.Task[str] | None = None
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._stop_requested = False
        self._skip_pages: set[str] = set()
        self._retry_pages: set[str] = set()

    def _load_layout(self) -> StudioState:
        if not self.layout_path.is_file():
            return StudioState()
        try:
            state = StudioState.from_dict(json.loads(self.layout_path.read_text(encoding="utf-8")))
        except Exception:
            return StudioState()
        state.run_status = RunStatus.IDLE
        state.active_page_id = None
        for worker in state.workers:
            worker.connected = False
            worker.status = WorkerStatus.IDLE
        return state

    def _emit(
        self,
        kind: str,
        message: str,
        *,
        worker: WorkerModel | None = None,
        payload: dict[str, Any] | None = None,
    ) -> StudioEvent:
        event = StudioEvent(
            kind=kind,
            message=message,
            page_id=worker.page_id if worker else None,
            role=worker.role if worker else None,
            payload=payload or {},
        )
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        if self.event_sink is not None:
            self.event_sink(event, self.state.to_dict())
        return event

    def _persist(self, *, event: tuple[str, str, WorkerModel | None] | None = None) -> None:
        self.state.updated_at = utc_now_iso()
        _atomic_write_json(self.layout_path, self.state.to_dict())
        if self.state.settings.task_id:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", self.state.settings.task_id).strip("._")
            if safe:
                _atomic_write_json(self.runs_dir / f"{safe}.json", self.state.to_dict())
        if event:
            self._emit(event[0], event[1], worker=event[2])
        elif self.event_sink is not None:
            self.event_sink(
                StudioEvent(kind="state", message="state updated"),
                self.state.to_dict(),
            )

    def snapshot(self) -> StudioState:
        return StudioState.from_dict(self.state.to_dict())

    async def connect(self) -> StudioState:
        await self.backend.connect(self.state.settings.cdp_url)
        self.connected = True
        self._emit("system", f"Connected to {self.state.settings.cdp_url}")
        return await self.refresh_tabs()

    async def disconnect(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            await self.stop_run()
            await self.wait_for_run()
        await self.backend.disconnect()
        self.connected = False
        for worker in self.state.workers:
            worker.connected = False
            if worker.status not in {WorkerStatus.COMPLETED, WorkerStatus.STOPPED}:
                worker.status = WorkerStatus.DISCONNECTED
        self._persist(event=("system", "Disconnected from CDP", None))

    async def refresh_tabs(self) -> StudioState:
        tabs = await self.backend.discover()
        old = {item.page_id: item for item in self.state.workers}
        merged: list[WorkerModel] = []
        for tab in tabs:
            existing = old.pop(tab.page_id, None)
            if existing is None:
                existing = WorkerModel(
                    page_id=tab.page_id,
                    title=tab.title,
                    url=tab.url,
                    role=tab.role,
                    order=len(merged),
                    enabled=bool(tab.role),
                )
            existing.title = tab.title
            existing.url = tab.url
            existing.role = tab.role
            existing.connected = True
            existing.browser_state = tab.browser_state
            existing.requires_login = tab.requires_login
            if existing.status is WorkerStatus.DISCONNECTED:
                existing.status = WorkerStatus.IDLE
            merged.append(existing)
        for missing in old.values():
            missing.connected = False
            missing.status = WorkerStatus.DISCONNECTED
            merged.append(missing)
        merged.sort(key=lambda item: item.order)
        self.state.workers = merged
        self.state.normalize_order()
        self._persist(event=("tabs", f"Discovered {len(tabs)} ChatGPT tab(s)", None))
        return self.snapshot()

    def _worker(self, page_id: str) -> WorkerModel:
        for worker in self.state.workers:
            if worker.page_id == page_id:
                return worker
        raise KeyError(page_id)

    async def assign_role(self, page_id: str, role: str) -> WorkerModel:
        role = validate_page_role(role)
        current = self._worker(page_id)
        described = await self.backend.assign_role(page_id, role)
        current.role = described.role
        current.title = described.title
        current.url = described.url
        current.browser_state = described.browser_state
        current.requires_login = described.requires_login
        current.enabled = True
        self._persist(event=("role", f"Assigned role {role}", current))
        return WorkerModel.from_dict(current.to_dict())

    async def release_role(self, page_id: str) -> WorkerModel:
        current = self._worker(page_id)
        described = await self.backend.release_role(page_id)
        current.role = None
        current.title = described.title
        current.url = described.url
        current.enabled = False
        self._persist(event=("role", "Released role", current))
        return WorkerModel.from_dict(current.to_dict())

    def set_enabled(self, page_id: str, enabled: bool) -> None:
        worker = self._worker(page_id)
        worker.enabled = bool(enabled)
        self._persist(event=("worker", f"Worker {'enabled' if enabled else 'disabled'}", worker))

    def reorder(self, page_id: str, target_index: int) -> None:
        self.state.workers = reorder_pending_workers(self.state.workers, page_id, target_index)
        self._persist(event=("order", f"Moved worker to position {target_index + 1}", self._worker(page_id)))

    async def update_prompt(
        self,
        page_id: str,
        prompt_template: str,
        *,
        stop_and_retry: bool = False,
    ) -> None:
        worker = self._worker(page_id)
        template = prompt_template.strip()
        if not template:
            raise ValueError("worker prompt must not be empty")
        worker.prompt_template = template
        self._persist(event=("prompt", "Updated worker prompt", worker))
        if stop_and_retry:
            if self.state.active_page_id != page_id:
                raise ValueError("Stop & Retry is only valid for the active worker")
            worker.pending_retry = True
            self._retry_pages.add(page_id)
            await self._stop_active(page_id)

    def update_settings(self, settings: StudioSettings) -> None:
        settings.validate()
        self.state.settings = settings
        self._persist(event=("settings", "Updated run settings", None))

    def _selected_workers(self) -> list[WorkerModel]:
        selected = [item for item in self.state.workers if item.enabled]
        if not selected:
            raise ValueError("select at least one worker")
        unassigned = [item.page_id for item in selected if not item.role]
        if unassigned:
            raise ValueError(f"selected workers are unassigned: {unassigned!r}")
        duplicates = duplicate_roles(selected)
        if duplicates:
            raise ValueError(f"duplicate roles are not allowed: {list(duplicates)!r}")
        disconnected = [item.page_id for item in selected if not item.connected]
        if disconnected:
            raise ValueError(f"selected workers are disconnected: {disconnected!r}")
        login = [item.page_id for item in selected if item.requires_login]
        if login:
            raise ValueError(f"selected workers require login: {login!r}")
        return selected

    async def start_run(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("a studio run is already active")
        self.state.settings.validate()
        goal = self.state.settings.global_goal.strip()
        if not goal:
            raise ValueError("global goal must not be empty")
        selected = self._selected_workers()
        if not self.state.settings.task_id.strip():
            timestamp = utc_now_iso().replace(":", "").replace("-", "")[:15]
            self.state.settings.task_id = f"{slug_task_id(goal)}-{timestamp}"
        self._stop_requested = False
        self._skip_pages.clear()
        self._retry_pages.clear()
        self._pause_gate.set()
        for worker in selected:
            worker.reset_for_run()
        self.state.run_status = RunStatus.STARTING
        self.state.active_page_id = None
        self.state.current_round = 0
        self._persist(event=("run", f"Starting task {self.state.settings.task_id}", None))
        self._run_task = asyncio.create_task(self._run_pipeline(), name="playwright-studio-run")
        await asyncio.sleep(0)

    async def wait_for_run(self) -> None:
        if self._run_task is not None:
            await self._run_task

    def pause_run(self) -> None:
        if self._run_task is None or self._run_task.done():
            return
        self._pause_gate.clear()
        self.state.run_status = (
            RunStatus.PAUSING if self.state.active_page_id else RunStatus.PAUSED
        )
        self._persist(event=("run", "Pause requested", None))

    def resume_run(self) -> None:
        if self._run_task is None or self._run_task.done():
            return
        self._pause_gate.set()
        self.state.run_status = RunStatus.RUNNING
        self._persist(event=("run", "Run resumed", None))

    async def _stop_active(self, page_id: str) -> None:
        worker = self._worker(page_id)
        worker.status = WorkerStatus.STOPPING
        self._persist(event=("worker", "Stopping active response", worker))
        try:
            await self.backend.stop(page_id)
        except Exception as exc:
            self._emit("warning", f"Stop command returned {type(exc).__name__}: {exc}", worker=worker)
        if self._active_operation is not None and not self._active_operation.done():
            self._active_operation.cancel()

    async def stop_worker(self, page_id: str) -> None:
        worker = self._worker(page_id)
        if self.state.active_page_id == page_id:
            self._skip_pages.add(page_id)
            await self._stop_active(page_id)
        else:
            self._skip_pages.add(page_id)
            worker.status = WorkerStatus.STOPPED
            worker.finished_at = utc_now_iso()
            self._persist(event=("worker", "Worker removed from current run", worker))

    async def stop_run(self) -> None:
        if self._run_task is None or self._run_task.done():
            self.state.run_status = RunStatus.STOPPED
            self._persist(event=("run", "Run stopped", None))
            return
        self._stop_requested = True
        self._pause_gate.set()
        self.state.run_status = RunStatus.STOPPING
        active = self.state.active_page_id
        self._persist(event=("run", "Stop requested", None))
        if active:
            await self._stop_active(active)

    async def _wait_if_paused(self) -> None:
        if self._pause_gate.is_set():
            return
        self.state.run_status = RunStatus.PAUSED
        self._persist(event=("run", "Run paused before next worker", None))
        await self._pause_gate.wait()
        if not self._stop_requested:
            self.state.run_status = RunStatus.RUNNING
            self._persist(event=("run", "Run continued", None))

    async def _execute_worker(
        self,
        worker: WorkerModel,
        *,
        ordinal: int,
        total_workers: int,
        round_index: int,
        prior_outputs: list[tuple[str, str]],
    ) -> str | None:
        while True:
            if self._stop_requested or worker.page_id in self._skip_pages:
                worker.status = WorkerStatus.STOPPED
                worker.finished_at = utc_now_iso()
                self._persist(event=("worker", "Worker skipped", worker))
                return None
            await self._wait_if_paused()
            if self._stop_requested:
                return None
            worker.status = WorkerStatus.PREPARING
            worker.started_at = utc_now_iso()
            worker.error = ""
            self.state.active_page_id = worker.page_id
            self.state.run_status = RunStatus.RUNNING
            self._persist(event=("worker", "Preparing task conversation", worker))
            try:
                await self.backend.prepare_task(worker.page_id, self.state.settings.task_id)
                prompt = render_worker_prompt(
                    global_goal=self.state.settings.global_goal,
                    worker=worker,
                    ordinal=ordinal,
                    total_workers=total_workers,
                    round_index=round_index,
                    total_rounds=self.state.settings.rounds,
                    prior_outputs=prior_outputs,
                    context_char_limit=self.state.settings.context_char_limit,
                )
                worker.last_prompt = prompt
                worker.status = WorkerStatus.RUNNING
                self._persist(event=("worker", "Prompt sent; waiting for response", worker))
                self._active_operation = asyncio.create_task(
                    self.backend.send_and_wait(
                        worker.page_id,
                        prompt,
                        self.state.settings.response_timeout_ms,
                        {
                            "studio": True,
                            "task_id": self.state.settings.task_id,
                            "round_index": round_index,
                            "role": worker.role or "UNASSIGNED",
                            "page_id": worker.page_id,
                            "response_count": len(worker.responses),
                        },
                    )
                )
                response = await self._active_operation
            except asyncio.CancelledError:
                self._active_operation = None
                if worker.page_id in self._retry_pages and not self._stop_requested:
                    self._retry_pages.discard(worker.page_id)
                    worker.pending_retry = False
                    worker.status = WorkerStatus.QUEUED
                    self._persist(event=("worker", "Requeued with edited prompt", worker))
                    continue
                worker.status = WorkerStatus.STOPPED
                worker.finished_at = utc_now_iso()
                self._persist(event=("worker", "Worker stopped", worker))
                return None
            except Exception as exc:
                self._active_operation = None
                worker.status = WorkerStatus.ERROR
                worker.error = f"{type(exc).__name__}: {exc}"
                worker.finished_at = utc_now_iso()
                self._persist(event=("error", worker.error, worker))
                raise
            self._active_operation = None
            worker.responses.append(response)
            worker.status = WorkerStatus.COMPLETED
            worker.finished_at = utc_now_iso()
            self._persist(event=("response", "Response completed", worker))
            return response

    async def _run_pipeline(self) -> None:
        prior_outputs: list[tuple[str, str]] = []
        try:
            selected_count = len(self._selected_workers())
            for round_index in range(self.state.settings.rounds):
                self.state.current_round = round_index
                completed_this_round: set[str] = set()
                for worker in self._selected_workers():
                    if worker.page_id not in self._skip_pages:
                        worker.status = WorkerStatus.QUEUED
                self._persist(event=("run", f"Starting round {round_index + 1}", None))
                while len(completed_this_round) < selected_count:
                    if self._stop_requested:
                        break
                    candidates = [
                        item
                        for item in sorted(self._selected_workers(), key=lambda entry: entry.order)
                        if item.page_id not in completed_this_round
                        and item.page_id not in self._skip_pages
                    ]
                    if not candidates:
                        break
                    worker = candidates[0]
                    response = await self._execute_worker(
                        worker,
                        ordinal=len(completed_this_round) + 1,
                        total_workers=selected_count,
                        round_index=round_index,
                        prior_outputs=prior_outputs,
                    )
                    completed_this_round.add(worker.page_id)
                    if response is not None:
                        prior_outputs.append((worker.role or "UNASSIGNED", response))
                if self._stop_requested:
                    break
            if self._stop_requested:
                self.state.run_status = RunStatus.STOPPED
                for worker in self.state.workers:
                    if worker.enabled and worker.status in {
                        WorkerStatus.IDLE,
                        WorkerStatus.QUEUED,
                        WorkerStatus.PREPARING,
                    }:
                        worker.status = WorkerStatus.STOPPED
                self._persist(event=("run", "Task stopped", None))
            else:
                self.state.run_status = RunStatus.COMPLETED
                self._persist(event=("run", "Collaborative task completed", None))
        except Exception as exc:
            self.state.run_status = RunStatus.ERROR
            self._persist(event=("error", f"Run failed: {type(exc).__name__}: {exc}", None))
        finally:
            self.state.active_page_id = None
            self._active_operation = None
            self._persist()


class StudioController:
    """Thread-safe facade used by Tkinter."""

    def __init__(
        self,
        *,
        runtime_dir: str | Path = ".runtime/studio",
        backend_factory: Callable[[], StudioBackend] = PlaywrightStudioBackend,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.backend_factory = backend_factory
        self._events: queue.Queue[StudioEvent] = queue.Queue()
        self._state_lock = threading.Lock()
        self._state = StudioState()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._engine: StudioEngine | None = None
        self._thread = threading.Thread(target=self._run_loop, name="playwright-studio", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("studio controller thread did not start")

    def _on_event(self, event: StudioEvent, state: dict[str, Any]) -> None:
        with self._state_lock:
            self._state = StudioState.from_dict(state)
        self._events.put(event)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._engine = StudioEngine(
            self.backend_factory(),
            runtime_dir=self.runtime_dir,
            event_sink=self._on_event,
        )
        with self._state_lock:
            self._state = self._engine.snapshot()
        self._ready.set()
        loop.run_forever()
        loop.run_until_complete(self._engine.disconnect())
        loop.close()

    def _submit(self, coroutine: Any) -> Future[Any]:
        if self._loop is None:
            raise RuntimeError("studio controller is not running")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def _sync_call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    def snapshot(self) -> StudioState:
        with self._state_lock:
            return StudioState.from_dict(self._state.to_dict())

    def poll_events(self, limit: int = 256) -> list[StudioEvent]:
        events: list[StudioEvent] = []
        for _ in range(max(limit, 0)):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def connect(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.connect())

    def refresh_tabs(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.refresh_tabs())

    def assign_role(self, page_id: str, role: str) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.assign_role(page_id, role))

    def release_role(self, page_id: str) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.release_role(page_id))

    def set_enabled(self, page_id: str, enabled: bool) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._sync_call(self._engine.set_enabled, page_id, enabled))

    def reorder(self, page_id: str, target_index: int) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._sync_call(self._engine.reorder, page_id, target_index))

    def update_prompt(
        self, page_id: str, prompt_template: str, *, stop_and_retry: bool = False
    ) -> Future[Any]:
        assert self._engine is not None
        return self._submit(
            self._engine.update_prompt(
                page_id,
                prompt_template,
                stop_and_retry=stop_and_retry,
            )
        )

    def update_settings(self, settings: StudioSettings) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._sync_call(self._engine.update_settings, settings))

    def start_run(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.start_run())

    def pause_run(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._sync_call(self._engine.pause_run))

    def resume_run(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._sync_call(self._engine.resume_run))

    def stop_worker(self, page_id: str) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.stop_worker(page_id))

    def stop_run(self) -> Future[Any]:
        assert self._engine is not None
        return self._submit(self._engine.stop_run())

    def shutdown(self, timeout: float = 30.0) -> None:
        if self._loop is None or self._engine is None:
            return
        future = self._submit(self._engine.disconnect())
        try:
            future.result(timeout=timeout)
        except Exception as exc:
            self._events.put(
                StudioEvent(
                    kind="warning",
                    message=f"Controller shutdown forced after {type(exc).__name__}: {exc}",
                )
            )
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=timeout)
            self._loop = None
