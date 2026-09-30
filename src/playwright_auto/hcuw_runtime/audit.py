from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    """One dashboard-safe audit event emitted by a runtime-v2 owner."""

    event_id: int
    source: str
    kind: str
    message: str
    created_at: str
    payload: dict[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "kind": self.kind,
            "message": self.message,
            "created_at": self.created_at,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class AuditSourceState:
    """Current status of one audit source shown on dashboard 9226."""

    source: str
    status: str
    message: str
    updated_at: str
    last_event_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "message": self.message,
            "updated_at": self.updated_at,
            "last_event_id": self.last_event_id,
            "payload": self.payload,
        }


class AuditTimeline:
    """Owner of runtime-v2 audit events and source states.

    This owner records what each observation/action source can currently show.
    It does not own DOM extraction, mutation observation, LISTEN transport, or
    browser mutation. Those source owners must publish their results here later.
    """

    def __init__(self, *, max_events: int = 300) -> None:
        self._max_events = max_events
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._source_states: dict[str, AuditSourceState] = {}
        self._next_event_id = 1
        self._lock = Lock()
        self._initialize_default_sources()

    def record_event(
        self,
        *,
        source: str,
        kind: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        with self._lock:
            event = AuditEvent(
                event_id=self._next_event_id,
                source=source,
                kind=kind,
                message=message,
                created_at=_now_iso(),
                payload=payload or {},
            )
            self._next_event_id += 1
            self._events.append(event)
            self._source_states[source] = AuditSourceState(
                source=source,
                status="active",
                message=message,
                updated_at=event.created_at,
                last_event_id=event.event_id,
                payload=event.payload,
            )
            return event

    def update_source_state(
        self,
        *,
        source: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditSourceState:
        with self._lock:
            state = AuditSourceState(
                source=source,
                status=status,
                message=message,
                updated_at=_now_iso(),
                payload=payload or {},
            )
            self._source_states[source] = state
            return state

    def snapshot(self, *, limit: int = 80) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)[-limit:]
            source_states = [
                self._source_states[key].to_payload()
                for key in sorted(self._source_states)
            ]
        return {
            "sources": source_states,
            "events": [event.to_payload() for event in events],
            "max_events": self._max_events,
        }

    def events_since(self, event_id: int, *, limit: int = 80) -> dict[str, Any]:
        with self._lock:
            events = [event for event in self._events if event.event_id > event_id]
            selected = events[-limit:]
        return {"items": [event.to_payload() for event in selected]}

    def _initialize_default_sources(self) -> None:
        now = _now_iso()
        defaults = {
            "dom": "DOM snapshot owner is not attached in this skeleton.",
            "mutation": "Mutation observer is not attached in this skeleton.",
            "listen": "Network/LISTEN observer is not attached in this skeleton.",
            "actions": "Runtime-v2 action audit is ready.",
        }
        for source, message in defaults.items():
            self._source_states[source] = AuditSourceState(
                source=source,
                status="inactive" if source != "actions" else "active",
                message=message,
                updated_at=now,
                payload={},
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
