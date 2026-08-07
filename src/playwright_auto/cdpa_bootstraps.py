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
from urllib.parse import urlparse
from uuid import UUID

from .file_lock import exclusive_file_lock, fsync_parent_directory


_BOOTSTRAP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CHATGPT_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com"})
_DEFAULT_MAX_BACKUPS = 7
_MAX_BACKUPS = 32

_NEW_REQUIRED_FIELDS = frozenset(
    {
        "bootstrap_id",
        "name",
        "description",
        "source_conversation_id",
        "prewarm_prompt",
        "max_backups",
        "donors",
        "enabled",
        "tags",
        "created_at",
        "updated_at",
    }
)
_NEW_OPTIONAL_INPUT_FIELDS = frozenset({"max_backups", "donors", "description", "enabled", "tags"})
_NEW_ALLOWED_FIELDS = _NEW_REQUIRED_FIELDS

_LEGACY_REQUIRED_FIELDS = frozenset(
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
_LEGACY_OPTIONAL_FIELDS = frozenset({"expires_at", "last_verified_at", "source_fingerprints"})
_LEGACY_ALLOWED_FIELDS = _LEGACY_REQUIRED_FIELDS | _LEGACY_OPTIONAL_FIELDS


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


def _validate_prompt(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("prewarm_prompt must be a string or null")
    normalized = value.strip()
    if not normalized or len(normalized) > 100_000:
        raise ValueError("prewarm_prompt must contain 1-100000 characters")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in normalized):
        raise ValueError("prewarm_prompt contains unsupported control characters")
    return normalized


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


def normalize_bootstrap_source(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("bootstrap source must be a canonical UUID or ChatGPT conversation URL")
    text = value.strip()
    try:
        return _validate_uuid(text, field="source_conversation_id")
    except ValueError:
        pass
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise ValueError("bootstrap source must be a canonical UUID or ChatGPT conversation URL") from exc
    path = parsed.path.rstrip("/")
    conversation_id = path.rsplit("/", 1)[-1] if path else ""
    if (
        (parsed.hostname or "").lower() not in _CHATGPT_HOSTS
        or not path.startswith("/c/")
        or not conversation_id
    ):
        raise ValueError("bootstrap source must be a canonical UUID or ChatGPT conversation URL")
    return _validate_uuid(conversation_id, field="source_conversation_id")


def _normalize_optional_source(value: Any) -> str | None:
    if value is None:
        return None
    return normalize_bootstrap_source(value)


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


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("tags must be a list with at most 32 items")
    normalized = [
        _validate_text(tag, field="tag", maximum=64, allow_empty=False) for tag in value
    ]
    if len(set(normalized)) != len(normalized):
        raise ValueError("tags must not contain duplicates")
    return normalized


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


def normalize_bootstrap_donor(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, MappingABC) or set(value) != {"conversation_id", "assistant_message_id"}:
        raise ValueError("donor must contain exactly conversation_id and assistant_message_id")
    return {
        "conversation_id": _validate_uuid(value["conversation_id"], field="donor conversation_id"),
        "assistant_message_id": _validate_uuid(
            value["assistant_message_id"], field="donor assistant_message_id"
        ),
    }


def _normalize_max_backups(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_BACKUPS:
        raise ValueError(f"max_backups must be an integer from 1 to {_MAX_BACKUPS}")
    return value


def is_legacy_bootstrap_record(value: Mapping[str, Any]) -> bool:
    return isinstance(value, MappingABC) and "conversation_id" in value and "terminal_assistant_message_id" in value


def normalize_legacy_bootstrap_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, MappingABC):
        raise ValueError("bootstrap record must be a mapping")
    keys = set(value)
    missing = _LEGACY_REQUIRED_FIELDS - keys
    unknown = keys - _LEGACY_ALLOWED_FIELDS
    if missing:
        raise ValueError(f"bootstrap record missing fields: {sorted(missing)!r}")
    if unknown:
        raise ValueError(f"bootstrap record has unknown fields: {sorted(unknown)!r}")
    enabled = value["enabled"]
    if type(enabled) is not bool:
        raise ValueError("enabled must be a bool")
    return {
        "bootstrap_id": _validate_bootstrap_id(value["bootstrap_id"]),
        "name": _validate_text(value["name"], field="name", maximum=200, allow_empty=False),
        "description": _validate_text(
            value["description"], field="description", maximum=2000, allow_empty=True
        ),
        "conversation_id": _validate_uuid(value["conversation_id"], field="conversation_id"),
        "terminal_assistant_message_id": _validate_uuid(
            value["terminal_assistant_message_id"], field="terminal_assistant_message_id"
        ),
        "enabled": enabled,
        "tags": _normalize_tags(value["tags"]),
        "created_at": _normalize_timestamp(value["created_at"], field="created_at"),
        "updated_at": _normalize_timestamp(value["updated_at"], field="updated_at"),
        "expires_at": _normalize_timestamp(value.get("expires_at"), field="expires_at", nullable=True),
        "last_verified_at": _normalize_timestamp(
            value.get("last_verified_at"), field="last_verified_at", nullable=True
        ),
        "source_fingerprints": _normalize_fingerprints(value.get("source_fingerprints", {})),
    }


def _migrate_legacy_bootstrap(value: Mapping[str, Any]) -> dict[str, Any]:
    legacy = normalize_legacy_bootstrap_record(value)
    return {
        "bootstrap_id": legacy["bootstrap_id"],
        "name": legacy["name"],
        "description": legacy["description"],
        "source_conversation_id": legacy["conversation_id"],
        "prewarm_prompt": None,
        "max_backups": _DEFAULT_MAX_BACKUPS,
        "donors": [
            {
                "conversation_id": legacy["conversation_id"],
                "assistant_message_id": legacy["terminal_assistant_message_id"],
            }
        ],
        "enabled": legacy["enabled"],
        "tags": legacy["tags"],
        "created_at": legacy["created_at"],
        "updated_at": legacy["updated_at"],
    }


def normalize_bootstrap_record(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, MappingABC):
        raise ValueError("bootstrap record must be a mapping")
    if is_legacy_bootstrap_record(value):
        return _migrate_legacy_bootstrap(value)

    keys = set(value)
    required_without_defaults = _NEW_REQUIRED_FIELDS - _NEW_OPTIONAL_INPUT_FIELDS
    missing = required_without_defaults - keys
    unknown = keys - _NEW_ALLOWED_FIELDS
    if missing:
        raise ValueError(f"bootstrap record missing fields: {sorted(missing)!r}")
    if unknown:
        raise ValueError(f"bootstrap record has unknown fields: {sorted(unknown)!r}")

    enabled = value.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a bool")
    max_backups = _normalize_max_backups(value.get("max_backups", _DEFAULT_MAX_BACKUPS))
    source = _normalize_optional_source(value.get("source_conversation_id"))
    prewarm_prompt = _validate_prompt(value.get("prewarm_prompt"))
    if source is None and prewarm_prompt is None:
        raise ValueError("bootstrap requires source or prewarm_prompt")

    raw_donors = value.get("donors", [])
    if not isinstance(raw_donors, list):
        raise ValueError("donors must be a list")
    donors = [normalize_bootstrap_donor(item) for item in raw_donors]
    identities = [(item["conversation_id"], item["assistant_message_id"]) for item in donors]
    if len(set(identities)) != len(identities):
        raise ValueError("donors must not contain duplicates")
    if len(donors) > max_backups:
        raise ValueError("donors must not exceed max_backups")

    return {
        "bootstrap_id": _validate_bootstrap_id(value["bootstrap_id"]),
        "name": _validate_text(value["name"], field="name", maximum=200, allow_empty=False),
        "description": _validate_text(
            value.get("description", ""), field="description", maximum=2000, allow_empty=True
        ),
        "source_conversation_id": source,
        "prewarm_prompt": prewarm_prompt,
        "max_backups": max_backups,
        "donors": donors,
        "enabled": enabled,
        "tags": _normalize_tags(value.get("tags", [])),
        "created_at": _normalize_timestamp(value["created_at"], field="created_at"),
        "updated_at": _normalize_timestamp(value["updated_at"], field="updated_at"),
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
    version = value["version"]
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("unsupported bootstrap catalog version")
    if not isinstance(value["entries"], dict):
        raise ValueError("bootstrap catalog entries must be an object")

    entries: dict[str, dict[str, Any]] = {}
    for bootstrap_id, raw_record in value["entries"].items():
        record = (
            _migrate_legacy_bootstrap(raw_record)
            if version == 1
            else normalize_bootstrap_record(raw_record)
        )
        if bootstrap_id != record["bootstrap_id"]:
            raise ValueError("catalog entry key must equal record bootstrap_id")
        entries[bootstrap_id] = record
    return {"version": 2, "entries": entries}


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
            candidate = _normalize_catalog({"version": 2, "entries": entries})
            if candidate != catalog:
                self._write(candidate)
        return normalized

    def add_donor(self, bootstrap_id: str, donor: Mapping[str, Any]) -> dict[str, Any]:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        normalized_donor = normalize_bootstrap_donor(donor)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            existing = catalog["entries"].get(bootstrap_id)
            if existing is None:
                raise ValueError(f"bootstrap does not exist: {bootstrap_id}")
            donors = [
                normalized_donor,
                *[item for item in existing["donors"] if item != normalized_donor],
            ][: int(existing["max_backups"])]
            if donors == existing["donors"]:
                return existing
            updated = {
                **existing,
                "donors": donors,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            entries = dict(catalog["entries"])
            entries[bootstrap_id] = normalize_bootstrap_record(updated)
            self._write({"version": 2, "entries": entries})
            return entries[bootstrap_id]

    def remove_donor(self, bootstrap_id: str, donor: Mapping[str, Any]) -> dict[str, Any]:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        normalized_donor = normalize_bootstrap_donor(donor)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            existing = catalog["entries"].get(bootstrap_id)
            if existing is None:
                raise ValueError(f"bootstrap does not exist: {bootstrap_id}")
            donors = [item for item in existing["donors"] if item != normalized_donor]
            if donors == existing["donors"]:
                return existing
            updated = {
                **existing,
                "donors": donors,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            entries = dict(catalog["entries"])
            entries[bootstrap_id] = normalize_bootstrap_record(updated)
            self._write({"version": 2, "entries": entries})
            return entries[bootstrap_id]

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
            entries[bootstrap_id] = normalize_bootstrap_record(updated)
            self._write({"version": 2, "entries": entries})
            return entries[bootstrap_id]

    def delete(self, bootstrap_id: str) -> bool:
        bootstrap_id = _validate_bootstrap_id(bootstrap_id)
        with exclusive_file_lock(self.lock_path):
            catalog = self._load()
            if bootstrap_id not in catalog["entries"]:
                return False
            entries = dict(catalog["entries"])
            del entries[bootstrap_id]
            self._write({"version": 2, "entries": entries})
            return True

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 2, "entries": {}}
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
