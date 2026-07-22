from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .observability import record_page_action
from .role_indicator import WINDOW_NAME_PREFIX, ensure_role_indicator

ROLE_STORAGE_KEY = "playwright-auto:role"
PAGE_ID_STORAGE_KEY = "playwright-auto:page-id"
TASK_ID_STORAGE_KEY = "playwright-auto:task-id"
TEAM_STORAGE_KEY = "playwright-auto:team"

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


def normalize_visible_text(value: Any) -> str:
    """Canonicalize browser-visible whitespace without changing non-whitespace text."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def visible_text_matches(actual: Any, expected: Any) -> bool:
    return normalize_visible_text(actual) == normalize_visible_text(expected)


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
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "making requests too quickly",
    "temporarily limited access",
)


def _page_lock(registry: weakref.WeakKeyDictionary[Any, asyncio.Lock], page: Any) -> asyncio.Lock:
    lock = registry.get(page)
    if lock is None:
        lock = asyncio.Lock()
        registry[page] = lock
    return lock


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
    page_task_id: str | None = None
    page_team: str | None = None
    response_activity_text: str = ""
    response_activity_structure: str = ""
    response_activity_turn_id: str | None = None

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
            "manual_input_pending": self.manual_input_pending,
            "image_count": self.image_count,
            "error_texts": list(self.error_texts),
            "messages": [message.to_dict() for message in self.messages],
        }


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


def receipt_user_message_seen(
    messages: Sequence[MessageSnapshot], receipt: SendReceipt
) -> bool:
    candidates = [
        message
        for message in messages
        if message.role == "user"
        and message.message_id not in receipt.baseline.user_message_ids
        and message.message_id not in receipt.baseline.message_ids
    ]
    message_id = str(receipt.user_message_id or "").strip()
    turn_id = str(receipt.user_turn_id or "").strip()
    if message_id or turn_id:
        return any(
            (bool(message_id) and message.message_id == message_id)
            or (bool(turn_id) and message.turn_id == turn_id)
            for message in candidates
        )
    return exact_prompt_seen(messages, receipt.baseline, receipt.prompt)


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
    latest = assistants[-1].text.casefold() if assistants else ""
    activity = str(getattr(snapshot, "response_activity_text", "") or "").casefold()
    return any(
        marker in latest or marker in activity
        for marker in _TRANSIENT_RESPONSE_MARKERS
    )


def response_activity_signature(
    snapshot: ChatGPTSnapshot,
    baseline: MessageBaseline,
) -> tuple[str, int]:
    assistants = new_assistant_turns(snapshot.messages, baseline)
    latest = assistants[-1] if assistants else None
    activity_text = str(getattr(snapshot, "response_activity_text", "") or "")
    activity_structure = str(
        getattr(snapshot, "response_activity_structure", "") or ""
    )
    activity_turn_id = str(
        getattr(snapshot, "response_activity_turn_id", "") or ""
    )
    length = max(
        len(latest.text) if latest is not None else 0,
        len(activity_text),
    )
    state = getattr(snapshot, "state", ChatGPTState.UNKNOWN)
    state_value = state.value if isinstance(state, ChatGPTState) else str(state)
    payload = "\0".join(
        (
            message_fingerprint(latest),
            str(length),
            activity_turn_id,
            activity_text,
            activity_structure,
            state_value,
            "1" if bool(getattr(snapshot, "stop_visible", False)) else "0",
            "\n".join(tuple(getattr(snapshot, "error_texts", ()) or ())),
            "\n".join(tuple(getattr(snapshot, "blocking_dialogs", ()) or ())),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), length


def looks_incomplete_response(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if re.fullmatch(r"(?is)(?:thinking|analyzing|working)(?:\.{3}|…)?", value):
        return True
    lowered = value.casefold()
    if any(marker in lowered for marker in _TRANSIENT_RESPONSE_MARKERS):
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


async def click_send_button(page: Any, timeout_ms: int = 8_000) -> str:
    await action_delay(page, "send", SEND_DELAY_MULTIPLIER)
    await record_page_action(page, "send", "click")
    result = await page.evaluate(
        r"""() => {
          const visible = (element) => Boolean(
            element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
          );
          const enabled = (element) => Boolean(
            element && !element.disabled && element.getAttribute('aria-disabled') !== 'true'
          );
          const composer = [...document.querySelectorAll(
            'div#prompt-textarea, [data-testid="composer"] [contenteditable="true"], form [contenteditable="true"], [contenteditable="true"][role="textbox"]'
          )].find((element) => visible(element) && enabled(element)) || null;
          const root = composer?.closest('form') || composer?.closest('[data-testid="composer"]') || document;
          const candidates = [...new Set([
            ...root.querySelectorAll('button,[role="button"]'),
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
            .sort((a, b) => b.score - a.score);
          const target = candidates[0]?.button || null;
          if (!target) return {ok: false, method: 'not_found'};
          try {
            target.focus();
            target.click();
            return {ok: true, method: 'dom_click'};
          } catch (error) {
            const form = composer?.closest('form') || null;
            if (form && typeof form.requestSubmit === 'function') {
              form.requestSubmit(target);
              return {ok: true, method: 'form_request_submit'};
            }
            return {ok: false, method: 'click_failed', error: String(error)};
          }
        }"""
    )
    if not result.get("ok"):
        method = str(result.get("method") or "unknown")
        await record_page_action(page, "send", "error", detail=method)
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
          const positive = [
            'continue', 'proceed', 'start', 'yes', 'ok', 'okay', 'accept',
            'approve', 'allow', 'run', 'go ahead', 'make a plan',
            'create plan', 'use plan'
          ];
          const negative = [
            'cancel', 'stop', 'not now', 'no thanks', 'dismiss', 'close',
            'delete', 'remove', 'archive', 'share', 'copy'
          ];
          const candidates = [...document.querySelectorAll('button,[role="button"]')]
            .map((button) => {
              const label = [
                button.innerText || button.textContent || '',
                button.getAttribute('aria-label') || '',
                button.getAttribute('data-testid') || ''
              ].join(' ').replace(/\\s+/g, ' ').trim();
              const lower = label.toLowerCase();
              return {button, label, lower};
            })
            .filter(({button, label, lower}) =>
              label && visible(button) && !button.disabled &&
              button.getAttribute('aria-disabled') !== 'true' &&
              positive.some((marker) => lower.includes(marker)) &&
              !negative.some((marker) => lower.includes(marker))
            );
          const target = candidates[0] || null;
          if (!target) return {ok: false, label: ''};
          target.button.click();
          return {ok: true, label: target.label};
        }"""
    )
    if not result.get("ok"):
        raise ChoicePromptBlockedError("no safe positive choice prompt is clickable")
    return str(result.get("label") or "safe choice")


async def send_prompt(
    page: Any, text: str, timeout_ms: int = 8_000, wait_for_stop: bool = True
) -> None:
    if not text.strip():
        raise ValueError("prompt must not be empty")
    await set_composer_text(page, text, timeout_ms=timeout_ms)
    await click_send_button(page, timeout_ms=timeout_ms)
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


async def inspect_chatgpt_page(page: Any) -> ChatGPTSnapshot:
    raw = await page.evaluate(
        r"""
        ([roleKey, pageIdKey, taskIdKey, teamKey, windowNamePrefix]) => {
          const visible = (element) => Boolean(
            element && (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
          );
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
          const attachmentLabel = (element) => [
            text(element),
            element?.getAttribute?.('aria-label') || '',
            element?.getAttribute?.('data-testid') || '',
          ].join(' ').replace(/\\s+/g, ' ').trim();
          const isRealAttachment = (element) => {
            const label = attachmentLabel(element).toLowerCase();
            if (!label || label.includes('composer-plus-btn') || label.includes('add files and more')) {
              return false;
            }
            return [
              'remove file', 'open image', 'attached', 'file uploaded',
              'uploading', 'remove attachment'
            ].some((marker) => label.includes(marker));
          };
          const attachmentMarkers = composerHost
            ? [...composerHost.querySelectorAll(
                '[data-testid*="attachment"], [data-testid*="file"], button[aria-label], [role="button"][aria-label]'
              )]
                .filter((element) => visible(element) && isRealAttachment(element))
                .map(attachmentLabel)
                .filter(Boolean)
            : [];

          const positiveChoiceMarkers = [
            'continue', 'proceed', 'start', 'yes', 'ok', 'okay', 'accept',
            'approve', 'allow', 'run', 'go ahead', 'make a plan',
            'create plan', 'use plan'
          ];
          const negativeChoiceMarkers = [
            'cancel', 'stop', 'not now', 'no thanks', 'dismiss', 'close',
            'delete', 'remove', 'archive', 'share', 'copy'
          ];
          const choicePromptLabels = composer ? [] : [...document.querySelectorAll('button,[role="button"]')]
            .filter((button) => visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true')
            .map((button) => attachmentLabel(button))
            .filter((label) => {
              const lower = label.toLowerCase();
              return label && positiveChoiceMarkers.some((marker) => lower.includes(marker)) &&
                !negativeChoiceMarkers.some((marker) => lower.includes(marker));
            })
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
            attachment_markers: [...new Set(attachmentMarkers)],
            choice_prompt_labels: [...new Set(choicePromptLabels)],
            error_present: Boolean(retry || authCallbackError || alertError),
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

    @asynccontextmanager
    async def workflow_guard(self):
        async with _page_lock(_PAGE_WORKFLOW_LOCKS, self.page):
            yield

    @asynccontextmanager
    async def mutation_guard(self):
        async with _page_lock(_PAGE_MUTATION_LOCKS, self.page):
            yield

    async def snapshot(self) -> ChatGPTSnapshot:
        return await inspect_chatgpt_page(self.page)

    async def assert_ownership(
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
            if snapshot.stop_visible:
                if not stop_first:
                    raise UnsafePageStateError(
                        "response is active; stop it explicitly before New chat"
                    )
                await stop_response(self.page, timeout_ms=timeout)
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
    ) -> None:
        snapshot = await self._interaction_snapshot(
            timeout_ms=timeout_ms,
            allow_attachments=expected_attachment_count > 0,
        )
        if len(snapshot.attachment_markers) != expected_attachment_count:
            raise ComposerConflictError(
                f"expected {expected_attachment_count} attachment(s), "
                f"found {len(snapshot.attachment_markers)}"
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
            allow_attachments=expected_attachment_count > 0,
        )
        if len(fresh.attachment_markers) != expected_attachment_count:
            raise ComposerConflictError(
                f"attachment count changed before send: expected "
                f"{expected_attachment_count}, found {len(fresh.attachment_markers)}"
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
        expected_attachment_count: int = 0,
    ) -> SendReceipt:
        prompt = text.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        if expected_attachment_count < 0:
            raise ValueError("expected_attachment_count must not be negative")
        timeout = timeout_ms or self.timeout_ms

        async with self.mutation_guard():
            before = await self.assert_ownership()
            self._assert_interaction_safe(before)
            assert self.binding is not None
            baseline = capture_message_baseline(before.messages)
            last_error: BaseException | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    await self._prepare_prompt_locked(
                        prompt,
                        timeout,
                        expected_attachment_count=expected_attachment_count,
                    )
                    # Fresh exact checks above intentionally precede the real click.
                    await click_send_button(self.page, timeout_ms=timeout)
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
                    )
                except (ComposerConflictError, PageOwnershipError):
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
                        )
                    recovered_text = normalize_visible_text(recovered.composer_text)
                    if recovered_text not in {"", normalize_visible_text(prompt)}:
                        raise ComposerConflictError(
                            "manual/unowned composer input appeared during send recovery"
                        ) from exc
                    if len(recovered.attachment_markers) != expected_attachment_count:
                        raise ComposerConflictError(
                            "attachment set changed during send recovery"
                        ) from exc

            raise SendRecoveryError(
                f"send failed after {max_attempts} exact attempt(s): "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error

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

        while time.monotonic() < deadline:
            try:
                snapshot = await self.assert_ownership()
            except PageOwnershipError:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(poll_ms / 1000)
                continue

            now = time.monotonic()
            last_snapshot = snapshot
            limited = rate_limit_dialogs(snapshot)
            if limited:
                raise RateLimitBlockedError(
                    f"request rate limit is active while waiting for response: {list(limited)!r}"
                )
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
            else:
                activity_samples += 1

            if choice_prompt_pending:
                if resolve_choice_prompt:
                    await self.resolve_choice_prompt(timeout_ms=min(timeout, 10_000))
                    choice_prompt_pending = False
                    candidate_fingerprint = ""
                    candidate_since = None
                    candidate_samples = 0
                    continue
                await asyncio.sleep(poll_ms / 1000)
                continue

            if manual_input_pending:
                await asyncio.sleep(poll_ms / 1000)
                continue

            user_provenance = receipt_user_message_seen(snapshot.messages, receipt)
            assistants = new_assistant_turns(snapshot.messages, receipt.baseline)
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
                    await self.assert_ownership()
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

            await asyncio.sleep(poll_ms / 1000)

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
    ) -> Any:
        from .upload import upload_files

        return await upload_files(
            self,
            paths,
            request_marker=request_marker,
            timeout_ms=timeout_ms or self.timeout_ms,
            max_total_bytes=max_total_bytes,
        )

    async def wait_upload_ready(
        self,
        *,
        request_marker: str,
        expected_count: int,
        timeout_ms: int | None = None,
        poll_ms: int = 100,
    ) -> ChatGPTSnapshot:
        from .upload import wait_upload_ready

        return await wait_upload_ready(
            self,
            request_marker=request_marker,
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
