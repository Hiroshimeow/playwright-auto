from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from .cdpa_config import CDPAConfig
from .cdpa_team import (
    allocate_team,
    normalize_team_base,
    physical_role,
    validate_exact_team,
)
from .file_lock import exclusive_file_lock, fsync_parent_directory

SCHEMA_VERSION = 1
CATALOG_VERSION = 1
TERMINAL = frozenset({"DONE", "STOPPED"})
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_TASK_STATUSES = frozenset({"INBOX", "RUNNING", "PAUSED", "BLOCKED", "DONE", "STOPPED"})
_HOP_STATES = frozenset({"pre_send", "sending", "sent", "waiting", "responded", "routed", "abandoned"})
_CLEANUP_STATES = frozenset({"ACTIVE", "CLEARING", "CLEARED"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str, *, maximum: int = 72) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return (slug or "task")[:maximum].rstrip("-")


def generate_task_id(task: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(f"{task}\0{time.time_ns()}".encode()).hexdigest()[:8]
    return f"cdpa-{stamp}-{digest}"


def _validate_task_id(value: str) -> str:
    task_id = str(value).strip()
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return task_id


class TaskStore:
    def __init__(self, config: CDPAConfig) -> None:
        self.config = config
        self.root = config.plans_root
        self.allocation_lock = self.root / ".cdpa-allocation.lock"
        self.catalog_path = self.root / ".cdpa-catalog.json"

    def _lock_path(self, manifest_path: Path) -> Path:
        return manifest_path.with_suffix(manifest_path.suffix + ".lock")

    def _catalog_key(self, manifest_path: str | Path) -> str:
        target = Path(manifest_path).expanduser().resolve()
        try:
            return target.relative_to(self.root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"manifest path is outside the CDPA plans root: {target}"
            ) from exc

    def _catalog_entry(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "manifest_path": str(Path(state["manifest_path"]).expanduser().resolve()),
            "task_id": str(state["task_id"]),
            "team": str(state["team"]),
            "team_suffix": int(state.get("team_suffix") or 1),
            "status": str(state.get("status") or "INBOX").upper(),
            "updated_at": str(state.get("updated_at") or utc_now()),
        }

    def _write_catalog_unlocked(self, catalog: Mapping[str, Any]) -> None:
        value = json.loads(json.dumps(dict(catalog), ensure_ascii=False, default=str))
        value["version"] = CATALOG_VERSION
        value.setdefault("entries", {})
        value["updated_at"] = utc_now()
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.catalog_path.with_suffix(self.catalog_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.catalog_path)
        fsync_parent_directory(self.catalog_path)

    def _load_catalog_unlocked(self, *, reconcile: bool = True) -> dict[str, Any]:
        if self.catalog_path.exists():
            value = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION:
                raise ValueError(
                    f"unsupported CDPA catalog version in {self.catalog_path}"
                )
            entries = value.get("entries")
            if not isinstance(entries, dict):
                raise ValueError(f"CDPA catalog entries must be an object: {self.catalog_path}")
        else:
            value = {
                "version": CATALOG_VERSION,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "entries": {},
            }

        changed = not self.catalog_path.exists()
        if reconcile:
            entries = value["entries"]
            for path in self._filesystem_primary_paths():
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("schema_version") != SCHEMA_VERSION:
                        continue
                    entry = self._catalog_entry(state)
                except Exception:
                    continue
                key = self._catalog_key(path)
                if entries.get(key) != entry:
                    entries[key] = entry
                    changed = True
        if changed:
            self._write_catalog_unlocked(value)
        return value

    def _catalog_record_associates_team(
        self,
        key: str,
        entry: Any,
        team: str,
    ) -> bool:
        if key == team or key.startswith(f"{team}/"):
            return True
        if not isinstance(entry, Mapping):
            return False
        if entry.get("team") == team:
            return True
        raw_path = entry.get("manifest_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return False
        try:
            relative = Path(raw_path).expanduser().resolve().relative_to(self.root.resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(relative.parts and relative.parts[0] == team)

    def _catalog_record_error(self, key: str, entry: Any) -> str | None:
        if not isinstance(key, str) or not key:
            return "catalog key must be a non-empty string"
        if chr(92) in key:
            return "catalog key must use POSIX separators"
        key_path = PurePosixPath(key)
        parts = key_path.parts
        if key_path.is_absolute() or len(parts) != 3 or any(
            part in {"", ".", ".."} for part in parts
        ):
            return "catalog key must use <team>/<task-id>/<manifest>.json"
        key_team, key_task_id, filename = parts
        try:
            validate_exact_team(key_team)
        except ValueError as exc:
            return f"catalog key team is invalid: {exc}"
        try:
            _validate_task_id(key_task_id)
        except ValueError as exc:
            return f"catalog key task ID is invalid: {exc}"
        if filename == "requests.json" or not filename.endswith(".json"):
            return "catalog key filename is not a primary-manifest JSON candidate"
        if not isinstance(entry, Mapping):
            return "catalog entry must be an object"
        raw_path = entry.get("manifest_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return "catalog entry manifest_path must be a non-empty string"
        try:
            target = Path(raw_path).expanduser().resolve()
            relative = target.relative_to(self.root.resolve())
        except (OSError, RuntimeError, ValueError):
            return "catalog entry manifest_path must be inside the plans root"
        if len(relative.parts) != 3:
            return "catalog entry manifest_path must use <team>/<task-id>/<manifest>.json"
        if str(target) != raw_path:
            return "catalog entry manifest_path must be absolute and canonical"
        if relative.as_posix() != key:
            return "catalog key does not match catalog entry manifest_path"
        team = entry.get("team")
        if not isinstance(team, str) or not team:
            return "catalog entry team must be a non-empty string"
        try:
            validate_exact_team(team)
        except ValueError as exc:
            return f"catalog entry team is invalid: {exc}"
        if team != key_team:
            return "catalog entry team does not match the catalog key"
        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return "catalog entry task_id must be a non-empty string"
        try:
            _validate_task_id(task_id)
        except ValueError as exc:
            return f"catalog entry task ID is invalid: {exc}"
        if task_id != key_task_id:
            return "catalog entry task_id does not match the catalog key"
        suffix = entry.get("team_suffix")
        if isinstance(suffix, bool) or not isinstance(suffix, int) or suffix < 1:
            return "catalog entry team_suffix must be a positive integer"
        status = entry.get("status")
        if not isinstance(status, str) or status.upper() not in _TASK_STATUSES:
            return "catalog entry status must be a supported task-status string"
        updated_at = entry.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            return "catalog entry updated_at must be a non-empty string"
        return None

    def _catalog_records_for_exact_team(
        self,
        catalog: Mapping[str, Any],
        team: str,
    ) -> dict[Path, tuple[str, Mapping[str, Any]]]:
        records: list[tuple[Path, str, Mapping[str, Any], bool]] = []
        for raw_key, raw_entry in catalog["entries"].items():
            key = str(raw_key)
            associated = self._catalog_record_associates_team(key, raw_entry, team)
            error = self._catalog_record_error(key, raw_entry)
            if error is not None:
                if associated:
                    raise ValueError(
                        f"exact team {team!r} has corrupt raw catalog entry {key!r}: {error}"
                    )
                continue
            entry = raw_entry
            target = Path(str(entry["manifest_path"])).expanduser().resolve()
            associated = associated or self._catalog_record_associates_team(
                key, entry, team
            )
            records.append((target, key, entry, associated))

        by_path: dict[Path, list[tuple[str, Mapping[str, Any], bool]]] = {}
        for target, key, entry, associated in records:
            by_path.setdefault(target, []).append((key, entry, associated))
        for target, duplicates in by_path.items():
            if len(duplicates) > 1 and any(item[2] for item in duplicates):
                keys = sorted(item[0] for item in duplicates)
                raise ValueError(
                    f"exact team {team!r} has duplicate raw catalog entries for "
                    f"{target}: {keys!r}"
                )

        return {
            target: (items[0][0], items[0][1])
            for target, items in by_path.items()
            if items[0][2]
        }

    def _sync_catalog_entry(self, state: Mapping[str, Any]) -> None:
        target = Path(str(state.get("manifest_path") or "")).expanduser().resolve()
        error = self._manifest_value_error(target, state)
        if error is not None:
            raise ValueError(f"refusing to catalog invalid CDPA task manifest {target}: {error}")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=True)
            key = self._catalog_key(state["manifest_path"])
            entry = self._catalog_entry(state)
            if catalog["entries"].get(key) != entry:
                catalog["entries"][key] = entry
                self._write_catalog_unlocked(catalog)

    def _manifest_value_error(
        self,
        path: str | Path,
        state: Mapping[str, Any],
        *,
        catalog_key: str | None = None,
        catalog_entry: Mapping[str, Any] | None = None,
        require_unique: bool = True,
        require_file: bool = True,
    ) -> str | None:
        candidate = Path(path).expanduser().resolve()
        if (require_file and not candidate.is_file()) or candidate.name == "requests.json":
            return "path is not a candidate primary manifest"
        try:
            relative = candidate.relative_to(self.root.resolve())
        except ValueError:
            return "manifest is outside the plans root"
        if len(relative.parts) != 3:
            return "manifest path must use <team>/<task-id>/<file>.json"
        team, task_id, filename = relative.parts
        if not filename.endswith(".json") or filename.endswith(".lock"):
            return "manifest filename is not canonical JSON"
        if not isinstance(state, Mapping) or state.get("schema_version") != SCHEMA_VERSION:
            return "unsupported or missing task manifest schema version"

        required_strings = (
            "manifest_path",
            "task_id",
            "task_title",
            "task_text",
            "task_slug",
            "repository",
            "team_base",
            "team",
            "status",
            "kanban_column",
            "active_action",
            "created_at",
            "updated_at",
        )
        for key in required_strings:
            if not isinstance(state.get(key), str) or not str(state.get(key)).strip():
                return f"task manifest field {key!r} must be a non-empty string"

        try:
            declared = Path(str(state["manifest_path"])).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return "manifest_path cannot be resolved"
        if declared != candidate:
            return "manifest_path does not match the actual file"
        if str(state["team"]) != team or str(state["task_id"]) != task_id:
            return "team/task identity does not match the directory layout"
        try:
            _validate_task_id(task_id)
        except ValueError as exc:
            return str(exc)

        task_slug = str(state["task_slug"])
        if task_slug != slugify(str(state["task_text"])):
            return "task_slug does not match the canonical task text slug"
        if filename != f"{task_slug}.json":
            return "manifest filename does not match task_slug"

        team_base = str(state["team_base"])
        suffix = state.get("team_suffix")
        if isinstance(suffix, bool) or not isinstance(suffix, int) or suffix < 1:
            return "team_suffix must be a positive integer"
        expected_team = team_base if suffix == 1 else f"{team_base}{suffix}"
        if team != expected_team:
            return "team does not match team_base/team_suffix"
        status = str(state["status"]).upper()
        if status not in _TASK_STATUSES:
            return f"unsupported task status {status!r}"

        typed_fields = {
            "roles": Mapping,
            "hops": list,
            "reports": list,
            "controls": list,
            "cleanup": Mapping,
            "route_timeline": list,
            "errors": list,
            "options": Mapping,
        }
        for key, expected_type in typed_fields.items():
            if not isinstance(state.get(key), expected_type):
                return f"task manifest field {key!r} has invalid type"

        roles = state["roles"]
        configured_roles = tuple(str(role).upper() for role in self.config.roles)
        if set(roles) != set(configured_roles):
            return "roles must contain exactly the configured logical roles"
        for logical in configured_roles:
            record = roles.get(logical)
            if not isinstance(record, Mapping):
                return f"role record {logical!r} must be an object"
            if str(record.get("logical_role") or "").upper() != logical:
                return f"role record {logical!r} has inconsistent logical_role"
            expected_physical = physical_role(logical, team_base, suffix)
            legacy_physical = f"{logical}{'' if suffix == 1 else suffix}"
            if str(record.get("physical_role") or "") not in {
                expected_physical,
                legacy_physical,
            }:
                return f"role record {logical!r} has inconsistent physical_role"
            turn = record.get("turn")
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
                return f"role record {logical!r} has invalid turn"
            if not isinstance(record.get("status"), str):
                return f"role record {logical!r} has invalid status"
            if not isinstance(record.get("online"), bool):
                return f"role record {logical!r} has invalid online flag"

        hops = state["hops"]
        if not hops:
            return "task manifest must contain at least one hop"
        hop_by_id: dict[int, Mapping[str, Any]] = {}
        for hop in hops:
            if not isinstance(hop, Mapping):
                return "each hop must be an object"
            hop_id = hop.get("hop_id")
            if isinstance(hop_id, bool) or not isinstance(hop_id, int) or hop_id < 1:
                return "hop_id must be a positive integer"
            if hop_id in hop_by_id:
                return "hop_id values must be unique"
            target_role = str(hop.get("target_role") or "").upper()
            source_role = hop.get("source_role")
            if target_role not in roles:
                return f"hop {hop_id} targets an unknown role"
            if source_role is not None and str(source_role).upper() not in roles:
                return f"hop {hop_id} has an unknown source role"
            if str(hop.get("physical_role") or "") != str(roles[target_role]["physical_role"]):
                return f"hop {hop_id} physical role does not match its target role"
            turn = hop.get("turn")
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
                return f"hop {hop_id} has invalid turn"
            if str(hop.get("state") or "") not in _HOP_STATES:
                return f"hop {hop_id} has invalid state"
            if str(hop.get("request_id") or "") != f"{task_id}-hop{hop_id}":
                return f"hop {hop_id} request_id is inconsistent"
            if not isinstance(hop.get("handoff"), str) or not str(hop.get("handoff")).strip():
                return f"hop {hop_id} has invalid handoff"
            ledger_path = Path(str(hop.get("ledger_path") or "")).expanduser().resolve()
            if ledger_path != (candidate.parent / "requests.json").resolve():
                return f"hop {hop_id} ledger_path is inconsistent"
            if not isinstance(hop.get("wait"), Mapping):
                return f"hop {hop_id} wait state must be an object"
            if not isinstance(hop.get("timestamps"), Mapping):
                return f"hop {hop_id} timestamps must be an object"
            if not isinstance(hop.get("errors"), list):
                return f"hop {hop_id} errors must be a list"
            hop_by_id[hop_id] = hop

        active_role = state.get("active_role")
        active_hop_id = state.get("active_hop_id")
        if status in TERMINAL:
            if active_role is not None or active_hop_id is not None:
                return "terminal task must not have an active role or hop"
        else:
            if str(active_role or "").upper() not in roles:
                return "nonterminal task must have a configured active role"
            if isinstance(active_hop_id, bool) or not isinstance(active_hop_id, int):
                return "nonterminal task must have an integer active_hop_id"
            active_hop = hop_by_id.get(active_hop_id)
            if active_hop is None:
                return "active_hop_id does not reference an existing hop"
            if str(active_hop.get("target_role") or "").upper() != str(active_role).upper():
                return "active role does not match the active hop target"

        cleanup = state["cleanup"]
        cleanup_state = str(cleanup.get("state") or "")
        if not cleanup_state:
            cleanup_state = "CLEARED" if cleanup.get("cleared_at") else "ACTIVE"
        if cleanup_state not in _CLEANUP_STATES:
            return "cleanup state is invalid"
        cleanup_phase = cleanup.get("phase")
        if cleanup_phase not in {
            None,
            "stop_pending",
            "close_pending",
            "closing",
            "verify_pending",
            "cleared",
        }:
            return "cleanup phase is invalid"
        if cleanup_state == "ACTIVE" and cleanup_phase is not None:
            return "active cleanup must not have an in-progress phase"
        if cleanup_state == "CLEARING" and cleanup_phase not in {
            None,
            "stop_pending",
            "close_pending",
            "closing",
            "verify_pending",
        }:
            return "clearing cleanup must have a resumable phase"
        if cleanup_state == "CLEARED" and cleanup_phase not in {None, "cleared"}:
            return "cleared cleanup has an inconsistent phase"

        if catalog_key is not None and catalog_key != relative.as_posix():
            return "catalog key does not match the primary manifest path"
        if catalog_entry is not None:
            canonical_entry = self._catalog_entry(state)
            for field in (
                "manifest_path",
                "task_id",
                "team",
                "team_suffix",
                "status",
                "updated_at",
            ):
                if field not in catalog_entry:
                    return f"catalog entry is missing canonical field {field!r}"
                if catalog_entry[field] != canonical_entry[field]:
                    return f"catalog field {field!r} does not match the primary manifest"

        if require_unique:
            valid_siblings: list[Path] = [candidate]
            for sibling in candidate.parent.glob("*.json"):
                sibling = sibling.resolve()
                if (
                    sibling == candidate
                    or sibling.name == "requests.json"
                    or not sibling.is_file()
                ):
                    continue
                try:
                    sibling_state = json.loads(sibling.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(sibling_state, Mapping)
                    and self._manifest_value_error(
                        sibling, sibling_state, require_unique=False
                    )
                    is None
                ):
                    valid_siblings.append(sibling)
            if valid_siblings != [candidate]:
                return "task directory must contain exactly one canonical primary manifest"
        return None

    def _primary_manifest_state(
        self,
        path: str | Path,
        *,
        catalog_key: str | None = None,
        catalog_entry: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        candidate = Path(path).expanduser().resolve()
        if not candidate.is_file():
            return None
        try:
            state = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(state, Mapping):
            return None
        if self._manifest_value_error(
            candidate,
            state,
            catalog_key=catalog_key,
            catalog_entry=catalog_entry,
        ) is not None:
            return None
        return dict(state)

    def _filesystem_manifest_like_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        candidates: list[Path] = []
        for path in self.root.glob("*/*/*.json"):
            if not path.is_file() or path.name == "requests.json" or path.name.endswith(".lock"):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                candidates.append(path.resolve())
                continue
            if not isinstance(value, Mapping):
                continue
            if value.get("schema_version") == SCHEMA_VERSION or {
                "manifest_path",
                "task_id",
                "team",
            }.issubset(value):
                candidates.append(path.resolve())
        return sorted(set(candidates))

    def _filesystem_reservations(self) -> list[dict[str, Any]]:
        reservations: list[dict[str, Any]] = []
        for path in self._filesystem_manifest_like_paths():
            if self._primary_manifest_state(path) is not None:
                continue
            relative = path.relative_to(self.root.resolve())
            team, task_id, _filename = relative.parts
            reservations.append(
                {
                    "manifest_path": str(path),
                    "task_id": task_id,
                    "team": team,
                    "status": "CORRUPT",
                }
            )
        return reservations

    @staticmethod
    def _team_suffix_reservation(base: str, team: str) -> int | None:
        if team == base:
            return 1
        if team.startswith(base):
            ending = team[len(base) :]
            if ending.isdigit() and int(ending) >= 2:
                return int(ending)
        return None

    def _filesystem_primary_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        primary = [
            path.resolve()
            for path in self._filesystem_manifest_like_paths()
            if self._primary_manifest_state(path) is not None
        ]
        return sorted(set(primary))

    def _catalog_existing_paths(self) -> list[Path]:
        if not self.catalog_path.exists():
            return []
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        entries = catalog.get("entries") if isinstance(catalog, Mapping) else None
        if not isinstance(entries, Mapping):
            return []
        paths: list[Path] = []
        for key, entry in entries.items():
            if not isinstance(entry, Mapping):
                continue
            raw = str(entry.get("manifest_path") or "").strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser().resolve()
            if self._primary_manifest_state(
                candidate,
                catalog_key=str(key),
                catalog_entry=entry,
            ) is not None:
                paths.append(candidate)
        return sorted(set(paths))

    def discover_paths(self) -> list[Path]:
        return sorted(set(self._filesystem_primary_paths()) | set(self._catalog_existing_paths()))

    def discover(self) -> list[dict[str, Any]]:
        return [self.load(path) for path in self.discover_paths()]

    def discover_with_errors(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        tasks: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        paths = self.discover_paths()
        for path in paths:
            try:
                tasks.append(self.load(path))
            except Exception as exc:
                errors.append(
                    {
                        "manifest_path": str(path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        diagnosed = {item["manifest_path"] for item in errors}
        for candidate in self._filesystem_manifest_like_paths():
            if candidate in paths or str(candidate) in diagnosed:
                continue
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                detail = f"{type(exc).__name__}: {exc}"
            else:
                detail = self._manifest_value_error(candidate, value) or "not a unique primary manifest"
            errors.append(
                {
                    "manifest_path": str(candidate),
                    "error": f"InvalidManifestError: {detail}",
                }
            )
            diagnosed.add(str(candidate))

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(self.allocation_lock):
                catalog = self._load_catalog_unlocked(reconcile=True)
        except Exception as exc:
            errors.append(
                {
                    "manifest_path": str(self.catalog_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return tasks, errors

        for key, entry in catalog["entries"].items():
            if not isinstance(entry, Mapping):
                continue
            manifest_path = str(entry.get("manifest_path") or "")
            if not manifest_path or manifest_path in diagnosed:
                continue
            candidate = Path(manifest_path).expanduser().resolve()
            if self._primary_manifest_state(
                candidate,
                catalog_key=str(key),
                catalog_entry=entry,
            ) is not None:
                continue
            if candidate.exists():
                error = (
                    "InvalidManifestError: cataloged path is not a canonical primary "
                    "CDPA manifest; it is reserved for diagnostics only"
                )
            else:
                error = (
                    "MissingManifestError: cataloged CDPA manifest is missing; "
                    "restore the exact file before reusing its team slot"
                )
            errors.append(
                {
                    "manifest_path": manifest_path,
                    "error": error,
                }
            )
        return tasks, errors

    def load(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self._lock_path(target)):
            value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid CDPA task manifest in {target}: root must be an object")
        error = self._manifest_value_error(target, value)
        if error is not None:
            raise ValueError(f"invalid CDPA task manifest in {target}: {error}")
        return dict(value)

    def _save_unlocked(self, target: Path, state: Mapping[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        value["schema_version"] = SCHEMA_VERSION
        value["updated_at"] = utc_now()
        error = self._manifest_value_error(target, value, require_file=False)
        if error is not None:
            raise ValueError(f"refusing to write invalid CDPA task manifest {target}: {error}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_parent_directory(target)
        return value

    def save(self, path: str | Path, state: Mapping[str, Any]) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self._lock_path(target)):
            saved = self._save_unlocked(target, state)
        self._sync_catalog_entry(saved)
        return saved

    def update(
        self,
        path: str | Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self._lock_path(target)):
            current = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(current, Mapping):
                raise ValueError(f"invalid CDPA task manifest in {target}: root must be an object")
            current_error = self._manifest_value_error(target, current)
            if current_error is not None:
                raise ValueError(f"invalid CDPA task manifest in {target}: {current_error}")
            current = dict(current)
            result = mutator(current)
            saved = self._save_unlocked(
                target,
                result if result is not None else current,
            )
        self._sync_catalog_entry(saved)
        return saved

    @contextmanager
    def task_run_lock(self, path: str | Path, *, blocking: bool = False) -> Iterator[None]:
        target = Path(path).expanduser().resolve()
        lock = target.with_suffix(target.suffix + ".run.lock")
        with exclusive_file_lock(lock, blocking=blocking):
            yield

    def create_task(
        self,
        task: str,
        *,
        requested_team: str | None = None,
        new_roles: Sequence[str] = (),
        new_all: bool = False,
        repository: str | Path | None = None,
        task_id: str | None = None,
        reserved_team_suffixes: Sequence[int] = (),
    ) -> dict[str, Any]:
        text = str(task).strip()
        if not text:
            raise ValueError("task must not be empty")
        task_id = _validate_task_id(task_id or generate_task_id(text))
        normalized_new = tuple(dict.fromkeys(str(role).strip().upper() for role in new_roles))
        unknown = set(normalized_new) - set(self.config.roles)
        if unknown:
            raise ValueError(f"unknown --new roles: {sorted(unknown)!r}")
        base = normalize_team_base(requested_team or task_id)
        repository_path = Path(repository or self.config.repository_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=True)
            catalog_entries = [
                entry
                for entry in catalog["entries"].values()
                if isinstance(entry, Mapping)
            ]
            filesystem_reservations = self._filesystem_reservations()
            catalog_task_ids = {
                str(entry.get("task_id") or "")
                for entry in catalog_entries
                if str(entry.get("task_id") or "")
            }
            if task_id in catalog_task_ids:
                raise ValueError(f"task_id is already cataloged: {task_id}")
            filesystem_task_ids = {
                str(entry.get("task_id") or "")
                for entry in filesystem_reservations
                if str(entry.get("task_id") or "")
            }
            if task_id in filesystem_task_ids:
                raise ValueError(f"task_id is already reserved by corrupt filesystem state: {task_id}")

            manifests = self.discover()
            actual_paths = {
                str(Path(item["manifest_path"]).expanduser().resolve())
                for item in manifests
            }
            catalog_reservations = [
                {
                    "manifest_path": str(entry.get("manifest_path") or ""),
                    "task_id": str(entry.get("task_id") or ""),
                    "team": str(entry.get("team") or ""),
                    "team_suffix": int(entry.get("team_suffix") or 1),
                    "status": "MISSING_OR_INVALID",
                }
                for entry in catalog_entries
                if str(entry.get("manifest_path") or "") not in actual_paths
            ]
            inferred_suffixes = {
                suffix
                for item in filesystem_reservations
                if (suffix := self._team_suffix_reservation(base, str(item.get("team") or "")))
                is not None
            }
            team, suffix = allocate_team(
                base,
                [*manifests, *catalog_reservations, *filesystem_reservations],
                reserved_suffixes=(*reserved_team_suffixes, *sorted(inferred_suffixes)),
            )
            terminal_candidates = sorted(
                (
                    item
                    for item in manifests
                    if str(item.get("status") or "").upper() in TERMINAL
                    and int(item.get("team_suffix") or 1) == suffix
                ),
                key=lambda item: str(
                    item.get("completed_at")
                    or item.get("stopped_at")
                    or item.get("updated_at")
                    or item.get("created_at")
                    or ""
                ),
                reverse=True,
            )
            reusable_teams = list(
                dict.fromkeys(
                    str(item.get("team"))
                    for item in terminal_candidates
                    if item.get("team")
                )
            )
            title_slug = slugify(text)
            target = (self.root / team / task_id / f"{title_slug}.json").resolve()
            if target.exists():
                raise FileExistsError(target)
            now = utc_now()
            roles = {}
            for logical in self.config.roles:
                roles[logical] = {
                    "logical_role": logical,
                    "physical_role": physical_role(logical, base, suffix),
                    "status": "pending" if logical == "PLAN" else "unallocated",
                    "turn": 0,
                    "page_id": None,
                    "page_url": None,
                    "online": False,
                    "conversation_generation": 0,
                    "constructor_sent_generation": None,
                    "reset_requested": bool(new_all or logical in normalized_new),
                    "reset_applied_generation": None,
                    "last_activity_at": None,
                    "last_error": None,
                }
            hop = {
                "hop_id": 1,
                "parent_hop_id": None,
                "source_role": None,
                "target_role": "PLAN",
                "physical_role": roles["PLAN"]["physical_role"],
                "turn": 1,
                "kind": "task",
                "handoff": text,
                "state": "pre_send",
                "request_id": f"{task_id}-hop1",
                "prompt": None,
                "prompt_sha256": None,
                "rendered_prompt_sha256": None,
                "ledger_path": str((target.parent / "requests.json").resolve()),
                "receipt": None,
                "message_identity": None,
                "response": None,
                "response_sha256": None,
                "report_path": None,
                "report_sha256": None,
                "report_size": None,
                "route": None,
                "repair_attempt": 0,
                "validation_error": None,
                "wait": {
                    "started_at": None,
                    "deadline_at": None,
                    "continuous_responding_since": None,
                    "activity_signature": None,
                    "activity_length": 0,
                    "activity_changed_at": None,
                    "activity_observed_at": None,
                    "transport_ui_active": False,
                    "last_stop_visible": False,
                    "refresh_count": 0,
                    "last_refresh_at": None,
                    "refresh_in_progress": None,
                    "recovery_baseline": None,
                },
                "timestamps": {"created_at": now},
                "errors": [],
            }
            state = {
                "schema_version": SCHEMA_VERSION,
                "manifest_path": str(target),
                "task_id": task_id,
                "task_title": text.splitlines()[0],
                "task_text": text,
                "task_slug": title_slug,
                "repository": str(repository_path),
                "requested_team": requested_team,
                "team_base": base,
                "team": team,
                "team_suffix": suffix,
                "reusable_teams": reusable_teams,
                "status": "INBOX",
                "kanban_column": "INBOX",
                "terminal_state": None,
                "active_role": "PLAN",
                "active_hop_id": 1,
                "active_action": "queued",
                "pause_reason": None,
                "block_code": None,
                "block_retryable": False,
                "block_reason": None,
                "stop_reason": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "stopped_at": None,
                "last_role_activity_at": None,
                "options": {"new_roles": list(normalized_new), "new_all": bool(new_all)},
                "roles": roles,
                "hops": [hop],
                "reports": [],
                "route_timeline": [],
                "controls": [],
                "errors": [],
                "cleanup": {
                    "state": "ACTIVE",
                    "phase": None,
                    "eligible_at": None,
                    "clear_requested_at": None,
                    "cleared_at": None,
                    "verified_empty_at": None,
                    "closed_tabs": 0,
                    "target_tabs": 0,
                    "retry_count": 0,
                    "last_error": None,
                    "last_error_at": None,
                    "status_before": None,
                    "terminal_state_before": None,
                    "active_role": None,
                    "active_hop_id": None,
                    "control_id": None,
                },
            }
            saved = self._save_unlocked(target, state)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    def _queue_resume(
        self,
        state: dict[str, Any],
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        status = str(state.get("status") or "").upper()
        if status in TERMINAL:
            raise ValueError(
                f"team {state.get('team')!r} has no resumable task; status is {status}"
            )
        pending = next(
            (
                item
                for item in state.get("controls") or []
                if isinstance(item, Mapping)
                and item.get("action") == "resume"
                and item.get("status") == "requested"
            ),
            None,
        )
        if pending is not None:
            return state
        sequence = len(state.get("controls") or []) + 1
        state.setdefault("controls", []).append(
            {
                "control_id": sequence,
                "action": "resume",
                "role": str(state.get("active_role") or "PLAN").upper(),
                "reason": str(reason or "").strip() or None,
                "status": "requested",
                "requested_at": utc_now(),
                "applied_at": None,
                "result": None,
            }
        )
        return state

    def request_resume(
        self,
        path: str | Path,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self.update(path, lambda state: self._queue_resume(state, reason=reason))

    def resume_team(
        self,
        exact_team: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        team = validate_exact_team(exact_team)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            try:
                catalog = self._load_catalog_unlocked(reconcile=False)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"cannot resume team {team!r}: unreadable catalog {self.catalog_path}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            catalog_records = self._catalog_records_for_exact_team(catalog, team)
            candidate_paths: set[Path] = set(catalog_records)

            for path in self._filesystem_manifest_like_paths():
                relative = path.relative_to(self.root.resolve())
                if relative.parts and relative.parts[0] == team:
                    candidate_paths.add(path)

            if not candidate_paths:
                raise ValueError(f"no CDPA task exists for exact team {team!r}")

            loaded: list[
                tuple[Path, dict[str, Any], tuple[str, Mapping[str, Any]] | None]
            ] = []
            for target in sorted(candidate_paths):
                if not target.is_relative_to(self.root.resolve()):
                    raise ValueError(
                        f"exact team {team!r} has corrupt catalog metadata outside the plans root: {target}"
                    )
                if not target.exists():
                    raise ValueError(
                        f"exact team {team!r} has a cataloged manifest that is missing: {target}"
                    )
                try:
                    with exclusive_file_lock(self._lock_path(target)):
                        state = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"exact team {team!r} has a corrupt unreadable manifest {target}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if not isinstance(state, Mapping):
                    raise ValueError(
                        f"exact team {team!r} has a corrupt manifest {target}: root must be an object"
                    )
                record = catalog_records.get(target)
                if record is not None:
                    key, entry = record
                    error = self._manifest_value_error(
                        target,
                        state,
                        catalog_key=key,
                        catalog_entry=entry,
                    )
                    if error is not None:
                        raise ValueError(
                            f"exact team {team!r} has a corrupt cataloged manifest {target}: {error}"
                        )
                else:
                    error = self._manifest_value_error(target, state)
                    if error is not None:
                        raise ValueError(
                            f"exact team {team!r} has a corrupt manifest {target}: {error}"
                        )
                loaded.append((target, dict(state), record))

            resumable = [
                item
                for item in loaded
                if str(item[1].get("status") or "").upper() not in TERMINAL
            ]
            if len(resumable) > 1:
                paths = ", ".join(str(path) for path, _state, _records in resumable)
                raise ValueError(
                    f"duplicate nonterminal manifests for exact team {team!r}: {paths}"
                )
            if not resumable:
                statuses = sorted(
                    {
                        str(state.get("status") or "unknown").upper()
                        for _path, state, _records in loaded
                    }
                )
                raise ValueError(
                    f"exact team {team!r} has no resumable nonterminal task; statuses={statuses}"
                )

            target, _snapshot, record = resumable[0]
            with exclusive_file_lock(self._lock_path(target)):
                try:
                    current = json.loads(target.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"exact team {team!r} became corrupt while resuming {target}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if not isinstance(current, Mapping):
                    raise ValueError(
                        f"exact team {team!r} became corrupt while resuming {target}: root must be an object"
                    )
                if record is not None:
                    key, entry = record
                    error = self._manifest_value_error(
                        target,
                        current,
                        catalog_key=key,
                        catalog_entry=entry,
                    )
                    if error is not None:
                        raise ValueError(
                            f"exact team {team!r} became corrupt while resuming {target}: {error}"
                        )
                else:
                    error = self._manifest_value_error(target, current)
                    if error is not None:
                        raise ValueError(
                            f"exact team {team!r} became corrupt while resuming {target}: {error}"
                        )
                if str(current.get("status") or "").upper() in TERMINAL:
                    raise ValueError(f"exact team {team!r} became terminal while resuming")
                queued = self._queue_resume(dict(current), reason=reason or "exact-team resume")
                saved = self._save_unlocked(target, queued)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    def request_control(
        self,
        path: str | Path,
        action: str,
        *,
        role: str | None = None,
        reason: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        allowed = {"pause", "resume", "retry", "stop", "restart_role", "open_tab", "new_chat", "route_plan", "clear_team"}
        action = str(action).strip().lower()
        if action not in allowed:
            raise ValueError(f"unsupported control action {action!r}")
        if role is not None:
            role = str(role).strip().upper()
            if role not in self.config.roles:
                raise ValueError(f"unsupported control role {role!r}")
        if action == "resume":
            return self.request_resume(path, reason=reason or "resume requested")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            sequence = len(state.get("controls") or []) + 1
            state.setdefault("controls", []).append(
                {
                    "control_id": sequence,
                    "action": action,
                    "role": role,
                    "reason": str(reason or "").strip() or None,
                    "confirmed": bool(confirmed),
                    "status": "requested",
                    "requested_at": utc_now(),
                    "applied_at": None,
                    "result": None,
                }
            )
            return state

        return self.update(path, mutate)
