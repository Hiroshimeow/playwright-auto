from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .chatgpt_graph import (
    BackendAuthError,
    BackendNotReadyError,
    BackendSchemaError,
    BackendUnavailableError,
    normalize_visible_text,
    visible_text_matches,
)
from .observability import record_page_action
from .role_indicator import WINDOW_NAME_PREFIX, ensure_role_indicator

ROLE_STORAGE_KEY = "playwright-auto:role"
PAGE_ID_STORAGE_KEY = "playwright-auto:page-id"
TASK_ID_STORAGE_KEY = "playwright-auto:task-id"
TEAM_STORAGE_KEY = "playwright-auto:team"
ATTACHMENT_OWNERSHIP_WINDOW_KEY = "__playwrightAutoAttachmentOwnershipV1"

SELECTORS = {
    "composer": '[contenteditable="true"][role="textbox"]',
    "submit": "#composer-submit-button",
    "send": 'button[data-testid="send-button"]',
    "stop": 'button[data-testid="stop-button"]',
    "new_chat": '[data-testid="create-new-chat-button"]',
    "login": '[data-testid="login-button"]',
    "message": "[data-message-author-role][data-message-id]",
    "turn": 'section[data-turn-id][data-testid^="conversation-turn-"]',
    "error_retry": '[data-testid="regenerate-thread-error-button"]',
}

SHORTCUTS = {
    "send_or_stop": "Enter",
    "toggle_dictation": "Control+Shift+D",
    "add_photos": "Control+U",
    "new_chat": "Control+Shift+O",
    "show_shortcuts": "Control+/",
    "toggle_dev_mode": "Control+.",
    "toggle_sidebar": "Control+Shift+S",
    "custom_instructions": "Control+Shift+I",
    "copy_last_code_block": "Control+Shift+;",
    "delete_chat": "Control+Shift+Delete",
}

# Visible actions are intentionally paced; DOM reads and emergency Stop are not.
_DEFAULT_ACTION_DELAY_MULTIPLIERS = {
    "composer_fill": 1.0,
    "send": 3.0,
    "new_chat": 4.0,
    "refresh": 4.0,
    "open_tab": 4.0,
    "close_tab": 4.0,
    "delete_dialog": 4.0,
    "delete_confirm": 5.0,
}
_ACTION_DELAY_MULTIPLIERS = dict(_DEFAULT_ACTION_DELAY_MULTIPLIERS)
SEND_DELAY_MULTIPLIER = _ACTION_DELAY_MULTIPLIERS["send"]
NAVIGATION_DELAY_MULTIPLIER = _ACTION_DELAY_MULTIPLIERS["new_chat"]
DIALOG_DELAY_MULTIPLIER = _ACTION_DELAY_MULTIPLIERS["delete_dialog"]
DELETE_DELAY_MULTIPLIER = _ACTION_DELAY_MULTIPLIERS["delete_confirm"]
_RANDOM_DELAY_MIN_SECONDS = 1.0
_RANDOM_DELAY_MAX_SECONDS = 1.5


def configure_random_delay(min_seconds: float = 1.0, max_seconds: float = 1.5) -> None:
    """Change the process-wide human pacing range used by visible actions."""
    minimum = float(min_seconds)
    maximum = float(max_seconds)
    if minimum <= 0 or maximum <= 0 or minimum > maximum:
        raise ValueError("random delay requires 0 < min_seconds <= max_seconds")
    global _RANDOM_DELAY_MIN_SECONDS, _RANDOM_DELAY_MAX_SECONDS
    _RANDOM_DELAY_MIN_SECONDS = minimum
    _RANDOM_DELAY_MAX_SECONDS = maximum


def configure_action_delays(
    min_seconds: float = 1.0,
    max_seconds: float = 1.5,
    multipliers: Mapping[str, float] | None = None,
) -> None:
    """Configure the shared visible-action delay policy for this process."""
    configure_random_delay(min_seconds, max_seconds)
    overrides = dict(multipliers or {})
    unknown = set(overrides) - set(_DEFAULT_ACTION_DELAY_MULTIPLIERS)
    if unknown:
        raise ValueError(f"unknown action delay multipliers: {sorted(unknown)!r}")
    configured = dict(_DEFAULT_ACTION_DELAY_MULTIPLIERS)
    for action, value in overrides.items():
        multiplier = float(value)
        if multiplier <= 0:
            raise ValueError(f"delay multiplier for {action!r} must be positive")
        configured[action] = multiplier
    _ACTION_DELAY_MULTIPLIERS.clear()
    _ACTION_DELAY_MULTIPLIERS.update(configured)
    global SEND_DELAY_MULTIPLIER, NAVIGATION_DELAY_MULTIPLIER
    global DIALOG_DELAY_MULTIPLIER, DELETE_DELAY_MULTIPLIER
    SEND_DELAY_MULTIPLIER = configured["send"]
    NAVIGATION_DELAY_MULTIPLIER = configured["new_chat"]
    DIALOG_DELAY_MULTIPLIER = configured["delete_dialog"]
    DELETE_DELAY_MULTIPLIER = configured["delete_confirm"]


def action_delay_multiplier(action: str) -> float:
    try:
        return _ACTION_DELAY_MULTIPLIERS[str(action)]
    except KeyError as exc:
        raise ValueError(f"unknown visible action {action!r}") from exc


def sample_random_delay(multiplier: float = 1.0) -> float:
    factor = float(multiplier)
    if factor <= 0:
        raise ValueError("delay multiplier must be positive")
    return random.uniform(
        _RANDOM_DELAY_MIN_SECONDS,
        _RANDOM_DELAY_MAX_SECONDS,
    ) * factor


async def random_delay(multiplier: float = 1.0) -> float:
    """Sleep for one globally configured random delay multiplied by ``multiplier``."""
    seconds = sample_random_delay(multiplier)
    await asyncio.sleep(seconds)
    return seconds


async def action_delay(page: Any, action: str, multiplier: float) -> float:
    """Expose an exact countdown event, then wait before a visible action."""
    seconds = sample_random_delay(multiplier)
    await record_page_action(page, action, "delay", delay_seconds=seconds)
    await asyncio.sleep(seconds)
    await record_page_action(page, action, "ready", delay_seconds=seconds)
    return seconds

_ROLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TEAM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def attachment_name_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    dot = expected.rfind(".")
    split = dot if dot > 0 else len(expected)
    stem, suffix = expected[:split], expected[split:]
    return re.fullmatch(
        rf"{re.escape(stem)}\([1-9]\d*\){re.escape(suffix)}",
        actual,
    ) is not None


def attachment_names_match(
    actual: Sequence[str],
    expected: Sequence[str],
) -> bool:
    return len(actual) == len(expected) and all(
        attachment_name_matches(str(name), str(expected[index]))
        for index, name in enumerate(actual)
    )


def _expected_attachment_contract(
    expected_names: Sequence[str] | None,
    expected_count: int,
) -> tuple[tuple[str, ...] | None, int]:
    if expected_count < 0:
        raise ValueError("expected_attachment_count must not be negative")
    if expected_names is None:
        return None, expected_count
    names = tuple(str(name).strip() for name in expected_names)
    if any(not name for name in names):
        raise ValueError("expected attachment names must be non-empty")
    if expected_count not in {0, len(names)}:
        raise ValueError("expected attachment count does not match expected names")
    return names, len(names)


def _assert_expected_attachment_markers(
    markers: Sequence[str],
    *,
    expected_names: tuple[str, ...] | None,
    expected_count: int,
    message: str,
) -> None:
    actual = tuple(str(marker) for marker in markers)
    matches = (
        attachment_names_match(actual, expected_names)
        if expected_names is not None
        else len(actual) == expected_count
    )
    if not matches:
        expected = list(expected_names) if expected_names is not None else expected_count
        raise ComposerConflictError(
            f"{message}: expected {expected!r}, found {list(actual)!r}"
        )


def rate_limit_dialogs(snapshot: "ChatGPTSnapshot") -> tuple[str, ...]:
    return tuple(
        dialog
        for dialog in snapshot.blocking_dialogs
        if any(marker in dialog.casefold() for marker in _RATE_LIMIT_MARKERS)
    )


class ChatGPTAutomationError(RuntimeError):
    pass


class PageOwnershipError(ChatGPTAutomationError):
    pass


class AuthenticationRequiredError(ChatGPTAutomationError):
    pass


class ComposerConflictError(ChatGPTAutomationError):
    pass


class UnsafePageStateError(ChatGPTAutomationError):
    pass


class ConversationTranscriptNotReadyError(UnsafePageStateError):
    pass


class RateLimitBlockedError(UnsafePageStateError):
    pass


class SendRecoveryError(ChatGPTAutomationError):
    pass


class IncompleteResponseTimeoutError(TimeoutError, ChatGPTAutomationError):
    """Exact-provenance response exists but remained structurally incomplete."""


class StableMalformedResponseError(ChatGPTAutomationError):
    """A clean, inactive assistant candidate stayed malformed through its grace period."""

    def __init__(
        self,
        candidate: "MessageSnapshot",
        validation_error: BaseException,
    ) -> None:
        self.candidate = candidate
        self.validation_error = validation_error
        super().__init__(f"stable malformed response: {validation_error}")


class ManualInputPendingError(ChatGPTAutomationError):
    pass


class ChoicePromptBlockedError(ChatGPTAutomationError):
    pass


class TaskBindingError(ChatGPTAutomationError):
    pass


_PAGE_WORKFLOW_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_PAGE_MUTATION_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_PAGE_WAIT_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_PAGE_PASSIVE_OBSERVATION_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_BACKEND_CONTEXT_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_STREAM_STATUS_CACHE_LIMIT = 32
_STREAM_STATUS_TERMINAL = frozenset({"COMPLETE", "FAILURE"})
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "making requests too quickly",
    "temporarily limited access",
)
_RATE_LIMIT_DIALOG_TEST_IDS = ("modal-conversation-history-rate-limit",)


def _page_lock(registry: weakref.WeakKeyDictionary[Any, asyncio.Lock], page: Any) -> asyncio.Lock:
    lock = registry.get(page)
    if lock is None:
        lock = asyncio.Lock()
        registry[page] = lock
    return lock


def _page_wait_state(page: Any) -> dict[str, Any]:
    state = {
        "probe": None,
        "snapshot": None,
        "receipt_key": None,
        "full_snapshot_at": 0.0,
    }
    try:
        existing = _PAGE_WAIT_STATES.get(page)
        if existing is not None:
            return existing
        _PAGE_WAIT_STATES[page] = state
    except TypeError:
        # Tiny fake page objects in unit tests may not support weak references.
        pass
    return state


def _page_passive_observation_state(page: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "listener": None,
        "close_listener": None,
        "scope": None,
        "scope_revision": 0,
        "latest": None,
        "wake_event": None,
        "tasks": set(),
        "event_count": 0,
    }
    try:
        existing = _PAGE_PASSIVE_OBSERVATION_STATES.get(page)
        if existing is not None:
            return existing
        _PAGE_PASSIVE_OBSERVATION_STATES[page] = state
        return state
    except TypeError:
        existing = getattr(page, "_playwright_auto_passive_observation_state", None)
        if isinstance(existing, dict):
            return existing
        try:
            setattr(page, "_playwright_auto_passive_observation_state", state)
        except Exception:
            pass
        return state


def _passive_evidence_matches_scope(
    evidence: Mapping[str, Any], scope: Mapping[str, Any]
) -> bool:
    if str(evidence.get("request_id") or "") != str(scope.get("request_id") or ""):
        return False
    if int(evidence.get("generation") or 0) != int(scope.get("generation") or 0):
        return False
    expected_conversation = _safe_identity_string(scope.get("conversation_id"))
    expected_user = _safe_identity_string(scope.get("accepted_user_message_id"))
    evidence_conversation = _safe_identity_string(evidence.get("conversation_id"))
    evidence_user = _safe_identity_string(evidence.get("observed_user_message_id"))
    if expected_conversation is not None and evidence_conversation != expected_conversation:
        return False
    if expected_user is not None and evidence_user != expected_user:
        return False
    return True


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
        context, "/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0", category="projects"
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
        context, "/backend-api/projects", category="project_create", method="post",
        data={"name": name, "instructions": ""},
    )
    resource = payload.get("resource")
    gizmo = resource.get("gizmo") if isinstance(resource, Mapping) else None
    project_id = gizmo.get("id") if isinstance(gizmo, Mapping) else None
    if not isinstance(project_id, str) or not project_id.startswith("g-p-"):
        raise BackendSchemaError("project_create response is missing resource.gizmo.id")
    return project_id


async def backend_set_conversation_project(context: Any, conversation_id: str, project_id: str) -> None:
    exact_id = _safe_identity_string(conversation_id)
    if exact_id is None or not isinstance(project_id, str) or not project_id.startswith("g-p-"):
        raise ValueError("conversation and Project IDs must be valid")
    await _backend_get_object(
        context, f"/backend-api/conversation/{exact_id}", category="project_membership",
        method="patch", data={"gizmo_id": project_id},
    )


class ChatGPTState(str, Enum):
    AUTH_REQUIRED = "auth_required"
    NEW_CHAT = "new_chat"
    DRAFT = "draft"
    SUBMITTING = "submitting"
    RESPONDING = "responding"
    WAITING_PROMPT = "waiting_prompt"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MessageSnapshot:
    role: str
    message_id: str
    turn_id: str | None
    text: str
    actions: tuple[str, ...]
    image_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "text": self.text,
            "actions": list(self.actions),
            "image_count": self.image_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageSnapshot":
        return cls(
            role=str(value.get("role") or ""),
            message_id=str(value.get("message_id") or ""),
            turn_id=(
                str(value["turn_id"]) if value.get("turn_id") is not None else None
            ),
            text=str(value.get("text") or ""),
            actions=tuple(str(item) for item in value.get("actions") or []),
            image_count=int(value.get("image_count") or 0),
        )


@dataclass(frozen=True)
class PageBinding:
    page_id: str
    role: str

    def to_dict(self) -> dict[str, str]:
        return {"page_id": self.page_id, "role": self.role}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PageBinding":
        return cls(
            page_id=str(value["page_id"]),
            role=validate_page_role(str(value["role"])),
        )


@dataclass(frozen=True)
class MessageBaseline:
    message_ids: frozenset[str]
    turn_ids: frozenset[str]
    assistant_turn_ids: frozenset[str]
    user_message_ids: frozenset[str]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "message_ids": sorted(self.message_ids),
            "turn_ids": sorted(self.turn_ids),
            "assistant_turn_ids": sorted(self.assistant_turn_ids),
            "user_message_ids": sorted(self.user_message_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageBaseline":
        return cls(
            message_ids=frozenset(str(item) for item in value.get("message_ids", [])),
            turn_ids=frozenset(str(item) for item in value.get("turn_ids", [])),
            assistant_turn_ids=frozenset(
                str(item) for item in value.get("assistant_turn_ids", [])
            ),
            user_message_ids=frozenset(
                str(item) for item in value.get("user_message_ids", [])
            ),
        )


def _safe_identity_string(value: Any, *, max_length: int = 512) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _matches_frontend_conversation_response(response: Any) -> bool:
    try:
        request = response.request
        parsed = urlparse(str(response.url))
        return (
            str(request.method).upper() == "POST"
            and parsed.scheme == "https"
            and parsed.hostname in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.path == "/backend-api/f/conversation"
        )
    except Exception:
        return False


def _paged_conversation_id_from_response(response: Any) -> str | None:
    try:
        request = response.request
        parsed = urlparse(str(response.url))
        if (
            str(request.method).upper() != "GET"
            or parsed.scheme != "https"
            or parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}
        ):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 3 or parts[:2] != ["backend-api", "conversations"]:
            return None
        return _safe_identity_string(parts[2])
    except Exception:
        return None


def _frontend_user_message_id(request: Any) -> str | None:
    try:
        payload = request.post_data_json
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    matches: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        author = message.get("author")
        if not isinstance(author, Mapping) or author.get("role") != "user":
            continue
        message_id = _safe_identity_string(message.get("id"))
        if message_id is not None:
            matches.append(message_id)
    return matches[0] if len(matches) == 1 else None


def _conversation_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        raw = value.get("conversation_id")
        conversation_id = _safe_identity_string(raw)
        if conversation_id is not None:
            found.add(conversation_id)
        for nested in value.values():
            found.update(_conversation_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_conversation_ids(nested))
    return found


_PASSIVE_OBSERVATION_MAX_MESSAGES = 64


def _normalized_observed_message(value: Any) -> dict[str, Any] | None:
    raw = value.get("message") if isinstance(value, Mapping) and isinstance(value.get("message"), Mapping) else value
    if not isinstance(raw, Mapping):
        return None
    message_id = _safe_identity_string(raw.get("id"))
    author = raw.get("author")
    if message_id is None or not isinstance(author, Mapping):
        return None
    role = _safe_identity_string(author.get("role"), max_length=64)
    if role is None:
        return None
    recipient = raw.get("recipient", "all")
    if not isinstance(recipient, str) or not recipient or len(recipient) > 256:
        return None
    content = raw.get("content")
    if not isinstance(content, Mapping):
        content = {"content_type": "text", "parts": []}
    content_type = content.get("content_type")
    if not isinstance(content_type, str) or not content_type or len(content_type) > 128:
        content_type = "text"
    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = []
    bounded_parts: list[Any] = []
    for part in parts[:32]:
        if isinstance(part, str):
            bounded_parts.append(part[:200_000])
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            bounded_parts.append({"text": str(part["text"])[:200_000]})
    return {
        "id": message_id,
        "author": {"role": role},
        "recipient": recipient,
        "content": {"content_type": content_type, "parts": bounded_parts},
    }


def _observed_linear_graph(
    values: Sequence[Any],
    observed_user_message_id: str,
    *,
    current_node: str | None = None,
    require_user_in_values: bool,
) -> dict[str, Any] | None:
    user_message_id = _safe_identity_string(observed_user_message_id)
    if user_message_id is None:
        return None
    normalized: list[dict[str, Any]] = []
    for value in values:
        message = _normalized_observed_message(value)
        if message is None:
            continue
        if any(existing["id"] == message["id"] for existing in normalized):
            normalized = [existing for existing in normalized if existing["id"] != message["id"]]
        normalized.append(message)
        if len(normalized) > _PASSIVE_OBSERVATION_MAX_MESSAGES:
            return None
    user_index = next(
        (index for index, message in enumerate(normalized) if message["id"] == user_message_id),
        None,
    )
    if require_user_in_values and user_index is None:
        return None
    branch = normalized[user_index:] if user_index is not None else normalized
    if user_index is None:
        branch = [
            {
                "id": user_message_id,
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": []},
            },
            *branch,
        ]
    if not branch or branch[0]["id"] != user_message_id:
        return None
    if len(branch) > _PASSIVE_OBSERVATION_MAX_MESSAGES:
        return None
    mapping: dict[str, Any] = {}
    for index, message in enumerate(branch):
        node_id = message["id"]
        parent = branch[index - 1]["id"] if index else None
        children = [branch[index + 1]["id"]] if index + 1 < len(branch) else []
        mapping[node_id] = {
            "id": node_id,
            "message": message,
            "parent": parent,
            "children": children,
        }
    exact_current = _safe_identity_string(current_node) if current_node is not None else None
    if exact_current is not None and exact_current not in mapping:
        return None
    return {
        "current_node": exact_current or branch[-1]["id"],
        "mapping": mapping,
    }


def _reduce_frontend_conversation_observation_body(
    body: bytes | str, observed_user_message_id: str
) -> dict[str, Any] | None:
    user_message_id = _safe_identity_string(observed_user_message_id)
    if user_message_id is None:
        return None
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(body, str):
        text = body
    else:
        return None
    conversation_ids: set[str] = set()
    observed_messages: list[Any] = []
    saw_done = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            saw_done = True
            continue
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        conversation_ids.update(_conversation_ids(decoded))
        if len(conversation_ids) > 1:
            return None
        if isinstance(decoded, Mapping):
            if isinstance(decoded.get("message"), Mapping):
                observed_messages.append(decoded["message"])
            elif isinstance(decoded.get("messages"), list):
                observed_messages.extend(decoded["messages"])
        if len(observed_messages) > _PASSIVE_OBSERVATION_MAX_MESSAGES:
            return None
    if len(conversation_ids) != 1:
        return None
    graph = _observed_linear_graph(
        observed_messages,
        user_message_id,
        require_user_in_values=False,
    )
    if graph is None:
        return None
    return {
        "source": "frontend_sse",
        "coverage": "complete" if saw_done and len(graph["mapping"]) > 1 else "partial",
        "observed_user_message_id": user_message_id,
        "conversation_id": next(iter(conversation_ids)),
        "graph": graph,
    }


def _reduce_paged_conversation_body(
    body: bytes | str,
    *,
    conversation_id: str,
    observed_user_message_id: str,
) -> dict[str, Any] | None:
    exact_conversation = _safe_identity_string(conversation_id)
    exact_user = _safe_identity_string(observed_user_message_id)
    if exact_conversation is None or exact_user is None:
        return None
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(body, str):
        text = body
    else:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, Mapping):
        return None
    messages = decoded.get("messages")
    current_node = _safe_identity_string(decoded.get("current_node"))
    if not isinstance(messages, list) or current_node is None:
        return None
    graph = _observed_linear_graph(
        messages,
        exact_user,
        current_node=current_node,
        require_user_in_values=True,
    )
    if graph is None:
        return None
    page_info = decoded.get("page_info")
    has_more = bool(page_info.get("has_more")) if isinstance(page_info, Mapping) else False
    continuation = decoded.get("context_truncation_continuation")
    return {
        "source": "paged_messages",
        "coverage": "partial" if has_more or continuation not in {None, ""} else "complete",
        "observed_user_message_id": exact_user,
        "conversation_id": exact_conversation,
        "graph": graph,
    }


def _reduce_frontend_conversation_body(
    body: bytes | str, observed_user_message_id: str
) -> dict[str, str] | None:
    user_message_id = _safe_identity_string(observed_user_message_id)
    if user_message_id is None:
        return None
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(body, str):
        text = body
    else:
        return None
    conversation_ids: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        conversation_ids.update(_conversation_ids(decoded))
        if len(conversation_ids) > 1:
            return None
    if len(conversation_ids) != 1:
        return None
    return {
        "observed_user_message_id": user_message_id,
        "conversation_id": next(iter(conversation_ids)),
    }


async def _reduce_frontend_conversation_response(
    response: Any, observed_user_message_id: str
) -> dict[str, str] | None:
    try:
        body = await response.body()
        return _reduce_frontend_conversation_body(body, observed_user_message_id)
    except Exception:
        return None


async def _reduce_frontend_conversation_response_observation(
    response: Any, observed_user_message_id: str
) -> dict[str, Any] | None:
    try:
        body = await response.body()
        return _reduce_frontend_conversation_observation_body(
            body, observed_user_message_id
        )
    except Exception:
        return None


@dataclass(frozen=True)
class SendReceipt:
    prompt: str
    prompt_sha256: str
    binding: PageBinding
    baseline: MessageBaseline
    attempts: int
    accepted_via: str
    session_id_before: str | None
    user_message_id: str | None = None
    user_turn_id: str | None = None
    conversation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "prompt_sha256": self.prompt_sha256,
            "binding": self.binding.to_dict(),
            "baseline": self.baseline.to_dict(),
            "attempts": self.attempts,
            "accepted_via": self.accepted_via,
            "session_id_before": self.session_id_before,
            "user_message_id": self.user_message_id,
            "user_turn_id": self.user_turn_id,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SendReceipt":
        prompt = str(value["prompt"])
        digest = str(value["prompt_sha256"])
        if prompt_digest(prompt) != digest:
            raise ValueError("send receipt prompt digest does not match prompt")
        attempts = int(value["attempts"])
        if attempts not in {1, 2}:
            raise ValueError("send receipt attempts must be 1 or 2")
        accepted_via = str(value["accepted_via"])
        allowed_acceptance = {
            "exact_user_message",
            "user_message_identity",
            "stop_button",
        }
        if accepted_via.startswith("post_reload:"):
            recovered_signal = accepted_via.split(":", 1)[1]
            if recovered_signal not in allowed_acceptance:
                raise ValueError("send receipt has an unsupported recovery acceptance signal")
        elif accepted_via not in allowed_acceptance:
            raise ValueError("send receipt has an unsupported acceptance signal")
        conversation_id = value.get("conversation_id")
        if conversation_id is not None:
            if (
                not isinstance(conversation_id, str)
                or not conversation_id
                or len(conversation_id) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in conversation_id)
            ):
                raise ValueError("send receipt conversation ID must be a bounded printable string")
        return cls(
            prompt=prompt,
            prompt_sha256=digest,
            binding=PageBinding.from_dict(value["binding"]),
            baseline=MessageBaseline.from_dict(value["baseline"]),
            attempts=attempts,
            accepted_via=accepted_via,
            session_id_before=(
                str(value["session_id_before"])
                if value.get("session_id_before") is not None
                else None
            ),
            user_message_id=(
                str(value["user_message_id"])
                if value.get("user_message_id") is not None
                else None
            ),
            user_turn_id=(
                str(value["user_turn_id"])
                if value.get("user_turn_id") is not None
                else None
            ),
            conversation_id=conversation_id,
        )


@dataclass(frozen=True)
class ChatGPTSnapshot:
    url: str
    session_id: str | None
    page_id: str | None
    page_role: str | None
    state: ChatGPTState
    requires_login: bool
    composer_present: bool
    composer_editable: bool
    composer_text: str
    send_visible: bool
    send_enabled: bool
    stop_visible: bool
    blocking_dialogs: tuple[str, ...]
    attachment_markers: tuple[str, ...]
    error_texts: tuple[str, ...]
    messages: tuple[MessageSnapshot, ...]
    choice_prompt_labels: tuple[str, ...] = ()
    retry_visible: bool = False
    page_task_id: str | None = None
    page_team: str | None = None
    response_activity_text: str = ""
    response_activity_structure: str = ""
    response_activity_turn_id: str | None = None
    response_activity_length: int = 0

    @property
    def conversation_url(self) -> str | None:
        return self.url if self.session_id else None

    @property
    def composer_empty(self) -> bool:
        return not self.composer_text.strip()

    @property
    def recent_assistant_messages(self) -> tuple[MessageSnapshot, ...]:
        return tuple(message for message in self.messages if message.role == "assistant")

    @property
    def manual_input_pending(self) -> bool:
        return bool(self.composer_text.strip() or self.attachment_markers)

    @property
    def choice_prompt_pending(self) -> bool:
        return bool(self.choice_prompt_labels) and not self.composer_present

    @property
    def image_count(self) -> int:
        return sum(message.image_count for message in self.messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "conversation_url": self.conversation_url,
            "session_id": self.session_id,
            "page_id": self.page_id,
            "page_role": self.page_role,
            "page_task_id": self.page_task_id,
            "page_team": self.page_team,
            "response_activity_text": self.response_activity_text,
            "response_activity_structure": self.response_activity_structure,
            "response_activity_turn_id": self.response_activity_turn_id,
            "response_activity_length": self.response_activity_length,
            "state": self.state.value,
            "requires_login": self.requires_login,
            "composer_present": self.composer_present,
            "composer_editable": self.composer_editable,
            "composer_text": self.composer_text,
            "composer_empty": self.composer_empty,
            "send_visible": self.send_visible,
            "send_enabled": self.send_enabled,
            "stop_visible": self.stop_visible,
            "blocking_dialogs": list(self.blocking_dialogs),
            "attachment_markers": list(self.attachment_markers),
            "choice_prompt_pending": self.choice_prompt_pending,
            "choice_prompt_labels": list(self.choice_prompt_labels),
            "retry_visible": self.retry_visible,
            "manual_input_pending": self.manual_input_pending,
            "image_count": self.image_count,
            "error_texts": list(self.error_texts),
            "messages": [message.to_dict() for message in self.messages],
        }


@dataclass(frozen=True)
class WaitProbe:
    url: str
    session_id: str | None
    page_id: str | None
    page_role: str | None
    page_task_id: str | None
    page_team: str | None
    requires_login: bool
    composer_present: bool
    composer_text: str
    attachment_count: int
    stop_visible: bool
    transport_active: bool
    error_texts: tuple[str, ...]
    blocking_dialogs: tuple[str, ...]
    choice_prompt_labels: tuple[str, ...]
    mcp_permission_allow_count: int
    mcp_permission_node_count: int
    last_user_message_id: str | None
    last_user_turn_id: str | None
    last_assistant_message_id: str | None
    last_assistant_turn_id: str | None
    assistant_text_length: int
    assistant_text_tail: str
    response_activity_length: int
    response_activity_tail: str
    response_activity_turn_id: str | None

    @property
    def composer_empty(self) -> bool:
        return not self.composer_text.strip()

    @property
    def manual_input_pending(self) -> bool:
        return bool(self.composer_text.strip() or self.attachment_count)

    @property
    def choice_prompt_pending(self) -> bool:
        return bool(self.choice_prompt_labels) and not self.composer_present

    @property
    def identity_signature(self) -> tuple[str | None, ...]:
        return (
            self.last_user_message_id,
            self.last_user_turn_id,
            self.last_assistant_message_id,
            self.last_assistant_turn_id,
            self.response_activity_turn_id,
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "page_role": self.page_role,
            "page_id": self.page_id,
            "page_task_id": self.page_task_id,
            "page_team": self.page_team,
            "requires_login": self.requires_login,
            "composer_present": self.composer_present,
            "composer_text": self.composer_text,
            "attachment_count": self.attachment_count,
            "stop_visible": self.stop_visible,
            "transport_active": self.transport_active,
            "error_texts": list(self.error_texts),
            "blocking_dialogs": list(self.blocking_dialogs),
            "choice_prompt_labels": list(self.choice_prompt_labels),
            "mcp_permission_allow_count": self.mcp_permission_allow_count,
            "mcp_permission_node_count": self.mcp_permission_node_count,
            "last_user_message_id": self.last_user_message_id,
            "last_user_turn_id": self.last_user_turn_id,
            "last_assistant_message_id": self.last_assistant_message_id,
            "last_assistant_turn_id": self.last_assistant_turn_id,
            "assistant_text_length": self.assistant_text_length,
            "assistant_text_tail": self.assistant_text_tail,
            "response_activity_length": self.response_activity_length,
            "response_activity_tail": self.response_activity_tail,
            "response_activity_turn_id": self.response_activity_turn_id,
        }

    @property
    def transition_signature(self) -> str:
        return json.dumps(
            [
                self.url,
                self.page_id,
                self.page_role,
                self.page_task_id,
                self.page_team,
                self.requires_login,
                self.composer_present,
                self.composer_text,
                self.attachment_count,
                self.stop_visible,
                self.transport_active,
                list(self.error_texts),
                list(self.blocking_dialogs),
                list(self.choice_prompt_labels),
                self.mcp_permission_allow_count,
                self.mcp_permission_node_count,
                self.last_user_message_id,
                self.last_user_turn_id,
                self.last_assistant_message_id,
                self.last_assistant_turn_id,
                self.response_activity_turn_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PageHealth:
    healthy: bool
    state: str
    action: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "state": self.state,
            "action": self.action,
            "detail": self.detail,
        }


def capture_message_baseline(messages: Sequence[MessageSnapshot]) -> MessageBaseline:
    return MessageBaseline(
        message_ids=frozenset(message.message_id for message in messages),
        turn_ids=frozenset(message.turn_id for message in messages if message.turn_id),
        assistant_turn_ids=frozenset(
            message.turn_id or message.message_id
            for message in messages
            if message.role == "assistant"
        ),
        user_message_ids=frozenset(
            message.message_id for message in messages if message.role == "user"
        ),
    )


async def wait_for_existing_conversation_messages(
    snapshot: ChatGPTSnapshot,
    read_snapshot: Callable[[], Awaitable[ChatGPTSnapshot]],
    *,
    timeout_seconds: float = 0.5,
) -> ChatGPTSnapshot:
    """Boundedly wait for a known reused conversation transcript to hydrate."""
    if snapshot.session_id is None or snapshot.messages:
        return snapshot
    session_id = snapshot.session_id
    deadline = time.monotonic() + timeout_seconds
    latest = snapshot
    while not latest.messages and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        latest = await read_snapshot()
        if latest.session_id != session_id:
            raise UnsafePageStateError(
                "existing conversation identity changed while waiting for transcript hydration"
            )
    if not latest.messages:
        raise ConversationTranscriptNotReadyError(
            "existing conversation transcript is not hydrated before send"
        )
    return latest


def new_messages_since(
    messages: Sequence[MessageSnapshot], baseline: MessageBaseline
) -> tuple[MessageSnapshot, ...]:
    return tuple(
        message for message in messages if message.message_id not in baseline.message_ids
    )


_REQUEST_MARKER_PATTERN = re.compile(
    r"(?m)^ROLE_REQUEST_ID:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\s*$"
)


def request_marker_from_prompt(prompt: str) -> str | None:
    matches = _REQUEST_MARKER_PATTERN.findall(str(prompt or ""))
    if len(matches) != 1:
        return None
    return f"ROLE_REQUEST_ID: {matches[0]}"


def exact_prompt_seen(
    messages: Sequence[MessageSnapshot], baseline: MessageBaseline, prompt: str
) -> bool:
    """Prove a new user request by exact text or an optional durable marker.

    ChatGPT may append UI-only text such as ``Show more`` to a collapsed long
    user message. Generic durable prompts may include one unique
    ``ROLE_REQUEST_ID`` marker; markerless callers rely on exact text after the
    captured baseline and fail closed when the rendered transcript is ambiguous.
    """
    expected = normalize_visible_text(prompt)
    marker = request_marker_from_prompt(prompt)
    for message in messages:
        if message.role != "user" or message.message_id in baseline.user_message_ids:
            continue
        visible = normalize_visible_text(message.text)
        if visible == expected:
            return True
        if marker and marker in message.text:
            return True
    return False


def unique_new_user_message(
    messages: Sequence[MessageSnapshot], baseline: MessageBaseline
) -> MessageSnapshot | None:
    """Return the one user message created after ``baseline``.

    Rendered text is deliberately irrelevant here. Long ChatGPT messages may be
    collapsed behind ``Show more``; durable post-send provenance therefore uses
    message/turn identity and fails closed when the transcript is ambiguous.
    """
    candidates = [
        message
        for message in messages
        if message.role == "user"
        and message.message_id not in baseline.user_message_ids
        and message.message_id not in baseline.message_ids
    ]
    return candidates[0] if len(candidates) == 1 else None


def _receipt_user_index(
    messages: Sequence[MessageSnapshot], receipt: SendReceipt
) -> int | None:
    message_id = str(receipt.user_message_id or "").strip()
    turn_id = str(receipt.user_turn_id or "").strip()
    expected = normalize_visible_text(receipt.prompt)
    marker = request_marker_from_prompt(receipt.prompt)
    matches: list[int] = []
    for index, message in enumerate(messages):
        if (
            message.role != "user"
            or message.message_id in receipt.baseline.user_message_ids
            or message.message_id in receipt.baseline.message_ids
        ):
            continue
        if message_id or turn_id:
            matched = (
                (bool(message_id) and message.message_id == message_id)
                or (bool(turn_id) and message.turn_id == turn_id)
            )
        else:
            visible = normalize_visible_text(message.text)
            matched = visible == expected or bool(marker and marker in message.text)
        if matched:
            matches.append(index)
    return matches[0] if len(matches) == 1 else None


def receipt_user_message_seen(
    messages: Sequence[MessageSnapshot], receipt: SendReceipt
) -> bool:
    return _receipt_user_index(messages, receipt) is not None


def all_assistant_turns_for_receipt(
    messages: Sequence[MessageSnapshot], receipt: SendReceipt
) -> tuple[MessageSnapshot, ...]:
    user_index = _receipt_user_index(messages, receipt)
    if user_index is None:
        return ()
    selected: list[MessageSnapshot] = []
    seen: set[str] = set()
    for message in messages[user_index + 1 :]:
        if message.role != "assistant":
            continue
        identity = message.turn_id or message.message_id
        if identity in receipt.baseline.assistant_turn_ids or identity in seen:
            continue
        seen.add(identity)
        selected.append(message)
    return tuple(selected)


def assistant_turns_for_receipt(
    messages: Sequence[MessageSnapshot], receipt: SendReceipt
) -> tuple[MessageSnapshot, ...]:
    user_index = _receipt_user_index(messages, receipt)
    if user_index is None:
        return ()
    selected: list[MessageSnapshot] = []
    seen: set[str] = set()
    for message in messages[user_index + 1 :]:
        if message.role == "user":
            selected.clear()
            continue
        if message.role != "assistant":
            continue
        identity = message.turn_id or message.message_id
        if identity in receipt.baseline.assistant_turn_ids or identity in seen:
            continue
        seen.add(identity)
        selected.append(message)
    return tuple(selected)


def new_assistant_turns(
    messages: Sequence[MessageSnapshot], baseline: MessageBaseline
) -> tuple[MessageSnapshot, ...]:
    selected: list[MessageSnapshot] = []
    seen: set[str] = set()
    for message in messages:
        if message.role != "assistant":
            continue
        identity = message.turn_id or message.message_id
        if identity in baseline.assistant_turn_ids or identity in seen:
            continue
        seen.add(identity)
        selected.append(message)
    return tuple(selected)


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


def message_fingerprint(message: MessageSnapshot | None) -> str:
    if message is None:
        return ""
    identity = message.turn_id or message.message_id
    payload = f"{identity}\0{message.text.strip()}\0{message.image_count}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def message_content_fingerprint(message: MessageSnapshot | None) -> str:
    if message is None:
        return ""
    payload = f"{message.role}\0{message.text.strip()}\0{message.image_count}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_response_recovery_baseline(
    messages: Sequence[MessageSnapshot],
    send_baseline: MessageBaseline,
) -> dict[str, list[str]]:
    assistants = new_assistant_turns(messages, send_baseline)
    return {
        "assistant_message_ids": sorted(
            {message.message_id for message in assistants if message.message_id}
        ),
        "assistant_turn_ids": sorted(
            {message.turn_id for message in assistants if message.turn_id}
        ),
        "assistant_fingerprints": sorted(
            {message_content_fingerprint(message) for message in assistants}
        ),
    }


def merge_response_recovery_baselines(
    *values: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    keys = (
        "assistant_message_ids",
        "assistant_turn_ids",
        "assistant_fingerprints",
    )
    merged = {key: set() for key in keys}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in keys:
            merged[key].update(str(item) for item in value.get(key) or [] if item)
    return {key: sorted(items) for key, items in merged.items()}


def response_is_stale(
    message: MessageSnapshot | None,
    recovery_baseline: Mapping[str, Any] | None,
) -> bool:
    if message is None or not isinstance(recovery_baseline, Mapping):
        return False
    return bool(
        message.message_id in set(recovery_baseline.get("assistant_message_ids") or [])
        or (
            message.turn_id
            and message.turn_id in set(recovery_baseline.get("assistant_turn_ids") or [])
        )
        or message_content_fingerprint(message)
        in set(recovery_baseline.get("assistant_fingerprints") or [])
    )


_TRANSIENT_RESPONSE_MARKERS = (
    "connection interrupted",
    "waiting for the complete answer",
    "message delivery timed out",
    "please try again",
)


def _looks_like_transient_response_notice(text: str) -> bool:
    value = normalize_visible_text(text).casefold()
    if not value or len(value) > 240:
        return False
    return any(
        value == marker
        or value.startswith(f"{marker} ")
        or value.startswith(f"{marker}.")
        or value.endswith(f" {marker}")
        or value.endswith(f" {marker}.")
        for marker in _TRANSIENT_RESPONSE_MARKERS
    )


def response_transport_ui_active(snapshot: ChatGPTSnapshot) -> bool:
    state = getattr(snapshot, "state", ChatGPTState.UNKNOWN)
    error_texts = tuple(getattr(snapshot, "error_texts", ()) or ())
    if state is ChatGPTState.ERROR or error_texts:
        return True
    assistants = tuple(
        message
        for message in tuple(getattr(snapshot, "messages", ()) or ())
        if message.role == "assistant"
    )
    latest = assistants[-1].text if assistants else ""
    activity = str(getattr(snapshot, "response_activity_text", "") or "")
    return _looks_like_transient_response_notice(
        latest
    ) or _looks_like_transient_response_notice(activity)


def response_activity_signature(
    snapshot: ChatGPTSnapshot,
    baseline: MessageBaseline,
) -> tuple[str, int]:
    assistants = new_assistant_turns(snapshot.messages, baseline)
    latest = assistants[-1] if assistants else None
    activity_text = normalize_visible_text(
        getattr(snapshot, "response_activity_text", "")
    )
    activity_turn_id = str(
        getattr(snapshot, "response_activity_turn_id", "") or ""
    )
    length = max(
        len(latest.text) if latest is not None else 0,
        int(getattr(snapshot, "response_activity_length", 0) or 0),
        len(activity_text),
    )
    payload = "\0".join(
        (
            message_fingerprint(latest),
            activity_turn_id,
            str(length),
            activity_text[-160:],
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), length


def looks_incomplete_response(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if re.fullmatch(r"(?is)(?:thinking|analyzing|working)(?:\.{3}|…)?", value):
        return True
    if _looks_like_transient_response_notice(value):
        return True
    if value.count("```") % 2 == 1:
        return True
    without_language_label = re.sub(r"(?is)^json\s*", "", value).strip()
    if re.match(r"(?is)^(?:json\s*)?\{\s*$", value):
        return True
    if without_language_label.startswith("{") or re.search(r"(?is)```json", value):
        depth = 0
        in_string = False
        escape = False
        for char in without_language_label:
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}" and depth:
                depth -= 1
        if depth > 0:
            return True
    return False


def extract_session_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "c" and parts[index + 1]:
            return parts[index + 1]
    return None


def validate_page_role(role: str) -> str:
    role = role.strip()
    if not _ROLE_PATTERN.fullmatch(role):
        raise ValueError(
            "role must start with a letter and contain only letters, digits, '_' or '-'"
        )
    return role


def classify_chatgpt_state(raw: Mapping[str, Any]) -> ChatGPTState:
    if raw.get("error_present"):
        return ChatGPTState.ERROR
    if raw.get("stop_visible"):
        return ChatGPTState.RESPONDING
    if str(raw.get("composer_text") or "").strip():
        return ChatGPTState.DRAFT
    if raw.get("requires_login"):
        return ChatGPTState.AUTH_REQUIRED

    messages = raw.get("messages") or []
    if messages:
        last_role = str(messages[-1].get("role") or "")
        if last_role == "user":
            return ChatGPTState.SUBMITTING
        return ChatGPTState.WAITING_PROMPT

    if raw.get("composer_present"):
        return ChatGPTState.NEW_CHAT
    return ChatGPTState.UNKNOWN


def recent_assistant_messages(
    messages: Sequence[MessageSnapshot], count: int
) -> tuple[MessageSnapshot, ...]:
    if count < 1:
        raise ValueError("count must be at least 1")
    assistants = [message for message in messages if message.role == "assistant"]
    return tuple(assistants[-count:])


def recent_assistant_turns(
    messages: Sequence[MessageSnapshot], count: int
) -> tuple[MessageSnapshot, ...]:
    """Return one latest assistant message per distinct logical turn."""
    if count < 1:
        raise ValueError("count must be at least 1")

    selected: list[MessageSnapshot] = []
    seen: set[str] = set()
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        identity = message.turn_id or message.message_id
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(message)
        if len(selected) == count:
            break
    selected.reverse()
    return tuple(selected)


async def clear_composer(page: Any, timeout_ms: int = 5_000) -> None:
    """Clear the ProseMirror composer using the interaction verified on ChatGPT."""
    composer = page.locator(SELECTORS["composer"]).first
    await composer.wait_for(state="visible", timeout=timeout_ms)
    if not (await composer.inner_text()).strip():
        return
    await composer.press("Control+A")
    await composer.press("Backspace")
    await page.wait_for_function(
        """selector => {
          const element = document.querySelector(selector);
          return Boolean(element) && !(element.innerText || '').trim();
        }""",
        arg=SELECTORS["composer"],
        timeout=timeout_ms,
    )


async def set_composer_text(page: Any, text: str, timeout_ms: int = 5_000) -> None:
    await action_delay(
        page,
        "composer_fill",
        action_delay_multiplier("composer_fill"),
    )
    await record_page_action(page, "composer_fill", "start", detail=f"{len(text)} chars")
    composer = page.locator(SELECTORS["composer"]).first
    await composer.wait_for(state="visible", timeout=timeout_ms)
    if text:
        await composer.fill(text)
        await page.wait_for_function(
            """([selector, expected]) => {
              const element = document.querySelector(selector);
              const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
              return Boolean(element) && normalize(element.innerText) === expected;
            }""",
            arg=[SELECTORS["composer"], normalize_visible_text(text)],
            timeout=timeout_ms,
        )
        await record_page_action(page, "composer_fill", "complete", detail=f"{len(text)} chars")
        return
    await clear_composer(page, timeout_ms=timeout_ms)
    await record_page_action(page, "composer_fill", "complete", detail="cleared")


async def open_new_chat(page: Any, timeout_ms: int = 8_000) -> str:
    """Open a new chat using the live DOM, with direct navigation as fallback."""
    await action_delay(page, "new_chat", NAVIGATION_DELAY_MULTIPLIER)
    await record_page_action(page, "new_chat", "click")
    clicked = await page.evaluate(
        """selector => {
          const candidates = [...document.querySelectorAll(selector)];
          const target = candidates.find((element) => {
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 &&
              getComputedStyle(element).pointerEvents !== 'none';
          });
          if (!target) return false;
          target.click();
          return true;
        }""",
        SELECTORS["new_chat"],
    )
    if clicked:
        try:
            await page.wait_for_function(
                """selector => {
                  const composer = document.querySelector(selector);
                  return location.pathname === '/' && Boolean(composer) &&
                    composer.getAttribute('contenteditable') === 'true' &&
                    composer.getAttribute('aria-disabled') !== 'true' &&
                    !((composer.innerText || '').trim());
                }""",
                arg=SELECTORS["composer"],
                timeout=timeout_ms,
            )
            await record_page_action(page, "new_chat", "complete", detail="dom")
            return "dom"
        except Exception:
            pass

    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    await page.locator(SELECTORS["composer"]).first.wait_for(
        state="visible", timeout=timeout_ms
    )
    await record_page_action(page, "new_chat", "complete", detail="navigate")
    return "navigate"


async def refresh_page(page: Any, timeout_ms: int = 15_000) -> None:
    await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)


async def click_send_button(
    page: Any,
    timeout_ms: int = 8_000,
    *,
    expected_url: str | None = None,
    expected_page_id: str | None = None,
    expected_role: str | None = None,
    expected_task_id: str | None = None,
    expected_team: str | None = None,
    expected_prompt: str | None = None,
    expected_attachment_ownership_token: str | None = None,
    expected_attachment_count: int = 0,
    expected_attachment_names: Sequence[str] | None = None,
) -> str:
    expected_names, expected_count = _expected_attachment_contract(
        expected_attachment_names, expected_attachment_count
    )
    if (expected_page_id is None) != (expected_role is None):
        raise ValueError("expected page ID and role must be provided together")
    if (expected_task_id is None) != (expected_team is None):
        raise ValueError("expected task ID and team must be provided together")
    normalized_page_id = str(expected_page_id or "").strip() or None
    normalized_role = validate_page_role(expected_role) if expected_role is not None else None
    normalized_task_id = str(expected_task_id or "").strip() or None
    normalized_team = str(expected_team or "").strip() or None
    normalized_attachment_token = (
        str(expected_attachment_ownership_token or "").strip() or None
    )
    if expected_page_id is not None and normalized_page_id is None:
        raise ValueError("expected page ID must be non-empty")
    if expected_task_id is not None and (
        normalized_task_id is None or len(normalized_task_id) > 256
    ):
        raise ValueError("expected task ID must contain 1-256 characters")
    if expected_team is not None and (
        normalized_team is None or not _TEAM_PATTERN.fullmatch(normalized_team)
    ):
        raise ValueError("expected team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    expected_hostname: str | None = None
    expected_path: str | None = None
    if expected_url is not None:
        parsed = urlparse(str(expected_url))
        expected_hostname = str(parsed.hostname or "").lower()
        expected_path = parsed.path or "/"
    await action_delay(page, "send", SEND_DELAY_MULTIPLIER)
    await record_page_action(page, "send", "click")
    normalized_prompt = (
        normalize_visible_text(expected_prompt) if expected_prompt is not None else None
    )
    result = await page.evaluate(
        r"""([expectedPrompt, expectedNames, expectedCount,
               expectedHostname, expectedPath, expectedPageId, expectedRole,
               expectedTaskId, expectedTeam, expectedAttachmentToken,
               roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix,
               attachmentOwnershipKey]) => {
          const visible = (element) => {
            const style = element ? window.getComputedStyle(element) : null;
            return Boolean(
              element && style && style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
              (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
            );
          };
          const firstVisible = (selector) =>
            [...document.querySelectorAll(selector)].find(visible) || null;
          const enabled = (element) => Boolean(
            element && !element.disabled && element.getAttribute('aria-disabled') !== 'true'
          );
          const editable = (element) => Boolean(element && element.isContentEditable);
          const text = (element) => (element?.innerText || '').replace(/\s+/g, ' ').trim();

          const validateOwnership = () => {
            if (expectedHostname !== null && (
                location.hostname.toLowerCase() !== expectedHostname ||
                location.pathname !== expectedPath
            )) {
              return {ok: false, method: 'ownership_conflict', reason: 'conversation_changed'};
            }
            if (expectedPageId === null && expectedTaskId === null) return null;
            let pageRole = null;
            let pageId = null;
            let pageTaskId = null;
            let pageTeam = null;
            try {
              pageRole = sessionStorage.getItem(roleKey);
              pageId = sessionStorage.getItem(pageIdKey);
              pageTaskId = sessionStorage.getItem(taskIdKey);
              pageTeam = sessionStorage.getItem(teamKey);
            } catch (_) {
              // Missing primary evidence may use the existing exact window.name mirror.
            }
            const needsMirror = Boolean(
              (expectedPageId !== null && (!pageRole || !pageId)) ||
              (expectedTaskId !== null && (!pageTaskId || !pageTeam))
            );
            if (needsMirror) {
              if (!window.name?.startsWith(windowNamePrefix)) {
                return {ok: false, method: 'ownership_conflict', reason: 'binding_evidence_missing'};
              }
              let binding = null;
              try {
                binding = JSON.parse(window.name.slice(windowNamePrefix.length));
              } catch (_) {
                return {ok: false, method: 'ownership_conflict', reason: 'binding_evidence_invalid'};
              }
              if (!binding || typeof binding !== 'object') {
                return {ok: false, method: 'ownership_conflict', reason: 'binding_evidence_invalid'};
              }
              const mirrorRole = binding.role || null;
              const mirrorPageId = binding.pageId || null;
              const mirrorTaskId = binding.taskId || null;
              const mirrorTeam = binding.team || null;
              if (
                (pageRole && mirrorRole && pageRole !== mirrorRole) ||
                (pageId && mirrorPageId && pageId !== mirrorPageId) ||
                (pageTaskId && mirrorTaskId && pageTaskId !== mirrorTaskId) ||
                (pageTeam && mirrorTeam && pageTeam !== mirrorTeam)
              ) {
                return {ok: false, method: 'ownership_conflict', reason: 'binding_mirror_conflict'};
              }
              pageRole = pageRole || mirrorRole;
              pageId = pageId || mirrorPageId;
              pageTaskId = pageTaskId || mirrorTaskId;
              pageTeam = pageTeam || mirrorTeam;
            }
            if (expectedPageId !== null && (
                pageId !== expectedPageId || pageRole !== expectedRole
            )) {
              return {ok: false, method: 'ownership_conflict', reason: 'binding_changed'};
            }
            if (expectedTaskId !== null && (
                pageTaskId !== expectedTaskId || pageTeam !== expectedTeam
            )) {
              return {ok: false, method: 'ownership_conflict', reason: 'task_binding_changed'};
            }
            return null;
          };
          const validatePageState = () => {
            const retry = firstVisible('[data-testid="regenerate-thread-error-button"]');
            const errorAlert = [...document.querySelectorAll('[role="alert"]')]
              .filter(visible)
              .find((element) => /error|failed|issue|try again/i.test(text(element))) || null;
            if (retry || errorAlert) {
              return {ok: false, method: 'page_state_conflict', reason: 'error_state'};
            }
            if (firstVisible('[role="dialog"], [data-testid^="modal-"]')) {
              return {ok: false, method: 'page_state_conflict', reason: 'blocking_dialog'};
            }
            if (firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]')) {
              return {ok: false, method: 'page_state_conflict', reason: 'active_response'};
            }
            return null;
          };
          const initialOwnershipConflict = validateOwnership();
          if (initialOwnershipConflict) return initialOwnershipConflict;
          const initialPageStateConflict = validatePageState();
          if (initialPageStateConflict) return initialPageStateConflict;

          const composerSelector = 'div#prompt-textarea, [data-testid="composer"] [contenteditable="true"], form [contenteditable="true"], [contenteditable="true"][role="textbox"]';
          const findComposer = () => [...document.querySelectorAll(composerSelector)]
            .find((element) => visible(element) && enabled(element) && editable(element)) || null;
          const composer = findComposer();
          if (!composer || (expectedPrompt !== null && text(composer) !== expectedPrompt)) {
            return {
              ok: false,
              method: 'composer_conflict',
              actual_prompt: composer ? text(composer) : null,
            };
          }
          const root = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || document;
          const composerHost = composer?.closest('form') || composer?.parentElement || null;
          const elementLabel = (element) => [
            text(element),
            element?.getAttribute?.('aria-label') || '',
            element?.getAttribute?.('data-testid') || '',
          ].join(' ').replace(/\s+/g, ' ').trim();
          const attachmentLabel = elementLabel;
          const filenameFromLabel = (value) => {
            const label = String(value || '').replace(/\s+/g, ' ').trim();
            const lower = label.toLowerCase();
            for (const prefix of [
              'remove file', 'remove attachment', 'open image',
              'attached file', 'file uploaded', 'uploading'
            ]) {
              const index = lower.indexOf(prefix);
              if (index < 0) continue;
              const candidate = label.slice(index + prefix.length)
                .replace(/^[\s:–—-]+/, '').trim();
              const indexed = candidate.match(/^\d+\s*:\s*(.+)$/);
              if (indexed) return indexed[1].trim();
              if (candidate) return candidate;
            }
            return '';
          };
          const directFilename = (element) => {
            for (const attribute of ['data-filename', 'data-file-name']) {
              const candidate = (element.getAttribute?.(attribute) || '').trim();
              if (candidate) return candidate;
            }
            return filenameFromLabel(element.getAttribute?.('aria-label'));
          };
          const hasAttachmentToken = (element) => {
            const tokens = (element.getAttribute?.('data-testid') || '')
              .toLowerCase()
              .split(/[^a-z0-9]+/)
              .filter(Boolean);
            return tokens.includes('attachment') || tokens.includes('file');
          };
          const leafFilename = (root) => {
            const candidates = [];
            for (const element of [root, ...root.querySelectorAll('*')]) {
              if (!visible(element) || element.matches('button,[role="button"],svg,path')) continue;
              if ([...element.children].some(visible)) continue;
              const candidate = text(element);
              if (candidate && !['remove', 'open', 'attached', 'uploading'].includes(candidate.toLowerCase())) {
                candidates.push(candidate);
              }
            }
            return candidates.length === 1 ? candidates[0] : '';
          };
          const collectAttachmentRecords = (host) => {
            const records = [];
            const seenAttachmentItems = new Set();
            if (host) {
              for (const candidate of host.querySelectorAll(
                '[data-filename], [data-file-name], [aria-label]'
              )) {
                if (!visible(candidate)) continue;
                const explicitItem = candidate.closest('[data-filename], [data-file-name]');
                const item = explicitItem && explicitItem !== host && host.contains(explicitItem) && visible(explicitItem) ? explicitItem : candidate;
                const filename = directFilename(item);
                if (!filename || seenAttachmentItems.has(item)) continue;
                seenAttachmentItems.add(item);
                records.push({element: item, filename});
              }
              for (const attachmentRoot of host.querySelectorAll('[data-testid]')) {
                if (!visible(attachmentRoot) || !hasAttachmentToken(attachmentRoot)) continue;
                const hasFilenameEvidence = [attachmentRoot, ...attachmentRoot.querySelectorAll(
                  '[data-filename], [data-file-name], [aria-label]'
                )].some((element) => visible(element) && Boolean(directFilename(element)));
                const hasNestedAttachmentRoot = [...attachmentRoot.querySelectorAll('[data-testid]')]
                  .some((element) =>
                    element !== attachmentRoot && visible(element) && hasAttachmentToken(element)
                  );
                if (hasFilenameEvidence || hasNestedAttachmentRoot || seenAttachmentItems.has(attachmentRoot)) continue;
                seenAttachmentItems.add(attachmentRoot);
                records.push({
                  element: attachmentRoot,
                  filename: leafFilename(attachmentRoot) || '\u0000unidentified attachment',
                });
              }
            }
            records.sort((left, right) => {
              if (left.element === right.element) return 0;
              const position = left.element.compareDocumentPosition(right.element);
              if (position & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
              if (position & Node.DOCUMENT_POSITION_PRECEDING) return 1;
              return 0;
            });
            return records;
          };
          const platformNameMatches = (actual, expected) => {
            if (actual === expected) return true;
            const dot = expected.lastIndexOf('.');
            const split = dot > 0 ? dot : expected.length;
            const stem = expected.slice(0, split);
            const suffix = expected.slice(split);
            if (!actual.startsWith(stem) || !actual.endsWith(suffix)) return false;
            const middle = actual.slice(stem.length, actual.length - suffix.length);
            return /^\([1-9]\d*\)$/.test(middle);
          };
          const attachmentMarkersMatch = (markers) => expectedNames === null
            ? markers.length === expectedCount
            : markers.length === expectedNames.length &&
              markers.every((marker, index) => platformNameMatches(marker, expectedNames[index]));
          const attachmentOwnershipMatches = (records, markers) => {
            if (expectedAttachmentToken === null) return true;
            const ownership = window[attachmentOwnershipKey];
            const tokenMatches = Boolean(
              ownership && ownership.phase === 'owned' && ownership.valid === true &&
              ownership.token === expectedAttachmentToken &&
              expectedNames !== null && Array.isArray(ownership.names) &&
              ownership.names.length === expectedNames.length &&
              ownership.names.every((name, index) => name === expectedNames[index]) &&
              Array.isArray(ownership.identities) &&
              ownership.identities.length === expectedNames.length &&
              ownership.identities.every((item, index) => item.name === expectedNames[index]) &&
              Array.isArray(ownership.fileRecords) &&
              ownership.fileRecords.length === expectedNames.length &&
              ownership.fileRecords.every((file) => file instanceof File)
            );
            const attachmentElementsMatch = Boolean(
              tokenMatches && Array.isArray(ownership.attachmentElements) &&
              ownership.attachmentElements.length === records.length &&
              ownership.attachmentElements.every((element, index) =>
                element === records[index].element && element?.isConnected
              )
            );
            if (!attachmentElementsMatch) return false;
            const liveInputs = [...document.querySelectorAll('input[type="file"]')]
              .filter((input) => input.files && input.files.length > 0);
            if (ownership.method === 'drop') {
              return liveInputs.length === 0 && Array.isArray(ownership.inputRecords) &&
                ownership.inputRecords.length === 0;
            }
            if (ownership.method !== 'input' || !Array.isArray(ownership.inputRecords) ||
                ownership.inputRecords.length !== liveInputs.length) {
              return false;
            }
            const liveFiles = [];
            const inputsMatch = ownership.inputRecords.every((record, index) => {
              const input = liveInputs[index];
              const files = [...(input.files || [])];
              liveFiles.push(...files);
              return record.input === input && input.isConnected &&
                Array.isArray(record.files) && record.files.length === files.length &&
                record.files.every((file, fileIndex) => file === files[fileIndex]);
            });
            return inputsMatch && liveFiles.length === ownership.fileRecords.length &&
              ownership.fileRecords.every((file, index) => file === liveFiles[index]);
          };
          const attachmentRecords = collectAttachmentRecords(composerHost);
          const attachmentMarkers = attachmentRecords.map((item) => item.filename);
          if (!attachmentMarkersMatch(attachmentMarkers)) {
            return {
              ok: false,
              method: 'attachment_conflict',
              actual: attachmentMarkers,
            };
          }
          if (!attachmentOwnershipMatches(attachmentRecords, attachmentMarkers)) {
            return {
              ok: false,
              method: 'attachment_conflict',
              reason: 'attachment_ownership_changed',
              actual: attachmentMarkers,
            };
          }
          const findSendTarget = (scopeRoot) => [...new Set([
            ...scopeRoot.querySelectorAll('button,[role="button"]'),
            ...document.querySelectorAll('button[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send"]')
          ])].map((button) => {
            const label = [
              button.innerText || button.textContent || '',
              button.getAttribute('aria-label') || '',
              button.getAttribute('data-testid') || ''
            ].join(' ').toLowerCase();
            const score =
              (button.getAttribute('data-testid') === 'send-button' ? 10 : 0) +
              (['Send prompt', 'Send'].includes(button.getAttribute('aria-label')) ? 8 : 0) +
              (label.includes('send') ? 4 : 0) +
              (button.type === 'submit' ? 3 : 0);
            return {button, score};
          }).filter((item) => visible(item.button) && enabled(item.button) && item.score >= 4)
            .sort((a, b) => b.score - a.score)[0]?.button || null;
          const target = findSendTarget(root);
          if (!target) return {ok: false, method: 'not_found'};

          const validateDispatchBoundary = () => {
            const ownershipConflict = validateOwnership();
            if (ownershipConflict) return ownershipConflict;
            const pageStateConflict = validatePageState();
            if (pageStateConflict) return pageStateConflict;
            const currentComposer = findComposer();
            if (!currentComposer || (
                expectedPrompt !== null && text(currentComposer) !== expectedPrompt
            )) {
              return {
                ok: false,
                method: 'composer_conflict',
                reason: 'composer_changed_during_click_dispatch',
                actual_prompt: currentComposer ? text(currentComposer) : null,
              };
            }
            const currentRoot = currentComposer.closest('form') ||
              currentComposer.closest('[data-testid="composer"]') || document;
            const currentHost = currentComposer.closest('form') ||
              currentComposer.parentElement || null;
            const currentRecords = collectAttachmentRecords(currentHost);
            const currentMarkers = currentRecords.map((item) => item.filename);
            if (!attachmentMarkersMatch(currentMarkers)) {
              return {
                ok: false,
                method: 'attachment_conflict',
                reason: 'attachment_names_changed_during_click_dispatch',
                actual: currentMarkers,
              };
            }
            if (!attachmentOwnershipMatches(currentRecords, currentMarkers)) {
              return {
                ok: false,
                method: 'attachment_conflict',
                reason: 'attachment_ownership_changed_during_click_dispatch',
                actual: currentMarkers,
              };
            }
            if (findSendTarget(currentRoot) !== target || !target.isConnected) {
              return {
                ok: false,
                method: 'page_state_conflict',
                reason: 'send_target_changed_during_click_dispatch',
              };
            }
            return null;
          };

          // The page application is trusted. These checks fail closed on observable
          // ownership drift; they are not a hostile-main-world attestation boundary.
          let dispatchGuardRan = false;
          let dispatchConflict = null;
          const dispatchGuard = (event) => {
            dispatchGuardRan = true;
            dispatchConflict = validateDispatchBoundary();
            if (!dispatchConflict) return;
            event.preventDefault();
            event.stopImmediatePropagation();
          };
          target.addEventListener('click', dispatchGuard, {capture: true, once: true});
          try {
            target.click();
          } catch (error) {
            target.removeEventListener('click', dispatchGuard, {capture: true});
            return {ok: false, method: 'click_failed', error: String(error)};
          }
          target.removeEventListener('click', dispatchGuard, {capture: true});
          if (!dispatchGuardRan) {
            return {ok: false, method: 'click_failed', reason: 'dispatch_guard_not_reached'};
          }
          if (dispatchConflict) return dispatchConflict;
          return {ok: true, method: 'dom_click'};
        }""",
        [
            normalized_prompt,
            list(expected_names) if expected_names is not None else None,
            expected_count,
            expected_hostname,
            expected_path,
            normalized_page_id,
            normalized_role,
            normalized_task_id,
            normalized_team,
            normalized_attachment_token,
            ROLE_STORAGE_KEY,
            PAGE_ID_STORAGE_KEY,
            TASK_ID_STORAGE_KEY,
            TEAM_STORAGE_KEY,
            WINDOW_NAME_PREFIX,
            ATTACHMENT_OWNERSHIP_WINDOW_KEY,
        ],
    )
    if not result.get("ok"):
        method = str(result.get("method") or "unknown")
        await record_page_action(page, "send", "error", detail=method)
        reason = str(result.get("reason") or method)
        if method == "ownership_conflict":
            raise PageOwnershipError(
                f"page ownership changed inside the atomic send boundary: {reason}"
            )
        if method == "page_state_conflict":
            raise UnsafePageStateError(
                f"page state changed inside the atomic send boundary: {reason}"
            )
        if method == "composer_conflict":
            raise ComposerConflictError(
                "composer changed or became unavailable inside the atomic send boundary"
            )
        if method == "attachment_conflict":
            raise ComposerConflictError(
                "attachment identity changed inside the atomic send boundary"
            )
        raise UnsafePageStateError(f"Send button could not be clicked: {method}")
    method = str(result.get("method") or "dom_click")
    await record_page_action(page, "send", "complete", detail=method)
    return method

async def click_safe_choice_prompt(page: Any) -> str:
    result = await page.evaluate(
        r"""() => {
          const visible = (element) => Boolean(
            element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
          );
          const overlaySelector = [
            '#playwright-auto-role-badge-v3',
            '#playwright-auto-role-control-v1',
            '#playwright-auto-role-badge',
            '#playwright-auto-role-badge-v2'
          ].join(',');
          const positive = /\b(?:continue|proceed|start|yes|ok|okay|accept|approve|allow|run)\b|\bgo ahead\b|\b(?:make|create|use) a plan\b/i;
          const negative = /\b(?:cancel|stop|dismiss|close|delete|remove|archive|share|copy)\b|\b(?:not now|no thanks)\b/i;
          const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const candidateLabel = (button) => {
            const textLabel = normalize(button.innerText || button.textContent);
            const ariaLabel = normalize(button.getAttribute('aria-label'));
            const testId = normalize(button.getAttribute('data-testid'));
            if ([textLabel, ariaLabel, testId].some((label) => negative.test(label))) return '';
            return [textLabel, ariaLabel].find((label) => positive.test(label)) || '';
          };
          const candidates = [...document.querySelectorAll('button,[role="button"]')]
            .map((button) => ({button, label: candidateLabel(button)}))
            .filter(({button, label}) =>
              label && visible(button) && !button.disabled &&
              button.getAttribute('aria-disabled') !== 'true' &&
              !button.closest(overlaySelector) &&
              Boolean(button.closest('main,[role="dialog"],[data-testid^="modal-"]'))
            );
          if (candidates.length !== 1) return {ok: false, label: ''};
          const target = candidates[0];
          target.button.click();
          return {ok: true, label: target.label};
        }"""
    )
    if not result.get("ok"):
        raise ChoicePromptBlockedError("safe positive choice prompt is missing or ambiguous")
    return str(result.get("label") or "safe choice")


async def click_mcp_permission_allow(
    page: Any,
    *,
    expected_page_id: str,
    expected_role: str,
    expected_task_id: str | None,
    expected_team: str | None,
    expected_user_message_id: str,
    allowed_connectors: Sequence[str],
    dispatch: bool = True,
    expected_target_message_id: str | None = None,
) -> dict[str, str]:
    """Dispatch one offered conversation-scoped MCP allow action through React.

    This deliberately does not click a visible button.  It accepts only an action
    object already offered by the loaded client, tied to a message after the exact
    accepted user turn and to a connector explicitly authorized by the caller.
    """
    user_message_id = _safe_identity_string(expected_user_message_id)
    connectors = tuple(sorted({str(item).strip().lower() for item in allowed_connectors if str(item).strip()}))
    if user_message_id is None:
        raise ValueError("MCP permission approval requires an exact accepted user message ID")
    if not connectors:
        raise UnsafePageStateError("MCP permission approval has no task-authorized connector")
    result = await page.evaluate(
        r"""async ([expectedPageId, expectedRole, expectedTaskId, expectedTeam,
                    expectedUserMessageId, allowedConnectors, dispatch,
                    expectedTargetMessageId, roleKey, pageIdKey,
                    taskIdKey, teamKey, windowNamePrefix]) => {
          const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const allowed = new Set(allowedConnectors.map((value) => String(value).toLowerCase()));
          let pageRole = null;
          let pageId = null;
          let pageTaskId = null;
          let pageTeam = null;
          try {
            pageRole = sessionStorage.getItem(roleKey);
            pageId = sessionStorage.getItem(pageIdKey);
            pageTaskId = sessionStorage.getItem(taskIdKey);
            pageTeam = sessionStorage.getItem(teamKey);
          } catch (_) {}
          if (window.name?.startsWith(windowNamePrefix)) {
            try {
              const binding = JSON.parse(window.name.slice(windowNamePrefix.length));
              pageRole = pageRole || binding.role || null;
              pageId = pageId || binding.pageId || null;
              pageTaskId = pageTaskId || binding.taskId || null;
              pageTeam = pageTeam || binding.team || null;
            } catch (_) {}
          }
          if (
            pageId !== expectedPageId || pageRole !== expectedRole ||
            (pageTaskId || null) !== (expectedTaskId || null) ||
            (pageTeam || null) !== (expectedTeam || null)
          ) return {ok: false, method: 'ownership_conflict'};

          const messages = [...document.querySelectorAll('[data-message-id]')];
          const acceptedIndex = messages.findIndex(
            (node) => node.getAttribute('data-message-id') === expectedUserMessageId
          );
          if (acceptedIndex < 0) return {ok: false, method: 'accepted_user_missing'};

          const permissionPattern = /^Allow (mcp-[A-Za-z0-9._-]+) for this conversation$/i;
          const permissionNodes = [...document.querySelectorAll('button[aria-label]')]
            .map((node) => {
              const match = normalize(node.getAttribute('aria-label')).match(permissionPattern);
              return match ? {node, connector: match[1].toLowerCase()} : null;
            })
            .filter(Boolean)
            .filter(({connector}) => allowed.has(connector));

          const walkValues = (value, seen, depth = 0) => {
            if (!value || typeof value !== 'object' || seen.has(value) || depth > 5) return [];
            seen.add(value);
            const found = [];
            if (
              value.action && typeof value.action === 'object' &&
              value.action.type === 'allow' && value.action.remember_answer === true &&
              typeof value.action.target_message_id === 'string' && value.action.target_message_id
            ) found.push(value.action);
            if (
              value.type === 'allow' && value.remember_answer === true &&
              typeof value.target_message_id === 'string' && value.target_message_id
            ) found.push(value);
            for (const nested of Object.values(value)) {
              if (Array.isArray(nested)) {
                for (const item of nested.slice(0, 32)) found.push(...walkValues(item, seen, depth + 1));
              } else if (nested && typeof nested === 'object') {
                found.push(...walkValues(nested, seen, depth + 1));
              }
            }
            return found;
          };
          const candidates = [];
          for (const {node, connector} of permissionNodes) {
            let current = node;
            let fiber = null;
            let handler = null;
            const roots = [];
            for (let depth = 0; current && depth < 10; depth += 1, current = current.parentElement) {
              roots.push(current);
              const fiberKey = Object.keys(current).find((key) => key.startsWith('__reactFiber$'));
              if (!fiber && fiberKey) fiber = current[fiberKey];
              const propsKey = Object.keys(current).find((key) => key.startsWith('__reactProps$'));
              const props = propsKey ? current[propsKey] : null;
              if (!handler && props && typeof props.onSelectOption === 'function') handler = props.onSelectOption;
            }
            const containers = [];
            for (const root of roots) {
              const propsKey = Object.keys(root).find((key) => key.startsWith('__reactProps$'));
              if (propsKey) containers.push(root[propsKey]);
            }
            for (let depth = 0; fiber && depth < 40; depth += 1, fiber = fiber.return) {
              if (!handler) {
                for (const props of [fiber.memoizedProps, fiber.pendingProps]) {
                  if (props && typeof props.onSelectOption === 'function') handler = props.onSelectOption;
                }
              }
              containers.push(fiber.memoizedProps, fiber.pendingProps);
            }
            if (typeof handler !== 'function') continue;
            const actions = [];
            const seen = new Set();
            for (const container of containers) actions.push(...walkValues(container, seen));
            const unique = new Map(actions.map((action) => [
              `${action.type}:${action.target_message_id}:${action.remember_answer}`, action
            ]));
            for (const action of unique.values()) {
              const targetIndex = messages.findIndex(
                (message) => message.getAttribute('data-message-id') === action.target_message_id
              );
              if (targetIndex <= acceptedIndex) continue;
              if (expectedTargetMessageId && action.target_message_id !== expectedTargetMessageId) continue;
              candidates.push({connector, node, handler, action});
            }
          }
          if (candidates.length !== 1) {
            return {ok: false, method: 'permission_conflict', count: candidates.length};
          }
          const candidate = candidates[0];
          if (!dispatch) {
            return {
              ok: true,
              method: 'offered',
              connector: candidate.connector,
              target_message_id: candidate.action.target_message_id,
              remember_answer: 'true',
            };
          }
          const event = {
            preventDefault() {},
            stopPropagation() {},
            currentTarget: candidate.node,
            target: candidate.node,
          };
          try {
            await candidate.handler(event, candidate.action);
          } catch (error) {
            return {ok: false, method: 'handler_failed', error: String(error)};
          }
          return {
            ok: true,
            method: 'react_handler',
            connector: candidate.connector,
            target_message_id: candidate.action.target_message_id,
            remember_answer: 'true',
          };
        }""",
        [
            expected_page_id,
            expected_role,
            expected_task_id,
            expected_team,
            user_message_id,
            list(connectors),
            bool(dispatch),
            expected_target_message_id,
            ROLE_STORAGE_KEY,
            PAGE_ID_STORAGE_KEY,
            TASK_ID_STORAGE_KEY,
            TEAM_STORAGE_KEY,
            WINDOW_NAME_PREFIX,
        ],
    )
    if not result.get("ok"):
        method = str(result.get("method") or "permission_conflict")
        if method == "ownership_conflict":
            raise PageOwnershipError("page ownership changed before MCP permission dispatch")
        raise UnsafePageStateError(f"MCP permission Allow is not safely dispatchable: {method}")
    detail = f"{result.get('connector')}:{result.get('target_message_id')}"
    await record_page_action(page, "mcp_allow", "complete", detail=detail)
    return {
        "method": str(result.get("method") or "react_handler"),
        "connector": str(result.get("connector") or ""),
        "target_message_id": str(result.get("target_message_id") or ""),
        "remember_answer": str(result.get("remember_answer") or ""),
    }


async def send_prompt(
    page: Any, text: str, timeout_ms: int = 8_000, wait_for_stop: bool = True
) -> None:
    if not text.strip():
        raise ValueError("prompt must not be empty")
    expected_url = str(getattr(page, "url", "") or "") or None
    await set_composer_text(page, text, timeout_ms=timeout_ms)
    await click_send_button(
        page,
        timeout_ms=timeout_ms,
        expected_url=expected_url,
        expected_prompt=text.strip(),
    )
    if wait_for_stop:
        await page.locator(SELECTORS["stop"]).wait_for(
            state="visible", timeout=timeout_ms
        )


def delete_chat_dialog(page: Any) -> Any:
    """Return the visible ChatGPT delete-conversation dialog locator."""
    return page.locator('[role="dialog"]').filter(has_text="Delete chat?")


async def wait_for_delete_chat_dialog(page: Any, timeout_ms: int = 8_000) -> Any:
    dialog = delete_chat_dialog(page)
    await dialog.wait_for(state="visible", timeout=timeout_ms)
    return dialog


async def open_delete_chat_dialog(page: Any, timeout_ms: int = 8_000) -> str:
    """Open, but never confirm, the current chat deletion dialog."""
    if extract_session_id(page.url) is None:
        raise UnsafePageStateError("current page is not a saved ChatGPT conversation")
    await action_delay(page, "delete_dialog", DIALOG_DELAY_MULTIPLIER)
    await record_page_action(page, "delete_dialog", "click")
    await page.bring_to_front()
    composer = page.locator(SELECTORS["composer"]).first
    if await composer.is_visible():
        await composer.click()
    await page.keyboard.press(SHORTCUTS["delete_chat"])
    await wait_for_delete_chat_dialog(page, timeout_ms=timeout_ms)
    await record_page_action(page, "delete_dialog", "complete", detail="keyboard")
    return "keyboard"


async def confirm_delete_chat(page: Any, timeout_ms: int = 15_000) -> str:
    """Confirm an already-visible delete dialog after a destructive-action delay."""
    dialog = await wait_for_delete_chat_dialog(page, timeout_ms=timeout_ms)
    delete_button = dialog.get_by_role("button", name="Delete", exact=True).last
    await delete_button.wait_for(state="visible", timeout=timeout_ms)
    await action_delay(page, "delete_confirm", DELETE_DELAY_MULTIPLIER)
    await record_page_action(page, "delete_confirm", "click")
    await delete_button.click(timeout=timeout_ms)
    await page.wait_for_function(
        "() => !location.pathname.startsWith('/c/')",
        timeout=timeout_ms,
    )
    await record_page_action(page, "delete_confirm", "complete", detail=page.url)
    return page.url


async def delete_current_chat(page: Any, timeout_ms: int = 15_000) -> str:
    """Open and explicitly confirm deletion of the current saved conversation."""
    await open_delete_chat_dialog(page, timeout_ms=timeout_ms)
    return await confirm_delete_chat(page, timeout_ms=timeout_ms)


async def stop_response(page: Any, timeout_ms: int = 5_000) -> str:
    await record_page_action(page, "stop", "click")
    stop = page.locator(SELECTORS["stop"])
    await stop.wait_for(state="visible", timeout=timeout_ms)
    clicked = await page.evaluate(
        """selector => {
          const button = document.querySelector(selector);
          if (!button) return false;
          button.click();
          return true;
        }""",
        SELECTORS["stop"],
    )
    if not clicked:
        raise RuntimeError("visible Stop button disappeared before DOM click")
    await page.wait_for_function(
        """selector => {
          const button = document.querySelector(selector);
          return !button || !(button.offsetWidth || button.offsetHeight || button.getClientRects().length);
        }""",
        arg=SELECTORS["stop"],
        timeout=timeout_ms,
    )
    await record_page_action(page, "stop", "complete", detail="dom")
    return "dom"


async def dismiss_rate_limit_dialog(page: Any, *, timeout_ms: int = 10_000) -> str:
    """Dismiss only the known ChatGPT request-throttling dialog."""
    result = await page.evaluate(
        r"""() => {
          const visible = (element) => Boolean(
            element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
          );
          const text = (element) => (element?.innerText || element?.textContent || '')
            .replace(/\s+/g, ' ').trim();
          const markers = [
            'too many requests',
            'making requests too quickly',
            'temporarily limited access',
          ];
          const dialogs = [...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')]
            .filter(visible)
            .filter((dialog) => {
              const lower = text(dialog).toLowerCase();
              return markers.some((marker) => lower.includes(marker));
            });
          if (!dialogs.length) return {ok: true, method: 'already_clear', label: ''};
          for (const dialog of dialogs) {
            const button = [...dialog.querySelectorAll('button,[role="button"]')]
              .find((candidate) => {
                if (!visible(candidate) || candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') {
                  return false;
                }
                const label = [text(candidate), candidate.getAttribute('aria-label') || '']
                  .join(' ').toLowerCase();
                return label.includes('got it') || label === 'ok' || label === 'okay';
              });
            if (button) {
              const label = text(button) || button.getAttribute('aria-label') || 'Got it';
              button.click();
              return {ok: true, method: 'known_rate_limit_button', label};
            }
          }
          return {ok: false, method: 'known_dialog_without_dismiss_button', label: ''};
        }"""
    )
    if not result.get("ok"):
        raise RateLimitBlockedError(
            "known rate-limit dialog is visible but its safe dismiss button was not found"
        )
    if result.get("method") == "already_clear":
        return "already_clear"
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        snapshot = await inspect_chatgpt_page(page)
        if not rate_limit_dialogs(snapshot):
            return str(result.get("label") or "Got it")
        await asyncio.sleep(0.1)
    raise RateLimitBlockedError("rate-limit dialog remained visible after safe dismiss")


async def assign_page_role(
    page: Any, role: str, *, force_new_page_id: bool = False
) -> dict[str, str]:
    role = validate_page_role(role)
    hostname = urlparse(page.url).hostname
    if hostname not in {"chatgpt.com", "www.chatgpt.com"}:
        raise ValueError("page role can only be assigned on chatgpt.com")

    assigned = await page.evaluate(
        """
        ([roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix, role, forceNewPageId]) => {
          let pageId = forceNewPageId ? null : sessionStorage.getItem(pageIdKey);
          if (!pageId) {
            pageId = crypto.randomUUID();
            sessionStorage.setItem(pageIdKey, pageId);
          }
          const taskId = sessionStorage.getItem(taskIdKey);
          const team = sessionStorage.getItem(teamKey);
          sessionStorage.setItem(roleKey, role);
          window.name = windowNamePrefix + JSON.stringify({
            role,
            pageId,
            taskId: taskId || null,
            team: team || null,
          });
          return {page_id: pageId, page_role: role};
        }
        """,
        [
            ROLE_STORAGE_KEY,
            PAGE_ID_STORAGE_KEY,
            TASK_ID_STORAGE_KEY,
            TEAM_STORAGE_KEY,
            WINDOW_NAME_PREFIX,
            role,
            force_new_page_id,
        ],
    )
    await ensure_role_indicator(
        page,
        expected_role=role,
        expected_page_id=assigned["page_id"],
    )
    return assigned


_WAIT_PROBE_READ_SCRIPT = "() => window.__PLAYWRIGHT_AUTO_WAIT_PROBE__?.() || null"
_WAIT_PROBE_WAIT_SCRIPT = "([signature, timeoutMs, fallback]) => window.__PLAYWRIGHT_AUTO_WAIT_PROBE_WAIT__?.(signature, timeoutMs, fallback) || null"
_WAIT_PROBE_INSTALL_SCRIPT = r"""([roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix]) => {
          window.__PLAYWRIGHT_AUTO_WAIT_PROBE__ = () => {
          const visible = (element) => Boolean(
            element && window.getComputedStyle(element).visibility !== 'hidden' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
          );
          const boundedText = (element, limit = 512) => {
            const value = (element?.innerText || element?.textContent || '')
              .replace(/\s+/g, ' ').trim();
            return value.length <= limit ? value : value.slice(0, limit);
          };
          const firstVisible = (selector) => [...document.querySelectorAll(selector)].find(visible) || null;
          const latest = (selector) => [...document.querySelectorAll(selector)].filter(visible).at(-1) || null;
          const identity = (element) => ({
            messageId: element?.getAttribute('data-message-id') || null,
            turnId: element?.closest('[data-turn-id]')?.getAttribute('data-turn-id') || null,
          });
          const textShape = (element) => {
            const value = (element?.innerText || element?.textContent || '').replace(/\s+/g, ' ').trim();
            return {length: value.length, tail: value.slice(-160)};
          };

          const composer = firstVisible('[contenteditable="true"][role="textbox"]');
          const composerRoot = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || null;
          const stop = firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]');
          const activeResponse = latest('[data-streaming-response-status]');
          const lastUser = latest('[data-message-author-role="user"][data-message-id]');
          const lastAssistant = latest('[data-message-author-role="assistant"][data-message-id]');
          const userIdentity = identity(lastUser);
          const assistantIdentity = identity(lastAssistant);
          const assistantShape = textShape(lastAssistant);
          const activityShape = textShape(activeResponse);
          const errors = [...document.querySelectorAll('[role="alert"]')]
            .filter(visible).slice(-4).map((element) => boundedText(element, 240)).filter(Boolean);
          const dialogs = [...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')]
            .filter(visible).slice(-4).map((element) => boundedText(element, 240) || 'dialog');
          const overlaySelector = '#playwright-auto-role-badge-v3,#playwright-auto-role-control-v1,#playwright-auto-role-badge,#playwright-auto-role-badge-v2';
          const positiveChoice = /\b(?:continue|proceed|start|yes|ok|okay|accept|approve|allow|run)\b|\bgo ahead\b|\b(?:make|create|use) a plan\b/i;
          const negativeChoice = /\b(?:cancel|stop|dismiss|close|delete|remove|archive|share|copy)\b|\b(?:not now|no thanks)\b/i;
          const choiceLabel = (element) => {
            const textLabel = boundedText(element, 120);
            const ariaLabel = (element.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
            const testId = (element.getAttribute('data-testid') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
            if ([textLabel, ariaLabel, testId].some((label) => negativeChoice.test(label))) return '';
            return [textLabel, ariaLabel].find((label) => positiveChoice.test(label)) || '';
          };
          const normalizeLabel = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const mcpConversationAllow = /^Allow mcp-[A-Za-z0-9._-]+ for this conversation$/i;
          const mcpPermissionNodes = [...document.querySelectorAll('button[aria-label]')]
            .filter((button) => mcpConversationAllow.test(normalizeLabel(button.getAttribute('aria-label'))));
          const mcpPermissionGroups = [...document.querySelectorAll('button')]
            .map((primary) => {
              if (
                !visible(primary) || primary.disabled ||
                primary.getAttribute('aria-disabled') === 'true' ||
                !primary.closest('main,[role="dialog"],[data-testid^="modal-"]')
              ) return null;
              const primaryLabel = normalizeLabel(primary.innerText || primary.textContent || primary.getAttribute('aria-label'));
              if (primaryLabel !== 'Allow') return null;
              const group = primary.parentElement;
              if (!group) return null;
              const permissionControls = [...group.querySelectorAll('button[aria-label]')]
                .filter((button) =>
                  button !== primary && visible(button) && !button.disabled &&
                  button.getAttribute('aria-disabled') !== 'true' &&
                  mcpConversationAllow.test(normalizeLabel(button.getAttribute('aria-label')))
                );
              return permissionControls.length === 1 ? {primary, permission: permissionControls[0]} : null;
            })
            .filter(Boolean);
          const mcpPermissionControls = new Set(
            mcpPermissionGroups.flatMap(({primary, permission}) => [primary, permission])
          );
          const choices = composer ? [] : [...document.querySelectorAll('button,[role="button"]')]
            .filter((element) =>
              visible(element) && !element.disabled &&
              element.getAttribute('aria-disabled') !== 'true' &&
              !element.closest(overlaySelector) &&
              !mcpPermissionControls.has(element) &&
              Boolean(element.closest('main,[role="dialog"],[data-testid^="modal-"]'))
            )
            .map(choiceLabel)
            .filter(Boolean)
            .slice(0, 8);
          const attachmentCount = composerRoot ? composerRoot.querySelectorAll(
            '[data-filename], [data-file-name], [data-testid*="attachment"], [data-testid*="file"]'
          ).length : 0;

          let pageRole = null;
          let pageId = null;
          let pageTaskId = null;
          let pageTeam = null;
          try {
            pageRole = sessionStorage.getItem(roleKey);
            pageId = sessionStorage.getItem(pageIdKey);
            pageTaskId = sessionStorage.getItem(taskIdKey);
            pageTeam = sessionStorage.getItem(teamKey);
          } catch (_) {}
          if ((!pageRole || !pageId || !pageTaskId || !pageTeam) && window.name?.startsWith(windowNamePrefix)) {
            try {
              const binding = JSON.parse(window.name.slice(windowNamePrefix.length));
              pageRole = pageRole || binding.role || null;
              pageId = pageId || binding.pageId || null;
              pageTaskId = pageTaskId || binding.taskId || null;
              pageTeam = pageTeam || binding.team || null;
            } catch (_) {}
          }
          return {
            url: location.href,
            page_role: pageRole,
            page_id: pageId,
            page_task_id: pageTaskId,
            page_team: pageTeam,
            requires_login: location.hostname === 'auth.openai.com' || Boolean(firstVisible('[data-testid="login-button"]')),
            composer_present: Boolean(composer),
            composer_text: boundedText(composer, 512),
            attachment_count: attachmentCount,
            stop_visible: Boolean(stop),
            transport_active: Boolean(activeResponse),
            error_texts: errors,
            blocking_dialogs: dialogs,
            choice_prompt_labels: [...new Set(choices)],
            mcp_permission_allow_count: mcpPermissionGroups.length,
            mcp_permission_node_count: mcpPermissionNodes.length,
            last_user_message_id: userIdentity.messageId,
            last_user_turn_id: userIdentity.turnId,
            last_assistant_message_id: assistantIdentity.messageId,
            last_assistant_turn_id: assistantIdentity.turnId,
            assistant_text_length: assistantShape.length,
            assistant_text_tail: assistantShape.tail,
            response_activity_length: activityShape.length,
            response_activity_tail: activityShape.tail,
            response_activity_turn_id: activeResponse?.closest('[data-turn-id]')?.getAttribute('data-turn-id') || null,
          };
          };
          const transitionSignature = (probe) => JSON.stringify([
            probe.url,
            probe.page_id,
            probe.page_role,
            probe.page_task_id,
            probe.page_team,
            probe.requires_login,
            probe.composer_present,
            probe.composer_text,
            probe.attachment_count,
            probe.stop_visible,
            probe.transport_active,
            probe.error_texts,
            probe.blocking_dialogs,
            probe.choice_prompt_labels,
            probe.mcp_permission_allow_count,
            probe.mcp_permission_node_count,
            probe.last_user_message_id,
            probe.last_user_turn_id,
            probe.last_assistant_message_id,
            probe.last_assistant_turn_id,
            probe.response_activity_turn_id,
          ]);
          window.__PLAYWRIGHT_AUTO_WAIT_PROBE_WAIT__ = (previous, timeoutMs, fallback) => new Promise((resolve) => {
            let settled = false;
            let debounce = null;
            let observer = null;
            let timer = null;
            const root = document.documentElement || document;
            const relevantSelector = [
              'button[data-testid="stop-button"]',
              'button[aria-label*="Stop"]',
              '[data-streaming-response-status]',
              '[data-message-author-role][data-message-id]',
              '[contenteditable="true"][role="textbox"]',
              '[role="alert"]',
              '[role="dialog"]',
              '[data-testid^="modal-"]',
              'button[aria-label^="Allow mcp-"]',
              '[data-filename]',
              '[data-file-name]',
            ].join(',');
            const nodeIsRelevant = (node) => {
              if (!(node instanceof Element)) return false;
              return node.matches(relevantSelector) || Boolean(node.querySelector(relevantSelector));
            };
            const mutationIsRelevant = (mutation) => {
              if (mutation.type === 'attributes') {
                return [
                  'data-streaming-response-status', 'data-message-id', 'data-turn-id',
                  'contenteditable', 'aria-disabled', 'aria-label', 'data-testid', 'role',
                  'data-filename', 'data-file-name',
                ].includes(mutation.attributeName || '');
              }
              return [...mutation.addedNodes, ...mutation.removedNodes].some(nodeIsRelevant);
            };
            const finish = (probe) => {
              if (settled) return;
              settled = true;
              if (debounce) clearTimeout(debounce);
              if (timer) clearTimeout(timer);
              if (observer) observer.disconnect();
              root.removeEventListener('input', onInput, true);
              resolve(probe);
            };
            const check = () => {
              const probe = window.__PLAYWRIGHT_AUTO_WAIT_PROBE__();
              if (transitionSignature(probe) !== previous) finish(probe);
            };
            const onInput = () => check();
            observer = new MutationObserver((mutations) => {
              if (!mutations.some(mutationIsRelevant)) return;
              if (debounce) clearTimeout(debounce);
              debounce = setTimeout(check, 50);
            });
            observer.observe(root, {
              subtree: true,
              childList: true,
              attributes: true,
              attributeFilter: [
                'data-streaming-response-status', 'data-message-id', 'data-turn-id',
                'contenteditable', 'aria-disabled', 'aria-label', 'data-testid', 'role',
                'data-filename', 'data-file-name',
              ],
            });
            root.addEventListener('input', onInput, true);
            timer = setTimeout(() => {
              const probe = window.__PLAYWRIGHT_AUTO_WAIT_PROBE__();
              finish(transitionSignature(probe) === previous ? fallback : probe);
            }, Math.max(0, Number(timeoutMs) || 0));
            check();
          });
          return true;
        }"""


async def inspect_chatgpt_wait_probe(
    page: Any,
    *,
    previous_transition_signature: str | None = None,
    previous_probe: WaitProbe | None = None,
    wait_ms: int = 0,
) -> WaitProbe:
    """Read bounded wait-state evidence without walking or serializing the transcript."""
    use_wait = bool(
        previous_transition_signature and previous_probe is not None and wait_ms > 0
    )
    script = _WAIT_PROBE_WAIT_SCRIPT if use_wait else _WAIT_PROBE_READ_SCRIPT
    argument = (
        [previous_transition_signature, wait_ms, previous_probe.to_raw()]
        if use_wait and previous_probe is not None
        else None
    )
    raw = await page.evaluate(script, argument) if argument is not None else await page.evaluate(script)
    if not isinstance(raw, dict):
        await page.evaluate(
            _WAIT_PROBE_INSTALL_SCRIPT,
            [
                ROLE_STORAGE_KEY,
                PAGE_ID_STORAGE_KEY,
                TASK_ID_STORAGE_KEY,
                TEAM_STORAGE_KEY,
                WINDOW_NAME_PREFIX,
            ],
        )
        raw = await page.evaluate(script, argument) if argument is not None else await page.evaluate(script)
    if not isinstance(raw, dict):
        raise RuntimeError("ChatGPT wait probe did not install")
    url = str(raw.get("url") or page.url)
    return WaitProbe(
        url=url,
        session_id=extract_session_id(url),
        page_id=raw.get("page_id"),
        page_role=raw.get("page_role"),
        page_task_id=raw.get("page_task_id"),
        page_team=raw.get("page_team"),
        requires_login=bool(raw.get("requires_login")),
        composer_present=bool(raw.get("composer_present")),
        composer_text=str(raw.get("composer_text") or ""),
        attachment_count=max(0, int(raw.get("attachment_count") or 0)),
        stop_visible=bool(raw.get("stop_visible")),
        transport_active=bool(raw.get("transport_active")),
        error_texts=tuple(str(value) for value in raw.get("error_texts") or ()),
        blocking_dialogs=tuple(str(value) for value in raw.get("blocking_dialogs") or ()),
        choice_prompt_labels=tuple(str(value) for value in raw.get("choice_prompt_labels") or ()),
        mcp_permission_allow_count=max(0, int(raw.get("mcp_permission_allow_count") or 0)),
        mcp_permission_node_count=max(0, int(raw.get("mcp_permission_node_count") or 0)),
        last_user_message_id=(str(raw["last_user_message_id"]) if raw.get("last_user_message_id") else None),
        last_user_turn_id=(str(raw["last_user_turn_id"]) if raw.get("last_user_turn_id") else None),
        last_assistant_message_id=(str(raw["last_assistant_message_id"]) if raw.get("last_assistant_message_id") else None),
        last_assistant_turn_id=(str(raw["last_assistant_turn_id"]) if raw.get("last_assistant_turn_id") else None),
        assistant_text_length=max(0, int(raw.get("assistant_text_length") or 0)),
        assistant_text_tail=str(raw.get("assistant_text_tail") or ""),
        response_activity_length=max(0, int(raw.get("response_activity_length") or 0)),
        response_activity_tail=str(raw.get("response_activity_tail") or ""),
        response_activity_turn_id=(str(raw["response_activity_turn_id"]) if raw.get("response_activity_turn_id") else None),
    )


async def inspect_chatgpt_page(page: Any) -> ChatGPTSnapshot:
    raw = await page.evaluate(
        r"""
        ([roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix]) => {
          const visible = (element) => {
            const style = element ? window.getComputedStyle(element) : null;
            return Boolean(
              element && style && style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
              (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
            );
          };
          const text = (element) => (element?.innerText || "").replace(/\s+/g, " ").trim();
          const firstVisible = (selector) => [...document.querySelectorAll(selector)].find(visible) || null;

          const composer = firstVisible('[contenteditable="true"][role="textbox"]');
          const composerRoot = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || document;
          const sendCandidates = [...new Set([
            ...composerRoot.querySelectorAll('button,[role="button"]'),
            ...document.querySelectorAll('button[data-testid="send-button"], button[aria-label="Send prompt"], button[aria-label="Send"]')
          ])].map((button) => {
            const label = [
              text(button),
              button.getAttribute('aria-label') || '',
              button.getAttribute('data-testid') || ''
            ].join(' ').toLowerCase();
            const score =
              (button.getAttribute('data-testid') === 'send-button' ? 10 : 0) +
              (['Send prompt', 'Send'].includes(button.getAttribute('aria-label')) ? 8 : 0) +
              (label.includes('send') ? 4 : 0) +
              (button.type === 'submit' ? 3 : 0);
            return {button, score};
          }).filter((item) => visible(item.button) && item.score >= 4)
            .sort((a, b) => b.score - a.score);
          const send = sendCandidates[0]?.button || null;
          const stop = firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]');
          const login = firstVisible('[data-testid="login-button"]');
          const retry = firstVisible('[data-testid="regenerate-thread-error-button"]');
          const errorTexts = [...document.querySelectorAll('[role="alert"]')]
            .filter(visible)
            .map(text)
            .filter(Boolean);
          const blockingDialogs = [...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')]
            .filter(visible)
            .map((element) => text(element) || element.getAttribute('data-testid') || 'dialog')
            .filter(Boolean);
          const composerHost = composer?.closest('form') || composer?.parentElement || null;
          const elementLabel = (element) => [
            text(element),
            element?.getAttribute?.('aria-label') || '',
            element?.getAttribute?.('data-testid') || '',
          ].join(' ').replace(/\s+/g, ' ').trim();
          const attachmentLabel = elementLabel;
          const filenameFromLabel = (value) => {
            const label = String(value || '').replace(/\s+/g, ' ').trim();
            const lower = label.toLowerCase();
            for (const prefix of [
              'remove file', 'remove attachment', 'open image',
              'attached file', 'file uploaded', 'uploading'
            ]) {
              const index = lower.indexOf(prefix);
              if (index < 0) continue;
              const candidate = label.slice(index + prefix.length)
                .replace(/^[\s:–—-]+/, '').trim();
              const indexed = candidate.match(/^\d+\s*:\s*(.+)$/);
              if (indexed) return indexed[1].trim();
              if (candidate) return candidate;
            }
            return '';
          };
          const directFilename = (element) => {
            for (const attribute of ['data-filename', 'data-file-name']) {
              const candidate = (element.getAttribute?.(attribute) || '').trim();
              if (candidate) return candidate;
            }
            return filenameFromLabel(element.getAttribute?.('aria-label'));
          };
          const hasAttachmentToken = (element) => {
            const tokens = (element.getAttribute?.('data-testid') || '')
              .toLowerCase()
              .split(/[^a-z0-9]+/)
              .filter(Boolean);
            return tokens.includes('attachment') || tokens.includes('file');
          };
          const leafFilename = (root) => {
            const candidates = [];
            for (const element of [root, ...root.querySelectorAll('*')]) {
              if (!visible(element) || element.matches('button,[role="button"],svg,path')) continue;
              if ([...element.children].some(visible)) continue;
              const candidate = text(element);
              if (candidate && !['remove', 'open', 'attached', 'uploading'].includes(candidate.toLowerCase())) {
                candidates.push(candidate);
              }
            }
            return candidates.length === 1 ? candidates[0] : '';
          };
          const attachmentRecords = [];
          const seenAttachmentItems = new Set();
          if (composerHost) {
            for (const candidate of composerHost.querySelectorAll(
              '[data-filename], [data-file-name], [aria-label]'
            )) {
              if (!visible(candidate)) continue;
              const explicitItem = candidate.closest('[data-filename], [data-file-name]');
              const item = explicitItem && explicitItem !== composerHost && composerHost.contains(explicitItem) && visible(explicitItem) ? explicitItem : candidate;
              const filename = directFilename(item);
              if (!filename || seenAttachmentItems.has(item)) continue;
              seenAttachmentItems.add(item);
              attachmentRecords.push({element: item, filename});
            }
            for (const root of composerHost.querySelectorAll('[data-testid]')) {
              if (!visible(root) || !hasAttachmentToken(root)) continue;
              const hasFilenameEvidence = [root, ...root.querySelectorAll(
                '[data-filename], [data-file-name], [aria-label]'
              )].some((element) => visible(element) && Boolean(directFilename(element)));
              const hasNestedAttachmentRoot = [...root.querySelectorAll('[data-testid]')]
                .some((element) =>
                  element !== root && visible(element) && hasAttachmentToken(element)
                );
              if (hasFilenameEvidence || hasNestedAttachmentRoot || seenAttachmentItems.has(root)) continue;
              seenAttachmentItems.add(root);
              attachmentRecords.push({
                element: root,
                filename: leafFilename(root) || '\u0000unidentified attachment',
              });
            }
          }
          attachmentRecords.sort((left, right) => {
            if (left.element === right.element) return 0;
            const position = left.element.compareDocumentPosition(right.element);
            if (position & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (position & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
          });
          const attachmentMarkers = attachmentRecords.map((item) => item.filename);

          const choiceOverlaySelector = '#playwright-auto-role-badge-v3,#playwright-auto-role-control-v1,#playwright-auto-role-badge,#playwright-auto-role-badge-v2';
          const positiveChoice = /\b(?:continue|proceed|start|yes|ok|okay|accept|approve|allow|run)\b|\bgo ahead\b|\b(?:make|create|use) a plan\b/i;
          const negativeChoice = /\b(?:cancel|stop|dismiss|close|delete|remove|archive|share|copy)\b|\b(?:not now|no thanks)\b/i;
          const choiceLabel = (button) => {
            const textLabel = text(button).slice(0, 120);
            const ariaLabel = (button.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
            const testId = (button.getAttribute('data-testid') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
            if ([textLabel, ariaLabel, testId].some((label) => negativeChoice.test(label))) return '';
            return [textLabel, ariaLabel].find((label) => positiveChoice.test(label)) || '';
          };
          const choicePromptLabels = composer ? [] : [...document.querySelectorAll('button,[role="button"]')]
            .filter((button) =>
              visible(button) && !button.disabled &&
              button.getAttribute('aria-disabled') !== 'true' &&
              !button.closest(choiceOverlaySelector) &&
              Boolean(button.closest('main,[role="dialog"],[data-testid^="modal-"]'))
            )
            .map(choiceLabel)
            .filter(Boolean)
            .slice(0, 12);

          const chatRoot = document.querySelector('main') || document.body;
          const messages = [...chatRoot.querySelectorAll('[data-message-author-role][data-message-id]')]
            .filter(visible)
            .map((element) => {
              const turn = element.closest('[data-turn-id]');
              const actions = [...element.querySelectorAll('button[data-testid]')]
                .filter(visible)
                .map((button) => button.dataset.testid)
                .filter(Boolean);
              const imageCount = [...element.querySelectorAll('img')].filter(visible).length;
              return {
                role: element.getAttribute('data-message-author-role') || '',
                message_id: element.getAttribute('data-message-id') || '',
                turn_id: turn?.getAttribute('data-turn-id') || null,
                text: text(element),
                actions: [...new Set(actions)],
                image_count: imageCount,
              };
            });

          const activeResponse = [...chatRoot.querySelectorAll('[data-streaming-response-status]')]
            .filter(visible)
            .at(-1) || null;
          const responseActivityText = text(activeResponse);
          const responseActivityStructure = activeResponse
            ? [...activeResponse.querySelectorAll('[data-testid]')]
                .filter(visible)
                .map((element) => element.getAttribute('data-testid') || '')
                .filter(Boolean)
                .join('|')
            : '';
          const responseActivityTurnId = activeResponse
            ?.closest('[data-turn-id]')
            ?.getAttribute('data-turn-id') || null;

          const requiresLogin = location.hostname === 'auth.openai.com' || Boolean(login);
          const authCallbackError = location.hostname === 'chatgpt.com' && location.pathname === '/auth/error';
          const alertError = errorTexts.some((value) => /error|failed|issue|try again/i.test(value));

          let pageRole = null;
          let pageId = null;
          let pageTaskId = null;
          let pageTeam = null;
          try {
            pageRole = sessionStorage.getItem(roleKey);
            pageId = sessionStorage.getItem(pageIdKey);
            pageTaskId = sessionStorage.getItem(taskIdKey);
            pageTeam = sessionStorage.getItem(teamKey);
          } catch (_) {
            // Storage can be unavailable on transient auth/error pages.
          }
          if ((!pageRole || !pageId || !pageTaskId || !pageTeam) && window.name?.startsWith(windowNamePrefix)) {
            try {
              const binding = JSON.parse(window.name.slice(windowNamePrefix.length));
              pageRole = pageRole || binding.role || null;
              pageId = pageId || binding.pageId || null;
              pageTaskId = pageTaskId || binding.taskId || null;
              pageTeam = pageTeam || binding.team || null;
            } catch (_) {
              // Invalid or unrelated window.name values are ignored.
            }
          }

          return {
            url: location.href,
            page_role: pageRole,
            page_id: pageId,
            page_task_id: pageTaskId,
            page_team: pageTeam,
            requires_login: requiresLogin,
            composer_present: Boolean(composer),
            composer_editable: Boolean(
              composer && composer.getAttribute('contenteditable') === 'true' &&
              composer.getAttribute('aria-disabled') !== 'true'
            ),
            composer_text: text(composer),
            send_visible: Boolean(send),
            send_enabled: Boolean(
              send && !send.disabled && send.getAttribute('aria-disabled') !== 'true'
            ),
            stop_visible: Boolean(stop),
            blocking_dialogs: [...new Set(blockingDialogs)],
            attachment_markers: attachmentMarkers,
            choice_prompt_labels: [...new Set(choicePromptLabels)],
            error_present: Boolean(retry || authCallbackError || alertError),
            retry_visible: Boolean(retry),
            error_texts: errorTexts,
            response_activity_text: responseActivityText,
            response_activity_structure: responseActivityStructure,
            response_activity_turn_id: responseActivityTurnId,
            messages,
          };
        }
        """,
        [
            ROLE_STORAGE_KEY,
            PAGE_ID_STORAGE_KEY,
            TASK_ID_STORAGE_KEY,
            TEAM_STORAGE_KEY,
            WINDOW_NAME_PREFIX,
        ],
    )

    messages = tuple(
        MessageSnapshot(
            role=str(message.get("role") or ""),
            message_id=str(message.get("message_id") or ""),
            turn_id=message.get("turn_id"),
            text=str(message.get("text") or ""),
            actions=tuple(str(action) for action in message.get("actions") or []),
            image_count=int(message.get("image_count") or 0),
        )
        for message in raw.get("messages") or []
    )
    raw_for_state = dict(raw)
    raw_for_state["messages"] = [message.to_dict() for message in messages]

    return ChatGPTSnapshot(
        url=str(raw.get("url") or page.url),
        session_id=extract_session_id(str(raw.get("url") or page.url)),
        page_id=raw.get("page_id"),
        page_role=raw.get("page_role"),
        page_task_id=raw.get("page_task_id"),
        page_team=raw.get("page_team"),
        response_activity_text=str(raw.get("response_activity_text") or ""),
        response_activity_structure=str(raw.get("response_activity_structure") or ""),
        response_activity_turn_id=(
            str(raw.get("response_activity_turn_id"))
            if raw.get("response_activity_turn_id") is not None
            else None
        ),
        response_activity_length=len(str(raw.get("response_activity_text") or "")),
        state=classify_chatgpt_state(raw_for_state),
        requires_login=bool(raw.get("requires_login")),
        composer_present=bool(raw.get("composer_present")),
        composer_editable=bool(raw.get("composer_editable")),
        composer_text=str(raw.get("composer_text") or ""),
        send_visible=bool(raw.get("send_visible")),
        send_enabled=bool(raw.get("send_enabled")),
        stop_visible=bool(raw.get("stop_visible")),
        blocking_dialogs=tuple(
            str(value) for value in raw.get("blocking_dialogs") or []
        ),
        attachment_markers=tuple(
            str(value) for value in raw.get("attachment_markers") or []
        ),
        error_texts=tuple(str(value) for value in raw.get("error_texts") or []),
        messages=messages,
        choice_prompt_labels=tuple(
            str(value) for value in raw.get("choice_prompt_labels") or []
        ),
        retry_visible=bool(raw.get("retry_visible")),
    )


class ChatGPTPage:
    """Safe object API for one physical ChatGPT tab.

    The tab binding is immutable after ``set_role`` unless an explicit rebind is
    requested. Mutating operations verify the binding immediately before action.
    A physical-tab workflow lock prevents two workflows from interleaving on the
    same page, while a separate mutation lock serializes individual UI actions.
    """

    def __init__(self, page: Any, *, timeout_ms: int = 8_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.binding: PageBinding | None = None
        self._owned_composer_text: str | None = None
        self._frontend_identity_task: asyncio.Future[dict[str, str] | None] | None = None
        self._frontend_identity_listener: Callable[[Any], None] | None = None
        self._wait_state = _page_wait_state(page)
        self._wait_metrics: dict[str, Any] = {
            "sparse_probes": 0,
            "full_snapshots": 0,
            "unchanged_ticks": 0,
            "sparse_errors": 0,
            "full_snapshot_reasons": {},
        }

    def _remove_frontend_identity_listener(self) -> None:
        listener = self._frontend_identity_listener
        self._frontend_identity_listener = None
        if listener is None:
            return
        try:
            self.page.remove_listener("response", listener)
        except Exception:
            try:
                self.page.off("response", listener)
            except Exception:
                pass

    def _reset_frontend_identity_observer(self) -> None:
        self._remove_frontend_identity_listener()
        future = self._frontend_identity_task
        self._frontend_identity_task = None
        if future is not None and not future.done():
            future.set_result(None)

    def _arm_frontend_identity_observer(self) -> None:
        self._reset_frontend_identity_observer()
        future: asyncio.Future[dict[str, str] | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._frontend_identity_task = future

        def finish(value: dict[str, str] | None) -> None:
            if not future.done():
                future.set_result(value)

        def on_response(response: Any) -> None:
            try:
                if not _matches_frontend_conversation_response(response):
                    return
                observed_user_message_id = _frontend_user_message_id(response.request)
                self._remove_frontend_identity_listener()
                if observed_user_message_id is None:
                    finish(None)
                    return

                async def reduce_response() -> None:
                    finish(
                        await _reduce_frontend_conversation_response(
                            response, observed_user_message_id
                        )
                    )

                asyncio.create_task(reduce_response())
            except Exception:
                self._remove_frontend_identity_listener()
                finish(None)

        try:
            self._frontend_identity_listener = on_response
            self.page.on("response", on_response)
        except Exception:
            self._frontend_identity_listener = None
            finish(None)

    def take_frontend_identity_task(
        self,
    ) -> asyncio.Future[dict[str, str] | None] | None:
        task = self._frontend_identity_task
        self._frontend_identity_task = None
        return task

    def _remove_passive_observer(self) -> None:
        state = _page_passive_observation_state(self.page)
        listener = state.get("listener")
        close_listener = state.get("close_listener")
        state["listener"] = None
        state["close_listener"] = None
        for event, callback in (("response", listener), ("close", close_listener)):
            if callback is None:
                continue
            try:
                self.page.remove_listener(event, callback)
            except Exception:
                try:
                    self.page.off(event, callback)
                except Exception:
                    pass
        for task in tuple(state.get("tasks") or ()):  # bounded reducer tasks only
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
        state["tasks"] = set()
        old_wake = state.get("wake_event")
        state["scope_revision"] = int(state.get("scope_revision") or 0) + 1
        state["scope"] = None
        state["latest"] = None
        state["wake_event"] = None
        if isinstance(old_wake, asyncio.Event):
            old_wake.set()

    def arm_passive_observer(
        self,
        *,
        request_id: str,
        generation: int,
        conversation_id: str | None = None,
        accepted_user_message_id: str | None = None,
    ) -> None:
        exact_request = _safe_identity_string(request_id)
        if exact_request is None:
            raise ValueError("passive observer request ID must be bounded and printable")
        if isinstance(generation, bool) or int(generation) < 0:
            raise ValueError("passive observer generation must be non-negative")
        exact_conversation = (
            _safe_identity_string(conversation_id) if conversation_id is not None else None
        )
        exact_user = (
            _safe_identity_string(accepted_user_message_id)
            if accepted_user_message_id is not None
            else None
        )
        state = _page_passive_observation_state(self.page)
        old_scope = state.get("scope") if isinstance(state.get("scope"), Mapping) else {}
        old_key = (old_scope.get("request_id"), old_scope.get("generation"))
        new_key = (exact_request, int(generation))
        same_request = old_key == new_key
        scope = {
            "request_id": exact_request,
            "generation": int(generation),
            "conversation_id": (
                exact_conversation
                if exact_conversation is not None
                else (old_scope.get("conversation_id") if same_request else None)
            ),
            "accepted_user_message_id": (
                exact_user
                if exact_user is not None
                else (old_scope.get("accepted_user_message_id") if same_request else None)
            ),
        }
        scope_changed = dict(old_scope) != scope
        latest = state.get("latest")
        retained_latest = bool(
            same_request
            and isinstance(latest, Mapping)
            and _passive_evidence_matches_scope(latest, scope)
        )
        if not retained_latest:
            state["latest"] = None
        if scope_changed:
            state["scope_revision"] = int(state.get("scope_revision") or 0) + 1
            state["wake_event"] = asyncio.Event()
        elif not isinstance(state.get("wake_event"), asyncio.Event):
            state["wake_event"] = asyncio.Event()
        state["scope"] = scope
        if retained_latest and isinstance(state.get("wake_event"), asyncio.Event):
            state["wake_event"].set()
        if state.get("listener") is not None:
            return

        page = self.page

        async def reduce_response(
            response: Any,
            *,
            scope_key: tuple[str, int],
            scope_revision: int,
            observed_user_message_id: str | None,
            paged_conversation_id: str | None,
        ) -> None:
            try:
                if paged_conversation_id is not None:
                    current_scope = state.get("scope")
                    if not isinstance(current_scope, Mapping):
                        return
                    expected_user = _safe_identity_string(
                        current_scope.get("accepted_user_message_id")
                    )
                    if expected_user is None:
                        return
                    evidence = _reduce_paged_conversation_body(
                        await response.body(),
                        conversation_id=paged_conversation_id,
                        observed_user_message_id=expected_user,
                    )
                else:
                    if observed_user_message_id is None:
                        return
                    evidence = await _reduce_frontend_conversation_response_observation(
                        response, observed_user_message_id
                    )
                if not isinstance(evidence, Mapping):
                    return
                current_scope = state.get("scope")
                if not isinstance(current_scope, Mapping):
                    return
                if (
                    current_scope.get("request_id"),
                    current_scope.get("generation"),
                ) != scope_key or int(state.get("scope_revision") or 0) != scope_revision:
                    return
                evidence_conversation = _safe_identity_string(evidence.get("conversation_id"))
                evidence_user = _safe_identity_string(evidence.get("observed_user_message_id"))
                expected_conversation = _safe_identity_string(
                    current_scope.get("conversation_id")
                )
                expected_user = _safe_identity_string(
                    current_scope.get("accepted_user_message_id")
                )
                if expected_conversation and evidence_conversation != expected_conversation:
                    return
                if expected_user and evidence_user != expected_user:
                    return
                refined_scope = dict(current_scope)
                if expected_conversation is None and evidence_conversation is not None:
                    refined_scope["conversation_id"] = evidence_conversation
                if expected_user is None and evidence_user is not None:
                    refined_scope["accepted_user_message_id"] = evidence_user
                if refined_scope != dict(current_scope):
                    state["scope"] = refined_scope
                state["latest"] = {
                    **dict(evidence),
                    "request_id": scope_key[0],
                    "generation": scope_key[1],
                    "observed_at": time.monotonic(),
                }
                state["event_count"] = int(state.get("event_count") or 0) + 1
                wake_event = state.get("wake_event")
                if isinstance(wake_event, asyncio.Event):
                    wake_event.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        def on_response(response: Any) -> None:
            current_scope = state.get("scope")
            if not isinstance(current_scope, Mapping):
                return
            request = str(current_scope.get("request_id") or "")
            generation_value = current_scope.get("generation")
            if not request or not isinstance(generation_value, int):
                return
            scope_key = (request, generation_value)
            scope_revision = int(state.get("scope_revision") or 0)
            observed_user_message_id: str | None = None
            paged_conversation_id: str | None = None
            if _matches_frontend_conversation_response(response):
                observed_user_message_id = _frontend_user_message_id(response.request)
                expected_user = _safe_identity_string(
                    current_scope.get("accepted_user_message_id")
                )
                if expected_user and observed_user_message_id != expected_user:
                    return
            else:
                paged_conversation_id = _paged_conversation_id_from_response(response)
                expected_conversation = _safe_identity_string(
                    current_scope.get("conversation_id")
                )
                if (
                    paged_conversation_id is None
                    or expected_conversation is None
                    or paged_conversation_id != expected_conversation
                ):
                    return
            task = asyncio.create_task(
                reduce_response(
                    response,
                    scope_key=scope_key,
                    scope_revision=scope_revision,
                    observed_user_message_id=observed_user_message_id,
                    paged_conversation_id=paged_conversation_id,
                )
            )
            tasks = state.setdefault("tasks", set())
            tasks.add(task)
            task.add_done_callback(lambda done: tasks.discard(done))

        def on_close(*_args: Any) -> None:
            self._remove_passive_observer()

        state["listener"] = on_response
        state["close_listener"] = on_close
        try:
            page.on("response", on_response)
            page.on("close", on_close)
        except Exception:
            self._remove_passive_observer()
            raise

    def passive_observation(
        self,
        *,
        request_id: str,
        generation: int,
    ) -> dict[str, Any]:
        state = _page_passive_observation_state(self.page)
        scope = state.get("scope")
        latest = state.get("latest")
        if not isinstance(scope, Mapping) or (
            scope.get("request_id"),
            scope.get("generation"),
        ) != (str(request_id), int(generation)):
            return {"coverage": "unknown", "event_count": int(state.get("event_count") or 0)}
        if not isinstance(latest, Mapping) or not _passive_evidence_matches_scope(latest, scope):
            return {"coverage": "unknown", "event_count": int(state.get("event_count") or 0)}
        return {**dict(latest), "event_count": int(state.get("event_count") or 0)}

    async def wait_for_passive_observation(
        self,
        *,
        request_id: str,
        generation: int,
        timeout_ms: int,
    ) -> bool | None:
        """Wait within the caller's probe window when this exact passive scope is armed.

        ``None`` means no passive scope owned the interval, so the caller must retain
        its normal DOM wait. ``False`` means passive waiting did own the interval but
        produced no usable exact wake; ``True`` means exact passive evidence woke it.
        """
        if timeout_ms <= 0:
            return None
        state = _page_passive_observation_state(self.page)
        scope = state.get("scope")
        if not isinstance(scope, Mapping) or (
            scope.get("request_id"),
            scope.get("generation"),
        ) != (str(request_id), int(generation)):
            return None
        revision = int(state.get("scope_revision") or 0)
        wake_event = state.get("wake_event")
        if not isinstance(wake_event, asyncio.Event):
            return None
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return False
        current_scope = state.get("scope")
        if (
            not isinstance(current_scope, Mapping)
            or int(state.get("scope_revision") or 0) != revision
            or (
                current_scope.get("request_id"),
                current_scope.get("generation"),
            ) != (str(request_id), int(generation))
        ):
            return False
        wake_event.clear()
        evidence = self.passive_observation(request_id=request_id, generation=generation)
        if evidence.get("coverage") == "unknown":
            return False
        expected_conversation = _safe_identity_string(current_scope.get("conversation_id"))
        expected_user = _safe_identity_string(current_scope.get("accepted_user_message_id"))
        return bool(expected_conversation and expected_user)

    def detach_passive_observer(self) -> None:
        self._remove_passive_observer()

    def _captured_conversation_id(self, accepted_user_message_id: str | None) -> str | None:
        task = self._frontend_identity_task
        if task is None or not task.done() or accepted_user_message_id is None:
            return None
        try:
            evidence = task.result()
        except Exception:
            return None
        if not isinstance(evidence, Mapping):
            return None
        if evidence.get("observed_user_message_id") != accepted_user_message_id:
            return None
        return _safe_identity_string(evidence.get("conversation_id"))

    @property
    def _backend_token(self) -> str | None:
        value = _backend_context_state(self.page.context).get("token")
        return str(value) if isinstance(value, str) and value else None

    async def _backend_session_token(self) -> str:
        return await _backend_session_token(self.page.context)

    async def _backend_get_object(self, path: str, *, category: str) -> dict[str, Any]:
        return await _backend_get_object(self.page.context, path, category=category)

    async def backend_stream_status(self, conversation_id: str) -> dict[str, Any]:
        return await backend_stream_status(self.page.context, conversation_id)

    def release_backend_stream_status(self, conversation_id: str) -> None:
        release_backend_stream_status(self.page.context, conversation_id)

    @property
    def _wait_probe(self) -> WaitProbe | None:
        return self._wait_state["probe"]

    @_wait_probe.setter
    def _wait_probe(self, value: WaitProbe | None) -> None:
        self._wait_state["probe"] = value

    @property
    def _wait_snapshot_cache(self) -> ChatGPTSnapshot | None:
        return self._wait_state["snapshot"]

    @_wait_snapshot_cache.setter
    def _wait_snapshot_cache(self, value: ChatGPTSnapshot | None) -> None:
        self._wait_state["snapshot"] = value

    @property
    def _wait_receipt_key(self) -> tuple[object, ...] | None:
        return self._wait_state["receipt_key"]

    @_wait_receipt_key.setter
    def _wait_receipt_key(self, value: tuple[object, ...] | None) -> None:
        self._wait_state["receipt_key"] = value

    @property
    def _wait_full_snapshot_at(self) -> float:
        return float(self._wait_state["full_snapshot_at"])

    @_wait_full_snapshot_at.setter
    def _wait_full_snapshot_at(self, value: float) -> None:
        self._wait_state["full_snapshot_at"] = float(value)

    @asynccontextmanager
    async def workflow_guard(self):
        async with _page_lock(_PAGE_WORKFLOW_LOCKS, self.page):
            yield

    @asynccontextmanager
    async def mutation_guard(self):
        async with _page_lock(_PAGE_MUTATION_LOCKS, self.page):
            yield

    async def snapshot(self) -> ChatGPTSnapshot:
        return await asyncio.wait_for(
            inspect_chatgpt_page(self.page),
            timeout=max(self.timeout_ms, 1) / 1000,
        )

    @property
    def wait_metrics(self) -> dict[str, Any]:
        return {
            **self._wait_metrics,
            "full_snapshot_reasons": dict(self._wait_metrics["full_snapshot_reasons"]),
        }

    def invalidate_wait_cache(self) -> None:
        self._wait_probe = None
        self._wait_snapshot_cache = None
        self._wait_receipt_key = None
        self._wait_full_snapshot_at = 0.0

    def _assert_wait_probe_ownership(self, probe: WaitProbe) -> None:
        current_hostname = urlparse(probe.url).hostname
        current_path = urlparse(probe.url).path
        if probe.requires_login or current_hostname == "auth.openai.com" or (
            current_hostname in {"chatgpt.com", "www.chatgpt.com"}
            and current_path == "/auth/error"
        ):
            raise AuthenticationRequiredError(
                f"tab requires authentication at {probe.url!r}; "
                f"visible binding role={probe.page_role!r} page_id={probe.page_id!r}"
            )
        if self.binding is None:
            raise PageOwnershipError("ChatGPT tab is not bound; run SetRoleBlock before mutation")
        if probe.page_id != self.binding.page_id:
            raise PageOwnershipError(
                f"physical tab changed: expected page_id={self.binding.page_id!r}, got {probe.page_id!r}"
            )
        if probe.page_role != self.binding.role:
            raise PageOwnershipError(
                f"logical role changed: expected role={self.binding.role!r}, got {probe.page_role!r}"
            )

    @staticmethod
    def _wait_key(receipt: SendReceipt) -> tuple[object, ...]:
        return (
            receipt.prompt_sha256,
            receipt.binding.page_id,
            receipt.binding.role,
            receipt.user_message_id,
            receipt.user_turn_id,
            tuple(sorted(receipt.baseline.message_ids)),
        )

    def _wait_full_reason(
        self,
        probe: WaitProbe,
        *,
        force_full: bool,
        safety_interval_ms: int,
    ) -> str | None:
        previous = self._wait_probe
        cached = self._wait_snapshot_cache
        if force_full:
            return "forced"
        if previous is None or cached is None:
            return "initial"
        if probe.identity_signature != previous.identity_signature:
            return "message_identity_changed"
        if previous.transport_active and not probe.transport_active:
            return "transport_completed"
        if previous.stop_visible and not probe.stop_visible:
            return "stop_completed"
        if (
            probe.error_texts != previous.error_texts
            or probe.blocking_dialogs != previous.blocking_dialogs
        ):
            return "error_or_dialog"
        if (
            probe.manual_input_pending != previous.manual_input_pending
            or probe.choice_prompt_pending != previous.choice_prompt_pending
            or probe.composer_text != previous.composer_text
            or probe.attachment_count != previous.attachment_count
        ):
            return "manual_or_choice"
        assistant_changed = (
            probe.assistant_text_length != previous.assistant_text_length
            or probe.assistant_text_tail != previous.assistant_text_tail
        )
        if assistant_changed and not probe.stop_visible and not probe.transport_active:
            return "assistant_changed_without_transport"
        if (time.monotonic() - self._wait_full_snapshot_at) * 1000 >= safety_interval_ms:
            return "safety_interval"
        return None

    @staticmethod
    def _snapshot_from_probe(cached: ChatGPTSnapshot, probe: WaitProbe) -> ChatGPTSnapshot:
        if probe.requires_login:
            state = ChatGPTState.AUTH_REQUIRED
        elif probe.error_texts:
            state = ChatGPTState.ERROR
        elif probe.stop_visible or probe.transport_active:
            state = ChatGPTState.RESPONDING
        else:
            state = cached.state
        markers = tuple(f"attachment-{index + 1}" for index in range(probe.attachment_count))
        return replace(
            cached,
            url=probe.url,
            session_id=probe.session_id,
            page_id=probe.page_id,
            page_role=probe.page_role,
            page_task_id=probe.page_task_id,
            page_team=probe.page_team,
            state=state,
            requires_login=probe.requires_login,
            composer_present=probe.composer_present,
            composer_text=probe.composer_text,
            stop_visible=probe.stop_visible,
            blocking_dialogs=probe.blocking_dialogs,
            attachment_markers=markers,
            error_texts=probe.error_texts,
            choice_prompt_labels=probe.choice_prompt_labels,
            response_activity_text=probe.response_activity_tail,
            response_activity_structure=f"bounded:{probe.response_activity_length}",
            response_activity_turn_id=probe.response_activity_turn_id,
            response_activity_length=probe.response_activity_length,
        )

    async def wait_snapshot(
        self,
        receipt: SendReceipt,
        *,
        force_full: bool = False,
        probe_wait_ms: int = 0,
        safety_interval_ms: int = 600_000,
    ) -> ChatGPTSnapshot:
        return await asyncio.wait_for(
            self._wait_snapshot_within_budget(
                receipt,
                force_full=force_full,
                probe_wait_ms=probe_wait_ms,
                safety_interval_ms=safety_interval_ms,
            ),
            timeout=max(self.timeout_ms, 1) / 1000,
        )

    async def _wait_snapshot_within_budget(
        self,
        receipt: SendReceipt,
        *,
        force_full: bool = False,
        probe_wait_ms: int = 0,
        safety_interval_ms: int = 600_000,
    ) -> ChatGPTSnapshot:
        key = self._wait_key(receipt)
        if key != self._wait_receipt_key:
            self.invalidate_wait_cache()
            self._wait_receipt_key = key
            force_full = True
        try:
            probe = await inspect_chatgpt_wait_probe(
                self.page,
                previous_transition_signature=(
                    self._wait_probe.transition_signature
                    if self._wait_probe is not None
                    else None
                ),
                previous_probe=self._wait_probe,
                wait_ms=max(0, int(probe_wait_ms)),
            )
            self._wait_metrics["sparse_probes"] += 1
            self._assert_wait_probe_ownership(probe)
        except (AuthenticationRequiredError, PageOwnershipError):
            raise
        except Exception:
            self._wait_metrics["sparse_errors"] += 1
            probe = None

        reason = "sparse_error" if probe is None else self._wait_full_reason(
            probe,
            force_full=force_full,
            safety_interval_ms=safety_interval_ms,
        )
        if reason is not None:
            full = await self.assert_ownership()
            self._wait_snapshot_cache = full
            self._wait_full_snapshot_at = time.monotonic()
            self._wait_metrics["full_snapshots"] += 1
            reasons = self._wait_metrics["full_snapshot_reasons"]
            reasons[reason] = int(reasons.get(reason, 0)) + 1
            if probe is None:
                self._wait_probe = WaitProbe(
                    url=full.url,
                    session_id=full.session_id,
                    page_id=full.page_id,
                    page_role=full.page_role,
                    page_task_id=full.page_task_id,
                    page_team=full.page_team,
                    requires_login=full.requires_login,
                    composer_present=full.composer_present,
                    composer_text=full.composer_text,
                    attachment_count=len(full.attachment_markers),
                    stop_visible=full.stop_visible,
                    transport_active=bool(full.response_activity_turn_id),
                    error_texts=full.error_texts,
                    blocking_dialogs=full.blocking_dialogs,
                    choice_prompt_labels=full.choice_prompt_labels,
                    mcp_permission_allow_count=0,
                    mcp_permission_node_count=0,
                    last_user_message_id=next((item.message_id for item in reversed(full.messages) if item.role == "user"), None),
                    last_user_turn_id=next((item.turn_id for item in reversed(full.messages) if item.role == "user"), None),
                    last_assistant_message_id=next((item.message_id for item in reversed(full.messages) if item.role == "assistant"), None),
                    last_assistant_turn_id=next((item.turn_id for item in reversed(full.messages) if item.role == "assistant"), None),
                    assistant_text_length=len(next((item.text for item in reversed(full.messages) if item.role == "assistant"), "")),
                    assistant_text_tail=next((item.text[-160:] for item in reversed(full.messages) if item.role == "assistant"), ""),
                    response_activity_length=full.response_activity_length,
                    response_activity_tail=full.response_activity_text[-160:],
                    response_activity_turn_id=full.response_activity_turn_id,
                )
            else:
                self._wait_probe = probe
            return full

        assert probe is not None and self._wait_snapshot_cache is not None
        self._wait_probe = probe
        self._wait_metrics["unchanged_ticks"] += 1
        return self._snapshot_from_probe(self._wait_snapshot_cache, probe)

    async def assert_ownership(
        self,
        snapshot: ChatGPTSnapshot | None = None,
        *,
        require_binding: bool = True,
    ) -> ChatGPTSnapshot:
        return await asyncio.wait_for(
            self._assert_ownership_within_budget(
                snapshot,
                require_binding=require_binding,
            ),
            timeout=max(self.timeout_ms, 1) / 1000,
        )

    async def _assert_ownership_within_budget(
        self,
        snapshot: ChatGPTSnapshot | None = None,
        *,
        require_binding: bool = True,
    ) -> ChatGPTSnapshot:
        current = snapshot or await self.snapshot()
        current_hostname = urlparse(current.url).hostname
        current_path = urlparse(current.url).path
        if current_hostname == "auth.openai.com" or (
            current_hostname in {"chatgpt.com", "www.chatgpt.com"}
            and current_path == "/auth/error"
        ):
            raise AuthenticationRequiredError(
                f"tab requires authentication at {current.url!r}; "
                f"visible binding role={current.page_role!r} page_id={current.page_id!r}"
            )
        if self.binding is None:
            if require_binding:
                raise PageOwnershipError(
                    "ChatGPT tab is not bound; run SetRoleBlock before mutation"
                )
            return current
        if current.page_id != self.binding.page_id:
            raise PageOwnershipError(
                f"physical tab changed: expected page_id={self.binding.page_id!r}, "
                f"got {current.page_id!r}"
            )
        if current.page_role != self.binding.role:
            raise PageOwnershipError(
                f"logical role changed: expected role={self.binding.role!r}, "
                f"got {current.page_role!r}"
            )
        try:
            await ensure_role_indicator(
                self.page,
                expected_role=self.binding.role,
                expected_page_id=self.binding.page_id,
            )
        except RuntimeError as exc:
            raise PageOwnershipError(str(exc)) from exc
        return current

    async def health(
        self, snapshot: ChatGPTSnapshot | None = None
    ) -> PageHealth:
        try:
            current = await self.assert_ownership(snapshot)
        except PageOwnershipError:
            raise
        except Exception as exc:
            return PageHealth(
                False,
                "connection_error",
                "reload",
                f"{type(exc).__name__}: {exc}",
            )
        if current.requires_login:
            return PageHealth(False, "auth_required", "manual_login")
        if current.state is ChatGPTState.ERROR:
            return PageHealth(
                False,
                "page_error",
                "reload",
                "; ".join(current.error_texts),
            )
        if (
            current.composer_present
            or current.stop_visible
            or current.messages
            or current.choice_prompt_pending
        ):
            return PageHealth(True, "healthy", "none")
        return PageHealth(False, "empty_snapshot", "reload")

    async def known_rate_limit_visible(self) -> bool:
        """Lightweight read-only check for the known account-throttle dialog."""
        return bool(
            await self.page.evaluate(
                r"""([markers, testids]) => {
                  const visible = (element) => Boolean(
                    element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                  );
                  const text = (element) => (element?.innerText || element?.textContent || '')
                    .replace(/\s+/g, ' ').trim().toLowerCase();
                  return [...document.querySelectorAll('[role=\"dialog\"], [data-testid^=\"modal-\"]')]
                    .filter(visible)
                    .some((dialog) =>
                      testids.includes(dialog.getAttribute('data-testid') || '') ||
                      markers.some((marker) => text(dialog).includes(marker))
                    );
                }""",
                [list(_RATE_LIMIT_MARKERS), list(_RATE_LIMIT_DIALOG_TEST_IDS)],
            )
        )

    async def dismiss_known_rate_limit(
        self, *, timeout_ms: int | None = None
    ) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            limited = rate_limit_dialogs(snapshot)
            unknown = tuple(
                dialog for dialog in snapshot.blocking_dialogs if dialog not in limited
            )
            if unknown:
                raise UnsafePageStateError(
                    f"refusing to dismiss unknown blocking dialog(s): {list(unknown)!r}"
                )
            if not limited:
                return "already_clear"
            result = await dismiss_rate_limit_dialog(self.page, timeout_ms=timeout)
            confirmed = await self.assert_ownership()
            if rate_limit_dialogs(confirmed):
                raise RateLimitBlockedError("request rate limit remained after safe dismiss")
            return result

    async def current_wait_probe(self) -> WaitProbe:
        probe = await inspect_chatgpt_wait_probe(self.page)
        self._assert_wait_probe_ownership(probe)
        return probe

    async def inspect_mcp_permission_allow(
        self,
        probe: WaitProbe,
        receipt: SendReceipt,
        *,
        allowed_connectors: Sequence[str],
    ) -> dict[str, str]:
        if receipt.user_message_id is None:
            raise UnsafePageStateError("MCP permission approval requires exact accepted user identity")
        self._assert_wait_probe_ownership(probe)
        assert self.binding is not None
        return await click_mcp_permission_allow(
            self.page,
            expected_page_id=self.binding.page_id,
            expected_role=self.binding.role,
            expected_task_id=probe.page_task_id,
            expected_team=probe.page_team,
            expected_user_message_id=receipt.user_message_id,
            allowed_connectors=allowed_connectors,
            dispatch=False,
        )

    async def approve_mcp_permission_allow(
        self,
        probe: WaitProbe,
        receipt: SendReceipt,
        *,
        allowed_connectors: Sequence[str],
        expected_target_message_id: str,
    ) -> dict[str, str]:
        """Explicitly dispatch one exact previously-inspected MCP allow action."""
        if receipt.user_message_id is None:
            raise UnsafePageStateError("MCP permission approval requires exact accepted user identity")
        async with self.mutation_guard():
            self._assert_wait_probe_ownership(probe)
            assert self.binding is not None
            return await click_mcp_permission_allow(
                self.page,
                expected_page_id=self.binding.page_id,
                expected_role=self.binding.role,
                expected_task_id=probe.page_task_id,
                expected_team=probe.page_team,
                expected_user_message_id=receipt.user_message_id,
                allowed_connectors=allowed_connectors,
                dispatch=True,
                expected_target_message_id=expected_target_message_id,
            )

    async def resolve_choice_prompt(
        self, *, timeout_ms: int | None = None
    ) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            before = await self.assert_ownership()
            if not before.choice_prompt_pending:
                raise ChoicePromptBlockedError("no safe choice prompt is pending")
            label = await click_safe_choice_prompt(self.page)
            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                current = await self.assert_ownership()
                if current.composer_present or not current.choice_prompt_pending:
                    return label
                await asyncio.sleep(0.1)
            raise ChoicePromptBlockedError(
                f"safe choice {label!r} was clicked but the prompt remained blocked"
            )

    async def wait_until_clean_ready(
        self,
        *,
        timeout_ms: int | None = None,
        poll_ms: int = 100,
        resolve_choice_prompt: bool = False,
    ) -> ChatGPTSnapshot:
        timeout = timeout_ms or self.timeout_ms
        deadline = time.monotonic() + timeout / 1000
        last_snapshot: ChatGPTSnapshot | None = None
        last_error: BaseException | None = None
        manual_pending = False
        choice_pending = False

        while time.monotonic() < deadline:
            try:
                last_snapshot = await self.assert_ownership()
            except PageOwnershipError:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(poll_ms / 1000)
                continue

            manual_pending = last_snapshot.manual_input_pending
            choice_pending = last_snapshot.choice_prompt_pending
            if rate_limit_dialogs(last_snapshot):
                await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
                await asyncio.sleep(poll_ms / 1000)
                continue
            if choice_pending and resolve_choice_prompt:
                await self.resolve_choice_prompt(timeout_ms=timeout)
                choice_pending = False
                continue
            if (
                last_snapshot.composer_present
                and last_snapshot.composer_editable
                and not last_snapshot.stop_visible
                and not manual_pending
                and not choice_pending
                and not last_snapshot.blocking_dialogs
                and last_snapshot.state is not ChatGPTState.ERROR
            ):
                return last_snapshot
            await asyncio.sleep(poll_ms / 1000)

        if manual_pending:
            raise ManualInputPendingError(
                "composer still contains manual text or attachments; automated mutation blocked"
            )
        if choice_pending:
            raise ChoicePromptBlockedError(
                f"choice prompt is still pending: {list(last_snapshot.choice_prompt_labels)!r}"
            )
        if last_error is not None:
            raise RuntimeError(
                f"clean-ready check failed after transient errors: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error
        raise TimeoutError(f"ChatGPT tab did not become clean-ready within {timeout} ms")

    async def recover_page(
        self,
        *,
        allow_new_chat: bool = False,
        resolve_choice_prompt: bool = True,
        timeout_ms: int | None = None,
    ) -> PageHealth:
        timeout = timeout_ms or self.timeout_ms
        initial = await self.health()
        if initial.action == "manual_login":
            return initial
        if initial.healthy:
            if resolve_choice_prompt:
                current = await self.assert_ownership()
                if current.choice_prompt_pending:
                    await self.resolve_choice_prompt(timeout_ms=timeout)
                    return await self.health()
            return initial

        try:
            await self.refresh(timeout_ms=timeout)
            await asyncio.sleep(0.25)
            refreshed = await self.health()
            if refreshed.healthy:
                if refreshed.state == "healthy" and resolve_choice_prompt:
                    current = await self.assert_ownership()
                    if current.choice_prompt_pending:
                        await self.resolve_choice_prompt(timeout_ms=timeout)
                return await self.health()
        except PageOwnershipError:
            raise
        except Exception:
            pass

        if allow_new_chat:
            current = await self.assert_ownership()
            if current.manual_input_pending:
                return PageHealth(
                    False,
                    "manual_input_pending",
                    "manual_clear_required",
                )
            if current.blocking_dialogs:
                return PageHealth(
                    False,
                    "blocking_dialog",
                    "manual_intervention",
                    "; ".join(current.blocking_dialogs),
                )
            try:
                await self.new_chat()
                return await self.health()
            except PageOwnershipError:
                raise
            except Exception as exc:
                return PageHealth(
                    False,
                    "recovery_failed",
                    "fresh_tab_or_rerole_required",
                    f"{type(exc).__name__}: {exc}",
                )

        final = await self.health()
        if final.healthy:
            return final
        return PageHealth(
            False,
            final.state,
            "fresh_tab_or_rerole_required",
            final.detail,
        )

    async def set_role(
        self,
        role: str,
        *,
        allow_rebind: bool = False,
        force_new_page_id: bool = False,
    ) -> dict[str, str]:
        role = validate_page_role(role)
        async with self.mutation_guard():
            if self.binding is not None and self.binding.role != role and not allow_rebind:
                raise PageOwnershipError(
                    f"tab is already bound to {self.binding.role!r}; explicit rebind required"
                )
            persisted = await self.snapshot()
            if (
                self.binding is None
                and persisted.page_id
                and persisted.page_role
                and not force_new_page_id
            ):
                persisted_role = validate_page_role(persisted.page_role)
                if persisted_role != role and not allow_rebind:
                    raise PageOwnershipError(
                        f"tab is already persistently bound to {persisted_role!r}; "
                        "explicit rebind required"
                    )
                if persisted_role == role:
                    self.binding = PageBinding(persisted.page_id, role)
                    await ensure_role_indicator(
                        self.page,
                        expected_role=role,
                        expected_page_id=persisted.page_id,
                    )
                    return {"page_id": persisted.page_id, "page_role": role}
            assigned = await assign_page_role(
                self.page, role, force_new_page_id=force_new_page_id
            )
            binding = PageBinding(
                page_id=assigned["page_id"],
                role=assigned["page_role"],
            )
            snapshot = await self.snapshot()
            if snapshot.page_id != binding.page_id or snapshot.page_role != binding.role:
                raise PageOwnershipError("role assignment did not persist exactly")
            self.binding = binding
            return assigned

    async def task_preflight(
        self,
        task_id: str,
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id must not be empty")
        if len(task_id) > 256:
            raise ValueError("task_id must be at most 256 characters")
        snapshot = await self.assert_ownership()
        current_task = snapshot.page_task_id
        if current_task == task_id:
            return {
                "task_id": task_id,
                "previous_task_id": current_task,
                "requires_new_chat": False,
            }
        if snapshot.manual_input_pending:
            raise ComposerConflictError(
                "task switch would discard manual draft or attachments"
            )
        snapshot = await self._interaction_snapshot(
            timeout_ms=timeout_ms or self.timeout_ms,
        )
        current_task = snapshot.page_task_id
        return {
            "task_id": task_id,
            "previous_task_id": current_task,
            "requires_new_chat": True,
        }

    async def restore_identity(
        self,
        *,
        page_id: str,
        role: str,
        task_id: str,
        team: str,
    ) -> ChatGPTSnapshot:
        """Restore an exact closed-tab identity on its reopened conversation page."""
        page_id = str(page_id).strip()
        role = validate_page_role(role)
        task_id = str(task_id).strip()
        team = str(team).strip()
        if not page_id or len(page_id) > 256:
            raise ValueError("page_id must contain 1-256 characters")
        if not task_id or len(task_id) > 256:
            raise ValueError("task_id must contain 1-256 characters")
        if not _TEAM_PATTERN.fullmatch(team):
            raise ValueError("team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        async with self.mutation_guard():
            await self.page.evaluate(
                """([roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix,
                       role, pageId, taskId, team]) => {
                  sessionStorage.setItem(roleKey, role);
                  sessionStorage.setItem(pageIdKey, pageId);
                  sessionStorage.setItem(taskIdKey, taskId);
                  sessionStorage.setItem(teamKey, team);
                  window.name = windowNamePrefix + JSON.stringify({
                    role,
                    pageId,
                    taskId,
                    team,
                  });
                }""",
                [
                    ROLE_STORAGE_KEY,
                    PAGE_ID_STORAGE_KEY,
                    TASK_ID_STORAGE_KEY,
                    TEAM_STORAGE_KEY,
                    WINDOW_NAME_PREFIX,
                    role,
                    page_id,
                    task_id,
                    team,
                ],
            )
            self.binding = PageBinding(page_id, role)
            await ensure_role_indicator(
                self.page,
                expected_role=role,
                expected_page_id=page_id,
                expected_task_id=task_id,
                expected_team=team,
            )
            confirmed = await self.assert_ownership()
            if confirmed.page_task_id != task_id or confirmed.page_team != team:
                raise TaskBindingError("restored task/team identity did not persist exactly")
            return confirmed

    async def bind_task_identity(
        self,
        task_id: str,
        team: str,
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, str]:
        task_id = str(task_id).strip()
        team = str(team).strip()
        if not task_id or len(task_id) > 256:
            raise ValueError("task_id must contain 1-256 characters")
        if not _TEAM_PATTERN.fullmatch(team):
            raise ValueError("team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if snapshot.manual_input_pending:
                raise ComposerConflictError(
                    "task/team binding blocked by manual draft or attachments"
                )
            self._assert_interaction_safe(snapshot)
            write_result = await self.page.evaluate(
                """([taskIdKey, taskId, teamKey, team, windowNamePrefix]) => {
                  const visible = (element) => Boolean(
                    element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                  );
                  const firstVisible = (selector) =>
                    [...document.querySelectorAll(selector)].find(visible) || null;
                  const composer = firstVisible('[contenteditable="true"][role="textbox"]');
                  if (!composer || composer.getAttribute('contenteditable') !== 'true' ||
                      composer.getAttribute('aria-disabled') === 'true') {
                    return {written: false, reason: 'composer_unavailable'};
                  }
                  if (firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]')) {
                    return {written: false, reason: 'active_response'};
                  }
                  if (firstVisible('[role="dialog"], [data-testid^="modal-"]')) {
                    return {written: false, reason: 'blocking_dialog'};
                  }
                  const errorTexts = [...document.querySelectorAll('[role="alert"]')]
                    .filter(visible)
                    .map((element) => element.innerText || '')
                    .filter((value) => /error|failed|issue|try again/i.test(value));
                  if (firstVisible('[data-testid="regenerate-thread-error-button"]') || errorTexts.length) {
                    return {written: false, reason: 'error_state'};
                  }
                  const composerHost = composer.closest('form') || composer.parentElement;
                  const attachment = composerHost && [...composerHost.querySelectorAll(
                    '[data-testid*="attachment"], [data-testid*="file"], button[aria-label], [role="button"][aria-label]'
                  )].filter(visible).some((element) => {
                    const label = [
                      element.innerText || '',
                      element.getAttribute?.('aria-label') || '',
                      element.getAttribute?.('data-testid') || '',
                    ].join(' ').toLowerCase();
                    return label && !label.includes('composer-plus-btn') &&
                      !label.includes('add files and more') && [
                        'remove file', 'open image', 'attached', 'file uploaded',
                        'uploading', 'remove attachment'
                      ].some((marker) => label.includes(marker));
                  });
                  if ((composer.innerText || '').trim() || attachment) {
                    return {written: false, reason: 'manual_input_pending'};
                  }
                  sessionStorage.setItem(taskIdKey, taskId);
                  sessionStorage.setItem(teamKey, team);
                  let binding = {};
                  if (window.name?.startsWith(windowNamePrefix)) {
                    try {
                      binding = JSON.parse(window.name.slice(windowNamePrefix.length)) || {};
                    } catch (_) {
                      binding = {};
                    }
                  }
                  window.name = windowNamePrefix + JSON.stringify({
                    role: binding.role || sessionStorage.getItem('playwright-auto:role'),
                    pageId: binding.pageId || sessionStorage.getItem('playwright-auto:page-id'),
                    taskId,
                    team,
                  });
                  return {written: true};
                }""",
                [
                    TASK_ID_STORAGE_KEY,
                    task_id,
                    TEAM_STORAGE_KEY,
                    team,
                    WINDOW_NAME_PREFIX,
                ],
            )
            reason = str((write_result or {}).get("reason") or "")
            if reason == "manual_input_pending":
                raise ComposerConflictError(
                    "task/team binding blocked by manual draft or attachments"
                )
            if reason == "active_response":
                raise UnsafePageStateError("page is already responding")
            if reason == "blocking_dialog":
                raise UnsafePageStateError("blocking dialog is open")
            if reason == "error_state":
                raise UnsafePageStateError("page is in error state")
            if reason == "composer_unavailable":
                raise UnsafePageStateError("composer is not present and editable")
            if not bool((write_result or {}).get("written")):
                raise TaskBindingError("task/team binding was not written")
            await ensure_role_indicator(
                self.page,
                expected_role=self.binding.role if self.binding else None,
                expected_page_id=self.binding.page_id if self.binding else None,
                expected_task_id=task_id,
                expected_team=team,
            )
            confirmed = await self.wait_until_clean_ready(
                timeout_ms=timeout,
                poll_ms=100,
            )
            if confirmed.page_task_id != task_id or confirmed.page_team != team:
                raise TaskBindingError("task/team binding did not persist exactly")
            return {"task_id": task_id, "team": team}

    async def prepare_task(
        self,
        task_id: str,
        *,
        force_new_chat: bool = False,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        task_id = str(task_id).strip()
        if not task_id:
            raise ValueError("task_id must not be empty")
        if len(task_id) > 256:
            raise ValueError("task_id must be at most 256 characters")
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if rate_limit_dialogs(snapshot):
                await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
                snapshot = await self.assert_ownership()
            current_task = snapshot.page_task_id
            if current_task == task_id and not force_new_chat:
                await ensure_role_indicator(
                    self.page,
                    expected_role=self.binding.role if self.binding else None,
                    expected_page_id=self.binding.page_id if self.binding else None,
                    expected_task_id=task_id,
                )
                return {
                    "task_id": task_id,
                    "previous_task_id": current_task,
                    "reused": True,
                    "new_chat_method": None,
                }

            if snapshot.stop_visible:
                raise UnsafePageStateError(
                    "response is active; refusing to switch task until it is stopped"
                )
            if snapshot.blocking_dialogs:
                limited = rate_limit_dialogs(snapshot)
                if limited:
                    raise RateLimitBlockedError(
                        f"request rate limit is active: {list(limited)!r}"
                    )
                raise UnsafePageStateError(
                    f"blocking dialog is open: {list(snapshot.blocking_dialogs)!r}"
                )
            if snapshot.manual_input_pending:
                raise ComposerConflictError(
                    "task switch would discard manual draft or attachments"
                )
            if not snapshot.composer_present or not snapshot.composer_editable:
                raise UnsafePageStateError("composer is not present and editable")

            method = await open_new_chat(self.page, timeout_ms=timeout)
            post = await self._interaction_snapshot(timeout_ms=timeout)
            if not post.composer_empty:
                raise UnsafePageStateError("task switch did not produce an empty composer")
            await self.page.evaluate(
                """([taskIdKey, value, windowNamePrefix]) => {
                  sessionStorage.setItem(taskIdKey, value);
                  let binding = {};
                  if (window.name?.startsWith(windowNamePrefix)) {
                    try {
                      binding = JSON.parse(window.name.slice(windowNamePrefix.length)) || {};
                    } catch (_) {
                      binding = {};
                    }
                  }
                  window.name = windowNamePrefix + JSON.stringify({
                    role: binding.role || sessionStorage.getItem('playwright-auto:role'),
                    pageId: binding.pageId || sessionStorage.getItem('playwright-auto:page-id'),
                    taskId: value,
                    team: binding.team || sessionStorage.getItem('playwright-auto:team') || null,
                  });
                }""",
                [TASK_ID_STORAGE_KEY, task_id, WINDOW_NAME_PREFIX],
            )
            await ensure_role_indicator(
                self.page,
                expected_role=self.binding.role if self.binding else None,
                expected_page_id=self.binding.page_id if self.binding else None,
                expected_task_id=task_id,
            )
            confirmed = await self.wait_until_clean_ready(
                timeout_ms=timeout,
                poll_ms=100,
            )
            if confirmed.page_task_id != task_id:
                raise TaskBindingError("task binding did not persist exactly")
            self._owned_composer_text = None
            return {
                "task_id": task_id,
                "previous_task_id": current_task,
                "reused": False,
                "new_chat_method": method,
            }

    @staticmethod
    def _assert_interaction_safe(
        snapshot: ChatGPTSnapshot,
        *,
        require_composer: bool = True,
        allow_responding: bool = False,
        allow_dialogs: bool = False,
        allow_attachments: bool = False,
    ) -> None:
        if snapshot.state is ChatGPTState.ERROR:
            raise UnsafePageStateError(
                f"page is in error state: {list(snapshot.error_texts)!r}"
            )
        if snapshot.stop_visible and not allow_responding:
            raise UnsafePageStateError("page is already responding")
        if snapshot.blocking_dialogs and not allow_dialogs:
            limited = rate_limit_dialogs(snapshot)
            if limited:
                raise RateLimitBlockedError(
                    f"request rate limit is active: {list(limited)!r}"
                )
            raise UnsafePageStateError(
                f"blocking dialog is open: {list(snapshot.blocking_dialogs)!r}"
            )
        if snapshot.attachment_markers and not allow_attachments:
            raise ComposerConflictError(
                "manual/unowned attachments are present; refusing to mutate composer"
            )
        if require_composer and (
            not snapshot.composer_present or not snapshot.composer_editable
        ):
            raise UnsafePageStateError("composer is not present and editable")

    async def _interaction_snapshot(
        self,
        *,
        timeout_ms: int,
        allow_attachments: bool = False,
    ) -> ChatGPTSnapshot:
        snapshot = await self.assert_ownership()
        if rate_limit_dialogs(snapshot):
            await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout_ms, 10_000))
            snapshot = await self.assert_ownership()
        if snapshot.composer_present and snapshot.composer_editable:
            self._assert_interaction_safe(
                snapshot,
                allow_attachments=allow_attachments,
            )
            return snapshot
        self._assert_interaction_safe(
            snapshot,
            require_composer=False,
            allow_attachments=allow_attachments,
        )
        snapshot = await self.wait_until_clean_ready(
            timeout_ms=timeout_ms,
            poll_ms=100,
        )
        self._assert_interaction_safe(
            snapshot,
            allow_attachments=allow_attachments,
        )
        return snapshot

    async def set_text(
        self,
        text: str,
        *,
        overwrite: bool = False,
        expected_existing: str | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        expected = text.strip()
        if not expected:
            await self.clear(
                force=overwrite,
                expected_text=expected_existing,
                timeout_ms=timeout_ms,
            )
            return
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self._interaction_snapshot(timeout_ms=timeout)
            existing = snapshot.composer_text.strip()
            if existing and not visible_text_matches(existing, expected):
                permitted = overwrite or (
                    expected_existing is not None
                    and visible_text_matches(existing, expected_existing)
                )
                if not permitted:
                    raise ComposerConflictError(
                        "composer contains unowned/manual input; refusing to overwrite"
                    )
            if not visible_text_matches(existing, expected):
                await set_composer_text(self.page, expected, timeout_ms=timeout)
            post = await self._interaction_snapshot(timeout_ms=timeout)
            if not visible_text_matches(post.composer_text, expected):
                raise ComposerConflictError("composer did not contain the exact requested text")
            self._owned_composer_text = expected

    async def clear(
        self,
        *,
        force: bool = False,
        expected_text: str | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self._interaction_snapshot(timeout_ms=timeout)
            existing = snapshot.composer_text.strip()
            if not existing:
                self._owned_composer_text = None
                return
            if expected_text is not None and not visible_text_matches(existing, expected_text):
                raise ComposerConflictError(
                    "composer no longer matches the text expected by this workflow"
                )
            if not force and not visible_text_matches(existing, self._owned_composer_text):
                raise ComposerConflictError(
                    "composer contains unowned/manual input; refusing to clear"
                )
            await clear_composer(self.page, timeout_ms=timeout)
            post = await self.assert_ownership()
            if not post.composer_empty:
                raise ComposerConflictError("composer did not become empty")
            self._owned_composer_text = None

    async def new_chat(
        self,
        *,
        discard_draft: bool = False,
        expected_draft_text: str | None = None,
        discard_attachments: bool = False,
        stop_first: bool = False,
        timeout_ms: int | None = None,
    ) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if rate_limit_dialogs(snapshot):
                await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
                snapshot = await self.assert_ownership()
            if snapshot.stop_visible:
                if not stop_first:
                    raise UnsafePageStateError(
                        "response is active; stop it explicitly before New chat"
                    )
                await stop_response(self.page, timeout_ms=timeout)
                snapshot = await self.assert_ownership()
                if rate_limit_dialogs(snapshot):
                    await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
                    snapshot = await self.assert_ownership()
            if snapshot.blocking_dialogs:
                limited = rate_limit_dialogs(snapshot)
                if limited:
                    raise RateLimitBlockedError(
                        f"request rate limit is active: {list(limited)!r}"
                    )
                raise UnsafePageStateError(
                    f"blocking dialog is open: {list(snapshot.blocking_dialogs)!r}"
                )
            if snapshot.attachment_markers and not discard_attachments:
                raise ComposerConflictError(
                    "New chat would discard manual/unowned attachments; "
                    "set discard_attachments=True explicitly"
                )
            actual_draft = normalize_visible_text(snapshot.composer_text)
            if expected_draft_text is not None:
                expected_draft = normalize_visible_text(expected_draft_text)
                if actual_draft != expected_draft:
                    raise ComposerConflictError(
                        "New chat draft does not match the exact automated prompt provenance"
                    )
                discard_draft = True
            if actual_draft and not discard_draft:
                raise ComposerConflictError(
                    "New chat would discard a draft; set discard_draft=True explicitly"
                )
            method = await open_new_chat(self.page, timeout_ms=timeout)
            post = await self.wait_until_clean_ready(
                timeout_ms=timeout,
                poll_ms=100,
            )
            if not post.composer_empty:
                raise UnsafePageStateError("New chat did not produce an empty composer")
            self._owned_composer_text = None
            return method

    async def open_delete_dialog(self, *, timeout_ms: int | None = None) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if snapshot.stop_visible:
                raise UnsafePageStateError("response is active; Stop before deleting chat")
            if snapshot.composer_text.strip() or snapshot.attachment_markers:
                raise ComposerConflictError(
                    "deleting this chat would discard manual draft or attachments"
                )
            return await open_delete_chat_dialog(self.page, timeout_ms=timeout)

    async def confirm_delete(self, *, timeout_ms: int | None = None) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            await self.assert_ownership()
            return await confirm_delete_chat(self.page, timeout_ms=timeout)

    async def delete_chat(self, *, timeout_ms: int | None = None) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if snapshot.stop_visible:
                raise UnsafePageStateError("response is active; Stop before deleting chat")
            if snapshot.composer_text.strip() or snapshot.attachment_markers:
                raise ComposerConflictError(
                    "deleting this chat would discard manual draft or attachments"
                )
            await open_delete_chat_dialog(self.page, timeout_ms=timeout)
            return await confirm_delete_chat(self.page, timeout_ms=timeout)

    async def refresh(
        self,
        *,
        allow_responding: bool = True,
        timeout_ms: int | None = None,
    ) -> None:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            before = await self.assert_ownership()
            if before.stop_visible and not allow_responding:
                raise UnsafePageStateError("refusing to refresh an active response")
            await refresh_page(self.page, timeout_ms=timeout)
            await self.assert_ownership()
            self._owned_composer_text = None

    async def _wait_send_acceptance(
        self,
        baseline: MessageBaseline,
        prompt: str,
        *,
        timeout_ms: int,
    ) -> tuple[str, MessageSnapshot | None] | None:
        deadline = time.monotonic() + timeout_ms / 1000
        stop_seen = False
        while time.monotonic() < deadline:
            snapshot = await self.assert_ownership()
            candidate = unique_new_user_message(snapshot.messages, baseline)
            if candidate is not None:
                signal = (
                    "exact_user_message"
                    if normalize_visible_text(candidate.text) == normalize_visible_text(prompt)
                    else "user_message_identity"
                )
                return signal, candidate
            stop_seen = stop_seen or snapshot.stop_visible
            await asyncio.sleep(0.05)
        if stop_seen:
            return "stop_button", None
        return None

    async def _prepare_prompt_locked(
        self,
        prompt: str,
        timeout_ms: int,
        *,
        expected_attachment_count: int = 0,
        expected_attachment_names: Sequence[str] | None = None,
    ) -> None:
        expected_names, expected_count = _expected_attachment_contract(
            expected_attachment_names, expected_attachment_count
        )
        snapshot = await self._interaction_snapshot(
            timeout_ms=timeout_ms,
            allow_attachments=expected_count > 0,
        )
        _assert_expected_attachment_markers(
            snapshot.attachment_markers,
            expected_names=expected_names,
            expected_count=expected_count,
            message="attachment set does not match before send",
        )
        existing = snapshot.composer_text.strip()
        if existing and not visible_text_matches(existing, prompt):
            raise ComposerConflictError(
                "composer changed or contains manual input before send"
            )
        if not visible_text_matches(existing, prompt):
            await set_composer_text(self.page, prompt, timeout_ms=timeout_ms)
        fresh = await self._interaction_snapshot(
            timeout_ms=timeout_ms,
            allow_attachments=expected_count > 0,
        )
        _assert_expected_attachment_markers(
            fresh.attachment_markers,
            expected_names=expected_names,
            expected_count=expected_count,
            message="attachment set changed before send",
        )
        if not visible_text_matches(fresh.composer_text, prompt):
            raise ComposerConflictError("exact prompt ownership was lost before send")
        if not fresh.send_visible or not fresh.send_enabled:
            raise UnsafePageStateError("Send button is not visible and enabled")
        self._owned_composer_text = prompt

    async def send(
        self,
        text: str,
        *,
        wait_for_stop: bool = True,
        timeout_ms: int | None = None,
        max_attempts: int = 2,
        recovery_reload: bool = True,
        expected_task_id: str | None = None,
        expected_team: str | None = None,
        expected_attachment_ownership_token: str | None = None,
        expected_attachment_count: int = 0,
        expected_attachment_names: Sequence[str] | None = None,
        require_existing_conversation_baseline: bool = False,
    ) -> SendReceipt:
        prompt = text.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        if (expected_task_id is None) != (expected_team is None):
            raise ValueError("expected task ID and team must be provided together")
        explicit_task_id = str(expected_task_id or "").strip() or None
        explicit_team = str(expected_team or "").strip() or None
        explicit_attachment_token = (
            str(expected_attachment_ownership_token or "").strip() or None
        )
        if expected_task_id is not None and (
            explicit_task_id is None or len(explicit_task_id) > 256
        ):
            raise ValueError("expected task ID must contain 1-256 characters")
        if expected_team is not None and (
            explicit_team is None or not _TEAM_PATTERN.fullmatch(explicit_team)
        ):
            raise ValueError("expected team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        expected_names, expected_count = _expected_attachment_contract(
            expected_attachment_names, expected_attachment_count
        )
        if expected_count > 0 and explicit_attachment_token is None:
            raise ComposerConflictError(
                "attachment send requires an exact live ownership token"
            )
        if expected_count == 0 and explicit_attachment_token is not None:
            raise ValueError(
                "attachment ownership token requires expected attachments"
            )
        timeout = timeout_ms or self.timeout_ms

        async with self.mutation_guard():
            before = await self.assert_ownership()
            if require_existing_conversation_baseline:
                try:
                    before = await wait_for_existing_conversation_messages(
                        before,
                        self.assert_ownership,
                    )
                except ConversationTranscriptNotReadyError as exc:
                    raise UnsafePageStateError(
                        "page state changed inside the atomic send boundary: "
                        "existing conversation transcript is not hydrated before send"
                    ) from exc
            self._assert_interaction_safe(
                before, allow_attachments=expected_count > 0
            )
            if (before.page_task_id is None) != (before.page_team is None):
                raise TaskBindingError(
                    "task/team ownership evidence is incomplete before send"
                )
            owned_task_id = before.page_task_id
            owned_team = before.page_team
            if explicit_task_id is not None:
                if owned_task_id != explicit_task_id or owned_team != explicit_team:
                    raise TaskBindingError(
                        "exact task/team ownership does not match before send"
                    )
                owned_task_id = explicit_task_id
                owned_team = explicit_team
            assert self.binding is not None
            baseline = capture_message_baseline(before.messages)
            last_error: BaseException | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    await self._prepare_prompt_locked(
                        prompt,
                        timeout,
                        expected_attachment_count=expected_count,
                        expected_attachment_names=expected_names,
                    )
                    # Passive network identity observation is armed around this same click.
                    self._arm_frontend_identity_observer()
                    # Ownership, safe page state, prompt, attachments, and click share one callback.
                    await click_send_button(
                        self.page,
                        timeout_ms=timeout,
                        expected_url=before.url,
                        expected_page_id=self.binding.page_id,
                        expected_role=self.binding.role,
                        expected_task_id=owned_task_id,
                        expected_team=owned_team,
                        expected_prompt=prompt,
                        expected_attachment_ownership_token=explicit_attachment_token,
                        expected_attachment_count=expected_count,
                        expected_attachment_names=expected_names,
                    )
                    self._owned_composer_text = None
                    acceptance = await self._wait_send_acceptance(
                        baseline,
                        prompt,
                        timeout_ms=min(timeout, 2_000),
                    )
                    if acceptance is None:
                        raise SendRecoveryError(
                            "send click produced no accepted user-message or stop evidence"
                        )
                    accepted_via, accepted_user = acceptance
                    if wait_for_stop and accepted_via != "stop_button":
                        # A completed very-fast response is also valid if exact provenance exists.
                        snapshot = await self.assert_ownership()
                        if not new_assistant_turns(snapshot.messages, baseline):
                            await self.page.locator(SELECTORS["stop"]).wait_for(
                                state="visible", timeout=timeout
                            )
                    return SendReceipt(
                        prompt=prompt,
                        prompt_sha256=prompt_digest(prompt),
                        binding=self.binding,
                        baseline=baseline,
                        attempts=attempt,
                        accepted_via=accepted_via,
                        session_id_before=before.session_id,
                        user_message_id=(accepted_user.message_id if accepted_user else None),
                        user_turn_id=(accepted_user.turn_id if accepted_user else None),
                        conversation_id=self._captured_conversation_id(
                            accepted_user.message_id if accepted_user else None
                        ),
                    )
                except (ComposerConflictError, PageOwnershipError, UnsafePageStateError):
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= max_attempts or not recovery_reload:
                        break
                    await refresh_page(self.page, timeout_ms=timeout)
                    recovered = await self.assert_ownership()
                    acceptance = await self._wait_send_acceptance(
                        baseline,
                        prompt,
                        timeout_ms=min(timeout, 1_500),
                    )
                    if acceptance is not None:
                        accepted_via, accepted_user = acceptance
                        return SendReceipt(
                            prompt=prompt,
                            prompt_sha256=prompt_digest(prompt),
                            binding=self.binding,
                            baseline=baseline,
                            attempts=attempt,
                            accepted_via=f"post_reload:{accepted_via}",
                            session_id_before=before.session_id,
                            user_message_id=(accepted_user.message_id if accepted_user else None),
                            user_turn_id=(accepted_user.turn_id if accepted_user else None),
                            conversation_id=self._captured_conversation_id(
                                accepted_user.message_id if accepted_user else None
                            ),
                        )
                    recovered_text = normalize_visible_text(recovered.composer_text)
                    if recovered_text not in {"", normalize_visible_text(prompt)}:
                        raise ComposerConflictError(
                            "manual/unowned composer input appeared during send recovery"
                        ) from exc
                    try:
                        _assert_expected_attachment_markers(
                            recovered.attachment_markers,
                            expected_names=expected_names,
                            expected_count=expected_count,
                            message="attachment set changed during send recovery",
                        )
                    except ComposerConflictError as conflict:
                        raise conflict from exc

            raise SendRecoveryError(
                f"send failed after {max_attempts} exact attempt(s): "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error

    async def retry_generation(
        self,
        receipt: SendReceipt,
        *,
        expected_task_id: str,
        expected_team: str,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Retry one already accepted user turn and prove new transport progress."""
        if self.binding != receipt.binding:
            raise PageOwnershipError("receipt belongs to another physical/logical tab")
        task_id = str(expected_task_id or "").strip()
        team = str(expected_team or "").strip()
        if not task_id or not team:
            raise ValueError("expected task ID and team are required")
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            before = await self.assert_ownership()
            if before.page_task_id != task_id or before.page_team != team:
                raise TaskBindingError("exact task/team ownership does not match before Retry")
            if before.composer_text.strip() or before.attachment_markers:
                raise ComposerConflictError(
                    "Retry generation blocked by manual draft or attachments"
                )
            if before.blocking_dialogs:
                raise UnsafePageStateError("Retry generation blocked by a dialog")
            if not receipt_user_message_seen(before.messages, receipt):
                raise SendRecoveryError(
                    "Retry generation requires the exact accepted user turn"
                )
            before_assistants = new_assistant_turns(before.messages, receipt.baseline)
            before_assistant_fingerprint = message_fingerprint(
                before_assistants[-1] if before_assistants else None
            )
            before_activity = (
                str(before.response_activity_turn_id or ""),
                str(before.response_activity_text or ""),
                str(before.response_activity_structure or ""),
                int(before.response_activity_length or 0),
            )
            click_result = await self.page.evaluate(
                r"""([roleKey, pageIdKey, taskIdKey, teamKey, expectedRole,
                       expectedPageId, expectedTaskId, expectedTeam]) => {
                  const visible = (element) => Boolean(
                    element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                  );
                  const composer = [...document.querySelectorAll(
                    '[contenteditable="true"][role="textbox"]'
                  )].find(visible) || null;
                  const composerText = (composer?.innerText || '').replace(/\s+/g, ' ').trim();
                  const composerHost = composer?.closest('form') || composer?.parentElement || null;
                  const attachments = composerHost ? [...composerHost.querySelectorAll(
                    '[data-filename], [data-file-name], [data-testid*=attachment], [data-testid*=file]'
                  )].filter(visible) : [];
                  if (sessionStorage.getItem(roleKey) !== expectedRole ||
                      sessionStorage.getItem(pageIdKey) !== expectedPageId ||
                      sessionStorage.getItem(taskIdKey) !== expectedTaskId ||
                      sessionStorage.getItem(teamKey) !== expectedTeam) {
                    return {clicked: false, reason: 'ownership_changed'};
                  }
                  if (composerText || attachments.length) {
                    return {clicked: false, reason: 'composer_conflict'};
                  }
                  if ([...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')].some(visible)) {
                    return {clicked: false, reason: 'blocking_dialog'};
                  }
                  const retry = [...document.querySelectorAll(
                    '[data-testid="regenerate-thread-error-button"]'
                  )].find(visible) || null;
                  if (!retry || retry.disabled || retry.getAttribute('aria-disabled') === 'true') {
                    return {clicked: false, reason: 'retry_unavailable'};
                  }
                  retry.click();
                  return {clicked: true, reason: null};
                }""",
                [
                    ROLE_STORAGE_KEY,
                    PAGE_ID_STORAGE_KEY,
                    TASK_ID_STORAGE_KEY,
                    TEAM_STORAGE_KEY,
                    receipt.binding.role,
                    receipt.binding.page_id,
                    task_id,
                    team,
                ],
            )
            if not isinstance(click_result, Mapping) or not click_result.get("clicked"):
                reason = (
                    str(click_result.get("reason") or "retry_unavailable")
                    if isinstance(click_result, Mapping)
                    else "retry_unavailable"
                )
                if reason == "composer_conflict":
                    raise ComposerConflictError(
                        "Retry generation blocked by manual draft or attachments"
                    )
                if reason == "ownership_changed":
                    raise PageOwnershipError("exact ownership changed before Retry click")
                raise UnsafePageStateError(f"Retry generation was not available: {reason}")

            deadline = time.monotonic() + timeout / 1000
            while time.monotonic() < deadline:
                current = await self.assert_ownership()
                if current.page_task_id != task_id or current.page_team != team:
                    raise TaskBindingError(
                        "exact task/team ownership changed after Retry click"
                    )
                if current.composer_text.strip() or current.attachment_markers:
                    raise ComposerConflictError(
                        "manual draft or attachments appeared after Retry click"
                    )
                if not receipt_user_message_seen(current.messages, receipt):
                    raise SendRecoveryError(
                        "accepted user-turn identity disappeared after Retry click"
                    )
                assistants = new_assistant_turns(current.messages, receipt.baseline)
                assistant_fingerprint = message_fingerprint(
                    assistants[-1] if assistants else None
                )
                activity = (
                    str(current.response_activity_turn_id or ""),
                    str(current.response_activity_text or ""),
                    str(current.response_activity_structure or ""),
                    int(current.response_activity_length or 0),
                )
                assistant_progress = bool(assistant_fingerprint) and (
                    assistant_fingerprint != before_assistant_fingerprint
                )
                activity_progress = any(activity) and activity != before_activity
                transport_active = bool(
                    current.stop_visible or current.response_activity_turn_id
                )
                if transport_active or assistant_progress or activity_progress:
                    self.invalidate_wait_cache()
                    return {
                        "progress": True,
                        "transport_active": transport_active,
                    }
                await asyncio.sleep(0.05)
        raise TimeoutError("Retry generation produced no verified progress")

    @staticmethod
    def _adaptive_wait_seconds(poll_ms: int, unchanged_ticks: int) -> float:
        base = poll_ms / 1000
        if unchanged_ticks < 4:
            return base
        return min(
            max(0.5, base * 2.0),
            base * min(5.0, 1.0 + (unchanged_ticks - 3) * 0.5),
        )

    async def wait_for_response(
        self,
        receipt: SendReceipt,
        *,
        timeout_ms: int | None = None,
        stable_ms: int = 1_000,
        poll_ms: int = 100,
        active_reload_after_ms: int | None = None,
        reload_wait_ms: int = 750,
        skeptical_after_reload: bool = True,
        resolve_choice_prompt: bool = False,
        stale_response_baseline: Mapping[str, Any] | None = None,
        candidate_validator: Callable[[MessageSnapshot], None] | None = None,
        minimum_samples: int = 1,
        invalid_grace_ms: int | None = None,
        expected_assistant_turn_id: str | None = None,
        expected_assistant_message_id: str | None = None,
    ) -> MessageSnapshot:
        timeout = timeout_ms or self.timeout_ms
        if self.binding != receipt.binding:
            raise PageOwnershipError("receipt belongs to another physical/logical tab")
        if poll_ms <= 0:
            raise ValueError("poll_ms must be positive")
        if stable_ms < 0:
            raise ValueError("stable_ms must not be negative")
        if minimum_samples < 1:
            raise ValueError("minimum_samples must be at least one")
        malformed_grace_ms = stable_ms if invalid_grace_ms is None else invalid_grace_ms
        if malformed_grace_ms < 0:
            raise ValueError("invalid_grace_ms must not be negative")
        if active_reload_after_ms is not None and active_reload_after_ms <= 0:
            raise ValueError("active_reload_after_ms must be positive or None")

        deadline = time.monotonic() + timeout / 1000
        candidate_fingerprint = ""
        candidate_since: float | None = None
        candidate_samples = 0
        activity_fingerprint = ""
        activity_since: float | None = None
        activity_samples = 0
        first_recovered_fingerprint = ""
        active_since: float | None = None
        reload_used = receipt.accepted_via.startswith("post_reload:")
        recovery_suspected = reload_used and skeptical_after_reload
        saw_incomplete_response = False
        saw_stale_assistant = False
        recovery_baseline = merge_response_recovery_baselines(stale_response_baseline)
        manual_input_pending = False
        choice_prompt_pending = False
        last_snapshot: ChatGPTSnapshot | None = None
        last_error: BaseException | None = None
        unchanged_ticks = 0

        while time.monotonic() < deadline:
            try:
                snapshot = await self.wait_snapshot(receipt)
            except PageOwnershipError:
                raise
            except Exception as exc:
                last_error = exc
                unchanged_ticks += 1
                await asyncio.sleep(self._adaptive_wait_seconds(poll_ms, unchanged_ticks))
                continue

            now = time.monotonic()
            last_snapshot = snapshot
            limited = rate_limit_dialogs(snapshot)
            if limited:
                await dismiss_rate_limit_dialog(self.page, timeout_ms=min(timeout, 10_000))
                unchanged_ticks = 0
                continue
            manual_input_pending = snapshot.manual_input_pending
            choice_prompt_pending = snapshot.choice_prompt_pending

            current_activity, _activity_length = response_activity_signature(
                snapshot,
                receipt.baseline,
            )
            if current_activity != activity_fingerprint:
                activity_fingerprint = current_activity
                activity_since = now
                activity_samples = 1
                unchanged_ticks = 0
            else:
                activity_samples += 1
                unchanged_ticks += 1

            if choice_prompt_pending:
                if resolve_choice_prompt:
                    await self.resolve_choice_prompt(timeout_ms=min(timeout, 10_000))
                    choice_prompt_pending = False
                    candidate_fingerprint = ""
                    candidate_since = None
                    candidate_samples = 0
                    continue
                await asyncio.sleep(self._adaptive_wait_seconds(poll_ms, unchanged_ticks))
                continue

            if manual_input_pending:
                await asyncio.sleep(self._adaptive_wait_seconds(poll_ms, unchanged_ticks))
                continue

            user_provenance = receipt_user_message_seen(snapshot.messages, receipt)
            assistants = assistant_turns_for_receipt(snapshot.messages, receipt)
            expected_turn = str(expected_assistant_turn_id or "").strip()
            expected_message = str(expected_assistant_message_id or "").strip()
            if expected_turn or expected_message:
                exact_assistants = all_assistant_turns_for_receipt(
                    snapshot.messages, receipt
                )
            else:
                exact_assistants = assistants
            if expected_turn:
                candidate = next(
                    (item for item in exact_assistants if item.turn_id == expected_turn),
                    None,
                )
            elif expected_message:
                candidate = next(
                    (item for item in exact_assistants if item.message_id == expected_message),
                    None,
                )
            else:
                candidate = assistants[-1] if assistants else None
            current_fingerprint = message_fingerprint(candidate)
            transport_ui_active = response_transport_ui_active(snapshot)

            candidate_present = candidate is not None and user_provenance
            if candidate_present and response_is_stale(candidate, recovery_baseline):
                saw_stale_assistant = True
                candidate_present = False

            if candidate_present and candidate is not None:
                if current_fingerprint != candidate_fingerprint:
                    candidate_fingerprint = current_fingerprint
                    candidate_since = now
                    candidate_samples = 1
                else:
                    candidate_samples += 1

                stable_elapsed_ms = (
                    (now - candidate_since) * 1000
                    if candidate_since is not None
                    else 0
                )
                time_stable = stable_ms == 0 or stable_elapsed_ms >= stable_ms
                incomplete = (
                    candidate.image_count == 0
                    and looks_incomplete_response(candidate.text)
                )
                if incomplete:
                    saw_incomplete_response = True
                validation_error: BaseException | None = None
                if not incomplete and candidate_validator is not None:
                    try:
                        candidate_validator(candidate)
                    except Exception as exc:
                        validation_error = exc

                if candidate_validator is None and minimum_samples == 1:
                    required_samples = 2 if snapshot.stop_visible else (
                        1 if stable_ms == 0 else 2
                    )
                else:
                    required_samples = minimum_samples
                if recovery_suspected:
                    if not first_recovered_fingerprint:
                        first_recovered_fingerprint = current_fingerprint
                    progressed_after_recovery = (
                        current_fingerprint != first_recovered_fingerprint
                    )
                    confirmed = progressed_after_recovery or candidate_samples >= max(
                        2,
                        required_samples,
                    )
                else:
                    confirmed = candidate_samples >= required_samples

                if (
                    not incomplete
                    and validation_error is None
                    and not transport_ui_active
                    and time_stable
                    and confirmed
                ):
                    return candidate

                if (
                    not incomplete
                    and validation_error is not None
                    and not snapshot.stop_visible
                    and not transport_ui_active
                ):
                    activity_elapsed_ms = (
                        (now - activity_since) * 1000
                        if activity_since is not None
                        else 0
                    )
                    if (
                        activity_samples >= 2
                        and candidate_samples >= 2
                        and activity_elapsed_ms >= malformed_grace_ms
                    ):
                        raise StableMalformedResponseError(candidate, validation_error)
            else:
                candidate_fingerprint = ""
                candidate_since = None
                candidate_samples = 0

            if snapshot.stop_visible:
                if active_since is None:
                    active_since = now
                if (
                    active_reload_after_ms is not None
                    and not reload_used
                    and (now - active_since) * 1000 >= active_reload_after_ms
                ):
                    recovery_baseline = merge_response_recovery_baselines(
                        recovery_baseline,
                        capture_response_recovery_baseline(
                            snapshot.messages,
                            receipt.baseline,
                        ),
                    )
                    await refresh_page(self.page, timeout_ms=timeout)
                    await self.wait_snapshot(receipt, force_full=True)
                    reload_used = True
                    recovery_suspected = skeptical_after_reload
                    candidate_fingerprint = ""
                    candidate_since = None
                    candidate_samples = 0
                    activity_fingerprint = ""
                    activity_since = None
                    activity_samples = 0
                    if reload_wait_ms:
                        await asyncio.sleep(reload_wait_ms / 1000)
                    active_since = None
                    continue
            else:
                active_since = None

            await asyncio.sleep(self._adaptive_wait_seconds(poll_ms, unchanged_ticks))

        if manual_input_pending:
            raise ManualInputPendingError(
                "manual composer text or attachments remained while waiting for response"
            )
        if choice_prompt_pending:
            labels = list(last_snapshot.choice_prompt_labels) if last_snapshot else []
            raise ChoicePromptBlockedError(
                f"choice prompt blocked response completion: {labels!r}"
            )
        if last_snapshot and last_snapshot.stop_visible:
            raise TimeoutError(
                f"assistant response remained active after {timeout} ms"
            )
        if saw_stale_assistant:
            raise IncompleteResponseTimeoutError(
                "only pre-refresh assistant output remained; stale response rejected"
            )
        if saw_incomplete_response:
            raise IncompleteResponseTimeoutError(
                "assistant output never stabilized as a structurally complete response"
            )
        if last_error is not None:
            raise TimeoutError(
                f"response wait exhausted after transient snapshot errors: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error
        raise TimeoutError(
            f"no accepted-user-identity assistant response within {timeout} ms"
        )

    async def stop(self, *, timeout_ms: int | None = None) -> str:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if not snapshot.stop_visible:
                raise UnsafePageStateError("Stop button is not visible")
            return await stop_response(self.page, timeout_ms=timeout)

    async def wait_for_state(
        self,
        expected: ChatGPTState,
        *,
        timeout_ms: int | None = None,
        poll_ms: int = 100,
        stable_ms: int = 0,
    ) -> ChatGPTSnapshot:
        timeout = timeout_ms or self.timeout_ms
        deadline = time.monotonic() + timeout / 1000
        stable_since: float | None = None
        last_snapshot: ChatGPTSnapshot | None = None

        while time.monotonic() < deadline:
            last_snapshot = await self.assert_ownership()
            if last_snapshot.state is expected:
                if stable_ms <= 0:
                    return last_snapshot
                if stable_since is None:
                    stable_since = time.monotonic()
                if (time.monotonic() - stable_since) * 1000 >= stable_ms:
                    return last_snapshot
            else:
                stable_since = None
            await asyncio.sleep(poll_ms / 1000)

        actual = last_snapshot.state.value if last_snapshot else "unavailable"
        raise TimeoutError(
            f"ChatGPT state did not become {expected.value!r} within {timeout} ms; "
            f"last state was {actual!r}"
        )

    async def upload_files(
        self,
        paths: Sequence[str],
        *,
        request_marker: str,
        timeout_ms: int | None = None,
        max_total_bytes: int = 20 * 1024 * 1024,
        expected_files: Sequence[Any] | None = None,
        file_snapshots: Sequence[Any] | None = None,
        exact_prompt: bool = False,
    ) -> Any:
        from .upload import upload_files

        return await upload_files(
            self,
            paths,
            request_marker=request_marker,
            timeout_ms=timeout_ms or self.timeout_ms,
            max_total_bytes=max_total_bytes,
            expected_files=expected_files,
            file_snapshots=file_snapshots,
            exact_prompt=exact_prompt,
        )

    async def current_attachment_ownership_token(
        self,
        *,
        expected_files: Sequence[Any],
    ) -> str | None:
        from .upload import current_attachment_ownership_token

        return await current_attachment_ownership_token(
            self.page,
            expected_files=expected_files,
        )

    async def wait_upload_ready(
        self,
        *,
        request_marker: str,
        exact_prompt: bool = False,
        expected_names: Sequence[str] | None = None,
        expected_count: int | None = None,
        timeout_ms: int | None = None,
        poll_ms: int = 100,
    ) -> ChatGPTSnapshot:
        from .upload import wait_upload_ready

        return await wait_upload_ready(
            self,
            request_marker=request_marker,
            exact_prompt=exact_prompt,
            expected_names=expected_names,
            expected_count=expected_count,
            timeout_ms=timeout_ms or self.timeout_ms,
            poll_ms=poll_ms,
        )

    async def recent_responses(
        self, count: int, *, by_turn: bool = True
    ) -> tuple[MessageSnapshot, ...]:
        snapshot = await self.assert_ownership()
        if by_turn:
            return recent_assistant_turns(snapshot.messages, count)
        return recent_assistant_messages(snapshot.messages, count)

    async def click_message_action(
        self,
        message_id: str,
        action_testid: str,
        *,
        timeout_ms: int | None = None,
    ) -> None:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            await self.assert_ownership()
            message = self.page.locator(
                f'[data-message-id="{message_id}"][data-message-author-role]'
            )
            await message.wait_for(state="visible", timeout=timeout)
            action = message.locator(f'button[data-testid="{action_testid}"]')
            await action.wait_for(state="visible", timeout=timeout)
            # Recheck ownership immediately before the click.
            await self.assert_ownership()
            await action.click(timeout=timeout)
