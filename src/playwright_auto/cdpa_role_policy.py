from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

_ROLE_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_ROLE_SEPARATORS = re.compile(r"[\s-]+")

TERMINAL_ROUTE_TOKENS = frozenset({"DONE", "PAUSE"})
CONTROL_ROUTE_TOKENS = frozenset({"WAIT"})
TASK_STATUS_TOKENS = frozenset(
    {"INBOX", "RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED"}
)
DEPENDENCY_PROJECTION_TOKENS = frozenset({"WAITING_DEPENDENCY", "DEPENDENCY_FAILED"})
RESERVED_ROLE_TOKENS = frozenset(
    TERMINAL_ROUTE_TOKENS
    | CONTROL_ROUTE_TOKENS
    | TASK_STATUS_TOKENS
    | DEPENDENCY_PROJECTION_TOKENS
)
DEFAULT_MAX_REACHABLE_ROLES = 32
DEFAULT_MAX_ROUTES = 8


class RolePolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RolePolicyResult:
    submitted_roles: tuple[str, ...]
    roles: tuple[str, ...]
    route_targets: tuple[str, ...]
    reachable_roles: tuple[str, ...]
    start_role: str
    closing_role: str
    semantic_routes: dict[str, dict[str, str]]
    route_contract_id: str
    max_routes: int
    max_route_repairs: int
    self_route_policy: dict[str, Any]

    def initial_role_slots(self) -> dict[str, dict[str, Any]]:
        return {
            role: {
                "role_key": role,
                "state": "materialized" if role == self.start_role else "declared",
                "materialized_at": None,
            }
            for role in self.reachable_roles
        }


def canonicalize_role_key(value: Any) -> str:
    if not isinstance(value, str):
        raise RolePolicyError("ROLE_KEY_INVALID", "role keys must be strings")
    normalized = _ROLE_SEPARATORS.sub("_", value.strip().upper())
    if not normalized or not _ROLE_KEY.fullmatch(normalized):
        raise RolePolicyError(
            "ROLE_KEY_INVALID",
            "role key must match ^[A-Z][A-Z0-9_]{0,31}$ after canonicalization",
        )
    if normalized in RESERVED_ROLE_TOKENS:
        raise RolePolicyError(
            "ROLE_KEY_RESERVED", f"role key {normalized!r} is reserved by the runtime"
        )
    return normalized


def normalize_role_list(value: Any, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if value is None:
        if allow_empty:
            return ()
        raise RolePolicyError("ROLE_SET_INVALID", f"{field} must be a non-empty array")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RolePolicyError("ROLE_SET_INVALID", f"{field} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str) and not item.strip():
            continue
        role = canonicalize_role_key(item)
        if role in seen:
            continue
        seen.add(role)
        result.append(role)
    if not result and not allow_empty:
        raise RolePolicyError("ROLE_SET_INVALID", f"{field} must not be empty")
    return tuple(result)


def normalize_self_route_policy(value: Any) -> dict[str, Any]:
    if value is None or value == "" or value == "forbidden":
        return {"mode": "forbidden", "max_self_routes": 0}
    if isinstance(value, str):
        normalized = value.strip().lower()
        match = re.fullmatch(r"bounded\(max_self_routes=(\d+)\)", normalized)
        if match:
            maximum = int(match.group(1))
            if maximum < 1:
                raise RolePolicyError(
                    "SELF_ROUTE_POLICY_INVALID", "max_self_routes must be positive"
                )
            return {"mode": "bounded", "max_self_routes": maximum}
        raise RolePolicyError(
            "SELF_ROUTE_POLICY_INVALID",
            "self_route_policy must be forbidden or bounded(max_self_routes=N)",
        )
    if isinstance(value, Mapping):
        unknown = set(value) - {"mode", "max_self_routes"}
        if unknown:
            raise RolePolicyError(
                "SELF_ROUTE_POLICY_INVALID",
                f"unknown self-route policy fields: {sorted(unknown)!r}",
            )
        mode = str(value.get("mode") or "").strip().lower()
        if mode == "forbidden":
            if value.get("max_self_routes") not in {None, 0}:
                raise RolePolicyError(
                    "SELF_ROUTE_POLICY_INVALID",
                    "forbidden self-route policy cannot carry a positive bound",
                )
            return {"mode": "forbidden", "max_self_routes": 0}
        if mode == "bounded":
            maximum = value.get("max_self_routes")
            if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
                raise RolePolicyError(
                    "SELF_ROUTE_POLICY_INVALID", "bounded self-route policy requires max_self_routes >= 1"
                )
            return {"mode": "bounded", "max_self_routes": maximum}
    raise RolePolicyError("SELF_ROUTE_POLICY_INVALID", "invalid self_route_policy")


def normalize_semantic_routes(value: Any) -> dict[str, dict[str, str]]:
    if value is None or value == "":
        return {}
    if not isinstance(value, Mapping):
        raise RolePolicyError("SEMANTIC_ROUTE_INVALID", "semantic routes must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for raw_source, raw_routes in value.items():
        source = canonicalize_role_key(raw_source)
        if not isinstance(raw_routes, Mapping):
            raise RolePolicyError(
                "SEMANTIC_ROUTE_INVALID", f"semantic routes for {source} must be an object"
            )
        routes: dict[str, str] = {}
        for raw_outcome, raw_destination in raw_routes.items():
            outcome = str(raw_outcome or "").strip()
            if not outcome:
                raise RolePolicyError("SEMANTIC_ROUTE_INVALID", "semantic route outcome must not be empty")
            if not isinstance(raw_destination, str):
                raise RolePolicyError("SEMANTIC_ROUTE_INVALID", "semantic route destination must be a string")
            destination = raw_destination.strip().upper()
            if destination != "DONE":
                destination = canonicalize_role_key(destination)
            routes[outcome] = destination
        normalized[source] = routes
    return normalized


def semantic_route_contract_id(value: Any) -> str:
    normalized = normalize_semantic_routes(value)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "route-contract-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _positive_int(value: Any, *, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = ">= 0" if minimum == 0 else ">= 1"
        raise RolePolicyError("ROLE_POLICY_INVALID", f"{field} must be an integer {comparator}")
    return value


def build_role_policy(
    submitted_roles: Any,
    *,
    route_targets: Any = (),
    semantic_routes: Any = None,
    start_role: Any = None,
    closing_role: Any = None,
    max_routes: Any = DEFAULT_MAX_ROUTES,
    max_route_repairs: Any = 2,
    self_route_policy: Any = "forbidden",
    max_reachable_roles: int = DEFAULT_MAX_REACHABLE_ROLES,
) -> RolePolicyResult:
    raw_submitted = tuple(
        str(item) for item in submitted_roles
    ) if isinstance(submitted_roles, Sequence) and not isinstance(submitted_roles, (str, bytes)) else ()
    roles = normalize_role_list(submitted_roles, field="roles")
    targets = normalize_role_list(route_targets, field="route_targets", allow_empty=True)
    semantic = normalize_semantic_routes(semantic_routes)
    policy = normalize_self_route_policy(self_route_policy)
    maximum_reachable = _positive_int(
        max_reachable_roles, field="max_reachable_roles", minimum=1
    )
    normalized_max_routes = _positive_int(max_routes, field="max_routes", minimum=1)
    normalized_max_repairs = _positive_int(
        max_route_repairs, field="max_route_repairs", minimum=0
    )

    normalized_start = roles[0] if start_role in {None, ""} else canonicalize_role_key(start_role)
    normalized_closing = roles[0] if closing_role in {None, ""} else canonicalize_role_key(closing_role)
    if normalized_start not in roles:
        raise RolePolicyError("ROLE_POLICY_INVALID", "start_role must belong to normalized initial roles")
    if normalized_closing not in roles:
        raise RolePolicyError("ROLE_POLICY_INVALID", "closing_role must belong to normalized initial roles")

    reachable: list[str] = []
    queued: list[str] = []
    seen: set[str] = set()
    for role in (*roles, *targets):
        if role not in seen:
            seen.add(role)
            reachable.append(role)
            queued.append(role)
    index = 0
    while index < len(queued):
        source = queued[index]
        index += 1
        for destination in semantic.get(source, {}).values():
            if destination == "DONE":
                continue
            if destination == source and policy["mode"] == "forbidden":
                raise RolePolicyError(
                    "SELF_ROUTE_FORBIDDEN",
                    f"semantic route for {source} targets itself while self-route is forbidden",
                )
            if destination in seen:
                continue
            seen.add(destination)
            reachable.append(destination)
            queued.append(destination)
            if len(reachable) > maximum_reachable:
                raise RolePolicyError(
                    "REACHABLE_ROLE_LIMIT",
                    f"reachable role closure exceeds max_reachable_roles={maximum_reachable}",
                )
    if len(reachable) > maximum_reachable:
        raise RolePolicyError(
            "REACHABLE_ROLE_LIMIT",
            f"reachable role closure exceeds max_reachable_roles={maximum_reachable}",
        )

    frozen_semantic = {
        role: dict(semantic.get(role, {}))
        for role in reachable
    }
    return RolePolicyResult(
        submitted_roles=raw_submitted,
        roles=roles,
        route_targets=targets,
        reachable_roles=tuple(reachable),
        start_role=normalized_start,
        closing_role=normalized_closing,
        semantic_routes=frozen_semantic,
        route_contract_id=semantic_route_contract_id(frozen_semantic),
        max_routes=normalized_max_routes,
        max_route_repairs=normalized_max_repairs,
        self_route_policy=policy,
    )
