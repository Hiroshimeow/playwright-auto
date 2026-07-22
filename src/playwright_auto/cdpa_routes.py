from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROUTES = frozenset({"PLAN", "DEV", "REVIEW", "TEST", "AUDIT", "DONE"})
_KEYS = frozenset({"route", "handoff"})
_FENCE = re.compile(r"^```json\s*(\{.*\})\s*```$", re.DOTALL | re.IGNORECASE)


class RouteContractError(ValueError):
    pass


@dataclass(frozen=True)
class RouteDecision:
    route: str
    handoff: str


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


def parse_route_response(text: str, *, source_role: str | None = None) -> RouteDecision:
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
    if route not in ROUTES:
        raise RouteContractError(f"unsupported route {route!r}")
    if not handoff:
        raise RouteContractError("handoff must not be empty")
    if route == "DONE" and str(source_role or "").strip().upper() != "PLAN":
        raise RouteContractError("only PLAN may route DONE")
    return RouteDecision(route, handoff)


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


def validate_report(
    handoff: str,
    *,
    repository_root: str | Path,
    plans_root: str | Path,
    team: str,
    physical_role: str,
    turn: int,
    task_id: str,
) -> ReportEvidence:
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
    if candidate.is_symlink():
        raise RouteContractError("report path must not be a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(team_root)
    except ValueError as exc:
        raise RouteContractError("report path escapes the assigned team") from exc
    if resolved != candidate:
        raise RouteContractError("report path must not traverse symlinks")
    try:
        stat = candidate.stat()
    except FileNotFoundError as exc:
        raise RouteContractError("report file does not exist") from exc
    if not candidate.is_file() or not os.path.isfile(candidate):
        raise RouteContractError("report path must be a regular file")
    if stat.st_size <= 0:
        raise RouteContractError("report file must not be empty")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return ReportEvidence(str(candidate), digest, stat.st_size)
