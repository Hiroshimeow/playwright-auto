from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class BackendError(RuntimeError):
    """Base class for safe backend-read failures."""


class BackendAuthError(BackendError):
    """The existing browser session could not supply valid backend auth."""


class BackendUnavailableError(BackendError):
    def __init__(self, status_code: int, category: str = "backend") -> None:
        self.status_code = int(status_code)
        self.category = str(category)
        super().__init__(f"{self.category} unavailable (HTTP {self.status_code})")


class BackendNotReadyError(BackendError):
    """Requested backend state is not materialized yet."""


class BackendSchemaError(BackendError):
    """Backend payload shape is incompatible with the exact resolver contract."""


class GraphIdentityError(BackendError):
    """Durable accepted-user identity is ambiguous or belongs to another branch."""


@dataclass(frozen=True)
class ResolvedAssistant:
    message_id: str
    text: str
    content_type: str


def _exact_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BackendSchemaError(f"graph {label} must be a bounded non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BackendSchemaError(f"graph {label} contains control characters")
    return value


def _graph_node(mapping: Mapping[str, Any], node_id: str) -> Mapping[str, Any]:
    raw = mapping.get(node_id)
    if not isinstance(raw, Mapping):
        raise BackendSchemaError("graph current branch references a missing node")
    if _exact_string(raw.get("id"), "node id") != node_id:
        raise BackendSchemaError("graph mapping key and node id differ")
    return raw


def _node(mapping: Mapping[str, Any], node_id: str) -> Mapping[str, Any]:
    raw = _graph_node(mapping, node_id)
    message = raw.get("message")
    if not isinstance(message, Mapping):
        raise BackendSchemaError("graph node is missing message object")
    if _exact_string(message.get("id"), "message id") != node_id:
        raise BackendSchemaError("graph node and canonical message id differ")
    author = message.get("author")
    if not isinstance(author, Mapping):
        raise BackendSchemaError("graph message is missing author object")
    role = author.get("role")
    if role not in {"user", "assistant", "tool", "system"}:
        raise BackendSchemaError("graph message has invalid author role")
    parent = raw.get("parent")
    if parent is not None:
        _exact_string(parent, "parent id")
    return raw


def _message_role(node: Mapping[str, Any]) -> str:
    return str(node["message"]["author"]["role"])


def _message_recipient(node: Mapping[str, Any]) -> str:
    recipient = node["message"].get("recipient", "all")
    if not isinstance(recipient, str) or not recipient:
        raise BackendSchemaError("graph message recipient must be a non-empty string")
    return recipient


def _assistant_text(node: Mapping[str, Any]) -> tuple[str, str] | None:
    message = node["message"]
    content = message.get("content")
    if not isinstance(content, Mapping):
        raise BackendSchemaError("graph message content must be an object")
    content_type = content.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise BackendSchemaError("graph content type must be a non-empty string")
    if content_type not in {"text", "multimodal_text"}:
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise BackendSchemaError("graph text content parts must be a list")
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, Mapping):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    text = "".join(chunks).strip()
    return (text, content_type) if text else None


def resolve_terminal_assistant(
    graph: Mapping[str, Any], accepted_user_message_id: str
) -> ResolvedAssistant:
    if not isinstance(graph, Mapping):
        raise BackendSchemaError("conversation graph must be an object")
    mapping = graph.get("mapping")
    current_node = graph.get("current_node")
    if not isinstance(mapping, Mapping) or not isinstance(current_node, str) or not current_node:
        raise BackendSchemaError("conversation graph requires mapping and current_node")
    if not isinstance(accepted_user_message_id, str) or not accepted_user_message_id:
        raise GraphIdentityError("accepted user message id is missing")

    reverse_chain: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    node_id: str | None = current_node
    while node_id is not None:
        if node_id in seen:
            raise BackendSchemaError("conversation graph current branch contains a cycle")
        seen.add(node_id)
        node = _graph_node(mapping, node_id)
        reverse_chain.append(node)
        if node_id == accepted_user_message_id:
            parent = node.get("parent")
            if parent is not None and str(parent) in seen:
                raise BackendSchemaError("conversation graph current branch contains a cycle")
            break
        parent = node.get("parent")
        node_id = str(parent) if parent is not None else None
    else:
        if any(
            isinstance(raw, Mapping)
            and isinstance(raw.get("message"), Mapping)
            and raw["message"].get("id") == accepted_user_message_id
            for raw in mapping.values()
        ):
            raise GraphIdentityError("accepted user message is not on the current branch")
        raise BackendNotReadyError("accepted user message is not materialized yet")

    chain = list(reversed(reverse_chain))
    for node in chain:
        _node(mapping, str(node["id"]))
    if _message_role(chain[0]) != "user":
        raise GraphIdentityError("accepted user message id does not identify a user node")

    terminal: ResolvedAssistant | None = None
    unresolved_tool_chain = False
    tool_result_seen = False
    previous_role = "user"
    for node in chain[1:]:
        role = _message_role(node)
        if role == "user":
            if previous_role != "tool":
                raise GraphIdentityError("a later human user turn follows the accepted turn")
        elif role == "tool":
            if unresolved_tool_chain:
                tool_result_seen = True
        elif role == "assistant":
            recipient = _message_recipient(node)
            if recipient != "all":
                unresolved_tool_chain = True
                tool_result_seen = False
            elif unresolved_tool_chain and not tool_result_seen:
                previous_role = role
                continue
            else:
                candidate = _assistant_text(node)
                if candidate is not None:
                    text, content_type = candidate
                    terminal = ResolvedAssistant(
                        message_id=str(node["message"]["id"]),
                        text=text,
                        content_type=content_type,
                    )
                    unresolved_tool_chain = False
                    tool_result_seen = False
        previous_role = role

    if terminal is None or unresolved_tool_chain:
        raise BackendNotReadyError("terminal assistant response is not materialized yet")
    return terminal
