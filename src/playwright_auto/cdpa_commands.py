from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

COMMAND_ORIGINS = frozenset({"operator", "independent_agent", "worker", "repair_task"})
COMMAND_STATES = frozenset(
    {"PENDING", "RUNNING", "APPLIED", "INEFFECTIVE", "REJECTED", "SUSPENDED"}
)
REPAIR_DISPOSITIONS = frozenset({"CONTINUE_IN_PARALLEL", "HOLD_FOR_REPAIR"})
REPAIR_SOURCE_AREAS = frozenset(
    {
        "cdpa_worker",
        "cdpa_store",
        "cdpa_actions",
        "dashboard",
        "dependencies",
        "queue",
        "transport",
        "tests",
        "prompts",
        "docs",
        "packaging",
    }
)

REPAIR_ROOT_CAUSE_MAX_CHARS = 1200
REPAIR_REASON_MAX_CHARS = 1200
REPAIR_REPRODUCTION_MAX_CHARS = 2400
REPAIR_SOURCE_AREA_MAX_COUNT = 8
REPAIR_REQUIRED_TEST_MAX_COUNT = 16
REPAIR_REQUIRED_TEST_MAX_CHARS = 300
REPAIR_LESSON_MAX_CHARS = 600
REPAIR_IDENTITY_MAX_CHARS = 512
REPAIR_REPOSITORY_MAX_CHARS = 4096


def _bounded_text(
    name: str,
    value: Any,
    maximum: int,
    *,
    optional: bool = False,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"repair {name} must be a string")
    if not isinstance(value, str):
        raise ValueError(f"repair {name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"repair {name} must not be empty")
    if len(text) > maximum:
        raise ValueError(f"repair {name} must be at most {maximum} characters")
    return text


def _bounded_optional_identity(name: str, value: Any) -> str | None:
    return _bounded_text(name, value, REPAIR_IDENTITY_MAX_CHARS, optional=True)


def _bounded_string_collection(
    name: str,
    item_name: str,
    value: Any,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"repair {name} must be a list or tuple")
    raw_count = len(value)
    if raw_count == 0:
        raise ValueError(f"repair {name} require at least one item")
    if raw_count > maximum:
        raise ValueError(f"repair {name} may contain at most {maximum} raw entries")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"repair {item_name} item must be a string")
    if len(set(value)) != raw_count:
        raise ValueError(f"repair {name} must not contain duplicates")
    normalized = tuple(item.strip() for item in value)
    if any(not item for item in normalized):
        raise ValueError(f"repair {item_name} item must not be empty")
    if len(set(normalized)) != raw_count:
        raise ValueError(f"repair {name} must not normalize to duplicates")
    return normalized


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha_json(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def conversation_identity(value: Any) -> str | None:
    try:
        path = urlparse(str(value or "")).path.rstrip("/")
    except ValueError:
        return None
    match = re.search(r"(?:^|/)c/([^/]+)$", path)
    return f"/c/{match.group(1)}" if match else None


def _active_hop(state: Mapping[str, Any]) -> Mapping[str, Any] | None:
    active_hop_id = state.get("active_hop_id")
    for hop in state.get("hops") or ():
        if isinstance(hop, Mapping) and hop.get("hop_id") == active_hop_id:
            return hop
    return None


def _normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"[\s\-_./:;]+", " ", text).strip()
    return text


def normalize_root_cause_key(value: str) -> str:
    normalized = _normalized_text(value)
    if not normalized:
        raise ValueError("root cause must not be empty")
    return "root-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def command_snapshot(state: Mapping[str, Any], *, role: str | None = None) -> dict[str, Any]:
    logical_role = str(role or state.get("active_role") or "").strip().upper() or None
    hop = _active_hop(state)
    role_record = (
        (state.get("roles") or {}).get(logical_role)
        if logical_role is not None and isinstance(state.get("roles"), Mapping)
        else None
    )
    if not isinstance(role_record, Mapping):
        role_record = {}
    receipt = hop.get("receipt") if isinstance(hop, Mapping) else None
    return {
        "status": str(state.get("status") or "").upper(),
        "updated_at": str(state.get("updated_at") or ""),
        "active_hop_id": state.get("active_hop_id"),
        "active_role": str(state.get("active_role") or "").upper() or None,
        "active_request_id": (
            str(hop.get("request_id") or "") or None if isinstance(hop, Mapping) else None
        ),
        "hop_state": (
            str(hop.get("state") or "") or None if isinstance(hop, Mapping) else None
        ),
        "hop_turn": hop.get("turn") if isinstance(hop, Mapping) else None,
        "handoff_sha256": (
            hashlib.sha256(str(hop.get("handoff") or "").encode("utf-8")).hexdigest()
            if isinstance(hop, Mapping)
            else None
        ),
        "role": logical_role,
        "physical_role": str(role_record.get("physical_role") or "") or None,
        "page_id": str(role_record.get("page_id") or "") or None,
        "conversation_id": conversation_identity(
            (hop.get("conversation_url") if isinstance(hop, Mapping) else None)
            or role_record.get("page_url")
        ),
        "conversation_generation": role_record.get("conversation_generation"),
        "receipt_sha256": _sha_json(receipt) if isinstance(receipt, Mapping) else None,
        "block_code": str(state.get("block_code") or "") or None,
    }


@dataclass(frozen=True)
class RepairRequest:
    root_cause_key: str
    root_cause: str
    repository: str
    repair_repository: str
    incident_id: str
    affected_task_id: str
    affected_team: str
    affected_hop_id: int | None
    affected_request_id: str | None
    affected_role: str | None
    affected_conversation_id: str | None
    disposition: str
    reason: str
    reproduction: str
    source_areas: tuple[str, ...]
    required_tests: tuple[str, ...]
    lesson: str | None

    @classmethod
    def validate_proposal(
        cls,
        *,
        root_cause: Any,
        disposition: Any,
        reason: Any,
        reproduction: Any,
        source_areas: Any,
        required_tests: Any,
        lesson: Any,
    ) -> dict[str, Any]:
        cause = _bounded_text("root cause", root_cause, REPAIR_ROOT_CAUSE_MAX_CHARS)
        normalized_reason = _bounded_text("reason", reason, REPAIR_REASON_MAX_CHARS)
        normalized_reproduction = _bounded_text(
            "reproduction", reproduction, REPAIR_REPRODUCTION_MAX_CHARS
        )
        normalized_disposition_value = _bounded_text(
            "disposition", disposition, REPAIR_IDENTITY_MAX_CHARS
        )
        assert normalized_disposition_value is not None
        normalized_disposition = normalized_disposition_value.upper()
        if normalized_disposition not in REPAIR_DISPOSITIONS:
            raise ValueError("unsupported repair disposition")
        areas = _bounded_string_collection(
            "source areas",
            "source area",
            source_areas,
            REPAIR_SOURCE_AREA_MAX_COUNT,
        )
        invalid = sorted(set(areas) - REPAIR_SOURCE_AREAS)
        if invalid:
            raise ValueError(f"unsupported repair source area(s): {invalid!r}")
        tests = _bounded_string_collection(
            "required tests",
            "required test",
            required_tests,
            REPAIR_REQUIRED_TEST_MAX_COUNT,
        )
        for item in tests:
            if "\n" in item or "\r" in item:
                raise ValueError("repair required test must be one line")
            if len(item) > REPAIR_REQUIRED_TEST_MAX_CHARS:
                raise ValueError(
                    f"repair required test must be at most {REPAIR_REQUIRED_TEST_MAX_CHARS} characters"
                )
        normalized_lesson = _bounded_text(
            "lesson", lesson, REPAIR_LESSON_MAX_CHARS, optional=True
        )
        if normalized_lesson and ("\n" in normalized_lesson or "\r" in normalized_lesson):
            raise ValueError("repair lesson must be one paragraph")
        return {
            "root_cause": cause,
            "disposition": normalized_disposition,
            "reason": normalized_reason,
            "reproduction": normalized_reproduction,
            "source_areas": areas,
            "required_tests": tests,
            "lesson": normalized_lesson,
        }

    @classmethod
    def _validated(
        cls,
        *,
        root_cause_key: Any,
        root_cause: Any,
        repository: Any,
        repair_repository: Any,
        incident_id: Any,
        affected_task_id: Any,
        affected_team: Any,
        affected_hop_id: Any,
        affected_request_id: Any,
        affected_role: Any,
        affected_conversation_id: Any,
        disposition: Any,
        reason: Any,
        reproduction: Any,
        source_areas: Sequence[Any],
        required_tests: Sequence[Any],
        lesson: Any,
        derive_root_cause_key: bool,
    ) -> "RepairRequest":
        proposal = cls.validate_proposal(
            root_cause=root_cause,
            disposition=disposition,
            reason=reason,
            reproduction=reproduction,
            source_areas=source_areas,
            required_tests=required_tests,
            lesson=lesson,
        )
        repository_value = _bounded_text(
            "request identity repository", repository, REPAIR_REPOSITORY_MAX_CHARS
        )
        repair_repository_value = _bounded_text(
            "request identity repair repository",
            repair_repository,
            REPAIR_REPOSITORY_MAX_CHARS,
        )
        incident = _bounded_text(
            "request identity incident", incident_id, REPAIR_IDENTITY_MAX_CHARS
        )
        task_id = _bounded_text(
            "request identity affected task", affected_task_id, REPAIR_IDENTITY_MAX_CHARS
        )
        team = _bounded_text(
            "request identity affected team", affected_team, REPAIR_IDENTITY_MAX_CHARS
        )
        if affected_hop_id is not None:
            if isinstance(affected_hop_id, bool) or not isinstance(affected_hop_id, int) or affected_hop_id < 0:
                raise ValueError("repair affected hop must be null or a non-negative integer")
        request_id = _bounded_optional_identity("affected request", affected_request_id)
        role = _bounded_optional_identity("affected role", affected_role)
        if role is not None:
            role = role.upper()
        conversation = _bounded_optional_identity(
            "affected conversation", affected_conversation_id
        )
        root_cause_value = proposal["root_cause"]
        assert isinstance(root_cause_value, str)
        expected_key = normalize_root_cause_key(root_cause_value)
        if derive_root_cause_key:
            if root_cause_key is not None:
                raise ValueError("derived repair root cause key must be null")
            provided_key = expected_key
        else:
            provided_key = _bounded_text(
                "root cause key", root_cause_key, REPAIR_IDENTITY_MAX_CHARS
            )
            assert provided_key is not None
        if provided_key != expected_key:
            raise ValueError("repair root cause key does not match root cause")
        assert isinstance(repository_value, str)
        assert isinstance(repair_repository_value, str)
        assert isinstance(incident, str)
        assert isinstance(task_id, str)
        assert isinstance(team, str)
        return cls(
            root_cause_key=expected_key,
            root_cause=root_cause_value,
            repository=repository_value,
            repair_repository=repair_repository_value,
            incident_id=incident,
            affected_task_id=task_id,
            affected_team=team,
            affected_hop_id=affected_hop_id,
            affected_request_id=request_id,
            affected_role=role,
            affected_conversation_id=conversation,
            disposition=proposal["disposition"],
            reason=proposal["reason"],
            reproduction=proposal["reproduction"],
            source_areas=proposal["source_areas"],
            required_tests=proposal["required_tests"],
            lesson=proposal["lesson"],
        )

    @classmethod
    def create(
        cls,
        *,
        root_cause: str,
        affected_state: Mapping[str, Any],
        incident_id: str,
        disposition: str,
        reason: str,
        reproduction: str,
        source_areas: Sequence[str],
        required_tests: Sequence[str],
        lesson: str | None,
        repair_repository: str | None = None,
    ) -> "RepairRequest":
        snapshot = command_snapshot(affected_state)
        return cls._validated(
            root_cause_key=None,
            root_cause=root_cause,
            repository=affected_state.get("repository"),
            repair_repository=str(
                repair_repository or affected_state.get("repository") or ""
            ),
            incident_id=incident_id,
            affected_task_id=affected_state.get("task_id"),
            affected_team=affected_state.get("team"),
            affected_hop_id=affected_state.get("active_hop_id"),
            affected_request_id=snapshot["active_request_id"],
            affected_role=snapshot["active_role"],
            affected_conversation_id=snapshot["conversation_id"],
            disposition=disposition,
            reason=reason,
            reproduction=reproduction,
            source_areas=source_areas,
            required_tests=required_tests,
            lesson=lesson,
            derive_root_cause_key=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_clone(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RepairRequest":
        if not isinstance(value, Mapping):
            raise ValueError("repair request must be an object")
        expected = {item.name for item in fields(cls)}
        if set(value) != expected:
            raise ValueError(f"repair request must contain exactly {sorted(expected)!r}")
        return cls._validated(
            root_cause_key=value.get("root_cause_key"),
            root_cause=value.get("root_cause"),
            repository=value.get("repository"),
            repair_repository=value.get("repair_repository"),
            incident_id=value.get("incident_id"),
            affected_task_id=value.get("affected_task_id"),
            affected_team=value.get("affected_team"),
            affected_hop_id=value.get("affected_hop_id"),
            affected_request_id=value.get("affected_request_id"),
            affected_role=value.get("affected_role"),
            affected_conversation_id=value.get("affected_conversation_id"),
            disposition=value.get("disposition"),
            reason=value.get("reason"),
            reproduction=value.get("reproduction"),
            source_areas=value["source_areas"],
            required_tests=value["required_tests"],
            lesson=value["lesson"],
            derive_root_cause_key=False,
        )

    def task_text(self) -> str:
        tests = "\n".join(f"- {item}" for item in self.required_tests)
        areas = ", ".join(self.source_areas)
        return "\n".join(
            (
                f"Repair CDPA root cause {self.root_cause_key}.",
                "",
                "Repository/worktree:",
                self.repair_repository,
                "",
                "Affected operation:",
                f"- repository: {self.repository}",
                f"- task: {self.affected_task_id}",
                f"- team: {self.affected_team}",
                f"- hop: {self.affected_hop_id}",
                f"- request: {self.affected_request_id}",
                f"- role: {self.affected_role}",
                f"- conversation: {self.affected_conversation_id}",
                f"- incident: {self.incident_id}",
                f"- disposition: {self.disposition}",
                "",
                "Evidence-backed root cause:",
                self.root_cause,
                "",
                "Reproduction:",
                self.reproduction,
                "",
                "Bounded source areas:",
                areas,
                "",
                "Required verification:",
                tests,
                "",
                "Constraints:",
                "Use existing CDPA worker, TaskStore, dependency DAG, locks, reports, and dashboard projections.",
                "Do not add another orchestrator, database, unrestricted shell action, or Maintainers self-invocation.",
                "Preserve the affected task identity, durable request provenance, unrelated dirty work, and PLAN-only DONE authority.",
                "Use TDD, independent TEST and REVIEW, compile/package checks, and a controlled live recovery pass.",
                "Add the proposed LEARNING.md rule only after the permanent repair and preserved-task resume are verified.",
                f"Proposed reusable lesson: {self.lesson or 'none'}",
            )
        )


@dataclass(frozen=True)
class WorkerCommand:
    command_id: str
    origin: str
    action: str
    reason: str
    task_id: str
    team: str
    repository: str
    role: str | None
    source_task_id: str | None
    source_event_key: str | None
    snapshot: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        origin: str,
        action: str,
        reason: str,
        state: Mapping[str, Any],
        role: str | None = None,
        source_task_id: str | None = None,
        source_event_key: str | None = None,
    ) -> "WorkerCommand":
        normalized_origin = str(origin).strip().lower()
        normalized_action = str(action).strip().lower()
        normalized_reason = str(reason).strip()
        logical_role = str(role).strip().upper() if role is not None else None
        task_id = str(state.get("task_id") or "").strip()
        team = str(state.get("team") or "").strip()
        repository = str(state.get("repository") or "").strip()
        if normalized_origin not in COMMAND_ORIGINS:
            raise ValueError("unsupported worker command origin")
        if not normalized_action or not normalized_reason or not task_id or not team or not repository:
            raise ValueError("worker command identity is incomplete")
        if logical_role == "MAINTAINERS":
            raise ValueError("worker command cannot target Maintainers")
        source_task = str(source_task_id or "").strip() or None
        source_event = str(source_event_key or "").strip() or None
        if (source_task is None) != (source_event is None):
            raise ValueError("worker command agent provenance requires both source fields")
        if normalized_origin == "independent_agent" and source_task is None:
            raise ValueError("independent-agent command requires source provenance")
        if normalized_origin != "independent_agent" and source_task is not None:
            raise ValueError("only independent-agent commands may carry source provenance")
        snapshot = command_snapshot(state, role=logical_role)
        identity = {
            "origin": normalized_origin,
            "action": normalized_action,
            "reason": normalized_reason,
            "task_id": task_id,
            "team": team,
            "repository": repository,
            "role": logical_role,
            "source_task_id": source_task,
            "source_event_key": source_event,
            "snapshot": snapshot,
        }
        command_id = "cmd-" + _sha_json(identity)[:24]
        return cls(
            command_id=command_id,
            origin=normalized_origin,
            action=normalized_action,
            reason=normalized_reason,
            task_id=task_id,
            team=team,
            repository=repository,
            role=logical_role,
            source_task_id=source_task,
            source_event_key=source_event,
            snapshot=snapshot,
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_clone(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerCommand":
        data = dict(value)
        data["snapshot"] = _json_clone(data.get("snapshot") or {})
        command = cls(**data)
        if command.origin not in COMMAND_ORIGINS:
            raise ValueError("unsupported worker command origin")
        expected = cls.create(
            origin=command.origin,
            action=command.action,
            reason=command.reason,
            state={
                "task_id": command.task_id,
                "team": command.team,
                "repository": command.repository,
                "status": command.snapshot.get("status"),
                "updated_at": command.snapshot.get("updated_at"),
                "active_hop_id": command.snapshot.get("active_hop_id"),
                "active_role": command.snapshot.get("active_role"),
                "roles": {
                    str(command.role or command.snapshot.get("role") or ""): {
                        "physical_role": command.snapshot.get("physical_role"),
                        "page_id": command.snapshot.get("page_id"),
                        "page_url": command.snapshot.get("conversation_id"),
                        "conversation_generation": command.snapshot.get(
                            "conversation_generation"
                        ),
                    }
                },
                "hops": (
                    [
                        {
                            "hop_id": command.snapshot.get("active_hop_id"),
                            "request_id": command.snapshot.get("active_request_id"),
                            "state": command.snapshot.get("hop_state"),
                            "turn": command.snapshot.get("hop_turn"),
                            "handoff": "",
                            "conversation_url": command.snapshot.get("conversation_id"),
                            "receipt": None,
                        }
                    ]
                    if command.snapshot.get("active_hop_id") is not None
                    else []
                ),
                "block_code": command.snapshot.get("block_code"),
            },
            role=command.role,
            source_task_id=command.source_task_id,
            source_event_key=command.source_event_key,
        )
        # Recomputing from a synthetic state cannot reproduce hashed handoff/receipt, so
        # validate the durable ID directly from the serialized identity instead.
        identity = command.to_dict()
        identity.pop("command_id", None)
        if command.command_id != "cmd-" + _sha_json(identity)[:24]:
            raise ValueError("worker command ID does not match its immutable payload")
        del expected
        return command


_WORKFLOW_CONTROL_ACTIONS = (
    "pause",
    "resume",
    "retry",
    "restart_role",
    "new_chat",
    "open_tab",
    "route_plan",
    "stop",
    "clear_team",
)
_WORKFLOW_TERMINAL = frozenset({"DONE", "STOPPED"})
_WORKFLOW_IN_FLIGHT = frozenset({"sending", "sent", "waiting"})


def workflow_control_eligibility(
    state: Mapping[str, Any],
    action: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    """Return the durable workflow control admission shared by store/worker/UI.

    Browser-only predicates still refine this at execution time. This helper only
    expresses facts present in the canonical manifest so dashboard enablement and
    locked admission cannot contradict one another.
    """
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _WORKFLOW_CONTROL_ACTIONS:
        return {"eligible": False, "reason": "Unsupported workflow control."}
    if isinstance(state.get("independent"), Mapping):
        return {"eligible": False, "reason": "Workflow controls do not apply to independent agents."}

    status = str(state.get("status") or "").upper()
    active_role = str(state.get("active_role") or "").upper() or None
    selected_role = str(role or active_role or "PLAN").upper()
    roles = state.get("roles") if isinstance(state.get("roles"), Mapping) else {}
    role_exists = selected_role in roles
    hop: Mapping[str, Any] | None = None
    active_hop_id = state.get("active_hop_id")
    for item in state.get("hops") or ():
        if isinstance(item, Mapping) and item.get("hop_id") == active_hop_id:
            hop = item
            break
    hop_state = str((hop or {}).get("state") or "")
    block_code = str(state.get("block_code") or "")
    guard_blocked = status == "BLOCKED" and block_code == "consecutive_self_route_limit"

    def yes() -> dict[str, Any]:
        return {"eligible": True, "reason": None, "role": selected_role}

    def no(reason: str) -> dict[str, Any]:
        return {"eligible": False, "reason": reason, "role": selected_role}

    if normalized_action == "pause":
        return yes() if status in {"INBOX", "RUNNING"} else no(
            "Pause is available only for INBOX or RUNNING work."
        )
    if normalized_action == "resume":
        if status in _WORKFLOW_TERMINAL:
            return no("Terminal work cannot be resumed.")
        if status == "WAITING" and str(state.get("waiting_code") or "") in {
            "dependencies",
            "dependency_waiting",
        }:
            return no("Resume cannot bypass unfinished dependencies.")
        if hop is None:
            return no("Resume requires an active durable hop.")
        return yes()
    if normalized_action == "retry":
        if status != "BLOCKED":
            return no("Retry hop is available only for BLOCKED work.")
        if not bool(state.get("block_retryable")):
            return no("This block is not retryable; use its stated recovery action.")
        return yes()
    if normalized_action in {"restart_role", "new_chat"}:
        if status in _WORKFLOW_TERMINAL:
            return no("Terminal work cannot replace its role context.")
        if not role_exists:
            return no("The selected workflow role does not exist for this task.")
        if guard_blocked and normalized_action == "restart_role":
            return no("Restart role cannot bypass the consecutive self-route guard; Resume is required.")
        if hop_state in _WORKFLOW_IN_FLIGHT:
            return no("This control cannot cross an in-flight or accepted Send boundary; use Resume.")
        return yes()
    if normalized_action == "open_tab":
        if status in _WORKFLOW_TERMINAL:
            return no("Terminal work has no active role tab to recover.")
        if not role_exists:
            return no("The selected workflow role does not exist for this task.")
        return yes()
    if normalized_action == "route_plan":
        if status in _WORKFLOW_TERMINAL:
            return no("Terminal work cannot route to PLAN.")
        if hop is None:
            return no("Route PLAN requires an active durable hop.")
        unresolved_accepted = (
            status == "BLOCKED"
            and block_code == "accepted_conversation_identity_unresolved"
            and hop_state == "waiting"
        )
        if unresolved_accepted:
            return yes()
        if active_role == "PLAN":
            return no("PLAN is already the active role.")
        if guard_blocked:
            return no("Route PLAN cannot bypass the consecutive self-route guard; Resume is required.")
        if hop_state in _WORKFLOW_IN_FLIGHT:
            return no("Route PLAN cannot abandon an in-flight Send; use Resume.")
        return yes()
    if normalized_action == "stop":
        return no("Stop is invalid for terminal work.") if status in _WORKFLOW_TERMINAL else yes()
    if normalized_action == "clear_team":
        # The actual control still requires confirmation before destructive
        # nonterminal cleanup and exact-owner browser preflight.
        return yes()
    return no("Unsupported workflow control.")


def workflow_control_matrix(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        action: workflow_control_eligibility(state, action)
        for action in _WORKFLOW_CONTROL_ACTIONS
    }


def validate_worker_command(command: WorkerCommand, state: Mapping[str, Any]) -> None:
    if str(state.get("task_id") or "") != command.task_id:
        raise ValueError("worker command task identity is stale")
    if str(state.get("team") or "") != command.team:
        raise ValueError("worker command team identity is stale")
    if str(state.get("repository") or "") != command.repository:
        raise ValueError("worker command repository identity is stale")
    current = command_snapshot(state, role=command.role)
    expected = command.snapshot
    # Explicit operator controls are authoritative. Their action-specific worker
    # validation remains in the control executor; background transport progress
    # must not make Pause/Stop/Clear/New Chat silently stale.
    if command.origin == "operator":
        return
    comparisons = (
        ("active_hop_id", "active hop"),
        ("active_request_id", "active request"),
        ("active_role", "active role"),
        ("hop_state", "hop state"),
        ("hop_turn", "hop turn"),
        ("handoff_sha256", "handoff"),
        ("physical_role", "physical role"),
        ("page_id", "page binding"),
        ("conversation_id", "conversation"),
        ("conversation_generation", "conversation generation"),
        ("receipt_sha256", "accepted-send receipt"),
    )
    for field, label in comparisons:
        if current.get(field) != expected.get(field):
            raise ValueError(f"worker command {label} snapshot is stale")
    if command.origin == "independent_agent":
        if command.action in {"resume", "open_tab", "retry", "restart_role", "new_chat"}:
            if current.get("status") != expected.get("status"):
                raise ValueError("worker command task status snapshot is stale")
        if command.action == "open_tab" and current.get("block_code") != expected.get(
            "block_code"
        ):
            raise ValueError("worker command operational block snapshot is stale")
