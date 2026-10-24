"""AsyncContinuation wake/delivery owner for managed TeamTask role turns.

One durable record per continuation lives in ``state["async_continuations"]``.
Phase 1.5 keeps its external-script compatibility callback shape; Gate B adds
ProviderJob and TeamTask source references while source lifecycle/result truth
stays with those source owners. This module owns acknowledgement linkage, WAIT
admission, delivery state, and parent-conversation checks only. The worker keeps
hop creation and the existing exactly-once command-ledger Send path; no writer
lease, ConversationBinding, second source lifecycle, or second Send owner exists.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, MutableMapping

from .cdpa_commands import conversation_identity
from .cdpa_dependencies import (
    ContinuationWaitEdge,
    dependency_readiness,
    validate_combined_wait_for_graph,
)
from .cdpa_materials import register_material

WAIT_ROUTE = "WAIT"
WAITING_CODE = "async_callback"
CALLBACK_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "partial"})
SOURCE_KINDS = frozenset({"external_script", "provider_job", "teamtask"})
COMPLETION_POLICIES = frozenset({"all", "each"})
FAILURE_POLICIES = frozenset({"block", "same_role_failure_handoff"})
OUTPUT_TAIL_CHARS = 4000
MAX_RESULT_REFS = 20
_WAIT_ADMISSIBLE_STATES = frozenset(
    {"spawn_confirmed", "source_acknowledged", "callback_received", "source_terminal"}
)


class AsyncContinuationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def continuation_id_for(task_id: str, spawn_idempotency_key: str) -> str:
    """Deterministic id so a duplicate spawn confirmation resolves to the same record."""
    digest = hashlib.sha256(f"{task_id}\n{spawn_idempotency_key}".encode("utf-8")).hexdigest()
    return f"ac-{digest[:24]}"


def _active_hop(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    active_hop_id = state.get("active_hop_id")
    return next(
        (
            hop
            for hop in state.get("hops") or ()
            if isinstance(hop, Mapping) and hop.get("hop_id") == active_hop_id
        ),
        None,
    )


def _registry(state: MutableMapping[str, Any]) -> dict[str, Any]:
    registry = state.setdefault("async_continuations", {})
    if not isinstance(registry, dict):
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "continuation registry is not an object")
    return registry


def get(state: Mapping[str, Any], continuation_id: str) -> dict[str, Any] | None:
    registry = state.get("async_continuations")
    record = registry.get(continuation_id) if isinstance(registry, Mapping) else None
    return record if isinstance(record, dict) else None


def register_spawn(
    state: MutableMapping[str, Any],
    *,
    continuation_id: str,
    role: str,
    job_id: str,
    spawn_idempotency_key: str,
    wake_prompt: str | None,
    failure_policy: str,
    now: str,
) -> dict[str, Any]:
    """Record a confirmed external spawn for the active role turn (idempotent)."""
    task_id = str(state.get("task_id") or "")
    if continuation_id != continuation_id_for(task_id, spawn_idempotency_key):
        raise AsyncContinuationError(
            "ASYNC_CONTINUATION_INVALID", "continuation id does not match its task and spawn key"
        )
    requested = {
        "role_key": role,
        "job_id": job_id,
        "spawn_idempotency_key": spawn_idempotency_key,
        "wake_prompt": wake_prompt,
        "failure_policy": failure_policy,
    }
    existing = get(state, continuation_id)
    if existing is not None:
        if {key: existing.get(key) for key in requested} != requested:
            raise AsyncContinuationError(
                "ASYNC_SPAWN_CONFLICT", "spawn key is already registered with different job metadata"
            )
        return existing
    if failure_policy not in FAILURE_POLICIES:
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "unsupported failure_policy")
    hop = _active_hop(state)
    if (
        str(state.get("status") or "").upper() != "RUNNING"
        or str(state.get("workflow_mode") or "team").strip().lower() != "team"
        or not str(state.get("route_contract_id") or "").strip()
        or str(state.get("active_role") or "").upper() != role
        or hop is None
        or str(hop.get("target_role") or "").upper() != role
        or str(hop.get("state") or "") not in {"sent", "waiting"}
        or not isinstance(hop.get("receipt"), Mapping)
    ):
        raise AsyncContinuationError(
            "ASYNC_SPAWN_REJECTED",
            "async spawn may register only during the active accepted turn of that TeamTask role",
        )
    if state.get("conversation_mode") == "temporary":
        raise AsyncContinuationError(
            "ASYNC_SPAWN_REJECTED", "async WAIT requires a persistent parent role conversation"
        )
    record = {
        "continuation_id": continuation_id,
        "task_id": task_id,
        **requested,
        "hop_id": hop.get("hop_id"),
        "request_id": hop.get("request_id"),
        "route_contract_id": state.get("route_contract_id"),
        "target_ref": {
            "task_id": task_id,
            "role_key": role,
            "route_contract_id": state.get("route_contract_id"),
        },
        "wake_policy": "same_role",
        "timeout_at": None,
        "source_kind": "external_script",
        "source_refs": [job_id],
        "source_acknowledged_at": now,
        "completion_policy": "all",
        "state": "spawn_confirmed",
        "registered_at": now,
        "spawn_confirmed_at": now,
        "parent": None,
        "callback": None,
        "source_results": {},
        "wake": None,
    }
    _registry(state)[continuation_id] = record
    return record


def register_source(
    state: MutableMapping[str, Any],
    *,
    continuation_id: str,
    role: str,
    source_kind: str,
    source_refs: list[str] | tuple[str, ...],
    failure_policy: str,
    completion_policy: str = "all",
    wake_prompt: str | None = None,
    timeout_at: str | None = None,
    now: str,
    all_tasks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Register acknowledged Gate-B ProviderJob/TeamTask sources without owning them."""
    kind = str(source_kind).strip().lower()
    if kind not in {"provider_job", "teamtask"}:
        raise AsyncContinuationError(
            "ASYNC_CONTINUATION_INVALID",
            "Gate-B source_kind must be provider_job or teamtask",
        )
    refs = tuple(dict.fromkeys(str(item).strip() for item in source_refs if str(item).strip()))
    if not refs:
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "source_refs must not be empty")
    if failure_policy not in FAILURE_POLICIES or completion_policy not in COMPLETION_POLICIES:
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "unsupported continuation policy")
    task_id = str(state.get("task_id") or "").strip()
    hop = _active_hop(state)
    if (
        str(state.get("status") or "").upper() != "RUNNING"
        or str(state.get("workflow_mode") or "team").strip().lower() != "team"
        or not str(state.get("route_contract_id") or "").strip()
        or str(state.get("active_role") or "").upper() != role
        or hop is None
        or str(hop.get("target_role") or "").upper() != role
    ):
        raise AsyncContinuationError(
            "ASYNC_SPAWN_REJECTED",
            "continuation source may register only for the active TeamTask role turn",
        )
    if kind == "teamtask":
        tasks = list(all_tasks)
        if not any(str(item.get("task_id") or "") == task_id for item in tasks):
            tasks.append(state)
        try:
            validate_combined_wait_for_graph(
                tasks,
                additional_edges=(
                    ContinuationWaitEdge(task_id, source_id, continuation_id)
                    for source_id in refs
                ),
            )
        except ValueError as exc:
            raise AsyncContinuationError("ASYNC_WAIT_CYCLE", str(exc)) from exc
    requested = {
        "role_key": role,
        "source_kind": kind,
        "source_refs": list(refs),
        "wake_prompt": wake_prompt,
        "failure_policy": failure_policy,
        "completion_policy": completion_policy,
        "timeout_at": timeout_at,
    }
    existing = get(state, continuation_id)
    if existing is not None:
        if {key: existing.get(key) for key in requested} != requested:
            raise AsyncContinuationError(
                "ASYNC_SPAWN_CONFLICT",
                "continuation id is already registered with different sources",
            )
        return existing
    record = {
        "continuation_id": continuation_id,
        "task_id": task_id,
        **requested,
        "hop_id": hop.get("hop_id"),
        "request_id": hop.get("request_id"),
        "route_contract_id": state.get("route_contract_id"),
        "target_ref": {
            "task_id": task_id,
            "role_key": role,
            "route_contract_id": state.get("route_contract_id"),
        },
        "wake_policy": "same_role",
        "cycle_validation": {
            "status": "passed" if kind == "teamtask" else "not_applicable",
            "graph": "depends_on_task_ids+continuation_waits" if kind == "teamtask" else None,
        },
        "source_acknowledged_at": now,
        "state": "source_acknowledged",
        "registered_at": now,
        "parent": None,
        "callback": None,
        "source_results": {},
        "wake": None,
    }
    _registry(state)[continuation_id] = record
    return record


def record_source_terminal(
    state: MutableMapping[str, Any],
    continuation_id: str,
    source_ref: str,
    *,
    status: str,
    material_ids: list[str] | tuple[str, ...] = (),
    summary: str | None = None,
    now: str,
) -> tuple[dict[str, Any], bool]:
    """Record terminal source evidence only; source lifecycle remains source-owned."""
    record = get(state, continuation_id)
    if record is None:
        raise AsyncContinuationError("ASYNC_CONTINUATION_UNKNOWN", "continuation is not registered")
    source = str(source_ref).strip()
    refs = tuple(str(item) for item in record.get("source_refs") or ())
    if source not in refs:
        raise AsyncContinuationError("ASYNC_SOURCE_UNKNOWN", "source is not registered on this continuation")
    normalized = str(status).strip().lower()
    if normalized not in CALLBACK_STATUSES:
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "unsupported source terminal status")
    results = record.setdefault("source_results", {})
    if not isinstance(results, dict):
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "source_results is not an object")
    projected = {
        "status": normalized,
        "material_ids": [str(item) for item in material_ids],
        "summary": str(summary or "")[:2000] or None,
        "received_at": now,
    }
    existing = results.get(source)
    if existing is not None:
        if existing == projected or (
            isinstance(existing, Mapping)
            and existing.get("status") == projected["status"]
            and existing.get("material_ids") == projected["material_ids"]
            and existing.get("summary") == projected["summary"]
        ):
            return record, False
        raise AsyncContinuationError("ASYNC_CALLBACK_CONFLICT", "source already terminalized differently")
    results[source] = projected
    terminal_refs = set(results)
    if record.get("completion_policy") == "each" or terminal_refs.issuperset(refs):
        record["state"] = "source_terminal"
    return record, True


def teamtask_terminal_matches(
    target: Mapping[str, Any], source_task_id: str
) -> tuple[str, ...]:
    registry = target.get("async_continuations")
    if not isinstance(registry, Mapping):
        return ()
    return tuple(
        str(continuation_id)
        for continuation_id, record in registry.items()
        if isinstance(record, Mapping)
        and record.get("source_kind") == "teamtask"
        and source_task_id in (record.get("source_refs") or ())
        and str(record.get("state") or "") not in {"delivered", "cancelled", "superseded"}
    )


def source_readiness(
    record: Mapping[str, Any], all_tasks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]
):
    if record.get("source_kind") != "teamtask":
        return None
    synthetic = {
        "task_id": f"continuation:{record.get('continuation_id')}",
        "depends_on_task_ids": list(record.get("source_refs") or ()),
    }
    return dependency_readiness(synthetic, all_tasks)


def wait_admissible(state: Mapping[str, Any], role: str) -> bool:
    """WAIT is in the route enum only while a confirmed spawn belongs to this role turn."""
    registry = state.get("async_continuations")
    if not isinstance(registry, Mapping):
        return False
    return any(
        isinstance(record, Mapping)
        and record.get("role_key") == role
        and record.get("hop_id") == state.get("active_hop_id")
        and record.get("state") in _WAIT_ADMISSIBLE_STATES
        for record in registry.values()
    )


def wait_continuation(
    state: Mapping[str, Any], hop: Mapping[str, Any], continuation_id: str
) -> dict[str, Any]:
    record = get(state, str(continuation_id or "").strip())
    if (
        record is None
        or record.get("role_key") != str(hop.get("target_role") or "").upper()
        or record.get("hop_id") != hop.get("hop_id")
        or record.get("route_contract_id") != state.get("route_contract_id")
        or record.get("state") not in _WAIT_ADMISSIBLE_STATES
    ):
        raise ValueError("WAIT handoff must name a confirmed continuation of this role turn")
    return record


def parent_identity(state: Mapping[str, Any], hop: Mapping[str, Any]) -> dict[str, Any]:
    """Exact parent role conversation evidence captured when the role returns WAIT."""
    receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
    binding = receipt.get("binding") if isinstance(receipt.get("binding"), Mapping) else {}
    role = str(hop.get("target_role") or "").upper()
    role_record = (state.get("roles") or {}).get(role) or {}
    return {
        "role_key": role,
        "conversation_id": str(receipt.get("conversation_id") or "").strip() or None,
        "page_url": str(role_record.get("page_url") or "").strip() or None,
        "page_id": binding.get("page_id"),
        "conversation_generation": int(role_record.get("conversation_generation") or 0),
        "user_message_id": receipt.get("user_message_id"),
        "request_id": hop.get("request_id"),
        "hop_id": hop.get("hop_id"),
    }


def parent_identity_error(state: Mapping[str, Any], record: Mapping[str, Any]) -> str | None:
    """Durable pre-wake check that the role still owns the exact parent conversation."""
    parent = record.get("parent") if isinstance(record.get("parent"), Mapping) else {}
    conversation_id = str(parent.get("conversation_id") or "").strip()
    if not conversation_id:
        return "parent role conversation has no canonical identity"
    if str(state.get("route_contract_id") or "") != str(record.get("route_contract_id") or ""):
        return "parent task route contract changed"
    role = str(record.get("role_key") or "")
    role_record = (state.get("roles") or {}).get(role)
    if not isinstance(role_record, Mapping):
        return "parent role slot is missing"
    if int(role_record.get("conversation_generation") or 0) != int(parent.get("conversation_generation") or 0):
        return "parent role conversation generation changed"
    if conversation_identity(role_record.get("page_url")) != f"/c/{conversation_id}":
        return "parent role page no longer points to the parent conversation"
    return None


def page_identity_error(record: Mapping[str, Any], *, url: Any, new_chat: bool) -> str | None:
    """Wake-time check of the acquired role page before the existing Send owner runs."""
    parent = record.get("parent") if isinstance(record.get("parent"), Mapping) else {}
    conversation_id = str(parent.get("conversation_id") or "").strip()
    if new_chat:
        return "acquisition opened a new chat instead of the parent conversation"
    if not conversation_id or conversation_identity(url) != f"/c/{conversation_id}":
        return "acquired role page is not the exact parent conversation"
    return None


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _tail(text: str | None) -> str | None:
    if text is None:
        return None
    value = str(text)
    return value[-OUTPUT_TAIL_CHARS:]


def record_callback(
    state: MutableMapping[str, Any],
    continuation_id: str,
    payload: Mapping[str, Any],
    *,
    now: str,
) -> tuple[dict[str, Any], bool]:
    """Record one callback result; returns (record, newly_recorded).

    A duplicate delivery of the same payload is a no-op. A different payload for an
    already completed continuation fails closed with ASYNC_CALLBACK_CONFLICT.
    """
    record = get(state, continuation_id)
    if record is None:
        raise AsyncContinuationError("ASYNC_CONTINUATION_UNKNOWN", "continuation is not registered for this task")
    digest = _payload_sha256(payload)
    existing = record.get("callback")
    if isinstance(existing, Mapping):
        if existing.get("payload_sha256") == digest:
            return record, False
        raise AsyncContinuationError(
            "ASYNC_CALLBACK_CONFLICT", "continuation already completed with a different callback payload"
        )
    status = str(payload.get("status") or "")
    if status not in CALLBACK_STATUSES:
        raise AsyncContinuationError("ASYNC_CONTINUATION_INVALID", "unsupported callback status")
    role = str(record.get("role_key") or "")
    material_ids: list[str] = []
    for stream in ("stdout", "stderr"):
        tail = _tail(payload.get(stream))
        if tail is None:
            continue
        material = register_material(
            state,
            source_kind="provider_result",
            material_type="log",
            version=continuation_id,
            content=tail,
            role_key=role,
            material_id=f"{continuation_id}-{stream}",
        )
        material_ids.append(material["material_id"])
    for index, ref in enumerate(list(payload.get("result_refs") or ())[:MAX_RESULT_REFS], start=1):
        material = register_material(
            state,
            source_kind="provider_result",
            material_type="artifact",
            version=continuation_id,
            content_ref=str(ref),
            role_key=role,
            material_id=f"{continuation_id}-result-{index}",
        )
        material_ids.append(material["material_id"])
    record["callback"] = {
        "status": status,
        "exit_code": payload.get("exit_code"),
        "summary": str(payload.get("summary") or "")[:2000] or None,
        "stdout_tail": _tail(payload.get("stdout")),
        "stderr_tail": _tail(payload.get("stderr")),
        "material_ids": material_ids,
        "payload_sha256": digest,
        "received_at": now,
    }
    record["state"] = "callback_received"
    return record, True


def wake_handoff(record: Mapping[str, Any]) -> str:
    """Bounded same-role wake input projected from source-owned terminal evidence."""
    callback = record.get("callback") if isinstance(record.get("callback"), Mapping) else {}
    source_results = (
        record.get("source_results") if isinstance(record.get("source_results"), Mapping) else {}
    )
    lines = [f"ASYNC_CALLBACK continuation_id={record.get('continuation_id')}"]
    if callback:
        lines.append(
            f"job_id={record.get('job_id')} status={callback.get('status')} "
            f"exit_code={callback.get('exit_code')}"
        )
        if callback.get("summary"):
            lines.append(f"summary: {callback['summary']}")
        if callback.get("material_ids"):
            lines.append("materials: " + ", ".join(str(item) for item in callback["material_ids"]))
        for stream in ("stdout", "stderr"):
            tail = callback.get(f"{stream}_tail")
            if tail:
                lines.append(f"{stream} (tail):\n{str(tail)[-1500:]}")
    else:
        for source_ref in record.get("source_refs") or ():
            result = source_results.get(source_ref) if isinstance(source_results, Mapping) else None
            if not isinstance(result, Mapping):
                continue
            lines.append(f"source={source_ref} status={result.get('status')}")
            if result.get("summary"):
                lines.append(f"summary: {result['summary']}")
            if result.get("material_ids"):
                lines.append("materials: " + ", ".join(str(item) for item in result["material_ids"]))
    if record.get("wake_prompt"):
        lines.append(f"wake instruction: {record['wake_prompt']}")
    lines.append("Continue this role turn from the async result; do not restart the source work.")
    return "\n".join(lines)


def wake_allowed(record: Mapping[str, Any]) -> bool:
    callback = record.get("callback") if isinstance(record.get("callback"), Mapping) else {}
    if callback:
        return callback.get("status") == "succeeded" or (
            record.get("failure_policy") == "same_role_failure_handoff"
        )
    results = record.get("source_results") if isinstance(record.get("source_results"), Mapping) else {}
    refs = tuple(str(item) for item in record.get("source_refs") or ())
    if not refs or not set(results).issuperset(refs):
        return False
    statuses = [str((results.get(ref) or {}).get("status") or "") for ref in refs]
    return all(status == "succeeded" for status in statuses) or (
        record.get("failure_policy") == "same_role_failure_handoff"
    )
