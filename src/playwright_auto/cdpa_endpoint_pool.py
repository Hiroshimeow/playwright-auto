from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import UUID

from .cdpa_endpoints import BrowserEndpointRegistry
from .chatgpt import ChatGPTPage
from .connection import connect


async def probe_cdp_version(address: str) -> Mapping[str, Any]:
    parsed = urlsplit(address)
    if parsed.scheme in {"ws", "wss"}:
        return {"webSocketDebuggerUrl": address}
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CDP version probe requires HTTP(S) or WebSocket endpoint")
    version_url = urlunsplit((parsed.scheme, parsed.netloc, "/json/version", "", ""))

    def fetch() -> Mapping[str, Any]:
        request = Request(version_url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=3.0) as response:
            raw = response.read(65_536)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("CDP /json/version did not return an object")
        return dict(value)

    return await asyncio.to_thread(fetch)


async def probe_chatgpt_auth(context: Any) -> Mapping[str, Any]:
    saw_login_required = False
    for page in tuple(getattr(context, "pages", ()) or ()):
        try:
            if (urlsplit(str(getattr(page, "url", ""))).hostname or "").lower() not in {
                "chatgpt.com",
                "www.chatgpt.com",
            }:
                continue
            snapshot = await ChatGPTPage(page, timeout_ms=5_000).snapshot()
        except Exception:
            continue
        if snapshot.requires_login:
            saw_login_required = True
            continue
        if snapshot.composer_present and snapshot.composer_editable:
            return {"auth_state": "authenticated", "chatgpt_ready": True}
    if saw_login_required:
        return {"auth_state": "login_required", "chatgpt_ready": False}
    # No existing ChatGPT page is not proof of logout. Allocation may open one.
    return {"auth_state": "unknown", "chatgpt_ready": True}


@dataclass
class _Runtime:
    endpoint_id: str
    address: str
    playwright: Any | None = None
    browser: Any | None = None
    context: Any | None = None
    physical_id: str | None = None
    health: str = "offline"
    auth_state: str = "unknown"
    chatgpt_ready: bool = False
    error: str | None = None
    observed_at: float | None = None
    retry_after: float = 0.0
    auth_probe_after: float = 0.0


class EndpointPool:
    """Own CDP attachments per durable endpoint without owning browser processes."""

    def __init__(
        self,
        registry: BrowserEndpointRegistry,
        *,
        connector: Callable[[str], Awaitable[tuple[Any, Any]]] = connect,
        version_probe: Callable[[str], Awaitable[Mapping[str, Any]]] = probe_cdp_version,
        auth_probe: Callable[[Any], Awaitable[Mapping[str, Any]]] = probe_chatgpt_auth,
        clock: Callable[[], float] = time.monotonic,
        retry_delay: float = 15.0,
        auth_refresh_seconds: float = 15.0,
    ) -> None:
        self.registry = registry
        self.connector = connector
        self.version_probe = version_probe
        self.auth_probe = auth_probe
        self.clock = clock
        self.retry_delay = retry_delay
        self.auth_refresh_seconds = auth_refresh_seconds
        self._runtimes: dict[str, _Runtime] = {}

    @property
    def contexts(self) -> dict[str, Any]:
        return {
            endpoint_id: runtime.context
            for endpoint_id, runtime in self._runtimes.items()
            if runtime.context is not None and runtime.health == "online"
        }

    async def _stop_attachment(self, runtime: _Runtime) -> None:
        playwright = runtime.playwright
        runtime.playwright = None
        runtime.browser = None
        runtime.context = None
        runtime.physical_id = None
        if playwright is not None:
            with suppress(Exception):  # A broken attachment must not take healthy peers down.
                result = playwright.stop()
                if inspect.isawaitable(result):
                    await result

    @staticmethod
    def _physical_identity(version: Mapping[str, Any], address: str) -> str:
        value = str(version.get("webSocketDebuggerUrl") or "").strip()
        path = urlsplit(value).path
        if path.startswith("/devtools/browser/"):
            with suppress(ValueError):
                return f"devtools:{UUID(path.removeprefix('/devtools/browser/'))}"
        return value or address

    async def _refresh_auth(self, runtime: _Runtime) -> None:
        try:
            auth = await asyncio.wait_for(self.auth_probe(runtime.context), timeout=8.0)
            runtime.auth_state = str(auth.get("auth_state") or "unknown")
            runtime.chatgpt_ready = bool(auth.get("chatgpt_ready"))
            runtime.error = None
        except Exception as exc:
            runtime.auth_state = "unknown"
            runtime.chatgpt_ready = False
            runtime.error = f"ChatGPT readiness probe failed: {exc}"
        runtime.auth_probe_after = self.clock() + self.auth_refresh_seconds
        runtime.observed_at = self.clock()

    async def _connect(self, record: Mapping[str, Any], runtime: _Runtime, claimed: set[str]) -> None:
        address = str(record["cdp_url"])
        runtime.health = "connecting"
        runtime.error = None
        runtime.observed_at = self.clock()
        try:
            version = await self.version_probe(address)
            playwright, browser = await asyncio.wait_for(self.connector(address), timeout=15.0)
            runtime.playwright = playwright
            runtime.browser = browser
            contexts = list(getattr(browser, "contexts", ()) or ())
            if not contexts:
                raise RuntimeError("CDP browser has no contexts")
            physical_id = self._physical_identity(version, address)
            if physical_id in claimed:
                runtime.playwright = playwright
                runtime.browser = browser
                runtime.context = contexts[0]
                runtime.physical_id = physical_id
                runtime.health = "conflict"
                runtime.auth_state = "unknown"
                runtime.chatgpt_ready = False
                runtime.error = "physical browser is already owned by another endpoint"
                await self._stop_attachment(runtime)
                runtime.retry_after = self.clock() + self.retry_delay
                return
            runtime.playwright = playwright
            runtime.browser = browser
            runtime.context = contexts[0]
            runtime.physical_id = physical_id
            claimed.add(physical_id)
            runtime.health = "online"
            await self._refresh_auth(runtime)
            runtime.retry_after = 0.0
            runtime.observed_at = self.clock()
        except Exception as exc:
            await self._stop_attachment(runtime)
            runtime.health = "offline"
            runtime.auth_state = "unknown"
            runtime.chatgpt_ready = False
            runtime.error = str(exc)
            runtime.retry_after = self.clock() + self.retry_delay
            runtime.observed_at = self.clock()

    async def refresh(self, *, wait: bool = False) -> dict[str, Any]:
        records = {item["endpoint_id"]: item for item in self.registry.list()}
        now = self.clock()

        for endpoint_id, runtime in list(self._runtimes.items()):
            record = records.get(endpoint_id)
            if record is None:
                await self._stop_attachment(runtime)
                del self._runtimes[endpoint_id]
                continue
            address_changed = runtime.address != record["cdp_url"]
            disconnected = runtime.browser is not None and not runtime.browser.is_connected()
            if address_changed or disconnected:
                await self._stop_attachment(runtime)
                runtime.address = str(record["cdp_url"])
                runtime.health = "offline"
                runtime.auth_state = "unknown"
                runtime.chatgpt_ready = False
                runtime.error = "endpoint disconnected" if disconnected else None
                runtime.retry_after = now + (0.0 if address_changed else self.retry_delay)

        claimed = {
            runtime.physical_id
            for runtime in self._runtimes.values()
            if runtime.context is not None and runtime.health == "online" and runtime.physical_id
        }
        for endpoint_id, record in records.items():
            runtime = self._runtimes.get(endpoint_id)
            if runtime is None:
                runtime = _Runtime(endpoint_id=endpoint_id, address=str(record["cdp_url"]))
                self._runtimes[endpoint_id] = runtime
            if runtime.context is not None:
                if wait or now >= runtime.auth_probe_after:
                    await self._refresh_auth(runtime)
                continue
            if not wait and now < runtime.retry_after:
                continue
            await self._connect(record, runtime, claimed)
        return self.snapshot()

    def snapshot(
        self,
        *,
        running_executions: Sequence[Mapping[str, Any]] = (),
        cooldown_scopes: Collection[str] = (),
        reservations: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        catalog = self.registry.snapshot()
        reserved = reservations or {}
        cooled = set(cooldown_scopes)
        endpoints = []
        for record in self.registry.list():
            endpoint_id = str(record["endpoint_id"])
            runtime = self._runtimes.get(endpoint_id)
            running = [
                dict(item)
                for item in running_executions
                if str(item.get("endpoint_id") or "") == endpoint_id
            ]
            capacity = int(record["max_active_tabs"])
            active_count = len(running) + max(0, int(reserved.get(endpoint_id, 0)))
            cooldown = str(record.get("rate_limit_scope") or "shared") in cooled
            item = dict(record)
            item.update(
                health=runtime.health if runtime else "offline",
                auth_state=runtime.auth_state if runtime else "unknown",
                chatgpt_ready=runtime.chatgpt_ready if runtime else False,
                error=runtime.error if runtime else None,
                observed_at=runtime.observed_at if runtime else None,
                active_count=active_count,
                capacity=capacity,
                available_slots=max(0, capacity - active_count),
                cooldown=cooldown,
                running_executions=running,
            )
            endpoints.append(item)
        default = next((item["endpoint_id"] for item in catalog["entries"].values() if item["is_default"]), None)
        return {"default_endpoint_id": default, "endpoints": endpoints}

    def select_endpoint(
        self,
        running_executions: Sequence[Mapping[str, Any]],
        *,
        affinity_endpoint_id: str | None = None,
        cooldown_scopes: Collection[str] = (),
        reservations: Mapping[str, int] | None = None,
    ) -> str | None:
        snapshot = self.snapshot(
            running_executions=running_executions,
            cooldown_scopes=cooldown_scopes,
            reservations=reservations,
        )
        eligible = [
            item
            for item in snapshot["endpoints"]
            if item["health"] == "online"
            and item["chatgpt_ready"]
            and not item["draining"]
            and not item["removed"]
            and not item["cooldown"]
            and item["available_slots"] > 0
        ]
        if affinity_endpoint_id is not None:
            preferred = next(
                (item for item in eligible if item["endpoint_id"] == affinity_endpoint_id),
                None,
            )
            if preferred is not None:
                return str(preferred["endpoint_id"])
        if not eligible:
            return None
        eligible.sort(key=lambda item: item["active_count"])
        return str(eligible[0]["endpoint_id"])

    async def mark_unavailable(self, endpoint_id: str, error: BaseException | str) -> None:
        runtime = self._runtimes.get(endpoint_id)
        if runtime is None:
            return
        await self._stop_attachment(runtime)
        runtime.health = "offline"
        runtime.auth_state = "unknown"
        runtime.chatgpt_ready = False
        runtime.error = str(error)
        runtime.retry_after = self.clock() + self.retry_delay
        runtime.observed_at = self.clock()

    async def close(self) -> None:
        for runtime in list(self._runtimes.values()):
            await self._stop_attachment(runtime)
        self._runtimes.clear()
