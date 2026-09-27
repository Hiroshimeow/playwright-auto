"""ChatGPT backend API/session/project helpers.

Browser UI ownership stays in chatgpt.py. This module owns only authenticated
backend reads/writes and the bounded stream-status cache.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any, Mapping

from .chatgpt_graph import (
    BackendAuthError,
    BackendNotReadyError,
    BackendSchemaError,
    BackendUnavailableError,
)

_BACKEND_CONTEXT_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_STREAM_STATUS_CACHE_LIMIT = 32
_STREAM_STATUS_TERMINAL = frozenset({"COMPLETE", "FAILURE"})


def _safe_identity_string(value: Any, *, max_length: int = 512) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if any(ord(char) < 0x20 for char in value):
        return None
    return value


def _backend_context_state(context: Any) -> dict[str, Any]:
    state = {"token": None, "stream_status": {}}
    try:
        existing = _BACKEND_CONTEXT_STATES.get(context)
        if existing is not None:
            return existing
        _BACKEND_CONTEXT_STATES[context] = state
        return state
    except TypeError:
        existing = getattr(context, "_playwright_auto_backend_state", None)
        if isinstance(existing, dict):
            return existing
        try:
            setattr(context, "_playwright_auto_backend_state", state)
        except Exception:
            pass
        return state


async def _backend_session_token(context: Any) -> str:
    state = _backend_context_state(context)
    token = state.get("token")
    if isinstance(token, str) and token:
        return token
    try:
        response = await context.request.get("https://chatgpt.com/api/auth/session")
    except Exception:
        raise BackendUnavailableError(0, "session") from None
    status = int(getattr(response, "status", 0) or 0)
    if status == 401:
        raise BackendAuthError("backend session is unauthorized")
    if status == 429 or status >= 500 or status < 200 or status >= 300:
        raise BackendUnavailableError(status, "session")
    try:
        payload = await response.json()
    except Exception:
        raise BackendSchemaError("session response is not valid JSON") from None
    if not isinstance(payload, Mapping):
        raise BackendSchemaError("session response must be an object")
    token = _safe_identity_string(payload.get("accessToken"), max_length=16_384)
    if token is None:
        raise BackendAuthError("backend session has no access token")
    state["token"] = token
    return token


async def _backend_get_object(
    context: Any,
    path: str,
    *,
    category: str,
    method: str = "get",
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = _backend_context_state(context)
    token = await _backend_session_token(context)
    for attempt in range(2):
        try:
            request = getattr(context.request, method)
            kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}"}}
            if data is not None:
                kwargs["data"] = dict(data)
            response = await request(f"https://chatgpt.com{path}", **kwargs)
        except Exception:
            raise BackendUnavailableError(0, category) from None
        status = int(getattr(response, "status", 0) or 0)
        if status == 401:
            state["token"] = None
            if attempt == 0:
                token = await _backend_session_token(context)
                continue
            raise BackendAuthError(f"{category} remained unauthorized after one refresh")
        if status == 404:
            raise BackendNotReadyError(f"{category} is not ready")
        if status == 429 or status >= 500 or status < 200 or status >= 300:
            raise BackendUnavailableError(status, category)
        try:
            payload = await response.json()
        except Exception:
            raise BackendSchemaError(f"{category} response is not valid JSON") from None
        if not isinstance(payload, Mapping):
            raise BackendSchemaError(f"{category} response must be an object")
        return dict(payload)
    raise BackendAuthError(f"{category} authentication failed")


def _prune_stream_status_cache(
    stream_status: dict[str, Any],
    *,
    now: float,
    reserve_for: str | None = None,
) -> None:
    if reserve_for is not None and reserve_for in stream_status:
        return
    target_size = _STREAM_STATUS_CACHE_LIMIT - (1 if reserve_for is not None else 0)
    if len(stream_status) <= target_size:
        return

    removable: list[tuple[int, float, str]] = []
    for conversation_id, raw_entry in stream_status.items():
        if not isinstance(raw_entry, Mapping):
            removable.append((0, 0.0, conversation_id))
            continue
        in_flight = raw_entry.get("in_flight")
        if isinstance(in_flight, asyncio.Future) and not in_flight.done():
            continue
        payload = raw_entry.get("payload")
        status = str(payload.get("status") or "") if isinstance(payload, Mapping) else ""
        next_poll_at = float(raw_entry.get("next_poll_at") or 0.0)
        if status in _STREAM_STATUS_TERMINAL:
            priority = 0
        elif next_poll_at <= now:
            priority = 1
        else:
            continue
        removable.append((priority, float(raw_entry.get("observed_at") or 0.0), conversation_id))

    for _priority, _observed_at, conversation_id in sorted(removable):
        if len(stream_status) <= target_size:
            break
        stream_status.pop(conversation_id, None)

    if len(stream_status) > target_size:
        raise BackendUnavailableError(0, "stream_status_slot_capacity")


def release_backend_stream_status(context: Any, conversation_id: str) -> None:
    exact_id = _safe_identity_string(conversation_id)
    if exact_id is None:
        return
    state = _backend_context_state(context)
    stream_status = state.setdefault("stream_status", {})
    entry = stream_status.get(exact_id)
    if isinstance(entry, Mapping):
        in_flight = entry.get("in_flight")
        if isinstance(in_flight, asyncio.Future) and not in_flight.done():
            return
    stream_status.pop(exact_id, None)


async def backend_stream_status(context: Any, conversation_id: str) -> dict[str, Any]:
    exact_id = _safe_identity_string(conversation_id)
    if exact_id is None:
        raise ValueError("conversation ID must be a bounded printable string")
    state = _backend_context_state(context)
    stream_status = state.setdefault("stream_status", {})
    now = time.monotonic()
    if exact_id not in stream_status:
        _prune_stream_status_cache(stream_status, now=now, reserve_for=exact_id)
    entry = stream_status.setdefault(exact_id, {})
    cached = entry.get("payload")
    if isinstance(cached, Mapping) and now < float(entry.get("next_poll_at") or 0.0):
        return dict(cached)
    in_flight = entry.get("in_flight")
    if isinstance(in_flight, asyncio.Future) and not in_flight.done():
        return dict(await asyncio.shield(in_flight))

    async def poll() -> dict[str, Any]:
        payload = await _backend_get_object(
            context,
            f"/backend-api/conversation/{exact_id}/stream_status",
            category="stream_status",
        )
        status = payload.get("status")
        if status not in {"IS_STREAMING", "COMPLETE", "FAILURE", "IS_STOP_REQUESTED"}:
            raise BackendSchemaError("stream_status response has unknown status")
        entry["payload"] = dict(payload)
        entry["observed_at"] = time.monotonic()
        entry["next_poll_at"] = time.monotonic() + 30.0
        return dict(payload)

    task = asyncio.create_task(poll())
    entry["in_flight"] = task
    try:
        return dict(await asyncio.shield(task))
    finally:
        if entry.get("in_flight") is task:
            entry["in_flight"] = None


async def backend_projects(context: Any) -> list[dict[str, str]]:
    payload = await _backend_get_object(
        context,
        "/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0",
        category="projects",
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise BackendSchemaError("projects response is missing items")
    projects: list[dict[str, str]] = []
    for item in items:
        gizmo = item.get("gizmo") if isinstance(item, Mapping) else None
        gizmo = gizmo.get("gizmo") if isinstance(gizmo, Mapping) else None
        display = gizmo.get("display") if isinstance(gizmo, Mapping) else None
        project_id = gizmo.get("id") if isinstance(gizmo, Mapping) else None
        name = display.get("name") if isinstance(display, Mapping) else None
        if not isinstance(project_id, str) or not project_id.startswith("g-p-") or not isinstance(name, str):
            raise BackendSchemaError("projects response has unknown item shape")
        projects.append({"id": project_id, "name": name})
    return projects


async def backend_create_project(context: Any, name: str) -> str:
    payload = await _backend_get_object(
        context,
        "/backend-api/projects",
        category="project_create",
        method="post",
        data={"name": name, "instructions": ""},
    )
    resource = payload.get("resource")
    gizmo = resource.get("gizmo") if isinstance(resource, Mapping) else None
    project_id = gizmo.get("id") if isinstance(gizmo, Mapping) else None
    if not isinstance(project_id, str) or not project_id.startswith("g-p-"):
        raise BackendSchemaError("project_create response is missing resource.gizmo.id")
    return project_id


async def backend_set_conversation_project(
    context: Any, conversation_id: str, project_id: str
) -> None:
    exact_id = _safe_identity_string(conversation_id)
    if exact_id is None or not isinstance(project_id, str) or not project_id.startswith("g-p-"):
        raise ValueError("conversation and Project IDs must be valid")
    await _backend_get_object(
        context,
        f"/backend-api/conversation/{exact_id}",
        category="project_membership",
        method="patch",
        data={"gizmo_id": project_id},
    )
