"""Small shared vocabulary for operational decisions; state lives in the existing hop."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .signals import RoleSignals


class Action(str, Enum):
    WAIT = "wait"
    ALLOW = "allow"
    REFRESH = "refresh"
    REPAIR = "repair"
    ACCEPT = "accept"
    BLOCK = "block"
    STATUS = "status"
    COOLDOWN = "cooldown"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    response: Any = None
    validation_error: str | None = None


@dataclass(frozen=True)
class Policy:
    allow_stable_seconds: float = 5.0
    post_allow_seconds: float = 5.0
    response_stable_seconds: float = 5.0
    minimum_samples: int = 2
    timeout_seconds: float = 600.0
    reload_settle_seconds: float = 20.0


@dataclass
class Context:
    signals: RoleSignals
    wait: dict[str, Any]
    policy: Policy
    now: datetime
    phase: str = "waiting"
    candidate: Any = None
    validation_error: str | None = None
    candidate_stable: bool = False
    candidate_key: str = ""

    @property
    def snapshot(self) -> Any:
        return self.signals.snapshot

    @property
    def permission(self) -> Mapping[str, Any] | None:
        return self.signals.permission

    @property
    def network_status(self) -> str | None:
        return self.signals.network_status

    @property
    def transport_active(self) -> bool:
        """Current generation activity from the normalized role signal frame."""
        return self.signals.transport_active

    @property
    def permission_present(self) -> bool:
        return self.signals.permission_present

    @property
    def draft_present(self) -> bool:
        return self.signals.draft_present

    @property
    def composer_ready(self) -> bool:
        return self.signals.composer_ready
