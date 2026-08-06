from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .cdpa_workflow_agents import validate_workflow_route_key
from .file_lock import exclusive_file_lock, fsync_parent_directory

ROUTES = frozenset({"PLAN", "DEV", "REVIEW", "TEST", "AUDIT", "DONE"})
_REPORT_MODES = frozenset({"file", "inline"})
_KEYS = frozenset({"route", "handoff"})
_FENCE = re.compile(r"^```json\s*(\{.*\})\s*```$", re.DOTALL | re.IGNORECASE)


class RouteContractError(ValueError):
    pass


class InlineReportMaterializationError(RouteContractError):
    pass


def normalize_report_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("report_mode must be 'file' or 'inline'")
    mode = value.strip().lower()
    if mode not in _REPORT_MODES:
        raise ValueError("report_mode must be 'file' or 'inline'")
    return mode


def effective_report_mode(
    value: Any,
    *,
    control_repository: str | Path,
    execution_repository: str | Path,
) -> str:
    del control_repository, execution_repository
    mode = normalize_report_mode(value)
    if mode != "file":
        raise ValueError("report_mode='inline' is no longer supported; workflow reports are file-only")
    return "file"


@dataclass(frozen=True)
class RouteDecision:
    route: str
    handoff: str


@dataclass(frozen=True)
class ParsedRoleResponse:
    decision: RouteDecision
    inline_report: str | None


@dataclass(frozen=True)
class ReportEvidence:
    path: str
    sha256: str
    size: int


def _decode(source: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RouteContractError(f"duplicate route field {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=pairs)
    except json.JSONDecodeError as exc:
        raise RouteContractError(f"invalid route JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RouteContractError("route response must be a JSON object")
    return value


def parse_route_response(
    text: str,
    *,
    source_role: str | None = None,
    allowed_routes: Sequence[str] | None = None,
) -> RouteDecision:
    source = str(text).strip()
    match = _FENCE.fullmatch(source)
    if match:
        source = match.group(1)
    elif not (source.startswith("{") and source.endswith("}")):
        raise RouteContractError("response must contain only one route JSON object")
    value = _decode(source)
    if set(value) != _KEYS:
        raise RouteContractError(f"route response must contain exactly {sorted(_KEYS)!r}")
    if not all(isinstance(value[key], str) for key in _KEYS):
        raise RouteContractError("route and handoff must be strings")
    route = value["route"].strip().upper()
    handoff = value["handoff"].strip()
    if allowed_routes is None:
        allowed = ROUTES
    else:
        try:
            allowed = frozenset(
                "DONE"
                if str(item).strip().upper() == "DONE"
                else validate_workflow_route_key(item)
                for item in allowed_routes
            )
        except ValueError as exc:
            raise RouteContractError(str(exc)) from exc
    if route not in allowed:
        if allowed_routes is not None:
            raise RouteContractError(
                f"route {route!r} is not selected or unavailable"
            )
        raise RouteContractError(f"unsupported route {route!r}")
    if not handoff:
        raise RouteContractError("handoff must not be empty")
    if route == "DONE" and str(source_role or "").strip().upper() != "PLAN":
        raise RouteContractError("only PLAN may route DONE")
    return RouteDecision(route, handoff)


def _contains_route_object(text: str) -> bool:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and _KEYS.issubset(value):
            return True
    return False


def _split_inline_response(text: str) -> tuple[str, str]:
    source = str(text).strip()
    fence_match = None
    fence_at = -1
    openings = re.finditer(r"(?im)(?:^|\n)(```json)", source)
    for opening in reversed(list(openings)):
        candidate_at = opening.start(1)
        candidate = _FENCE.fullmatch(source[candidate_at:])
        if candidate is not None:
            fence_at = candidate_at
            fence_match = candidate
            break
    if fence_match is not None:
        report = source[:fence_at].rstrip()
        route_source = fence_match.group(1)
    else:
        candidates: list[tuple[int, str]] = []
        for index, character in enumerate(source):
            if character != "{":
                continue
            tail = source[index:].strip()
            try:
                value = _decode(tail)
            except RouteContractError:
                continue
            if set(value) == _KEYS:
                candidates.append((index, tail))
        if not candidates:
            raise RouteContractError(
                "inline response must end with one terminal route JSON object"
            )
        index, route_source = candidates[-1]
        report = source[:index].rstrip()
    if not report.strip():
        raise RouteContractError("inline Markdown report must not be empty")
    if _contains_route_object(report):
        raise RouteContractError(
            "inline response must contain exactly one terminal route JSON object"
        )
    return report, route_source


def parse_role_response(
    text: str,
    *,
    source_role: str,
    report_mode: str = "file",
    allowed_routes: Sequence[str] | None = None,
) -> ParsedRoleResponse:
    mode = str(report_mode).strip().lower()
    if mode not in {"file", "inline"}:
        raise ValueError("report_mode must be 'file' or 'inline'")
    if mode == "file":
        if re.search(r'"handoff"\s*:\s*"INLINE"', str(text), re.IGNORECASE):
            raise RouteContractError(
                "file report mode requires a file report handoff and no inline body"
            )
        return ParsedRoleResponse(
            parse_route_response(
                text,
                source_role=source_role,
                allowed_routes=allowed_routes,
            ),
            None,
        )
    report, route_source = _split_inline_response(text)
    decision = parse_route_response(
        route_source,
        source_role=source_role,
        allowed_routes=allowed_routes,
    )
    if decision.handoff != "INLINE":
        raise RouteContractError('inline report mode requires handoff "INLINE"')
    return ParsedRoleResponse(decision, report)


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return str(path)


def expected_report_relative(
    *,
    plans_root: str | Path,
    repository_root: str | Path,
    team: str,
    physical_role: str,
    turn: int,
    task_id: str,
) -> str:
    root = Path(repository_root).expanduser().resolve()
    configured = Path(plans_root).expanduser()
    plans = (configured if configured.is_absolute() else root / configured).resolve()
    expected = plans / str(team) / f"{physical_role}_turn{int(turn)}_{task_id}.md"
    return _display_path(expected, root)


def _validated_report_location(
    handoff: str,
    *,
    repository_root: str | Path,
    plans_root: str | Path,
    team: str,
    physical_role: str,
    turn: int,
    task_id: str,
) -> tuple[Path, Path, Path]:
    root = Path(repository_root).expanduser().resolve()
    configured = Path(plans_root).expanduser()
    plans = (configured if configured.is_absolute() else root / configured).resolve()
    team_root = (plans / str(team)).resolve()
    expected = team_root / f"{physical_role}_turn{int(turn)}_{task_id}.md"
    raw = Path(str(handoff).strip()).expanduser()
    unresolved = raw if raw.is_absolute() else root / raw
    candidate = Path(os.path.abspath(unresolved))
    try:
        candidate.relative_to(team_root)
    except ValueError as exc:
        raise RouteContractError("report path escapes the assigned team") from exc
    if candidate != expected:
        raise RouteContractError(
            f"report path must exactly match {_display_path(expected, root)}"
        )
    return root, team_root, candidate



def materialize_inline_report(
    report: str,
    *,
    expected_report_path: str,
    repository_root: str | Path,
    plans_root: str | Path,
    team: str,
    physical_role: str,
    turn: int,
    task_id: str,
) -> ReportEvidence:
    body = str(report)
    if not body.strip():
        raise RouteContractError("inline Markdown report must not be empty")
    expected_relative = expected_report_relative(
        plans_root=plans_root,
        repository_root=repository_root,
        team=team,
        physical_role=physical_role,
        turn=turn,
        task_id=task_id,
    )
    if str(expected_report_path).strip() != expected_relative:
        raise RouteContractError("inline expected report path is inconsistent")
    _root, team_root, target = _validated_report_location(
        expected_relative,
        repository_root=repository_root,
        plans_root=plans_root,
        team=team,
        physical_role=physical_role,
        turn=turn,
        task_id=task_id,
    )
    if target.is_symlink():
        raise RouteContractError("report path must not be a symlink")
    data = body.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(target.suffix + ".lock")
    if lock.is_symlink():
        raise RouteContractError("report lock path must not be a symlink")
    temporary: Path | None = None
    try:
        with exclusive_file_lock(lock):
            if target.parent.resolve() != team_root or target.is_symlink():
                raise RouteContractError("report path must not traverse symlinks")
            if target.exists():
                if not target.is_file() or target.read_bytes() != data:
                    raise RouteContractError(
                        "inline report path already contains different content"
                    )
            else:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=target.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                temporary = None
                fsync_parent_directory(target)
    except OSError as exc:
        raise InlineReportMaterializationError(
            "inline report materialization failed"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return ReportEvidence(str(target), hashlib.sha256(data).hexdigest(), len(data))
