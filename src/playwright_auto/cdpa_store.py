from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from contextlib import ExitStack, contextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Sequence

from .cdpa_bootstraps import (
    is_legacy_bootstrap_record,
    normalize_bootstrap_record,
    normalize_legacy_bootstrap_record,
)
from .cdpa_commands import (
    conversation_identity,
    RepairRequest,
    WorkerCommand,
    command_snapshot,
    workflow_control_eligibility,
    normalize_chatgpt_conversation_url,
    normalize_saved_chatgpt_conversation_url,
)
from .cdpa_config import CDPAConfig
from .cdpa_endpoints import BrowserEndpointRegistry
from .cdpa_dependencies import (
    DependencyReadiness,
    dependency_readiness,
    validate_new_dependencies,
)
from .cdpa_identity import generate_task_id
from .cdpa_independent import (
    INDEPENDENT_COLUMN,
    INDEPENDENT_ROLE,
    TASK_MODE_INDEPENDENT,
    TASK_MODE_WORKFLOW,
    canonical_independent_events,
    independent_cycle_limit_reached,
    is_independent_task,
    normalize_agent_name,
    normalize_completion_request,
    normalize_event,
    normalize_manual_instruction,
    normalize_max_cycles,
    normalize_system_prompt,
    record_consumed_event,
    record_recovery_release,
    refresh_recovery_warmup_on_enable,
    task_mode,
    validate_independent_object,
    validate_trigger_settings,
)
from .cdpa_routes import effective_report_mode, normalize_report_mode
from .cdpa_safety import sanitize_text
from .cdpa_store_common import TERMINAL, _validate_task_id, slugify, utc_now
from .cdpa_workflow_agents import (
    WorkflowAgentCatalog,
    normalize_workflow_display_name,
    ordered_system_routes,
    task_workflow_definitions,
    validate_workflow_route_key,
)
from .cdpa_team import (
    allocate_team,
    exact_team_ready_waiters,
    exact_team_reuse_eligible,
    has_other_nonterminal_team_work,
    is_active_team_owner,
    is_team_availability_barrier,
    normalize_team_base,
    physical_role,
    queued_team_tasks,
    validate_exact_team,
)
from .file_lock import (
    atomic_replace,
    exclusive_file_lock,
    fsync_parent_directory,
    read_shared_bytes,
    read_shared_text,
)
from .upload import collect_file_identities

SCHEMA_VERSION = 1
CATALOG_VERSION = 1
_TASK_STATUSES = frozenset({"INBOX", "WAITING", "RUNNING", "PAUSED", "BLOCKED", "DONE", "STOPPED"})
_HOP_STATES = frozenset({"waiting_trigger", "pre_send", "sending", "sent", "waiting", "responded", "routed", "abandoned"})
_CLEANUP_STATES = frozenset({"ACTIVE", "CLEARING", "CLEARED"})
_MAINTENANCE_STATES = frozenset({"OPEN", "RUNNING", "SUSPENDED", "RESOLVED", "ESCALATED"})
_BOOTSTRAP_CONTEXT_SOURCES = frozenset(
    {"bootstrap_donor", "bootstrap_native", "bootstrap_ui", "fresh_fallback"}
)


class TeamWorkExistsError(RuntimeError):
    pass


def report_mode_from_options(options: Mapping[str, Any]) -> str:
    if "report_mode" not in options:
        return "file"
    return normalize_report_mode(options["report_mode"])


def normalize_workflow_roles(
    value: Any,
    configured_roles: Sequence[str],
    *,
    require_plan: bool = True,
) -> tuple[str, ...]:
    available = tuple(str(role).strip().upper() for role in configured_roles)
    if value is None:
        return available
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("roles must be a list of workflow roles")
    requested: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("roles must contain non-empty strings")
        requested.append(item.strip().upper())
    if not requested:
        raise ValueError("roles must not be empty")
    if len(set(requested)) != len(requested):
        raise ValueError("roles must not contain duplicates")
    unknown = set(requested) - set(available)
    if unknown:
        raise ValueError(f"unknown workflow roles: {sorted(unknown)!r}")
    if require_plan and "PLAN" not in requested:
        raise ValueError("roles must include PLAN")
    selected = set(requested)
    return tuple(role for role in available if role in selected)


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


def canonicalize_task_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("task must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("task must not be empty")
    return text


def normalize_governing_contract(
    objective: Any,
    directives: Any,
) -> tuple[str, str]:
    if not isinstance(objective, str):
        raise ValueError("governing_contract_required: governing_objective must be a nonblank string")
    objective = objective.replace("\r\n", "\n").replace("\r", "\n")
    if not objective.strip():
        raise ValueError("governing_contract_required: governing_objective must be a nonblank string")
    if not isinstance(directives, (list, tuple)):
        raise ValueError("governing_contract_required: governing_directives must be an explicit list")
    normalized: list[str] = []
    for item in directives:
        if not isinstance(item, str):
            raise ValueError("governing_contract_required: governing_directives entries must be strings")
        item = item.replace("\r\n", "\n").replace("\r", "\n")
        if not item.strip():
            raise ValueError("governing_contract_required: governing_directives entries must be nonblank")
        normalized.append(item)
    payload = json.dumps(
        {"objective": objective, "directives": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload) > 4096:
        raise ValueError("governing_contract_required: governing semantic payload exceeds 4096 code points")
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _optional_solo_conversation_url(value: Any) -> str | None:
    if value is None:
        return None
    normalized = normalize_chatgpt_conversation_url(value)
    if normalized is None:
        raise ValueError(
            "conversation URL must be null or an exact ChatGPT conversation URL ending in /c/<conversation-id>"
        )
    return normalized


def effective_task_goal(state: Mapping[str, Any]) -> str:
    value = state.get("effective_goal")
    if isinstance(value, str) and value.strip():
        return value
    if str(state.get("workflow_mode") or "team").strip().lower() == "solo":
        return str(state.get("goal") or "")
    return str(state.get("task_text") or "")


def task_goal_for_hop(state: Mapping[str, Any], hop_id: int) -> str:
    selected = (
        str(state.get("goal") or "")
        if str(state.get("workflow_mode") or "team").strip().lower() == "solo"
        else str(state.get("task_text") or "")
    )
    revisions = state.get("goal_revisions")
    if not isinstance(revisions, list):
        return selected
    for revision in revisions:
        if not isinstance(revision, Mapping):
            continue
        applies_from = revision.get("applies_from_hop_id")
        goal = revision.get("goal")
        if (
            isinstance(applies_from, int)
            and not isinstance(applies_from, bool)
            and applies_from <= int(hop_id)
            and isinstance(goal, str)
            and goal.strip()
        ):
            selected = goal
    return selected


def governing_semantic_for_hop(
    state: Mapping[str, Any],
    hop_id: int,
) -> tuple[str, str, int, str]:
    payload = state.get("governing_semantic_payload")
    digest = state.get("governing_semantic_sha256")
    revision_number = 0
    revisions = state.get("goal_revisions")
    if isinstance(revisions, list):
        for revision in revisions:
            if not isinstance(revision, Mapping):
                continue
            applies_from = revision.get("applies_from_hop_id")
            if (
                isinstance(applies_from, int)
                and not isinstance(applies_from, bool)
                and applies_from <= int(hop_id)
            ):
                payload = revision.get("governing_semantic_payload")
                digest = revision.get("governing_semantic_sha256")
                revision_number = int(revision.get("revision") or 0)
    if not isinstance(payload, str) or not isinstance(digest, str):
        raise ValueError(
            "governing_contract_unavailable: authoritative governing semantic payload is missing"
        )
    try:
        semantic = json.loads(payload)
        if not isinstance(semantic, Mapping) or list(semantic) != [
            "objective",
            "directives",
        ]:
            raise ValueError
        canonical_payload, canonical_digest = normalize_governing_contract(
            semantic.get("objective"),
            semantic.get("directives"),
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "governing_contract_unavailable: governing semantic payload is invalid"
        ) from exc
    if canonical_payload != payload or canonical_digest != digest:
        raise ValueError(
            "governing_contract_unavailable: governing semantic source/hash mismatch"
        )
    goal = task_goal_for_hop(state, int(hop_id))
    goal_sha256 = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    return payload, digest, revision_number, goal_sha256


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
    original = effective_task_goal(target).strip()
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


def replacement_task_for(
    state: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    task_id = str(state.get("task_id") or "")
    if not task_id:
        return None
    return next(
        (
            item
            for item in tasks
            if str(item.get("replaces_task_id") or "") == task_id
        ),
        None,
    )


def is_replaced_immutable_history(
    state: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> bool:
    return replacement_task_for(state, tasks) is not None


from .cdpa_store_independent import IndependentAgentStoreMixin


class TaskStore(IndependentAgentStoreMixin):
    def __init__(self, config: CDPAConfig) -> None:
        self.config = config
        self.root = config.plans_root
        self.allocation_lock = self.root / ".cdpa-allocation.lock"
        self.catalog_path = self.root / ".cdpa-catalog.json"
        self.phase4_journal_path = self.root / ".cdpa-phase4-replacement.journal"
        self.repair_journal_path = self.root / ".cdpa-repair-transaction.journal"
        self.workflow_agents = WorkflowAgentCatalog(config)
        self.endpoints = BrowserEndpointRegistry(config)

    def endpoint_id_for(self, state: Mapping[str, Any]) -> str:
        """Missing legacy assignments refer to the original browser, never the default."""
        return str(state["browser_endpoint_id"]) if "browser_endpoint_id" in state else self.endpoints.legacy_endpoint_id

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
        atomic_replace(temporary, self.catalog_path)
        fsync_parent_directory(self.catalog_path)

    def _load_catalog_unlocked(self, *, reconcile: bool = True) -> dict[str, Any]:
        if self.catalog_path.exists():
            value = json.loads(read_shared_text(self.catalog_path, encoding="utf-8"))
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
                    state = json.loads(read_shared_text(path, encoding="utf-8"))
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

    def repository_projects(self) -> dict[str, str]:
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            value = catalog.get("repository_projects", {})
            if not isinstance(value, Mapping):
                raise ValueError("CDPA catalog repository_projects must be an object")
            result: dict[str, str] = {}
            for repository, project_id in value.items():
                canonical = str(Path(str(repository)).expanduser().resolve())
                if canonical != repository or not isinstance(project_id, str) or not project_id.startswith("g-p-") or project_id in result.values():
                    raise ValueError("CDPA catalog repository_projects contains invalid identity")
                result[canonical] = project_id
            return result

    def set_repository_project(self, repository: str | Path, project_id: str) -> None:
        canonical = str(Path(repository).expanduser().resolve())
        if not isinstance(project_id, str) or not project_id.startswith("g-p-"):
            raise ValueError("Project ID must start with g-p-")
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            projects = catalog.get("repository_projects", {})
            if not isinstance(projects, dict):
                raise ValueError("CDPA catalog repository_projects must be an object")
            if any(owner != canonical and value == project_id for owner, value in projects.items()):
                raise ValueError("Project ID is already mapped to another repository")
            projects[canonical] = project_id
            catalog["repository_projects"] = projects
            self._write_catalog_unlocked(catalog)

    def clear_repository_projects(self) -> int:
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            projects = catalog.get("repository_projects", {})
            if not isinstance(projects, dict):
                raise ValueError("CDPA catalog repository_projects must be an object")
            removed = len(projects)
            if not removed:
                return 0
            catalog["repository_projects"] = {}
            self._write_catalog_unlocked(catalog)
            return removed

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
        include_unreadable = not bool(catalog.get("entries"))
        for path in self._filesystem_manifest_like_paths_for_team(
            team,
            include_unreadable=include_unreadable,
        ):
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
                    state = json.loads(read_shared_text(target, encoding="utf-8"))
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
        try:
            mode = task_mode(state)
        except ValueError as exc:
            return str(exc)

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

        if "browser_endpoint_id" in state:
            endpoint_id = state["browser_endpoint_id"]
            if not isinstance(endpoint_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", endpoint_id) is None:
                return "browser_endpoint_id must be a durable endpoint identifier"
        if "required_browser_endpoint_id" in state:
            endpoint_id = state["required_browser_endpoint_id"]
            if not isinstance(endpoint_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", endpoint_id) is None:
                return "required_browser_endpoint_id must be a durable endpoint identifier"
        if "conversation_mode" in state and state["conversation_mode"] not in {"persistent", "temporary"}:
            return "conversation_mode must be persistent or temporary"
        if "context_policy" in state and state["context_policy"] not in {"fresh", "bootstrap"}:
            return "context_policy must be fresh or bootstrap"
        if state.get("context_policy") == "fresh" and state.get("bootstrap") is not None:
            return "fresh context policy must not include a bootstrap snapshot"
        if state.get("context_policy") == "bootstrap" and not isinstance(state.get("bootstrap"), Mapping):
            return "bootstrap context policy requires a bootstrap snapshot"

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
        applied_command_ids = state.get("applied_command_ids", [])
        if (
            not isinstance(applied_command_ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in applied_command_ids)
            or len(set(applied_command_ids)) != len(applied_command_ids)
        ):
            return "task manifest applied_command_ids must be a unique string array"
        effective_goal = state.get("effective_goal")
        goal_revisions = state.get("goal_revisions")
        if effective_goal is not None and (
            not isinstance(effective_goal, str) or not effective_goal.strip()
        ):
            return "effective_goal must be a non-empty string"
        if goal_revisions is not None:
            if not isinstance(goal_revisions, list):
                return "goal_revisions must be a list"
            expected_revision = 1
            latest_goal = None
            for revision in goal_revisions:
                if not isinstance(revision, Mapping):
                    return "goal revision must be an object"
                if set(revision) != {
                    "revision",
                    "changed_at",
                    "applies_from_hop_id",
                    "goal",
                    "governing_semantic_payload",
                    "governing_semantic_sha256",
                    "external_command_id",
                }:
                    return "goal revision keys are invalid"
                if revision.get("revision") != expected_revision:
                    return "goal revisions must be consecutively ordered"
                changed_at = revision.get("changed_at")
                applies_from = revision.get("applies_from_hop_id")
                goal = revision.get("goal")
                command_id = revision.get("external_command_id")
                if not isinstance(changed_at, str) or not changed_at.strip():
                    return "goal revision changed_at must be a non-empty string"
                if (
                    isinstance(applies_from, bool)
                    or not isinstance(applies_from, int)
                    or applies_from < 2
                ):
                    return "goal revision applies_from_hop_id must be at least 2"
                if not isinstance(goal, str) or not goal.strip():
                    return "goal revision goal must be a non-empty string"
                semantic_payload = revision.get("governing_semantic_payload")
                semantic_sha256 = revision.get("governing_semantic_sha256")
                if (
                    not isinstance(semantic_payload, str)
                    or len(semantic_payload) > 4096
                    or not isinstance(semantic_sha256, str)
                    or hashlib.sha256(semantic_payload.encode("utf-8")).hexdigest()
                    != semantic_sha256
                ):
                    return "goal revision governing contract is invalid"
                try:
                    semantic = json.loads(semantic_payload)
                    if not isinstance(semantic, Mapping) or list(semantic) != [
                        "objective",
                        "directives",
                    ]:
                        return "goal revision governing contract is invalid"
                    canonical_payload, canonical_sha256 = normalize_governing_contract(
                        semantic.get("objective"),
                        semantic.get("directives"),
                    )
                except (ValueError, json.JSONDecodeError):
                    return "goal revision governing contract is invalid"
                if (
                    canonical_payload != semantic_payload
                    or canonical_sha256 != semantic_sha256
                ):
                    return "goal revision governing contract is invalid"
                if not isinstance(command_id, str) or not command_id.strip():
                    return "goal revision external_command_id must be a non-empty string"
                latest_goal = goal
                expected_revision += 1
            if goal_revisions and effective_goal != latest_goal:
                return "effective_goal must match the latest goal revision"
            if not goal_revisions and effective_goal is not None:
                return "effective_goal requires at least one goal revision"
        elif effective_goal is not None:
            return "effective_goal requires goal_revisions"
        if mode == TASK_MODE_INDEPENDENT and (
            effective_goal is not None or goal_revisions is not None
        ):
            return "independent task must not contain workflow goal revisions"
        try:
            report_mode_from_options(state["options"])
            dependencies = normalize_dependency_ids(state.get("depends_on_task_ids"))
            _optional_nonempty_string(state.get("replaces_task_id"), "replaces_task_id")
            _optional_nonempty_string(
                state.get("replacement_incident_id"),
                "replacement_incident_id",
            )
            _optional_nonempty_string(
                state.get("replaced_by_task_id"),
                "replaced_by_task_id",
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
                "trigger",
                "bootstrap",
                "browser_pool",
                "role_reconnect",
            }:
                return "waiting reason is invalid"
            if waiting.get("reason") == "browser_pool" and (
                waiting.get("resume_status") not in {"INBOX", "RUNNING"}
                or not isinstance(waiting.get("resume_column"), str)
                or not isinstance(waiting.get("resume_action"), str)
            ):
                return "browser_pool waiting requires a valid resume state"
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
        if mode == TASK_MODE_INDEPENDENT and maintenance is not None:
            return "independent task must not contain legacy maintenance state"
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

        bootstrap = state.get("bootstrap")
        if bootstrap is not None:
            if mode != TASK_MODE_WORKFLOW:
                return "bootstrap snapshot is only valid for workflow tasks"
            try:
                normalized_bootstrap = (
                    normalize_legacy_bootstrap_record(bootstrap)
                    if is_legacy_bootstrap_record(bootstrap)
                    else normalize_bootstrap_record(bootstrap)
                )
            except ValueError as exc:
                return f"bootstrap snapshot is invalid: {exc}"
            if dict(bootstrap) != normalized_bootstrap:
                return "bootstrap snapshot must be canonical"

        roles = state["roles"]
        if mode == TASK_MODE_INDEPENDENT:
            independent_error = validate_independent_object(state.get("independent"))
            if independent_error is not None:
                return independent_error
            independent = state["independent"]
            _display, _agent_key, expected_agent_team = normalize_agent_name(
                independent.get("agent_name")
            )
            if team != expected_agent_team or team_base != expected_agent_team or suffix != 1:
                return "independent exact team does not match immutable agent identity"
            configured_roles = (INDEPENDENT_ROLE,)
            if state.get("depends_on_task_ids"):
                return "independent task must not use workflow dependencies"
            if state.get("queue") is not None:
                return "independent task must not use exact-team workflow queue state"
        else:
            if "independent" in state:
                return "workflow task must not contain independent state"
            try:
                configured_roles = tuple(
                    task_workflow_definitions(state, self.config)
                )
            except ValueError as exc:
                return str(exc)
        if set(roles) != set(configured_roles):
            return "roles must contain exactly the task-mode logical roles"
        for logical in configured_roles:
            record = roles.get(logical)
            if not isinstance(record, Mapping):
                return f"role record {logical!r} must be an object"
            if "browser_endpoint_id" in record:
                endpoint_id = record["browser_endpoint_id"]
                if not isinstance(endpoint_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", endpoint_id) is None:
                    return f"role record {logical!r} browser_endpoint_id must be a durable endpoint identifier"
            if str(record.get("logical_role") or "").upper() != logical:
                return f"role record {logical!r} has inconsistent logical_role"
            expected_physical = physical_role(logical, team_base, suffix)
            allowed_physical = {expected_physical}
            if mode == TASK_MODE_WORKFLOW:
                allowed_physical.add(f"{logical}{'' if suffix == 1 else suffix}")
            if str(record.get("physical_role") or "") not in allowed_physical:
                return f"role record {logical!r} has inconsistent physical_role"
            turn = record.get("turn")
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
                return f"role record {logical!r} has invalid turn"
            if not isinstance(record.get("status"), str):
                return f"role record {logical!r} has invalid status"
            if not isinstance(record.get("online"), bool):
                return f"role record {logical!r} has invalid online flag"
            context_source = record.get("context_source")
            if context_source is not None:
                if context_source in _BOOTSTRAP_CONTEXT_SOURCES:
                    if bootstrap is None:
                        return f"role record {logical!r} context_source requires bootstrap snapshot"
                elif context_source == "fresh":
                    if state.get("context_policy") != "fresh":
                        return f"role record {logical!r} fresh context requires explicit fresh policy"
                elif context_source == "execution_recovery":
                    if not any(
                        isinstance(hop, Mapping)
                        and hop.get("kind") == "execution_recovery"
                        and hop.get("target_role") == logical
                        and hop.get("recovery_of_request_id")
                        for hop in state.get("hops", [])
                    ):
                        return f"role record {logical!r} recovery context requires durable recovery provenance"
                else:
                    return f"role record {logical!r} has invalid context_source"
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
            kind = str(hop.get("kind") or "")
            if mode == TASK_MODE_INDEPENDENT and kind not in {"independent_job", "independent_cycle"}:
                return f"hop {hop_id} has invalid independent kind"
            if mode == TASK_MODE_WORKFLOW and kind in {"independent_job", "independent_cycle"}:
                return f"hop {hop_id} has invalid workflow kind"
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
                    sibling_state = json.loads(read_shared_text(sibling, encoding="utf-8"))
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
            state = json.loads(read_shared_text(candidate, encoding="utf-8"))
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

    def _filesystem_manifest_like_paths(
        self, *, include_unreadable: bool | None = None
    ) -> list[Path]:
        if not self.root.exists():
            return []
        root = self.root.resolve()
        if include_unreadable is None:
            include_unreadable = not self.catalog_path.exists()
        candidates: list[Path] = []
        for path in self.root.glob("*/*/*.json"):
            if not path.is_file() or path.name == "requests.json" or path.name.endswith(".lock"):
                continue
            candidate = path.resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            team_directory, task_directory, _filename = relative.parts
            try:
                value = json.loads(read_shared_text(path, encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                manifest_lock = candidate.with_suffix(candidate.suffix + ".lock")
                if include_unreadable or manifest_lock.is_file():
                    candidates.append(candidate)
                continue
            if not isinstance(value, Mapping):
                continue
            looks_like_manifest = value.get("schema_version") == SCHEMA_VERSION or {
                "manifest_path",
                "task_id",
                "team",
            }.issubset(value)
            if not looks_like_manifest:
                continue
            declared_team = str(value.get("team") or "")
            declared_task_id = str(value.get("task_id") or "")
            if declared_team and declared_task_id and (
                declared_team != team_directory or declared_task_id != task_directory
            ):
                # Benchmark snapshots and exported evidence may contain a complete
                # manifest copy. They are not runtime sources unless their physical
                # <team>/<task_id>/ directory matches the declared identity.
                continue
            candidates.append(candidate)
        return sorted(set(candidates))

    def _filesystem_manifest_like_paths_for_team(
        self,
        team: str,
        *,
        include_unreadable: bool | None = None,
    ) -> list[Path]:
        if not self.root.exists():
            return []
        root = self.root.resolve()
        team_root = (root / team).resolve()
        if not team_root.is_relative_to(root) or not team_root.exists():
            return []
        if include_unreadable is None:
            include_unreadable = not self.catalog_path.exists()
        candidates: list[Path] = []
        for path in team_root.glob("*/*.json"):
            if not path.is_file() or path.name == "requests.json" or path.name.endswith(".lock"):
                continue
            candidate = path.resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) != 3 or relative.parts[0] != team:
                continue
            team_directory, task_directory, _filename = relative.parts
            try:
                value = json.loads(read_shared_text(path, encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                manifest_lock = candidate.with_suffix(candidate.suffix + ".lock")
                if include_unreadable or manifest_lock.is_file():
                    candidates.append(candidate)
                continue
            if not isinstance(value, Mapping):
                continue
            looks_like_manifest = value.get("schema_version") == SCHEMA_VERSION or {
                "manifest_path",
                "task_id",
                "team",
            }.issubset(value)
            if not looks_like_manifest:
                continue
            declared_team = str(value.get("team") or "")
            declared_task_id = str(value.get("task_id") or "")
            if declared_team and declared_task_id and (
                declared_team != team_directory or declared_task_id != task_directory
            ):
                continue
            candidates.append(candidate)
        return sorted(set(candidates))

    def _filesystem_reservations(
        self, *, include_unreadable: bool | None = None
    ) -> list[dict[str, Any]]:
        reservations: list[dict[str, Any]] = []
        for path in self._filesystem_manifest_like_paths(
            include_unreadable=include_unreadable
        ):
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
            catalog = json.loads(read_shared_text(self.catalog_path, encoding="utf-8"))
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
                value = json.loads(read_shared_text(candidate, encoding="utf-8"))
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
                    value = json.loads(read_shared_text(candidate, encoding="utf-8"))
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

    def _discover_with_errors_unlocked(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        catalog = self._load_catalog_unlocked(reconcile=False)
        tasks, errors = self._discover_with_catalog_unlocked(catalog)
        records = [
            (Path(str(task["manifest_path"])).expanduser().resolve(), task)
            for task in tasks
        ]
        try:
            self.validate_repository_integrity(records)
        except ValueError as exc:
            errors.append(
                {
                    "manifest_path": str(self.catalog_path),
                    "error": f"DependencyGraphError: {exc}",
                }
            )
        return tasks, errors

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
        tasks, errors = self._filter_catalog_tasks(tasks, errors, catalog)
        records = [
            (Path(str(task["manifest_path"])).expanduser().resolve(), task)
            for task in tasks
        ]
        try:
            self.validate_repository_integrity(records)
        except ValueError as exc:
            errors.append(
                {
                    "manifest_path": str(self.catalog_path),
                    "error": f"DependencyGraphError: {exc}",
                }
            )
        return tasks, errors

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
                value = json.loads(read_shared_text(candidate, encoding="utf-8"))
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

    def load_task_id(self, task_id: str) -> dict[str, Any] | None:
        """Load one task through the compact catalog without filesystem discovery."""
        expected = _validate_task_id(task_id)
        with exclusive_file_lock(self.allocation_lock):
            catalog = self._load_catalog_unlocked(reconcile=False)
            matches = [
                Path(str(entry.get("manifest_path") or "")).expanduser().resolve()
                for entry in catalog.get("entries", {}).values()
                if isinstance(entry, Mapping)
                and str(entry.get("task_id") or "") == expected
            ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError(f"task_id is ambiguous in catalog: {expected}")
        return self.load(matches[0])

    def duplicate_task_ids(self) -> set[str]:
        return self._duplicate_dependency_task_ids(self._dependency_states())

    def validate_repository_integrity(
        self,
        records: Sequence[tuple[Path, Mapping[str, Any]]],
    ) -> None:
        """Validate duplicate identities and the dependency DAG over loaded records."""
        duplicate_ids = self._duplicate_dependency_task_ids(records)
        if duplicate_ids:
            raise ValueError(f"duplicate task ID(s): {sorted(duplicate_ids)!r}")
        tasks = [item for _path, item in records]
        for item in tasks:
            task_id = str(item.get("task_id") or "")
            validate_new_dependencies(
                task_id,
                normalize_dependency_ids(item.get("depends_on_task_ids")),
                [other for other in tasks if other is not item],
                allow_missing=True,
            )

    def _dependency_graph_error(
        self,
        target: Path,
        state: Mapping[str, Any],
    ) -> str | None:
        records = self._dependency_states(target=target, state=state)
        try:
            self.validate_repository_integrity(records)
        except ValueError as exc:
            return str(exc)
        return None

    def load(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        with exclusive_file_lock(self._lock_path(target)):
            value = json.loads(read_shared_text(target, encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid CDPA task manifest in {target}: root must be an object")
        error = self._manifest_value_error(target, value)
        if error is not None:
            raise ValueError(f"invalid CDPA task manifest in {target}: {error}")
        return dict(value)

    def _save_unlocked(
        self,
        target: Path,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = json.loads(json.dumps(dict(state), ensure_ascii=False, default=str))
        value["schema_version"] = SCHEMA_VERSION
        now = utc_now()
        value["updated_at"] = now
        error = self._manifest_value_error(target, value, require_file=False)
        if error is not None:
            raise ValueError(f"refusing to write invalid CDPA task manifest {target}: {error}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                atomic_replace(temporary, target)
                break
            except OSError as exc:
                if os.name != "nt" or (
                    not isinstance(exc, PermissionError)
                    and getattr(exc, "winerror", None) != 5
                ) or attempt == 5:
                    raise
                time.sleep(0.05)
        fsync_parent_directory(target)
        return value

    def _load_current_manifest_unlocked(self, target: Path) -> dict[str, Any]:
        current = json.loads(read_shared_text(target, encoding="utf-8"))
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
        catalog = self._load_catalog_unlocked(reconcile=False)
        entry = catalog.get("entries", {}).get(self._catalog_key(target), {})
        if isinstance(entry, Mapping) and entry.get("replaced_by_task_id"):
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

    @staticmethod
    def _clock_datetime(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _legacy_running_clock(
        cls,
        state: Mapping[str, Any],
        *,
        start_at: object,
        now: datetime,
    ) -> tuple[float, str | None]:
        start = cls._clock_datetime(start_at)
        if start is None:
            return 0.0, None
        elapsed = 0.0
        running_since = start
        running = True
        controls: list[tuple[datetime, str]] = []
        for item in state.get("controls") or []:
            if not isinstance(item, Mapping):
                continue
            action = str(item.get("action") or "").strip().lower()
            if action not in {"pause", "resume"}:
                continue
            applied = cls._clock_datetime(item.get("applied_at"))
            if applied is None or applied < start or applied > now:
                continue
            controls.append((applied, action))
        for applied, action in sorted(controls):
            if action == "pause" and running:
                elapsed += max(0.0, (applied - running_since).total_seconds())
                running_since = applied
                running = False
            elif action == "resume" and not running:
                running_since = applied
                running = True

        status = str(state.get("status") or "").upper()
        if running and status != "RUNNING":
            boundary = None
            if status == "BLOCKED":
                boundary = cls._clock_datetime(state.get("blocked_at"))
            elif status == "WAITING":
                waiting = state.get("waiting")
                if isinstance(waiting, Mapping):
                    boundary = cls._clock_datetime(waiting.get("since"))
            if boundary is None:
                boundary = cls._clock_datetime(state.get("updated_at")) or now
            boundary = min(max(boundary, running_since), now)
            elapsed += max(0.0, (boundary - running_since).total_seconds())
            running = False
        if running:
            return elapsed, running_since.isoformat()
        return elapsed, None

    @classmethod
    def _apply_running_clocks(
        cls,
        original: Mapping[str, Any],
        candidate: dict[str, Any],
        *,
        at: str,
    ) -> None:
        now = cls._clock_datetime(at) or datetime.now(timezone.utc)
        before_status = str(original.get("status") or "").upper()
        after_status = str(candidate.get("status") or "").upper()

        if "running_elapsed_seconds" in original or "running_since" in original:
            elapsed = max(0.0, float(original.get("running_elapsed_seconds") or 0.0))
            since = cls._clock_datetime(original.get("running_since"))
        else:
            elapsed, legacy_since = cls._legacy_running_clock(
                original, start_at=original.get("started_at"), now=now
            )
            since = cls._clock_datetime(legacy_since)

        if before_status == "RUNNING" and after_status != "RUNNING":
            if since is not None:
                elapsed += max(0.0, (now - since).total_seconds())
            since = None
        elif before_status != "RUNNING" and after_status == "RUNNING":
            since = now
        elif after_status != "RUNNING":
            since = None
        candidate["running_elapsed_seconds"] = round(elapsed, 6)
        candidate["running_since"] = since.isoformat() if since is not None else None

        before_identity = (
            f"{str(original.get('active_role') or '').upper()}:{original.get('active_hop_id')}"
            if original.get("active_role") and original.get("active_hop_id") is not None
            else None
        )
        after_identity = (
            f"{str(candidate.get('active_role') or '').upper()}:{candidate.get('active_hop_id')}"
            if candidate.get("active_role") and candidate.get("active_hop_id") is not None
            else None
        )

        def hop_created(state: Mapping[str, Any], hop_id: object) -> object:
            for hop in state.get("hops") or []:
                if not isinstance(hop, Mapping) or hop.get("hop_id") != hop_id:
                    continue
                timestamps = hop.get("timestamps")
                if isinstance(timestamps, Mapping):
                    return timestamps.get("created_at")
            return None

        if after_identity is None:
            candidate["active_role_running_identity"] = None
            candidate["active_role_running_elapsed_seconds"] = 0.0
            candidate["active_role_running_since"] = None
            return

        stored_identity = str(original.get("active_role_running_identity") or "") or None
        if stored_identity == before_identity:
            role_elapsed = max(
                0.0, float(original.get("active_role_running_elapsed_seconds") or 0.0)
            )
            role_since = cls._clock_datetime(original.get("active_role_running_since"))
        else:
            role_elapsed, legacy_role_since = cls._legacy_running_clock(
                original,
                start_at=hop_created(original, original.get("active_hop_id")),
                now=now,
            )
            role_since = cls._clock_datetime(legacy_role_since)

        if after_identity != before_identity:
            role_elapsed = 0.0
            if after_status == "RUNNING":
                created = cls._clock_datetime(
                    hop_created(candidate, candidate.get("active_hop_id"))
                )
                role_since = max(created or now, now if before_status != "RUNNING" else created or now)
            else:
                role_since = None
        elif before_status == "RUNNING" and after_status != "RUNNING":
            if role_since is not None:
                role_elapsed += max(0.0, (now - role_since).total_seconds())
            role_since = None
        elif before_status != "RUNNING" and after_status == "RUNNING":
            role_since = now
        elif after_status != "RUNNING":
            role_since = None

        candidate["active_role_running_identity"] = after_identity
        candidate["active_role_running_elapsed_seconds"] = round(role_elapsed, 6)
        candidate["active_role_running_since"] = (
            role_since.isoformat() if role_since is not None else None
        )

    def _mutate_manifest(
        self,
        target: Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        *,
        expected_updated_at: str | None = None,
        mutable_fields: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                if (
                    expected_updated_at is not None
                    and str(current.get("updated_at") or "") != expected_updated_at
                ):
                    raise ValueError("task changed; reload before saving")
                original = json.loads(
                    json.dumps(current, ensure_ascii=False, default=str)
                )
                before_dependencies = normalize_dependency_ids(
                    current.get("depends_on_task_ids")
                )
                result = mutator(current)
                candidate = result if result is not None else current
                for field in (
                    "browser_endpoint_id",
                    "required_browser_endpoint_id",
                    "conversation_mode",
                    "context_policy",
                    "endpoint_pinned",
                ):
                    if field in mutable_fields:
                        continue
                    if field in original:
                        if field not in candidate or candidate[field] != original[field]:
                            raise ValueError(f"task {field} is immutable")
                    elif field == "endpoint_pinned" and field in candidate:
                        raise ValueError("task endpoint_pinned is immutable")
                if (
                    "browser_endpoint_id" not in original
                    and "browser_endpoint_id" in candidate
                ):
                    if candidate["browser_endpoint_id"] != self.endpoint_id_for(original):
                        raise ValueError("legacy task browser_endpoint_id is immutable")
                if before_dependencies != normalize_dependency_ids(
                    candidate.get("depends_on_task_ids")
                ):
                    raise ValueError(
                        "dependency change requires an explicit dependency mutation"
                    )
                if candidate == original:
                    return original
                before_status = str(original.get("status") or "").upper()
                after_status = str(candidate.get("status") or "").upper()
                transition_at = utc_now()
                self._apply_running_clocks(original, candidate, at=transition_at)
                if after_status == "BLOCKED":
                    if before_status != "BLOCKED":
                        candidate["blocked_at"] = transition_at
                    elif not candidate.get("blocked_at"):
                        candidate["blocked_at"] = str(
                            original.get("updated_at") or utc_now()
                        )
                else:
                    candidate.pop("blocked_at", None)
                if candidate == original:
                    return original
                saved = self._save_unlocked(target, candidate)
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

    def update(
        self,
        path: str | Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        return self._mutate_manifest(target, mutator)

    def update_initial_placement(
        self,
        path: str | Path,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        """Persist the one-time endpoint choice for an unplaced workflow team."""
        target = Path(path).expanduser().resolve()

        def place(current: dict[str, Any]) -> Mapping[str, Any] | None:
            if current.get("endpoint_pinned", True) is not False:
                raise ValueError("initial endpoint placement requires an unpinned task")
            before = json.loads(json.dumps(current, ensure_ascii=False))
            active_hop_id = current.get("active_hop_id")
            hop = next(
                (
                    item
                    for item in current.get("hops") or ()
                    if isinstance(item, Mapping) and item.get("hop_id") == active_hop_id
                ),
                None,
            )
            if (
                not isinstance(hop, Mapping)
                or str(hop.get("state") or "") != "pre_send"
                or hop.get("receipt") is not None
                or hop.get("message_identity") is not None
            ):
                raise ValueError("initial endpoint placement requires pristine pre-Send state")
            active_role = str(current.get("active_role") or "").upper()
            role = (current.get("roles") or {}).get(active_role)
            if not isinstance(role, Mapping) or role.get("page_id") or role.get("page_url"):
                raise ValueError("initial endpoint placement requires an unbound active role")

            result = mutator(current)
            candidate = result if result is not None else current
            if candidate.get("endpoint_pinned") is not True:
                raise ValueError("initial endpoint placement must pin the selected endpoint")

            allowed_top = {
                "browser_endpoint_id",
                "endpoint_pinned",
                "conversation_mode",
                "context_policy",
                "roles",
                "hops",
            }
            for key in before.keys() | candidate.keys():
                if key not in allowed_top and before.get(key) != candidate.get(key):
                    raise ValueError(
                        f"initial endpoint placement cannot mutate {key}"
                    )

            admission = (
                before.get("admission")
                if isinstance(before.get("admission"), Mapping)
                else {}
            )
            if bool(admission.get("browser_endpoint_explicit", False)):
                raise ValueError(
                    "initial endpoint placement requires omitted endpoint provenance"
                )
            if bool(admission.get("required_endpoint_explicit", False)):
                raise ValueError(
                    "initial endpoint placement cannot override a required endpoint"
                )
            if (
                before.get("conversation_mode") != candidate.get("conversation_mode")
                and bool(
                    admission.get(
                        "conversation_mode_explicit",
                        before.get("conversation_mode_explicit", False),
                    )
                )
            ):
                raise ValueError(
                    "initial endpoint placement cannot rewrite explicit conversation mode"
                )
            endpoint = self.endpoints.get(str(candidate.get("browser_endpoint_id") or ""))
            if not bool(
                admission.get(
                    "conversation_mode_explicit",
                    before.get("conversation_mode_explicit", False),
                )
            ):
                expected_mode = (
                    "temporary"
                    if endpoint.get("temporary_default")
                    else "persistent"
                )
                if candidate.get("conversation_mode") != expected_mode:
                    raise ValueError(
                        "initial endpoint placement must use the selected endpoint default mode"
                    )
            if before.get("context_policy") != candidate.get("context_policy"):
                if bool(admission.get("context_policy_explicit", False)):
                    raise ValueError(
                        "initial endpoint placement cannot rewrite explicit context policy"
                    )
                if before.get("bootstrap") is not None:
                    raise ValueError(
                        "initial endpoint placement cannot rewrite bootstrap context"
                    )
                if candidate.get("context_policy") != "fresh":
                    raise ValueError(
                        "initial endpoint placement may derive only Fresh context"
                    )
            if (
                before.get("bootstrap") is not None
                and candidate.get("conversation_mode") == "temporary"
            ):
                raise ValueError(
                    "initial endpoint placement cannot move bootstrap context to Temporary"
                )

            active_role = str(before.get("active_role") or "").upper()
            before_roles = before.get("roles") or {}
            candidate_roles = candidate.get("roles") or {}
            if set(before_roles) != set(candidate_roles):
                raise ValueError("initial endpoint placement cannot change workflow roles")
            for role_key in before_roles:
                old_role = before_roles[role_key]
                new_role = candidate_roles[role_key]
                if role_key != active_role:
                    if old_role != new_role:
                        raise ValueError(
                            "initial endpoint placement cannot mutate inactive roles"
                        )
                    continue
                for key in set(old_role) | set(new_role):
                    if key != "browser_endpoint_id" and old_role.get(key) != new_role.get(key):
                        raise ValueError(
                            f"initial endpoint placement cannot mutate active role {key}"
                        )

            before_hops = before.get("hops") or []
            candidate_hops = candidate.get("hops") or []
            if len(before_hops) != len(candidate_hops):
                raise ValueError("initial endpoint placement cannot change hop history")
            for old_hop, new_hop in zip(before_hops, candidate_hops):
                if old_hop.get("hop_id") != active_hop_id:
                    if old_hop != new_hop:
                        raise ValueError(
                            "initial endpoint placement cannot mutate inactive hops"
                        )
                    continue
                for key in set(old_hop) | set(new_hop):
                    if key != "browser_endpoint_id" and old_hop.get(key) != new_hop.get(key):
                        raise ValueError(
                            f"initial endpoint placement cannot mutate active hop {key}"
                        )
            return candidate

        return self._mutate_manifest(
            target,
            place,
            mutable_fields=frozenset(
                {
                    "browser_endpoint_id",
                    "conversation_mode",
                    "context_policy",
                    "endpoint_pinned",
                }
            ),
        )

    def update_for_control(
        self,
        path: str | Path,
        action: str,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        mutable_fields = (
            frozenset({"required_browser_endpoint_id"})
            if str(action).strip().lower() == "release_required_browser_endpoint"
            else frozenset()
        )
        return self._mutate_manifest(target, mutator, mutable_fields=mutable_fields)

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

    @staticmethod
    def _is_dependency_waiting_state(state: Mapping[str, Any]) -> bool:
        waiting = state.get("waiting")
        waiting_reason = (
            str(waiting.get("reason") or "")
            if isinstance(waiting, Mapping)
            else ""
        )
        return (
            waiting_reason in {"dependency", "dependency_team_busy"}
            or str(state.get("waiting_code") or "").startswith("dependency")
            or str(state.get("active_action") or "").startswith("waiting_dependency")
        )

    @staticmethod
    def _apply_dependency_barrier_state(
        state: dict[str, Any],
        readiness: DependencyReadiness,
        *,
        blocked_by_task_id: str | None = None,
    ) -> bool:
        if readiness.ready:
            return False
        waiting = (
            state.get("waiting")
            if isinstance(state.get("waiting"), Mapping)
            else {}
        )
        team_waiting = blocked_by_task_id is not None
        waiting_kind = "dependency_team_busy" if team_waiting else "dependency"
        code = (
            "dependency_team_busy"
            if team_waiting
            else "dependency_missing"
            if readiness.missing
            else "dependency_stopped"
            if readiness.stopped
            else "dependency"
        )
        blocked_ids = [*readiness.waiting_on, *readiness.missing]
        reason = (
            "Waiting for dependencies and exact-team ownership"
            if team_waiting
            else "Waiting for dependencies"
        )
        details = [*blocked_ids, *([blocked_by_task_id] if blocked_by_task_id else [])]
        if details:
            reason += ": " + ", ".join(details)
        desired = {
            "reason": waiting_kind,
            "waiting_on": list(readiness.waiting_on),
            "stopped": list(readiness.stopped),
            "missing": list(readiness.missing),
            "since": waiting.get("since") or utc_now(),
        }
        if team_waiting:
            desired["blocked_by_task_id"] = blocked_by_task_id
        unchanged = (
            str(state.get("status") or "").upper() == "WAITING"
            and state.get("kanban_column") == "WAITING"
            and state.get("active_action")
            == ("waiting_dependency_team" if team_waiting else "waiting_dependency")
            and state.get("waiting_code") == code
            and state.get("waiting_reason") == reason
            and dict(waiting) == desired
            and state.get("block_code") is None
            and state.get("block_retryable") is False
            and state.get("block_reason") is None
            and state.get("pause_reason") is None
        )
        if unchanged:
            return False
        state.update(
            status="WAITING",
            kanban_column="WAITING",
            active_action=(
                "waiting_dependency_team" if team_waiting else "waiting_dependency"
            ),
            waiting_reason=reason,
            waiting_code=code,
            waiting=desired,
            block_code=None,
            block_retryable=False,
            block_reason=None,
            pause_reason=None,
        )
        state.pop("blocked_at", None)
        state.pop("resume_column", None)
        state.setdefault("dependency_events", []).append(
            {
                "at": utc_now(),
                "status": "WAITING",
                "message": reason,
                "waiting_on": list(readiness.waiting_on),
                "stopped": list(readiness.stopped),
                "missing": list(readiness.missing),
            }
        )
        return True

    def enforce_dependency_barrier(
        self,
        state: dict[str, Any],
        tasks: Sequence[Mapping[str, Any]],
    ) -> bool:
        if is_independent_task(state):
            return False
        status = str(state.get("status") or "").upper()
        if status in TERMINAL or status == "PAUSED":
            return False
        dependencies = normalize_dependency_ids(state.get("depends_on_task_ids"))
        if not dependencies:
            return False
        return self._apply_dependency_barrier_state(
            state,
            dependency_readiness(state, tasks),
        )

    def refresh_scheduling(
        self,
        path: str | Path,
        *,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        target = Path(path).expanduser().resolve()
        if tasks is None:
            self.recover_phase4_replacement()
            scheduling_tasks, _errors = self.discover_with_errors()
        else:
            scheduling_tasks = list(tasks)
        if state is not None:
            current = state
            status = str(current.get("status") or "").upper()
            dependencies = normalize_dependency_ids(current.get("depends_on_task_ids"))
            dependency_waiting_origin = self._is_dependency_waiting_state(current)
            queue = current.get("queue")
            queue_pending = (
                isinstance(queue, Mapping)
                and queue.get("reuse_team") is True
                and queue.get("released_at") is None
            )
            if (
                is_independent_task(current)
                or status in TERMINAL
                or status == "PAUSED"
                or (
                    not queue_pending
                    and not dependencies
                    and not (status == "WAITING" and dependency_waiting_origin)
                )
            ):
                return current, False
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_phase4_replacement_unlocked()
            current = state if state is not None else self.load(target)
            status = str(current.get("status") or "").upper()
            if is_independent_task(current):
                return current, False
            if status in TERMINAL or status == "PAUSED":
                return current, False
            dependencies = normalize_dependency_ids(current.get("depends_on_task_ids"))
            dependency_waiting_origin = self._is_dependency_waiting_state(current)
            queue = current.get("queue")
            queue_pending = (
                isinstance(queue, Mapping)
                and queue.get("reuse_team") is True
                and queue.get("released_at") is None
            )
            if (
                not queue_pending
                and not dependencies
                and not (status == "WAITING" and dependency_waiting_origin)
            ):
                return current, False
            readiness = dependency_readiness(current, scheduling_tasks)
            if status == "WAITING" and (current.get("waiting") or {}).get("reason") == "browser_pool" and readiness.ready:
                return current, False  # Capacity release belongs to the worker allocator, not dependency/team queues.
            next_state = json.loads(json.dumps(current, ensure_ascii=False))
            changed = False
            if status != "WAITING":
                changed = self.enforce_dependency_barrier(
                    next_state,
                    scheduling_tasks,
                )
                if not changed:
                    return current, False
                with exclusive_file_lock(self._lock_path(target)):
                    latest = self._load_current_manifest_unlocked(target)
                    if latest.get("updated_at") != current.get("updated_at"):
                        raise ValueError("task changed while refreshing scheduling state")
                    saved = self._save_unlocked(target, next_state)
                self._catalog_saved_manifest_unlocked(saved)
                return saved, True
            team = str(current.get("team") or "")
            current_id = str(current.get("task_id") or "")
            blocked_by: str | None = None
            selected_id: str | None = None

            if status == "WAITING" and (
                queue_pending or dependencies or dependency_waiting_origin
            ):
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
                    else current_id
                    if dependency_waiting_origin and readiness.ready
                    else None
                )
                blocked_by = barrier_id or (
                    selected_id if selected_id != current_id else None
                )

            if queue_pending:
                if readiness.ready and blocked_by is None and selected_id == current_id:
                    if dependencies and (
                        current.get("waiting") or {}
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
                        current.get("waiting")
                        if isinstance(current.get("waiting"), Mapping)
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
                        and current.get("waiting_code") == code
                        and current.get("waiting_reason") == reason
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
            elif dependencies or dependency_waiting_origin:
                waiting = (
                    current.get("waiting")
                    if isinstance(current.get("waiting"), Mapping)
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
                        current.get("waiting_code") == code
                        and current.get("waiting_reason") == reason
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
                return current, False
            with exclusive_file_lock(self._lock_path(target)):
                latest = self._load_current_manifest_unlocked(target)
                if latest.get("updated_at") != current.get("updated_at"):
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
        workflow_roles: tuple[str, ...],
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
        workflow_agents: Mapping[str, Mapping[str, Any]] | None = None,
        normalized_bootstrap: Mapping[str, Any] | None = None,
        browser_endpoint_id: str | None = None,
        required_browser_endpoint_id: str | None = None,
        conversation_mode: str | None = None,
        context_policy: str | None = None,
        workflow_mode: str = "team",
        goal: str = "",
        conversation_url: str | None = None,
        initial_role: str | None = None,
        role_conversation_urls: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        endpoint = self.endpoints.resolve(browser_endpoint_id)
        required_endpoint = (
            self.endpoints.resolve(required_browser_endpoint_id)
            if required_browser_endpoint_id is not None
            else None
        )
        mode = conversation_mode or ("temporary" if endpoint["temporary_default"] else "persistent")
        if mode not in {"persistent", "temporary"}:
            raise ValueError("conversation_mode must be persistent or temporary")
        if context_policy not in {None, "fresh", "bootstrap"}:
            raise ValueError("context_policy must be fresh or bootstrap")
        if context_policy == "fresh" and normalized_bootstrap is not None:
            raise ValueError("fresh context policy must not include a bootstrap snapshot")
        if context_policy == "bootstrap" and normalized_bootstrap is None:
            raise ValueError("bootstrap context policy requires a bootstrap snapshot")
        if mode == "temporary" and normalized_bootstrap is not None:
            raise ValueError("temporary workflows do not support bootstrap context")
        initial_role = initial_role or (workflow_roles[0] if workflow_mode == "solo" else "PLAN")
        roles = {}
        for logical in workflow_roles:
            roles[logical] = {
                "logical_role": logical,
                "physical_role": physical_role(logical, base, suffix),
                "binding_id": str(uuid4()),
                "status": "pending" if logical == initial_role else "unallocated",
                "turn": 0,
                "page_id": None,
                "page_url": (role_conversation_urls or {}).get(logical),
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
            "target_role": initial_role,
            "physical_role": roles[initial_role]["physical_role"],
            "turn": 1,
            "kind": "task",
            "handoff": "Initial task intake; no prior role handoff.",
            "state": "pre_send",
            "request_id": f"{task_id}-hop1",
            "prompt": None,
            "prompt_sha256": None,
            "rendered_prompt_sha256": None,
            "ledger_path": str((target.parent / "requests.json").resolve()),
            "receipt": None,
            "message_identity": None,
            **(
                {"conversation_url": (role_conversation_urls or {}).get(initial_role) or conversation_url}
                if (role_conversation_urls or {}).get(initial_role) or conversation_url
                else {}
            ),
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
            "task_mode": TASK_MODE_WORKFLOW,
            "workflow_mode": workflow_mode,
            "browser_endpoint_id": endpoint["endpoint_id"],
            **(
                {"required_browser_endpoint_id": required_endpoint["endpoint_id"]}
                if required_endpoint is not None
                else {}
            ),
            "conversation_mode": mode,
            **({"context_policy": context_policy} if context_policy is not None else {}),
            **(
                {
                    "bootstrap": json.loads(
                        json.dumps(dict(normalized_bootstrap), ensure_ascii=False)
                    )
                }
                if normalized_bootstrap is not None
                else {}
            ),
            "manifest_path": str(target),
            "task_id": task_id,
            "task_title": text.splitlines()[0],
            "task_text": text,
            **({"goal": goal} if workflow_mode == "solo" else {}),
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
            "active_role": initial_role,
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
            "workflow_agents": (
                json.loads(json.dumps(dict(workflow_agents), ensure_ascii=False))
                if workflow_agents is not None
                else None
            ),
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


    def list_workflow_agents(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        return self.workflow_agents.list_agents(include_deleted=include_deleted)

    def create_workflow_agent(
        self,
        *,
        display_name: Any,
        system_prompt: Any,
        continuation_contract: Any = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        return self.workflow_agents.create(
            display_name=display_name,
            system_prompt=system_prompt,
            continuation_contract=continuation_contract,
            external_command_id=external_command_id,
        )

    def update_workflow_agent(
        self,
        route_key: str,
        *,
        display_name: Any,
        system_prompt: Any,
        continuation_contract: Any = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        return self.workflow_agents.update(
            route_key,
            display_name=display_name,
            system_prompt=system_prompt,
            continuation_contract=continuation_contract,
            external_command_id=external_command_id,
        )

    def delete_workflow_agent(
        self,
        route_key: str,
        *,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        key = validate_workflow_route_key(route_key)
        if key in self.config.roles:
            raise ValueError("system workflow agents cannot be deleted")
        dependents = [
            state
            for state in self.discover()
            if str(state.get("status") or "").upper() not in TERMINAL
            and not is_independent_task(state)
            and key in state.get("roles", {})
        ]
        if dependents:
            task_ids = sorted(str(state.get("task_id") or "") for state in dependents)
            raise ValueError(
                f"nonterminal task depends on workflow agent {key}: {task_ids!r}"
            )
        return self.workflow_agents.delete(
            key, external_command_id=external_command_id
        )

    @staticmethod
    def _record_external_command(
        state: dict[str, Any], command_id: str | None
    ) -> None:
        value = str(command_id or "").strip()
        if not value:
            return
        applied = state.setdefault("applied_command_ids", [])
        if value not in applied:
            applied.append(value)

    def create_task(
        self,
        task: str,
        *,
        requested_team: str | None = None,
        reuse_team: str | None = None,
        roles: Sequence[str] | None = None,
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
        bootstrap: Mapping[str, Any] | None = None,
        browser_endpoint_id: str | None = None,
        required_browser_endpoint_id: str | None = None,
        temporary: bool = False,
        conversation_mode: str | None = None,
        context_policy: str | None = None,
        workflow_mode: str = "team",
        goal: str | None = None,
        conversation_url: str | None = None,
        initial_role: str | None = None,
        role_conversation_urls: Mapping[str, str] | None = None,
        governing_objective: str | None = None,
        governing_directives: Sequence[str] | None = None,
        admission_provenance: Mapping[str, Any] | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        if type(temporary) is not bool:
            raise ValueError("temporary must be a boolean")
        if conversation_mode is not None and conversation_mode not in {"persistent", "temporary"}:
            raise ValueError("conversation_mode must be persistent or temporary")
        if context_policy not in {None, "fresh", "bootstrap"}:
            raise ValueError("context_policy must be fresh or bootstrap")
        context_policy_was_explicit = context_policy is not None
        normalized_workflow_mode = str(workflow_mode or "team").strip().lower()
        if normalized_workflow_mode not in {"team", "solo"}:
            raise ValueError("workflow_mode must be team or solo")
        goal_text = str(goal or "")
        normalized_goal = canonicalize_task_text(goal_text) if goal_text else ""
        normalized_conversation_url = _optional_solo_conversation_url(conversation_url)
        if normalized_workflow_mode == "team":
            if goal is not None or normalized_conversation_url is not None:
                raise ValueError("goal and conversation_url are valid only for solo workflow tasks")
        elif browser_endpoint_id is None or not str(browser_endpoint_id).strip():
            raise ValueError("solo workflow task requires an explicit browser endpoint")
        if role_conversation_urls is not None and not isinstance(role_conversation_urls, Mapping):
            raise ValueError("role_conversation_urls must be a role-to-URL object")
        if normalized_workflow_mode == "solo" and (initial_role is not None or role_conversation_urls):
            raise ValueError("initial_role and role_conversation_urls belong to Team Tasks")
        normalized_urls: dict[str, str] = {}
        for key, value in (role_conversation_urls or {}).items():
            role = str(key).strip().upper()
            if not role or role in normalized_urls:
                raise ValueError("role_conversation_urls contains an invalid or duplicate role")
            normalized = normalize_saved_chatgpt_conversation_url(value)
            if normalized is None:
                raise ValueError(f"role {role} requires an exact persistent ChatGPT UUID URL")
            normalized_urls[role] = normalized
        if normalized_urls and not browser_endpoint_id:
            raise ValueError("adopting Team role URLs requires an explicitly selected browser endpoint")
        if len({str(conversation_identity(url)).lower() for url in normalized_urls.values()}) != len(normalized_urls):
            raise ValueError("one ChatGPT conversation cannot belong to multiple Team roles")
        requested_initial_role = str(initial_role or "").strip().upper() or None
        text = canonicalize_task_text(task)
        governing_payload: str | None = None
        governing_sha256: str | None = None
        if normalized_workflow_mode == "team":
            governing_payload, governing_sha256 = normalize_governing_contract(
                governing_objective,
                governing_directives,
            )
        task_id = _validate_task_id(task_id or generate_task_id(text))
        catalog_routes = tuple(
            item["route_key"] for item in self.workflow_agents.list_agents()
        )
        if normalized_workflow_mode == "solo" and (
            roles is None
            or isinstance(roles, (str, bytes))
            or not isinstance(roles, Sequence)
            or len(roles) != 1
        ):
            raise ValueError("solo workflow task requires exactly one selected role")
        requested_roles = (
            normalize_workflow_roles(
                roles,
                catalog_routes,
                require_plan=normalized_workflow_mode != "solo",
            )
            if roles is not None
            else None
        )
        if normalized_workflow_mode == "solo":
            if requested_roles is None or len(requested_roles) != 1:
                raise ValueError("solo workflow task requires exactly one selected role")
            if reuse_team is not None:
                raise ValueError("solo workflow task does not support exact-team reuse")
        normalized_new = tuple(dict.fromkeys(str(role).strip().upper() for role in new_roles))
        unknown = set(normalized_new) - set(catalog_routes)
        if unknown:
            raise ValueError(f"unknown --new roles: {sorted(unknown)!r}")
        normalized_replaces = _optional_nonempty_string(replaces_task_id, "replaces_task_id")
        normalized_incident = _optional_nonempty_string(
            replacement_incident_id, "replacement_incident_id"
        )
        normalized_bootstrap = (
            normalize_bootstrap_record(bootstrap) if bootstrap is not None else None
        )
        if context_policy is None:
            context_policy = "bootstrap" if normalized_bootstrap is not None else "fresh"
        if normalized_workflow_mode == "solo":
            if normalized_bootstrap is not None:
                raise ValueError("solo workflow task does not support bootstrap context")
            context_policy = "fresh"
            conversation_mode = "persistent"
        if context_policy == "fresh" and normalized_bootstrap is not None:
            raise ValueError("fresh context policy must not include a bootstrap snapshot")
        if context_policy == "bootstrap" and normalized_bootstrap is None:
            raise ValueError("bootstrap context policy requires a bootstrap snapshot")
        if requested_team is not None and reuse_team is not None:
            raise ValueError("requested_team and reuse_team are mutually exclusive")
        exact_reuse_team = validate_exact_team(reuse_team) if reuse_team is not None else None
        base = normalize_team_base(requested_team or task_id) if exact_reuse_team is None else ""
        repository_path = Path(repository or self.config.repository_root).expanduser().resolve()
        normalized_report_mode = effective_report_mode(
            report_mode,
            control_repository=self.config.repository_root,
            execution_repository=repository_path,
        )
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
            filesystem_reservations = self._filesystem_reservations(
                include_unreadable=not bool(catalog["entries"])
            )
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
            endpoint = self.endpoints.resolve(browser_endpoint_id)
            required_endpoint = (
                self.endpoints.resolve(required_browser_endpoint_id)
                if required_browser_endpoint_id is not None
                else None
            )
            if required_endpoint is not None:
                if browser_endpoint_id is None:
                    endpoint = required_endpoint
                elif required_endpoint["endpoint_id"] != endpoint["endpoint_id"]:
                    raise ValueError(
                        "workflow team uses one endpoint; browser endpoint and required endpoint must match"
                    )
            if normalized_workflow_mode == "solo":
                if (
                    required_endpoint is not None
                    and required_endpoint["endpoint_id"] != endpoint["endpoint_id"]
                ):
                    raise ValueError(
                        "solo workflow task browser endpoint and required endpoint must match"
                    )
                required_endpoint = endpoint
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
                compositions = {
                    tuple(task_workflow_definitions(item, self.config))
                    for item in exact_states
                }
                if len(compositions) != 1:
                    raise ValueError(
                        f"exact team {exact_reuse_team!r} has inconsistent role composition"
                    )
                inherited_roles = compositions.pop()
                if requested_roles is None:
                    workflow_roles = inherited_roles
                elif requested_roles != inherited_roles:
                    raise ValueError(
                        f"exact team {exact_reuse_team!r} role composition is locked to "
                        f"{list(inherited_roles)!r}"
                    )
                else:
                    workflow_roles = requested_roles
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
                reusable_teams = (
                    [team] if exact_team_reuse_eligible(exact_states, team) else []
                )
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
                workflow_roles = requested_roles or catalog_routes
                reusable_teams = list(
                    dict.fromkeys(
                        str(item.get("team"))
                        for item in terminal_candidates
                        if item.get("team")
                    )
                )
            if set(normalized_new) - set(workflow_roles):
                raise ValueError("new_roles must be selected workflow roles")
            if normalized_urls:
                requested_ids = {
                    str(conversation_identity(url)).lower()
                    for url in normalized_urls.values()
                }
                for existing_task in manifests:
                    if str(existing_task.get("status") or "").upper() in TERMINAL:
                        continue
                    occupied = [
                        str(item.get("page_url") or "")
                        for item in (existing_task.get("roles") or {}).values()
                        if isinstance(item, Mapping)
                    ] + [
                        str(item.get("conversation_url") or "")
                        for item in existing_task.get("hops") or ()
                        if isinstance(item, Mapping)
                    ]
                    if any(
                        candidate and normalize_chatgpt_conversation_url(candidate) is not None
                        and str(conversation_identity(candidate)).lower() in requested_ids
                        for candidate in occupied
                    ):
                        raise ValueError(
                            f"conversation URL already reserved by task {existing_task.get('task_id')}"
                        )
            if requested_initial_role and requested_initial_role not in workflow_roles:
                raise ValueError("initial_role must be a selected Team workflow role")
            if set(normalized_urls) - set(workflow_roles):
                raise ValueError("role_conversation_urls must reference selected Team workflow roles")
            if set(normalized_urls) & (set(normalized_new) | (set(workflow_roles) if new_all else set())):
                raise ValueError("an adopted role URL cannot also request a Fresh role reset")
            selected_mode = conversation_mode or ("temporary" if temporary or endpoint["temporary_default"] else "persistent")
            if normalized_urls and selected_mode != "persistent":
                raise ValueError("adopting an existing conversation requires persistent ChatGPT mode")
            workflow_agent_snapshot = self.workflow_agents.resolve(
                workflow_roles,
                require_plan=normalized_workflow_mode != "solo",
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
                workflow_roles=workflow_roles,
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
                workflow_agents=workflow_agent_snapshot,
                normalized_bootstrap=normalized_bootstrap,
                browser_endpoint_id=endpoint["endpoint_id"],
                required_browser_endpoint_id=(
                    required_endpoint["endpoint_id"]
                    if required_endpoint is not None
                    else None
                ),
                conversation_mode=selected_mode,
                context_policy=context_policy,
                workflow_mode=normalized_workflow_mode,
                goal=normalized_goal,
                conversation_url=normalized_conversation_url,
                initial_role=requested_initial_role,
                role_conversation_urls=normalized_urls,
            )
            endpoint_pinned = bool(
                normalized_workflow_mode == "solo"
                or exact_reuse_team is not None
                or browser_endpoint_id is not None
                or required_endpoint is not None
            )
            supplied = dict(admission_provenance or {})
            state["task_text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if normalized_goal:
                state["goal_sha256"] = hashlib.sha256(
                    normalized_goal.encode("utf-8")
                ).hexdigest()
            if governing_payload is not None:
                state["governing_semantic_payload"] = governing_payload
                state["governing_semantic_sha256"] = governing_sha256
            state["admission"] = {
                "requested_team_explicit": bool(supplied.get("requested_team_explicit", requested_team is not None)),
                "repository_explicit": bool(supplied.get("repository_explicit", repository is not None)),
                "browser_endpoint_explicit": bool(supplied.get("browser_endpoint_explicit", browser_endpoint_id is not None)),
                "required_endpoint_explicit": bool(supplied.get("required_endpoint_explicit", required_browser_endpoint_id is not None)),
                "conversation_mode_explicit": bool(supplied.get("conversation_mode_explicit", temporary or conversation_mode is not None)),
                "context_policy_explicit": bool(supplied.get("context_policy_explicit", context_policy_was_explicit)),
                "bootstrap_present": normalized_bootstrap is not None,
                "endpoint_pin_intent": endpoint_pinned,
                "normalized_team": team,
                "normalized_repository": str(repository_path),
                "normalized_browser_endpoint_id": endpoint["endpoint_id"],
                "normalized_required_browser_endpoint_id": (
                    required_endpoint["endpoint_id"] if required_endpoint is not None else None
                ),
                "normalized_conversation_mode": selected_mode,
                "normalized_context_policy": context_policy,
            }
            state["endpoint_pinned"] = endpoint_pinned
            state["conversation_mode_explicit"] = bool(
                temporary or conversation_mode is not None
            )
            self._record_external_command(state, external_command_id)
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
            atomic_replace(temporary, path)
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
            atomic_replace(temporary, self.phase4_journal_path)
            fsync_parent_directory(self.phase4_journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_phase4_journal_unlocked(self) -> dict[str, Any] | None:
        if not self.phase4_journal_path.exists():
            return None
        value = json.loads(read_shared_text(self.phase4_journal_path, encoding="utf-8"))
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
            atomic_replace(temporary, path)
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
            current = json.loads(read_shared_text(self.catalog_path, encoding="utf-8"))
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
        if hashlib.sha256(read_shared_bytes(target_path)).hexdigest() != journal["target_sha256"]:
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
        catalog = json.loads(read_shared_text(self.catalog_path, encoding="utf-8"))
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
        self._recover_repair_transaction_unlocked()
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
                data = read_shared_bytes(manifest_path)
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

    def _write_repair_journal_unlocked(self, value: Mapping[str, Any]) -> None:
        body = json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
        body["version"] = 1
        body["operation"] = "create_or_gate_repair"
        temporary = self.repair_journal_path.with_suffix(".journal.tmp")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            atomic_replace(temporary, self.repair_journal_path)
            fsync_parent_directory(self.repair_journal_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_repair_journal_unlocked(self) -> dict[str, Any] | None:
        if not self.repair_journal_path.exists():
            return None
        value = json.loads(read_shared_text(self.repair_journal_path, encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("repair transaction journal root must be an object")
        if value.get("version") != 1 or value.get("operation") != "create_or_gate_repair":
            raise ValueError("unsupported repair transaction journal")
        for field in (
            "root_cause_key",
            "repair_task_id",
            "affected_task_id",
            "incident_id",
            "operation_key",
            "disposition",
            "repair_manifest_path",
            "affected_manifest_path",
            "created_at",
        ):
            if not isinstance(value.get(field), str) or not str(value.get(field)).strip():
                raise ValueError(f"repair transaction journal {field} must be non-empty")
        if value.get("disposition") not in {"CONTINUE_IN_PARALLEL", "HOLD_FOR_REPAIR"}:
            raise ValueError("repair transaction journal disposition is invalid")
        writes = value.get("writes")
        if not isinstance(writes, list) or not writes:
            raise ValueError("repair transaction journal writes must be non-empty")
        seen: set[Path] = set()
        for index, item in enumerate(writes):
            if not isinstance(item, Mapping) or item.get("kind") not in {"repair", "affected"}:
                raise ValueError("repair transaction journal write is invalid")
            path = self._phase4_absolute_path(item.get("path"), f"writes[{index}].path")
            if path in seen:
                raise ValueError("repair transaction journal has duplicate paths")
            seen.add(path)
            self._phase4_decode_bytes(item.get("before"), f"writes[{index}].before")
            after = self._phase4_decode_bytes(item.get("after"), f"writes[{index}].after")
            if after is None or hashlib.sha256(after).hexdigest() != item.get("after_sha256"):
                raise ValueError("repair transaction journal write hash is invalid")
        catalog = value.get("catalog")
        if not isinstance(catalog, Mapping):
            raise ValueError("repair transaction journal catalog is invalid")
        self._phase4_decode_bytes(catalog.get("before"), "catalog.before")
        after_catalog = self._phase4_decode_bytes(catalog.get("after"), "catalog.after")
        if after_catalog is None or hashlib.sha256(after_catalog).hexdigest() != catalog.get(
            "after_sha256"
        ):
            raise ValueError("repair transaction journal catalog hash is invalid")
        return dict(value)

    def _clear_repair_journal_unlocked(self) -> None:
        self.repair_journal_path.unlink(missing_ok=True)
        fsync_parent_directory(self.repair_journal_path)

    def _recover_repair_transaction_unlocked(self) -> dict[str, Any] | None:
        journal = self._load_repair_journal_unlocked()
        if journal is None:
            return None
        write_entries = [dict(item) for item in journal["writes"]]
        write_paths = [
            self._phase4_absolute_path(item["path"], "write.path")
            for item in write_entries
        ]
        with ExitStack() as stack:
            for path in sorted(set(write_paths), key=str):
                stack.enter_context(exclusive_file_lock(self._lock_path(path)))
            for item, path in zip(write_entries, write_paths, strict=True):
                before = self._phase4_decode_bytes(item.get("before"), "write.before")
                after = self._phase4_decode_bytes(item.get("after"), "write.after")
                assert after is not None
                current = read_shared_bytes(path) if path.exists() else None
                if current == after:
                    continue
                if current != before:
                    raise RuntimeError(
                        f"repair transaction found conflicting manifest state: {path}"
                    )
                self._phase4_write_bytes_unlocked(path, after)
            catalog_before = self._phase4_decode_bytes(
                journal["catalog"].get("before"), "catalog.before"
            )
            catalog_after = self._phase4_decode_bytes(
                journal["catalog"].get("after"), "catalog.after"
            )
            assert catalog_after is not None
            current_catalog = read_shared_bytes(self.catalog_path) if self.catalog_path.exists() else None
            if current_catalog != catalog_after:
                if current_catalog != catalog_before:
                    raise RuntimeError("repair transaction found conflicting task catalog")
                self._phase4_write_bytes_unlocked(self.catalog_path, catalog_after)

            repair_path = self._phase4_absolute_path(
                journal["repair_manifest_path"], "repair_manifest_path"
            )
            affected_path = self._phase4_absolute_path(
                journal["affected_manifest_path"], "affected_manifest_path"
            )
            repair = json.loads(read_shared_text(repair_path, encoding="utf-8"))
            affected = json.loads(read_shared_text(affected_path, encoding="utf-8"))
            if not isinstance(repair, Mapping) or not isinstance(affected, Mapping):
                raise RuntimeError("repair transaction installed invalid manifest roots")
            metadata = repair.get("repair")
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("root_cause_key") != journal["root_cause_key"]
                or repair.get("task_id") != journal["repair_task_id"]
            ):
                raise RuntimeError("repair transaction installed invalid repair provenance")
            operations = metadata.get("affected_operations") or []
            if not any(
                isinstance(item, Mapping)
                and (
                    item.get("operation_key") == journal["operation_key"]
                    or (
                        item.get("affected_task_id") == journal["affected_task_id"]
                        and item.get("incident_id") == journal["incident_id"]
                        and item.get("disposition") == journal["disposition"]
                    )
                )
                for item in operations
            ):
                raise RuntimeError("repair transaction lost repair-side affected operation")
            links = affected.get("repair_links") or []
            if not any(
                isinstance(item, Mapping)
                and item.get("repair_task_id") == journal["repair_task_id"]
                and item.get("root_cause_key") == journal["root_cause_key"]
                and item.get("incident_id") == journal["incident_id"]
                and item.get("disposition") == journal["disposition"]
                and (
                    item.get("operation_key") == journal["operation_key"]
                    or item.get("operation_key") is None
                )
                for item in links
            ):
                raise RuntimeError("repair transaction lost affected-task relationship")
            dependencies = normalize_dependency_ids(
                affected.get("depends_on_task_ids")
            )
            wait = affected.get("repair_wait")
            if journal["disposition"] == "HOLD_FOR_REPAIR":
                if (
                    not isinstance(wait, Mapping)
                    or wait.get("repair_task_id") != journal["repair_task_id"]
                    or wait.get("incident_id") != journal["incident_id"]
                    or wait.get("disposition") != "HOLD_FOR_REPAIR"
                    or wait.get("state") != "WAITING"
                    or journal["repair_task_id"] not in dependencies
                ):
                    raise RuntimeError("repair transaction lost HOLD_FOR_REPAIR gate")
            elif journal["repair_task_id"] in dependencies or (
                isinstance(wait, Mapping)
                and wait.get("repair_task_id") == journal["repair_task_id"]
                and wait.get("state") == "WAITING"
            ):
                raise RuntimeError(
                    "repair transaction lost CONTINUE_IN_PARALLEL postcondition"
                )
            self._clear_repair_journal_unlocked()
            return {"repair": dict(repair), "affected": dict(affected)}

    def recover_repair_transaction(self) -> dict[str, Any] | None:
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            return self._recover_repair_transaction_unlocked()

    @staticmethod
    def _operator_originated_terminal_or_pause(state: Mapping[str, Any]) -> bool:
        status = str(state.get("status") or "").upper()
        if status == "PAUSED":
            return True
        if status != "STOPPED":
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("origin") == "operator"
            and item.get("action") == "stop"
            and item.get("status") in {"applied", "requested"}
            for item in reversed(state.get("controls") or [])
        )

    def create_or_gate_repair(
        self,
        request: RepairRequest | Mapping[str, Any],
        *,
        source_manifest_path: str | Path | None = None,
        source_event_key: str | None = None,
        require_affected_status: str | None = None,
    ) -> dict[str, Any]:
        repair_request = (
            request
            if isinstance(request, RepairRequest)
            else RepairRequest.from_dict(request)
        )
        affected_repository = Path(repair_request.repository).expanduser().resolve()
        repair_repository = Path(repair_request.repair_repository).expanduser().resolve()
        for repository, label in (
            (affected_repository, "affected task"),
            (repair_repository, "repair task"),
        ):
            if not any(
                repository.is_relative_to(root)
                for root in self.config.repository_allowed_roots
            ):
                raise ValueError(f"{label} repository is outside repositories.allowed_roots")
        if repair_repository != self.config.repository_root:
            raise ValueError("repair task repository must match the CDPA repository")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_repair_transaction_unlocked()
            self._recover_phase4_replacement_unlocked()
            catalog = self._load_catalog_unlocked(reconcile=False)
            catalog_before = read_shared_bytes(self.catalog_path) if self.catalog_path.exists() else None
            manifests, _errors = self._discover_with_catalog_unlocked(catalog)
            affected = next(
                (
                    item
                    for item in manifests
                    if item.get("task_id") == repair_request.affected_task_id
                    and item.get("team") == repair_request.affected_team
                ),
                None,
            )
            if affected is None:
                raise ValueError("affected repair task does not exist")

            if source_manifest_path is not None:
                expected_source_event = str(source_event_key or "").strip()
                if not expected_source_event:
                    raise ValueError("repair source_event_key is required with source_manifest_path")
                source_path = Path(source_manifest_path).expanduser().resolve()
                affected_path = Path(str(affected["manifest_path"])).expanduser().resolve()
                with ExitStack() as stack:
                    for manifest_path in sorted({source_path, affected_path}, key=str):
                        stack.enter_context(exclusive_file_lock(self._lock_path(manifest_path)))
                    source_current = self._load_current_manifest_unlocked(source_path)
                    affected_current = self._load_current_manifest_unlocked(affected_path)
                    self._assert_manifest_mutable_unlocked(source_path, source_current)
                    self._assert_manifest_mutable_unlocked(affected_path, affected_current)
                    if not is_independent_task(source_current):
                        raise ValueError("repair source is not an independent task")
                    independent = source_current.get("independent")
                    active = (
                        independent.get("active_event")
                        if isinstance(independent, Mapping)
                        else None
                    )
                    if not isinstance(active, Mapping):
                        raise ValueError("repair source has no active independent job")
                    if str(active.get("event_key") or "") != expected_source_event:
                        raise ValueError("bound independent repair source event is stale")
                    if str(active.get("trigger_type") or "").lower() not in {
                        "interval",
                        "check_all",
                    }:
                        raise ValueError("bound independent repair requires interval/check_all")
                    if str(active.get("target_task_id") or "") != repair_request.affected_task_id:
                        raise ValueError("bound independent repair target changed")
                    if active.get("target_hop_id") != repair_request.affected_hop_id:
                        raise ValueError("bound independent repair hop changed")
                    if require_affected_status is not None and str(
                        affected_current.get("status") or ""
                    ).upper() != str(require_affected_status).upper():
                        raise ValueError(
                            f"affected repair task must remain {str(require_affected_status).upper()}"
                        )
                    affected = affected_current

            if (
                Path(str(affected.get("repository") or "")).expanduser().resolve()
                != affected_repository
            ):
                raise ValueError("affected task repository does not match repair request")
            if str(affected.get("status") or "").upper() == "DONE":
                raise ValueError("cannot repair a DONE task")
            if self._operator_originated_terminal_or_pause(affected):
                raise ValueError("operator-originated Pause/Stop is authoritative")
            if any(
                isinstance(item, Mapping)
                and item.get("origin") == "operator"
                and item.get("status") == "requested"
                and item.get("action")
                in {"pause", "stop", "clear_team", "restart_role", "new_chat"}
                for item in affected.get("controls") or []
            ):
                raise ValueError("pending operator lifecycle control is authoritative")
            if affected.get("active_hop_id") != repair_request.affected_hop_id:
                raise ValueError("affected active hop changed before repair creation")
            active_hop = next(
                (
                    item
                    for item in affected.get("hops") or []
                    if isinstance(item, Mapping)
                    and item.get("hop_id") == affected.get("active_hop_id")
                ),
                None,
            )
            active_snapshot = command_snapshot(affected)
            active_request_id = active_snapshot["active_request_id"]
            if active_request_id != repair_request.affected_request_id:
                raise ValueError("affected active request changed before repair creation")
            if active_snapshot["active_role"] != repair_request.affected_role:
                raise ValueError("affected active role changed before repair creation")
            if (
                active_snapshot["conversation_id"]
                != repair_request.affected_conversation_id
            ):
                raise ValueError("affected conversation changed before repair creation")

            existing = next(
                (
                    item
                    for item in manifests
                    if isinstance(item.get("repair"), Mapping)
                    and item["repair"].get("root_cause_key")
                    == repair_request.root_cause_key
                    and Path(str(item.get("repository") or "")).expanduser().resolve()
                    == repair_repository
                ),
                None,
            )
            now = utc_now()
            operation_identity = "|".join(
                (
                    repair_request.root_cause_key,
                    repair_request.affected_task_id,
                    repair_request.incident_id,
                    repair_request.disposition,
                    str(repair_request.affected_hop_id),
                    str(repair_request.affected_request_id),
                    str(affected_repository),
                    str(repair_repository),
                )
            )
            operation_key = "repair-op-" + hashlib.sha256(
                operation_identity.encode("utf-8")
            ).hexdigest()[:24]
            operation = {
                "operation_key": operation_key,
                "at": now,
                "affected_task_id": repair_request.affected_task_id,
                "affected_team": repair_request.affected_team,
                "affected_repository": str(affected_repository),
                "repair_repository": str(repair_repository),
                "affected_hop_id": repair_request.affected_hop_id,
                "affected_request_id": repair_request.affected_request_id,
                "affected_role": repair_request.affected_role,
                "affected_conversation_id": repair_request.affected_conversation_id,
                "incident_id": repair_request.incident_id,
                "disposition": repair_request.disposition,
                "reason": repair_request.reason,
                "reproduction": repair_request.reproduction,
                "source_areas": list(repair_request.source_areas),
                "required_tests": list(repair_request.required_tests),
                "lesson": repair_request.lesson,
            }

            if existing is None:
                repair_id = (
                    f"cdpa-repair-{repair_request.root_cause_key.removeprefix('root-')}"
                )
                known_ids = {str(item.get("task_id") or "") for item in manifests}
                if repair_id in known_ids:
                    raise ValueError("repair task ID conflicts with unrelated work")
                base = f"cdpa-repair-{repair_request.root_cause_key[-12:]}"
                team, suffix = allocate_team(base, manifests)
                task_text = repair_request.task_text()
                repair_path = (
                    self.root / team / repair_id / f"{slugify(task_text)}.json"
                ).resolve()
                repair_state = self._initial_task_state(
                    text=task_text,
                    requested_team=base,
                    task_id=repair_id,
                    repository_path=repair_repository,
                    base=base,
                    team=team,
                    suffix=suffix,
                    reusable_teams=[],
                    target=repair_path,
                    normalized_new=(),
                    workflow_roles=ordered_system_routes(self.config),
                    new_all=False,
                    workflow_agents=self.workflow_agents.resolve(
                        ordered_system_routes(self.config)
                    ),
                    normalized_report_mode="file",
                    normalized_dependencies=(),
                    normalized_replaces=None,
                    normalized_incident=None,
                    normalized_attachments=(),
                    readiness=dependency_readiness(
                        {"task_id": repair_id, "depends_on_task_ids": []}, manifests
                    ),
                    queue_reuse=False,
                    queue_blocked_by=None,
                    now=now,
                )
                repair_state["priority"] = "urgent_repair"
                repair_state["repair"] = {
                    **repair_request.to_dict(),
                    "kind": "cdpa_repair",
                    "priority": "urgent",
                    "state": "ACTIVE",
                    "created_at": now,
                    "updated_at": now,
                    "affected_task_ids": [repair_request.affected_task_id],
                    "affected_operations": [operation],
                }
                repair_state.setdefault("dependency_events", []).append(
                    {
                        "at": now,
                        "status": "REPAIR_CREATED",
                        "message": (
                            f"Urgent repair created for {repair_request.affected_task_id}"
                        ),
                        "affected_task_id": repair_request.affected_task_id,
                        "affected_team": repair_request.affected_team,
                        "incident_id": repair_request.incident_id,
                        "operation_key": operation_key,
                        "root_cause_key": repair_request.root_cause_key,
                        "disposition": repair_request.disposition,
                    }
                )
            else:
                repair_state = json.loads(json.dumps(existing, ensure_ascii=False))
                repair_id = str(repair_state.get("task_id") or "")
                team = str(repair_state.get("team") or "")
                repair_path = Path(str(repair_state["manifest_path"])).expanduser().resolve()
                if repair_id == repair_request.affected_task_id:
                    raise ValueError("a repair task cannot recursively repair itself")
                metadata = repair_state.get("repair")
                if not isinstance(metadata, dict):
                    raise ValueError("existing repair task metadata is invalid")
                operations = [
                    dict(item)
                    for item in metadata.get("affected_operations") or []
                    if isinstance(item, Mapping)
                ]
                synthesized_legacy_operation = False
                if not operations and metadata.get("affected_task_id"):
                    synthesized_legacy_operation = True
                    operations.append(
                        {
                            "operation_key": metadata.get("operation_key"),
                            "at": metadata.get("created_at"),
                            "affected_task_id": metadata.get("affected_task_id"),
                            "affected_team": metadata.get("affected_team"),
                            "affected_hop_id": metadata.get("affected_hop_id"),
                            "affected_request_id": metadata.get("affected_request_id"),
                            "affected_role": metadata.get("affected_role"),
                            "affected_conversation_id": metadata.get(
                                "affected_conversation_id"
                            ),
                            "incident_id": metadata.get("incident_id"),
                            "disposition": metadata.get("disposition"),
                            "reason": metadata.get("reason"),
                            "reproduction": metadata.get("reproduction"),
                            "source_areas": list(metadata.get("source_areas") or []),
                            "required_tests": list(
                                metadata.get("required_tests") or []
                            ),
                            "lesson": metadata.get("lesson"),
                        }
                    )
                exact_operation = next(
                    (
                        item
                        for item in operations
                        if item.get("operation_key") == operation_key
                        or (
                            item.get("affected_task_id")
                            == repair_request.affected_task_id
                            and item.get("incident_id") == repair_request.incident_id
                            and item.get("disposition")
                            == repair_request.disposition
                            and item.get("affected_hop_id")
                            == repair_request.affected_hop_id
                            and (str(item.get("affected_request_id") or "") or None)
                            == repair_request.affected_request_id
                        )
                    ),
                    None,
                )
                affected_links = [
                    item
                    for item in affected.get("repair_links") or []
                    if isinstance(item, Mapping)
                ]
                exact_link = next(
                    (
                        item
                        for item in affected_links
                        if item.get("repair_task_id") == repair_id
                        and item.get("root_cause_key")
                        == repair_request.root_cause_key
                        and item.get("incident_id") == repair_request.incident_id
                        and item.get("disposition") == repair_request.disposition
                    ),
                    None,
                )
                dependencies = normalize_dependency_ids(
                    affected.get("depends_on_task_ids")
                )
                wait = affected.get("repair_wait")
                if repair_request.disposition == "HOLD_FOR_REPAIR":
                    relationship_complete = (
                        exact_link is not None
                        and str(affected.get("status") or "").upper() == "WAITING"
                        and repair_id in dependencies
                        and isinstance(wait, Mapping)
                        and wait.get("repair_task_id") == repair_id
                        and wait.get("root_cause_key")
                        == repair_request.root_cause_key
                        and wait.get("incident_id") == repair_request.incident_id
                        and wait.get("disposition") == "HOLD_FOR_REPAIR"
                        and wait.get("state") == "WAITING"
                        and wait.get("preserved_hop_id")
                        == repair_request.affected_hop_id
                        and (str(wait.get("preserved_request_id") or "") or None)
                        == repair_request.affected_request_id
                    )
                else:
                    relationship_complete = (
                        exact_link is not None
                        and repair_id not in dependencies
                        and not (
                            isinstance(wait, Mapping)
                            and wait.get("repair_task_id") == repair_id
                            and wait.get("state") == "WAITING"
                        )
                    )
                if exact_operation is not None and relationship_complete:
                    return {"repair": dict(repair_state), "affected": dict(affected)}
                if synthesized_legacy_operation:
                    metadata["affected_operations"] = operations
                    metadata["affected_task_ids"] = list(
                        dict.fromkeys(
                            str(item.get("affected_task_id") or "")
                            for item in operations
                            if str(item.get("affected_task_id") or "")
                        )
                    )
                    metadata["updated_at"] = now
                repair_status = str(repair_state.get("status") or "").upper()
                if repair_status == "DONE":
                    raise ValueError(
                        "repair root cause is already resolved; recurring evidence "
                        "requires a new root-cause identity"
                    )
                if repair_status == "STOPPED":
                    raise ValueError("existing repair task is STOPPED and cannot be reused")
                if exact_operation is None:
                    previous = next(
                        (
                            item
                            for item in reversed(operations)
                            if item.get("affected_task_id")
                            == repair_request.affected_task_id
                        ),
                        None,
                    )
                    if previous is not None:
                        operation["previous_disposition"] = previous.get("disposition")
                        operation["supersedes_operation_key"] = previous.get(
                            "operation_key"
                        )
                    operations.append(operation)
                    metadata["affected_operations"] = operations
                    metadata["affected_task_ids"] = list(
                        dict.fromkeys(
                            str(item.get("affected_task_id") or "")
                            for item in operations
                            if str(item.get("affected_task_id") or "")
                        )
                    )
                    metadata["updated_at"] = now
                    event_status = (
                        "REPAIR_DISPOSITION_CHANGED"
                        if previous is not None
                        and previous.get("disposition")
                        != repair_request.disposition
                        else "REPAIR_AFFECTED_ATTACHED"
                    )
                    repair_state.setdefault("dependency_events", []).append(
                        {
                            "at": now,
                            "status": event_status,
                            "message": (
                                f"Repair linked to {repair_request.affected_task_id} "
                                f"with {repair_request.disposition}"
                            ),
                            "affected_task_id": repair_request.affected_task_id,
                            "affected_team": repair_request.affected_team,
                            "incident_id": repair_request.incident_id,
                            "operation_key": operation_key,
                            "root_cause_key": repair_request.root_cause_key,
                            "disposition": repair_request.disposition,
                            "previous_disposition": (
                                previous.get("disposition")
                                if previous is not None
                                else None
                            ),
                        }
                    )

            affected_path = Path(str(affected["manifest_path"])).expanduser().resolve()
            affected_after = json.loads(json.dumps(affected, ensure_ascii=False))
            prior_links = [
                item
                for item in affected_after.get("repair_links") or []
                if isinstance(item, Mapping)
            ]
            exact_link = next(
                (
                    item
                    for item in prior_links
                    if item.get("repair_task_id") == repair_id
                    and item.get("root_cause_key") == repair_request.root_cause_key
                    and item.get("incident_id") == repair_request.incident_id
                    and item.get("disposition") == repair_request.disposition
                ),
                None,
            )
            previous_link = next(
                (
                    item
                    for item in reversed(prior_links)
                    if item.get("repair_task_id") == repair_id
                    and item.get("root_cause_key") == repair_request.root_cause_key
                ),
                None,
            )
            link = {
                "operation_key": operation_key,
                "at": now,
                "repair_task_id": repair_id,
                "repair_team": team,
                "root_cause_key": repair_request.root_cause_key,
                "affected_repository": str(affected_repository),
                "repair_repository": str(repair_repository),
                "affected_hop_id": repair_request.affected_hop_id,
                "affected_request_id": repair_request.affected_request_id,
                "incident_id": repair_request.incident_id,
                "disposition": repair_request.disposition,
                "blocker": repair_request.reason,
            }
            if previous_link is not None and previous_link.get("disposition") != (
                repair_request.disposition
            ):
                link["previous_disposition"] = previous_link.get("disposition")
                link["supersedes_operation_key"] = previous_link.get("operation_key")
            if exact_link is None:
                affected_after.setdefault("repair_links", []).append(link)
                affected_after.setdefault("route_timeline", []).append(
                    {"at": now, "kind": "repair_link", **link}
                )

            current_status = str(affected.get("status") or "").upper()
            current_wait = affected.get("repair_wait")
            if repair_request.disposition == "HOLD_FOR_REPAIR":
                already_held = (
                    current_status == "WAITING"
                    and isinstance(current_wait, Mapping)
                    and current_wait.get("repair_task_id") == repair_id
                    and current_wait.get("root_cause_key")
                    == repair_request.root_cause_key
                    and current_wait.get("state") == "WAITING"
                )
                if current_status != "BLOCKED" and not already_held:
                    raise ValueError(
                        "HOLD_FOR_REPAIR requires a non-operator BLOCKED affected task"
                    )
                if not isinstance(active_hop, Mapping):
                    raise ValueError(
                        "HOLD_FOR_REPAIR requires a preserved active hop on the affected task"
                    )
                dependencies = list(
                    normalize_dependency_ids(affected.get("depends_on_task_ids"))
                )
                if repair_id not in dependencies:
                    dependencies.append(repair_id)
                affected_after["depends_on_task_ids"] = dependencies
                if already_held:
                    wait_value = dict(current_wait)
                    wait_value.update(link)
                    wait_value["decision_changed_at"] = now
                    affected_after["repair_wait"] = wait_value
                else:
                    affected_after["repair_wait"] = {
                        **link,
                        "state": "WAITING",
                        "original_status": affected.get("status"),
                        "original_block_code": affected.get("block_code"),
                        "original_block_reason": affected.get("block_reason"),
                        "original_stop_reason": affected.get("stop_reason"),
                        "preserved_hop_id": affected.get("active_hop_id"),
                        "preserved_request_id": active_request_id,
                        "waiting_since": now,
                        "released_at": None,
                    }
                affected_after.update(
                    status="WAITING",
                    kanban_column="WAITING",
                    active_action="waiting_repair",
                    waiting_code="repair_dependency",
                    waiting_reason=f"Waiting for urgent repair {repair_id}",
                    waiting={
                        "reason": "dependency",
                        "waiting_on": [repair_id],
                        "stopped": [],
                        "missing": [],
                        "since": (
                            current_wait.get("waiting_since")
                            if already_held and isinstance(current_wait, Mapping)
                            else now
                        ),
                    },
                    block_code=None,
                    block_retryable=False,
                    block_reason=None,
                    stop_reason=None,
                )
                if exact_link is None or not already_held:
                    affected_after.setdefault("dependency_events", []).append(
                        {
                            "at": now,
                            "status": "REPAIR_WAIT",
                            "message": f"Held on urgent repair {repair_id}",
                            "repair_task_id": repair_id,
                            "waiting_on": [repair_id],
                            "stopped": [],
                            "missing": [],
                            "incident_id": repair_request.incident_id,
                            "operation_key": operation_key,
                            "root_cause_key": repair_request.root_cause_key,
                            "disposition": repair_request.disposition,
                        }
                    )
            elif (
                current_status == "WAITING"
                and isinstance(current_wait, Mapping)
                and current_wait.get("repair_task_id") == repair_id
                and current_wait.get("root_cause_key")
                == repair_request.root_cause_key
                and current_wait.get("state") == "WAITING"
            ):
                if current_wait.get("preserved_hop_id") != affected.get(
                    "active_hop_id"
                ) or (str(current_wait.get("preserved_request_id") or "") or None) != (
                    active_request_id
                ):
                    raise ValueError(
                        "repair disposition change found changed preserved hop/request"
                    )
                dependencies = [
                    item
                    for item in normalize_dependency_ids(
                        affected.get("depends_on_task_ids")
                    )
                    if item != repair_id
                ]
                affected_after["depends_on_task_ids"] = dependencies
                role = str(affected.get("active_role") or "PLAN").upper()
                affected_after.update(
                    status="RUNNING",
                    kanban_column=(
                        "PLANNING"
                        if role == "PLAN"
                        else "WORKING"
                        if role == "DEV"
                        else "VERIFYING"
                    ),
                    active_action=(
                        "observe_response"
                        if str(active_hop.get("state") or "") == "waiting"
                        else "resume_preserved_hop"
                    ),
                    waiting_code=None,
                    waiting_reason=None,
                    waiting={
                        "reason": None,
                        "waiting_on": [],
                        "stopped": [],
                        "missing": [],
                        "since": None,
                    },
                )
                released_wait = dict(current_wait)
                released_wait.update(link)
                released_wait["state"] = "DISPOSITION_CHANGED"
                released_wait["released_at"] = now
                released_wait["release_event"] = (
                    "Maintainers changed repair disposition to CONTINUE_IN_PARALLEL"
                )
                affected_after["repair_wait"] = released_wait
                affected_after.setdefault("dependency_events", []).append(
                    {
                        "at": now,
                        "status": "REPAIR_DISPOSITION_CHANGED",
                        "message": released_wait["release_event"],
                        "repair_task_id": repair_id,
                        "waiting_on": [],
                        "stopped": [],
                        "missing": [],
                        "incident_id": repair_request.incident_id,
                        "operation_key": operation_key,
                        "root_cause_key": repair_request.root_cause_key,
                        "disposition": repair_request.disposition,
                    }
                )
                affected_after.setdefault("route_timeline", []).append(
                    {
                        "at": now,
                        "kind": "repair_disposition_change",
                        "repair_task_id": repair_id,
                        "incident_id": repair_request.incident_id,
                        "operation_key": operation_key,
                        "disposition": repair_request.disposition,
                        "hop_id": affected.get("active_hop_id"),
                        "request_id": active_request_id,
                    }
                )

            writes = {repair_path: repair_state, affected_path: affected_after}
            for path, value in writes.items():
                value["schema_version"] = SCHEMA_VERSION
                value["updated_at"] = now
                error = self._manifest_value_error(
                    path, value, require_file=path.exists()
                )
                if error is not None:
                    raise ValueError(f"invalid repair transaction manifest {path}: {error}")
            proposed = []
            for item in manifests:
                if item.get("task_id") == affected_after.get("task_id"):
                    proposed.append(affected_after)
                elif item.get("task_id") == repair_state.get("task_id"):
                    proposed.append(repair_state)
                else:
                    proposed.append(item)
            if existing is None:
                proposed.append(repair_state)
            for item in proposed:
                validate_new_dependencies(
                    str(item["task_id"]),
                    normalize_dependency_ids(item.get("depends_on_task_ids")),
                    [other for other in proposed if other is not item],
                    allow_missing=True,
                )

            next_catalog = json.loads(json.dumps(catalog, ensure_ascii=False, default=str))
            next_catalog.setdefault("entries", {})
            for path, value in writes.items():
                next_catalog["entries"][self._catalog_key(path)] = self._catalog_entry(value)
            next_catalog["version"] = CATALOG_VERSION
            next_catalog["updated_at"] = now
            catalog_after = json.dumps(
                next_catalog, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            journal_writes = []
            for path in sorted(writes, key=str):
                before = read_shared_bytes(path) if path.exists() else None
                after = self._phase4_bytes(writes[path])
                journal_writes.append(
                    {
                        "kind": "repair" if path == repair_path else "affected",
                        "task_id": str(writes[path]["task_id"]),
                        "path": self._phase4_relative_path(path),
                        "before": self._phase4_encode_bytes(before),
                        "after": self._phase4_encode_bytes(after),
                        "after_sha256": hashlib.sha256(after).hexdigest(),
                    }
                )
            self._write_repair_journal_unlocked(
                {
                    "root_cause_key": repair_request.root_cause_key,
                    "repair_task_id": repair_id,
                    "affected_task_id": repair_request.affected_task_id,
                    "incident_id": repair_request.incident_id,
                    "operation_key": operation_key,
                    "disposition": repair_request.disposition,
                    "repair_manifest_path": self._phase4_relative_path(repair_path),
                    "affected_manifest_path": self._phase4_relative_path(affected_path),
                    "created_at": now,
                    "writes": journal_writes,
                    "catalog": {
                        "before": self._phase4_encode_bytes(catalog_before),
                        "after": self._phase4_encode_bytes(catalog_after),
                        "after_sha256": hashlib.sha256(catalog_after).hexdigest(),
                    },
                }
            )
            result = self._recover_repair_transaction_unlocked()
            if result is None:
                raise RuntimeError("repair transaction disappeared before verification")
            return result

    def replace_task_and_rewire(
        self,
        target_task_id: str,
        replacement_task: str,
        *,
        reuse_team: bool,
        rewire_children: bool,
        incident_id: str | None = None,
        source_task_id: str | None = None,
        source_event_key: str | None = None,
        corrected_task_text: str | None = None,
        corrected_goal: str | None = None,
        governing_objective: str | None = None,
        governing_directives: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        target_id = _validate_task_id(target_task_id)
        recovery_instruction = str(replacement_task).strip()
        if not recovery_instruction:
            raise ValueError("replacement task must not be empty")
        incident = _optional_nonempty_string(incident_id, "incident_id")
        if incident is None:
            raise ValueError("replacement incident_id is required")
        source_task = _optional_nonempty_string(source_task_id, "source_task_id")
        source_event = _optional_nonempty_string(source_event_key, "source_event_key")
        if (source_task is None) != (source_event is None):
            raise ValueError("replacement source provenance requires both source fields")
        if not isinstance(reuse_team, bool) or not isinstance(rewire_children, bool):
            raise ValueError("replacement flags must be booleans")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            recovered = self._recover_phase4_replacement_unlocked()
            if recovered is not None and source_task is None and (
                recovered["replacement"].get("replaces_task_id") == target_id
                and recovered["replacement"].get("replacement_incident_id") == incident
            ):
                return recovered
            catalog = self._load_catalog_unlocked(reconcile=False)
            catalog_before = (
                read_shared_bytes(self.catalog_path) if self.catalog_path.exists() else None
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
                    value = json.loads(read_shared_text(manifest_path, encoding="utf-8"))
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

                if source_task is not None:
                    source_state = next(
                        (
                            item
                            for item in manifests
                            if str(item.get("task_id") or "") == source_task
                        ),
                        None,
                    )
                    source_independent = (
                        source_state.get("independent")
                        if isinstance(source_state, Mapping)
                        else None
                    )
                    active_event = (
                        source_independent.get("active_event")
                        if isinstance(source_independent, Mapping)
                        else None
                    )
                    current_event_keys = (
                        {
                            str(event.get("event_key") or "")
                            for event in canonical_independent_events(
                                source_state, manifests
                            )
                        }
                        if isinstance(source_state, Mapping)
                        else set()
                    )
                    if (
                        not isinstance(source_state, Mapping)
                        or not is_independent_task(source_state)
                        or not isinstance(active_event, Mapping)
                        or str(active_event.get("event_key") or "") != source_event
                        or str(active_event.get("target_task_id") or "") != target_id
                        or source_event not in current_event_keys
                    ):
                        raise ValueError(
                            "replacement source event is stale or targets different work"
                        )

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
                durable_admission_integrity_incident = (
                    str(target_state.get("block_code") or "")
                    == "accepted_admission_integrity_corruption"
                    or any(
                        isinstance(item, Mapping)
                        and "accepted admission integrity corruption"
                        in str(item.get("error") or "").lower()
                        for item in target_state.get("errors") or []
                    )
                    or any(
                        "accepted admission integrity corruption" in str(error).lower()
                        for hop_record in target_state.get("hops") or []
                        if isinstance(hop_record, Mapping)
                        for error in hop_record.get("errors") or []
                    )
                )
                incident_correction = (
                    durable_admission_integrity_incident
                    or corrected_task_text is not None
                    or corrected_goal is not None
                )
                if incident_correction and target_status != "STOPPED":
                    raise ValueError(
                        "admission-integrity replacement requires a STOPPED target"
                    )
                if reuse_team and target_status != "STOPPED":
                    raise ValueError(
                        "reuse_team requires a STOPPED replacement target"
                    )

                workflow_mode = str(
                    target_state.get("workflow_mode") or "team"
                ).strip().lower()
                if incident_correction:
                    if corrected_task_text is None:
                        raise ValueError(
                            "admission-integrity replacement requires corrected_task_text"
                        )
                    if workflow_mode == "solo" and corrected_goal is None:
                        raise ValueError(
                            "admission-integrity replacement requires corrected_goal for Solo Task"
                        )
                    task_text = canonicalize_task_text(corrected_task_text)
                    if workflow_mode == "team":
                        semantic_payload, semantic_sha256 = normalize_governing_contract(
                            governing_objective,
                            governing_directives,
                        )
                    else:
                        semantic_payload = None
                        semantic_sha256 = None
                else:
                    task_text = replacement_continuation_text(
                        target_state, recovery_instruction
                    )
                    semantic_payload = (
                        str(target_state.get("governing_semantic_payload"))
                        if isinstance(target_state.get("governing_semantic_payload"), str)
                        else None
                    )
                    semantic_sha256 = (
                        str(target_state.get("governing_semantic_sha256"))
                        if isinstance(target_state.get("governing_semantic_sha256"), str)
                        else None
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
                repository_path = Path(target_state["repository"]).expanduser().resolve()
                report_mode = "file"
                workflow_agents = task_workflow_definitions(
                    target_state, self.config
                )
                replacement_goal = ""
                if workflow_mode == "solo":
                    replacement_goal = (
                        canonicalize_task_text(corrected_goal)
                        if corrected_goal is not None
                        else str(target_state.get("goal") or "")
                    )
                replacement_state = self._initial_task_state(
                    text=task_text,
                    requested_team=target_state.get("requested_team"),
                    task_id=replacement_id,
                    repository_path=repository_path,
                    base=base,
                    team=team,
                    suffix=suffix,
                    reusable_teams=reusable_teams,
                    target=replacement_path,
                    normalized_new=(),
                    workflow_roles=tuple(workflow_agents),
                    new_all=False,
                    workflow_agents=workflow_agents,
                    normalized_report_mode=report_mode,
                    normalized_dependencies=dependencies,
                    normalized_replaces=target_id,
                    normalized_incident=incident,
                    normalized_attachments=(),
                    readiness=readiness,
                    queue_reuse=False,
                    queue_blocked_by=None,
                    now=now,
                    normalized_bootstrap=(
                        target_state.get("bootstrap")
                        if isinstance(target_state.get("bootstrap"), Mapping)
                        else None
                    ),
                    browser_endpoint_id=self.endpoint_id_for(target_state),
                    required_browser_endpoint_id=(
                        str(target_state.get("required_browser_endpoint_id") or "").strip()
                        or None
                    ),
                    conversation_mode=str(target_state.get("conversation_mode") or "persistent"),
                    context_policy=(
                        str(target_state.get("context_policy"))
                        if target_state.get("context_policy") in {"fresh", "bootstrap"}
                        else None
                    ),
                    workflow_mode=workflow_mode,
                    goal=replacement_goal,
                )
                replacement_state["endpoint_pinned"] = True
                replacement_state["conversation_mode_explicit"] = bool(
                    target_state.get("conversation_mode_explicit", True)
                )
                replacement_state["task_text_sha256"] = hashlib.sha256(
                    task_text.encode("utf-8")
                ).hexdigest()
                if workflow_mode == "solo" and replacement_goal:
                    replacement_state["goal_sha256"] = hashlib.sha256(
                        replacement_goal.encode("utf-8")
                    ).hexdigest()
                if workflow_mode == "team" and semantic_payload is not None:
                    replacement_state["governing_semantic_payload"] = semantic_payload
                    replacement_state["governing_semantic_sha256"] = semantic_sha256
                if isinstance(target_state.get("admission"), Mapping):
                    replacement_state["admission"] = json.loads(
                        json.dumps(target_state["admission"], ensure_ascii=False)
                    )
                    replacement_state["admission"]["endpoint_pin_intent"] = True
                    replacement_state["admission"]["normalized_team"] = team
                    replacement_state["admission"]["normalized_browser_endpoint_id"] = (
                        self.endpoint_id_for(target_state)
                    )
                    replacement_state["admission"]["normalized_conversation_mode"] = (
                        replacement_state["conversation_mode"]
                    )
                    replacement_state["admission"]["normalized_context_policy"] = (
                        replacement_state.get("context_policy")
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
                target_entry = next_catalog["entries"].get(
                    self._catalog_key(target_state["manifest_path"])
                )
                if isinstance(target_entry, dict):
                    target_entry["replaced_by_task_id"] = replacement_id
                next_catalog["updated_at"] = utc_now()

                originals: dict[Path, bytes | None] = {
                    manifest_path: (
                        read_shared_bytes(manifest_path)
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
                        read_shared_bytes(Path(target_state["manifest_path"]))
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
                        atomic_replace(staged[manifest_path], manifest_path)
                        installed.append(manifest_path)
                        fsync_parent_directory(manifest_path)
                    atomic_replace(
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
        external_command_id: str | None = None,
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
                and item.get("status") in {"requested", "recovering"}
            ),
            None,
        )
        normalized_reason = str(reason or "").strip() or None
        normalized_command_id = (
            str(external_command_id).strip() if external_command_id else None
        )
        if pending is not None:
            if normalized_command_id:
                commands = pending.setdefault("external_commands", [])
                existing_id = str(pending.get("external_command_id") or "").strip()
                if existing_id and not any(
                    isinstance(item, Mapping) and item.get("command_id") == existing_id
                    for item in commands
                ):
                    commands.append(
                        {"command_id": existing_id, "reason": pending.get("reason")}
                    )
                if not any(
                    isinstance(item, Mapping)
                    and item.get("command_id") == normalized_command_id
                    for item in commands
                ):
                    commands.append(
                        {
                            "command_id": normalized_command_id,
                            "reason": normalized_reason,
                        }
                    )
            return state
        if not is_independent_task(state):
            eligibility = workflow_control_eligibility(state, "resume")
            if not eligibility["eligible"]:
                raise ValueError(str(eligibility["reason"] or "Resume is not eligible."))
        sequence = len(state.get("controls") or []) + 1
        state.setdefault("controls", []).append(
            {
                "control_id": sequence,
                "action": "resume",
                "role": str(state.get("active_role") or "PLAN").upper(),
                "reason": normalized_reason,
                "status": "requested",
                "requested_at": utc_now(),
                "applied_at": None,
                "result": None,
                "external_command_id": normalized_command_id,
                "external_commands": (
                    [
                        {
                            "command_id": normalized_command_id,
                            "reason": normalized_reason,
                        }
                    ]
                    if normalized_command_id
                    else []
                ),
            }
        )
        return state

    @staticmethod
    def _release_dependency_state(
        state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        dependencies = list(normalize_dependency_ids(state.get("depends_on_task_ids")))
        if not dependencies and not TaskStore._is_dependency_waiting_state(state):
            raise ValueError("task is not dependency-waiting")
        repair_wait = state.get("repair_wait")
        if (
            isinstance(repair_wait, dict)
            and repair_wait.get("state") == "WAITING"
            and str(repair_wait.get("repair_task_id") or "") in dependencies
        ):
            repair_task_id = str(repair_wait["repair_task_id"])
            preserved_hop_id = repair_wait.get("preserved_hop_id")
            preserved_request_id = str(repair_wait.get("preserved_request_id") or "") or None
            if state.get("active_hop_id") != preserved_hop_id:
                raise ValueError("repair-held task active hop changed before release")
            active_hop = next(
                (
                    item
                    for item in state.get("hops") or []
                    if isinstance(item, Mapping)
                    and item.get("hop_id") == preserved_hop_id
                ),
                None,
            )
            if not isinstance(active_hop, Mapping) or (
                str(active_hop.get("request_id") or "") or None
            ) != preserved_request_id:
                raise ValueError("repair-held task active request changed before release")
            state["depends_on_task_ids"] = [
                item for item in dependencies if item != repair_task_id
            ]
            role = str(state.get("active_role") or "PLAN").upper()
            state["status"] = "RUNNING"
            state["kanban_column"] = (
                "PLANNING" if role == "PLAN" else "WORKING" if role == "DEV" else "VERIFYING"
            )
            state["active_action"] = (
                "observe_response"
                if str(active_hop.get("state") or "") == "waiting"
                else "resume_preserved_hop"
            )
            state["waiting_reason"] = None
            state["waiting_code"] = None
            state["waiting"] = {
                "reason": None,
                "waiting_on": [],
                "stopped": [],
                "missing": [],
                "since": None,
            }
            repair_wait["state"] = "RELEASED"
            repair_wait["released_at"] = utc_now()
            repair_wait["release_event"] = reason
            state.setdefault("dependency_events", []).append(
                {
                    "at": repair_wait["released_at"],
                    "status": "REPAIR_RELEASED",
                    "message": reason,
                    "repair_task_id": repair_task_id,
                    "waiting_on": [],
                    "stopped": [],
                    "missing": [],
                    "preserved_hop_id": preserved_hop_id,
                    "preserved_request_id": preserved_request_id,
                }
            )
            state.setdefault("route_timeline", []).append(
                {
                    "at": repair_wait["released_at"],
                    "kind": "repair_release",
                    "repair_task_id": repair_task_id,
                    "hop_id": preserved_hop_id,
                    "request_id": preserved_request_id,
                    "hop_state": active_hop.get("state"),
                }
            )
            return state
        active_hop = next(
            (
                item
                for item in state.get("hops") or []
                if isinstance(item, Mapping)
                and item.get("hop_id") == state.get("active_hop_id")
            ),
            None,
        )
        hop_state = str(active_hop.get("state") or "") if active_hop else ""
        if hop_state == "pre_send":
            state["status"] = "INBOX"
            state["kanban_column"] = "INBOX"
            state["active_action"] = "queued"
        else:
            role = str(state.get("active_role") or "PLAN").upper()
            state["status"] = "RUNNING"
            state["kanban_column"] = (
                "PLANNING"
                if role == "PLAN"
                else "WORKING"
                if role == "DEV"
                else "VERIFYING"
            )
            state["active_action"] = (
                "observe_response"
                if hop_state in {"sent", "waiting"}
                else "process_response"
                if hop_state == "responded"
                else "resume_preserved_hop"
            )
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
                "preserved_hop_id": state.get("active_hop_id"),
                "preserved_request_id": (
                    active_hop.get("request_id") if active_hop else None
                ),
                "preserved_hop_state": hop_state or None,
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

    def remove_parent_dependency(
        self,
        path: str | Path,
        parent_task_id: str,
        *,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        parent_id = _validate_task_id(parent_task_id)
        command_id = str(external_command_id or "").strip() or None
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.allocation_lock):
            self._recover_phase4_replacement_unlocked()
            catalog = self._load_catalog_unlocked(reconcile=False)
            with exclusive_file_lock(self._lock_path(target)):
                current = self._load_current_manifest_unlocked(target)
                self._assert_manifest_mutable_unlocked(target, current)
                if command_id in current.get("applied_command_ids", []):
                    return current
                if is_independent_task(current):
                    raise ValueError("independent tasks do not have workflow parents")
                if str(current.get("status") or "").upper() in TERMINAL:
                    raise ValueError("cannot remove a parent from a terminal task")
                parents = list(
                    normalize_dependency_ids(current.get("depends_on_task_ids"))
                )
                if parent_id not in parents:
                    raise ValueError(
                        f"task {current.get('task_id')!r} does not depend on {parent_id!r}"
                    )
                remaining = [item for item in parents if item != parent_id]
                now = utc_now()
                next_state = json.loads(
                    json.dumps(current, ensure_ascii=False, default=str)
                )
                next_state["depends_on_task_ids"] = remaining
                event = {
                    "at": now,
                    "status": "PARENT_REMOVED",
                    "message": f"Operator removed parent dependency {parent_id}",
                    "parent_task_id": parent_id,
                    "remaining_parent_task_ids": remaining,
                    "external_command_id": command_id,
                }
                next_state.setdefault("dependency_events", []).append(event)
                next_state.setdefault("route_timeline", []).append(
                    {"kind": "parent_removed", **event}
                )
                self._record_external_command(next_state, command_id)
                saved = self._save_unlocked(target, next_state)
            catalog["entries"][self._catalog_key(target)] = self._catalog_entry(saved)
            self._write_catalog_unlocked(catalog)

        tasks, _errors = self.discover_with_errors()
        reconciled, _changed = self.refresh_scheduling(
            target,
            tasks=tasks,
            state=saved,
        )
        return reconciled

    def request_resume(
        self,
        path: str | Path,
        *,
        reason: str | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if external_command_id in state.get("applied_command_ids", []):
                return state
            self._queue_resume(
                state, reason=reason, external_command_id=external_command_id
            )
            self._record_external_command(state, external_command_id)
            return state

        return self.update(path, mutate)

    def resume_team(
        self,
        exact_team: str,
        *,
        reason: str | None = None,
        external_command_id: str | None = None,
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
            include_unreadable = not bool(catalog.get("entries"))

            for path in self._filesystem_manifest_like_paths_for_team(
                team,
                include_unreadable=include_unreadable,
            ):
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
                        state = json.loads(read_shared_text(target, encoding="utf-8"))
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

            exact_states = [state for _path, state, _record in loaded]
            if catalog.get("entries"):
                dependency_ids = {
                    dependency_id
                    for state in exact_states
                    for dependency_id in normalize_dependency_ids(
                        state.get("depends_on_task_ids")
                    )
                }
                all_tasks = list(exact_states)
                exact_task_ids = {
                    str(state.get("task_id") or "") for state in exact_states
                }
                for dependency_id in sorted(dependency_ids - exact_task_ids):
                    matches = [
                        (key, entry)
                        for key, entry in catalog["entries"].items()
                        if isinstance(entry, Mapping)
                        and str(entry.get("task_id") or "") == dependency_id
                    ]
                    if not matches:
                        continue
                    if len(matches) != 1:
                        raise ValueError(
                            f"dependency task_id is ambiguous in catalog: {dependency_id}"
                        )
                    key, entry = matches[0]
                    dependency_path = Path(
                        str(entry.get("manifest_path") or "")
                    ).expanduser().resolve()
                    if not dependency_path.exists():
                        raise ValueError(
                            f"dependency catalog manifest is missing: {dependency_id}"
                        )
                    with exclusive_file_lock(self._lock_path(dependency_path)):
                        try:
                            dependency_state = json.loads(
                                read_shared_text(dependency_path, encoding="utf-8")
                            )
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ValueError(
                                f"dependency manifest is unreadable: {dependency_id}: "
                                f"{type(exc).__name__}: {exc}"
                            ) from exc
                    if not isinstance(dependency_state, Mapping):
                        raise ValueError(
                            f"dependency manifest root must be an object: {dependency_id}"
                        )
                    error = self._manifest_value_error(
                        dependency_path,
                        dependency_state,
                        catalog_key=str(key),
                        catalog_entry=entry,
                    )
                    if error is not None:
                        raise ValueError(
                            f"dependency manifest is corrupt: {dependency_id}: {error}"
                        )
                    all_tasks.append(dict(dependency_state))
            else:
                # Legacy fallback only: repositories without a compact catalog must
                # discover once so exact-team dependency readiness remains fail-closed.
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
                    current = json.loads(read_shared_text(target, encoding="utf-8"))
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
                if external_command_id in queued.get("applied_command_ids", []):
                    return dict(queued)
                self._queue_resume(
                    queued,
                    reason=reason or "exact-team resume",
                    external_command_id=external_command_id,
                )
                self._record_external_command(queued, external_command_id)
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
        origin: str | None = None,
        source_task_id: str | None = None,
        source_event_key: str | None = None,
        external_command_id: str | None = None,
    ) -> Mapping[str, Any]:
        action = str(action).strip().lower()
        control_role = role
        if action == "resume" and control_role is None:
            control_role = str(state.get("active_role") or "PLAN").upper()
        source_task = str(source_task_id or "").strip() or None
        source_event = str(source_event_key or "").strip() or None
        if (source_task is None) != (source_event is None):
            raise ValueError(
                "independent-agent control provenance requires both source fields"
            )
        normalized_origin = str(origin or "operator").strip().lower()
        normalized_reason = str(reason or "").strip() or None
        if not is_independent_task(state):
            if action == "safe_restore":
                if normalized_origin != "independent_agent" or source_task is None:
                    raise ValueError(
                        "safe_restore is available only to an independent-agent target control"
                    )
            else:
                eligibility = workflow_control_eligibility(
                    state,
                    action,
                    role=control_role,
                )
                if not eligibility["eligible"]:
                    raise ValueError(str(eligibility["reason"] or f"{action} is not eligible."))
        command_reason = normalized_reason or f"{action} requested"
        command = WorkerCommand.create(
            origin=normalized_origin,
            action=action,
            reason=command_reason,
            state=state,
            role=control_role,
            source_task_id=source_task,
            source_event_key=source_event,
        )
        if source_task is not None:
            existing = next(
                (
                    item
                    for item in state.get("controls") or []
                    if isinstance(item, Mapping)
                    and item.get("source_task_id") == source_task
                    and item.get("source_event_key") == source_event
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.get("action") != action
                    or existing.get("role") != control_role
                    or existing.get("reason") != normalized_reason
                    or existing.get("origin") not in {None, normalized_origin}
                ):
                    raise ValueError(
                        "independent-agent control provenance already belongs to another payload"
                    )
                if existing.get("command") is None:
                    existing["origin"] = normalized_origin
                    existing["command"] = command.to_dict()
                    existing["command_state"] = "PENDING"
                return existing
        sequence = len(state.get("controls") or []) + 1
        control = {
            "control_id": sequence,
            "action": action,
            "role": control_role,
            "reason": normalized_reason,
            "confirmed": bool(confirmed),
            "origin": normalized_origin,
            "command": command.to_dict(),
            "command_state": "PENDING",
            "status": "requested",
            "requested_at": utc_now(),
            "applied_at": None,
            "result": None,
            "external_command_id": str(external_command_id).strip()
            if external_command_id
            else None,
        }
        if source_task is not None:
            control["source_task_id"] = source_task
            control["source_event_key"] = source_event
        state.setdefault("controls", []).append(control)
        return control

    def change_goal(
        self,
        path: str | Path,
        goal: str,
        *,
        governing_objective: str | None = None,
        governing_directives: Sequence[str] | None = None,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        goal = canonicalize_task_text(goal)
        semantic_payload, semantic_sha256 = normalize_governing_contract(
            governing_objective,
            governing_directives,
        )
        command_id = str(external_command_id or "").strip()

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if is_independent_task(state):
                raise ValueError("Change goal is available only for workflow tasks")
            if str(state.get("status") or "").upper() != "RUNNING":
                raise ValueError("Change goal is available only while the task is RUNNING")
            active_hop_id = state.get("active_hop_id")
            if (
                isinstance(active_hop_id, bool)
                or not isinstance(active_hop_id, int)
                or active_hop_id < 1
            ):
                raise ValueError("RUNNING task has no active hop")
            revisions = state.get("goal_revisions") or []
            if command_id and command_id in state.get("applied_command_ids", []):
                matching = [
                    item for item in revisions
                    if isinstance(item, Mapping)
                    and item.get("external_command_id") == command_id
                ]
                if (
                    len(matching) != 1
                    or matching[0].get("goal") != goal
                    or matching[0].get("governing_semantic_sha256") != semantic_sha256
                ):
                    raise ValueError("change-goal command provenance does not match its payload")
                return state
            if effective_task_goal(state) == goal:
                raise ValueError("replacement goal is unchanged")
            if not command_id:
                raise ValueError("external_command_id is required for a goal change")
            revision = {
                "revision": len(revisions) + 1,
                "changed_at": utc_now(),
                "applies_from_hop_id": active_hop_id + 1,
                "goal": goal,
                "governing_semantic_payload": semantic_payload,
                "governing_semantic_sha256": semantic_sha256,
                "external_command_id": command_id,
            }
            state["effective_goal"] = goal
            state.setdefault("goal_revisions", []).append(revision)
            self._record_external_command(state, command_id)
            return state

        return self.update(path, mutate)

    def change_role_url(
        self,
        path: str | Path,
        *,
        role: str,
        url: str,
        expected_browser_endpoint_id: str,
        external_command_id: str,
    ) -> dict[str, Any]:
        """Change one persistent role URL without moving its endpoint or replaying a Send.

        A role that owns an existing page can only change the URL spelling for
        the same conversation. Moving that role to a different conversation
        requires its old binding to be safely released first.
        """
        role = validate_workflow_route_key(str(role or "").strip().upper())
        normalized = normalize_saved_chatgpt_conversation_url(url)
        if normalized is None:
            raise ValueError("Change URL requires an exact ChatGPT /c/<UUID> HTTPS URL")
        target_id = conversation_identity(normalized)
        endpoint_id = str(expected_browser_endpoint_id or "").strip()
        command_id = str(external_command_id or "").strip()
        if not endpoint_id or not command_id:
            raise ValueError("Change URL requires expected_browser_endpoint_id and external_command_id")
        target = Path(path).expanduser().resolve()

        # Same-worker task creation and command dispatch share this canonical
        # store; check other live reservations before the per-task atomic update.
        for other in self.discover():
            if str(other.get("status") or "").upper() in TERMINAL:
                continue
            for other_role, record in (other.get("roles") or {}).items():
                if (
                    str(other.get("manifest_path") or "") == str(target)
                    and str(other_role).upper() == role
                ):
                    continue
                if not isinstance(record, Mapping):
                    continue
                other_id = conversation_identity(record.get("page_url"))
                if other_id is None:
                    active = next(
                        (
                            hop for hop in other.get("hops") or ()
                            if isinstance(hop, Mapping)
                            and hop.get("hop_id") == other.get("active_hop_id")
                            and str(hop.get("target_role") or "").upper() == str(other_role).upper()
                        ),
                        None,
                    )
                    if active is not None and str(active.get("state") or "") in {
                        "sending", "sent", "waiting", "responded",
                    }:
                        other_id = conversation_identity(active.get("conversation_url"))
                        receipt = active.get("receipt") if isinstance(active.get("receipt"), Mapping) else {}
                        if other_id is None and receipt.get("conversation_id"):
                            other_id = conversation_identity(
                                f"https://chatgpt.com/c/{receipt['conversation_id']}"
                            )
                if other_id == target_id:
                    raise ValueError(
                        f"conversation already reserved by {other.get('task_id')}/{other_role}"
                    )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            revisions = state.get("role_url_revisions") or []
            if command_id in state.get("applied_command_ids", []):
                records = [
                    item for item in revisions
                    if isinstance(item, Mapping)
                    and item.get("external_command_id") == command_id
                ]
                if (
                    len(records) != 1
                    or records[0].get("role") != role
                    or records[0].get("url") != normalized
                    or records[0].get("browser_endpoint_id") != endpoint_id
                ):
                    raise ValueError("Change URL replay provenance does not match its payload")
                return state
            if is_independent_task(state) or str(state.get("status") or "").upper() in TERMINAL:
                raise ValueError("Change URL requires a nonterminal Solo/Team task")
            if state.get("conversation_mode") == "temporary":
                raise ValueError("Change URL is unavailable for Temporary chats")
            record = (state.get("roles") or {}).get(role)
            if not isinstance(record, dict):
                raise ValueError(f"role {role!r} does not belong to this task")
            pinned = str(record.get("browser_endpoint_id") or state.get("browser_endpoint_id") or "").strip()
            if pinned != endpoint_id:
                raise ValueError(f"role endpoint mismatch: expected {pinned}, got {endpoint_id}")
            if state.get("endpoint_pinned") is False:
                raise ValueError("role endpoint must be pinned before Change URL")
            current_url = str(record.get("page_url") or "").strip()
            if current_url == normalized:
                raise ValueError("role already has this URL")
            current_id = conversation_identity(current_url)
            history_ids: set[str] = set()
            for hop in state.get("hops") or ():
                if not isinstance(hop, Mapping) or str(hop.get("target_role") or "").upper() != role:
                    continue
                receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else {}
                receipt_id = str(receipt.get("conversation_id") or "").strip()
                identity = conversation_identity(hop.get("conversation_url"))
                if identity is None and receipt_id:
                    identity = conversation_identity(f"https://chatgpt.com/c/{receipt_id}")
                if identity is not None:
                    history_ids.add(identity)
            known_id = current_id or (next(iter(history_ids)) if len(history_ids) == 1 else None)
            active_hop = next(
                (
                    hop for hop in state.get("hops") or ()
                    if isinstance(hop, dict)
                    and hop.get("hop_id") == state.get("active_hop_id")
                    and str(hop.get("target_role") or "").upper() == role
                ),
                None,
            )
            active_state = str((active_hop or {}).get("state") or "")
            # A durable Send boundary is never transferable to a different chat.
            active_receipt = (
                active_hop.get("receipt")
                if isinstance((active_hop or {}).get("receipt"), Mapping)
                else {}
            )
            active_id = conversation_identity((active_hop or {}).get("conversation_url"))
            if active_receipt.get("conversation_id"):
                active_id = conversation_identity(
                    f"https://chatgpt.com/c/{active_receipt['conversation_id']}"
                )
            if (
                active_state in {"sending", "sent", "waiting", "responded"}
                and (active_id or known_id) != target_id
            ):
                raise ValueError("active request is in flight; cannot change its conversation identity")
            if record.get("page_id") and known_id != target_id:
                raise ValueError(
                    "role still has a recorded page binding; cannot replace it with a different conversation"
                )
            for other_role, other in state["roles"].items():
                if other_role != role and conversation_identity(other.get("page_url")) == target_id:
                    raise ValueError(f"conversation is already assigned to role {other_role}")
            record["page_url"] = normalized
            if (
                active_hop is not None and active_state == "pre_send"
                and active_hop.get("receipt") is None
                and active_hop.get("message_identity") is None
            ):
                active_hop["conversation_url"] = normalized
            state.setdefault("role_url_revisions", []).append({
                "role": role,
                "url": normalized,
                "previous_url": current_url or None,
                "browser_endpoint_id": pinned,
                "changed_at": utc_now(),
                "external_command_id": command_id,
            })
            self._record_external_command(state, command_id)
            return state

        return self.update(target, mutate)

    def request_control(
        self,
        path: str | Path,
        action: str,
        *,
        role: str | None = None,
        reason: str | None = None,
        confirmed: bool = False,
        external_command_id: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"pause", "resume", "retry", "stop", "reset", "restart_role", "open_tab", "close_tab", "new_chat", "route_plan", "release_required_browser_endpoint", "clear_team", "archive_worktree"}
        action = str(action).strip().lower()
        if action not in allowed:
            raise ValueError(f"unsupported control action {action!r}")
        if role is not None:
            role = str(role).strip().upper()
            if role != INDEPENDENT_ROLE:
                role = validate_workflow_route_key(role)
        if action == "resume":
            return self.request_resume(
                path,
                reason=reason or "resume requested",
                external_command_id=external_command_id,
            )

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if is_independent_task(state) and action == "release_required_browser_endpoint":
                raise ValueError(
                    "Required browser endpoint release is available only for workflow tasks"
                )
            if is_independent_task(state) and action == "stop":
                raise ValueError("independent agents use Reset; Stop is not supported")
            if not is_independent_task(state) and action == "reset":
                raise ValueError("Reset is available only for independent agents")
            if role is not None:
                expected_roles = (
                    {INDEPENDENT_ROLE}
                    if is_independent_task(state)
                    else set(state.get("roles", {}))
                )
                if role not in expected_roles:
                    raise ValueError(f"unsupported control role {role!r}")
            if external_command_id in state.get("applied_command_ids", []):
                return state
            self._queue_control(
                state,
                action,
                role=role,
                reason=reason,
                confirmed=confirmed,
                origin="operator",
                external_command_id=external_command_id,
            )
            self._record_external_command(state, external_command_id)
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
        reason = sanitize_text(reason, max_chars=2000).strip()
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
