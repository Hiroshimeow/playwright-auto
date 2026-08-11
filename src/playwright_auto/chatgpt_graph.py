from __future__ import annotations

from dataclasses import dataclass
import json
import ntpath
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


def resolve_completed_file_write(
    graph: Mapping[str, Any],
    accepted_user_message_id: str,
    terminal_assistant_message_id: str,
    *,
    recipient: str,
    expected_path: str,
) -> str:
    """Return exact content from one completed file write on the accepted branch."""
    if not isinstance(graph, Mapping):
        raise BackendSchemaError("conversation graph must be an object")
    mapping = graph.get("mapping")
    if not isinstance(mapping, Mapping):
        raise BackendSchemaError("conversation graph requires mapping")
    if not isinstance(accepted_user_message_id, str) or not accepted_user_message_id:
        raise GraphIdentityError("accepted user message id is missing")
    if not isinstance(terminal_assistant_message_id, str) or not terminal_assistant_message_id:
        raise GraphIdentityError("terminal assistant message id is missing")
    exact_recipient = _exact_string(recipient, "file write recipient")
    exact_expected = _exact_string(expected_path, "expected file write path")
    if not ntpath.isabs(exact_expected):
        raise BackendSchemaError("expected file write path must be absolute")

    reverse_chain: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    node_id: str | None = terminal_assistant_message_id
    while node_id is not None:
        if node_id in seen:
            raise BackendSchemaError("file write branch contains a cycle")
        seen.add(node_id)
        node = _node(mapping, node_id)
        reverse_chain.append(node)
        if node_id == accepted_user_message_id:
            break
        parent = node.get("parent")
        node_id = str(parent) if parent is not None else None
    if not reverse_chain or str(reverse_chain[-1]["message"]["id"]) != accepted_user_message_id:
        raise GraphIdentityError("terminal assistant is not on the accepted user branch")
    chain = list(reversed(reverse_chain))
    terminal_index = len(chain) - 1
    if _message_role(chain[0]) != "user":
        raise GraphIdentityError("accepted user message id does not identify a user node")
    terminal = chain[terminal_index]
    if _message_role(terminal) != "assistant" or _message_recipient(terminal) != "all":
        raise GraphIdentityError("terminal assistant identity is not a public assistant response")

    normalized_expected = ntpath.normcase(ntpath.normpath(exact_expected))
    matches: list[str] = []
    for index, node in enumerate(chain[1:terminal_index], start=1):
        if _message_role(node) != "assistant" or _message_recipient(node) != exact_recipient:
            continue
        extracted = _assistant_text(node)
        if extracted is None:
            raise BackendSchemaError("file write tool call is missing JSON arguments")
        raw_arguments, _content_type = extracted
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise BackendSchemaError("file write tool call arguments must be JSON") from exc
        if not isinstance(arguments, Mapping):
            raise BackendSchemaError("file write tool call arguments must be an object")
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str) or not ntpath.isabs(path):
            raise BackendSchemaError("file write tool call path must be absolute")
        if not isinstance(content, str):
            raise BackendSchemaError("file write tool call content must be a string")
        if ntpath.normcase(ntpath.normpath(path)) != normalized_expected:
            continue
        if index + 1 >= len(chain) or _message_role(chain[index + 1]) != "tool":
            raise BackendSchemaError("matching file write tool call has no completed tool result")
        matches.append(content)

    if len(matches) != 1:
        raise BackendSchemaError("expected exactly one completed write for the exact report path")
    return matches[0]



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


def _current_branch_node_ids(graph: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[str]]:
    if not isinstance(graph, Mapping):
        raise BackendSchemaError("conversation graph must be an object")
    mapping = graph.get("mapping")
    current_node = graph.get("current_node")
    if not isinstance(mapping, Mapping) or not isinstance(current_node, str) or not current_node:
        raise BackendSchemaError("conversation graph requires mapping and current_node")
    reverse: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current_node
    while node_id is not None:
        if node_id in seen:
            raise BackendSchemaError("conversation graph current branch contains a cycle")
        seen.add(node_id)
        raw = _graph_node(mapping, node_id)
        reverse.append(node_id)
        parent = raw.get("parent")
        node_id = str(parent) if parent is not None else None
    return mapping, list(reversed(reverse))


def resolve_latest_terminal_assistant(graph: Mapping[str, Any]) -> ResolvedAssistant:
    mapping, chain = _current_branch_node_ids(graph)
    latest_user_id: str | None = None
    for node_id in reversed(chain):
        raw = _graph_node(mapping, node_id)
        message = raw.get("message")
        if not isinstance(message, Mapping):
            continue
        node = _node(mapping, node_id)
        if _message_role(node) == "user":
            latest_user_id = node_id
            break
    if latest_user_id is None:
        raise BackendNotReadyError("conversation has no materialized user turn")
    return resolve_terminal_assistant(graph, latest_user_id)


def resolve_bootstrap_donor(
    graph: Mapping[str, Any], assistant_message_id: str
) -> ResolvedAssistant:
    mapping, chain = _current_branch_node_ids(graph)
    if not isinstance(assistant_message_id, str) or not assistant_message_id:
        raise GraphIdentityError("bootstrap assistant message id is missing")
    if assistant_message_id not in chain:
        if assistant_message_id in mapping:
            raise GraphIdentityError("bootstrap assistant message is not on the current branch")
        raise GraphIdentityError("bootstrap assistant message is missing")
    node = _node(mapping, assistant_message_id)
    if _message_role(node) != "assistant" or _message_recipient(node) != "all":
        raise GraphIdentityError("bootstrap donor must identify a public assistant message")
    candidate = _assistant_text(node)
    if candidate is None:
        raise GraphIdentityError("bootstrap donor assistant has no branchable text content")
    text, content_type = candidate
    return ResolvedAssistant(
        message_id=assistant_message_id,
        text=text,
        content_type=content_type,
    )


def resolve_inherited_assistant(
    graph: Mapping[str, Any], accepted_user_message_id: str
) -> ResolvedAssistant:
    mapping, chain = _current_branch_node_ids(graph)
    if not isinstance(accepted_user_message_id, str) or not accepted_user_message_id:
        raise GraphIdentityError("accepted user message id is missing")
    if accepted_user_message_id not in chain:
        if accepted_user_message_id in mapping:
            raise GraphIdentityError("accepted user message is not on the current branch")
        raise BackendNotReadyError("accepted user message is not materialized yet")
    accepted_index = chain.index(accepted_user_message_id)
    accepted = _node(mapping, accepted_user_message_id)
    if _message_role(accepted) != "user":
        raise GraphIdentityError("accepted user message id does not identify a user node")
    for node_id in reversed(chain[:accepted_index]):
        raw = _graph_node(mapping, node_id)
        if not isinstance(raw.get("message"), Mapping):
            continue
        node = _node(mapping, node_id)
        if _message_role(node) != "assistant" or _message_recipient(node) != "all":
            continue
        candidate = _assistant_text(node)
        if candidate is None:
            continue
        text, content_type = candidate
        return ResolvedAssistant(message_id=node_id, text=text, content_type=content_type)
    raise GraphIdentityError("accepted role turn has no inherited public bootstrap assistant")
