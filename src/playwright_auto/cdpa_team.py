from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .cdpa_dependencies import dependency_readiness

TERMINAL_STATUSES = frozenset({"DONE", "STOPPED"})
ACTIVE_TEAM_OWNER_STATUSES = frozenset({"INBOX", "RUNNING", "PAUSED", "BLOCKED"})
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


def is_active_team_owner(manifest: Mapping[str, Any]) -> bool:
    return str(manifest.get("status") or "").upper() in ACTIVE_TEAM_OWNER_STATUSES


def is_team_availability_barrier(manifest: Mapping[str, Any]) -> bool:
    if is_active_team_owner(manifest):
        return True
    cleanup = manifest.get("cleanup")
    if not isinstance(cleanup, Mapping):
        return False
    cleanup_state = str(cleanup.get("state") or "").upper()
    return cleanup_state == "CLEARING" or (
        cleanup_state == "CLEARED" and not cleanup.get("verified_empty_at")
    )


def exact_team_reuse_eligible(
    manifests: Iterable[Mapping[str, Any]],
    exact_team: str,
) -> bool:
    team = validate_exact_team(exact_team)
    team_tasks = [
        manifest
        for manifest in manifests
        if str(manifest.get("team") or "") == team
    ]
    if not team_tasks:
        return False
    identities = {
        (
            str(manifest.get("team_base") or manifest.get("team") or ""),
            int(manifest.get("team_suffix") or 1),
        )
        for manifest in team_tasks
    }
    if len(identities) != 1:
        return False
    return sum(is_team_availability_barrier(manifest) for manifest in team_tasks) <= 1


def exact_team_waiter_order_key(item: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        0 if item.get("priority") == "urgent_repair" else 1,
        str(item.get("created_at") or ""),
        str(item.get("task_id") or ""),
    )


def exact_team_ready_waiters(
    manifests: Iterable[Mapping[str, Any]],
    exact_team: str,
    *,
    dependency_tasks: Sequence[Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    team = validate_exact_team(exact_team)
    team_tasks = [
        manifest
        for manifest in manifests
        if str(manifest.get("team") or "") == team
    ]
    graph_tasks = list(dependency_tasks) if dependency_tasks is not None else team_tasks
    return sorted(
        (
            manifest
            for manifest in team_tasks
            if str(manifest.get("status") or "").upper() == "WAITING"
            and (
                (
                    isinstance(manifest.get("queue"), Mapping)
                    and manifest["queue"].get("reuse_team") is True
                    and manifest["queue"].get("released_at") is None
                )
                or bool(manifest.get("depends_on_task_ids"))
            )
            and dependency_readiness(manifest, graph_tasks).ready
        ),
        key=exact_team_waiter_order_key,
    )


def has_other_nonterminal_team_work(
    manifests: Iterable[Mapping[str, Any]],
    exact_team: str,
    *,
    exclude_task_id: str | None = None,
) -> bool:
    team = validate_exact_team(exact_team)
    excluded = str(exclude_task_id or "")
    return any(
        str(manifest.get("team") or "") == team
        and str(manifest.get("task_id") or "") != excluded
        and str(manifest.get("status") or "").upper() not in TERMINAL_STATUSES
        for manifest in manifests
    )


def has_queued_team_work(
    manifests: Iterable[Mapping[str, Any]],
    exact_team: str,
    *,
    exclude_task_id: str | None = None,
) -> bool:
    team = validate_exact_team(exact_team)
    excluded = str(exclude_task_id or "")
    return any(
        str(manifest.get("team") or "") == team
        and str(manifest.get("task_id") or "") != excluded
        and isinstance(manifest.get("queue"), Mapping)
        and manifest["queue"].get("reuse_team") is True
        and str(manifest.get("status") or "").upper() not in TERMINAL_STATUSES
        for manifest in manifests
    )


def queued_team_tasks(
    manifests: Iterable[Mapping[str, Any]],
    exact_team: str,
) -> list[Mapping[str, Any]]:
    team = validate_exact_team(exact_team)
    queued = []
    for manifest in manifests:
        queue = manifest.get("queue")
        if (
            str(manifest.get("team") or "") == team
            and isinstance(queue, Mapping)
            and queue.get("reuse_team") is True
            and queue.get("released_at") is None
            and str(manifest.get("status") or "").upper() not in TERMINAL_STATUSES
        ):
            queued.append(manifest)
    return sorted(
        queued,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("task_id") or ""),
        ),
    )


def physical_role(logical_role: str, team_base: str, team_suffix: int) -> str:
    role = str(logical_role).strip().upper()
    if role not in {"PLAN", "DEV", "REVIEW", "TEST", "AUDIT", "AGENT"}:
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
    queued_team_work: bool = False,
) -> bool:
    if queued_team_work:
        return False
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
