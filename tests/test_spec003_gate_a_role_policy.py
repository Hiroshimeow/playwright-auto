from __future__ import annotations

import pytest

from playwright_auto.cdpa_role_policy import (
    RolePolicyError,
    build_role_policy,
    canonicalize_role_key,
    semantic_route_contract_id,
)


def test_t007_role_key_canonicalization_and_reserved_tokens() -> None:
    assert canonicalize_role_key(" review role ") == "REVIEW_ROLE"
    assert canonicalize_role_key("a-b  c") == "A_B_C"
    assert canonicalize_role_key("A1") == "A1"
    for invalid in ("1A", "A!", "A" * 33):
        with pytest.raises(RolePolicyError, match="role key"):
            canonicalize_role_key(invalid)
    for reserved in (
        "DONE",
        "PAUSE",
        "WAIT",
        "INBOX",
        "RUNNING",
        "WAITING",
        "BLOCKED",
        "PAUSED",
        "STOPPED",
        "WAITING_DEPENDENCY",
        "DEPENDENCY_FAILED",
    ):
        with pytest.raises(RolePolicyError) as error:
            canonicalize_role_key(reserved)
        assert error.value.code == "ROLE_KEY_RESERVED"


def test_t008_duplicate_collapse_defaults_and_initial_membership() -> None:
    policy = build_role_policy(["review", " REVIEW "])
    assert policy.roles == ("REVIEW",)
    assert policy.start_role == "REVIEW"
    assert policy.closing_role == "REVIEW"

    for field in ("start_role", "closing_role"):
        with pytest.raises(RolePolicyError) as error:
            build_role_policy(
                ["REVIEW"],
                route_targets=["DEV"],
                **{field: "DEV"},
            )
        assert error.value.code == "ROLE_POLICY_INVALID"


def test_t009_no_special_role_requirement() -> None:
    for roles in (["DEV", "TEST"], ["A"], ["AUDIT"], ["custom-role"]):
        policy = build_role_policy(roles)
        assert policy.roles
        assert policy.closing_role == policy.roles[0]


def test_t010_transitive_reachability_bound_and_frozen_contract_id() -> None:
    semantic = {
        "REVIEW": {"defect": "DEV"},
        "DEV": {"verified": "TEST"},
        "TEST": {"done": "DONE"},
    }
    policy = build_role_policy(["REVIEW"], route_targets=["AUDIT"], semantic_routes=semantic)
    assert policy.reachable_roles == ("REVIEW", "AUDIT", "DEV", "TEST")
    assert policy.route_contract_id == semantic_route_contract_id(policy.semantic_routes)
    assert policy.route_contract_id != semantic_route_contract_id(
        {**semantic, "AUDIT": {"issue": "DEV"}}
    )
    with pytest.raises(RolePolicyError) as error:
        build_role_policy(["A"], semantic_routes={"A": {"x": "B"}, "B": {"y": "C"}}, max_reachable_roles=2)
    assert error.value.code == "REACHABLE_ROLE_LIMIT"


def test_t011_single_role_review_delegates_to_lazy_dev_slot() -> None:
    policy = build_role_policy(
        ["REVIEW"], semantic_routes={"REVIEW": {"defect": "DEV"}}
    )
    assert policy.roles == ("REVIEW",)
    assert policy.reachable_roles == ("REVIEW", "DEV")
    slots = policy.initial_role_slots()
    assert slots["REVIEW"]["state"] == "materialized"
    assert slots["DEV"]["state"] == "declared"


def test_t012_generic_a_to_b_is_identical_to_named_delegation() -> None:
    generic = build_role_policy(["A"], semantic_routes={"A": {"next": "B"}})
    named = build_role_policy(["REVIEW"], semantic_routes={"REVIEW": {"next": "DEV"}})
    assert generic.reachable_roles == ("A", "B")
    assert named.reachable_roles == ("REVIEW", "DEV")
    assert [item["state"] for item in generic.initial_role_slots().values()] == [
        item["state"] for item in named.initial_role_slots().values()
    ]


def test_t013_dev_only_may_reach_review_without_changing_closer() -> None:
    policy = build_role_policy(["DEV"], semantic_routes={"DEV": {"review": "REVIEW"}})
    assert policy.closing_role == "DEV"
    assert policy.reachable_roles == ("DEV", "REVIEW")


def test_t018_semantic_self_route_forbidden_by_default_and_bounded_allowed() -> None:
    with pytest.raises(RolePolicyError) as error:
        build_role_policy(["A"], semantic_routes={"A": {"retry": "A"}})
    assert error.value.code == "SELF_ROUTE_FORBIDDEN"

    policy = build_role_policy(
        ["A"],
        semantic_routes={"A": {"retry": "A"}},
        self_route_policy={"mode": "bounded", "max_self_routes": 2},
    )
    assert policy.reachable_roles == ("A",)
    assert policy.self_route_policy == {"mode": "bounded", "max_self_routes": 2}
