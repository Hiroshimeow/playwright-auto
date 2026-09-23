"""Legacy session-capability observation value used by diagnostics only.

Normal workflow Send admission does not depend on this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SessionCapabilityObservation:
    endpoint_id: str
    conversation_id: str
    conversation_generation: int
    connector_id: str
    target_alias: str | None
    target_device_id: str | None
    principal_scope: str | None
    project_binding: str | None
    required_operations: tuple[str, ...]
    policy_revision: str
    status: str
    observed_at: str
    session_revision: str
    reason_code: str | None = None
    reason_detail: str | None = None
    evidence_ref: str | None = None
    recovery_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SessionCapabilityObservation":
        if not isinstance(value, Mapping):
            raise TypeError("session capability observation must be a mapping")
        status = str(value.get("status") or "").strip().lower()
        if status not in {"ready", "unavailable", "unknown"}:
            raise ValueError("invalid session capability status")
        return cls(
            endpoint_id=str(value.get("endpoint_id") or ""),
            conversation_id=str(value.get("conversation_id") or ""),
            conversation_generation=int(value.get("conversation_generation") or 0),
            connector_id=str(value.get("connector_id") or ""),
            target_alias=(str(value["target_alias"]) if value.get("target_alias") is not None else None),
            target_device_id=(str(value["target_device_id"]) if value.get("target_device_id") is not None else None),
            principal_scope=(str(value["principal_scope"]) if value.get("principal_scope") is not None else None),
            project_binding=(str(value["project_binding"]) if value.get("project_binding") is not None else None),
            required_operations=tuple(str(item) for item in value.get("required_operations") or ()),
            policy_revision=str(value.get("policy_revision") or ""),
            status=status,
            observed_at=str(value.get("observed_at") or ""),
            session_revision=str(value.get("session_revision") or ""),
            reason_code=(str(value["reason_code"]) if value.get("reason_code") is not None else None),
            reason_detail=(str(value["reason_detail"]) if value.get("reason_detail") is not None else None),
            evidence_ref=(str(value["evidence_ref"]) if value.get("evidence_ref") is not None else None),
            recovery_id=(str(value["recovery_id"]) if value.get("recovery_id") is not None else None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "conversation_id": self.conversation_id,
            "conversation_generation": self.conversation_generation,
            "connector_id": self.connector_id,
            "target_alias": self.target_alias,
            "target_device_id": self.target_device_id,
            "principal_scope": self.principal_scope,
            "project_binding": self.project_binding,
            "required_operations": list(self.required_operations),
            "policy_revision": self.policy_revision,
            "status": self.status,
            "observed_at": self.observed_at,
            "session_revision": self.session_revision,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "evidence_ref": self.evidence_ref,
            "recovery_id": self.recovery_id,
        }
