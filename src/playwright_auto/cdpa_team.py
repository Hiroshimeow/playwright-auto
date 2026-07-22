from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

TERMINAL_STATUSES = frozenset({"DONE", "STOPPED"})
_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
_EXACT_TEAM = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_EXACT_TEAM_LENGTH = 128


def normalize_team_base(value: str) -> str:
    cleaned = _NAME.sub("-", str(value).strip()).strip("-_").lower()
    if not cleaned:
        raise ValueError("team name must contain letters or digits")
    if len(cleaned) > 48:
        cleaned = cleaned[:48].rstrip("-_")
    return cleaned


def validate_exact_team(value: str) -> str:
    team = str(value)
    if not team:
        raise ValueError("exact team must not be empty")
    if team != team.strip():
        raise ValueError("exact team must not contain leading or trailing whitespace")
    if len(team) > _MAX_EXACT_TEAM_LENGTH:
        raise ValueError(
            f"exact team exceeds {_MAX_EXACT_TEAM_LENGTH} characters: {len(team)}"
        )
    if not _EXACT_TEAM.fullmatch(team):
        raise ValueError(
            "exact team must use lowercase letters, digits, underscores, or hyphens "
            "and start with a letter or digit"
        )
    return team


def allocate_team(
    base: str,
    manifests: Iterable[Mapping[str, Any]],
    *,
    reserved_suffixes: Iterable[int] = (),
) -> tuple[str, int]:
    base = normalize_team_base(base)
    active = [
        item
        for item in manifests
        if str(item.get("status") or "").upper() not in TERMINAL_STATUSES
        and (
            str(item.get("team_base") or "") == base
            or str(item.get("team") or "") == base
            or re.fullmatch(rf"{re.escape(base)}\d+", str(item.get("team") or ""))
        )
    ]
    occupied_names = {str(item.get("team")) for item in active}
    occupied_slots = {
        int(item.get("team_suffix") or 1)
        for item in active
        if str(item.get("team_suffix") or "1").isdigit()
    }
    occupied_slots.update(
        int(suffix)
        for suffix in reserved_suffixes
        if int(suffix) > 0
    )
    suffix = 1
    while True:
        candidate = base if suffix == 1 else f"{base}{suffix}"
        if suffix not in occupied_slots and candidate not in occupied_names:
            return candidate, suffix
        suffix += 1


def physical_role(logical_role: str, team_base: str, team_suffix: int) -> str:
    role = str(logical_role).strip().upper()
    if role not in {"PLAN", "DEV", "REVIEW", "TEST", "AUDIT"}:
        raise ValueError(f"unsupported CDPA role {role!r}")
    suffix = int(team_suffix)
    if suffix <= 0:
        raise ValueError("team suffix must be positive")
    ending = "" if suffix == 1 else str(suffix)
    return f"{normalize_team_base(team_base)}-{role.lower()}{ending}"


def cleanup_eligible(
    manifest: Mapping[str, Any],
    *,
    now: datetime | None = None,
    idle_seconds: float,
) -> bool:
    if str(manifest.get("status") or "").upper() not in TERMINAL_STATUSES:
        return False
    if manifest.get("active_role"):
        return False
    raw = manifest.get("last_role_activity_at") or manifest.get("completed_at") or manifest.get("stopped_at")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - last).total_seconds() >= float(idle_seconds)
