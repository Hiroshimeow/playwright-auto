from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_lock import open_shared_read


def _role_matches(event_role: object, expected_role: str | None) -> bool:
    if not expected_role:
        return True
    actual = str(event_role or "").strip().upper()
    expected = expected_role.strip().upper()
    return actual == expected or actual.endswith(f"-{expected}")


def project_live_event(event: dict[str, Any]) -> dict[str, Any]:
    source = str(event.get("source") or "").upper()
    event_type = (
        "transcript.upsert"
        if source == "TRANSCRIPT" and str(event.get("kind") or "") == "message"
        else "audit.append"
    )
    return {**event, "type": event_type}


@dataclass(frozen=True)
class LiveEnvelope:
    type: str
    stream_epoch: str
    seq: int
    payload: dict[str, Any]

    @property
    def event_id(self) -> str | None:
        value = str(self.payload.get("event_id") or "").strip()
        return value or None

    def as_data(self) -> dict[str, Any]:
        return {
            **self.payload,
            "type": self.type,
            "stream_epoch": self.stream_epoch,
            "seq": self.seq,
        }


class LiveSubscription:
    def __init__(self, task_id: str, role: str | None, *, max_queue: int) -> None:
        self.task_id = task_id
        self.role = role
        self.queue: queue.Queue[LiveEnvelope | dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self.closed = False
        self.overflowed = False

    def matches(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("task_id") or "") != self.task_id:
            return False
        return _role_matches(payload.get("role"), self.role)

    def put(self, item: LiveEnvelope | dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self.overflowed = True
            self.closed = True
            return False

    def get(self, timeout: float | None = None) -> LiveEnvelope | dict[str, Any]:
        return self.queue.get(timeout=timeout)


class LiveLogFollower:
    def __init__(self, hub: "LiveStreamHub", path: str | Path, *, poll_seconds: float = 0.1) -> None:
        self.hub = hub
        self.path = Path(path)
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""

    @property
    def active(self) -> bool:
        return self._active

    def _stat_identity(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))

    def activate_locked(self) -> None:
        if self._active:
            return
        try:
            stat = self.path.stat()
            self._identity = self._stat_identity()
            self._offset = int(stat.st_size)
        except FileNotFoundError:
            self._identity = None
            self._offset = 0
        self._partial = b""
        self._active = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="cdpa-live-log", daemon=True)
            self._thread.start()
        self._wake.set()

    def deactivate_locked(self) -> None:
        self._active = False
        self._partial = b""
        self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _read_once(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._identity is not None or self._offset:
                self.hub.reset_stream()
                self._identity = None
                self._offset = 0
                self._partial = b""
            return
        identity = self._stat_identity()
        size = int(stat.st_size)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity or size < self._offset:
            self.hub.reset_stream()
            self._identity = identity
            self._offset = 0
            self._partial = b""
        if size <= self._offset:
            return
        try:
            with open_shared_read(self.path) as handle:
                handle.seek(self._offset)
                chunk = handle.read(size - self._offset)
        except FileNotFoundError:
            return
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop() if lines else b""
        for raw in lines:
            raw = raw.rstrip(b"\r")
            if not raw:
                continue
            try:
                value = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.hub.publish(value)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._active:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            self._read_once()
            self._stop.wait(self.poll_seconds)


class LiveStreamHub:
    def __init__(
        self,
        path: str | Path,
        *,
        ring_size: int = 512,
        subscriber_queue_size: int = 256,
        poll_seconds: float = 0.1,
    ) -> None:
        self.lock = threading.RLock()
        self.stream_epoch = uuid4().hex
        self.ring_size = ring_size
        self.subscribers: set[LiveSubscription] = set()
        self._seq_by_scope: dict[tuple[str, str | None], int] = {}
        self._rings: dict[tuple[str, str | None], deque[LiveEnvelope]] = {}
        self.subscriber_queue_size = subscriber_queue_size
        self.follower = LiveLogFollower(self, path, poll_seconds=poll_seconds)
        self._has_activated = False

    @staticmethod
    def _scope(task_id: str, role: str | None) -> tuple[str, str | None]:
        return (str(task_id), str(role).strip().upper() if role else None)

    def _ensure_scope_locked(self, scope: tuple[str, str | None]) -> None:
        self._seq_by_scope.setdefault(scope, 0)
        self._rings.setdefault(scope, deque(maxlen=self.ring_size))

    def publish(self, event: dict[str, Any]) -> list[LiveEnvelope]:
        payload = project_live_event(event)
        published: list[LiveEnvelope] = []
        with self.lock:
            for scope in tuple(self._rings):
                task_id, role = scope
                if str(payload.get("task_id") or "") != task_id:
                    continue
                if not _role_matches(payload.get("role"), role):
                    continue
                seq = self._seq_by_scope[scope] + 1
                self._seq_by_scope[scope] = seq
                envelope = LiveEnvelope(payload["type"], self.stream_epoch, seq, payload)
                self._rings[scope].append(envelope)
                published.append(envelope)
                for subscription in tuple(self.subscribers):
                    if self._scope(subscription.task_id, subscription.role) != scope:
                        continue
                    if not subscription.put(envelope):
                        self.subscribers.discard(subscription)
            if not self.subscribers:
                self.follower.deactivate_locked()
        return published

    def _reset_stream_locked(self) -> None:
        self.stream_epoch = uuid4().hex
        for scope in self._rings:
            self._seq_by_scope[scope] = 0
            self._rings[scope].clear()
        for subscription in tuple(self.subscribers):
            reset = {"type": "stream.reset", "stream_epoch": self.stream_epoch, "seq": 0}
            if not subscription.put(reset):
                self.subscribers.discard(subscription)

    def reset_stream(self) -> None:
        with self.lock:
            self._reset_stream_locked()

    @staticmethod
    def parse_cursor(value: str | None) -> tuple[str, int] | None:
        text = str(value or "").strip()
        if not text:
            return None
        epoch, separator, raw_seq = text.rpartition(":")
        if not separator or not epoch:
            raise ValueError("invalid stream cursor")
        try:
            seq = int(raw_seq)
        except ValueError as exc:
            raise ValueError("invalid stream cursor") from exc
        if seq < 0:
            raise ValueError("invalid stream cursor")
        return epoch, seq

    def subscribe(
        self,
        task_id: str,
        role: str | None,
        *,
        last_event_id: str | None = None,
    ) -> tuple[LiveSubscription, dict[str, Any] | None]:
        subscription = LiveSubscription(
            task_id,
            role,
            max_queue=self.subscriber_queue_size,
        )
        try:
            cursor = self.parse_cursor(last_event_id)
        except ValueError:
            cursor = ("", -1)
        with self.lock:
            scope = self._scope(task_id, role)
            self._ensure_scope_locked(scope)
            if self._has_activated and not self.follower.active:
                self._reset_stream_locked()
            self.follower.activate_locked()
            self._has_activated = True
            self.subscribers.add(subscription)
            head = self._seq_by_scope[scope]
            ring = self._rings[scope]
            if cursor is None:
                control: dict[str, Any] | None = {
                    "type": "stream.ready",
                    "stream_epoch": self.stream_epoch,
                    "seq": head,
                }
            else:
                epoch, requested = cursor
                oldest = ring[0].seq if ring else head + 1
                valid = (
                    epoch == self.stream_epoch
                    and requested <= head
                    and requested >= oldest - 1
                )
                if valid:
                    for envelope in ring:
                        if envelope.seq > requested and not subscription.put(envelope):
                            break
                    control = None
                else:
                    control = {
                        "type": "stream.reset",
                        "stream_epoch": self.stream_epoch,
                        "seq": head,
                    }
            return subscription, control

    def unsubscribe(self, subscription: LiveSubscription) -> None:
        with self.lock:
            subscription.closed = True
            self.subscribers.discard(subscription)
            if not self.subscribers:
                self.follower.deactivate_locked()

    def close(self) -> None:
        with self.lock:
            for subscription in self.subscribers:
                subscription.closed = True
            self.subscribers.clear()
            self.follower.deactivate_locked()
        self.follower.stop()
