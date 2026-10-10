from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .chatgpt_backend import (
    _STREAM_STATUS_CACHE_LIMIT,
    _backend_context_state,
    _backend_get_object,
    _backend_session_token,
    _safe_identity_string,
    backend_create_project,
    backend_projects,
    backend_set_conversation_project,
    backend_stream_status,
    release_backend_stream_status,
)
from .chatgpt_composer import (
    COMPOSER_REPLACE_TEXT_JS as _COMPOSER_REPLACE_TEXT_JS,
    COMPOSER_RESOLVER_JS as _COMPOSER_RESOLVER_JS,
    replace_owned_composer_text as _replace_owned_composer_text,
)
from .chatgpt_errors import (
    AuthenticationRequiredError,
    ChatGPTAutomationError,
    ChoicePromptBlockedError,
    ComposerConflictError,
    ConversationTranscriptNotReadyError,
    IncompleteResponseTimeoutError,
    ManualInputPendingError,
    PageOwnershipError,
    RateLimitBlockedError,
    SendRecoveryError,
    StableMalformedResponseError,
    TaskBindingError,
    UnsafePageStateError,
)
from .chatgpt_graph import (
    BackendAuthError,
    BackendNotReadyError,
    BackendSchemaError,
    BackendUnavailableError,
    normalize_visible_text,
    rendered_user_text_matches,
    visible_text_matches,
)
from .chatgpt_response_projection import RESPONSE_PROJECTION_JS, RETRY_RESOLVER_JS
from .chatgpt_models import (
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    PageHealth,
    ResponseSegment,
    SendReceipt,
    WaitProbe,
    all_assistant_turns_for_receipt,
    assistant_turns_for_receipt,
    bound_assistant_for_receipt,
    capture_message_baseline,
    capture_response_recovery_baseline,
    classify_chatgpt_state,
    exact_prompt_seen,
    extract_session_id,
    looks_incomplete_response,
    merge_response_recovery_baselines,
    message_content_fingerprint,
    message_fingerprint,
    new_assistant_turns,
    new_messages_since,
    prompt_digest,
    receipt_user_message_seen,
    recent_assistant_messages,
    recent_assistant_turns,
    request_marker_from_prompt,
    response_activity_signature,
    response_is_stale,
    response_transport_ui_active,
    unique_new_user_message,
    validate_page_role,
)
from .observability import record_page_action
from .role_indicator import WINDOW_NAME_PREFIX, ensure_role_indicator
from .role_runtime.signals import (
    SignalIdentity,
    record_full_snapshot,
    record_probe,
    record_send_acceptance,
    record_signal_error,
)

ROLE_STORAGE_KEY = "playwright-auto:role"
PAGE_ID_STORAGE_KEY = "playwright-auto:page-id"
TASK_ID_STORAGE_KEY = "playwright-auto:task-id"
TEAM_STORAGE_KEY = "playwright-auto:team"
BINDING_ID_STORAGE_KEY = "playwright-auto:binding-id"
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


_CLOUDFLARE_CHALLENGE_JS = r"""
() => {
  const title = (document.title || '').trim().toLowerCase();
  const strongMarker = Boolean(document.querySelector(
    'script[src*="/cdn-cgi/challenge-platform/"], ' +
    'script[src*="challenges.cloudflare.com/turnstile"], ' +
    '[name="cf-turnstile-response"]'
  ));
  return strongMarker && title === 'just a moment...';
}
"""


async def cloudflare_challenge_visible(page: Any) -> bool:
    """Return true for the provider challenge page without mutating it."""
    url = str(getattr(page, "url", "") or "")
    if "__cf_chl_rt_tk=" in url:
        return True
    try:
        return await page.evaluate(_CLOUDFLARE_CHALLENGE_JS) is True
    except Exception:
        # Navigation can replace the execution context while the challenge is
        # settling. The next readiness probe will retry against the new one.
        return False


_PAGE_WORKFLOW_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_PAGE_MUTATION_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
_PAGE_WAIT_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_PAGE_WAIT_PROBE_INIT: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
_PAGE_AMBIENT_OBSERVATION_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_PAGE_PASSIVE_OBSERVATION_STATES: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
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


def _mcp_permission_action_from_message(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    metadata = value.get("metadata")
    jit = metadata.get("jit_plugin_data") if isinstance(metadata, Mapping) else None
    from_server = jit.get("from_server") if isinstance(jit, Mapping) else None
    body = from_server.get("body") if isinstance(from_server, Mapping) else None
    actions = body.get("actions") if isinstance(body, Mapping) else None
    if not isinstance(actions, list):
        return None
    fallback = None
    for item in actions[:16]:
        if not isinstance(item, Mapping) or str(item.get("type") or "").lower() != "allow":
            continue
        allow = item.get("allow")
        target = _safe_identity_string(allow.get("target_message_id") if isinstance(allow, Mapping) else None)
        if target and fallback is None:
            fallback = {"type": "allow", "target_message_id": target, "remember_answer": False, "label": "Allow"}
        for option in item.get("split_action_options") or ():
            if not isinstance(option, Mapping):
                continue
            action = option.get("action")
            if not isinstance(action, Mapping) or str(action.get("type") or "").lower() != "allow":
                continue
            option_target = _safe_identity_string(action.get("target_message_id"))
            if not option_target:
                continue
            candidate = {
                "type": "allow",
                "target_message_id": option_target,
                "remember_answer": bool(action.get("remember_answer")),
                "label": str(option.get("label") or "Allow").strip()[:256] or "Allow",
            }
            if candidate["remember_answer"]:
                return candidate
            if fallback is None:
                fallback = candidate
    return fallback


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
    permission_action: dict[str, Any] | None = None
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
                message = decoded["message"]
                observed_messages.append(message)
                permission_action = _mcp_permission_action_from_message(message) or permission_action
            elif isinstance(decoded.get("messages"), list):
                messages = decoded["messages"]
                observed_messages.extend(messages)
                for message in messages:
                    permission_action = _mcp_permission_action_from_message(message) or permission_action
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
    result = {
        "source": "frontend_sse",
        "coverage": "complete" if saw_done and len(graph["mapping"]) > 1 else "partial",
        "observed_user_message_id": user_message_id,
        "conversation_id": next(iter(conversation_ids)),
        "graph": graph,
    }
    if permission_action is not None:
        result["permission_action"] = permission_action
    return result


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


async def clear_composer(page: Any, timeout_ms: int = 5_000) -> None:
    """Clear only the uniquely owned composer."""
    await _replace_owned_composer_text(page, "", timeout_ms=timeout_ms)


async def set_composer_text(page: Any, text: str, timeout_ms: int = 5_000) -> None:
    await action_delay(page, "composer_fill", action_delay_multiplier("composer_fill"))
    await record_page_action(page, "composer_fill", "start", detail=f"{len(text)} chars")
    try:
        await _replace_owned_composer_text(page, text, timeout_ms=timeout_ms)
    except Exception as exc:
        await record_page_action(
            page,
            "composer_fill",
            "error",
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    await record_page_action(page, "composer_fill", "complete", detail=f"{len(text)} chars")


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
    fallback_reason = "new_chat_dom_missing"
    if clicked:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        ready = """() => {
          const composer = (__CDPA_COMPOSER_RESOLVER__)().element;
          return location.pathname === '/' && Boolean(composer) &&
            !((composer.innerText || '').trim());
        }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS)
        try:
            await page.wait_for_function(ready, timeout=timeout_ms)
        except (TimeoutError, PlaywrightTimeoutError):
            fallback_reason = "new_chat_dom_timeout"
            # The click may already have committed despite a readiness timeout.
            # Transport/renderer errors are not authorization to navigate again.
            if await page.evaluate(ready):
                await record_page_action(page, "new_chat", "complete", detail="dom")
                return "dom"
        else:
            await record_page_action(page, "new_chat", "complete", detail="dom")
            return "dom"

    # Pre-click discard permission does not authorize discarding input that
    # appeared while the UI transition/readiness observation was pending.
    probe = await asyncio.wait_for(
        inspect_chatgpt_wait_probe(page), timeout=max(timeout_ms, 1) / 1000
    )
    if probe.composer_ambiguous or probe.composer_text.strip() or probe.attachment_count:
        raise ComposerConflictError(
            "New chat fallback would discard current draft or attachments, "
            "or composer ownership is ambiguous"
        )
    navigation = await record_page_action(
        page, "goto", "start", reason=fallback_reason
    )
    try:
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    except Exception as exc:
        await record_page_action(
            page, "goto", "error", reason=fallback_reason,
            identity=navigation, detail=type(exc).__name__,
        )
        raise
    await record_page_action(
        page, "goto", "complete", reason=fallback_reason, identity=navigation
    )
    await page.wait_for_function(
        """() => Boolean((__CDPA_COMPOSER_RESOLVER__)().element)"""
        .replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS),
        polling=50,
        timeout=timeout_ms,
    )
    await record_page_action(page, "new_chat", "complete", detail="navigate")
    return "navigate"


async def refresh_page(
    page: Any, timeout_ms: int = 15_000, *, reason: str | None = None,
    identity: dict[str, Any] | None = None,
) -> None:
    reload_event = await record_page_action(
        page, "reload", "start", reason=reason, identity=identity
    )
    try:
        await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        await record_page_action(
            page, "reload", "error", reason=reason, identity=reload_event,
            detail=type(exc).__name__,
        )
        raise
    await record_page_action(
        page, "reload", "complete", reason=reason, identity=reload_event
    )


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

          const conversationId = (path) => {
            const match = String(path || '').match(
              /^\/c\/([0-9a-fA-F-]{36})\/?$|^\/g\/[^/]+\/c\/([0-9a-fA-F-]{36})\/?$/
            );
            return match ? (match[1] || match[2]).toLowerCase() : null;
          };
          const validateOwnership = () => {
            const expectedId = conversationId(expectedPath);
            const currentId = conversationId(location.pathname);
            const sameConversation = Boolean(expectedId && expectedId === currentId);
            if (expectedHostname !== null && (
                location.hostname.toLowerCase() !== expectedHostname ||
                (location.pathname !== expectedPath && !sameConversation)
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
            if (errorAlert) {
              return {ok: false, method: 'page_state_conflict', reason: 'error_state'};
            }
            if (firstVisible('[role="dialog"]:not(#playwright-auto-role-control-v1), [data-testid^="modal-"]')) {
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

          const findComposer = () => (__CDPA_COMPOSER_RESOLVER__)().element;
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
          const findSendTarget = (scopeRoot) => [
            ...scopeRoot.querySelectorAll('button,[role="button"]')
          ].map((button) => {
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
        }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS),
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


async def click_preferred_mcp_allow(
    page: Any,
    *,
    passive_action: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Dispatch the current MCP Allow with minimal friction."""
    result = await page.evaluate(
        r"""async (passiveAction) => {
          const visible = (e) => Boolean(e && getComputedStyle(e).visibility !== 'hidden' &&
            (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
          const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim();
          const target = passiveAction?.type === 'allow' ? String(passiveAction.target_message_id || '') : '';
          const roots = [...document.querySelectorAll('button[aria-label^="Allow "][aria-label$=" for this conversation"], [data-message-id]')];
          for (const root of roots) {
            let node = root, fiber = null, handler = null, handlerKind = null;
            const containers = [];
            for (let i = 0; node && i < 10; i += 1, node = node.parentElement) {
              const pk = Object.keys(node).find(k => k.startsWith('__reactProps'));
              const fk = Object.keys(node).find(k => k.startsWith('__reactFiber'));
              if (pk) containers.push(node[pk]);
              if (!fiber && fk) fiber = node[fk];
            }
            for (let i = 0; fiber && i < 40; i += 1, fiber = fiber.return) {
              containers.push(fiber.memoizedProps, fiber.pendingProps);
            }
            for (const props of containers) {
              if (!props || typeof props !== 'object') continue;
              if (typeof props.onSelectOption === 'function') {
                handler = props.onSelectOption; handlerKind = 'select'; break;
              }
              if (!handler && typeof props.handleUserAction === 'function') {
                handler = props.handleUserAction; handlerKind = 'user_action';
              }
            }
            if (!handler) continue;
            const seen = new Set(), found = [];
            const walk = (value, depth = 0) => {
              if (!value || typeof value !== 'object' || seen.has(value) || depth > 6) return;
              seen.add(value);
              if (value.type === 'allow' && typeof value.target_message_id === 'string') found.push(value);
              if (value.action?.type === 'allow' && typeof value.action.target_message_id === 'string') found.push(value.action);
              for (const child of Object.values(value)) walk(child, depth + 1);
            };
            for (const props of containers) walk(props);
            if (passiveAction?.type === 'allow') found.push(passiveAction);
            const candidates = found.filter(a => !target || a.target_message_id === target)
              .sort((a,b) => Number(b.remember_answer === true) - Number(a.remember_answer === true));
            if (!candidates.length) continue;
            const chosen = candidates[0];
            if (handlerKind === 'user_action') {
              await handler(chosen);
            } else {
              await handler({preventDefault(){}, stopPropagation(){}, currentTarget:root, target:root}, chosen);
            }
            return {method:'react_handler', target_message_id:chosen.target_message_id,
              remember_answer:chosen.remember_answer === true ? 'true' : 'false'};
          }
          const conversationPattern = /^Allow\b.*\bconversation\b/i;
          const conversationButtons = () => [...document.querySelectorAll('button,[role="menuitem"],[role="option"]')]
            .filter(e => visible(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true' &&
              conversationPattern.test(norm(e.innerText || e.textContent || e.getAttribute('aria-label'))));
          const direct = conversationButtons();
          if (direct.length === 1) { direct[0].click(); return {method:'dom_click_conversation', target_message_id:'', remember_answer:'true'}; }
          const surfaces = [...document.querySelectorAll('[data-codex-approval-surface="true"]')].filter(visible);
          if (surfaces.length === 1) {
            const surface = surfaces[0];
            const options = [...surface.querySelectorAll('button')].find(e => visible(e) && !e.disabled &&
              e.getAttribute('aria-disabled') !== 'true' && norm(e.getAttribute('aria-label')) === 'Approval options');
            const once = [...surface.querySelectorAll('button')].find(e => visible(e) && !e.disabled &&
              e.getAttribute('aria-disabled') !== 'true' && /^Allow once\b/i.test(norm(e.innerText || e.textContent)));
            if (options && once) {
              options.click();
              for (let i = 0; i < 20; i += 1) {
                await new Promise(resolve => setTimeout(resolve, 50));
                const candidates = conversationButtons();
                if (candidates.length === 1) { candidates[0].click(); return {method:'dom_click_conversation_menu', target_message_id:'', remember_answer:'true'}; }
                if (candidates.length > 1) { once.click(); return {method:'dom_click_once_fallback_ambiguous', target_message_id:'', remember_answer:'false'}; }
              }
              once.click(); return {method:'dom_click_once_fallback_missing', target_message_id:'', remember_answer:'false'};
            }
          }
          const plain = [...document.querySelectorAll('button')]
            .find(e => visible(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true' && norm(e.innerText || e.textContent) === 'Allow');
          if (plain) { plain.click(); return {method:'dom_click', target_message_id:'', remember_answer:'false'}; }
          return null;
        }""",
        dict(passive_action) if isinstance(passive_action, Mapping) else None,
    )
    if not isinstance(result, Mapping):
        return None
    detail = f"{result.get('method')}:{result.get('target_message_id') or ''}"
    await record_page_action(page, "mcp_allow", "complete", detail=detail)
    return {
        "method": str(result.get("method") or ""),
        "target_message_id": str(result.get("target_message_id") or ""),
        "remember_answer": str(result.get("remember_answer") or "false"),
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
    await page.evaluate(
        """() => {
          const composer = (__CDPA_COMPOSER_RESOLVER__)().element;
          if (composer) composer.focus();
        }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS)
    )
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
          const logicalTurnId = (element, role) => {
            const turnId = element?.closest('[data-turn-id]')?.getAttribute('data-turn-id');
            if (turnId) return turnId;
            const groupKey = element?.closest('[data-turn-key]')?.getAttribute('data-turn-key');
            if (groupKey && role) return `group:${role}:${groupKey}`;
            return element?.closest('[data-turn-id-container]')
              ?.getAttribute('data-turn-id-container') || null;
          };
          const semanticMessageId = (element, role) => {
            let owner = element?.closest('[data-chatgpt-search-message-ids]') || null;
            if (!owner && role) {
              const turn = element?.closest('[data-turn-key],[data-turn-id],[data-turn-id-container]');
              const matches = turn ? [...turn.querySelectorAll(
                '[data-chatgpt-search-unit-key][data-chatgpt-search-message-ids]'
              )].filter((unit) => String(unit.getAttribute('data-chatgpt-search-unit-key') || '')
                .endsWith(`:${role}`)) : [];
              if (matches.length === 1) owner = matches[0];
            }
            const values = String(owner?.getAttribute('data-chatgpt-search-message-ids') || '')
              .trim().split(/\s+/).filter(Boolean);
            const unique = [...new Set(values)];
            return unique.length === 1 ? unique[0] : null;
          };
          const identity = (element, role) => ({
            messageId: element?.getAttribute('data-message-id') || semanticMessageId(element, role),
            turnId: logicalTurnId(element, role),
          });
          const textShape = (element) => {
            const value = (element?.innerText || element?.textContent || '').replace(/\s+/g, ' ').trim();
            return {length: value.length, tail: value.slice(-160)};
          };
          const semanticTurns = () => {
            const counts = {user: 0, assistant: 0};
            const turns = [];
            for (const heading of document.querySelectorAll('main h4')) {
              const label = (heading.textContent || '').replace(/\s+/g, ' ').trim();
              const role = label === 'You said:' ? 'user' : label === 'ChatGPT said:' ? 'assistant' : null;
              if (!role || !heading.parentElement) continue;
              const element = heading.parentElement;
              const raw = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
              const body = raw.startsWith(label) ? raw.slice(label.length).trim() : raw;
              const ordinal = counts[role]++;
              turns.push({
                role,
                element,
                messageId: semanticMessageId(element, role) || `DOM:${role}:${ordinal}`,
                turnId: logicalTurnId(element, role),
                text: body,
              });
            }
            return turns;
          };

          const composerOwner = (__CDPA_COMPOSER_RESOLVER__)();
          const composer = composerOwner.element;
          const composerRoot = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || null;
          const stop = firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]');
          const retry = (__CDPA_RETRY_RESOLVER__);
          const activeResponse = latest('[data-streaming-response-status]');
          const projection = (__CDPA_RESPONSE_PROJECTION__);
          const semantic = semanticTurns();
          const semanticUser = semantic.filter((turn) => turn.role === 'user').at(-1) || null;
          const semanticAssistant = semantic.filter((turn) => turn.role === 'assistant').at(-1) || null;
          const lastUser = latest('[data-message-author-role="user"][data-message-id]');
          const assistantNodes = [...document.querySelectorAll('[data-message-author-role="assistant"][data-message-id]')]
            .filter(visible);
          const lastAssistant = assistantNodes.filter((element) => textShape(element).length > 0).at(-1)
            || assistantNodes.at(-1) || null;
          const userIdentity = lastUser ? identity(lastUser, 'user') : {
            messageId: semanticUser?.messageId || null,
            turnId: semanticUser?.turnId || null,
          };
          const assistantIdentity = lastAssistant ? identity(lastAssistant, 'assistant') : {
            messageId: semanticAssistant?.messageId || null,
            turnId: semanticAssistant?.turnId || null,
          };
          const assistantText = lastAssistant ? boundedText(lastAssistant, 24000) : (semanticAssistant?.text || '');
          const projectedActivityText = projection.segments.map((segment) => segment.text).join(' ').slice(0, 24000);
          const activityText = projectedActivityText || (activeResponse ? boundedText(activeResponse, 24000) : (semanticAssistant?.text || ''));
          const assistantShape = {
            length: assistantText.length,
            tail: assistantText.slice(-160),
          };
          const activityShape = {
            length: activityText.length,
            tail: activityText.slice(-160),
          };
          const errors = [...document.querySelectorAll('[role="alert"]')]
            .filter(visible).slice(-4).map((element) => boundedText(element, 240)).filter(Boolean);
          const dialogs = [...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')]
            .filter(element => element.id !== 'playwright-auto-role-control-v1')
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
          const mcpConversationAllow = /^Allow\b.*\bconversation\b/i;
          const mcpPermissionNodes = [...document.querySelectorAll('button[aria-label]')]
            .filter((button) => mcpConversationAllow.test(normalizeLabel(button.getAttribute('aria-label'))));
          const splitApprovalSurfaces = [...document.querySelectorAll('[data-codex-approval-surface="true"]')]
            .filter(visible)
            .filter((surface) => {
              const buttons = [...surface.querySelectorAll('button')].filter((button) =>
                visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true');
              return buttons.some((button) => /^Allow once\b/i.test(normalizeLabel(button.innerText || button.textContent))) &&
                buttons.some((button) => normalizeLabel(button.getAttribute('aria-label')) === 'Approval options');
            });
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
            composer_ambiguous: Boolean(composerOwner.ambiguous),
            composer_owner_source: composerOwner.source || 'none',
            attachment_count: attachmentCount,
            stop_visible: Boolean(stop),
            retry_visible: Boolean(retry),
            transport_active: Boolean(stop) || Boolean(projection.active),
            error_texts: errors,
            blocking_dialogs: dialogs,
            choice_prompt_labels: [...new Set(choices)],
            mcp_permission_allow_count: mcpPermissionGroups.length + splitApprovalSurfaces.length,
            mcp_permission_node_count: mcpPermissionNodes.length + splitApprovalSurfaces.length,
            last_user_message_id: userIdentity.messageId,
            last_user_turn_id: userIdentity.turnId,
            last_assistant_message_id: assistantIdentity.messageId,
            last_assistant_turn_id: assistantIdentity.turnId,
            assistant_text_length: assistantShape.length,
            assistant_text_tail: assistantShape.tail,
            response_activity_length: activityShape.length,
            response_activity_tail: activityShape.tail,
            response_activity_turn_id: projection.active ? projection.turn_id : null,
            response_segments: projection.segments,
            response_projection_active: Boolean(projection.active),
            last_user_text: lastUser ? boundedText(lastUser, 12000) : (semanticUser?.text || ''),
            assistant_text: assistantText,
            response_activity_text: activityText,
          };
          };
          return true;
        }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS).replace(
            '__CDPA_RESPONSE_PROJECTION__', RESPONSE_PROJECTION_JS
        ).replace('__CDPA_RETRY_RESOLVER__', RETRY_RESOLVER_JS)


async def inspect_chatgpt_wait_probe(
    page: Any,
    *,
    previous_probe: WaitProbe | None = None,
    wait_ms: int = 0,
) -> WaitProbe:
    """Read bounded wait-state evidence without walking or serializing the transcript."""
    if hasattr(page, "add_init_script") and not _PAGE_WAIT_PROBE_INIT.get(page, False):
        args = [ROLE_STORAGE_KEY, PAGE_ID_STORAGE_KEY, TASK_ID_STORAGE_KEY, TEAM_STORAGE_KEY, WINDOW_NAME_PREFIX]
        await page.add_init_script(
            script=f"({_WAIT_PROBE_INSTALL_SCRIPT})({json.dumps(args, ensure_ascii=False)});"
        )
        # A reconnect can attach to a document that still holds an older probe
        # function. Install the current reader into the live document too.
        await page.evaluate(_WAIT_PROBE_INSTALL_SCRIPT, args)
        _PAGE_WAIT_PROBE_INIT[page] = True

    # PageObserver is the one page-lifetime wake source for real Playwright
    # pages. Lightweight unit-test doubles may expose only evaluate/add_init_script;
    # they keep the read-only probe path and do not create another observer.
    observer_capable = all(
        hasattr(page, name)
        for name in ("on", "expose_binding", "add_init_script", "context")
    )
    if observer_capable:
        from .role_runtime.page_observer import observer_for

        observer = observer_for(page)
        await observer.install()
        if previous_probe is not None and wait_ms > 0:
            await observer.wait(max(0, wait_ms) / 1000)
    elif previous_probe is not None and wait_ms > 0:
        await asyncio.sleep(max(0, wait_ms) / 1000)

    raw = await page.evaluate(_WAIT_PROBE_READ_SCRIPT)
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
        raw = await page.evaluate(_WAIT_PROBE_READ_SCRIPT)
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
        composer_ambiguous=bool(raw.get("composer_ambiguous")),
        composer_owner_source=str(raw.get("composer_owner_source") or ""),
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
        retry_visible=bool(raw.get("retry_visible")),
        last_user_text=str(raw.get("last_user_text") or ""),
        assistant_text=str(raw.get("assistant_text") or ""),
        response_activity_text=str(raw.get("response_activity_text") or ""),
        response_segments=tuple(
            ResponseSegment(
                key=str(segment.get("key") or ""),
                kind=str(segment.get("kind") or "status"),
                text=str(segment.get("text") or ""),
                active=bool(segment.get("active")),
            )
            for segment in raw.get("response_segments") or ()
            if segment.get("key") and segment.get("text")
        ),
        response_projection_active=bool(raw.get("response_projection_active")),
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

          const composerOwner = (__CDPA_COMPOSER_RESOLVER__)();
          const composer = composerOwner.element;
          const composerRoot = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || document;
          const sendCandidates = [
            ...composerRoot.querySelectorAll('button,[role="button"]')
          ].map((button) => {
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
          const retry = (__CDPA_RETRY_RESOLVER__);
          const errorTexts = [...document.querySelectorAll('[role="alert"]')]
            .filter(visible)
            .map(text)
            .filter(Boolean);
          const blockingDialogs = [...document.querySelectorAll('[role="dialog"], [data-testid^="modal-"]')]
            .filter(element => element.id !== 'playwright-auto-role-control-v1')
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

          const latestTurnRoot = [...document.querySelectorAll('[data-turn-key],[data-turn-id],[data-turn-id-container]')]
            .filter(visible).at(-1) || null;
          const chatRoot = latestTurnRoot?.closest('main') || composer?.closest('main') || document.body;
          const logicalTurnId = (element, role) => {
            const turnId = element?.closest('[data-turn-id]')?.getAttribute('data-turn-id');
            if (turnId) return turnId;
            const groupKey = element?.closest('[data-turn-key]')?.getAttribute('data-turn-key');
            if (groupKey && role) return `group:${role}:${groupKey}`;
            return element?.closest('[data-turn-id-container]')
              ?.getAttribute('data-turn-id-container') || null;
          };
          const semanticMessageId = (element, role) => {
            let owner = element?.closest('[data-chatgpt-search-message-ids]') || null;
            if (!owner && role) {
              const turn = element?.closest('[data-turn-key],[data-turn-id],[data-turn-id-container]');
              const matches = turn ? [...turn.querySelectorAll(
                '[data-chatgpt-search-unit-key][data-chatgpt-search-message-ids]'
              )].filter((unit) => String(unit.getAttribute('data-chatgpt-search-unit-key') || '')
                .endsWith(`:${role}`)) : [];
              if (matches.length === 1) owner = matches[0];
            }
            const values = String(owner?.getAttribute('data-chatgpt-search-message-ids') || '')
              .trim().split(/\s+/).filter(Boolean);
            const unique = [...new Set(values)];
            return unique.length === 1 ? unique[0] : null;
          };
          const completedAriaAction = (value) => {
            const label = String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (label === 'copy') return 'copy';
            if (label === 'regenerate response') return 'regenerate';
            if (['rate response', 'good response', 'bad response'].includes(label)) return 'feedback';
            return '';
          };
          const messageActions = (element, scope = element) => {
            const testIds = [...element.querySelectorAll('button[data-testid]')]
              .filter(visible)
              .map((button) => button.dataset.testid)
              .filter(Boolean);
            const ariaActions = [...scope.querySelectorAll('button[aria-label]')]
              .filter(visible)
              .map((button) => completedAriaAction(button.getAttribute('aria-label')))
              .filter(Boolean);
            return [...new Set([...testIds, ...ariaActions])];
          };
          const logicalTurnIds = new Set();
          for (const turn of chatRoot.querySelectorAll('[data-turn-id]')) {
            const value = turn.getAttribute('data-turn-id');
            if (value) logicalTurnIds.add(value);
          }
          for (const container of chatRoot.querySelectorAll('[data-turn-id-container]')) {
            const value = container.getAttribute('data-turn-id-container');
            if (value) logicalTurnIds.add(value);
          }
          for (const group of chatRoot.querySelectorAll('[data-turn-key]')) {
            const key = group.getAttribute('data-turn-key');
            if (!key) continue;
            logicalTurnIds.add(`group:user:${key}`);
            logicalTurnIds.add(`group:assistant:${key}`);
          }
          const legacyMessages = [...chatRoot.querySelectorAll('[data-message-author-role][data-message-id]')]
            .filter(visible)
            .map((element) => {
              const role = element.getAttribute('data-message-author-role') || '';
              const turnId = logicalTurnId(element, role);
              const actions = messageActions(element);
              const imageCount = [...element.querySelectorAll('img')].filter(visible).length;
              return {
                role: element.getAttribute('data-message-author-role') || '',
                message_id: element.getAttribute('data-message-id') || '',
                turn_id: turnId,
                text: text(element),
                actions: [...new Set(actions)],
                image_count: imageCount,
              };
            });
          const semanticCounts = {user: 0, assistant: 0};
          const semanticMessages = [];
          for (const heading of chatRoot.querySelectorAll('h4')) {
            const label = (heading.textContent || '').replace(/\s+/g, ' ').trim();
            const role = label === 'You said:' ? 'user' : label === 'ChatGPT said:' ? 'assistant' : null;
            if (!role || !heading.parentElement) continue;
            const element = heading.parentElement;
            const rawText = text(element);
            const body = rawText.startsWith(label) ? rawText.slice(label.length).trim() : rawText;
            const actionScope = role === 'assistant'
              ? (element.closest('[data-turn-key],[data-turn-id],[data-turn-id-container]') || element)
              : element;
            const actions = messageActions(element, actionScope);
            const semanticTurnId = logicalTurnId(element, role);
            const semanticOrdinal = semanticCounts[role]++;
            semanticMessages.push({
              role,
              message_id: semanticMessageId(element, role) || `DOM:${role}:${semanticTurnId || semanticOrdinal}`,
              turn_id: semanticTurnId,
              text: body,
              actions,
              image_count: [...element.querySelectorAll('img')].filter(visible).length,
            });
          }
          const messages = legacyMessages.length ? legacyMessages : semanticMessages;

          const activeResponse = [...chatRoot.querySelectorAll('[data-streaming-response-status]')]
            .filter(visible)
            .at(-1) || null;
          const projection = (__CDPA_RESPONSE_PROJECTION__);
          const semanticAssistant = semanticMessages.filter((message) => message.role === 'assistant').at(-1) || null;
          const projectedActivityText = projection.segments.map((segment) => segment.text).join(' ').slice(0, 24000);
          const responseActivityText = projectedActivityText || (activeResponse ? text(activeResponse) : (semanticAssistant?.text || ''));
          const responseActivityStructure = activeResponse
            ? [...activeResponse.querySelectorAll('[data-testid]')]
                .filter(visible)
                .map((element) => element.getAttribute('data-testid') || '')
                .filter(Boolean)
                .join('|')
            : (semanticAssistant ? 'semantic-assistant' : '');
          const responseActivityTurnId = projection.active ? projection.turn_id : null;

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
            composer_ambiguous: Boolean(composerOwner.ambiguous),
            composer_owner_source: composerOwner.source || 'none',
            send_visible: Boolean(send),
            send_enabled: Boolean(
              send && !send.disabled && send.getAttribute('aria-disabled') !== 'true'
            ),
            stop_visible: Boolean(stop),
            blocking_dialogs: [...new Set(blockingDialogs)],
            attachment_markers: attachmentMarkers,
            choice_prompt_labels: [...new Set(choicePromptLabels)],
            error_present: Boolean(authCallbackError || alertError),
            mcp_permission_node_count: [...document.querySelectorAll('button')]
              .filter(b => /^Allow\b.*\bconversation\b/i.test(text(b) || (b.getAttribute('aria-label') || '').trim())).length +
              document.querySelectorAll('[data-codex-approval-surface="true"]').length,
            mcp_permission_allow_count: [...document.querySelectorAll('button')]
              .filter(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true' &&
                (text(b) === 'Allow' || /^Allow\b.*\bconversation\b/i.test(text(b) || (b.getAttribute('aria-label') || '').trim()))).length +
              [...document.querySelectorAll('[data-codex-approval-surface="true"]')].filter(visible).length,
            retry_visible: Boolean(retry),
            error_texts: errorTexts,
            response_activity_text: responseActivityText,
            response_activity_structure: responseActivityStructure,
            response_activity_turn_id: responseActivityTurnId,
            response_segments: projection.segments,
            response_projection_active: Boolean(projection.active),
            logical_turn_ids: [...logicalTurnIds],
            messages,
          };
        }
        """.replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS)
          .replace('__CDPA_RESPONSE_PROJECTION__', RESPONSE_PROJECTION_JS)
          .replace('__CDPA_RETRY_RESOLVER__', RETRY_RESOLVER_JS),
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
        logical_turn_ids=tuple(
            str(value) for value in raw.get("logical_turn_ids") or [] if value
        ),
        response_segments=tuple(
            ResponseSegment(
                key=str(segment.get("key") or ""),
                kind=str(segment.get("kind") or "status"),
                text=str(segment.get("text") or ""),
                active=bool(segment.get("active")),
            )
            for segment in raw.get("response_segments") or ()
            if segment.get("key") and segment.get("text")
        ),
        response_projection_active=bool(raw.get("response_projection_active")),
        state=classify_chatgpt_state(raw_for_state),
        requires_login=bool(raw.get("requires_login")),
        composer_present=bool(raw.get("composer_present")),
        composer_editable=bool(raw.get("composer_editable")),
        composer_text=str(raw.get("composer_text") or ""),
        composer_ambiguous=bool(raw.get("composer_ambiguous")),
        composer_owner_source=str(raw.get("composer_owner_source") or ""),
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
        mcp_permission_allow_count=max(0, int(raw.get("mcp_permission_allow_count") or 0)),
        mcp_permission_node_count=max(0, int(raw.get("mcp_permission_node_count") or 0)),
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
        self._frontend_request_user_message_id: str | None = None
        self._wait_state = _page_wait_state(page)
        self._wait_metrics: dict[str, Any] = {
            "sparse_probes": 0,
            "full_snapshots": 0,
            "unchanged_ticks": 0,
            "sparse_errors": 0,
            "full_snapshot_reasons": {},
        }

    async def observe_session_tool_capability(
        self, *, requirement: Mapping[str, Any], phase: str
    ) -> dict[str, Any]:
        """Observe the exact owned page, without sending a probe or trusting chat prose.

        The current web UI exposes composer controls, not a session-bound MCP
        device/operation authorization attestation. Report that real limitation;
        never echo requested device/account/operations as observed READY evidence.
        """
        from datetime import datetime, timezone
        from urllib.parse import urlsplit

        snapshot = await self.assert_ownership()
        base = {
            **dict(requirement),
            "status": "unknown", "target_device_id": None,
            "principal_scope": None, "project_binding": None,
            "required_operations": [],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "session_revision": "",
            "evidence_ref": "live_page_dom:session_target_operations_unobservable",
            "reason_code": "session_tool_visibility_unknown",
        }
        try:
            live = await asyncio.wait_for(self.page.evaluate("""() => ({
                url: location.href,
                time_origin: performance.timeOrigin,
                ready: document.readyState,
                composer_present: !!document.querySelector('#prompt-textarea'),
                controls: [...document.querySelectorAll('form button, form [role="button"]')]
                  .filter(e => e.getClientRects().length)
                  .slice(0,40).map(e => ({
                    testid: e.getAttribute('data-testid'),
                    label: (e.getAttribute('aria-label') || '').slice(0,120)
                  }))
            })"""), timeout=min(self.timeout_ms / 1000, 5.0))
        except Exception as exc:
            base["reason_detail"] = f"Exact page probe failed: {type(exc).__name__}; no capability evidence was assumed."
            return base
        if not isinstance(live, Mapping):
            base["reason_detail"] = "Exact page probe returned no structured DOM observation."
            return base
        observed_url = str(live.get("url") or "")
        path = urlsplit(observed_url).path
        base["conversation_id"] = path if path.startswith('/c/') else f"WEB:{snapshot.page_id}"
        origin = live.get("time_origin")
        base["session_revision"] = f"{snapshot.page_id}:{origin}" if origin is not None else ""
        if observed_url != str(snapshot.url):
            base["reason_detail"] = "Page navigated during observation; exact-session capability is unknown."
            return base
        if snapshot.requires_login:
            base["status"] = "unavailable"
            base["reason_code"] = "tool_authorization_failed"
            base["reason_detail"] = "The exact owned page requires authentication."
            return base
        base["reason_detail"] = (
            f"Owned page observed during {phase}; composer_present={bool(live.get('composer_present'))}. "
            "The observed web UI provides no authenticated session-bound connector function list, "
            "stable target device, account/project scope or operation grants. Composer labels, "
            "conversation text and external gateway health are not authorization evidence."
        )
        return base

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
        self._frontend_request_user_message_id = None
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
                status = getattr(response, "status", None)
                if (
                    observed_user_message_id is None
                    or not isinstance(status, int)
                    or not 200 <= status < 300
                ):
                    finish(None)
                    return
                # Response headers tie this successful POST to the exact
                # outgoing user identity. Body parsing remains asynchronous
                # conversation-id enrichment and never gates Send acceptance.
                self._frontend_request_user_message_id = observed_user_message_id

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
        from .role_runtime.page_observer import observer_for
        observer_for(self.page).close()

    def install_ambient_observer(self) -> None:
        from .role_runtime.page_observer import observer_for
        observer = observer_for(self.page)
        observer.attach()
        if not observer.installed:
            observer._spawn(observer.install())

    def ambient_permission_action(self) -> dict[str, Any] | None:
        return self.page_observation().get("permission_action")

    def clear_ambient_permission_action(self) -> None:
        self.clear_permission_action()

    def arm_passive_observer(self, *, request_id: str, generation: int,
                             conversation_id: str | None = None,
                             accepted_user_message_id: str | None = None,
                             task_id: str | None = None, team: str | None = None) -> None:
        # Kept as a Send-boundary adapter: request metadata is not observation authority.
        self.install_ambient_observer()

    def passive_observation(self, *, request_id: str = "", generation: int = 0) -> dict[str, Any]:
        evidence = self.page_observation()
        messages = evidence.get("messages") or []
        latest_user = next((m.get("id") for m in reversed(messages)
                            if m.get("author", {}).get("role") == "user"), None)
        # Optional graph-shaped information serves existing report tooling. Failure
        # does not discard a live permission/response or prevent normal operation.
        graph = _observed_linear_graph(messages, latest_user, require_user_in_values=True) if latest_user else None
        return {
            **evidence, "graph": graph,
            "coverage": "complete" if evidence.get("response") else "partial",
            "observed_user_message_id": latest_user,
            "event_count": evidence.get("diagnostics", {}).get("observations", 0),
        }

    async def wait_for_passive_observation(self, *, request_id: str, generation: int,
                                          timeout_ms: int) -> bool:
        return await self.wait_for_page_observation(timeout_ms=timeout_ms)

    def clear_passive_permission_action(self) -> None:
        self.clear_permission_action()

    def detach_passive_observer(self) -> None:
        # Passive observer lifetime is independent from controller polling.
        return None

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
        if (
            safety_interval_ms is not None
            and safety_interval_ms > 0
            and (time.monotonic() - self._wait_full_snapshot_at) * 1000 >= safety_interval_ms
        ):
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
            state = classify_chatgpt_state({
                "composer_present": probe.composer_present,
                "composer_text": probe.composer_text,
                "messages": [message.to_dict() for message in cached.messages],
            })
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
            composer_ambiguous=probe.composer_ambiguous,
            composer_owner_source=probe.composer_owner_source,
            stop_visible=probe.stop_visible,
            retry_visible=probe.retry_visible,
            mcp_permission_allow_count=probe.mcp_permission_allow_count,
            mcp_permission_node_count=probe.mcp_permission_node_count,
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
        safety_interval_ms: int | None = None,
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
        safety_interval_ms: int | None = None,
    ) -> ChatGPTSnapshot:
        key = self._wait_key(receipt)
        if key != self._wait_receipt_key:
            self.invalidate_wait_cache()
            self._wait_receipt_key = key
            force_full = True
        previous_probe = self._wait_probe
        try:
            probe = await inspect_chatgpt_wait_probe(
                self.page,
                previous_probe=previous_probe,
                wait_ms=max(0, int(probe_wait_ms)),
            )
            self._wait_metrics["sparse_probes"] += 1
            self._assert_wait_probe_ownership(probe)
            record_probe(probe, previous_probe)
        except (AuthenticationRequiredError, PageOwnershipError):
            raise
        except Exception as exc:
            self._wait_metrics["sparse_errors"] += 1
            record_signal_error(
                SignalIdentity(
                    page_url=str(getattr(self.page, "url", "") or ""),
                    page_id=(self.binding.page_id if self.binding else None),
                    role=(self.binding.role if self.binding else None),
                ),
                component="chatgpt_wait",
                stage="sparse_probe",
                error=exc,
            )
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
            record_full_snapshot(full)
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
                    composer_ambiguous=full.composer_ambiguous,
                    composer_owner_source=full.composer_owner_source,
                    attachment_count=len(full.attachment_markers),
                    stop_visible=full.stop_visible,
                    transport_active=bool(full.response_activity_turn_id),
                    error_texts=full.error_texts,
                    blocking_dialogs=full.blocking_dialogs,
                    choice_prompt_labels=full.choice_prompt_labels,
                    mcp_permission_allow_count=full.mcp_permission_allow_count,
                    mcp_permission_node_count=full.mcp_permission_node_count,
                    retry_visible=full.retry_visible,
                    last_user_message_id=next((item.message_id for item in reversed(full.messages) if item.role == "user"), None),
                    last_user_turn_id=next((item.turn_id for item in reversed(full.messages) if item.role == "user"), None),
                    last_assistant_message_id=next((item.message_id for item in reversed(full.messages) if item.role == "assistant"), None),
                    last_assistant_turn_id=next((item.turn_id for item in reversed(full.messages) if item.role == "assistant"), None),
                    assistant_text_length=len(next((item.text for item in reversed(full.messages) if item.role == "assistant"), "")),
                    assistant_text_tail=next((item.text[-160:] for item in reversed(full.messages) if item.role == "assistant"), ""),
                    response_activity_length=full.response_activity_length,
                    response_activity_tail=full.response_activity_text[-160:],
                    response_activity_turn_id=full.response_activity_turn_id,
                    last_user_text=next((item.text[:12000] for item in reversed(full.messages) if item.role == "user"), ""),
                    assistant_text=next((item.text[:24000] for item in reversed(full.messages) if item.role == "assistant" and str(item.text or "").strip()), ""),
                    response_activity_text=full.response_activity_text[:24000],
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

    async def read_wait_probe(self) -> WaitProbe:
        return await inspect_chatgpt_wait_probe(self.page)

    async def current_wait_probe(self) -> WaitProbe:
        probe = await self.read_wait_probe()
        self._assert_wait_probe_ownership(probe)
        return probe

    async def install_page_observer(self) -> None:
        from .role_runtime.page_observer import observer_for
        await observer_for(self.page).install()

    def page_observation(self) -> dict[str, Any]:
        from .role_runtime.page_observer import observer_for
        return observer_for(self.page).observation()

    async def wait_for_page_observation(
        self, *, timeout_ms: int = 5_000
    ) -> bool:
        from .role_runtime.page_observer import observer_for
        return await observer_for(self.page).wait(timeout_ms / 1000)

    def clear_permission_action(self) -> None:
        from .role_runtime.page_observer import observer_for
        observer_for(self.page).clear_permission()

    def clear_page_response_state(self) -> None:
        from .role_runtime.page_observer import observer_for
        observer_for(self.page).clear_response_state()

    async def conversation_info(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """G2: optional cached information only; never called by role admission."""
        from .role_runtime.page_observer import observer_for
        observer = observer_for(self.page)
        if observer.info is not None and not refresh:
            return dict(observer.info)
        current = extract_session_id(str(self.page.url))
        if not current or current.startswith("WEB:"):
            return None
        # Bound both successful and failed explicit lookups; repeated inspection
        # must not become a second polling channel.
        if time.monotonic() - observer.info_at < 30:
            return dict(observer.info) if observer.info else None
        observer.info_at = time.monotonic()
        try:
            value = await asyncio.wait_for(_backend_get_object(
                self.page.context, f"/backend-api/conversation/{current}", category="conversation_info"
            ), timeout=10)
        except (BackendAuthError, BackendUnavailableError, BackendSchemaError, TimeoutError):
            return None
        observer.info = dict(value)
        return dict(observer.info)

    async def mcp_allow_visible(self) -> bool:
        return bool(
            await self.page.evaluate(
                r"""() => {
                  const visible = (e) => Boolean(e && getComputedStyle(e).visibility !== 'hidden' &&
                    (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
                  const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim();
                  return [...document.querySelectorAll('button')].some((button) =>
                    visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
                    (norm(button.innerText || button.textContent) === 'Allow' ||
                     /^Allow\b.*\bconversation\b/i.test(norm(button.innerText || button.textContent || button.getAttribute('aria-label'))))
                  ) || [...document.querySelectorAll('[data-codex-approval-surface="true"]')].some((surface) =>
                    visible(surface) && [...surface.querySelectorAll('button')].some((button) =>
                      visible(button) && /^Allow once\b/i.test(norm(button.innerText || button.textContent))) &&
                    [...surface.querySelectorAll('button')].some((button) =>
                      visible(button) && norm(button.getAttribute('aria-label')) === 'Approval options'));
                }"""
            )
        )

    async def auto_allow_mcp_permission(
        self,
        *,
        passive_action: Mapping[str, Any] | None = None,
    ) -> dict[str, str] | None:
        async with self.mutation_guard():
            result = await click_preferred_mcp_allow(
                self.page,
                passive_action=passive_action,
            )
            if result is not None:
                self.invalidate_wait_cache()
            return result



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
            if await cloudflare_challenge_visible(self.page):
                raise RateLimitBlockedError(
                    "Cloudflare challenge blocked ChatGPT page readiness"
                )
            try:
                last_snapshot = await self.assert_ownership()
            except (
                PageOwnershipError,
                AuthenticationRequiredError,
                ComposerConflictError,
                ManualInputPendingError,
                ChoicePromptBlockedError,
            ):
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
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            if isinstance(last_error, (TimeoutError, PlaywrightTimeoutError)):
                raise type(last_error)(
                    "clean-ready check failed after transient timeout: "
                    f"{type(last_error).__name__}: {last_error}"
                ) from last_error
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
        binding_id: str | None = None,
    ) -> ChatGPTSnapshot:
        """Restore an exact closed-tab identity on its reopened conversation page."""
        page_id = str(page_id).strip()
        role = validate_page_role(role)
        task_id = str(task_id).strip()
        team = str(team).strip()
        binding_id = str(binding_id or "").strip() or None
        if binding_id is not None and len(binding_id) > 256:
            raise ValueError("binding_id must be at most 256 characters")
        if not page_id or len(page_id) > 256:
            raise ValueError("page_id must contain 1-256 characters")
        if not task_id or len(task_id) > 256:
            raise ValueError("task_id must contain 1-256 characters")
        if not _TEAM_PATTERN.fullmatch(team):
            raise ValueError("team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        async with self.mutation_guard():
            await self.page.evaluate(
                """([roleKey, pageIdKey, taskIdKey, teamKey, bindingIdKey, windowNamePrefix,
                       role, pageId, taskId, team, bindingId]) => {
                  sessionStorage.setItem(roleKey, role);
                  sessionStorage.setItem(pageIdKey, pageId);
                  sessionStorage.setItem(taskIdKey, taskId);
                  sessionStorage.setItem(teamKey, team);
                  if (bindingId) sessionStorage.setItem(bindingIdKey, bindingId);
                  window.name = windowNamePrefix + JSON.stringify({
                    role,
                    pageId,
                    taskId,
                    team,
                    bindingId: bindingId || sessionStorage.getItem(bindingIdKey) || null,
                  });
                }""",
                [
                    ROLE_STORAGE_KEY,
                    PAGE_ID_STORAGE_KEY,
                    TASK_ID_STORAGE_KEY,
                    TEAM_STORAGE_KEY,
                    BINDING_ID_STORAGE_KEY,
                    WINDOW_NAME_PREFIX,
                    role,
                    page_id,
                    task_id,
                    team,
                    binding_id,
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
        binding_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, str]:
        task_id = str(task_id).strip()
        team = str(team).strip()
        binding_id = str(binding_id or "").strip() or None
        if binding_id is not None and len(binding_id) > 256:
            raise ValueError("binding_id must be at most 256 characters")
        if not task_id or len(task_id) > 256:
            raise ValueError("task_id must contain 1-256 characters")
        if not _TEAM_PATTERN.fullmatch(team):
            raise ValueError("team must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self.assert_ownership()
            if (
                snapshot.page_task_id == task_id
                and snapshot.page_team == team
                and binding_id is None
            ):
                return {"task_id": task_id, "team": team}
            if snapshot.manual_input_pending:
                raise ComposerConflictError(
                    "task/team binding blocked by manual draft or attachments"
                )
            self._assert_interaction_safe(snapshot)
            write_result = await self.page.evaluate(
                """([taskIdKey, taskId, teamKey, team, bindingIdKey, bindingId, windowNamePrefix]) => {
                  const visible = (element) => Boolean(
                    element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                  );
                  const firstVisible = (selector) =>
                    [...document.querySelectorAll(selector)].find(visible) || null;
                  const owner = (__CDPA_COMPOSER_RESOLVER__)();
                  const composer = owner.element;
                  if (!composer) {
                    return {
                      written: false,
                      reason: owner.ambiguous ? 'composer_ambiguous' : 'composer_unavailable',
                    };
                  }
                  if (firstVisible('button[data-testid="stop-button"], button[aria-label*="Stop"]')) {
                    return {written: false, reason: 'active_response'};
                  }
                  if (firstVisible('[role="dialog"]:not(#playwright-auto-role-control-v1), [data-testid^="modal-"]')) {
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
                  if (bindingId) sessionStorage.setItem(bindingIdKey, bindingId);
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
                    bindingId: bindingId || sessionStorage.getItem(bindingIdKey) || null,
                  });
                  return {written: true};
                }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS),
                [
                    TASK_ID_STORAGE_KEY,
                    task_id,
                    TEAM_STORAGE_KEY,
                    team,
                    BINDING_ID_STORAGE_KEY,
                    binding_id,
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
            if reason == "composer_ambiguous":
                raise ComposerConflictError("composer ownership is ambiguous")
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
        if snapshot.requires_login or snapshot.state is ChatGPTState.AUTH_REQUIRED:
            raise AuthenticationRequiredError("ChatGPT sign-in is required")
        if snapshot.state is ChatGPTState.ERROR and not (
            snapshot.retry_visible and not snapshot.error_texts
        ):
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
        discard_attachments: bool = False,
        timeout_ms: int | None = None,
    ) -> None:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            snapshot = await self._interaction_snapshot(
                timeout_ms=timeout,
                allow_attachments=discard_attachments,
            )
            existing = snapshot.composer_text.strip()
            if expected_text is not None and not visible_text_matches(existing, expected_text):
                raise ComposerConflictError(
                    "composer no longer matches the text expected by this workflow"
                )
            if existing and not force and not visible_text_matches(existing, self._owned_composer_text):
                raise ComposerConflictError(
                    "composer contains unowned/manual input; refusing to clear"
                )
            if discard_attachments and snapshot.attachment_markers:
                removed = await self.page.evaluate(
                    """() => {
                      const visible = (element) => Boolean(
                        element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
                      );
                      const owner = (__CDPA_COMPOSER_RESOLVER__)();
                      if (owner.ambiguous) return -1;
                      const composer = owner.element;
                      const host = composer?.closest('form') || composer?.parentElement || null;
                      if (!host) return 0;
                      let removed = 0;
                      for (const element of host.querySelectorAll('button,[role="button"]')) {
                        if (!visible(element)) continue;
                        const label = [
                          element.getAttribute?.('aria-label') || '',
                          element.getAttribute?.('data-testid') || '',
                          element.innerText || '',
                        ].join(' ').toLowerCase();
                        if (label.includes('remove file') || label.includes('remove attachment')) {
                          element.click();
                          removed += 1;
                        }
                      }
                      return removed;
                    }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS)
                )
                if int(removed or 0) < 0:
                    raise ComposerConflictError("composer ownership is ambiguous")
                if not int(removed or 0):
                    raise ComposerConflictError("composer attachments could not be discarded")
            if existing:
                await clear_composer(self.page, timeout_ms=timeout)
            post = await self.assert_ownership()
            if not post.composer_empty or (discard_attachments and post.attachment_markers):
                raise ComposerConflictError("composer did not become empty")
            self._owned_composer_text = None

    async def _clear_exact_owned_composer_locked(
        self, expected_text: str, *, timeout_ms: int
    ) -> bool:
        snapshot = await self.assert_ownership()
        existing = snapshot.composer_text.strip()
        if not existing or not visible_text_matches(existing, expected_text):
            return False
        await set_composer_text(self.page, "", timeout_ms=timeout_ms)
        post = await self.assert_ownership()
        if post.composer_text.strip():
            raise ComposerConflictError("owned composer did not become empty")
        self._owned_composer_text = None
        return True

    async def clear_exact_owned_composer(
        self, expected_text: str, *, timeout_ms: int | None = None
    ) -> bool:
        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            return await self._clear_exact_owned_composer_locked(
                expected_text, timeout_ms=timeout
            )

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
        reason: str | None = None,
    ) -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        timeout = timeout_ms or self.timeout_ms
        async with self.mutation_guard():
            before = await self.assert_ownership(require_binding=self.binding is not None)
            if before.stop_visible and not allow_responding:
                raise UnsafePageStateError("refusing to refresh an active response")
            if before.manual_input_pending:
                raise ComposerConflictError("refresh would discard a manual draft or attachments")
            observation_timeout = max(min(timeout, 2_000), 1) / 1000
            before_document = await asyncio.wait_for(
                self.page.evaluate(
                    """() => ({
                        url: location.href,
                        time_origin: performance.timeOrigin,
                        ready_state: document.readyState
                    })"""
                ),
                timeout=observation_timeout,
            )
            refresh_event = await record_page_action(
                self.page, "refresh", "start", reason=reason,
                identity={"page_url": before.url, "page_id": before.page_id,
                          "role": before.page_role, "task_id": before.page_task_id,
                          "team": before.page_team},
            )
            committed_reload = False
            try:
                await refresh_page(
                    self.page, timeout_ms=timeout, reason=reason, identity=refresh_event,
                )
            except (TimeoutError, PlaywrightTimeoutError) as reload_error:
                async def verify_committed_reload() -> bool:
                    after = await self.assert_ownership(
                        require_binding=self.binding is not None
                    )
                    after_document = await self.page.evaluate(
                        """() => ({
                            url: location.href,
                            time_origin: performance.timeOrigin,
                            ready_state: document.readyState
                        })"""
                    )
                    exact_identity = (
                        after.page_id == before.page_id
                        and after.page_role == before.page_role
                        and after.page_task_id == before.page_task_id
                        and after.page_team == before.page_team
                        and after.session_id == before.session_id
                        and after.url == before.url
                    )
                    before_origin = (
                        before_document.get("time_origin")
                        if isinstance(before_document, Mapping)
                        else None
                    )
                    after_origin = (
                        after_document.get("time_origin")
                        if isinstance(after_document, Mapping)
                        else None
                    )
                    origins_valid = (
                        isinstance(before_origin, (int, float))
                        and not isinstance(before_origin, bool)
                        and math.isfinite(float(before_origin))
                        and isinstance(after_origin, (int, float))
                        and not isinstance(after_origin, bool)
                        and math.isfinite(float(after_origin))
                    )
                    committed = (
                        origins_valid
                        and after_document.get("url") == before_document.get("url")
                        and after_origin != before_origin
                    )
                    usable = (
                        str(after_document.get("ready_state") or "")
                        in {"interactive", "complete"}
                        and not after.requires_login
                        and (
                            after.composer_present
                            or after.stop_visible
                            or bool(after.messages)
                            or after.choice_prompt_pending
                        )
                    )
                    return exact_identity and committed and usable

                try:
                    verified = await asyncio.wait_for(
                        verify_committed_reload(), timeout=observation_timeout
                    )
                except (PageOwnershipError, AuthenticationRequiredError) as post_error:
                    await record_page_action(
                        self.page, "refresh", "error", reason=reason,
                        identity=refresh_event, detail=type(post_error).__name__,
                    )
                    raise reload_error
                except Exception as post_error:
                    await record_page_action(
                        self.page, "refresh", "error", reason=reason,
                        identity=refresh_event, detail=type(post_error).__name__,
                    )
                    raise reload_error from post_error
                if not verified:
                    await record_page_action(
                        self.page, "refresh", "error", reason=reason,
                        identity=refresh_event, detail="reload_commit_not_verified",
                    )
                    raise reload_error
                committed_reload = True
            except Exception as exc:
                await record_page_action(
                    self.page, "refresh", "error", reason=reason,
                    identity=refresh_event, detail=type(exc).__name__,
                )
                raise
            else:
                try:
                    await self.assert_ownership(require_binding=self.binding is not None)
                except Exception as exc:
                    await record_page_action(
                        self.page, "refresh", "error", reason=reason,
                        identity=refresh_event, detail=type(exc).__name__,
                    )
                    raise
            await record_page_action(
                self.page, "refresh", "complete", reason=reason, identity=refresh_event,
                detail="verified_committed_reload" if committed_reload else "verified_ownership",
            )
            self._owned_composer_text = None

    async def _wait_send_acceptance(
        self,
        baseline: MessageBaseline,
        pre_send: ChatGPTSnapshot,
        *,
        timeout_ms: int,
        expected_prompt: str | None = None,
    ) -> tuple[str, MessageSnapshot | None] | None:
        deadline = time.monotonic() + timeout_ms / 1000
        accepted_user: MessageSnapshot | None = None
        last_snapshot = pre_send
        fallback_probe_ms = 250

        def accepted(
            via: str, user: MessageSnapshot | None
        ) -> tuple[str, MessageSnapshot | None]:
            record_send_acceptance(
                SignalIdentity.from_snapshot(last_snapshot),
                last_snapshot,
                outcome="accepted",
                accepted_via=via,
            )
            return via, user

        while True:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                record_send_acceptance(
                    SignalIdentity.from_snapshot(last_snapshot),
                    last_snapshot,
                    outcome="timeout",
                )
                return None
            wait_ms = max(1, min(fallback_probe_ms, math.ceil(remaining_seconds * 1000)))
            await self.wait_for_page_observation(timeout_ms=wait_ms)
            snapshot = await self.assert_ownership()
            last_snapshot = snapshot
            candidate = unique_new_user_message(snapshot.messages, baseline)
            # Preserve exact user-turn acceptance when it is available. Response-bound
            # DOM evidence also proves the confirmed click was accepted without requiring
            # prompt text or backend/listener identity.
            if candidate is not None:
                if expected_prompt is None or rendered_user_text_matches(candidate.text, expected_prompt):
                    accepted_user = candidate
                    return accepted("exact_user_message", accepted_user)
            # A fresh post-click DOM signal confirms dispatch. Never reuse a
            # Stop control or assistant turn that existed before this click.
            if snapshot.stop_visible and not pre_send.stop_visible:
                return accepted("stop_button", None)
            previous_assistants = {
                message.message_id for message in pre_send.messages
                if message.role == "assistant"
            }
            if any(
                message.role == "assistant"
                and message.message_id
                and message.message_id not in previous_assistants
                for message in snapshot.messages
            ):
                return accepted("assistant_message", None)
            if (
                snapshot.response_activity_turn_id
                and snapshot.response_activity_turn_id != pre_send.response_activity_turn_id
            ):
                return accepted("response_activity_turn", None)
            if (
                snapshot.response_activity_text
                and snapshot.response_activity_text != pre_send.response_activity_text
            ) or (
                snapshot.response_activity_structure
                and snapshot.response_activity_structure != pre_send.response_activity_structure
            ):
                return accepted("assistant_activity", None)
            # A generic observer wake or a stale LISTEN stream is not evidence.

    async def _prepare_prompt_locked(
        self,
        prompt: str,
        timeout_ms: int,
        *,
        expected_attachment_count: int = 0,
        expected_attachment_names: Sequence[str] | None = None,
        stable_seconds: float = 5.0,
    ) -> None:
        expected_names, expected_count = _expected_attachment_contract(
            expected_attachment_names, expected_attachment_count
        )
        deadline = time.monotonic() + timeout_ms / 1000
        stable_since: float | None = None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("composer did not remain stable before send")
            snapshot = await self._interaction_snapshot(
                timeout_ms=timeout_ms,
                allow_attachments=expected_count > 0,
            )
            _assert_expected_attachment_markers(
                snapshot.attachment_markers,
                expected_names=expected_names,
                expected_count=expected_count,
                message="attachment set changed before send",
            )
            existing = snapshot.composer_text.strip()
            if not existing:
                await set_composer_text(self.page, prompt, timeout_ms=timeout_ms)
                stable_since = None
                continue
            if not visible_text_matches(existing, prompt):
                raise ComposerConflictError(
                    "composer changed or contains manual input before send"
                )
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            remaining = stable_seconds - (now - stable_since)
            if remaining > 0:
                await asyncio.sleep(min(0.1, remaining))
                continue
            if not snapshot.send_visible or not snapshot.send_enabled:
                raise UnsafePageStateError("Send button is not visible and enabled")
            self._owned_composer_text = prompt
            return

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
            baseline = capture_message_baseline(before.messages, before.logical_turn_ids)
            last_error: BaseException | None = None
            dispatched_clicks = 0
            click_outcome_ambiguous = False

            for attempt in range(1, max_attempts + 1):
                try:
                    await self._prepare_prompt_locked(
                        prompt,
                        timeout,
                        expected_attachment_count=expected_count,
                        expected_attachment_names=expected_names,
                    )
                    pre_send = await self.assert_ownership()
                    baseline = capture_message_baseline(
                        pre_send.messages, pre_send.logical_turn_ids
                    )
                    # Reset LISTEN generation state immediately before this one Send.
                    try:
                        await self.install_page_observer()
                        self.clear_page_response_state()
                    except Exception:
                        pass
                    # Send acceptance is based on fresh DOM evidence only.
                    # Ownership, safe page state, prompt, attachments, and click share one callback.
                    try:
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
                    except Exception:
                        click_outcome_ambiguous = True
                        raise
                    dispatched_clicks += 1
                    self._owned_composer_text = None
                    acceptance_timeout_ms = 60_000
                    acceptance = await self._wait_send_acceptance(
                        baseline,
                        pre_send,
                        timeout_ms=acceptance_timeout_ms,
                        expected_prompt=prompt,
                    )
                    if acceptance is None:
                        raise SendRecoveryError(
                            "send click produced no response-bound DOM evidence within "
                            f"{acceptance_timeout_ms / 1000:g} seconds"
                        )
                    accepted_via, accepted_user = acceptance
                    try:
                        await self._clear_exact_owned_composer_locked(
                            prompt, timeout_ms=min(timeout, 5_000)
                        )
                    except Exception:
                        # Send is already proven; stale-composer cleanup must not
                        # turn a running role back into a send failure.
                        pass
                    # Response-bound DOM evidence already proves this one click was accepted.
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
                    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

                    # Already under mutation_guard: calling refresh() would deadlock.
                    # A timed-out reload may have committed; reconcile only exact
                    # acceptance in that new document, never authorize another click.
                    recovery_before = await self.assert_ownership()
                    recovered_text = normalize_visible_text(recovery_before.composer_text)
                    if recovered_text not in {"", normalize_visible_text(prompt)} or recovery_before.attachment_markers:
                        raise ComposerConflictError(
                            "reload would discard manual input or attachments"
                        ) from exc
                    probe = """() => ({url: location.href,
                        time_origin: performance.timeOrigin,
                        ready_state: document.readyState})"""
                    probe_timeout = max(min(timeout, 2_000), 1) / 1000
                    document_before = await asyncio.wait_for(
                        self.page.evaluate(probe), timeout=probe_timeout
                    )
                    try:
                        await refresh_page(
                            self.page, timeout_ms=timeout, reason="send_recovery",
                            identity={"page_url": recovery_before.url,
                                      "page_id": recovery_before.page_id,
                                      "role": recovery_before.page_role,
                                      "task_id": recovery_before.page_task_id,
                                      "team": recovery_before.page_team},
                        )
                    except (TimeoutError, PlaywrightTimeoutError) as reload_error:
                        async def reconcile_committed_acceptance():
                            recovered = await self.assert_ownership()
                            document_after = await self.page.evaluate(probe)
                            if not isinstance(document_before, Mapping) or not isinstance(document_after, Mapping):
                                return None
                            origins = (document_before.get("time_origin"), document_after.get("time_origin"))
                            if not all(
                                isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(float(value)) for value in origins
                            ) or origins[0] == origins[1]:
                                return None
                            if (document_before.get("url") != recovery_before.url
                                    or document_after.get("url") != recovered.url
                                    or document_after.get("ready_state") not in {"interactive", "complete"}
                                    or recovered.requires_login):
                                return None
                            if any(
                                getattr(recovered, field) != getattr(before, field)
                                or getattr(recovery_before, field) != getattr(before, field)
                                for field in ("page_id", "page_role", "page_task_id", "page_team")
                            ):
                                return None
                            candidate = unique_new_user_message(recovered.messages, baseline)
                            if candidate is None or not rendered_user_text_matches(candidate.text, prompt):
                                return None
                            # Only the existing provisional new-chat identity may
                            # canonicalize, and only with the exact new user turn.
                            for prior in (before, recovery_before):
                                exact = recovered.url == prior.url and recovered.session_id == prior.session_id
                                canonicalized = (
                                    str(prior.session_id or "").startswith("WEB:")
                                    and urlparse(prior.url).hostname == urlparse(recovered.url).hostname
                                    and urlparse(prior.url).path in {"", "/"}
                                    and urlparse(recovered.url).path.startswith("/c/")
                                    and recovered.session_id == extract_session_id(recovered.url)
                                )
                                if not (exact or canonicalized):
                                    return None
                            return recovered, ("exact_user_message", candidate)

                        try:
                            reconciled = await asyncio.wait_for(
                                reconcile_committed_acceptance(), timeout=probe_timeout
                            )
                        except Exception as post_error:
                            raise reload_error from post_error
                        if reconciled is None:
                            raise reload_error
                        recovered, acceptance = reconciled
                    else:
                        recovered = await self.assert_ownership()
                        candidate = unique_new_user_message(recovered.messages, baseline)
                        acceptance = (
                            ("exact_user_message", candidate)
                            if candidate is not None
                            and rendered_user_text_matches(candidate.text, prompt)
                            else None
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
                    # A missing post-reload user turn is not proof of rejection.
                    # Preserve ambiguity for durable reconciliation, never click twice.
                    break

            if click_outcome_ambiguous:
                boundary = "send click dispatch outcome is ambiguous"
            elif dispatched_clicks:
                boundary = f"send acceptance is ambiguous after {dispatched_clicks} confirmed click(s)"
            else:
                boundary = "send failed before any click was dispatched"
            raise SendRecoveryError(
                f"{boundary}; no resend occurred: "
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
                  const owner = (__CDPA_COMPOSER_RESOLVER__)();
                  if (owner.ambiguous) {
                    return {clicked: false, reason: 'composer_ambiguous'};
                  }
                  const composer = owner.element;
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
                  if ([...document.querySelectorAll('[role="dialog"]:not(#playwright-auto-role-control-v1), [data-testid^="modal-"]')].some(visible)) {
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
                }""".replace('__CDPA_COMPOSER_RESOLVER__', _COMPOSER_RESOLVER_JS),
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
                if reason in {"composer_conflict", "composer_ambiguous"}:
                    raise ComposerConflictError(
                        "Retry generation blocked by manual draft, attachments, or ambiguous composer ownership"
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
                transport_active = bool(current.stop_visible)
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
        bound_assistant_turn_id = str(expected_assistant_turn_id or "").strip()
        bound_assistant_message_id = str(expected_assistant_message_id or "").strip()

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

            if receipt.user_message_id or receipt.user_turn_id:
                candidate, binding_error = bound_assistant_for_receipt(
                    snapshot.messages,
                    receipt,
                    assistant_message_id=bound_assistant_message_id or None,
                    assistant_turn_id=bound_assistant_turn_id or None,
                    allow_user_steering=True,
                )
                if binding_error is not None:
                    raise SendRecoveryError(
                        f"exact response binding failed: {binding_error}"
                    )
                if candidate is not None:
                    old_bound_identity = bound_assistant_turn_id or bound_assistant_message_id
                    new_bound_identity = str(candidate.turn_id or candidate.message_id or "")
                    if (
                        old_bound_identity
                        and new_bound_identity
                        and old_bound_identity != new_bound_identity
                    ):
                        candidate_fingerprint = ""
                        candidate_since = None
                        candidate_samples = 0
                    bound_assistant_turn_id = str(candidate.turn_id or "")
                    bound_assistant_message_id = str(candidate.message_id or "")
            else:
                # Legacy receipts predate exact user identity; preserve their old
                # read path without using it for new accepted operations.
                from .role_runtime.response import current_assistant
                candidate = current_assistant(snapshot.messages, receipt.baseline)

            current_fingerprint = message_fingerprint(candidate)
            bound_activity = bool(
                candidate is not None
                and candidate.turn_id
                and snapshot.response_activity_turn_id
                and candidate.turn_id == snapshot.response_activity_turn_id
            )
            transport_ui_active = bool(
                bound_activity
                or (
                    candidate is not None
                    and response_transport_ui_active(snapshot)
                    and not candidate.actions
                )
            )

            candidate_present = candidate is not None
            if candidate_present and response_is_stale(candidate, recovery_baseline):
                saw_stale_assistant = True
                candidate_present = False

            if candidate_present and candidate is not None:
                if current_fingerprint != candidate_fingerprint:
                    if candidate_fingerprint and snapshot.stop_visible:
                        active_since = now
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

                completed_action = any(
                    "copy" in str(action).lower()
                    or "feedback" in str(action).lower()
                    or "regenerate" in str(action).lower()
                    for action in candidate.actions
                )
                exact_receipt = bool(receipt.user_message_id or receipt.user_turn_id)
                if (
                    not incomplete
                    and validation_error is None
                    and not transport_ui_active
                    and (completed_action or not exact_receipt)
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
                    await refresh_page(
                        self.page, timeout_ms=timeout, reason="legacy_active_response_stall",
                        identity={"page_url": snapshot.url, "page_id": snapshot.page_id,
                                  "role": snapshot.page_role, "task_id": snapshot.page_task_id,
                                  "team": snapshot.page_team},
                    )
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
