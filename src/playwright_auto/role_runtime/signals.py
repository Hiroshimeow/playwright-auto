"""Normalized role signals and their semantic trace.

Raw DOM/network capture remains provider-specific. This module owns the normalized
observation shape consumed by policy and the semantic logs used to diagnose it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..observability import append_live_event


@dataclass(frozen=True)
class SignalIdentity:
    page_url: str = ""
    page_id: str | None = None
    role: str | None = None
    task_id: str | None = None
    team: str | None = None
    request_id: str | None = None

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Any,
        *,
        request_id: str | None = None,
        page_id: str | None = None,
        role: str | None = None,
        task_id: str | None = None,
        team: str | None = None,
    ) -> "SignalIdentity":
        return cls(
            page_url=str(getattr(snapshot, "url", "") or ""),
            page_id=getattr(snapshot, "page_id", None) or page_id,
            role=getattr(snapshot, "page_role", None) or role,
            task_id=getattr(snapshot, "page_task_id", None) or task_id,
            team=getattr(snapshot, "page_team", None) or team,
            request_id=request_id,
        )

    def event_kwargs(self) -> dict[str, Any]:
        return {
            "page_url": self.page_url,
            "page_id": self.page_id,
            "role": self.role,
            "task_id": self.task_id,
            "team": self.team,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class RoleSignals:
    snapshot: Any
    permission: Mapping[str, Any] | None = None
    network_status: str | None = None
    network_response: Mapping[str, Any] | None = None
    diagnostics: Mapping[str, Any] | None = None
    listener_attached: bool = False
    streaming_supported: bool = False

    @classmethod
    def from_sources(
        cls, snapshot: Any, observation: Mapping[str, Any] | None = None
    ) -> "RoleSignals":
        raw = observation if isinstance(observation, Mapping) else {}
        permission = raw.get("permission_action")
        response = raw.get("response")
        diagnostics = raw.get("diagnostics")
        return cls(
            snapshot=snapshot,
            permission=(dict(permission) if isinstance(permission, Mapping) else None),
            network_status=(str(raw.get("status")) if raw.get("status") else None),
            network_response=(dict(response) if isinstance(response, Mapping) else None),
            diagnostics=(dict(diagnostics) if isinstance(diagnostics, Mapping) else {}),
            listener_attached=bool(raw.get("listener_attached")),
            streaming_supported=bool(raw.get("streaming_supported")),
        )

    @property
    def transport_active(self) -> bool:
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
    def composer_ambiguous(self) -> bool:
        return bool(getattr(self.snapshot, "composer_ambiguous", False))

    @property
    def composer_ready(self) -> bool:
        return bool(
            getattr(self.snapshot, "composer_present", False)
            and getattr(self.snapshot, "composer_editable", True)
            and not getattr(self.snapshot, "composer_ambiguous", False)
        )

    def listen_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.network_status:
            values["status"] = self.network_status
        response = self.network_response
        if isinstance(response, Mapping):
            values["response_message_id"] = str(
                response.get("message_id") or response.get("id") or ""
            )
            values["response_turn_id"] = str(response.get("turn_id") or "")
            values["response_text_length"] = len(str(response.get("text") or ""))
        if self.permission is not None:
            values["permission_target"] = str(
                self.permission.get("target_message_id") or ""
            )
        return values

    def debug_values(self) -> dict[str, Any]:
        snapshot = self.snapshot
        response = self.network_response or {}
        errors = tuple(str(v)[:240] for v in getattr(snapshot, "error_texts", ()) or ())
        dialogs = tuple(str(v)[:240] for v in getattr(snapshot, "blocking_dialogs", ()) or ())
        choices = tuple(str(v)[:160] for v in getattr(snapshot, "choice_prompt_labels", ()) or ())
        diagnostics = {
            str(key): int(value)
            for key, value in (self.diagnostics or {}).items()
            if isinstance(value, int) and value
        }
        return {
            "dom_state": str(
                getattr(getattr(snapshot, "state", None), "value",
                        getattr(snapshot, "state", None)) or ""
            ),
            "dom_composer_present": bool(getattr(snapshot, "composer_present", False)),
            "dom_composer_nonempty": bool(
                str(getattr(snapshot, "composer_text", "") or "").strip()
            ),
            "dom_composer_ambiguous": bool(
                getattr(snapshot, "composer_ambiguous", False)
            ),
            "dom_composer_owner_source": str(
                getattr(snapshot, "composer_owner_source", "") or ""
            ),
            "dom_stop_visible": bool(getattr(snapshot, "stop_visible", False)),
            "dom_retry_visible": bool(getattr(snapshot, "retry_visible", False)),
            "dom_errors": list(errors),
            "dom_dialogs": list(dialogs),
            "dom_choices": list(choices),
            "dom_permission_count": int(
                getattr(snapshot, "mcp_permission_node_count", 0) or 0
            ),
            "listen_status": self.network_status,
            "listen_response_message_id": str(
                response.get("message_id") or response.get("id") or ""
            ),
            "listen_response_turn_id": str(response.get("turn_id") or ""),
            "listen_response_text_length": len(str(response.get("text") or "")),
            "listen_permission_target": str(
                (self.permission or {}).get("target_message_id") or ""
            ),
            "listener_attached": self.listener_attached,
            "streaming_supported": self.streaming_supported,
            "observer_diagnostics": diagnostics,
        }

    def listen_transcript(self) -> dict[str, Any] | None:
        response = self.network_response
        if not isinstance(response, Mapping):
            return None
        text = str(response.get("text") or "")
        message_id = str(response.get("message_id") or response.get("id") or "")
        if not message_id or not text.strip():
            return None
        return {
            "author": "assistant",
            "message_id": message_id,
            "turn_id": str(response.get("turn_id") or "") or None,
            "text": text[:24000],
            "text_length": len(text),
            "complete": True,
            "projection_source": "listen",
        }


def probe_values(probe: Any) -> dict[str, Any]:
    return {
        "composer_present": bool(getattr(probe, "composer_present", False)),
        "composer_nonempty": bool(
            str(getattr(probe, "composer_text", "") or "").strip()
        ),
        "composer_ambiguous": bool(
            getattr(probe, "composer_ambiguous", False)
        ),
        "composer_owner_source": str(
            getattr(probe, "composer_owner_source", "") or ""
        ),
        "attachment_count": int(getattr(probe, "attachment_count", 0) or 0),
        "stop_visible": bool(getattr(probe, "stop_visible", False)),
        "retry_visible": bool(getattr(probe, "retry_visible", False)),
        "transport_active": bool(getattr(probe, "transport_active", False)),
        "requires_login": bool(getattr(probe, "requires_login", False)),
        "error_count": len(tuple(getattr(probe, "error_texts", ()) or ())),
        "dialog_count": len(tuple(getattr(probe, "blocking_dialogs", ()) or ())),
        "choice_count": len(
            tuple(getattr(probe, "choice_prompt_labels", ()) or ())
        ),
        "mcp_permission_node_count": int(
            getattr(probe, "mcp_permission_node_count", 0) or 0
        ),
        "mcp_permission_allow_count": int(
            getattr(probe, "mcp_permission_allow_count", 0) or 0
        ),
        "last_user_message_id": getattr(probe, "last_user_message_id", None),
        "last_user_turn_id": getattr(probe, "last_user_turn_id", None),
        "last_assistant_message_id": getattr(
            probe, "last_assistant_message_id", None
        ),
        "last_assistant_turn_id": getattr(probe, "last_assistant_turn_id", None),
        "assistant_text_length": int(
            getattr(probe, "assistant_text_length", 0) or 0
        ),
        "response_activity_length": int(
            getattr(probe, "response_activity_length", 0) or 0
        ),
        "response_activity_turn_id": getattr(
            probe, "response_activity_turn_id", None
        ),
    }


def transcript_messages(probe: Any) -> tuple[dict[str, Any], ...]:
    messages: list[dict[str, Any]] = []
    last_user_message_id = getattr(probe, "last_user_message_id", None)
    last_user_text = str(getattr(probe, "last_user_text", "") or "")
    if last_user_message_id and last_user_text:
        messages.append({
            "author": "user",
            "message_id": last_user_message_id,
            "turn_id": getattr(probe, "last_user_turn_id", None),
            "text": last_user_text,
            "text_length": len(last_user_text),
            "complete": True,
        })

    activity_turn_id = getattr(probe, "response_activity_turn_id", None)
    assistant_turn_id = getattr(probe, "last_assistant_turn_id", None)
    activity_text = str(getattr(probe, "response_activity_text", "") or "")
    assistant_text = str(getattr(probe, "assistant_text", "") or "")
    activity_is_newer = bool(
        activity_turn_id
        and activity_turn_id != assistant_turn_id
        and activity_text
    )
    if activity_is_newer:
        messages.append({
            "author": "assistant",
            "message_id": None,
            "turn_id": activity_turn_id,
            "text": activity_text,
            "text_length": int(
                getattr(probe, "response_activity_length", 0) or len(activity_text)
            ),
            "complete": False,
        })
    elif getattr(probe, "last_assistant_message_id", None) and assistant_text:
        logical_turn_id = assistant_turn_id or activity_turn_id
        active = bool(
            getattr(probe, "stop_visible", False)
            or getattr(probe, "transport_active", False)
        ) and (activity_turn_id is None or logical_turn_id == activity_turn_id)
        messages.append({
            "author": "assistant",
            "message_id": getattr(probe, "last_assistant_message_id", None),
            "turn_id": logical_turn_id,
            "text": assistant_text,
            "text_length": int(
                getattr(probe, "assistant_text_length", 0) or len(assistant_text)
            ),
            "complete": not active,
        })
    elif activity_turn_id and activity_text:
        messages.append({
            "author": "assistant",
            "message_id": None,
            "turn_id": activity_turn_id,
            "text": activity_text,
            "text_length": int(
                getattr(probe, "response_activity_length", 0) or len(activity_text)
            ),
            "complete": False,
        })
    return tuple(messages)



def _emit_signal(
    identity: SignalIdentity,
    source: str,
    kind: str,
    *,
    changes: Mapping[str, Any] | None = None,
    values: Mapping[str, Any] | None = None,
    detail: str | None = None,
) -> None:
    """The only semantic signal-log sink for role-runtime observations."""
    append_live_event(
        source,
        kind,
        **identity.event_kwargs(),
        changes=dict(changes or {}),
        values=dict(values or {}),
        detail=detail,
    )


def record_transcript(identity: SignalIdentity, values: Mapping[str, Any]) -> None:
    _emit_signal(identity, "TRANSCRIPT", "message", values=values)


def record_probe(current: Any, previous: Any | None = None) -> None:
    current_values = probe_values(current)
    previous_values = probe_values(previous) if previous is not None else {}
    changes = {
        key: {"from": previous_values.get(key), "to": value}
        for key, value in current_values.items()
        if previous is None or previous_values.get(key) != value
    }
    identity = SignalIdentity(
        page_url=str(getattr(current, "url", "") or ""),
        page_id=getattr(current, "page_id", None),
        role=getattr(current, "page_role", None),
        task_id=getattr(current, "page_task_id", None),
        team=getattr(current, "page_team", None),
    )
    if changes:
        _emit_signal(
            identity,
            "DOM",
            "wait_probe",
            changes=changes,
            values=current_values,
            detail=("initial probe" if previous is None else None),
        )
    for values in transcript_messages(current):
        record_transcript(identity, values)


def record_full_snapshot(snapshot: Any) -> None:
    identity = SignalIdentity.from_snapshot(snapshot)
    messages = tuple(getattr(snapshot, "messages", ()) or ())[-24:]
    latest_assistant_message_id = next(
        (
            getattr(item, "message_id", None)
            for item in reversed(messages)
            if getattr(item, "role", None) == "assistant"
            and str(getattr(item, "text", "") or "").strip()
        ),
        None,
    )
    for message in messages:
        text = str(getattr(message, "text", "") or "")
        if not text.strip():
            continue
        role = str(getattr(message, "role", "") or "")
        message_id = getattr(message, "message_id", None)
        turn_id = getattr(message, "turn_id", None)
        complete = not (
            role == "assistant"
            and (
                (
                    getattr(snapshot, "response_activity_turn_id", None)
                    and turn_id == getattr(snapshot, "response_activity_turn_id", None)
                )
                or (
                    not getattr(snapshot, "response_activity_turn_id", None)
                    and bool(getattr(snapshot, "stop_visible", False))
                    and message_id == latest_assistant_message_id
                )
            )
        )
        record_transcript(
            identity,
            {
                "author": role,
                "message_id": message_id,
                "turn_id": turn_id,
                "text": text[:24000],
                "text_length": len(text),
                "complete": complete,
            },
        )


def record_cycle(
    signals: RoleSignals,
    wait: dict[str, Any],
    *,
    identity: SignalIdentity,
    phase: str,
    previous_decision: tuple[Any, Any],
    decision: Any,
) -> None:
    listen_values = signals.listen_values()
    listen_signature = json.dumps(
        listen_values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    listen_changed = bool(
        listen_values and wait.get("live_listen_signature") != listen_signature
    )
    if listen_changed:
        wait["live_listen_signature"] = listen_signature
        _emit_signal(identity, "LISTEN", "network_signal", values=listen_values)
        transcript = signals.listen_transcript()
        if transcript is not None:
            bound_message_id = str(wait.get("bound_assistant_message_id") or "").strip()
            bound_turn_id = str(wait.get("bound_assistant_turn_id") or "").strip()
            if (
                (bound_message_id and transcript.get("message_id") == bound_message_id)
                or (bound_turn_id and transcript.get("turn_id") == bound_turn_id)
            ):
                record_transcript(identity, transcript)

    current_decision = (
        str(
            getattr(
                getattr(decision, "action", None),
                "value",
                getattr(decision, "action", ""),
            )
            or ""
        ),
        str(getattr(decision, "reason", "") or ""),
    )
    bound_progress = wait.get("bound_assistant_progress")
    if isinstance(bound_progress, Mapping):
        bound_text = str(bound_progress.get("text") or "")
        record_transcript(
            identity,
            {
                "author": "assistant",
                "message_id": bound_progress.get("message_id"),
                "turn_id": bound_progress.get("turn_id"),
                "text": bound_text,
                "text_length": len(bound_text),
                "complete": current_decision[0] == "accept",
            },
        )

    if previous_decision != current_decision:
        values = signals.debug_values()
        values.update(
            {
                "phase": phase,
                "action": current_decision[0],
                "reason": current_decision[1],
            }
        )
        _emit_signal(identity, "CTRL", "role_decision", values=values)


def record_send_acceptance(
    identity: SignalIdentity,
    snapshot: Any,
    *,
    outcome: str,
    accepted_via: str | None = None,
) -> None:
    values = RoleSignals.from_sources(snapshot).debug_values()
    values.update(
        {
            "outcome": str(outcome),
            "accepted_via": str(accepted_via or ""),
            "authority": "dom",
        }
    )
    _emit_signal(identity, "SIGNAL", "send_acceptance", values=values)


def record_signal_error(
    identity: SignalIdentity,
    *,
    component: str,
    stage: str,
    error: BaseException,
) -> None:
    _emit_signal(
        identity,
        "SIGNAL",
        "error",
        values={
            "component": str(component),
            "stage": str(stage),
            "error_type": type(error).__name__,
        },
    )
