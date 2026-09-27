"""ChatGPT runtime value objects and pure response correlation helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .chatgpt_graph import normalize_visible_text

_ROLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


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
            "exact_user_message",  # legacy receipt
            "user_message_identity",  # legacy receipt
            "listen_streaming",  # legacy receipt; never emitted by current Send
            "stop_button",  # legacy receipt
            "assistant_message",
            "response_activity_turn",
            "assistant_activity",
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
    mcp_permission_allow_count: int = 0
    mcp_permission_node_count: int = 0
    page_task_id: str | None = None
    page_team: str | None = None
    response_activity_text: str = ""
    response_activity_structure: str = ""
    response_activity_turn_id: str | None = None
    response_activity_length: int = 0
    composer_ambiguous: bool = False
    composer_owner_source: str = ""

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
            "composer_ambiguous": self.composer_ambiguous,
            "composer_owner_source": self.composer_owner_source,
            "composer_empty": self.composer_empty,
            "send_visible": self.send_visible,
            "send_enabled": self.send_enabled,
            "stop_visible": self.stop_visible,
            "blocking_dialogs": list(self.blocking_dialogs),
            "attachment_markers": list(self.attachment_markers),
            "choice_prompt_pending": self.choice_prompt_pending,
            "choice_prompt_labels": list(self.choice_prompt_labels),
            "retry_visible": self.retry_visible,
            "mcp_permission_allow_count": self.mcp_permission_allow_count,
            "mcp_permission_node_count": self.mcp_permission_node_count,
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
    retry_visible: bool = False
    last_user_text: str = ""
    assistant_text: str = ""
    response_activity_text: str = ""
    composer_ambiguous: bool = False
    composer_owner_source: str = ""

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
    if raw.get("requires_login"):
        return ChatGPTState.AUTH_REQUIRED
    # A failed generation can coexist with a usable composer. Retry is an
    # interruption for the role controller, not a fatal browser state.
    if raw.get("error_present") and (
        not raw.get("retry_visible") or raw.get("error_texts")
    ):
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
