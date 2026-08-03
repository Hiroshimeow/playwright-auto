from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cdpa_config import CDPAConfig
from .cdpa_independent import normalize_system_prompt
from .file_lock import exclusive_file_lock, fsync_parent_directory

CATALOG_VERSION = 1
_ROUTE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_CUSTOM_PREFIX = "WF_"
_MAX_DISPLAY_NAME = 80
SYSTEM_WORKFLOW_ROUTE_ORDER = ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT")


def ordered_system_routes(config: CDPAConfig) -> tuple[str, ...]:
    configured = set(config.roles)
    return tuple(route for route in SYSTEM_WORKFLOW_ROUTE_ORDER if route in configured)


def validate_workflow_route_key(value: Any) -> str:
    route_key = str(value or "").strip().upper()
    if not _ROUTE_KEY.fullmatch(route_key):
        raise ValueError(
            "workflow route key must start with a letter and use at most 32 "
            "uppercase letters, digits, or underscores"
        )
    return route_key


def normalize_workflow_display_name(value: Any) -> str:
    display_name = str(value or "").strip()
    if not display_name:
        raise ValueError("workflow agent display name must not be empty")
    if len(display_name) > _MAX_DISPLAY_NAME:
        raise ValueError(
            f"workflow agent display name must be at most {_MAX_DISPLAY_NAME} characters"
        )
    return display_name


def normalize_workflow_definition(
    value: Mapping[str, Any],
    *,
    expected_route_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("workflow agent definition must be an object")
    route_key = validate_workflow_route_key(
        expected_route_key if expected_route_key is not None else value.get("route_key")
    )
    if expected_route_key is not None and validate_workflow_route_key(
        value.get("route_key")
    ) != route_key:
        raise ValueError("workflow agent definition route_key is inconsistent")
    is_system = value.get("is_system")
    if not isinstance(is_system, bool):
        raise ValueError("workflow agent is_system must be a boolean")
    deleted_at = value.get("deleted_at")
    if deleted_at is not None and (
        not isinstance(deleted_at, str) or not deleted_at.strip()
    ):
        raise ValueError("workflow agent deleted_at must be null or a non-empty string")
    return {
        "route_key": route_key,
        "display_name": normalize_workflow_display_name(value.get("display_name")),
        "system_prompt": normalize_system_prompt(value.get("system_prompt")),
        "is_system": is_system,
        "deleted_at": deleted_at,
    }


def workflow_snapshot(
    definitions: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in definitions:
        definition = normalize_workflow_definition(raw)
        route_key = definition["route_key"]
        if definition["deleted_at"] is not None:
            raise ValueError(f"workflow agent {route_key!r} is deleted")
        if route_key in result:
            raise ValueError("workflow agent snapshot contains duplicate route keys")
        result[route_key] = definition
    if "PLAN" not in result:
        raise ValueError("workflow agent snapshot must include PLAN")
    return result


def task_workflow_definitions(
    state: Mapping[str, Any], config: CDPAConfig
) -> dict[str, dict[str, Any]]:
    roles = state.get("roles")
    if not isinstance(roles, Mapping):
        raise ValueError("task roles must be an object")
    raw_snapshot = state.get("workflow_agents")
    if raw_snapshot is None:
        unknown = set(roles) - set(config.roles)
        if unknown:
            raise ValueError("legacy workflow task contains unsupported custom roles")
        return {
            role: {
                "route_key": role,
                "display_name": role,
                "system_prompt": config.constructor_paths[role]
                .read_text(encoding="utf-8")
                .strip(),
                "is_system": True,
                "deleted_at": None,
            }
            for role in ordered_system_routes(config)
            if role in roles
        }
    if not isinstance(raw_snapshot, Mapping):
        raise ValueError("workflow_agents must be an object")
    result: dict[str, dict[str, Any]] = {}
    for raw_key, raw_definition in raw_snapshot.items():
        route_key = validate_workflow_route_key(raw_key)
        definition = normalize_workflow_definition(
            raw_definition, expected_route_key=route_key
        )
        if definition["deleted_at"] is not None:
            raise ValueError("task workflow snapshot must not contain deleted definitions")
        if definition["is_system"] != (route_key in config.roles):
            raise ValueError("task workflow snapshot has inconsistent system identity")
        result[route_key] = definition
    if list(result) != list(roles):
        raise ValueError("workflow_agents keys must exactly match task role order")
    if "PLAN" not in result:
        raise ValueError("workflow task must include PLAN")
    return result


class WorkflowAgentCatalog:
    """One file-backed source of truth for mutable workflow agent definitions."""

    def __init__(self, config: CDPAConfig) -> None:
        self.config = config
        self.root = config.plans_root.resolve()
        self.path = self.root / "workflow-agents.json"
        self.lock_path = self.root / ".workflow-agents.lock"

    def _empty(self) -> dict[str, Any]:
        return {"version": CATALOG_VERSION, "entries": {}, "commands": {}}

    def _load_unlocked(self) -> dict[str, Any]:
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError("workflow agent catalog paths must not be symlinks")
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("workflow agent catalog contains malformed JSON") from exc
        if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION:
            raise ValueError("unsupported workflow agent catalog version")
        entries = value.get("entries")
        commands = value.get("commands")
        if not isinstance(entries, dict) or not isinstance(commands, dict):
            raise ValueError("workflow agent catalog entries and commands must be objects")
        normalized_entries: dict[str, dict[str, Any]] = {}
        for raw_key, raw in entries.items():
            route_key = validate_workflow_route_key(raw_key)
            definition = normalize_workflow_definition(raw, expected_route_key=route_key)
            if definition["is_system"] != (route_key in self.config.roles):
                raise ValueError(
                    f"workflow agent {route_key!r} has inconsistent system identity"
                )
            normalized_entries[route_key] = definition
        normalized_commands: dict[str, dict[str, str]] = {}
        for command_id, record in commands.items():
            if (
                not isinstance(command_id, str)
                or not command_id.strip()
                or not isinstance(record, Mapping)
                or not isinstance(record.get("kind"), str)
                or not isinstance(record.get("payload_sha256"), str)
                or not isinstance(record.get("route_key"), str)
            ):
                raise ValueError("workflow agent command provenance is malformed")
            normalized_commands[command_id] = {
                "kind": str(record["kind"]),
                "payload_sha256": str(record["payload_sha256"]),
                "route_key": validate_workflow_route_key(record["route_key"]),
            }
        return {
            "version": CATALOG_VERSION,
            "entries": normalized_entries,
            "commands": normalized_commands,
        }

    def _write_unlocked(self, value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError("workflow agent catalog paths must not be symlinks")
        data = json.dumps(
            dict(value), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.root,
                prefix="workflow-agents.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fsync_parent_directory(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _payload_hash(kind: str, payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            {"kind": kind, "payload": dict(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def _system_default(self, route_key: str) -> dict[str, Any]:
        return {
            "route_key": route_key,
            "display_name": route_key,
            "system_prompt": self.config.constructor_paths[route_key]
            .read_text(encoding="utf-8")
            .strip(),
            "is_system": True,
            "deleted_at": None,
        }

    def _effective_from(
        self,
        value: Mapping[str, Any],
        route_key: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        key = validate_workflow_route_key(route_key)
        raw = value["entries"].get(key)
        if key in self.config.roles:
            definition = self._system_default(key)
            if raw is not None:
                definition.update(
                    display_name=raw["display_name"],
                    system_prompt=raw["system_prompt"],
                )
            return definition
        if raw is None:
            return None
        definition = dict(raw)
        if definition["deleted_at"] is not None and not include_deleted:
            return None
        return definition

    def list_agents(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        with exclusive_file_lock(self.lock_path):
            value = self._load_unlocked()
            result = [
                self._effective_from(value, role)
                for role in ordered_system_routes(self.config)
            ]
            result.extend(
                self._effective_from(value, route_key, include_deleted=include_deleted)
                for route_key in sorted(value["entries"])
                if route_key not in self.config.roles
            )
            return [item for item in result if item is not None]

    def get(
        self, route_key: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        with exclusive_file_lock(self.lock_path):
            return self._effective_from(
                self._load_unlocked(), route_key, include_deleted=include_deleted
            )

    def resolve(self, route_keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        requested = [validate_workflow_route_key(item) for item in route_keys]
        if len(set(requested)) != len(requested):
            raise ValueError("workflow roles must not contain duplicates")
        if "PLAN" not in requested:
            raise ValueError("roles must include PLAN")
        with exclusive_file_lock(self.lock_path):
            value = self._load_unlocked()
            result: dict[str, dict[str, Any]] = {}
            for route_key in requested:
                definition = self._effective_from(value, route_key)
                if definition is None:
                    raise ValueError(
                        f"deleted or missing workflow agent: {route_key}"
                    )
                result[route_key] = definition
            return result

    def _replay(
        self,
        value: Mapping[str, Any],
        *,
        external_command_id: str | None,
        kind: str,
        payload_sha256: str,
    ) -> dict[str, Any] | None:
        command_id = str(external_command_id or "").strip()
        if not command_id:
            return None
        record = value["commands"].get(command_id)
        if record is None:
            return None
        if record["kind"] != kind or record["payload_sha256"] != payload_sha256:
            raise ValueError("workflow agent command provenance does not match payload")
        definition = self._effective_from(
            value, record["route_key"], include_deleted=True
        )
        if definition is None:
            raise ValueError("workflow agent command provenance references a missing identity")
        return definition

    @staticmethod
    def _record_command(
        value: dict[str, Any],
        *,
        external_command_id: str | None,
        kind: str,
        payload_sha256: str,
        route_key: str,
    ) -> None:
        command_id = str(external_command_id or "").strip()
        if command_id:
            value["commands"][command_id] = {
                "kind": kind,
                "payload_sha256": payload_sha256,
                "route_key": route_key,
            }

    def create(
        self,
        *,
        display_name: Any,
        system_prompt: Any,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        display = normalize_workflow_display_name(display_name)
        prompt = normalize_system_prompt(system_prompt)
        payload = {"display_name": display, "system_prompt": prompt}
        payload_sha256 = self._payload_hash("create", payload)
        with exclusive_file_lock(self.lock_path):
            value = self._load_unlocked()
            replay = self._replay(
                value,
                external_command_id=external_command_id,
                kind="create",
                payload_sha256=payload_sha256,
            )
            if replay is not None:
                return replay
            seed = str(external_command_id or uuid.uuid4())
            route_key = _CUSTOM_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:12].upper()
            while route_key in value["entries"] or route_key in self.config.roles:
                seed = str(uuid.uuid4())
                route_key = _CUSTOM_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:12].upper()
            definition = {
                "route_key": route_key,
                "display_name": display,
                "system_prompt": prompt,
                "is_system": False,
                "deleted_at": None,
            }
            value["entries"][route_key] = definition
            self._record_command(
                value,
                external_command_id=external_command_id,
                kind="create",
                payload_sha256=payload_sha256,
                route_key=route_key,
            )
            self._write_unlocked(value)
            return dict(definition)

    def update(
        self,
        route_key: str,
        *,
        display_name: Any,
        system_prompt: Any,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        key = validate_workflow_route_key(route_key)
        display = normalize_workflow_display_name(display_name)
        prompt = normalize_system_prompt(system_prompt)
        payload = {
            "route_key": key,
            "display_name": display,
            "system_prompt": prompt,
        }
        payload_sha256 = self._payload_hash("update", payload)
        with exclusive_file_lock(self.lock_path):
            value = self._load_unlocked()
            replay = self._replay(
                value,
                external_command_id=external_command_id,
                kind="update",
                payload_sha256=payload_sha256,
            )
            if replay is not None:
                return replay
            current = self._effective_from(value, key, include_deleted=True)
            if current is None:
                raise ValueError(f"workflow agent does not exist: {key}")
            if current["deleted_at"] is not None:
                raise ValueError(f"workflow agent is deleted: {key}")
            definition = {
                "route_key": key,
                "display_name": display,
                "system_prompt": prompt,
                "is_system": key in self.config.roles,
                "deleted_at": None,
            }
            value["entries"][key] = definition
            self._record_command(
                value,
                external_command_id=external_command_id,
                kind="update",
                payload_sha256=payload_sha256,
                route_key=key,
            )
            self._write_unlocked(value)
            return dict(definition)

    def delete(
        self,
        route_key: str,
        *,
        deleted_at: str | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        key = validate_workflow_route_key(route_key)
        if key in self.config.roles:
            raise ValueError("system workflow agents cannot be deleted")
        timestamp = deleted_at or datetime.now(timezone.utc).isoformat()
        payload = {"route_key": key}
        payload_sha256 = self._payload_hash("delete", payload)
        with exclusive_file_lock(self.lock_path):
            value = self._load_unlocked()
            replay = self._replay(
                value,
                external_command_id=external_command_id,
                kind="delete",
                payload_sha256=payload_sha256,
            )
            if replay is not None:
                return replay
            current = self._effective_from(value, key, include_deleted=True)
            if current is None:
                raise ValueError(f"workflow agent does not exist: {key}")
            definition = dict(current)
            if definition["deleted_at"] is None:
                definition["deleted_at"] = timestamp
                value["entries"][key] = definition
            self._record_command(
                value,
                external_command_id=external_command_id,
                kind="delete",
                payload_sha256=payload_sha256,
                route_key=key,
            )
            self._write_unlocked(value)
            return definition
