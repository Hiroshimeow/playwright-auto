from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from .cdpa_config import CDPAConfig
from .cdpa_dependencies import dependency_readiness, validate_new_dependencies
from .cdpa_team import (
    allocate_team,
    exact_team_ready_waiters,
    has_other_nonterminal_team_work,
    is_active_team_owner,
    is_team_availability_barrier,
    normalize_team_base,
    physical_role,
    queued_team_tasks,
    validate_exact_team,
)
from .file_lock import exclusive_file_lock, fsync_parent_directory
from .upload import collect_file_identities

SCHEMA_VERSION = 1
CATALOG_VERSION = 1
TERMINAL = frozenset({"DONE", "STOPPED"})
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_TASK_STATUSES = frozenset({"INBOX", "WAITING", "RUNNING", "PAUSED", "BLOCKED", "DONE", "STOPPED"})
_HOP_STATES = frozenset({"pre_send", "sending", "sent", "waiting", "responded", "routed", "abandoned"})
_CLEANUP_STATES = frozenset({"ACTIVE", "CLEARING", "CLEARED"})
_MAINTENANCE_STATES = frozenset({"OPEN", "RUNNING", "RESOLVED", "ESCALATED"})
_REPORT_MODES = frozenset({"file", "inline"})


class TeamWorkExistsError(RuntimeError):
    pass


def normalize_report_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("report_mode must be 'file' or 'inline'")
    mode = value.strip().lower()
    if mode not in _REPORT_MODES:
        raise ValueError("report_mode must be 'file' or 'inline'")
    return mode


def report_mode_from_options(options: Mapping[str, Any]) -> str:
    if "report_mode" not in options:
        return "file"
    return normalize_report_mode(options["report_mode"])


def normalize_dependency_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("depends_on_task_ids must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("dependency IDs must be non-empty strings")
        task_id = item.strip()
        if task_id in seen:
            raise ValueError(f"duplicate dependency ID: {task_id}")
        seen.add(task_id)
        result.append(task_id)
    return tuple(result)


def _optional_nonempty_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be null or a non-empty string")
    return value.strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def retained_report_references(
    state: Mapping[str, Any], *, limit: int = 20
) -> list[dict[str, Any]]:
    reports = state.get("reports")
    if not isinstance(reports, list) or limit <= 0:
        return []
    references: list[dict[str, Any]] = []
    for report in reports[-limit:]:
        if not isinstance(report, Mapping):
            continue
        path = str(report.get("path") or "").strip()
        if not path:
            continue
        references.append(
            {
                "report_id": report.get("report_id"),
                "physical_role": str(report.get("physical_role") or "") or None,
                "turn": report.get("turn"),
                "path": path,
            }
        )
    return references


def replacement_continuation_text(
    target: Mapping[str, Any], recovery_instruction: str
) -> str:
    task_id = str(target.get("task_id") or "").strip()
    original = str(target.get("task_text") or "").strip()
    repository = str(target.get("repository") or "").strip()
    instruction = str(recovery_instruction).strip()
    if not task_id or not original or not repository or not instruction:
        raise ValueError("replacement continuation context is incomplete")
    reports = retained_report_references(target)
    report_lines = [
        "- "
        + " ".join(
            (
                f"report_id={report['report_id']}",
                f"role={report['physical_role']}",
                f"turn={report['turn']}",
                f"path={report['path']}",
            )
        )
        for report in reports
    ] or ["- none"]
    return "\n".join(
        [
            f"Continue replaced CDPA task {task_id}.",
            "",
            "Maintainers recovery instruction:",
            instruction,
            "",
            "Original requested outcome:",
            original,
            "",
            "Repository/worktree:",
            repository,
            "",
            "Retained role reports from the replaced task:",
            *report_lines,
        ]
    )


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
        self.phase4_journal_path = self.root / ".cdpa-phase4-replacement.journal"

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

    def _load_exact_team_states_unlocked(
        self,
        team: str,
        catalog: Mapping[str, Any],
    ) -> list[tuple[Path, dict[str, Any], tuple[str, Mapping[str, Any]] | None]]:
        catalog_records = self._catalog_records_for_exact_team(catalog, team)
        candidate_paths: set[Path] = set(catalog_records)
        for path in self._filesystem_manifest_like_paths():
            relative = path.relative_to(self.root.resolve())
            if relative.parts and relative.parts[0] == team:
                candidate_paths.add(path)
        if not candidate_paths:
            raise ValueError(f"no CDPA task exists for exact team {team!r}")

        loaded = []
        for target in sorted(candidate_paths):
            if not target.is_relative_to(self.root.resolve()):
                raise ValueError(
                    f"exact team {team!r} has corrupt catalog metadata outside "
                    f"the plans root: {target}"
                )
            if not target.exists():
                raise ValueError(
                    f"exact team {team!r} has a cataloged manifest that is missing: "
                    f"{target}"
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
                    f"exact team {team!r} has a corrupt manifest {target}: "
                    "root must be an object"
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
                prefix = "corrupt cataloged manifest"
            else:
                error = self._manifest_value_error(target, state)
                prefix = "corrupt manifest"
            if error is not None:
                raise ValueError(
                    f"exact team {team!r} has a {prefix} {target}: {error}"
                )
            loaded.append((target, dict(state), record))
        return loaded

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
        try:
            report_mode_from_options(state["options"])
            dependencies = normalize_dependency_ids(state.get("depends_on_task_ids"))
            _optional_nonempty_string(state.get("replaces_task_id"), "replaces_task_id")
            _optional_nonempty_string(
                state.get("replacement_incident_id"),
                "replacement_incident_id",
            )
        except ValueError as exc:
            return f"task manifest {exc}"
        if task_id in dependencies:
            return "task cannot depend on itself"
        dependency_events = state.get("dependency_events")
        if dependency_events is not None:
            if not isinstance(dependency_events, list):
                return "dependency_events must be a list"
            if any(not isinstance(item, Mapping) for item in dependency_events):
                return "dependency event must be an object"
        waiting = state.get("waiting")
        if waiting is not None:
            if not isinstance(waiting, Mapping):
                return "waiting must be an object"
            if waiting.get("reason") not in {
                None,
                "dependency",
                "team_busy",
                "dependency_team_busy",
            }:
                return "waiting reason is invalid"
            for field in ("waiting_on", "stopped", "missing"):
                try:
                    normalize_dependency_ids(waiting.get(field, []))
                except ValueError as exc:
                    return f"waiting.{field} {exc}"
            blocked_by = waiting.get("blocked_by_task_id")
            if blocked_by is not None and (
                not isinstance(blocked_by, str) or not blocked_by.strip()
            ):
                return "waiting.blocked_by_task_id must be null or a non-empty string"
            since = waiting.get("since")
            if since is not None and (not isinstance(since, str) or not since.strip()):
                return "waiting.since must be null or a non-empty string"
        elif status == "WAITING":
            return "WAITING task must contain waiting state"

        for derived_field in (
            "queue_position",
            "queue_length",
            "owner_task_ids",
        ):
            if derived_field in state:
                return f"derived field {derived_field!r} must not be persisted"
        queue = state.get("queue")
        if queue is not None:
            if not isinstance(queue, Mapping):
                return "queue must be an object"
            if not isinstance(queue.get("reuse_team"), bool):
                return "queue.reuse_team must be a boolean"
            blocked_by = queue.get("blocked_by_task_id")
            if blocked_by is not None and (
                not isinstance(blocked_by, str) or not blocked_by.strip()
            ):
                return "queue.blocked_by_task_id must be null or a non-empty string"
            enqueued_at = queue.get("enqueued_at")
            if not isinstance(enqueued_at, str) or not enqueued_at.strip():
                return "queue.enqueued_at must be a non-empty string"
            released_at = queue.get("released_at")
            if released_at is not None and (
                not isinstance(released_at, str) or not released_at.strip()
            ):
                return "queue.released_at must be null or a non-empty string"
        queue_events = state.get("queue_events")
        if queue_events is not None:
            if not isinstance(queue_events, list):
                return "queue_events must be a list"
            if any(not isinstance(item, Mapping) for item in queue_events):
                return "queue event must be an object"

        attachments = state.get("attachments")
        if attachments is not None:
            if not isinstance(attachments, list):
                return "attachments must be a list"
            seen_attachment_paths: set[str] = set()
            seen_attachment_identities: set[tuple[str, str, int, str, str]] = set()
            required_attachment_keys = {"path", "name", "size", "sha256", "mime_type"}
            for attachment in attachments:
                if not isinstance(attachment, Mapping):
                    return "attachment must be an object"
                if set(attachment) != required_attachment_keys:
                    return "attachment keys are invalid"
                raw_path = attachment.get("path")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    return "attachment path must be a non-empty string"
                attachment_path = Path(raw_path).expanduser()
                if not attachment_path.is_absolute():
                    return "attachment path must be absolute"
                canonical_path = str(attachment_path.resolve())
                if canonical_path != raw_path:
                    return "attachment path must be canonical"
                name = attachment.get("name")
                if (
                    not isinstance(name, str)
                    or not name.strip()
                    or Path(name).name != name
                ):
                    return "attachment name must be a filename"
                size = attachment.get("size")
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    return "attachment size must be a non-negative integer"
                digest = attachment.get("sha256")
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    return "attachment sha256 must be a lowercase 64-character digest"
                mime_type = attachment.get("mime_type")
                if not isinstance(mime_type, str) or not mime_type.strip():
                    return "attachment mime_type must be a non-empty string"
                identity = (canonical_path, name, size, digest, mime_type)
                if canonical_path in seen_attachment_paths or identity in seen_attachment_identities:
                    return "attachment identities must be unique"
                seen_attachment_paths.add(canonical_path)
                seen_attachment_identities.add(identity)

        maintenance = state.get("maintenance")
        if maintenance is not None:
            if not isinstance(maintenance, Mapping):
                return "maintenance must be an object"
            incidents = maintenance.get("incidents")
            if not isinstance(incidents, list):
                return "maintenance incidents must be a list"
            incident_by_id: dict[str, Mapping[str, Any]] = {}
            for incident in incidents:
                if not isinstance(incident, Mapping):
                    return "maintenance incident must be an object"
                incident_id = incident.get("incident_id")
                if not isinstance(incident_id, str) or not incident_id.strip():
                    return "maintenance incident_id must be a non-empty string"
                if incident_id in incident_by_id:
                    return "maintenance incident IDs must be unique"
                if not isinstance(incident.get("key"), str) or not str(incident.get("key")).strip():
                    return "maintenance incident key must be a non-empty string"
                if str(incident.get("state") or "").upper() not in _MAINTENANCE_STATES:
                    return "maintenance incident state is invalid"
                incident_by_id[incident_id] = incident
            active_incident_id = maintenance.get("active_incident_id")
            if active_incident_id is not None:
                if not isinstance(active_incident_id, str) or not active_incident_id.strip():
                    return "maintenance active_incident_id must be null or a non-empty string"
                active_incident = incident_by_id.get(active_incident_id)
                if active_incident is None:
                    return "maintenance active_incident_id does not reference an incident"
                if str(active_incident.get("state") or "").upper() not in {"OPEN", "RUNNING"}:
                    return "maintenance active incident must be OPEN or RUNNING"

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
            uploaded_generation = record.get("attachments_uploaded_generation")
            conversation_generation = record.get("conversation_generation")
            if uploaded_generation is not None and (
                isinstance(uploaded_generation, bool)
                or not isinstance(uploaded_generation, int)
                or uploaded_generation < 0
                or not isinstance(conversation_generation, int)
                or isinstance(conversation_generation, bool)
                or uploaded_generation > conversation_generation
            ):
                return f"role record {logical!r} has invalid attachments_uploaded_generation"

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

    def _catalog_existing_paths_from(
        self,
        catalog: Mapping[str, Any],
    ) -> list[Path]:
        entries = catalog.get("entries")
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

    def _catalog_existing_paths(self) -> list[Path]:
        if not self.catalog_path.exists():
            return []
        try:
            catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(catalog, Mapping):
            return []
        return self._catalog_existing_paths_from(catalog)

    def discover_paths(self) -> list[Path]:
        return sorted(set(self._filesystem_primary_paths()) | set(self._catalog_existing_paths()))

    def discover(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in self.discover_paths():
            try:
                tasks.append(self.load(path))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
        return tasks

    def _discover_candidates(
        self,
        catalog: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        tasks: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        paths = (
            sorted(
                set(self._filesystem_primary_paths())
                | set(self._catalog_existing_paths_from(catalog))
            )
            if catalog is not None
            else self.discover_paths()
        )
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
        return tasks, errors

    def _filter_catalog_tasks(
        self,
        tasks: list[dict[str, Any]],
        errors: list[dict[str, str]],
        catalog: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        diagnosed = {item["manifest_path"] for item in errors}
        identity_paths: dict[tuple[str, str], list[Path]] = {}
        for task in tasks:
            identity = (str(task["team"]), str(task["task_id"]))
            identity_paths.setdefault(identity, []).append(
                Path(str(task["manifest_path"])).expanduser().resolve()
            )
        canonical_paths = {
            path for matched_paths in identity_paths.values() for path in matched_paths
        }

        valid_catalog_paths: set[Path] = set()
        invalid_catalog_paths: set[Path] = set()
        for key, entry in catalog["entries"].items():
            associated_paths: set[Path] = set()
            key_candidate: Path | None = None
            mapping_candidate: Path | None = None
            identity_candidate: Path | None = None
            key_path = PurePosixPath(key)
            if (
                chr(92) not in key
                and not key_path.is_absolute()
                and len(key_path.parts) == 3
                and all(part not in {"", ".", ".."} for part in key_path.parts)
            ):
                try:
                    key_candidate = (self.root / Path(*key_path.parts)).resolve()
                    key_candidate.relative_to(self.root.resolve())
                except (OSError, RuntimeError, ValueError):
                    key_candidate = None
                else:
                    associated_paths.add(key_candidate)
            if isinstance(entry, Mapping):
                raw_path = entry.get("manifest_path")
                if isinstance(raw_path, str) and raw_path.strip():
                    try:
                        mapping_candidate = Path(raw_path).expanduser().resolve()
                        mapping_candidate.relative_to(self.root.resolve())
                    except (OSError, RuntimeError, ValueError):
                        mapping_candidate = None
                    else:
                        associated_paths.add(mapping_candidate)
                declared_team = entry.get("team")
                declared_task_id = entry.get("task_id")
                if isinstance(declared_team, str) and isinstance(declared_task_id, str):
                    matches = identity_paths.get((declared_team, declared_task_id), [])
                    if len(matches) == 1:
                        identity_candidate = matches[0]
                        associated_paths.add(identity_candidate)

            record_error = self._catalog_record_error(key, entry)
            if record_error is not None:
                invalid_catalog_paths.update(associated_paths)
                mapping_misidentifies = (
                    identity_candidate is not None
                    and mapping_candidate is not None
                    and (
                        not mapping_candidate.exists()
                        or (
                            mapping_candidate in canonical_paths
                            and mapping_candidate != identity_candidate
                        )
                    )
                )
                diagnostic_path = (
                    identity_candidate
                    if mapping_misidentifies
                    else mapping_candidate
                    or identity_candidate
                    or key_candidate
                    or self.catalog_path
                )
                manifest_path = str(diagnostic_path)
                catalog_error = (
                    f"InvalidManifestError: raw catalog entry {key!r}: "
                    f"catalog metadata {record_error}"
                )
                if manifest_path in diagnosed:
                    existing = next(
                        item for item in errors if item["manifest_path"] == manifest_path
                    )
                    existing["error"] = f'{existing["error"]}; {catalog_error}'
                else:
                    errors.append(
                        {
                            "manifest_path": manifest_path,
                            "error": catalog_error,
                        }
                    )
                continue

            assert isinstance(entry, Mapping)
            manifest_path = str(entry["manifest_path"])
            if manifest_path in diagnosed:
                continue
            candidate = Path(manifest_path).expanduser().resolve()
            if self._primary_manifest_state(
                candidate,
                catalog_key=str(key),
                catalog_entry=entry,
            ) is not None:
                valid_catalog_paths.add(candidate)
                continue
            if candidate.exists():
                invalid_catalog_paths.add(candidate)
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                else:
                    detail = self._manifest_value_error(
                        candidate,
                        value,
                        catalog_key=str(key),
                        catalog_entry=entry,
                    ) or "cataloged path is not a canonical primary CDPA manifest"
                error = f"InvalidManifestError: {detail}"
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
        rejected_paths = invalid_catalog_paths - valid_catalog_paths
        if rejected_paths:
            tasks = [
                task
                for task in tasks
                if Path(str(task["manifest_path"])).expanduser().resolve()
                not in rejected_paths
            ]
        return tasks, errors

    def _discover_with_catalog_unlocked(
        self,
        catalog: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        tasks, errors = self._discover_candidates(catalog)
        return self._filter_catalog_tasks(tasks, errors, catalog)

    def discover_with_errors(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        tasks, errors = self._discover_candidates()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with exclusive_file_lock(self.allocation_lock):
                catalog = self._load_catalog_unlocked(reconcile=False)
        except Exception as exc:
            errors.append(
                {
                    "manifest_path": str(self.catalog_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return tasks, errors
        return self._filter_catalog_tasks(tasks, errors, catalog)

    def _dependency_states(
        self,
        *,
        target: Path | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> list[tuple[Path, Mapping[str, Any]]]:
        records: list[tuple[Path, Mapping[str, Any]]] = []
        for candidate in self._filesystem_manifest_like_paths():
            if target is not None and candidate == target:
                continue
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, Mapping)
                and self._manifest_value_error(candidate, value) is None
            ):
                records.append((candidate, value))
        if target is not None and state is not None:
            records.append((target, state))
        return records

    @staticmethod
    def _duplicate_dependency_task_ids(
        records: Sequence[tuple[Path, Mapping[str, Any]]],
    ) -> set[str]:
        counts: dict[str, int] = {}
        for _path, item in records:
            task_id = str(item.get("task_id") or "")
            counts[task_id] = counts.get(task_id, 0) + 1
        return {task_id for task_id, count in counts.items() if task_id and count > 1}

    def duplicate_task_ids(self) -> set[str]:
        return self._duplicate_dependency_task_ids(self._dependency_states())

    def _dependency_graph_error(
        self,
        target: Path,
        state: Mapping[str, Any],
    ) -> str | None:
        records = self._dependency_states(target=target, state=state)
        duplicate_ids = self._duplicate_dependency_task_ids(records)
        task_id = str(state.get("task_id") or "")
        if task_id in duplicate_ids:
            return f"duplicate task ID {task_id!r}"
        parent_ids = normalize_dependency_ids(state.get("depends_on_task_ids"))
        ambiguous = [parent_id for parent_id in parent_ids if parent_id in duplicate_ids]
        if ambiguous:
            return f"ambiguous dependency task(s): {ambiguous!r}"
        tasks = [
            item
            for path, item in records
            if path != target and str(item.get("task_id") or "") not in duplicate_ids
        ]
        try:
            validate_new_dependencies(
                task_id,
                parent_ids,
                tasks,
                allow_missing=True,
            )
        except ValueError as exc:
            return str(exc)
        return None

    def load(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self._lock_path(target)):
            value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid CDPA task manifest in {target}: root must be an object")
        error = self._manifest_value_error(target, value)
        if error is None:
            error = self._dependency_graph_error(target, value)
        if error is not None:
            raise ValueError(f"invalid CDPA task manifest in {target}: {error}")
        return dict(value)

    def _save_unlocked(
        self,
        target: Path,
        state: Mapping[str, Any],
        *,
        maintenance_write: bool = False,
    ) -> dict[str, Any]:
        value = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        value["schema_version"] = SCHEMA_VERSION
        previous_updated_at = str(value.get("updated_at") or "")
        now = utc_now()
        value["updated_at"] = now
        maintenance = value.get("maintenance")
        if maintenance_write and isinstance(maintenance, dict):
            previous_worker_at = str(maintenance.get("worker_updated_at") or "")
            if previous_updated_at and previous_updated_at != previous_worker_at:
                maintenance["observed_task_updated_at"] = previous_updated_at
            maintenance["worker_updated_at"] = now
        error = self._manifest_value_error(target, value, require_file=False)
        if error is None:
            error = self._dependency_graph_error(target, value)
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

    def _load_current_manifest_unlocked(self, target: Path) -> dict[str, Any]:
        current = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(current, Mapping):
            raise ValueError(
                f"invalid CDPA task manifest in {target}: root must be an object"
            )
        error = self._manifest_value_error(target, current)
        if error is not None:
            raise ValueError(f"invalid CDPA task manifest in {target}: {error}")
        return dict(current)

    def _assert_manifest_mutable_unlocked(
        self,
        target: Path,
        current: Mapping[str, Any],
    ) -> None:
        task_id = str(current.get("task_id") or "")
        for candidate in self._filesystem_primary_paths():
            if candidate == target:
                continue
            replacement = self._primary_manifest_state(candidate)
            if replacement is not None and replacement.get("replaces_task_id") == task_id:
                raise ValueError(
                    f"task {task_id!r} is immutable history after replacement"
                )

    def _catalog_saved_manifest_unlocked(self, saved: Mapping[str, Any]) -> None:
        target = Path(str(saved["manifest_path"])).expanduser().resolve()
        catalog = self._load_catalog_unlocked(reconcile=False)
        key = self._catalog_key(target)
        entry = self._catalog_entry(saved)
        if catalog["entries"].get(key) != entry:
            catalog["entries"][key] = entry
            self._write_catalog_unlocked(catalog)

    def _mutate_manifest(
        self,
        target: Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        *,
        maintenance_write: bool = False,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            recovered = self._recover_phase4_replacement_unlocked()
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                if (
                    recovered is not None
                    and expected_updated_at is not None
                    and str(current.get("updated_at") or "") != expected_updated_at
                ):
                    raise ValueError(
                        "task changed during Phase-4 recovery; reload before saving"
                    )
                result = mutator(current)
                saved = self._save_unlocked(
                    target,
                    result if result is not None else current,
                    maintenance_write=maintenance_write,
                )
            self._catalog_saved_manifest_unlocked(saved)
            return saved

    def save(self, path: str | Path, state: Mapping[str, Any]) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        replacement = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))

        def replace_current(current: dict[str, Any]) -> dict[str, Any]:
            if normalize_dependency_ids(current.get("depends_on_task_ids")) != normalize_dependency_ids(
                replacement.get("depends_on_task_ids")
            ):
                raise ValueError(
                    "task dependencies changed; reload before saving"
                )
            return replacement

        return self._mutate_manifest(
            target,
            replace_current,
            expected_updated_at=str(replacement.get("updated_at") or ""),
        )

    def save_maintenance(
        self,
        path: str | Path,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist only maintenance metadata without hiding task-side changes."""
        target = Path(path).expanduser().resolve()
        maintenance = state.get("maintenance")
        if not isinstance(maintenance, Mapping):
            raise ValueError("maintenance must be an object")
        maintenance_value = json.loads(
            json.dumps(dict(maintenance), ensure_ascii=False, default=str)
        )

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            current["maintenance"] = maintenance_value
            return current

        return self._mutate_manifest(
            target,
            merge,
            maintenance_write=True,
        )

    def update(
        self,
        path: str | Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        return self._mutate_manifest(target, mutator)

    def begin_team_cleanup(
        self,
        path: str | Path,
        state: Mapping[str, Any],
        *,
        expected_state: Mapping[str, Any],
        control_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        target = Path(path).expanduser().resolve()
        replacement = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        expected = json.loads(
            json.dumps(dict(expected_state), ensure_ascii=False, default=str)
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_phase4_replacement_unlocked()
            catalog = self._load_catalog_unlocked(reconcile=False)
            tasks, _errors = self._discover_with_catalog_unlocked(catalog)
            canonical = next(
                (
                    item
                    for item in tasks
                    if Path(str(item.get("manifest_path") or "")).expanduser().resolve()
                    == target
                ),
                None,
            )
            if canonical is None:
                raise ValueError("cleanup target is not a canonical CDPA task")
            team = str(canonical.get("team") or "")
            task_id = str(canonical.get("task_id") or "")
            sibling_conflict = has_other_nonterminal_team_work(
                tasks,
                team,
                exclude_task_id=task_id,
            )
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                current_content = {
                    key: value for key, value in current.items() if key != "updated_at"
                }
                expected_content = {
                    key: value for key, value in expected.items() if key != "updated_at"
                }
                changed = (
                    str(current.get("updated_at") or "")
                    != str(expected.get("updated_at") or "")
                    and current_content != expected_content
                )
                if sibling_conflict or changed:
                    if control_id is None:
                        return current, False
                    control = next(
                        (
                            item
                            for item in current.get("controls") or []
                            if isinstance(item, dict)
                            and item.get("control_id") == control_id
                            and item.get("action") == "clear_team"
                        ),
                        None,
                    )
                    if control is None:
                        raise ValueError("Clear Team control changed while cleanup was being prepared")
                    reason = (
                        "TeamWorkExistsError: Clear Team is blocked while other "
                        "nonterminal exact-team work exists, including queued exact-team work"
                        if sibling_conflict
                        else "ValueError: task changed while cleanup was being prepared"
                    )
                    control["status"] = "rejected"
                    control["result"] = reason
                    control["applied_at"] = utc_now()
                    saved = self._save_unlocked(target, current)
                    catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
                    self._write_catalog_unlocked(catalog)
                    return saved, False
                replacement["updated_at"] = current.get("updated_at")
                if normalize_dependency_ids(current.get("depends_on_task_ids")) != normalize_dependency_ids(
                    replacement.get("depends_on_task_ids")
                ):
                    raise ValueError("task dependencies changed; reload before cleanup")
                saved = self._save_unlocked(target, replacement)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved, True

    def update_maintenance(
        self,
        path: str | Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        """Atomically mutate maintenance-owned task state under the manifest lock."""
        target = Path(path).expanduser().resolve()
        return self._mutate_manifest(
            target,
            mutator,
            maintenance_write=True,
        )

    def refresh_scheduling(
        self,
        path: str | Path,
        *,
        tasks: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        target = Path(path).expanduser().resolve()
        if tasks is None:
            self.recover_phase4_replacement()
            scheduling_tasks, _errors = self.discover_with_errors()
        else:
            scheduling_tasks = list(tasks)
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_phase4_replacement_unlocked()
            state = self.load(target)
            if str(state.get("status") or "").upper() in TERMINAL:
                return state, False
            dependencies = normalize_dependency_ids(state.get("depends_on_task_ids"))
            readiness = dependency_readiness(state, scheduling_tasks)
            queue = state.get("queue")
            queue_pending = (
                isinstance(queue, Mapping)
                and queue.get("reuse_team") is True
                and queue.get("released_at") is None
            )
            next_state = json.loads(json.dumps(state, ensure_ascii=False))
            changed = False
            status = str(state.get("status") or "").upper()
            team = str(state.get("team") or "")
            current_id = str(state.get("task_id") or "")
            blocked_by: str | None = None
            selected_id: str | None = None

            if status == "WAITING" and (queue_pending or dependencies):
                catalog = self._load_catalog_unlocked(reconcile=False)
                exact_tasks = [
                    item
                    for _path, item, _record in self._load_exact_team_states_unlocked(
                        team,
                        catalog,
                    )
                ]
                exact_task_ids = {
                    str(item.get("task_id") or "") for item in exact_tasks
                }
                recorded_blocker = (
                    str(queue.get("blocked_by_task_id") or "")
                    if isinstance(queue, Mapping)
                    else ""
                )
                if queue_pending and recorded_blocker and recorded_blocker not in exact_task_ids:
                    raise ValueError(
                        f"exact team {team!r} queue blocker is missing: "
                        f"{recorded_blocker}"
                    )
                barriers = [
                    item for item in exact_tasks if is_team_availability_barrier(item)
                ]
                if len(barriers) > 1:
                    raise ValueError(f"exact team {team!r} has multiple active owners")
                barrier_id = (
                    str(barriers[0].get("task_id") or "") if barriers else None
                )
                ready_waiters = exact_team_ready_waiters(
                    exact_tasks,
                    team,
                    dependency_tasks=scheduling_tasks,
                )
                selected_id = (
                    str(ready_waiters[0].get("task_id") or "")
                    if ready_waiters
                    else None
                )
                blocked_by = barrier_id or (
                    selected_id if selected_id != current_id else None
                )

            if queue_pending:
                if readiness.ready and blocked_by is None and selected_id == current_id:
                    if dependencies and (
                        state.get("waiting") or {}
                    ).get("waiting_on"):
                        next_state.setdefault("dependency_events", []).append(
                            {
                                "at": utc_now(),
                                "status": "RELEASED",
                                "message": "All dependencies are DONE",
                                "waiting_on": [],
                                "stopped": [],
                                "missing": [],
                            }
                        )
                    self._release_queued_state(
                        next_state,
                        reason="Exact-team queue released",
                    )
                    changed = True
                else:
                    dependency_waiting = not readiness.ready
                    team_waiting = blocked_by is not None
                    waiting_kind = (
                        "dependency_team_busy"
                        if dependency_waiting and team_waiting
                        else "dependency"
                        if dependency_waiting
                        else "team_busy"
                    )
                    blocked_ids = [*readiness.waiting_on, *readiness.missing]
                    reason = (
                        "Waiting for dependencies and exact-team ownership"
                        if dependency_waiting and team_waiting
                        else "Waiting for dependencies"
                        if dependency_waiting
                        else "Waiting for exact-team ownership"
                    )
                    details = [*blocked_ids, *([blocked_by] if blocked_by else [])]
                    if details:
                        reason += ": " + ", ".join(details)
                    code = (
                        "dependency_team_busy"
                        if dependency_waiting and team_waiting
                        else "dependency_missing"
                        if readiness.missing
                        else "dependency_stopped"
                        if readiness.stopped
                        else "dependency"
                        if dependency_waiting
                        else "team_busy"
                    )
                    waiting = (
                        state.get("waiting")
                        if isinstance(state.get("waiting"), Mapping)
                        else {}
                    )
                    desired = {
                        "reason": waiting_kind,
                        "waiting_on": list(readiness.waiting_on),
                        "stopped": list(readiness.stopped),
                        "missing": list(readiness.missing),
                        "blocked_by_task_id": blocked_by,
                        "since": waiting.get("since") or utc_now(),
                    }
                    unchanged = (
                        status == "WAITING"
                        and state.get("waiting_code") == code
                        and state.get("waiting_reason") == reason
                        and dict(waiting) == desired
                        and isinstance(queue, Mapping)
                        and queue.get("blocked_by_task_id") == blocked_by
                    )
                    if not unchanged:
                        next_state.update(
                            status="WAITING",
                            kanban_column="WAITING",
                            active_action=(
                                "waiting_dependency_team"
                                if dependency_waiting and team_waiting
                                else "waiting_dependency"
                                if dependency_waiting
                                else "waiting_team"
                            ),
                            waiting_reason=reason,
                            waiting_code=code,
                            waiting=desired,
                        )
                        next_state["queue"]["blocked_by_task_id"] = blocked_by
                        next_state.setdefault("queue_events", []).append(
                            {
                                "at": utc_now(),
                                "status": "WAITING",
                                "message": reason,
                                "blocked_by_task_id": blocked_by,
                            }
                        )
                        changed = True
            elif dependencies:
                waiting = (
                    state.get("waiting")
                    if isinstance(state.get("waiting"), Mapping)
                    else {}
                )
                dependency_waiting = not readiness.ready
                team_waiting = blocked_by is not None
                waiting_kind = (
                    "dependency_team_busy"
                    if dependency_waiting and team_waiting
                    else "dependency"
                    if dependency_waiting
                    else "team_busy"
                    if team_waiting
                    else None
                )
                desired = {
                    "reason": waiting_kind,
                    "waiting_on": list(readiness.waiting_on),
                    "stopped": list(readiness.stopped),
                    "missing": list(readiness.missing),
                    "since": (
                        None
                        if readiness.ready and not team_waiting
                        else waiting.get("since") or utc_now()
                    ),
                }
                if team_waiting:
                    desired["blocked_by_task_id"] = blocked_by
                if (
                    readiness.ready
                    and not team_waiting
                    and selected_id == current_id
                    and status == "WAITING"
                ):
                    self._release_dependency_state(
                        next_state,
                        reason="All dependencies are DONE; task released to PLAN",
                    )
                    changed = True
                elif status == "WAITING":
                    code = (
                        "dependency_team_busy"
                        if dependency_waiting and team_waiting
                        else "dependency_missing"
                        if readiness.missing
                        else "dependency_stopped"
                        if readiness.stopped
                        else "dependency"
                        if dependency_waiting
                        else "team_busy"
                    )
                    blocked_ids = [*readiness.waiting_on, *readiness.missing]
                    reason = (
                        "Waiting for dependencies and exact-team ownership"
                        if dependency_waiting and team_waiting
                        else "Waiting for dependencies"
                        if dependency_waiting
                        else "Waiting for exact-team ownership"
                    )
                    details = [*blocked_ids, *([blocked_by] if blocked_by else [])]
                    if details:
                        reason += ": " + ", ".join(details)
                    unchanged = (
                        state.get("waiting_code") == code
                        and state.get("waiting_reason") == reason
                        and dict(waiting) == desired
                    )
                    if not unchanged:
                        next_state.update(
                            status="WAITING",
                            kanban_column="WAITING",
                            active_action=(
                                "waiting_dependency_team"
                                if dependency_waiting and team_waiting
                                else "waiting_dependency"
                                if dependency_waiting
                                else "waiting_team"
                            ),
                            waiting_reason=reason,
                            waiting_code=code,
                            waiting=desired,
                        )
                        next_state.setdefault("dependency_events", []).append(
                            {
                                "at": utc_now(),
                                "status": "WAITING",
                                "message": reason,
                                "waiting_on": list(readiness.waiting_on),
                                "stopped": list(readiness.stopped),
                                "missing": list(readiness.missing),
                            }
                        )
                        changed = True

            if not changed:
                return state, False
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                if current.get("updated_at") != state.get("updated_at"):
                    raise ValueError("task changed while refreshing scheduling state")
                saved = self._save_unlocked(target, next_state)
            self._catalog_saved_manifest_unlocked(saved)
            return saved, True

    @contextmanager
    def task_run_lock(self, path: str | Path, *, blocking: bool = False) -> Iterator[None]:
        target = Path(path).expanduser().resolve()
        lock = target.with_suffix(target.suffix + ".run.lock")
        with exclusive_file_lock(lock, blocking=blocking):
            yield

    def _initial_task_state(
        self,
        *,
        text: str,
        requested_team: str | None,
        task_id: str,
        repository_path: Path,
        base: str,
        team: str,
        suffix: int,
        reusable_teams: list[str],
        target: Path,
        normalized_new: tuple[str, ...],
        new_all: bool,
        normalized_report_mode: str,
        normalized_dependencies: tuple[str, ...],
        normalized_replaces: str | None,
        normalized_incident: str | None,
        normalized_attachments: tuple[dict[str, Any], ...],
        readiness: Any,
        queue_reuse: bool,
        queue_blocked_by: str | None,
        now: str,
    ) -> dict[str, Any]:
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
                "attachments_uploaded_generation": None,
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
        dependency_waiting = not readiness.ready
        queue_waiting = bool(queue_reuse)
        waiting_ids = [*readiness.waiting_on, *readiness.missing]
        if dependency_waiting and queue_waiting:
            waiting_kind = "dependency_team_busy"
            waiting_reason = "Waiting for dependencies and exact-team ownership"
            if waiting_ids:
                waiting_reason += ": " + ", ".join(waiting_ids)
        elif dependency_waiting:
            waiting_kind = "dependency"
            waiting_reason = "Waiting for dependencies: " + ", ".join(waiting_ids)
        elif queue_waiting:
            waiting_kind = "team_busy"
            waiting_reason = "Waiting for exact-team ownership"
            if queue_blocked_by:
                waiting_reason += f": {queue_blocked_by}"
        else:
            waiting_kind = None
            waiting_reason = None
        waiting_status = dependency_waiting or queue_waiting
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_path": str(target),
            "task_id": task_id,
            "task_title": text.splitlines()[0],
            "task_text": text,
            "task_slug": slugify(text),
            "repository": str(repository_path),
            "requested_team": requested_team,
            "team_base": base,
            "team": team,
            "team_suffix": suffix,
            "reusable_teams": reusable_teams,
            "status": "WAITING" if waiting_status else "INBOX",
            "kanban_column": "WAITING" if waiting_status else "INBOX",
            "terminal_state": None,
            "active_role": "PLAN",
            "active_hop_id": 1,
            "active_action": (
                "waiting_dependency_team"
                if waiting_kind == "dependency_team_busy"
                else "waiting_dependency"
                if waiting_kind == "dependency"
                else "waiting_team"
                if waiting_kind == "team_busy"
                else "queued"
            ),
            "pause_reason": None,
            "waiting_reason": waiting_reason,
            "waiting_code": (
                "dependency_team_busy"
                if waiting_kind == "dependency_team_busy"
                else "dependency_missing"
                if readiness.missing
                else "dependency_stopped"
                if readiness.stopped
                else waiting_kind
            ),
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
            "options": {
                "new_roles": list(normalized_new),
                "new_all": bool(new_all),
                "report_mode": normalized_report_mode,
            },
            "depends_on_task_ids": list(normalized_dependencies),
            "replaces_task_id": normalized_replaces,
            "replacement_incident_id": normalized_incident,
            "attachments": [dict(item) for item in normalized_attachments],
            "dependency_events": (
                []
                if readiness.ready
                else [{
                    "at": now,
                    "status": "WAITING",
                    "message": "Waiting for dependencies: " + ", ".join(waiting_ids),
                    "waiting_on": list(readiness.waiting_on),
                    "stopped": list(readiness.stopped),
                    "missing": list(readiness.missing),
                }]
            ),
            "queue": (
                {
                    "reuse_team": True,
                    "blocked_by_task_id": queue_blocked_by,
                    "enqueued_at": now,
                    "released_at": None,
                }
                if queue_reuse
                else None
            ),
            "queue_events": (
                [{
                    "at": now,
                    "status": "WAITING",
                    "message": waiting_reason,
                    "blocked_by_task_id": queue_blocked_by,
                }]
                if queue_reuse
                else []
            ),
            "waiting": {
                "reason": waiting_kind,
                "waiting_on": list(readiness.waiting_on),
                "stopped": list(readiness.stopped),
                "missing": list(readiness.missing),
                **(
                    {"blocked_by_task_id": queue_blocked_by}
                    if queue_reuse
                    else {}
                ),
                "since": now if waiting_status else None,
            },
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

    def create_task(
        self,
        task: str,
        *,
        requested_team: str | None = None,
        reuse_team: str | None = None,
        new_roles: Sequence[str] = (),
        new_all: bool = False,
        repository: str | Path | None = None,
        task_id: str | None = None,
        reserved_team_suffixes: Sequence[int] = (),
        report_mode: str = "file",
        depends_on_task_ids: Sequence[str] = (),
        replaces_task_id: str | None = None,
        replacement_incident_id: str | None = None,
        upload_paths: Sequence[str | Path] = (),
    ) -> dict[str, Any]:
        text = str(task).strip()
        if not text:
            raise ValueError("task must not be empty")
        task_id = _validate_task_id(task_id or generate_task_id(text))
        normalized_new = tuple(dict.fromkeys(str(role).strip().upper() for role in new_roles))
        unknown = set(normalized_new) - set(self.config.roles)
        if unknown:
            raise ValueError(f"unknown --new roles: {sorted(unknown)!r}")
        normalized_report_mode = normalize_report_mode(report_mode)
        normalized_replaces = _optional_nonempty_string(replaces_task_id, "replaces_task_id")
        normalized_incident = _optional_nonempty_string(
            replacement_incident_id, "replacement_incident_id"
        )
        if requested_team is not None and reuse_team is not None:
            raise ValueError("requested_team and reuse_team are mutually exclusive")
        exact_reuse_team = validate_exact_team(reuse_team) if reuse_team is not None else None
        base = normalize_team_base(requested_team or task_id) if exact_reuse_team is None else ""
        repository_path = Path(repository or self.config.repository_root).expanduser().resolve()
        if isinstance(upload_paths, (str, bytes)):
            raise ValueError("upload_paths must be a sequence of file paths")
        try:
            normalized_attachments = tuple(
                identity.to_dict() for identity in collect_file_identities(upload_paths)
            ) if upload_paths else ()
        except FileNotFoundError as exc:
            failed = Path(str(exc.filename or (exc.args[0] if exc.args else "attachment"))).name
            raise ValueError(f"upload file is missing or not regular: {failed}") from exc
        except OSError as exc:
            failed = Path(str(exc.filename or (exc.args[0] if exc.args else "attachment"))).name
            raise ValueError(f"upload file cannot be read: {failed}") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_phase4_replacement_unlocked()
            catalog = self._load_catalog_unlocked(reconcile=False)
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

            exact_loaded = (
                self._load_exact_team_states_unlocked(exact_reuse_team, catalog)
                if exact_reuse_team is not None
                else []
            )
            manifests, _discovery_errors = self._discover_with_catalog_unlocked(catalog)
            if task_id in {
                str(item.get("task_id") or "") for item in manifests
            }:
                raise ValueError(f"task_id is already used by a valid manifest: {task_id}")
            requested_dependencies = normalize_dependency_ids(depends_on_task_ids)
            duplicate_task_ids = self.duplicate_task_ids()
            ambiguous_dependencies = [
                parent_id
                for parent_id in requested_dependencies
                if parent_id in duplicate_task_ids
            ]
            if ambiguous_dependencies:
                raise ValueError(
                    f"ambiguous dependency task(s): {ambiguous_dependencies!r}"
                )
            normalized_dependencies = validate_new_dependencies(
                task_id, requested_dependencies, manifests
            )
            readiness = dependency_readiness(
                {"task_id": task_id, "depends_on_task_ids": normalized_dependencies},
                manifests,
            )
            queue_blocked_by: str | None = None
            if exact_reuse_team is not None:
                exact_states = [state for _path, state, _record in exact_loaded]
                identities = {
                    (
                        str(item.get("team_base") or ""),
                        int(item.get("team_suffix") or 1),
                    )
                    for item in exact_states
                }
                if len(identities) != 1:
                    raise ValueError(
                        f"exact team {exact_reuse_team!r} has inconsistent identity"
                    )
                base, suffix = identities.pop()
                team = exact_reuse_team
                availability_barriers = [
                    item for item in exact_states if is_team_availability_barrier(item)
                ]
                if len(availability_barriers) > 1:
                    raise ValueError(
                        f"exact team {team!r} has multiple active owners"
                    )
                queue_blocked_by = (
                    str(availability_barriers[0].get("task_id") or "") or None
                    if availability_barriers
                    else None
                )
                if queue_blocked_by is None:
                    pending_queue = queued_team_tasks(exact_states, team)
                    if pending_queue:
                        queue_blocked_by = (
                            str(pending_queue[0].get("task_id") or "") or None
                        )
                terminal_candidates = sorted(
                    (
                        item
                        for item in exact_states
                        if str(item.get("status") or "").upper() in TERMINAL
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
                reusable_teams = [team] if terminal_candidates else []
            else:
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
                    if (
                        suffix := self._team_suffix_reservation(
                            base, str(item.get("team") or "")
                        )
                    )
                    is not None
                }
                team, suffix = allocate_team(
                    base,
                    [*manifests, *catalog_reservations, *filesystem_reservations],
                    reserved_suffixes=(
                        *reserved_team_suffixes,
                        *sorted(inferred_suffixes),
                    ),
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
            state = self._initial_task_state(
                text=text,
                requested_team=requested_team,
                task_id=task_id,
                repository_path=repository_path,
                base=base,
                team=team,
                suffix=suffix,
                reusable_teams=reusable_teams,
                target=target,
                normalized_new=normalized_new,
                new_all=new_all,
                normalized_report_mode=normalized_report_mode,
                normalized_dependencies=normalized_dependencies,
                normalized_replaces=normalized_replaces,
                normalized_incident=normalized_incident,
                normalized_attachments=normalized_attachments,
                readiness=readiness,
                queue_reuse=exact_reuse_team is not None,
                queue_blocked_by=queue_blocked_by,
                now=now,
            )
            saved = self._save_unlocked(target, state)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    @staticmethod
    def _phase4_bytes(state: Mapping[str, Any]) -> bytes:
        return json.dumps(
            dict(state), ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")

    @staticmethod
    def _restore_bytes(path: Path, data: bytes | None) -> None:
        temporary = path.with_suffix(path.suffix + ".phase4.rollback.tmp")
        try:
            if data is None:
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    fsync_parent_directory(path)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            fsync_parent_directory(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _phase4_encode_bytes(data: bytes | None) -> str | None:
        if data is None:
            return None
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _phase4_decode_bytes(value: Any, field: str) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"Phase-4 journal {field} must be base64 or null")
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError(f"Phase-4 journal {field} is invalid base64") from exc

    def _phase4_relative_path(self, path: str | Path) -> str:
        target = Path(path).expanduser().resolve()
        try:
            return target.relative_to(self.root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"Phase-4 journal path escapes plans root: {target}") from exc

    def _phase4_absolute_path(self, value: Any, field: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Phase-4 journal {field} must be a non-empty path")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"Phase-4 journal {field} must be a safe relative path")
        target = (self.root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"Phase-4 journal {field} escapes plans root") from exc
        return target

    def _write_phase4_journal_unlocked(self, value: Mapping[str, Any]) -> None:
        body = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
        body["version"] = 1
        body["operation"] = "replace_task_and_rewire"
        temporary = self.phase4_journal_path.with_suffix(".journal.tmp")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.phase4_journal_path)
            fsync_parent_directory(self.phase4_journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_phase4_journal_unlocked(self) -> dict[str, Any] | None:
        if not self.phase4_journal_path.exists():
            return None
        value = json.loads(self.phase4_journal_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("Phase-4 journal root must be an object")
        if value.get("version") != 1 or value.get("operation") != "replace_task_and_rewire":
            raise ValueError("unsupported Phase-4 replacement journal")
        for field in (
            "incident_id",
            "target_task_id",
            "replacement_task_id",
            "target_manifest_path",
            "replacement_manifest_path",
            "target_sha256",
            "created_at",
        ):
            if not isinstance(value.get(field), str) or not str(value.get(field)).strip():
                raise ValueError(f"Phase-4 journal {field} must be a non-empty string")
        if not isinstance(value.get("writes"), list) or not value["writes"]:
            raise ValueError("Phase-4 journal writes must be a non-empty list")
        if not isinstance(value.get("catalog"), Mapping):
            raise ValueError("Phase-4 journal catalog must be an object")
        seen_paths: set[Path] = set()
        seen_tasks: set[str] = set()
        replacement_entries = 0
        for index, item in enumerate(value["writes"]):
            if not isinstance(item, Mapping):
                raise ValueError("Phase-4 journal write must be an object")
            kind = item.get("kind")
            if kind not in {"replacement", "child"}:
                raise ValueError("Phase-4 journal write kind is invalid")
            path = self._phase4_absolute_path(item.get("path"), f"writes[{index}].path")
            if path in seen_paths:
                raise ValueError("Phase-4 journal contains duplicate write paths")
            seen_paths.add(path)
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("Phase-4 journal write task_id must be non-empty")
            if task_id in seen_tasks:
                raise ValueError("Phase-4 journal contains duplicate task IDs")
            seen_tasks.add(task_id)
            self._phase4_decode_bytes(item.get("before"), f"writes[{index}].before")
            after = self._phase4_decode_bytes(item.get("after"), f"writes[{index}].after")
            if after is None:
                raise ValueError("Phase-4 journal write after bytes are required")
            if hashlib.sha256(after).hexdigest() != item.get("after_sha256"):
                raise ValueError("Phase-4 journal write after hash is invalid")
            if kind == "replacement":
                replacement_entries += 1
        if replacement_entries != 1:
            raise ValueError("Phase-4 journal must contain one replacement write")
        catalog = value["catalog"]
        self._phase4_decode_bytes(catalog.get("before"), "catalog.before")
        after_catalog = self._phase4_decode_bytes(catalog.get("after"), "catalog.after")
        if after_catalog is None:
            raise ValueError("Phase-4 journal catalog after bytes are required")
        if hashlib.sha256(after_catalog).hexdigest() != catalog.get("after_sha256"):
            raise ValueError("Phase-4 journal catalog after hash is invalid")
        return dict(value)

    def _clear_phase4_journal_unlocked(self) -> None:
        self.phase4_journal_path.unlink(missing_ok=True)
        fsync_parent_directory(self.phase4_journal_path)

    def _phase4_parse_manifest_bytes(self, path: Path, data: bytes) -> dict[str, Any]:
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Phase-4 manifest bytes for {path}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid Phase-4 manifest root for {path}")
        error = self._manifest_value_error(path, value, require_file=path.exists())
        if error is not None:
            raise ValueError(f"invalid Phase-4 manifest {path}: {error}")
        return dict(value)

    def _phase4_write_bytes_unlocked(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".phase4.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            fsync_parent_directory(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _phase4_catalog_for_states(
        self,
        states: Sequence[Mapping[str, Any]],
        fallback_bytes: bytes,
        write_paths: set[Path],
    ) -> dict[str, Any]:
        try:
            current = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            current = json.loads(fallback_bytes.decode("utf-8"))
        if not isinstance(current, dict) or current.get("version") != CATALOG_VERSION:
            raise ValueError("Phase-4 recovery catalog is invalid")
        if not isinstance(current.get("entries"), dict):
            raise ValueError("Phase-4 recovery catalog entries are invalid")
        for state in states:
            manifest_path = Path(str(state["manifest_path"])).expanduser().resolve()
            if manifest_path in write_paths:
                current["entries"][self._catalog_key(manifest_path)] = self._catalog_entry(state)
        return current

    def _verify_phase4_replacement_unlocked(
        self,
        journal: Mapping[str, Any],
        states: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        target_id = str(journal["target_task_id"])
        replacement_id = str(journal["replacement_task_id"])
        incident = str(journal["incident_id"])
        index = {str(item["task_id"]): item for item in states}
        replacement = index.get(replacement_id)
        if replacement is None:
            raise RuntimeError("Phase-4 recovery replacement manifest is missing")
        if (
            replacement.get("replaces_task_id") != target_id
            or replacement.get("replacement_incident_id") != incident
        ):
            raise RuntimeError("Phase-4 recovery replacement provenance is invalid")
        target_path = self._phase4_absolute_path(
            journal["target_manifest_path"], "target_manifest_path"
        )
        if hashlib.sha256(target_path.read_bytes()).hexdigest() != journal["target_sha256"]:
            raise RuntimeError("Phase-4 recovery mutated immutable parent history")
        affected_ids = {
            str(item["task_id"])
            for item in journal["writes"]
            if item.get("kind") == "child"
        }
        rewired: list[dict[str, Any]] = []
        for child_id in sorted(affected_ids):
            child = index.get(child_id)
            if child is None:
                raise RuntimeError(f"Phase-4 recovery child is missing: {child_id}")
            parents = list(normalize_dependency_ids(child.get("depends_on_task_ids")))
            if target_id in parents or parents.count(replacement_id) != 1:
                raise RuntimeError(f"Phase-4 recovery child is not fully rewired: {child_id}")
            rewired.append(dict(child))
        for state in states:
            parents = list(normalize_dependency_ids(state.get("depends_on_task_ids")))
            if target_id in parents:
                raise RuntimeError(
                    f"Phase-4 recovery left dependency on old task: {state['task_id']}"
                )
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        if not isinstance(catalog, Mapping) or not isinstance(catalog.get("entries"), Mapping):
            raise RuntimeError("Phase-4 recovery catalog is invalid")
        write_paths = {
            self._phase4_absolute_path(item["path"], "write.path")
            for item in journal["writes"]
        }
        for state in states:
            manifest_path = Path(str(state["manifest_path"])).expanduser().resolve()
            if manifest_path not in write_paths:
                continue
            key = self._catalog_key(manifest_path)
            if catalog["entries"].get(key) != self._catalog_entry(state):
                raise RuntimeError(f"Phase-4 recovery catalog mismatch for {state['task_id']}")
        return {"replacement": dict(replacement), "rewired_children": rewired}

    def _recover_phase4_replacement_unlocked(self) -> dict[str, Any] | None:
        journal = self._load_phase4_journal_unlocked()
        if journal is None:
            return None
        target_id = str(journal["target_task_id"])
        replacement_id = str(journal["replacement_task_id"])
        incident = str(journal["incident_id"])
        write_entries = [dict(item) for item in journal["writes"]]
        journal_paths = {
            self._phase4_absolute_path(item["path"], "write.path")
            for item in write_entries
        }
        catalog_after = self._phase4_decode_bytes(
            journal["catalog"]["after"], "catalog.after"
        )
        assert catalog_after is not None
        try:
            catalog_snapshot = json.loads(catalog_after.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Phase-4 recovery catalog after bytes are invalid") from exc
        if (
            not isinstance(catalog_snapshot, Mapping)
            or catalog_snapshot.get("version") != CATALOG_VERSION
            or not isinstance(catalog_snapshot.get("entries"), Mapping)
        ):
            raise ValueError("Phase-4 recovery catalog after snapshot is invalid")
        planning, _discovery_errors = self._discover_with_catalog_unlocked(
            catalog_snapshot
        )
        canonical_paths = sorted(
            {
                Path(item["manifest_path"]).expanduser().resolve()
                for item in planning
            }
            | journal_paths,
            key=str,
        )
        with ExitStack() as stack:
            for manifest_path in canonical_paths:
                stack.enter_context(exclusive_file_lock(self._lock_path(manifest_path)))
            states_by_path: dict[Path, dict[str, Any]] = {}
            for manifest_path in canonical_paths:
                if not manifest_path.exists():
                    continue
                data = manifest_path.read_bytes()
                states_by_path[manifest_path] = self._phase4_parse_manifest_bytes(
                    manifest_path, data
                )

            replacement_entry = next(
                item for item in write_entries if item["kind"] == "replacement"
            )
            replacement_path = self._phase4_absolute_path(
                replacement_entry["path"], "replacement.path"
            )
            replacement = states_by_path.get(replacement_path)
            if replacement is None:
                after = self._phase4_decode_bytes(
                    replacement_entry["after"], "replacement.after"
                )
                assert after is not None
                self._phase4_write_bytes_unlocked(replacement_path, after)
                replacement = self._phase4_parse_manifest_bytes(replacement_path, after)
                states_by_path[replacement_path] = replacement
            elif (
                replacement.get("task_id") != replacement_id
                or replacement.get("replaces_task_id") != target_id
                or replacement.get("replacement_incident_id") != incident
            ):
                raise RuntimeError("Phase-4 recovery found conflicting replacement state")

            affected_paths: set[Path] = set()
            for item in write_entries:
                if item["kind"] == "child":
                    affected_paths.add(
                        self._phase4_absolute_path(item["path"], "child.path")
                    )
            for manifest_path, state in list(states_by_path.items()):
                if str(state.get("task_id")) in {target_id, replacement_id}:
                    continue
                parents = list(normalize_dependency_ids(state.get("depends_on_task_ids")))
                if target_id not in parents and manifest_path not in affected_paths:
                    continue
                if target_id in parents:
                    if replacement_id in parents:
                        raise RuntimeError(
                            f"Phase-4 recovery child contains old and new dependency: {state['task_id']}"
                        )
                    updated = json.loads(json.dumps(state, ensure_ascii=False))
                    updated["depends_on_task_ids"] = [
                        replacement_id if parent == target_id else parent
                        for parent in parents
                    ]
                    duplicate_event = any(
                        isinstance(event, Mapping)
                        and event.get("status") == "REWIRED"
                        and event.get("incident_id") == incident
                        and event.get("old_task_id") == target_id
                        and event.get("new_task_id") == replacement_id
                        for event in updated.get("dependency_events") or []
                    )
                    if not duplicate_event:
                        updated.setdefault("dependency_events", []).append(
                            {
                                "at": utc_now(),
                                "status": "REWIRED",
                                "message": (
                                    f"Dependency rewired from {target_id} to {replacement_id}"
                                ),
                                "incident_id": incident,
                                "old_task_id": target_id,
                                "new_task_id": replacement_id,
                            }
                        )
                    updated["updated_at"] = utc_now()
                    data = self._phase4_bytes(updated)
                    error = self._manifest_value_error(
                        manifest_path, updated, require_file=True
                    )
                    if error is not None:
                        raise ValueError(
                            f"invalid Phase-4 recovered child {manifest_path}: {error}"
                        )
                    self._phase4_write_bytes_unlocked(manifest_path, data)
                    states_by_path[manifest_path] = updated
                else:
                    if parents.count(replacement_id) != 1:
                        raise RuntimeError(
                            f"Phase-4 recovery child lost replacement dependency: {state['task_id']}"
                        )

            states = list(states_by_path.values())
            for state in states:
                validate_new_dependencies(
                    str(state["task_id"]),
                    normalize_dependency_ids(state.get("depends_on_task_ids")),
                    [other for other in states if other is not state],
                    allow_missing=True,
                )
            catalog = self._phase4_catalog_for_states(
                states,
                catalog_after,
                journal_paths,
            )
            self._write_catalog_unlocked(catalog)
            result = self._verify_phase4_replacement_unlocked(journal, states)
            self._clear_phase4_journal_unlocked()
            return result

    def recover_phase4_replacement(self) -> dict[str, Any] | None:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            return self._recover_phase4_replacement_unlocked()

    def replace_task_and_rewire(
        self,
        target_task_id: str,
        replacement_task: str,
        *,
        reuse_team: bool,
        rewire_children: bool,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        target_id = _validate_task_id(target_task_id)
        recovery_instruction = str(replacement_task).strip()
        if not recovery_instruction:
            raise ValueError("replacement task must not be empty")
        incident = _optional_nonempty_string(incident_id, "incident_id")
        if incident is None:
            raise ValueError("replacement incident_id is required")
        if not isinstance(reuse_team, bool) or not isinstance(rewire_children, bool):
            raise ValueError("replacement flags must be booleans")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            recovered = self._recover_phase4_replacement_unlocked()
            if recovered is not None and (
                recovered["replacement"].get("replaces_task_id") == target_id
                and recovered["replacement"].get("replacement_incident_id") == incident
            ):
                return recovered
            catalog = self._load_catalog_unlocked(reconcile=False)
            catalog_before = (
                self.catalog_path.read_bytes() if self.catalog_path.exists() else None
            )
            planning, _discovery_errors = self._discover_with_catalog_unlocked(catalog)
            canonical_paths = sorted(
                {
                    Path(item["manifest_path"]).expanduser().resolve()
                    for item in planning
                },
                key=str,
            )
            with ExitStack() as stack:
                for manifest_path in canonical_paths:
                    stack.enter_context(
                        exclusive_file_lock(self._lock_path(manifest_path))
                    )

                manifests: list[dict[str, Any]] = []
                for manifest_path in canonical_paths:
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if not isinstance(value, Mapping):
                        raise ValueError(
                            f"invalid CDPA task manifest in {manifest_path}: "
                            "root must be an object"
                        )
                    error = self._manifest_value_error(manifest_path, value)
                    if error is not None:
                        raise ValueError(
                            f"invalid CDPA task manifest in {manifest_path}: {error}"
                        )
                    manifests.append(dict(value))

                existing = next(
                    (
                        item
                        for item in manifests
                        if item.get("replaces_task_id") == target_id
                        and item.get("replacement_incident_id") == incident
                    ),
                    None,
                )
                if existing is not None:
                    return {
                        "replacement": existing,
                        "rewired_children": [
                            item
                            for item in manifests
                            if existing["task_id"]
                            in normalize_dependency_ids(
                                item.get("depends_on_task_ids")
                            )
                        ],
                    }

                target_state = next(
                    (
                        item
                        for item in manifests
                        if item.get("task_id") == target_id
                    ),
                    None,
                )
                if target_state is None:
                    raise ValueError(
                        f"replacement target does not exist: {target_id}"
                    )
                target_status = str(target_state.get("status") or "").upper()
                if target_status not in {"STOPPED", "BLOCKED"}:
                    raise ValueError(
                        "replacement target must be STOPPED or BLOCKED"
                    )
                cleanup = target_state.get("cleanup")
                if isinstance(cleanup, Mapping) and str(
                    cleanup.get("state") or ""
                ).upper() in {"CLEARING", "CLEARED"}:
                    raise ValueError(
                        "replacement target cleanup is already in progress"
                    )
                if reuse_team and target_status != "STOPPED":
                    raise ValueError(
                        "reuse_team requires a STOPPED replacement target"
                    )

                task_text = replacement_continuation_text(
                    target_state, recovery_instruction
                )
                known_ids = {str(item["task_id"]) for item in manifests}
                replacement_id = generate_task_id(task_text)
                while replacement_id in known_ids:
                    replacement_id = generate_task_id(task_text)
                base = str(
                    target_state.get("team_base")
                    or target_state.get("team")
                    or target_id
                )
                if reuse_team:
                    team = str(target_state["team"])
                    suffix = int(target_state.get("team_suffix") or 1)
                    reusable_teams = [team]
                else:
                    team, suffix = allocate_team(
                        base,
                        manifests,
                        reserved_suffixes=(
                            int(target_state.get("team_suffix") or 1),
                        ),
                    )
                    reusable_teams = []

                dependencies = validate_new_dependencies(
                    replacement_id,
                    normalize_dependency_ids(
                        target_state.get("depends_on_task_ids")
                    ),
                    manifests,
                )
                readiness = dependency_readiness(
                    {
                        "task_id": replacement_id,
                        "depends_on_task_ids": dependencies,
                    },
                    manifests,
                )
                title_slug = slugify(task_text)
                replacement_path = (
                    self.root
                    / team
                    / replacement_id
                    / f"{title_slug}.json"
                ).resolve()
                replacement_lock_path = self._lock_path(replacement_path)
                replacement_directory_existed = replacement_path.parent.exists()
                success = False

                def cleanup_failed_replacement_directory() -> None:
                    if success or replacement_directory_existed:
                        return
                    replacement_lock_path.unlink(missing_ok=True)
                    if replacement_path.parent.exists():
                        try:
                            replacement_path.parent.rmdir()
                        except OSError:
                            pass

                stack.callback(cleanup_failed_replacement_directory)
                stack.enter_context(exclusive_file_lock(replacement_lock_path))
                if replacement_path.exists():
                    raise FileExistsError(replacement_path)

                now = utc_now()
                report_mode = report_mode_from_options(
                    target_state.get("options")
                    if isinstance(target_state.get("options"), Mapping)
                    else {}
                )
                replacement_state = self._initial_task_state(
                    text=task_text,
                    requested_team=target_state.get("requested_team"),
                    task_id=replacement_id,
                    repository_path=Path(target_state["repository"])
                    .expanduser()
                    .resolve(),
                    base=base,
                    team=team,
                    suffix=suffix,
                    reusable_teams=reusable_teams,
                    target=replacement_path,
                    normalized_new=(),
                    new_all=False,
                    normalized_report_mode=report_mode,
                    normalized_dependencies=dependencies,
                    normalized_replaces=target_id,
                    normalized_incident=incident,
                    normalized_attachments=(),
                    readiness=readiness,
                    queue_reuse=False,
                    queue_blocked_by=None,
                    now=now,
                )
                replacement_state["dependency_events"].append(
                    {
                        "at": now,
                        "status": "REPLACEMENT_CREATED",
                        "message": f"Replacement created for {target_id}",
                        "incident_id": incident,
                        "old_task_id": target_id,
                        "new_task_id": replacement_id,
                    }
                )

                updated_by_id: dict[str, dict[str, Any]] = {}
                if rewire_children:
                    for item in manifests:
                        parents = list(
                            normalize_dependency_ids(
                                item.get("depends_on_task_ids")
                            )
                        )
                        if target_id not in parents:
                            continue
                        updated = json.loads(
                            json.dumps(item, ensure_ascii=False)
                        )
                        updated["depends_on_task_ids"] = [
                            replacement_id if parent == target_id else parent
                            for parent in parents
                        ]
                        updated.setdefault("dependency_events", []).append(
                            {
                                "at": now,
                                "status": "REWIRED",
                                "message": (
                                    f"Dependency rewired from {target_id} "
                                    f"to {replacement_id}"
                                ),
                                "incident_id": incident,
                                "old_task_id": target_id,
                                "new_task_id": replacement_id,
                            }
                        )
                        updated["updated_at"] = now
                        updated_by_id[str(updated["task_id"])] = updated

                writes: dict[Path, dict[str, Any]] = {
                    replacement_path: replacement_state
                }
                for updated in updated_by_id.values():
                    writes[
                        Path(updated["manifest_path"])
                        .expanduser()
                        .resolve()
                    ] = updated
                for manifest_path, value in writes.items():
                    value["schema_version"] = SCHEMA_VERSION
                    value["updated_at"] = now
                    error = self._manifest_value_error(
                        manifest_path,
                        value,
                        require_file=manifest_path.exists(),
                    )
                    if error is not None:
                        raise ValueError(
                            "refusing invalid replacement graph manifest "
                            f"{manifest_path}: {error}"
                        )

                proposed: list[Mapping[str, Any]] = [
                    updated_by_id.get(str(item["task_id"]), item)
                    for item in manifests
                ]
                proposed.append(replacement_state)
                for item in proposed:
                    validate_new_dependencies(
                        str(item["task_id"]),
                        normalize_dependency_ids(
                            item.get("depends_on_task_ids")
                        ),
                        [other for other in proposed if other is not item],
                        allow_missing=True,
                    )

                next_catalog = json.loads(
                    json.dumps(catalog, ensure_ascii=False, default=str)
                )
                next_catalog["version"] = CATALOG_VERSION
                next_catalog.setdefault("entries", {})
                for manifest_path, value in writes.items():
                    next_catalog["entries"][
                        self._catalog_key(manifest_path)
                    ] = self._catalog_entry(value)
                next_catalog["updated_at"] = utc_now()

                originals: dict[Path, bytes | None] = {
                    manifest_path: (
                        manifest_path.read_bytes()
                        if manifest_path.exists()
                        else None
                    )
                    for manifest_path in writes
                }
                originals[self.catalog_path] = catalog_before
                catalog_after = json.dumps(
                    next_catalog,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                journal_writes = []
                for manifest_path in sorted(writes, key=str):
                    after = self._phase4_bytes(writes[manifest_path])
                    journal_writes.append(
                        {
                            "kind": (
                                "replacement"
                                if manifest_path == replacement_path
                                else "child"
                            ),
                            "task_id": str(writes[manifest_path]["task_id"]),
                            "path": self._phase4_relative_path(manifest_path),
                            "before": self._phase4_encode_bytes(
                                originals[manifest_path]
                            ),
                            "after": self._phase4_encode_bytes(after),
                            "after_sha256": hashlib.sha256(after).hexdigest(),
                        }
                    )
                journal = {
                    "version": 1,
                    "operation": "replace_task_and_rewire",
                    "incident_id": incident,
                    "target_task_id": target_id,
                    "replacement_task_id": replacement_id,
                    "target_manifest_path": self._phase4_relative_path(
                        target_state["manifest_path"]
                    ),
                    "replacement_manifest_path": self._phase4_relative_path(
                        replacement_path
                    ),
                    "target_sha256": hashlib.sha256(
                        Path(target_state["manifest_path"]).read_bytes()
                    ).hexdigest(),
                    "created_at": now,
                    "writes": journal_writes,
                    "catalog": {
                        "before": self._phase4_encode_bytes(catalog_before),
                        "after": self._phase4_encode_bytes(catalog_after),
                        "after_sha256": hashlib.sha256(catalog_after).hexdigest(),
                    },
                }
                staged: dict[Path, Path] = {}
                installed: list[Path] = []
                try:
                    for manifest_path in sorted(writes, key=str):
                        manifest_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = manifest_path.with_suffix(
                            manifest_path.suffix + ".phase4.tmp"
                        )
                        staged[manifest_path] = temporary
                        with temporary.open("wb") as handle:
                            handle.write(
                                self._phase4_bytes(writes[manifest_path])
                            )
                            handle.flush()
                            os.fsync(handle.fileno())

                    catalog_temporary = self.catalog_path.with_suffix(
                        self.catalog_path.suffix + ".phase4.tmp"
                    )
                    staged[self.catalog_path] = catalog_temporary
                    with catalog_temporary.open("wb") as handle:
                        handle.write(catalog_after)
                        handle.flush()
                        os.fsync(handle.fileno())

                    self._write_phase4_journal_unlocked(journal)
                    durable_journal = self._load_phase4_journal_unlocked()
                    if durable_journal is None:
                        raise RuntimeError("Phase-4 journal disappeared before install")
                    journal = durable_journal
                    for manifest_path in sorted(writes, key=str):
                        os.replace(staged[manifest_path], manifest_path)
                        installed.append(manifest_path)
                        fsync_parent_directory(manifest_path)
                    os.replace(
                        staged[self.catalog_path], self.catalog_path
                    )
                    installed.append(self.catalog_path)
                    fsync_parent_directory(self.catalog_path)
                    result = self._verify_phase4_replacement_unlocked(
                        journal, proposed
                    )
                    self._clear_phase4_journal_unlocked()
                    success = True
                except Exception:
                    rollback_error: BaseException | None = None
                    try:
                        for installed_path in reversed(installed):
                            self._restore_bytes(
                                installed_path, originals[installed_path]
                            )
                    except BaseException as exc:
                        rollback_error = exc
                    if rollback_error is None:
                        self._clear_phase4_journal_unlocked()
                    else:
                        raise rollback_error
                    raise
                finally:
                    for temporary in staged.values():
                        temporary.unlink(missing_ok=True)

            return result

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

    @staticmethod
    def _release_dependency_state(
        state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        if not normalize_dependency_ids(state.get("depends_on_task_ids")):
            raise ValueError("task is not dependency-waiting")
        state["status"] = "INBOX"
        state["kanban_column"] = "INBOX"
        state["active_action"] = "queued"
        state["waiting_reason"] = None
        state["waiting_code"] = None
        state["waiting"] = {
            "reason": None,
            "waiting_on": [],
            "stopped": [],
            "missing": [],
            "since": None,
        }
        state.setdefault("dependency_events", []).append(
            {
                "at": utc_now(),
                "status": "RELEASED",
                "message": reason,
                "waiting_on": [],
                "stopped": [],
                "missing": [],
            }
        )
        return state

    @staticmethod
    def _release_queued_state(
        state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        queue = state.get("queue")
        if not isinstance(queue, dict) or queue.get("reuse_team") is not True:
            raise ValueError("task is not a same-team queue entry")
        if queue.get("released_at"):
            return state
        now = utc_now()
        queue["released_at"] = now
        queue["blocked_by_task_id"] = None
        state["status"] = "INBOX"
        state["kanban_column"] = "INBOX"
        state["active_action"] = "queued"
        state["waiting_reason"] = None
        state["waiting_code"] = None
        state["waiting"] = {
            "reason": None,
            "waiting_on": [],
            "stopped": [],
            "missing": [],
            "blocked_by_task_id": None,
            "since": None,
        }
        state["reusable_teams"] = list(
            dict.fromkeys([*state.get("reusable_teams", []), str(state["team"])])
        )
        state.setdefault("queue_events", []).append(
            {
                "at": now,
                "status": "RELEASED",
                "message": reason,
                "blocked_by_task_id": None,
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
            self._recover_phase4_replacement_unlocked()
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

            all_tasks, _discovery_errors = self._discover_with_catalog_unlocked(catalog)
            barriers = [
                item for item in loaded if is_team_availability_barrier(item[1])
            ]
            if len(barriers) > 1:
                paths = ", ".join(str(path) for path, _state, _record in barriers)
                raise ValueError(
                    "duplicate nonterminal manifests; "
                    f"exact team {team!r} has multiple active owners: {paths}"
                )
            releasing_queue = False
            releasing_dependency = False
            if barriers:
                target, snapshot, record = barriers[0]
                if not is_active_team_owner(snapshot):
                    raise ValueError(
                        f"exact team {team!r} cleanup is still clearing"
                    )
            else:
                exact_states = [state for _path, state, _record in loaded]
                ready_waiters = exact_team_ready_waiters(
                    exact_states,
                    team,
                    dependency_tasks=all_tasks,
                )
                selected_id = (
                    str(ready_waiters[0].get("task_id") or "")
                    if ready_waiters
                    else None
                )
                selected = next(
                    (
                        item
                        for item in loaded
                        if str(item[1].get("task_id") or "") == selected_id
                    ),
                    None,
                )
                if selected is None:
                    waiting_candidates = [
                        state
                        for state in exact_states
                        if str(state.get("status") or "").upper() == "WAITING"
                        and (
                            bool(normalize_dependency_ids(state.get("depends_on_task_ids")))
                            or (
                                isinstance(state.get("queue"), Mapping)
                                and state["queue"].get("reuse_team") is True
                                and state["queue"].get("released_at") is None
                            )
                        )
                    ]
                    if waiting_candidates:
                        queue_only = all(
                            isinstance(state.get("queue"), Mapping)
                            and state["queue"].get("reuse_team") is True
                            and state["queue"].get("released_at") is None
                            for state in waiting_candidates
                        )
                        kind = "queued" if queue_only else "waiting"
                        raise ValueError(
                            f"exact team {team!r} {kind} tasks are not dependency-ready"
                        )
                    statuses = sorted(
                        {
                            str(state.get("status") or "unknown").upper()
                            for state in exact_states
                        }
                    )
                    raise ValueError(
                        f"exact team {team!r} has no resumable nonterminal task; "
                        f"statuses={statuses}"
                    )
                target, selected_snapshot, record = selected
                selected_queue = selected_snapshot.get("queue")
                releasing_queue = (
                    isinstance(selected_queue, Mapping)
                    and selected_queue.get("reuse_team") is True
                    and selected_queue.get("released_at") is None
                )
                releasing_dependency = not releasing_queue
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
                self._assert_manifest_mutable_unlocked(target, current)
                if str(current.get("status") or "").upper() in TERMINAL:
                    raise ValueError(f"exact team {team!r} became terminal while resuming")
                queued = dict(current)
                if releasing_queue or releasing_dependency:
                    if str(queued.get("status") or "").upper() != "WAITING":
                        raise ValueError(
                            f"exact team {team!r} selected task is no longer waiting"
                        )
                    if not dependency_readiness(queued, all_tasks).ready:
                        raise ValueError(
                            f"exact team {team!r} selected task is no longer dependency-ready"
                        )
                    if releasing_queue:
                        self._release_queued_state(
                            queued,
                            reason="Exact-team queue released by resume",
                        )
                    else:
                        self._release_dependency_state(
                            queued,
                            reason="All dependencies are DONE; task released by resume",
                        )
                self._queue_resume(queued, reason=reason or "exact-team resume")
                saved = self._save_unlocked(target, queued)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)
            return saved

    def _queue_control(
        self,
        state: dict[str, Any],
        action: str,
        *,
        role: str | None = None,
        reason: str | None = None,
        confirmed: bool = False,
        maintenance_incident_id: str | None = None,
        maintenance_request_id: str | None = None,
    ) -> Mapping[str, Any]:
        control_role = role
        if action == "resume" and control_role is None:
            control_role = str(state.get("active_role") or "PLAN").upper()
        incident_id = str(maintenance_incident_id or "").strip() or None
        request_id = str(maintenance_request_id or "").strip() or None
        if (incident_id is None) != (request_id is None):
            raise ValueError(
                "maintenance control provenance requires both incident and request IDs"
            )
        normalized_reason = str(reason or "").strip() or None
        if incident_id is not None:
            existing = next(
                (
                    item
                    for item in state.get("controls") or []
                    if isinstance(item, Mapping)
                    and item.get("maintenance_incident_id") == incident_id
                    and item.get("maintenance_request_id") == request_id
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.get("action") != action
                    or existing.get("role") != control_role
                    or existing.get("reason") != normalized_reason
                ):
                    raise ValueError(
                        "maintenance control provenance already belongs to another payload"
                    )
                return existing
        sequence = len(state.get("controls") or []) + 1
        control = {
            "control_id": sequence,
            "action": action,
            "role": control_role,
            "reason": normalized_reason,
            "confirmed": bool(confirmed),
            "status": "requested",
            "requested_at": utc_now(),
            "applied_at": None,
            "result": None,
        }
        if incident_id is not None:
            control["maintenance_incident_id"] = incident_id
            control["maintenance_request_id"] = request_id
        state.setdefault("controls", []).append(control)
        return control

    def request_control(
        self,
        path: str | Path,
        action: str,
        *,
        role: str | None = None,
        reason: str | None = None,
        confirmed: bool = False,
        maintenance_incident_id: str | None = None,
        maintenance_request_id: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"pause", "resume", "retry", "stop", "restart_role", "open_tab", "new_chat", "route_plan", "clear_team"}
        action = str(action).strip().lower()
        if action not in allowed:
            raise ValueError(f"unsupported control action {action!r}")
        if role is not None:
            role = str(role).strip().upper()
            if role not in self.config.roles:
                raise ValueError(f"unsupported control role {role!r}")
        incident_id = str(maintenance_incident_id or "").strip() or None
        request_id = str(maintenance_request_id or "").strip() or None
        if (incident_id is None) != (request_id is None):
            raise ValueError(
                "maintenance control provenance requires both incident and request IDs"
            )
        if action == "resume" and incident_id is None:
            return self.request_resume(path, reason=reason or "resume requested")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            self._queue_control(
                state,
                action,
                role=role,
                reason=reason,
                confirmed=confirmed,
                maintenance_incident_id=incident_id,
                maintenance_request_id=request_id,
            )
            return state

        return self.update(path, mutate)

    def reject_control(
        self,
        path: str | Path,
        control_id: int,
        reason: str,
        *,
        action: str | None = None,
    ) -> dict[str, Any]:
        control_id = int(control_id)
        reason = str(reason).strip()
        expected_action = str(action or "").strip().lower() or None
        if control_id < 1:
            raise ValueError("control_id must be positive")
        if not reason:
            raise ValueError("control rejection reason must not be empty")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            control = next(
                (
                    item
                    for item in state.get("controls") or []
                    if isinstance(item, dict)
                    and item.get("control_id") == control_id
                ),
                None,
            )
            if control is None:
                raise ValueError(f"control {control_id} no longer exists")
            if expected_action is not None and control.get("action") != expected_action:
                raise ValueError(
                    f"control {control_id} is not action {expected_action!r}"
                )
            if control.get("status") == "requested":
                control["status"] = "rejected"
                control["result"] = reason
                control["applied_at"] = utc_now()
            return state

        return self.update(path, mutate)
