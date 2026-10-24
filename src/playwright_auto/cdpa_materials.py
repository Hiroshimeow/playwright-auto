from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, MutableMapping

SOURCE_KINDS = frozenset(
    {"file", "inline", "api", "generated_prompt", "route_json", "provider_result", "transcript"}
)
MATERIAL_TYPES = frozenset(
    {"report", "handoff", "prompt", "skill", "rule", "task_context", "log", "artifact"}
)
DEFAULT_INLINE_LIMIT = 64 * 1024


class MaterialRefError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MaterialRef:
    material_id: str
    task_id: str
    source_kind: str
    material_type: str
    version: str
    mime_type: str
    content_length: int
    truncated: bool
    content: str | None
    content_ref: str | None
    role_key: str | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kind(value: Any, *, allowed: frozenset[str], field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", f"unsupported {field}: {normalized!r}")
    return normalized


def _material_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 160:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "material_id is invalid")
    return normalized


def register_material(
    state: MutableMapping[str, Any],
    *,
    source_kind: str,
    material_type: str,
    version: str = "v1",
    content: str | None = None,
    content_ref: str | None = None,
    mime_type: str = "text/plain",
    role_key: str | None = None,
    material_id: str | None = None,
) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "").strip()
    if not task_id:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "task_id is required")
    source = _kind(source_kind, allowed=SOURCE_KINDS, field="source_kind")
    material = _kind(material_type, allowed=MATERIAL_TYPES, field="material_type")
    identifier = _material_id(material_id or f"mat-{uuid.uuid4().hex[:20]}")
    encoded = content.encode("utf-8") if content is not None else b""
    record = {
        "material_id": identifier,
        "task_id": task_id,
        "role_key": str(role_key).strip().upper() if role_key else None,
        "source_kind": source,
        "material_type": material,
        "version": str(version or "v1"),
        "mime_type": str(mime_type or "text/plain"),
        "content_length": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest() if content is not None else None,
        "content": content,
        "content_ref": str(content_ref).strip() if content_ref else None,
    }
    materials = state.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "materials registry is not an object")
    existing = materials.get(identifier)
    if existing is not None:
        if json.dumps(existing, sort_keys=True, default=str) != json.dumps(record, sort_keys=True, default=str):
            raise MaterialRefError("MATERIAL_ID_CONFLICT", "material_id already belongs to different metadata")
        return dict(existing)
    materials[identifier] = record
    return dict(record)


def bounded_material_payload(
    *,
    task_id: str,
    material_id: str,
    locator: Mapping[str, Any],
    content: bytes | str | None,
    max_bytes: int = DEFAULT_INLINE_LIMIT,
) -> dict[str, Any]:
    source = _kind(locator.get("source_kind", "inline"), allowed=SOURCE_KINDS, field="source_kind")
    material = _kind(locator.get("material_type", "report"), allowed=MATERIAL_TYPES, field="material_type")
    identifier = _material_id(material_id)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "max_bytes must be positive")
    if content is None:
        return MaterialRef(
            material_id=identifier,
            task_id=task_id,
            role_key=(str(locator.get("role_key") or "").strip().upper() or None),
            source_kind=source,
            material_type=material,
            version=str(locator.get("version") or "v1"),
            mime_type=str(locator.get("mime_type") or "text/plain"),
            content_length=int(locator.get("content_length") or locator.get("size") or 0),
            truncated=False,
            content=None,
            content_ref=(str(locator.get("content_ref") or "").strip() or None),
            error={
                "code": "MATERIAL_CONTENT_UNAVAILABLE",
                "message": "Material content is unavailable for bounded inline display.",
            },
        ).to_dict()
    raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
    bounded = raw[:max_bytes]
    text = bounded.decode("utf-8", errors="replace")
    return MaterialRef(
        material_id=identifier,
        task_id=task_id,
        role_key=(str(locator.get("role_key") or "").strip().upper() or None),
        source_kind=source,
        material_type=material,
        version=str(locator.get("version") or "v1"),
        mime_type=str(locator.get("mime_type") or "text/plain"),
        content_length=len(raw),
        truncated=len(raw) > max_bytes,
        content=text,
        content_ref=(str(locator.get("content_ref") or "").strip() or None),
        error=None,
    ).to_dict()


def missing_material_payload(*, task_id: str, material_id: str) -> dict[str, Any]:
    return MaterialRef(
        material_id=_material_id(material_id),
        task_id=str(task_id),
        role_key=None,
        source_kind="inline",
        material_type="report",
        version="v1",
        mime_type="text/plain",
        content_length=0,
        truncated=False,
        content=None,
        content_ref=None,
        error={"code": "MATERIAL_NOT_FOUND", "message": "Material does not exist."},
    ).to_dict()
