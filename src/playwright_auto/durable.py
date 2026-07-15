from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .chatgpt import (
    ChatGPTSnapshot,
    MessageBaseline,
    PageBinding,
    SendReceipt,
    validate_page_role,
)
from .upload import FileIdentity


class DurableRequestError(RuntimeError):
    pass


class DurableRequestBusyError(DurableRequestError):
    pass


class RequestStatus(str, Enum):
    NEW = "new"
    PROMPT_SET = "prompt_set"
    UPLOADING = "uploading"
    UPLOAD_READY = "upload_ready"
    SENDING = "sending"
    SENT = "sent"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"


class DurableRecoveryState(str, Enum):
    COMPLETED = "completed"
    SENT_WAITING_RESPONSE = "sent_waiting_response"
    COMPOSER_PROMPT_ONLY_PENDING = "composer_prompt_only_pending"
    UPLOAD_READY_NOT_SENT = "upload_ready_not_sent"
    COMPOSER_PROMPT_AND_ATTACHMENTS_PENDING = (
        "composer_prompt_and_attachments_pending"
    )
    COMPOSER_PROMPT_MISSING_ATTACHMENTS = "composer_prompt_missing_attachments"
    COMPOSER_ATTACHMENTS_WITHOUT_MARKER = "composer_attachments_without_marker"
    MANUAL_COMPOSER_DIRTY = "manual_composer_dirty"
    SENT_MARKER_MISSING = "sent_marker_missing"
    RECOVERY_MARKER_NOT_FOUND = "recovery_marker_not_found"


_ALLOWED_TRANSITIONS: dict[RequestStatus, set[RequestStatus]] = {
    RequestStatus.NEW: {
        RequestStatus.PROMPT_SET,
        RequestStatus.SENDING,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.PROMPT_SET: {
        RequestStatus.UPLOADING,
        RequestStatus.UPLOAD_READY,
        RequestStatus.SENDING,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.UPLOADING: {
        RequestStatus.UPLOAD_READY,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.UPLOAD_READY: {
        RequestStatus.UPLOADING,
        RequestStatus.SENDING,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.SENDING: {
        RequestStatus.SENT,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.SENT: {
        RequestStatus.COMPLETED,
        RequestStatus.FAILED_RETRYABLE,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.COMPLETED: set(),
    RequestStatus.FAILED_RETRYABLE: {
        RequestStatus.PROMPT_SET,
        RequestStatus.UPLOADING,
        RequestStatus.UPLOAD_READY,
        RequestStatus.SENDING,
        RequestStatus.SENT,
        RequestStatus.COMPLETED,
        RequestStatus.FAILED_FINAL,
    },
    RequestStatus.FAILED_FINAL: set(),
}


def normalize_prompt(prompt: str) -> str:
    lines = str(prompt).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def request_marker(request_id: str) -> str:
    return f"ROLE_REQUEST_ID: {request_id}"


def render_prompt(prompt: str, marker: str) -> str:
    normalized = normalize_prompt(prompt)
    if marker in normalized:
        return normalized
    return f"{normalized}\n\n{marker}"


def canonical_json_value(value: Any) -> Any:
    """Return a stable JSON-compatible representation for durable identity."""
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def build_idempotency_key(
    *,
    role: str,
    prompt: str,
    source_context: Any = None,
    role_prompt_hash: str = "",
    files: Sequence[FileIdentity] = (),
) -> str:
    role = validate_page_role(role)
    payload = {
        "role": role,
        "prompt": normalize_prompt(prompt),
        "source_context": canonical_json_value(source_context),
        "role_prompt_hash": str(role_prompt_hash),
        "files": sorted(
            [
                {
                    "path": item.path,
                    "name": item.name,
                    "size": item.size,
                    "sha256": item.sha256,
                }
                for item in files
            ],
            key=lambda item: (item["path"], item["sha256"]),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DurableRequestRecord:
    request_id: str
    idempotency_key: str
    role: str
    prompt: str
    rendered_prompt: str
    marker: str
    status: RequestStatus
    created_at: float
    updated_at: float
    attempts: int = 0
    source_context: Any = None
    role_prompt_hash: str = ""
    files: tuple[FileIdentity, ...] = ()
    binding: PageBinding | None = None
    baseline: MessageBaseline | None = None
    session_id_before: str | None = None
    receipt: dict[str, Any] | None = None
    upload_receipt: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "role": self.role,
            "prompt": self.prompt,
            "rendered_prompt": self.rendered_prompt,
            "marker": self.marker,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "source_context": self.source_context,
            "role_prompt_hash": self.role_prompt_hash,
            "files": [item.to_dict() for item in self.files],
            "binding": self.binding.to_dict() if self.binding else None,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "session_id_before": self.session_id_before,
            "receipt": self.receipt,
            "upload_receipt": self.upload_receipt,
            "response": self.response,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DurableRequestRecord":
        return cls(
            request_id=str(value["request_id"]),
            idempotency_key=str(value["idempotency_key"]),
            role=validate_page_role(str(value["role"])),
            prompt=str(value["prompt"]),
            rendered_prompt=str(value["rendered_prompt"]),
            marker=str(value["marker"]),
            status=RequestStatus(str(value["status"])),
            created_at=float(value["created_at"]),
            updated_at=float(value["updated_at"]),
            attempts=int(value.get("attempts") or 0),
            source_context=value.get("source_context"),
            role_prompt_hash=str(value.get("role_prompt_hash") or ""),
            files=tuple(
                FileIdentity.from_dict(dict(item)) for item in value.get("files", [])
            ),
            binding=(
                PageBinding.from_dict(value["binding"])
                if value.get("binding")
                else None
            ),
            baseline=(
                MessageBaseline.from_dict(value["baseline"])
                if value.get("baseline")
                else None
            ),
            session_id_before=(
                str(value["session_id_before"])
                if value.get("session_id_before") is not None
                else None
            ),
            receipt=(dict(value["receipt"]) if value.get("receipt") else None),
            upload_receipt=(
                dict(value["upload_receipt"])
                if value.get("upload_receipt")
                else None
            ),
            response=(dict(value["response"]) if value.get("response") else None),
            error=(str(value["error"]) if value.get("error") else None),
        )


class RequestLedger:
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                else:
                    raw = {"version": self.VERSION, "records": {}}
                if raw.get("version") != self.VERSION:
                    raise DurableRequestError("unsupported durable ledger version")
                if not isinstance(raw.get("records"), dict):
                    raise DurableRequestError("durable ledger records must be an object")
                yield raw
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                payload = json.dumps(
                    raw, ensure_ascii=False, indent=2, sort_keys=True
                )
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def request_lock(self, request_id: str) -> Iterator[None]:
        lock_path = self.path.with_suffix(
            self.path.suffix + f".{request_id}.request.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise DurableRequestBusyError(
                    f"durable request {request_id} is already running"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def begin(
        self,
        *,
        role: str,
        prompt: str,
        source_context: Any = None,
        role_prompt_hash: str = "",
        files: Sequence[FileIdentity] = (),
    ) -> DurableRequestRecord:
        role = validate_page_role(role)
        normalized = normalize_prompt(prompt)
        if not normalized:
            raise ValueError("durable prompt must not be empty")
        source_context = canonical_json_value(source_context)
        key = build_idempotency_key(
            role=role,
            prompt=normalized,
            source_context=source_context,
            role_prompt_hash=role_prompt_hash,
            files=files,
        )
        request_id = key[:24]
        marker = request_marker(request_id)
        with self._locked() as data:
            existing = data["records"].get(request_id)
            if existing:
                record = DurableRequestRecord.from_dict(existing)
                if record.idempotency_key != key:
                    raise DurableRequestError("request ID collision in durable ledger")
                return record
            now = time.time()
            record = DurableRequestRecord(
                request_id=request_id,
                idempotency_key=key,
                role=role,
                prompt=normalized,
                rendered_prompt=render_prompt(normalized, marker),
                marker=marker,
                status=RequestStatus.NEW,
                created_at=now,
                updated_at=now,
                source_context=source_context,
                role_prompt_hash=role_prompt_hash,
                files=tuple(files),
            )
            data["records"][request_id] = record.to_dict()
            return record

    def get(self, request_id: str) -> DurableRequestRecord | None:
        with self._locked() as data:
            value = data["records"].get(request_id)
            return DurableRequestRecord.from_dict(value) if value else None

    def update(
        self,
        request_id: str,
        *,
        status: RequestStatus | None = None,
        **changes: Any,
    ) -> DurableRequestRecord:
        with self._locked() as data:
            value = data["records"].get(request_id)
            if not value:
                raise KeyError(request_id)
            record = DurableRequestRecord.from_dict(value)
            target = status or record.status
            if target != record.status and target not in _ALLOWED_TRANSITIONS[record.status]:
                raise DurableRequestError(
                    f"invalid durable transition {record.status.value} -> {target.value}"
                )
            allowed_fields = {
                "attempts",
                "binding",
                "baseline",
                "session_id_before",
                "receipt",
                "upload_receipt",
                "response",
                "error",
            }
            unknown = set(changes) - allowed_fields
            if unknown:
                raise TypeError(f"unsupported durable record fields: {sorted(unknown)!r}")
            record = replace(
                record,
                status=target,
                updated_at=time.time(),
                **changes,
            )
            data["records"][request_id] = record.to_dict()
            return record


def classify_recovery_state(
    record: DurableRequestRecord, snapshot: ChatGPTSnapshot
) -> DurableRecoveryState:
    if record.status is RequestStatus.COMPLETED and record.response:
        return DurableRecoveryState.COMPLETED
    marker_in_transcript = any(
        message.role == "user" and record.marker in message.text
        for message in snapshot.messages
    )
    if marker_in_transcript:
        return DurableRecoveryState.SENT_WAITING_RESPONSE
    if record.status in {RequestStatus.SENDING, RequestStatus.SENT}:
        # Once the ledger crosses the send boundary, absence of transcript
        # evidence is ambiguous. Never infer that it is safe to click again.
        return DurableRecoveryState.SENT_MARKER_MISSING

    composer_text = snapshot.composer_text.strip()
    marker_in_composer = record.marker in composer_text
    attachment_count = len(snapshot.attachment_markers)
    expected_files = len(record.files)
    if marker_in_composer:
        if expected_files == 0:
            return DurableRecoveryState.COMPOSER_PROMPT_ONLY_PENDING
        if attachment_count >= expected_files:
            return DurableRecoveryState.UPLOAD_READY_NOT_SENT
        if attachment_count > 0:
            return DurableRecoveryState.COMPOSER_PROMPT_AND_ATTACHMENTS_PENDING
        return DurableRecoveryState.COMPOSER_PROMPT_MISSING_ATTACHMENTS
    if attachment_count:
        return DurableRecoveryState.COMPOSER_ATTACHMENTS_WITHOUT_MARKER
    if composer_text:
        return DurableRecoveryState.MANUAL_COMPOSER_DIRTY
    return DurableRecoveryState.RECOVERY_MARKER_NOT_FOUND


def receipt_from_record(record: DurableRequestRecord) -> SendReceipt:
    if record.receipt:
        return SendReceipt.from_dict(record.receipt)
    if not record.binding or not record.baseline:
        raise DurableRequestError(
            "cannot recover sent request without persisted binding and baseline"
        )
    return SendReceipt(
        prompt=record.rendered_prompt,
        prompt_sha256=hashlib.sha256(
            record.rendered_prompt.strip().encode("utf-8")
        ).hexdigest(),
        binding=record.binding,
        baseline=record.baseline,
        attempts=max(1, min(record.attempts or 1, 2)),
        accepted_via="exact_user_message",
        session_id_before=record.session_id_before,
    )
