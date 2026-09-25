"""Small shared vocabulary for operational decisions; state lives in the existing hop."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


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
    snapshot: Any
    wait: dict[str, Any]
    policy: Policy
    now: datetime
    permission: Mapping[str, Any] | None = None
    phase: str = "waiting"
    candidate: Any = None
    validation_error: str | None = None
    candidate_stable: bool = False
    candidate_key: str = ""
    network_status: str | None = None

    @property
    def transport_active(self) -> bool:
        """Authoritative generation activity used for operational decisions.

        response_activity_turn_id is intentionally excluded: it is DOM progress
        evidence for audit/retry diagnostics, not proof that generation is live.
        """
        state = getattr(self.snapshot, "state", None)
        state_value = str(getattr(state, "value", state) or "").lower()
        return bool(
            getattr(self.snapshot, "stop_visible", False)
            or state_value == "responding"
            or str(self.network_status or "").upper()
            in {"IS_STREAMING", "IS_STOP_REQUESTED"}
        )

    @property
    def permission_present(self) -> bool:
        return bool(
            self.permission
            or int(getattr(self.snapshot, "mcp_permission_node_count", 0) or 0)
            or int(getattr(self.snapshot, "mcp_permission_allow_count", 0) or 0)
        )

    @property
    def draft_present(self) -> bool:
        return bool(
            str(getattr(self.snapshot, "composer_text", "") or "").strip()
            or getattr(self.snapshot, "attachment_markers", ())
            or int(getattr(self.snapshot, "attachment_count", 0) or 0)
        )

    @property
    def composer_ready(self) -> bool:
        return bool(
            getattr(self.snapshot, "composer_present", False)
            and getattr(self.snapshot, "composer_editable", True)
        )
