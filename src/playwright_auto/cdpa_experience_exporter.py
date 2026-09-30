from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from .cdpa_config import CDPA_ROLES
from .cdpa_evolution_lab import canonical_bytes, make_document
from .cdpa_identity import validate_task_id
from .cdpa_routes import RouteContractError, validate_file_report
from .durable import DurableRequestError, RequestLedger
from .cdpa_store_common import TERMINAL, slugify
from .cdpa_team import physical_role
from .cdpa_workflow_agents import normalize_workflow_definition, workflow_closing_role

_SCHEMA_VERSION = 1
_SPLITS = {"train", "heldout", "unassigned"}
_TERMINAL_EVIDENCE_STATES = {"DONE", "BLOCKED", "PAUSED", "STOPPED", "CLEARED"}
_TASK_STATUSES = {"INBOX", "WAITING", "RUNNING", "PAUSED", "BLOCKED", "DONE", "STOPPED"}
_HOP_STATES = {"waiting_trigger", "pre_send", "sending", "sent", "waiting", "responded", "routed", "abandoned"}
_REQUIRED_MANIFEST_STRINGS = (
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
_REQUIRED_MANIFEST_TYPES = {
    "roles": Mapping,
    "hops": list,
    "reports": list,
    "controls": list,
    "cleanup": Mapping,
    "route_timeline": list,
    "errors": list,
    "options": Mapping,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path, repository: Path) -> str:
    try:
        return path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _report_path(repository: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else repository / path


def _valid_role_hop_identity(path: Path, state: Mapping[str, Any], mode: str) -> bool:
    roles = state["roles"]
    if not roles:
        return False
    role_keys = list(roles)
    if mode == "independent":
        if role_keys != ["AGENT"]:
            return False
    else:
        snapshot = state.get("workflow_agents")
        if snapshot is None:
            if any(role not in CDPA_ROLES for role in role_keys):
                return False
        elif not isinstance(snapshot, Mapping) or list(snapshot) != role_keys:
            return False
        else:
            try:
                for role, raw in snapshot.items():
                    definition = normalize_workflow_definition(raw, expected_route_key=role)
                    if definition["deleted_at"] is not None or definition["is_system"] != (role in CDPA_ROLES):
                        return False
            except ValueError:
                return False
        workflow_mode = str(state.get("workflow_mode") or "team").strip().lower()
        if workflow_mode not in {"team", "solo"}:
            return False
        if workflow_mode == "team" and workflow_closing_role(roles) is None:
            return False
        if workflow_mode == "solo" and len(roles) != 1:
            return False

    team_base = str(state["team_base"])
    suffix = int(state["team_suffix"])
    for logical, record in roles.items():
        if not isinstance(record, Mapping):
            return False
        try:
            expected_physical = physical_role(str(logical), team_base, suffix)
        except ValueError:
            return False
        allowed_physical = {expected_physical}
        if mode == "workflow":
            allowed_physical.add(f"{logical}{'' if suffix == 1 else suffix}")
        turn = record.get("turn")
        if (
            str(record.get("logical_role") or "").upper() != str(logical).upper()
            or str(record.get("physical_role") or "") not in allowed_physical
            or isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 0
            or not isinstance(record.get("status"), str)
            or not isinstance(record.get("online"), bool)
        ):
            return False

    task_id = str(state["task_id"])
    ledger_path = (path.parent / "requests.json").resolve()
    hop_by_id: dict[int, Mapping[str, Any]] = {}
    for hop in state["hops"]:
        if not isinstance(hop, Mapping):
            return False
        hop_id = hop.get("hop_id")
        if isinstance(hop_id, bool) or not isinstance(hop_id, int) or hop_id < 1 or hop_id in hop_by_id:
            return False
        target_role = str(hop.get("target_role") or "").upper()
        source_role = hop.get("source_role")
        turn = hop.get("turn")
        kind = str(hop.get("kind") or "")
        try:
            declared_ledger = Path(str(hop.get("ledger_path") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        if (
            target_role not in roles
            or (source_role is not None and str(source_role).upper() not in roles)
            or str(hop.get("physical_role") or "") != str(roles[target_role]["physical_role"])
            or isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 1
            or str(hop.get("state") or "") not in _HOP_STATES
            or (mode == "independent" and kind not in {"independent_job", "independent_cycle"})
            or (mode == "workflow" and kind in {"independent_job", "independent_cycle"})
            or str(hop.get("request_id") or "") != f"{task_id}-hop{hop_id}"
            or not isinstance(hop.get("handoff"), str)
            or not str(hop.get("handoff") or "").strip()
            or declared_ledger != ledger_path
            or not isinstance(hop.get("wait"), Mapping)
            or not isinstance(hop.get("timestamps"), Mapping)
            or not isinstance(hop.get("errors"), list)
        ):
            return False
        hop_by_id[hop_id] = hop
    if not hop_by_id:
        return False

    status = str(state["status"]).upper()
    active_role = state.get("active_role")
    active_hop_id = state.get("active_hop_id")
    if status in TERMINAL:
        return active_role is None and active_hop_id is None
    if str(active_role or "").upper() not in roles or isinstance(active_hop_id, bool) or not isinstance(active_hop_id, int):
        return False
    active_hop = hop_by_id.get(active_hop_id)
    return active_hop is not None and str(active_hop.get("target_role") or "").upper() == str(active_role).upper()


def _canonical_primary_manifest(plan: Path, path: Path, state: Any) -> bool:
    if not isinstance(state, Mapping) or state.get("schema_version") != _SCHEMA_VERSION:
        return False
    try:
        relative = path.resolve().relative_to(plan.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    if len(relative.parts) != 3 or path.name == "requests.json" or not path.name.endswith(".json"):
        return False
    team, task_id, filename = relative.parts
    if any(not isinstance(state.get(key), str) or not str(state.get(key)).strip() for key in _REQUIRED_MANIFEST_STRINGS):
        return False
    if any(not isinstance(state.get(key), expected) for key, expected in _REQUIRED_MANIFEST_TYPES.items()):
        return False
    try:
        if Path(str(state["manifest_path"])).expanduser().resolve() != path.resolve():
            return False
        validate_task_id(task_id)
    except (OSError, RuntimeError, ValueError):
        return False
    if state.get("team") != team or state.get("task_id") != task_id:
        return False
    task_slug = str(state["task_slug"])
    if task_slug != slugify(str(state["task_text"])) or filename != f"{task_slug}.json":
        return False
    suffix = state.get("team_suffix")
    if isinstance(suffix, bool) or not isinstance(suffix, int) or suffix < 1:
        return False
    expected_team = str(state["team_base"]) if suffix == 1 else f"{state['team_base']}{suffix}"
    if team != expected_team or str(state["status"]).upper() not in _TASK_STATUSES:
        return False
    mode = str(state.get("task_mode") or "workflow").strip().lower()
    if mode not in {"workflow", "independent"}:
        return False
    return _valid_role_hop_identity(path, state, mode)


def _primary_manifests(repository: Path) -> Iterable[Path]:
    plan = repository / ".plan"
    if not plan.is_dir():
        return []
    candidates: list[Path] = []
    for task_dir in sorted((p for p in plan.glob("*/*") if p.is_dir()), key=lambda p: p.as_posix()):
        valid: list[Path] = []
        for path in sorted(task_dir.glob("*.json"), key=lambda p: p.as_posix()):
            if path.name == "requests.json" or path.name.endswith(".lock"):
                continue
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if _canonical_primary_manifest(plan, path, state):
                valid.append(path)
        if len(valid) == 1:
            candidates.append(valid[0])
    return candidates


def _role(hop: Mapping[str, Any]) -> str | None:
    role = hop.get("target_role") or hop.get("role")
    if isinstance(role, str):
        return role.upper()
    physical = hop.get("physical_role")
    if isinstance(physical, str):
        for candidate in ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT"):
            if physical.lower().endswith(f"-{candidate.lower()}"):
                return candidate
    return None


def _source_identity(
    manifest: Mapping[str, Any],
    task_id: str,
    source_identities: Mapping[str, Mapping[str, Any]] | None,
    baseline_known_failures: Mapping[str, list[str]] | None,
) -> dict[str, Any]:
    supplied = dict((source_identities or {}).get(task_id, {}))
    if not supplied:
        for key in ("source_identity", "git_identity", "git"):
            value = manifest.get(key)
            if isinstance(value, Mapping):
                supplied = dict(value)
                break
    result = {
        "repository": manifest.get("repository") or manifest.get("workspace"),
        "requested_baseline": supplied.get("requested_baseline"),
        "observed_head": supplied.get("observed_head"),
        "dirty_state": supplied.get("dirty_state", "unknown"),
        "pre_state_sha256": supplied.get("pre_state_sha256"),
        "oracle_sha256": supplied.get("oracle_sha256"),
    }
    result["pre_state_available"] = bool(result["pre_state_sha256"])
    exact_baseline = (
        result["requested_baseline"]
        if result["requested_baseline"] and result["requested_baseline"] == result["observed_head"]
        else None
    )
    result["known_failures"] = list((baseline_known_failures or {}).get(exact_baseline, [])) if exact_baseline else []
    return result


def _split(task_id: str, split_manifest: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    value = dict((split_manifest or {}).get(task_id, {}))
    split = value.get("split", "unassigned")
    if split not in _SPLITS:
        raise ValueError(f"invalid split for {task_id}: {split}")
    return {"family_id": value.get("family_id"), "split": split}


def _text_integrity_failure(hop: Mapping[str, Any], kind: str, role: str) -> dict[str, Any] | None:
    expected = hop.get(f"{kind}_sha256")
    if not isinstance(expected, str) or not expected:
        return None
    value = hop.get(kind)
    observed = hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None
    if observed == expected:
        return None
    return {
        "kind": kind,
        "role": role,
        "hop_id": hop.get("hop_id"),
        "expected_sha256": expected,
        "observed_sha256": observed,
    }


def _report_snapshot(
    repository: Path,
    hop: Mapping[str, Any],
    role: str,
    *,
    team: str,
    task_id: str,
) -> tuple[Path, str, int, dict[str, Any] | None]:
    evidence = validate_file_report(
        str(hop.get("report_path") or ""),
        repository_root=repository,
        plans_root=repository / ".plan",
        team=team,
        physical_role=str(hop.get("physical_role") or ""),
        turn=int(hop.get("turn")),
        task_id=task_id,
    )
    path = _report_path(repository, evidence.path)
    if path is None:
        raise RouteContractError("report path is missing")
    observed_hash = evidence.sha256
    observed_size = evidence.size
    expected_hash = hop.get("report_sha256")
    expected_size = hop.get("report_size")
    hash_mismatch = isinstance(expected_hash, str) and bool(expected_hash) and expected_hash != observed_hash
    size_mismatch = type(expected_size) is int and expected_size != observed_size
    failure = None
    if hash_mismatch or size_mismatch:
        failure = {
            "kind": "report",
            "role": role,
            "hop_id": hop.get("hop_id"),
            "path": _relative(path, repository),
            "expected_sha256": expected_hash,
            "observed_sha256": observed_hash,
            "expected_size": expected_size,
            "observed_size": observed_size,
        }
    return path, observed_hash, observed_size, failure


def _downstream(
    repository: Path,
    hops: list[Mapping[str, Any]],
    index: int,
    *,
    team: str,
    task_id: str,
) -> tuple[list[dict[str, Any]], list[tuple[Path, str]], list[dict[str, Any]]]:
    reports: list[dict[str, Any]] = []
    sources: list[tuple[Path, str]] = []
    failures: list[dict[str, Any]] = []
    for hop in hops[index + 1 :]:
        role = _role(hop)
        if role == "DEV":
            break
        if role not in {"TEST", "REVIEW", "PLAN"}:
            continue
        declared_report = hop.get("report_path")
        if not isinstance(declared_report, str) or not declared_report.strip():
            continue
        try:
            snapshot = _report_snapshot(
                repository, hop, role, team=team, task_id=task_id
            )
        except RouteContractError as exc:
            failures.append({
                "kind": "report_path",
                "role": role,
                "hop_id": hop.get("hop_id"),
                "path": hop.get("report_path"),
                "reason": str(exc),
            })
            continue
        path, observed_hash, _observed_size, report_failure = snapshot
        sources.append((path, observed_hash))
        response_failure = _text_integrity_failure(hop, "response", role)
        if report_failure is not None:
            failures.append(report_failure)
        if response_failure is not None:
            failures.append(response_failure)
        if report_failure is not None or response_failure is not None:
            continue
        reports.append({
            "role": role,
            "turn": hop.get("turn"),
            "hop_id": hop.get("hop_id"),
            "route": hop.get("route"),
            "report_path": _relative(path, repository),
            "report_sha256": hop.get("report_sha256") or observed_hash,
            "response_sha256": hop.get("response_sha256"),
        })
    return reports, sources, failures


def _recovery_events(manifest: Mapping[str, Any], hops: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for control in manifest.get("controls", []) or []:
        if isinstance(control, Mapping) and str(control.get("action", "")).lower() in {
            "resume", "retry", "restart_role", "new_chat", "open_tab", "route_plan"
        }:
            events.append(dict(control))
    for hop in hops:
        wait = hop.get("wait")
        if isinstance(wait, Mapping) and (wait.get("refresh_count") or wait.get("recovery_baseline") or wait.get("last_refresh_result")):
            events.append({"hop_id": hop.get("hop_id"), "wait_recovery": dict(wait)})
    return events


def _record_for_hop(
    repository: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    ledger_path: Path,
    hops: list[Mapping[str, Any]],
    index: int,
    *,
    split_manifest: Mapping[str, Mapping[str, Any]] | None,
    source_identities: Mapping[str, Mapping[str, Any]] | None,
    baseline_known_failures: Mapping[str, list[str]] | None,
) -> dict[str, Any] | None:
    hop = hops[index]
    task_id = str(manifest.get("task_id") or manifest_path.parent.name)
    team = str(manifest.get("team") or manifest_path.parent.parent.name)
    try:
        report_snapshot = _report_snapshot(
            repository, hop, "DEV", team=team, task_id=task_id
        )
    except RouteContractError:
        return None
    report_path, observed_report_hash, observed_report_size, report_failure = report_snapshot
    request_id = str(hop.get("request_id") or "")
    ledger_failure = None
    ledger_record = None
    try:
        durable_record = RequestLedger(ledger_path).peek(request_id)
        if durable_record is None:
            raise DurableRequestError("durable request record is missing")
        prompt = str(hop.get("prompt") or "")
        expected_source = {
            "task_id": task_id,
            "team": team,
            "hop_id": hop.get("hop_id"),
            "manifest": manifest.get("manifest_path"),
        }
        mismatches = [
            name
            for name, matches in (
                ("request_id", durable_record.request_id == request_id),
                ("role", durable_record.role == str(hop.get("physical_role") or "")),
                ("prompt", durable_record.prompt == prompt),
                ("rendered_prompt", durable_record.rendered_prompt == prompt),
                ("source_context", durable_record.source_context == expected_source),
            )
            if not matches
        ]
        if mismatches:
            raise DurableRequestError(
                "durable request identity does not match DEV hop: "
                + ", ".join(mismatches)
            )
        ledger_record = durable_record.to_dict()
    except (
        DurableRequestError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
    ) as exc:
        ledger_failure = {
            "kind": "request_ledger",
            "role": "DEV",
            "hop_id": hop.get("hop_id"),
            "request_id": request_id,
            "path": _relative(ledger_path, repository),
            "reason": str(exc),
        }
    downstream, downstream_sources, downstream_failures = _downstream(
        repository, hops, index, team=team, task_id=task_id
    )
    integrity_failures: list[dict[str, Any]] = []
    if report_failure is not None:
        integrity_failures.append(report_failure)
    if ledger_failure is not None:
        integrity_failures.append(ledger_failure)
    for kind in ("prompt", "response"):
        failure = _text_integrity_failure(hop, kind, "DEV")
        if failure is not None:
            integrity_failures.append(failure)
    integrity_failures.extend(downstream_failures)

    source = _source_identity(manifest, task_id, source_identities, baseline_known_failures)
    oracle_available = bool(source.get("oracle_sha256"))
    if integrity_failures:
        replay_class = "QUALITATIVE_ONLY"
        exclusion_reason = "evidence integrity check failed"
    elif source["pre_state_available"] and oracle_available:
        replay_class = "FULL_STATIC"
        exclusion_reason = None
    elif manifest.get("status") == "DONE" and downstream:
        replay_class = "DECISION_REPLAY"
        exclusion_reason = "exact historical pre-state and evaluator-owned oracle are not both available"
    else:
        replay_class = "QUALITATIVE_ONLY"
        exclusion_reason = (
            "task does not have accepted DONE closure"
            if manifest.get("status") != "DONE"
            else "downstream acceptance evidence is incomplete"
        )

    receipt = hop.get("receipt") if isinstance(hop.get("receipt"), Mapping) else None
    send_state = hop.get("state") or (ledger_record or {}).get("status")
    provenance_sources: list[tuple[Path, str | None]] = [
        (manifest_path, None),
        (ledger_path, None),
        (report_path, observed_report_hash),
        *downstream_sources,
    ]
    provenance = []
    seen: set[str] = set()
    for path, observed_hash in provenance_sources:
        if not path.is_file():
            continue
        relative = _relative(path, repository)
        if relative in seen:
            continue
        seen.add(relative)
        provenance.append({"path": relative, "sha256": observed_hash or _sha256(path)})

    task_errors = list(manifest.get("errors", []) or [])
    hop_errors = list(hop.get("errors", []) or [])
    record = {
        "schema_version": _SCHEMA_VERSION,
        "experience_id": f"{task_id}-dev-hop{hop.get('hop_id')}",
        "task_identity": {
            "task_id": task_id,
            "team": manifest.get("team") or manifest_path.parent.parent.name,
            "workspace": manifest.get("workspace") or manifest.get("repository"),
            "task_mode": manifest.get("task_mode", "workflow"),
            "status": manifest.get("status"),
        },
        "baseline_identity": source,
        "role_case": {
            "role": "DEV",
            "turn": hop.get("turn"),
            "hop_id": hop.get("hop_id"),
            "request_id": request_id,
            "source_role": hop.get("source_role"),
        },
        "input": {
            "handoff": hop.get("handoff"),
            "prompt": hop.get("prompt"),
            "prompt_sha256": hop.get("prompt_sha256"),
            "expected_report_path": hop.get("expected_report_path"),
        },
        "transport": {
            "endpoint_id": hop.get("browser_endpoint_id") or hop.get("endpoint_id"),
            "conversation_generation": hop.get("conversation_generation"),
            "conversation_url": hop.get("conversation_url"),
            "send_state": send_state,
            "receipt": dict(receipt) if receipt else None,
            "ledger_record": ledger_record,
            "timestamps": dict(hop.get("timestamps") or {}),
        },
        "dev_output": {
            "report_path": _relative(report_path, repository),
            "report_sha256": hop.get("report_sha256") or observed_report_hash,
            "report_size": hop.get("report_size") if type(hop.get("report_size")) is int else observed_report_size,
            "route": hop.get("route"),
            "response": hop.get("response"),
            "response_sha256": hop.get("response_sha256"),
        },
        "downstream": {
            "reports": downstream,
            "final_status": manifest.get("status"),
            "completed_at": manifest.get("completed_at"),
        },
        "operations": {
            "block_code": manifest.get("block_code"),
            "block_reason": manifest.get("block_reason"),
            "block_retryable": manifest.get("block_retryable"),
            "pause_events": [
                dict(control)
                for control in manifest.get("controls", []) or []
                if isinstance(control, Mapping) and str(control.get("action", "")).lower() == "pause"
            ],
            "controls": list(manifest.get("controls", []) or []),
            "errors": task_errors + hop_errors,
            "recovery_events": _recovery_events(manifest, hops),
        },
        "replayability": {
            "class": replay_class,
            "pre_state_available": source["pre_state_available"],
            "oracle_available": oracle_available,
            "exclusion_reason": exclusion_reason,
        },
        "contamination": _split(task_id, split_manifest),
        "provenance": {
            "schema_version": _SCHEMA_VERSION,
            "sources": provenance,
            "integrity_failures": integrity_failures,
        },
    }
    return make_document(record)


def iter_experience_records(
    repository: str | Path,
    *,
    split_manifest: Mapping[str, Mapping[str, Any]] | None = None,
    source_identities: Mapping[str, Mapping[str, Any]] | None = None,
    baseline_known_failures: Mapping[str, list[str]] | None = None,
) -> Iterable[dict[str, Any]]:
    repository = Path(repository).resolve()
    records: list[dict[str, Any]] = []
    for manifest_path in _primary_manifests(repository):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, Mapping):
            continue
        if manifest.get("task_id") != manifest_path.parent.name or manifest.get("team") != manifest_path.parent.parent.name:
            continue
        if manifest.get("status") not in _TERMINAL_EVIDENCE_STATES:
            continue
        hops = manifest.get("hops")
        if not isinstance(hops, list):
            continue
        ledger_path = manifest_path.parent / "requests.json"
        for index, hop in enumerate(hops):
            if not isinstance(hop, Mapping) or _role(hop) != "DEV":
                continue
            record = _record_for_hop(
                repository, manifest_path, manifest, ledger_path, hops, index,
                split_manifest=split_manifest,
                source_identities=source_identities,
                baseline_known_failures=baseline_known_failures,
            )
            if record is not None:
                records.append(record)
    return iter(sorted(records, key=lambda item: item["experience_id"]))


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def _validate_destination(repository: Path, destination: Path) -> Path:
    destination = destination.absolute()
    for path in [destination, *destination.parents]:
        if _is_reparse_point(path):
            raise ValueError("destination reparse point is not allowed")
        if path == repository.parent:
            break
    resolved = destination.resolve()
    plan = (repository / ".plan").resolve()
    if resolved == plan or plan in resolved.parents:
        raise ValueError("destination must be outside .plan")
    return resolved


def _validate_output_path(destination: Path, path: Path) -> None:
    try:
        relative = path.absolute().relative_to(destination)
    except ValueError as exc:
        raise ValueError("export path must remain inside destination") from exc
    current = destination
    if _is_reparse_point(current):
        raise ValueError("destination reparse point is not allowed")
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise ValueError(f"export path contains a reparse point: {current}")
    resolved = path.resolve()
    if resolved != destination and destination not in resolved.parents:
        raise ValueError("export path must remain inside destination")


def _write_immutable(destination: Path, path: Path, value: Mapping[str, Any]) -> None:
    data = canonical_bytes(value) + b"\n"
    _validate_output_path(destination, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_output_path(destination, path)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable export already exists with different bytes: {path}")
        return
    path.write_bytes(data)


def export_history(
    repository: str | Path,
    destination: str | Path,
    *,
    split_manifest: Mapping[str, Mapping[str, Any]] | None = None,
    source_identities: Mapping[str, Mapping[str, Any]] | None = None,
    baseline_known_failures: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    repository = Path(repository).resolve()
    destination = _validate_destination(repository, Path(destination))
    records = list(iter_experience_records(
        repository,
        split_manifest=split_manifest,
        source_identities=source_identities,
        baseline_known_failures=baseline_known_failures,
    ))
    root = destination / "evolution_evidence" / "experience_v1"
    entries = []
    counts = {"FULL_STATIC": 0, "DECISION_REPLAY": 0, "QUALITATIVE_ONLY": 0}
    for record in records:
        path = root / "records" / f"{record['experience_id']}.json"
        _write_immutable(destination, path, record)
        counts[record["replayability"]["class"]] += 1
        entries.append({
            "experience_id": record["experience_id"],
            "path": path.relative_to(destination).as_posix(),
            "sha256": record["sha256"],
            "replayability": record["replayability"]["class"],
        })
    index = make_document({
        "schema_version": _SCHEMA_VERSION,
        "record_count": len(entries),
        "replayability_counts": counts,
        "records": entries,
    })
    _write_immutable(destination, root / "index.json", index)
    return index
