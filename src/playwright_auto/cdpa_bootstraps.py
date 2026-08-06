from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping as MappingABC
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

from .file_lock import exclusive_file_lock, fsync_parent_directory


_REQUIRED_FIELDS = frozenset(
    {
        "bootstrap_id",
        "name",
        "description",
        "conversation_id",
        "terminal_assistant_message_id",
        "enabled",
        "tags",
        "created_at",
        "updated_at",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {"expires_at", "last_verified_at", "source_fingerprints"}
)
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_BOOTSTRAP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _validate_bootstrap_id(value: Any) -> str:
    if not isinstance(value, str) or _BOOTSTRAP_ID.fullmatch(value) is None:
        raise ValueError("bootstrap_id must be a canonical 1-128 character identifier")
    return value


def _validate_text(value: Any, *, field: str, maximum: int, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if (not allow_empty and not value) or len(value) > maximum:
        raise ValueError(f"{field} has invalid length")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _validate_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase hyphenated UUID")
    return value


def _normalize_timestamp(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_fingerprints(value: Any) -> dict[str, str]:
    if not isinstance(value, MappingABC):
        raise ValueError("source_fingerprints must be a mapping")
    if len(value) > 32:
        raise ValueError("source_fingerprints must contain at most 32 entries")
    normalized: dict[str, str] = {}
    for source, fingerprint in value.items():
        key = _validate_text(
            source,
            field="source_fingerprints key",
            maximum=256,
            allow_empty=False,
        )
        if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
            raise ValueError("source_fingerprints values must be canonical lowercase SHA-256")
        normalized[key] = fingerprint
    return normalized


def normalize_bootstrap_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, MappingABC):
        raise ValueError("bootstrap record must be a mapping")
    keys = set(value)
    missing = _REQUIRED_FIELDS - keys
    unknown = keys - _ALLOWED_FIELDS
    if missing:
        raise ValueError(f"bootstrap record missing fields: {sorted(missing)!r}")
    if unknown:
        raise ValueError(f"bootstrap record has unknown fields: {sorted(unknown)!r}")

    enabled = value["enabled"]
    if type(enabled) is not bool:
        raise ValueError("enabled must be a bool")

    tags = value["tags"]
    if not isinstance(tags, list) or len(tags) > 32:
        raise ValueError("tags must be a list with at most 32 items")
    normalized_tags = [
        _validate_text(tag, field="tag", maximum=64, allow_empty=False) for tag in tags
    ]
    if len(set(normalized_tags)) != len(normalized_tags):
        raise ValueError("tags must not contain duplicates")

    return {
        "bootstrap_id": _validate_bootstrap_id(value["bootstrap_id"]),
        "name": _validate_text(value["name"], field="name", maximum=200, allow_empty=False),
        "description": _validate_text(
            value["description"],
            field="description",
            maximum=2000,
            allow_empty=True,
        ),
        "conversation_id": _validate_uuid(value["conversation_id"], field="conversation_id"),
        "terminal_assistant_message_id": _validate_uuid(
            value["terminal_assistant_message_id"],
            field="terminal_assistant_message_id",
        ),
        "enabled": enabled,
        "tags": normalized_tags,
        "created_at": _normalize_timestamp(value["created_at"], field="created_at"),
        "updated_at": _normalize_timestamp(value["updated_at"], field="updated_at"),
        "expires_at": _normalize_timestamp(
            value.get("expires_at"), field="expires_at", nullable=True
        ),
        "last_verified_at": _normalize_timestamp(
            value.get("last_verified_at"), field="last_verified_at", nullable=True
        ),
        "source_fingerprints": _normalize_fingerprints(
            value.get("source_fingerprints", {})
        ),
    }


def _decode_json(source: str) -> Any:
    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key!r}")
            result[key] = item
        return result

    try:
        return json.loads(source, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid bootstrap catalog JSON: {exc.msg}") from exc


def _normalize_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "entries"}:
        raise ValueError("bootstrap catalog must contain exactly version and entries")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported bootstrap catalog version")
    if not isinstance(value["entries"], dict):
        raise ValueError("bootstrap catalog entries must be an object")

    entries: dict[str, dict[str, Any]] = {}
    anchors: dict[tuple[str, str], str] = {}
    for bootstrap_id, raw_record in value["entries"].items():
        record = normalize_bootstrap_record(raw_record)
        if bootstrap_id != record["bootstrap_id"]:
            raise ValueError("catalog entry key must equal record bootstrap_id")
        anchor = (record["conversation_id"], record["terminal_assistant_message_id"])
        previous = anchors.get(anchor)
        if previous is not None and previous != bootstrap_id:
            raise ValueError(
                f"duplicate anchor identity for bootstrap_id {previous!r} and {bootstrap_id!r}"
            )
        anchors[anchor] = bootstrap_id
        entries[bootstrap_id] = record
    return {"version": 1, "entries": entries}


class BootstrapCatalog:
    def __init__(self, repository_root: str | Path) -> None:
        root = Path(repository_root).expanduser().resolve()
        self.path = root / ".runtime" / "cdpa-bootstraps.json"
        self.lock_path = root / ".runtime" / "cdpa-bootstraps.json.lock"

    def list(self) -> list[dict[str, Any]]:
        catalog = self._load()
        return [catalog["entries"][key] for key in sorted(catalog["entries"])]

    def get(self, bootstrap_id: str) -> dict[str, Any] | None:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        return self._load()["entries"].get(bootstrap_id)

    def upsert(self, record: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_bootstrap_record(record)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            entries = dict(catalog["entries"])
            entries[normalized["bootstrap_id"]] = normalized
            candidate = _normalize_catalog({"version": 1, "entries": entries})
            if candidate != catalog:
                self._write(candidate)
        return normalized

    def disable(self, bootstrap_id: str) -> dict[str, Any] | None:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            existing = catalog["entries"].get(bootstrap_id)
            if existing is None:
                return None
            if not existing["enabled"]:
                return existing
            updated = {
                **existing,
                "enabled": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            entries = dict(catalog["entries"])
            entries[bootstrap_id] = updated
            self._write(_normalize_catalog({"version": 1, "entries": entries}))
            return normalize_bootstrap_record(updated)

    def delete(self, bootstrap_id: str) -> bool:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            if bootstrap_id not in catalog["entries"]:
                return False
            entries = dict(catalog["entries"])
            del entries[bootstrap_id]
            self._write(_normalize_catalog({"version": 1, "entries": entries}))
            return True

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "entries": {}}
        return _normalize_catalog(_decode_json(self.path.read_text(encoding="utf-8")))

    def _write(self, catalog: Mapping[str, Any]) -> None:
        normalized = _normalize_catalog(catalog)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(normalized, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fsync_parent_directory(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
