from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .cdpa_role_policy import (
    canonicalize_role_key,
    normalize_self_route_policy,
    normalize_semantic_routes,
    semantic_route_contract_id,
)


class RoutePolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RouteContract:
    current_role: str
    reachable_roles: tuple[str, ...]
    closing_role: str
    allowed_routes: tuple[str, ...]
    semantic_routes: dict[str, str]
    route_contract_id: str
    max_routes: int
    route_seq: int
    max_route_repairs: int
    repair_count: int
    self_route_policy: dict[str, Any]
    self_route_counts: dict[str, int]
    budget_exhausted: bool = False

    def prompt_route_lines(self) -> tuple[str, ...]:
        lines = tuple(
            f"{outcome} -> {destination}"
            for outcome, destination in self.semantic_routes.items()
        )
        return (*lines, "Allowed routes: " + "|".join(self.allowed_routes))


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, *, field: str) -> int:
    parsed = _nonnegative_int(value, field=field)
    if parsed < 1:
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", f"{field} must be positive")
    return parsed


def _budget_exhausted(consumed: int, limit: int) -> bool:
    return consumed >= limit


def route_budget_exhausted(state: Mapping[str, Any]) -> bool:
    """Return whether the task has consumed its configured role-transition budget."""
    consumed = _nonnegative_int(state.get("route_seq", 0), field="route_seq")
    limit = _positive_int(state.get("max_routes"), field="max_routes")
    return _budget_exhausted(consumed, limit)


def route_repair_budget_exhausted(state: Mapping[str, Any]) -> bool:
    """Return whether the active role turn has consumed its route-repair budget."""
    consumed = _nonnegative_int(
        state.get("route_repair_count", 0), field="route_repair_count"
    )
    limit = _nonnegative_int(
        state.get("max_route_repairs", 0), field="max_route_repairs"
    )
    return _budget_exhausted(consumed, limit)


def build_route_contract(
    *,
    current_role: Any,
    reachable_roles: Any,
    closing_role: Any,
    semantic_routes: Any,
    route_contract_id: Any,
    max_routes: Any,
    route_seq: Any,
    max_route_repairs: Any,
    repair_count: Any = 0,
    self_route_policy: Any = "forbidden",
    self_route_counts: Mapping[str, Any] | None = None,
) -> RouteContract:
    try:
        current = canonicalize_role_key(current_role)
        closing = canonicalize_role_key(closing_role)
        reachable = tuple(canonicalize_role_key(item) for item in reachable_roles)
        semantic = normalize_semantic_routes(semantic_routes)
        self_policy = normalize_self_route_policy(self_route_policy)
    except ValueError as exc:
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", str(exc)) from exc
    if not reachable or len(set(reachable)) != len(reachable):
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", "reachable_roles must be non-empty and unique")
    if current not in reachable or closing not in reachable:
        raise RoutePolicyError(
            "ROUTE_CONTRACT_INVALID", "current_role and closing_role must be reachable"
        )
    expected_contract_id = semantic_route_contract_id(semantic)
    if str(route_contract_id or "") != expected_contract_id:
        raise RoutePolicyError(
            "ROUTE_CONTRACT_DRIFT",
            "semantic route configuration changed after task creation",
        )
    route_limit = _positive_int(max_routes, field="max_routes")
    sequence = _nonnegative_int(route_seq, field="route_seq")
    repair_limit = _nonnegative_int(max_route_repairs, field="max_route_repairs")
    repairs = _nonnegative_int(repair_count, field="repair_count")
    counts: dict[str, int] = {role: 0 for role in reachable}
    for raw_role, raw_count in dict(self_route_counts or {}).items():
        try:
            role = canonicalize_role_key(raw_role)
        except ValueError as exc:
            raise RoutePolicyError("ROUTE_CONTRACT_INVALID", str(exc)) from exc
        if role not in counts:
            raise RoutePolicyError("ROUTE_CONTRACT_INVALID", "self_route_counts contains unreachable role")
        counts[role] = _nonnegative_int(raw_count, field=f"self_route_counts[{role}]")

    budget_exhausted = _budget_exhausted(sequence, route_limit)
    if budget_exhausted and current != closing:
        raise RoutePolicyError(
            "ROUTE_BUDGET_EXHAUSTED",
            "route budget is exhausted before the next non-closing role Send",
        )

    if budget_exhausted:
        allowed = ("DONE",)
    else:
        allowed_list = [role for role in reachable if role != current]
        if self_policy["mode"] == "bounded" and counts[current] < self_policy["max_self_routes"]:
            allowed_list.append(current)
        allowed_list.append("PAUSE")
        if current == closing:
            allowed_list.append("DONE")
        allowed = tuple(dict.fromkeys(allowed_list))

    current_semantic: dict[str, str] = {}
    for outcome, destination in semantic.get(current, {}).items():
        mapped = closing if destination == "DONE" and current != closing else destination
        if mapped == current and self_policy["mode"] == "forbidden":
            raise RoutePolicyError(
                "ROUTE_CONTRACT_INVALID", "forbidden semantic self-route reached prompt generation"
            )
        if mapped not in allowed:
            if budget_exhausted and current == closing:
                continue
            if (
                mapped == current
                and self_policy["mode"] == "bounded"
                and counts[current] >= self_policy["max_self_routes"]
            ):
                continue
            raise RoutePolicyError(
                "ROUTE_CONTRACT_INVALID",
                f"semantic route destination {mapped!r} is outside the closed route enum",
            )
        current_semantic[outcome] = mapped

    return RouteContract(
        current_role=current,
        reachable_roles=reachable,
        closing_role=closing,
        allowed_routes=allowed,
        semantic_routes=current_semantic,
        route_contract_id=expected_contract_id,
        max_routes=route_limit,
        route_seq=sequence,
        max_route_repairs=repair_limit,
        repair_count=repairs,
        self_route_policy=self_policy,
        self_route_counts=counts,
        budget_exhausted=budget_exhausted,
    )


def apply_valid_route_transition(
    state: dict[str, Any],
    *,
    source_role: str,
    target_role: str,
    materialized_at: str,
) -> None:
    source = canonicalize_role_key(source_role)
    target = canonicalize_role_key(target_role)
    reachable = tuple(state.get("reachable_roles") or ())
    if source not in reachable or target not in reachable:
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", "route transition escaped reachable_roles")
    state["route_seq"] = int(state.get("route_seq") or 0) + 1
    counts = state.setdefault("self_route_counts", {})
    if source == target:
        counts[source] = int(counts.get(source) or 0) + 1
    else:
        state["route_repair_count"] = 0
    slots = state.setdefault("role_slots", {})
    slot = slots.get(target)
    if not isinstance(slot, dict):
        raise RoutePolicyError("ROUTE_CONTRACT_INVALID", "target role slot is missing")
    if slot.get("state") == "declared":
        slot["state"] = "materialized"
        slot["materialized_at"] = materialized_at
    role_record = state.get("roles", {}).get(target)
    if isinstance(role_record, dict):
        role_record["slot_state"] = slot["state"]
        role_record["materialized_at"] = slot.get("materialized_at")
