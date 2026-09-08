"""One page-lifetime source of DOM/network signals, shared by every role consumer.

There is no request/hop/user-ID scope here. Events belong to the current Page and
conversation. G2/history is stored separately as information, never a route gate.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import weakref
from collections import Counter
from typing import Any, Mapping
from urllib.parse import urlparse

from .listen import ConversationDecoder, conversation_id, extract, source_kind

_OBSERVERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

DOM_SCRIPT = r"""(() => {
  if (window !== window.top || !['chatgpt.com','www.chatgpt.com'].includes(location.hostname)) return;
  if (window.__CDPA_ROLE_DOM_V1__) return;
  let pending = false;
  const notify = () => {
    if (pending) return;
    pending = true;
    setTimeout(() => {
      pending = false;
      if (typeof window.__cdpaRoleWake === 'function')
        Promise.resolve(window.__cdpaRoleWake()).catch(() => {});
    }, 50);
  };
  const start = () => {
    if (window.__CDPA_ROLE_DOM_V1__ || !document.documentElement) return;
    const observer = new MutationObserver(records => {
      if (records.some(r => {
        const target = r.target.nodeType === 1 ? r.target : r.target.parentElement;
        return target && !target.closest('#playwright-auto-role-badge-v3,#playwright-auto-role-control-v1') &&
          (target.closest('main,form,[role="dialog"],[role="alert"]') ||
           [...r.addedNodes].some(n => n.nodeType === 1 && n.matches('main,[role="dialog"],[role="alert"]')));
      })) notify();
    });
    observer.observe(document.documentElement, {subtree:true, childList:true, characterData:true,
      attributes:true, attributeFilter:['hidden','style','class','aria-label','aria-disabled',
        'data-message-id','data-streaming-response-status','data-testid']});
    document.addEventListener('input', notify, true);
    window.addEventListener('pageshow', notify);
    window.__CDPA_ROLE_DOM_V1__ = observer;
    notify();
  };
  start();
  if (!document.documentElement) document.addEventListener('DOMContentLoaded', start, {once:true});
})()
"""


class PageObserver:
    def __init__(self, page: Any):
        self._page_ref = weakref.ref(page)
        self.dom_wake = asyncio.Event()
        self.network_wake = asyncio.Event()
        self.install_lock = asyncio.Lock()
        self.installed = False
        self.attached = False
        self.closed = False
        self.epoch = 0
        self.current_conversation = conversation_id(page.url)
        self.permission: dict[str, Any] | None = None
        self.response: dict[str, Any] | None = None
        self.status: str | None = None
        self.messages: list[Mapping[str, Any]] = []
        self.info: dict[str, Any] | None = None
        self.info_at = 0.0
        self.metrics: Counter = Counter()
        self.tasks: set[asyncio.Task] = set()
        self.streams: dict[str, dict[str, Any]] = {}
        self.cdp = None
        self.streaming_supported = True
        self._handlers: list[tuple[str, Any]] = []
        self._last_permission = None

    @property
    def page(self):
        page = self._page_ref()
        if page is None:
            raise RuntimeError("observed Page no longer exists")
        return page

    def _spawn(self, coroutine):
        if self.closed:
            coroutine.close()
            return
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def done(item):
            self.tasks.discard(item)
            if not item.cancelled():
                try:
                    item.result()
                except Exception:
                    self.metrics["observer_errors"] += 1
        task.add_done_callback(done)

    def attach(self) -> None:
        if self.attached or self.closed:
            return
        self.attached = True
        self._handlers = [
            ("request", self._on_request), ("requestfinished", self._on_finished),
            ("framenavigated", self._on_navigation), ("close", self.close),
            ("websocket", self._on_websocket),
        ]
        for event, handler in self._handlers:
            self.page.on(event, handler)

    async def install(self) -> None:
        self.attach()
        if self.installed or self.closed:
            return
        async with self.install_lock:
            if self.installed or self.closed:
                return
            page = self.page
            try:
                await page.expose_binding("__cdpaRoleWake", lambda *_args: self.dom_wake.set())
            except Exception as exc:
                # A reconnect may find the binding in this automation connection already.
                if "already" not in str(exc).lower():
                    raise
            await page.add_init_script(DOM_SCRIPT)
            await page.evaluate(DOM_SCRIPT)
            try:
                self.cdp = await page.context.new_cdp_session(page)
                self.cdp.on("Network.responseReceived", self._on_cdp_response)
                self.cdp.on("Network.dataReceived", self._on_data)
                self.cdp.on("Network.loadingFinished", self._on_loaded)
                self.cdp.on("Network.loadingFailed", self._on_failed)
                await self.cdp.send("Network.enable", {"maxTotalBufferSize": 8_388_608, "maxResourceBufferSize": 2_097_152})
            except Exception:
                self.metrics["streaming_unavailable"] += 1
                self.streaming_supported = False
            self.installed = True

    def _on_navigation(self, frame) -> None:
        if frame != self.page.main_frame:
            return
        new = conversation_id(self.page.url)
        old = self.current_conversation
        # ChatGPT replaces provisional WEB:* with the canonical ID in-place. Do
        # not discard the first live permission merely because that alias resolves.
        alias_resolution = bool(old and old.startswith("WEB:") and new and not new.startswith("WEB:"))
        if new != old and not alias_resolution:
            self.epoch += 1
            self.permission = self.response = None
            self.status = None
            self.messages = []
            self.info = None
            self._last_permission = None
        self.current_conversation = new
        self.dom_wake.set()

    def _on_request(self, request) -> None:
        kind = source_kind(request.method, request.url)
        if kind:
            self.metrics[f"requests_{kind}"] += 1
        if kind == "live":
            # Later operator prompts/Retry are ordinary new live activity on this
            # conversation, not a reason to detach or wait for an old user ID.
            self.response = None
            self.status = "IS_STREAMING"
            self.network_wake.set()

    def _on_finished(self, request) -> None:
        kind = source_kind(request.method, request.url)
        if kind:
            self._spawn(self._read_finished(request, kind, self.epoch))

    async def _read_finished(self, request, kind: str, epoch: int) -> None:
        try:
            response = await request.response()
            if response is None or response.status >= 400:
                self.metrics["http_error"] += 1
                return
            # This is already requestfinished: no long-lived body await in a wait loop.
            body = await asyncio.wait_for(response.body(), 5)
            if epoch != self.epoch or self.closed:
                return
            if len(body) > 8_388_608:
                self.metrics["body_too_large"] += 1
                return
            if kind == "history":
                value = json.loads(body)
                if isinstance(value, dict):
                    self.info, self.info_at = value, time.monotonic()
                self.metrics["history_info"] += 1
                return
            decoder = ConversationDecoder()
            for item in decoder.feed(body, final=True):
                self.publish(item, epoch=epoch, source="response_body")
            self.metrics["parse_errors"] += decoder.errors
        except (ValueError, TimeoutError):
            self.metrics["parse_errors"] += 1

    def _on_cdp_response(self, params) -> None:
        response = params.get("response") or {}
        url = str(response.get("url") or "")
        if source_kind("POST", url) != "live" or int(response.get("status") or 0) >= 400:
            return
        if not self.streaming_supported or len(self.streams) >= 16:
            return
        request_id = params["requestId"]
        stream = {"decoder": ConversationDecoder(), "epoch": self.epoch, "pending": [], "ready": False}
        self.streams[request_id] = stream
        self._spawn(self._enable_stream(request_id, stream))

    async def _enable_stream(self, request_id: str, stream: dict) -> None:
        try:
            reply = await self.cdp.send("Network.streamResourceContent", {"requestId": request_id})
        except Exception as exc:
            self.metrics["stream_setup_failed"] += 1
            if "not found" in str(exc).lower() or "wasn't found" in str(exc).lower():
                self.streaming_supported = False
            self.streams.pop(request_id, None)
            return
        # Data received while the enabling command was in flight follows bufferedData.
        chunks = [reply.get("bufferedData") or "", *stream["pending"]]
        stream["pending"] = []
        stream["ready"] = True
        for chunk in chunks:
            self._decode_chunk(stream, chunk)
        if stream.get("finished"):
            self._finish_stream(request_id)

    def _decode_chunk(self, stream, encoded: str) -> None:
        if not encoded:
            return
        try:
            data = base64.b64decode(encoded, validate=True)
            for item in stream["decoder"].feed(data):
                self.publish(item, epoch=stream["epoch"], source="network_chunk")
            self.metrics["stream_chunks"] += 1
        except (ValueError, TypeError):
            self.metrics["parse_errors"] += 1

    def _on_data(self, params) -> None:
        stream = self.streams.get(params.get("requestId"))
        data = params.get("data")
        if stream is None or not data:
            return
        if stream["ready"]:
            self._decode_chunk(stream, data)
        elif sum(map(len, stream["pending"])) < 2_097_152:
            stream["pending"].append(data)

    def _on_loaded(self, params) -> None:
        request_id = params.get("requestId")
        stream = self.streams.get(request_id)
        if stream is not None:
            stream["finished"] = True
            if stream["ready"]:
                self._finish_stream(request_id)

    def _finish_stream(self, request_id) -> None:
        stream = self.streams.pop(request_id, None)
        if stream is None:
            return
        for item in stream["decoder"].feed(b"", final=True):
            self.publish(item, epoch=stream["epoch"], source="network_chunk")
        self.metrics["parse_errors"] += stream["decoder"].errors

    def _on_failed(self, params) -> None:
        self.streams.pop(params.get("requestId"), None)
        self.metrics["request_failed"] += 1
        self.network_wake.set()

    def _on_websocket(self, socket) -> None:
        host = (urlparse(socket.url).hostname or "").lower()
        if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
            return
        def receive(data):
            try:
                if len(data) > 1_048_576:
                    return
                value = extract(json.loads(data))
                # User-wide sockets can multiplex conversations. Unlike a response
                # from this Page, a socket frame needs its conversation address.
                if value.get("conversation_id"):
                    self.publish(value, epoch=self.epoch, source="websocket")
            except (ValueError, TypeError):
                self.metrics["socket_unparsed"] += 1
        socket.on("framereceived", receive)

    def publish(self, value: Mapping[str, Any], *, epoch: int, source: str) -> None:
        if self.closed or epoch != self.epoch:
            self.metrics["stale_document"] += 1
            return
        current = conversation_id(self.page.url)
        candidate = value.get("conversation_id")
        if value.get("mixed_conversations") or (candidate and current and not current.startswith("WEB:") and candidate != current):
            self.metrics["other_conversation"] += 1
            return
        if value.get("status"):
            self.status = str(value["status"])
        if value.get("response"):
            self.response = dict(value["response"])
        for message in value.get("messages") or ():
            if isinstance(message, Mapping):
                message_id = message.get("id")
                self.messages = [old for old in self.messages if not message_id or old.get("id") != message_id]
                self.messages.append(dict(message))
        self.messages = self.messages[-128:]
        action = value.get("permission_action")
        if isinstance(action, Mapping):
            key = (action.get("target_message_id"), action.get("remember_answer"))
            if key != self._last_permission:
                self.permission = dict(action)
                self._last_permission = key
                self.metrics["permissions"] += 1
        self.metrics["observations"] += 1
        self.metrics[source] += 1
        self.network_wake.set()

    def observation(self) -> dict[str, Any]:
        return {
            "permission_action": dict(self.permission) if self.permission else None,
            "response": dict(self.response) if self.response else None,
            "status": self.status, "conversation_id": self.current_conversation,
            "messages": list(self.messages), "diagnostics": dict(self.metrics),
            "listener_attached": self.attached, "streaming_supported": self.streaming_supported,
        }

    def clear_permission(self) -> None:
        self.permission = None
        # Keep last identity so the body fallback cannot resurrect a chunk already dispatched.

    async def wait(self, seconds: float = 5, *, dom_only: bool = False) -> bool:
        events = [self.dom_wake] if dom_only else [self.dom_wake, self.network_wake]
        if any(event.is_set() for event in events):
            for event in events:
                event.clear()
            return True
        tasks = [asyncio.create_task(event.wait()) for event in events]
        try:
            done, _ = await asyncio.wait(tasks, timeout=max(0, seconds), return_when=asyncio.FIRST_COMPLETED)
            for event in events:
                event.clear()
            return bool(done)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self, *_args) -> None:
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()
        self.streams.clear()
        page = self._page_ref()
        if page is not None:
            for event, handler in self._handlers:
                page.remove_listener(event, handler)
            _OBSERVERS.pop(page, None)
        self._handlers = []


def observer_for(page: Any) -> PageObserver:
    observer = _OBSERVERS.get(page)
    if observer is None:
        observer = PageObserver(page)
        _OBSERVERS[page] = observer
    return observer
