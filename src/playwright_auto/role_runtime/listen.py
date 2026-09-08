"""Bounded incremental conversation decoding, independent of CDPA request identity.

This module classifies traffic and extracts current signals; it never fetches a
conversation, validates a graph, clicks anything or decides whether a task may run.
"""
from __future__ import annotations

import codecs
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

HOSTS = {"chatgpt.com", "www.chatgpt.com"}


def conversation_id(url: str) -> str | None:
    parsed = urlparse(str(url))
    if parsed.hostname not in HOSTS:
        return None
    parts = parsed.path.rstrip("/").split("/")
    return parts[-1] if len(parts) >= 3 and parts[-2] == "c" else None


def source_kind(method: str, url: str) -> str | None:
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS:
        return None
    if method.upper() == "POST" and parsed.path in {
        "/backend-api/f/conversation", "/backend-api/conversation",
    }:
        return "live"
    if method.upper() == "GET":
        if re.fullmatch(r"/backend-api/conversations?/[^/]+", parsed.path):
            return "history"
        if re.fullmatch(r"/backend-api/conversation/[^/]+/stream_status", parsed.path):
            return "status"
    return None


def _identity(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(ord(c) >= 32 for c in value) else None


def extract(value: Any) -> dict[str, Any]:
    """Extract action/message signals even from partial/nested transport envelopes."""
    stack = [(value, 0)]
    permissions: list[dict[str, Any]] = []
    messages: list[Mapping[str, Any]] = []
    conversations: set[str] = set()
    status = None
    nodes = 0
    while stack and nodes < 4096:
        item, depth = stack.pop()
        nodes += 1
        if depth > 16:
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in reversed(item[:512]))
            continue
        if not isinstance(item, Mapping):
            continue
        cid = _identity(item.get("conversation_id"))
        if cid:
            conversations.add(cid)
        if item.get("type") == "allow":
            target = _identity(item.get("target_message_id"))
            nested = item.get("allow")
            if target is None and isinstance(nested, Mapping):
                target = _identity(nested.get("target_message_id"))
            if target:
                permissions.append({
                    "type": "allow", "target_message_id": target,
                    "remember_answer": item.get("remember_answer") is True,
                    "label": str(item.get("label") or "Allow")[:256],
                })
        raw_status = item.get("status")
        if raw_status in ("IS_STREAMING", "COMPLETE", "FAILURE", "IS_STOP_REQUESTED"):
            status = raw_status
        author = item.get("author")
        if isinstance(author, Mapping) and author.get("role") in {"user", "assistant", "tool"}:
            messages.append(item)
        # Do not recursively interpret normal prose as protocol. Only structured
        # mappings/lists are inspected; JSON strings in tool/assistant text stay text.
        stack.extend((child, depth + 1) for child in reversed(tuple(item.values()))
                     if isinstance(child, (Mapping, list)))
    result: dict[str, Any] = {}
    if len(conversations) == 1:
        result["conversation_id"] = next(iter(conversations))
    elif len(conversations) > 1:
        result["mixed_conversations"] = True
    if permissions:
        preferred = [action for action in permissions if action["remember_answer"]]
        result["permission_action"] = (preferred or permissions)[-1]
    if status:
        result["status"] = status
    if messages:
        result["messages"] = messages[-128:]
        latest = messages[-1]
        if latest.get("author", {}).get("role") == "assistant":
            terminal = latest.get("end_turn") is True or latest.get("status") == "finished_successfully"
            # Analysis/tool-directed assistant messages are not route results.
            if terminal and latest.get("recipient", "all") in {"all", "user", None} and latest.get("channel", "final") not in {"analysis", "commentary"}:
                content = latest.get("content")
                parts = content.get("parts", []) if isinstance(content, Mapping) else []
                text = "\n".join(part for part in parts if isinstance(part, str))
                if text.strip():
                    result["response"] = {
                        "role": "assistant", "message_id": str(latest.get("id") or ""),
                        "turn_id": None, "text": text, "actions": [], "image_count": 0,
                    }
    return result


class ConversationDecoder:
    def __init__(self, *, max_buffer: int = 1_048_576):
        self.max_buffer = max_buffer
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self.errors = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer.encode("utf-8"))

    def _decode(self, packet: str) -> list[dict[str, Any]]:
        lines = [line[5:].lstrip() for line in packet.splitlines() if line.startswith("data:")]
        payload = "\n".join(lines) if lines else packet.strip()
        if not payload or payload == "[DONE]" or payload.startswith(":"):
            return []
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            # Older captures sometimes omit blank SSE separators. Each data line
            # can still be a complete JSON event; never let one corrupt frame poison later ones.
            if len(lines) > 1:
                return [item for line in lines for item in self._decode(line)]
            self.errors += 1
            return []
        return [extract(decoded)]

    def feed(self, chunk: bytes | str, *, final: bool = False) -> list[dict[str, Any]]:
        self._buffer += self._utf8.decode(chunk, final=final) if isinstance(chunk, bytes) else chunk
        results = []
        while True:
            match = re.search(r"\r?\n\r?\n", self._buffer)
            if match is None:
                break
            packet, self._buffer = self._buffer[:match.start()], self._buffer[match.end():]
            results.extend(self._decode(packet))
        if final and self._buffer.strip():
            results.extend(self._decode(self._buffer))
            self._buffer = ""
        if self.buffered_bytes > self.max_buffer:
            self._buffer = ""
            self.errors += 1
        return results
