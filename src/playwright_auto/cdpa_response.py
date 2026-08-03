from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableMapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def start_wait_budget(
    wait: MutableMapping[str, Any],
    *,
    timeout_seconds: float,
    now: datetime | None = None,
) -> None:
    current = now or utc_now()
    if parse_time(wait.get("started_at")) is None:
        wait["started_at"] = iso(current)
    if parse_time(wait.get("deadline_at")) is None:
        started = parse_time(wait["started_at"]) or current
        wait["deadline_at"] = iso(started + timedelta(seconds=float(timeout_seconds)))
    wait.setdefault("continuous_responding_since", None)
    wait.setdefault("activity_signature", None)
    wait.setdefault("activity_length", 0)
    wait.setdefault("activity_changed_at", wait.get("started_at"))
    wait.setdefault("activity_observed_at", None)
    wait.setdefault("transport_ui_active", False)
    wait.setdefault("refresh_count", 0)
    wait.setdefault("last_refresh_at", None)
    wait.setdefault("refresh_in_progress", None)


def remaining_timeout_ms(
    wait: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> int:
    deadline = parse_time(wait.get("deadline_at"))
    if deadline is None:
        raise ValueError("response wait deadline is missing")
    remaining = (deadline - (now or utc_now())).total_seconds()
    return max(0, round(remaining * 1000))


def observe_response_activity(
    wait: MutableMapping[str, Any],
    *,
    signature: str,
    length: int,
    now: datetime | None = None,
) -> bool:
    current = now or utc_now()
    previous = wait.get("activity_signature")
    changed = previous is None or str(previous) != str(signature)
    wait["activity_signature"] = str(signature)
    wait["activity_length"] = max(0, int(length))
    if changed or parse_time(wait.get("activity_observed_at")) is None:
        wait["activity_observed_at"] = iso(current)
    if changed:
        wait["activity_changed_at"] = iso(current)
    elif parse_time(wait.get("activity_changed_at")) is None:
        wait["activity_changed_at"] = wait.get("started_at") or iso(current)
    return changed


def observe_responding(
    wait: MutableMapping[str, Any],
    *,
    stop_visible: bool,
    composer_empty: bool,
    manual_input_pending: bool,
    now: datetime | None = None,
) -> bool:
    signal = bool(stop_visible and composer_empty and not manual_input_pending)
    if signal:
        if parse_time(wait.get("continuous_responding_since")) is None:
            wait["continuous_responding_since"] = iso(now or utc_now())
    else:
        wait["continuous_responding_since"] = None
    return signal


def recover_incomplete_refresh(
    wait: MutableMapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    progress = wait.get("refresh_in_progress")
    if not isinstance(progress, Mapping):
        return False
    current = now or utc_now()
    recovered = dict(progress)
    recovered.update(
        {
            "finished_at": iso(current),
            "status": "interrupted",
            "error": "worker restarted before refresh completion was recorded",
        }
    )
    anchor = (
        parse_time(wait.get("last_refresh_at"))
        or parse_time(progress.get("started_at"))
        or current
    )
    wait["last_refresh_at"] = iso(anchor)
    wait["continuous_responding_since"] = iso(anchor)
    wait["last_refresh_result"] = recovered
    wait["refresh_in_progress"] = None
    return True


def refresh_due(
    wait: Mapping[str, Any],
    *,
    refresh_after_seconds: float,
    composer_empty: bool = True,
    manual_input_pending: bool = False,
    now: datetime | None = None,
) -> bool:
    if wait.get("refresh_in_progress"):
        return False
    if not composer_empty or manual_input_pending:
        return False
    activity = parse_time(wait.get("activity_changed_at"))
    continuous = parse_time(wait.get("continuous_responding_since"))
    started = parse_time(wait.get("started_at"))
    last = parse_time(wait.get("last_refresh_at"))
    anchors = [value for value in (activity, continuous, started, last) if value is not None]
    if not anchors:
        return False
    anchor = max(anchors)
    return ((now or utc_now()) - anchor).total_seconds() >= float(refresh_after_seconds)


def begin_refresh(wait: MutableMapping[str, Any], *, now: datetime | None = None) -> None:
    current = now or utc_now()
    wait["refresh_count"] = int(wait.get("refresh_count") or 0) + 1
    wait["last_refresh_at"] = iso(current)
    wait["refresh_in_progress"] = {"started_at": iso(current), "status": "started"}
    wait["continuous_responding_since"] = iso(current)


def finish_refresh(
    wait: MutableMapping[str, Any],
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    current = now or utc_now()
    progress = dict(wait.get("refresh_in_progress") or {})
    progress["finished_at"] = iso(current)
    progress["status"] = "failed" if error else "completed"
    progress["error"] = error
    wait["last_refresh_result"] = progress
    wait["refresh_in_progress"] = None
