from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .cdpa_safety import sanitize_text
from .cdpa_team import normalize_team_base, validate_exact_team

TASK_MODE_WORKFLOW = "workflow"
TASK_MODE_INDEPENDENT = "independent"
INDEPENDENT_ROLE = "AGENT"
INDEPENDENT_COLUMN = "INDEPENDENT_AGENTS"
MIN_INDEPENDENT_INTERVAL_MINUTES = 20
MAX_AGENT_NAME_CHARS = 80
MAX_SYSTEM_PROMPT_CHARS = 50_000
MAX_MANUAL_INSTRUCTION_CHARS = 4_000
MAX_TRIGGER_TEAMS = 100
MAX_TRIGGER_ROLES = 5
MAX_SEEN_EVENT_KEYS = 500
BUILTIN_MAINTAINERS_PROMPT = """# Maintainers

You are the built-in Maintainers independent agent. You are a normal one-agent CDPA task using the shared independent-agent engine, not a workflow role, coordinator, or sidecar.

Use the appropriate Superpower skill before acting. Use `@mcp-g8` for every repository inspection, command, runtime check, browser action, test, file operation, and CDPA control.

## Recovery responsibility

This is the CDPA single-operator local runtime system. Restore and verify stable operation: investigate the exact canonical recovery event, recover the affected task safely, preserve accepted-send and conversation identity, restore exact ownership, release queues or dependencies, and prevent recurrence of demonstrated operational defects. Do not broaden the job into general product improvement.

Use these worker-owned commands directly as needed:

- `independent_task_control` for the current event's exact target task only;
- `independent_create_repair` for a bounded normal repair task;
- `independent_continue` for another investigation/check cycle;
- `independent_complete` only after the operational and learning work below is finished.

Never emit route JSON, maintenance decision JSON, recovery arrays, action lists, route/action JSON, or instructions for the worker to parse from prose. Perform actions through the command mailbox and report the evidence and verified result in Markdown.

Preserve the exact task, team, hop, request, accepted-send receipt, conversation URL, dependencies, reports, and operator provenance. Never resend an accepted request. Never automatically reverse an explicit operator Pause, Stop, Clear Team, Restart role, or New Chat. If the canonical event is no longer eligible, do not act on stale evidence.

Use no more than five investigation/recovery/check cycles for one job. In that bound:

1. inspect the exact failure and retained evidence;
2. apply the smallest safe recovery through existing controls;
3. verify the task is stable or correctly waiting on a repair;
4. create or reuse a normal repair task when required;
5. perform one bounded post-incident learning pass, then complete the job.

A control is successful only after its action-specific postcondition is true. Use `independent_continue` when another bounded operational verification cycle is required.

## Repair decision

Create or reuse a normal repair task only when a demonstrated runtime/system defect recurs, recovery treats only a symptom, or the same defect can destabilize other current local tasks. Use `HOLD_FOR_REPAIR` only when continuation threatens ownership, accepted-send, durable-state, dependency, or idempotency integrity; otherwise use `CONTINUE_IN_PARALLEL`. One-off environmental incidents do not require repair work when direct recovery is safe and stable.

Repair creation and learning are separate decisions. A lesson never substitutes for source repair, and a pending repair is not proof that the defect is fixed.

Do not create repair work for cosmetic UI issues, trusted-local metadata visibility, product policy, compliance, generic privacy/security hardening, packaging breadth, or hypothetical future deployments unless explicitly requested or tied to a concrete current operational failure.

## Bounded learning pass

Run the learning pass only after the operational outcome is verified. This is one bounded learning pass, not recursive self-editing or automatic prompt evolution.

Inspect the incident evidence, actions, verified result, prior occurrences, current repository-root `LEARNING.md`, and related retained repair/report evidence. In the completion report, separate **Facts, Inference, and Proposed reusable rule**.

Add or revise a lesson only when all of these are true:

- observed failure, action, and verified postcondition support the rule;
- causal evidence supports why the failure occurred or why the rule works;
- transferability is shown by the same root cause in retained evidence or by a deterministic invariant or regression that applies across tasks;
- the rule states an applicability condition and a concrete action or check;
- the rule is consistent with operator intent, exact ownership, accepted-send non-replay, idempotency, dependency integrity, and current repository rules.

A plausible explanation, similar symptom, one machine occurrence, or transient outage is insufficient. Record `SKIPPED — insufficient reusable evidence` and leave `LEARNING.md` unchanged when the gate is not met.

Before mutation, search for equivalent or conflicting guidance; prefer revising the matching lesson over adding a duplicate. If evidence proves a matching lesson incomplete, stale, ineffective, or wrong, update that exact lesson and record `REVISED`; use a narrowly adjacent `SUPERSEDED` note only when retaining the old wording is necessary to prevent ambiguity. Never layer conflicting advice or edit unrelated lessons.

Keep lesson text concise, operational, and generalized. Remove secrets, credentials, raw paths, transient IDs, or timestamps, including task, team, page, request, and incident identifiers. `LEARNING.md` is not an incident log: do not add chronology, speculation, task-specific steps, stale facts, unsupported consequences, or generic warnings.

## `LEARNING.md` write boundary

The learning path may mutate only repository-root `LEARNING.md` through the shared repository operation. Invoke `uv run python -m playwright_auto.cdpa_learning --repository <repository-root>` with `@mcp-g8 shell_execute` and exactly one JSON object on stdin containing `disposition`, `old_text`, and `new_text`. Never mutate `LEARNING.md` through generic file tools.

The shared operation must:

1. resolve the repository root and confirm containment;
2. acquire the repository learning lock, then read repository-root `LEARNING.md` immediately before mutation as UTF-8;
3. require one byte-exact complete Markdown span and apply one bounded exact-content section or bullet edit without whitespace-fuzzy matching;
4. reject stale or conflicting target content instead of overwriting it;
5. validate Markdown structure, sanitization, and duplicate lessons while serialized writers preserve unrelated concurrent edits;
6. atomically replace and fsync the file, then read back and validate UTF-8, Markdown structure, repository containment, unchanged unrelated content, and absence of duplicate or conflicting lessons before releasing the lock.

Do not claim success until the operation returns validated JSON. Record exactly one learning disposition in the Maintainers report: `ADDED`, `REVISED`, `SUPERSEDED`, or `SKIPPED`, with supporting evidence outside `LEARNING.md`.

Through this learning path, never edit task manifests, SQLite, request ledgers, source code, tests, configuration, role reports, or task deliverables. Do not create another agent lifecycle, memory system, approval flow, scheduler, queue, store, or worker prose parser.

## Completion

Complete with one durable outcome:

- `SUCCESS` when recovery is verified stable;
- `NO_ACTION` when the event is already resolved and no mutation is needed;
- `REPAIR_REQUIRED` when a repair task owns the permanent operational correction;
- `OPERATOR_REQUIRED` when recovery is unsafe or impossible and exact evidence is recorded.

Call `independent_complete` only after stability/correct-waiting verification, the separate repair decision, and the single bounded learning disposition are recorded. Do not edit task manifests directly, impersonate PLAN/DEV/TEST/REVIEW/AUDIT, or mark workflow tasks DONE."""
BUILTIN_MONITOR_PROMPT = """You are the built-in Monitor independent agent using the shared one-agent task engine inside the CDPA single-operator local runtime system. Monitor operational flow only: stalled work, unexpected blocking, offline ownership, queue/dependency drift, repeated recovery, and failure to reach the next expected state. Review the exact interval, task-DONE, role-completion, team-state, CHECK_ALL, or Run-now trigger context. Report evidence-backed progress and blockers. Do not invent product-policy, compliance, generic privacy/security, packaging, or hypothetical deployment release gates. Use independent_activate_agent to activate Maintainers by immutable name only for a genuine unexpected recovery incident, and use independent_continue or independent_complete explicitly. Do not create a second scheduler, queue, event store, coordinator, or routing path, and never emit action JSON for the worker to parse."""

INDEPENDENT_OUTCOMES = frozenset(
    {"SUCCESS", "NO_ACTION", "REPAIR_REQUIRED", "OPERATOR_REQUIRED"}
)
_OPERATOR_ACTIONS = frozenset(
    {"pause", "stop", "clear_team", "restart_role", "new_chat"}
)
_RECOVERY_WAITING_CODES = frozenset(
    {
        "queue_release_failed",
        "queue_rebind_failed",
        "team_owner_conflict",
        "dependency_operational_failure",
    }
)
_ALLOWED_TRIGGER_KEYS = frozenset(
    {
        "recovery",
        "interval_minutes",
        "task_done",
        "role_completed",
        "teams",
        "states",
        "check_all",
    }
)
_ALLOWED_TEAM_STATES = frozenset(
    {"INBOX", "WAITING", "RUNNING", "PAUSED", "BLOCKED", "DONE", "STOPPED"}
)
_ALLOWED_ROLES = frozenset({"PLAN", "DEV", "TEST", "REVIEW", "AUDIT"})


def task_mode(state: Mapping[str, Any]) -> str:
    value = str(state.get("task_mode") or TASK_MODE_WORKFLOW).strip().lower()
    if value not in {TASK_MODE_WORKFLOW, TASK_MODE_INDEPENDENT}:
        raise ValueError(f"unsupported task_mode {value!r}")
    return value


def is_independent_task(state: Mapping[str, Any]) -> bool:
    return task_mode(state) == TASK_MODE_INDEPENDENT


def normalize_agent_name(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ValueError("agent name must be a string")
    display = " ".join(value.strip().split())
    if not display:
        raise ValueError("agent name must not be empty")
    if len(display) > MAX_AGENT_NAME_CHARS:
        raise ValueError(f"agent name must be at most {MAX_AGENT_NAME_CHARS} characters")
    key = unicodedata.normalize("NFKC", display).casefold()
    team_base = normalize_team_base(f"agent-{display}")
    team = validate_exact_team(team_base)
    return display, key, team


def normalize_system_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("system prompt must be a string")
    prompt = value.strip()
    if not prompt:
        raise ValueError("system prompt must not be empty")
    if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
        raise ValueError(
            f"system prompt must be at most {MAX_SYSTEM_PROMPT_CHARS} characters"
        )
    return prompt


def normalize_manual_instruction(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("instruction must be a string")
    instruction = sanitize_text(value, max_chars=MAX_MANUAL_INSTRUCTION_CHARS).strip()
    if not instruction:
        raise ValueError("instruction must not be empty")
    if len(value.strip()) > MAX_MANUAL_INSTRUCTION_CHARS:
        raise ValueError(
            f"instruction must be at most {MAX_MANUAL_INSTRUCTION_CHARS} characters"
        )
    return instruction


def _string_list(
    value: Any,
    *,
    field: str,
    maximum: int,
    upper: bool = False,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{field} may contain at most {maximum} items")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field} items must be non-empty strings")
        item = raw.strip().upper() if upper else raw.strip()
        if item in result:
            raise ValueError(f"{field} must not contain duplicates")
        result.append(item)
    return result


def validate_trigger_settings(value: Any) -> dict[str, Any]:
    raw = {} if value is None else value
    if not isinstance(raw, Mapping):
        raise ValueError("trigger settings must be an object")
    unknown = set(raw) - _ALLOWED_TRIGGER_KEYS
    if unknown:
        raise ValueError(f"unknown trigger settings: {sorted(unknown)!r}")
    interval_raw = raw.get("interval_minutes")
    if interval_raw is None:
        interval = None
    else:
        if isinstance(interval_raw, bool) or not isinstance(interval_raw, int):
            raise ValueError("interval_minutes must be an integer or null")
        if interval_raw < MIN_INDEPENDENT_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be at least {MIN_INDEPENDENT_INTERVAL_MINUTES}"
            )
        interval = interval_raw
    roles = _string_list(
        raw.get("role_completed"),
        field="role_completed",
        maximum=MAX_TRIGGER_ROLES,
        upper=True,
    )
    invalid_roles = sorted(set(roles) - _ALLOWED_ROLES)
    if invalid_roles:
        raise ValueError(f"unsupported role_completed roles: {invalid_roles!r}")
    teams = [
        validate_exact_team(item)
        for item in _string_list(
            raw.get("teams"), field="teams", maximum=MAX_TRIGGER_TEAMS
        )
    ]
    states = _string_list(
        raw.get("states"), field="states", maximum=len(_ALLOWED_TEAM_STATES), upper=True
    )
    invalid_states = sorted(set(states) - _ALLOWED_TEAM_STATES)
    if invalid_states:
        raise ValueError(f"unsupported team states: {invalid_states!r}")
    if states and not teams:
        raise ValueError("states require at least one selected exact team")
    return {
        "recovery": bool(raw.get("recovery")),
        "interval_minutes": interval,
        "task_done": bool(raw.get("task_done")),
        "role_completed": roles,
        "teams": teams,
        "states": states,
        "check_all": bool(raw.get("check_all")),
    }


def validate_independent_object(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return "independent must be an object"
    required = {
        "agent_name",
        "agent_key",
        "agent_generation",
        "previous_task_id",
        "enabled",
        "system_prompt",
        "trigger_settings",
        "watermarks",
        "active_event",
        "occurrence_counts",
        "max_cycles",
        "cycle",
        "completion_request",
        "continuation_request",
        "new_chat_next_job",
        "idle_since",
        "last_outcome",
        "successor_task_id",
    }
    missing = required - set(value)
    if missing:
        return f"independent is missing fields: {sorted(missing)!r}"
    try:
        display, key, _team = normalize_agent_name(value.get("agent_name"))
        if value.get("agent_key") != key:
            return "independent agent_key is not canonical"
        normalize_system_prompt(value.get("system_prompt"))
        validate_trigger_settings(value.get("trigger_settings"))
    except ValueError as exc:
        return str(exc)
    generation = value.get("agent_generation")
    max_cycles = value.get("max_cycles")
    cycle = value.get("cycle")
    for field, item, minimum in (
        ("agent_generation", generation, 1),
        ("max_cycles", max_cycles, 1),
        ("cycle", cycle, 0),
    ):
        if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
            return f"independent {field} is invalid"
    if not isinstance(value.get("enabled"), bool):
        return "independent enabled must be a boolean"
    if not isinstance(value.get("new_chat_next_job"), bool):
        return "independent new_chat_next_job must be a boolean"
    if not isinstance(value.get("close_tab_when_idle", False), bool):
        return "independent close_tab_when_idle must be a boolean"
    watermarks = value.get("watermarks")
    if not isinstance(watermarks, Mapping):
        return "independent watermarks must be an object"
    seen = watermarks.get("seen_event_keys", [])
    if (
        not isinstance(seen, list)
        or len(seen) > MAX_SEEN_EVENT_KEYS
        or any(not isinstance(item, str) or not item for item in seen)
        or len(set(seen)) != len(seen)
    ):
        return "independent seen_event_keys must be a bounded unique string array"
    cursors = watermarks.get("event_cursors", {})
    if not isinstance(cursors, Mapping):
        return "independent event_cursors must be an object"
    try:
        for trigger_type, cursor in cursors.items():
            if not isinstance(trigger_type, str) or not trigger_type.strip():
                raise ValueError("trigger type must be a non-empty string")
            normalize_event_cursor(cursor)
    except ValueError as exc:
        return f"independent event_cursors {exc}"
    counts = value.get("occurrence_counts")
    if not isinstance(counts, Mapping) or any(
        not isinstance(key_value, str)
        or not key_value
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for key_value, count in counts.items()
    ):
        return "independent occurrence_counts is invalid"
    active_event = value.get("active_event")
    if active_event is not None:
        try:
            normalize_event(active_event)
        except ValueError as exc:
            return f"independent active_event {exc}"
    completion = value.get("completion_request")
    if completion is not None:
        try:
            normalize_completion_request(completion)
        except ValueError as exc:
            return f"independent completion_request {exc}"
    continuation = value.get("continuation_request")
    if continuation is not None and not isinstance(continuation, Mapping):
        return "independent continuation_request must be null or an object"
    for field in ("previous_task_id", "idle_since", "successor_task_id"):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            return f"independent {field} must be null or a non-empty string"
    last = value.get("last_outcome")
    if last is not None:
        try:
            normalize_completion_request(last)
        except ValueError as exc:
            return f"independent last_outcome {exc}"
    return None


def normalize_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("must be an object")
    event_key = str(value.get("event_key") or "").strip()
    trigger_type = str(value.get("trigger_type") or "").strip().lower()
    occurred_at = str(value.get("occurred_at") or "").strip()
    if not event_key or not trigger_type or not occurred_at:
        raise ValueError("identity is incomplete")
    event = {
        "event_key": event_key,
        "trigger_type": trigger_type,
        "occurred_at": occurred_at,
        "target_team": str(value.get("target_team") or "").strip() or None,
        "target_task_id": str(value.get("target_task_id") or "").strip() or None,
        "target_role": str(value.get("target_role") or "").strip().upper() or None,
        "target_hop_id": value.get("target_hop_id"),
        "failure_signature": sanitize_text(
            value.get("failure_signature"), max_chars=300
        ).strip()
        or None,
        "occurrence_count": int(value.get("occurrence_count") or 1),
        "check_count": int(value.get("check_count") or 1),
    }
    if value.get("instruction") is not None:
        event["instruction"] = normalize_manual_instruction(value.get("instruction"))
    if event["target_hop_id"] is not None and (
        isinstance(event["target_hop_id"], bool)
        or not isinstance(event["target_hop_id"], int)
        or event["target_hop_id"] < 0
    ):
        raise ValueError("target_hop_id is invalid")
    if event["occurrence_count"] < 1 or event["check_count"] < 1:
        raise ValueError("counts must be positive")
    return event


def normalize_event_cursor(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("cursor must be an object")
    occurred_at = str(value.get("occurred_at") or "").strip()
    event_key = str(value.get("event_key") or "").strip()
    if not occurred_at or not event_key:
        raise ValueError("cursor identity is incomplete")
    return {"occurred_at": occurred_at, "event_key": event_key}


def _cursor_position(value: Mapping[str, Any]) -> tuple[str, str]:
    cursor = normalize_event_cursor(value)
    return cursor["occurred_at"], cursor["event_key"]


def event_is_consumed(
    event: Mapping[str, Any], watermarks: Mapping[str, Any]
) -> bool:
    normalized = normalize_event(event)
    cursors = watermarks.get("event_cursors")
    if not isinstance(cursors, Mapping):
        return False
    cursor = cursors.get(normalized["trigger_type"])
    if not isinstance(cursor, Mapping):
        return False
    return (normalized["occurred_at"], normalized["event_key"]) <= _cursor_position(cursor)


def record_consumed_event(
    independent: dict[str, Any], event: Mapping[str, Any]
) -> dict[str, str]:
    normalized = normalize_event(event)
    watermarks = independent.setdefault("watermarks", {})
    cursors = watermarks.setdefault("event_cursors", {})
    cursor = {
        "occurred_at": normalized["occurred_at"],
        "event_key": normalized["event_key"],
    }
    previous = cursors.get(normalized["trigger_type"])
    if not isinstance(previous, Mapping) or _cursor_position(previous) < _cursor_position(cursor):
        cursors[normalized["trigger_type"]] = cursor
    if normalized["trigger_type"] in {"interval", "check_all"}:
        watermarks["last_interval_slot"] = max(
            int(watermarks.get("last_interval_slot") or -1),
            int(normalized["check_count"]),
        )
    return cursor


def normalize_completion_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("must be an object")
    outcome = str(value.get("outcome") or "").strip().upper()
    if outcome not in INDEPENDENT_OUTCOMES:
        raise ValueError("outcome is invalid")
    summary = sanitize_text(value.get("summary"), max_chars=1200).strip()
    if not summary:
        raise ValueError("summary must not be empty")
    return {
        "outcome": outcome,
        "summary": summary,
        "target_task_id": str(value.get("target_task_id") or "").strip() or None,
        "repair_task_id": str(value.get("repair_task_id") or "").strip() or None,
        "requested_at": str(value.get("requested_at") or "").strip() or None,
    }


def normalized_failure_signature(state: Mapping[str, Any]) -> str:
    code = str(
        state.get("block_code")
        or state.get("waiting_code")
        or "unexpected_stopped"
    ).strip().lower()
    reason = unicodedata.normalize(
        "NFKC",
        sanitize_text(
            state.get("block_reason")
            or state.get("waiting_reason")
            or state.get("stop_reason"),
            max_chars=800,
        ),
    ).casefold()
    reason = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", reason)
    reason = re.sub(r"\d+", "<n>", reason)
    reason = re.sub(r"\s+", " ", reason).strip()
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    return f"{code}:{digest}"


def _event_time(state: Mapping[str, Any]) -> str:
    return str(
        state.get("blocked_at")
        or ((state.get("waiting") or {}).get("since") if isinstance(state.get("waiting"), Mapping) else None)
        or state.get("stopped_at")
        or state.get("completed_at")
        or state.get("updated_at")
        or state.get("created_at")
        or ""
    )


def _active_hop(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    active = state.get("active_hop_id")
    for hop in state.get("hops") or ():
        if isinstance(hop, Mapping) and hop.get("hop_id") == active:
            return hop
    return None


def _operator_caused_terminal_or_pause(state: Mapping[str, Any]) -> bool:
    status = str(state.get("status") or "").upper()
    if status == "PAUSED":
        return True
    event_at = _event_time(state)
    applied = []
    for control in state.get("controls") or ():
        if not isinstance(control, Mapping):
            continue
        action = str(control.get("action") or "").lower()
        origin = str(
            control.get("origin")
            or ((control.get("command") or {}).get("origin") if isinstance(control.get("command"), Mapping) else "")
            or "operator"
        ).lower()
        applied_at = str(control.get("applied_at") or control.get("requested_at") or "")
        if (
            action in _OPERATOR_ACTIONS
            and origin == "operator"
            and str(control.get("status") or "") in {"applied", "requested"}
            and (not event_at or not applied_at or applied_at >= event_at)
        ):
            applied.append(control)
    return bool(applied)


def _recovery_event(
    source: Mapping[str, Any], occurrence_counts: Mapping[str, Any]
) -> dict[str, Any] | None:
    status = str(source.get("status") or "").upper()
    waiting_code = str(source.get("waiting_code") or "").lower()
    if status not in {"BLOCKED", "STOPPED"} and not (
        status == "WAITING" and waiting_code in _RECOVERY_WAITING_CODES
    ):
        return None
    if _operator_caused_terminal_or_pause(source):
        return None
    code = str(source.get("block_code") or source.get("waiting_code") or "").lower()
    signature = normalized_failure_signature(source)
    team = str(source.get("team") or "")
    count_key = f"{team}:{signature}"
    occurrence = int(occurrence_counts.get(count_key) or 0) + 1
    hop = _active_hop(source)
    task_id = str(source.get("task_id") or "")
    hop_id = hop.get("hop_id") if isinstance(hop, Mapping) else None
    event_at = _event_time(source)
    waiting = source.get("waiting")
    waiting_since = (
        waiting.get("since") if isinstance(waiting, Mapping) else None
    )
    episode = str(
        source.get("blocked_at")
        or waiting_since
        or source.get("stopped_at")
        or event_at
    )
    event_key = (
        f"recovery:{task_id}:{episode}:{hop_id or 0}:"
        f"{code or 'unexpected_stopped'}:{signature.rsplit(':', 1)[-1]}"
    )
    return normalize_event(
        {
            "event_key": event_key,
            "trigger_type": "recovery",
            "occurred_at": event_at,
            "target_team": team,
            "target_task_id": task_id,
            "target_role": source.get("active_role")
            or (hop.get("target_role") if isinstance(hop, Mapping) else None),
            "target_hop_id": hop_id,
            "failure_signature": signature,
            "occurrence_count": occurrence,
            "check_count": 1,
        }
    )


def _task_done_event(source: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(source.get("status") or "").upper() != "DONE":
        return None
    completed = str(source.get("completed_at") or source.get("updated_at") or "")
    task_id = str(source.get("task_id") or "")
    return normalize_event(
        {
            "event_key": f"done:{task_id}:{completed}",
            "trigger_type": "task_done",
            "occurred_at": completed,
            "target_team": source.get("team"),
            "target_task_id": task_id,
        }
    )


def _role_events(
    source: Mapping[str, Any], selected_roles: Sequence[str]
) -> Iterable[dict[str, Any]]:
    selected = set(selected_roles)
    for report in source.get("reports") or ():
        if not isinstance(report, Mapping):
            continue
        role = str(report.get("role") or "").upper()
        if role not in selected:
            continue
        task_id = str(source.get("task_id") or "")
        report_id = str(report.get("report_id") or report.get("sha256") or "")
        created = str(report.get("created_at") or source.get("updated_at") or "")
        yield normalize_event(
            {
                "event_key": f"role-complete:{task_id}:{report_id}:{role}",
                "trigger_type": "role_completed",
                "occurred_at": created,
                "target_team": source.get("team"),
                "target_task_id": task_id,
                "target_role": role,
                "target_hop_id": report.get("hop_id"),
            }
        )


def canonical_independent_events(
    agent_state: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    independent = agent_state.get("independent")
    if not isinstance(independent, Mapping) or not bool(independent.get("enabled")):
        return []
    settings = validate_trigger_settings(independent.get("trigger_settings"))
    own_key = str(independent.get("agent_key") or "")
    own_team = str(agent_state.get("team") or "")
    counts = independent.get("occurrence_counts")
    occurrence_counts = counts if isinstance(counts, Mapping) else {}
    events: list[dict[str, Any]] = []
    for source in tasks:
        if source is agent_state or str(source.get("team") or "") == own_team:
            continue
        source_mode = task_mode(source)
        source_independent = source.get("independent")
        if source_mode == TASK_MODE_INDEPENDENT:
            source_key = (
                str(source_independent.get("agent_key") or "")
                if isinstance(source_independent, Mapping)
                else ""
            )
            if source_key == own_key or str(source.get("team") or "") == own_team:
                continue
            continue
        if settings["recovery"]:
            event = _recovery_event(source, occurrence_counts)
            if event is not None:
                events.append(event)
        if settings["task_done"]:
            event = _task_done_event(source)
            if event is not None:
                events.append(event)
        if settings["role_completed"]:
            events.extend(_role_events(source, settings["role_completed"]))
        if (
            settings["teams"]
            and str(source.get("team") or "") in settings["teams"]
            and str(source.get("status") or "").upper() in settings["states"]
        ):
            task_id = str(source.get("task_id") or "")
            updated = str(source.get("updated_at") or "")
            events.append(
                normalize_event(
                    {
                        "event_key": f"team-state:{task_id}:{source.get('status')}:{updated}",
                        "trigger_type": "team_state",
                        "occurred_at": updated,
                        "target_team": source.get("team"),
                        "target_task_id": task_id,
                    }
                )
            )
    interval = settings["interval_minutes"]
    if interval is not None:
        current = now or datetime.now(timezone.utc)
        slot = int(current.timestamp() // (interval * 60))
        last_slot = int(
            ((independent.get("watermarks") or {}).get("last_interval_slot") or -1)
        )
        if slot > last_slot:
            interval_trigger = "check_all" if settings["check_all"] else "interval"
            event_prefix = "check-all" if interval_trigger == "check_all" else "interval"
            events.append(
                normalize_event(
                    {
                        "event_key": f"{event_prefix}:{own_key}:{slot}",
                        "trigger_type": interval_trigger,
                        "occurred_at": datetime.fromtimestamp(
                            slot * interval * 60, timezone.utc
                        ).isoformat(),
                        "check_count": slot,
                    }
                )
            )
    watermarks = independent.get("watermarks") or {}
    seen = set(
        item
        for item in (watermarks.get("seen_event_keys") or [])
        if isinstance(item, str)
    )
    return sorted(
        (
            event
            for event in events
            if event["event_key"] not in seen
            and not event_is_consumed(event, watermarks)
        ),
        key=lambda item: (str(item["occurred_at"]), str(item["event_key"])),
    )


def claimed_event_keys(tasks: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return globally exclusive recovery event keys.

    Nonexclusive trigger classes are intentionally scoped to each agent's own
    watermark. Sharing those triggers is part of the independent-agent contract.
    """
    keys: set[str] = set()
    for state in tasks:
        if not is_independent_task(state):
            continue
        independent = state.get("independent")
        if not isinstance(independent, Mapping):
            continue
        active = independent.get("active_event")
        if (
            isinstance(active, Mapping)
            and str(active.get("trigger_type") or "").lower() == "recovery"
            and active.get("event_key")
        ):
            keys.add(str(active["event_key"]))
        exclusive_seen = (independent.get("watermarks") or {}).get(
            "seen_recovery_event_keys"
        )
        if isinstance(exclusive_seen, list):
            keys.update(
                str(item)
                for item in exclusive_seen
                if isinstance(item, str) and item
            )
    return keys


def claimed_recovery_cursor(
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    for state in tasks:
        if not is_independent_task(state):
            continue
        independent = state.get("independent")
        watermarks = independent.get("watermarks") if isinstance(independent, Mapping) else None
        cursors = watermarks.get("event_cursors") if isinstance(watermarks, Mapping) else None
        cursor = cursors.get("recovery") if isinstance(cursors, Mapping) else None
        if not isinstance(cursor, Mapping):
            continue
        normalized = normalize_event_cursor(cursor)
        if latest is None or _cursor_position(latest) < _cursor_position(normalized):
            latest = normalized
    return latest


def claim_oldest_event(
    state: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    all_tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    candidate = deepcopy(dict(state))
    independent = candidate.get("independent")
    if not isinstance(independent, dict):
        raise ValueError("independent task state is missing")
    if independent.get("active_event") is not None:
        return candidate
    if str(candidate.get("status") or "").upper() != "WAITING":
        return candidate
    other_tasks = [
        item
        for item in all_tasks
        if str(item.get("task_id")) != str(state.get("task_id"))
    ]
    unavailable = claimed_event_keys(other_tasks)
    recovery_cursor = claimed_recovery_cursor(other_tasks)
    event = next(
        (
            normalize_event(item)
            for item in sorted(
                events, key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_key") or ""))
            )
            if (
                str(item.get("trigger_type") or "").lower() != "recovery"
                or (
                    str(item.get("event_key") or "") not in unavailable
                    and (
                        recovery_cursor is None
                        or (
                            str(item.get("occurred_at") or ""),
                            str(item.get("event_key") or ""),
                        )
                        > _cursor_position(recovery_cursor)
                    )
                )
            )
        ),
        None,
    )
    if event is None:
        return candidate
    independent["active_event"] = event
    independent["cycle"] = 1
    independent["completion_request"] = None
    independent["continuation_request"] = None
    independent["idle_since"] = None
    if event["failure_signature"] and event["target_team"]:
        key = f"{event['target_team']}:{event['failure_signature']}"
        counts = independent.setdefault("occurrence_counts", {})
        counts[key] = max(int(counts.get(key) or 0), int(event["occurrence_count"]))
    candidate["status"] = "RUNNING"
    candidate["kanban_column"] = INDEPENDENT_COLUMN
    candidate["active_action"] = "queued"
    candidate["waiting"] = None
    candidate["waiting_reason"] = None
    candidate["waiting_code"] = None
    hops = candidate.get("hops") or []
    active_hop_id = candidate.get("active_hop_id")
    for hop in hops:
        if isinstance(hop, dict) and hop.get("hop_id") == active_hop_id:
            hop["state"] = "pre_send"
            hop["handoff"] = json.dumps(event, ensure_ascii=False, sort_keys=True)
            break
    return candidate


def event_context(
    event: Mapping[str, Any], *, cycle: int, max_cycles: int
) -> dict[str, Any]:
    normalized = normalize_event(event)
    return {
        **normalized,
        "cycle": int(cycle),
        "max_cycles": int(max_cycles),
    }
