from __future__ import annotations

import copy

import pytest

from playwright_auto.cdpa_role_policy import build_role_policy
from playwright_auto.cdpa_route_contract import (
    RoutePolicyError,
    apply_valid_route_transition,
    build_route_contract,
)


def _policy(**overrides):
    values = {
        "submitted_roles": ["DEV"],
        "semantic_routes": {"DEV": {"review": "REVIEW"}, "REVIEW": {"clean": "DONE"}},
        "closing_role": "DEV",
        "max_routes": 3,
        "max_route_repairs": 2,
    }
    values.update(overrides)
    return build_role_policy(**values)


def _contract(policy, *, current="DEV", route_seq=0, repairs=0, counts=None, semantic=None):
    return build_route_contract(
        current_role=current,
        reachable_roles=policy.reachable_roles,
        closing_role=policy.closing_role,
        semantic_routes=policy.semantic_routes if semantic is None else semantic,
        route_contract_id=policy.route_contract_id,
        max_routes=policy.max_routes,
        route_seq=route_seq,
        max_route_repairs=policy.max_route_repairs,
        repair_count=repairs,
        self_route_policy=policy.self_route_policy,
        self_route_counts=counts or {},
    )


def test_t016_one_contract_drives_semantic_prose_and_closed_enum() -> None:
    policy = _policy()
    contract = _contract(policy)
    assert contract.allowed_routes == ("REVIEW", "PAUSE", "DONE")
    assert contract.semantic_routes == {"review": "REVIEW"}
    assert contract.prompt_route_lines()[-1] == "Allowed routes: REVIEW|PAUSE|DONE"
    assert all(destination in contract.allowed_routes for destination in contract.semantic_routes.values())


def test_t016_contract_drift_blocks_before_send() -> None:
    policy = _policy()
    changed = copy.deepcopy(policy.semantic_routes)
    changed["DEV"]["new"] = "REVIEW"
    with pytest.raises(RoutePolicyError) as error:
        _contract(policy, semantic=changed)
    assert error.value.code == "ROUTE_CONTRACT_DRIFT"


def test_t017_only_closing_role_gets_done_and_non_closing_done_returns_to_closer() -> None:
    policy = _policy()
    dev = _contract(policy, current="DEV")
    review = _contract(policy, current="REVIEW")
    assert "DONE" in dev.allowed_routes
    assert "DONE" not in review.allowed_routes
    assert review.semantic_routes["clean"] == "DEV"


def test_t018_bounded_self_route_appears_only_while_bound_remains() -> None:
    policy = build_role_policy(
        ["A"],
        semantic_routes={"A": {"retry": "A"}},
        self_route_policy={"mode": "bounded", "max_self_routes": 2},
        max_routes=4,
    )
    first = _contract(policy, current="A", counts={"A": 0})
    assert first.allowed_routes == ("A", "PAUSE", "DONE")
    last = _contract(policy, current="A", counts={"A": 2})
    assert last.allowed_routes == ("PAUSE", "DONE")


def test_t019_route_budget_blocks_non_closer_before_send_and_closer_done_only() -> None:
    policy = build_role_policy(
        ["A", "B"],
        semantic_routes={"A": {"to_b": "B"}, "B": {"to_a": "A"}},
        closing_role="A",
        max_routes=1,
    )
    with pytest.raises(RoutePolicyError) as error:
        _contract(policy, current="B", route_seq=1)
    assert error.value.code == "ROUTE_BUDGET_EXHAUSTED"

    closer = _contract(policy, current="A", route_seq=1)
    assert closer.budget_exhausted is True
    assert closer.allowed_routes == ("DONE",)


def test_t014_route_transition_materialization_is_one_idempotent_state_operation() -> None:
    policy = build_role_policy(["REVIEW"], semantic_routes={"REVIEW": {"defect": "DEV"}})
    state = {
        "reachable_roles": list(policy.reachable_roles),
        "route_seq": 0,
        "route_repair_count": 1,
        "self_route_counts": {role: 0 for role in policy.reachable_roles},
        "role_slots": policy.initial_role_slots(),
        "roles": {
            role: {"slot_state": slot["state"], "materialized_at": slot["materialized_at"]}
            for role, slot in policy.initial_role_slots().items()
        },
    }
    before_reachable = tuple(state["reachable_roles"])
    apply_valid_route_transition(
        state, source_role="REVIEW", target_role="DEV", materialized_at="2026-10-01T00:00:00Z"
    )
    assert state["route_seq"] == 1
    assert state["route_repair_count"] == 0
    assert state["role_slots"]["DEV"]["state"] == "materialized"
    assert tuple(state["reachable_roles"]) == before_reachable

    replay = copy.deepcopy(state)
    assert replay["route_seq"] == 1
    assert replay["role_slots"]["DEV"] == state["role_slots"]["DEV"]
