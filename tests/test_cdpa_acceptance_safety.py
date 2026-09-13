from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "accept_cdpa_workflow.py"
    spec = importlib.util.spec_from_file_location("cdpa_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_worker_churn_is_diagnostic_not_a_tab_safety_failure():
    module = harness()
    before = {"user": "page", "extension": "background_page", "old": "service_worker"}
    after = {"user": "page", "extension": "background_page", "new": "service_worker"}
    assert module.stable_target_ids(before) == module.stable_target_ids(after)
    assert set(before) != set(after)


@pytest.mark.parametrize("after", [{"extension": "background_page"}, {"user": "page"}, {"user": "page", "extension": "background_page", "leaked": "page"}])
def test_target_guard_rejects_lost_user_surfaces_and_leaked_pages(after):
    before = {"user": "page", "extension": "background_page"}
    assert harness().stable_target_ids(before) != harness().stable_target_ids(after)


@pytest.mark.parametrize("roles", [["PLAN", "DEV", "TEST", "PLAN"], ["PLAN", "DEV", "TEST", "REVIEW"]])
def test_pipeline_gate_rejects_missing_required_roles(roles):
    state = {"hops": [{"target_role": role, "kind": "handoff"} for role in roles]}
    with pytest.raises(AssertionError):
        harness().assert_pipeline_role_sequence(state)


def test_pipeline_gate_allows_bounded_repair_without_counting_a_new_role():
    roles = ["PLAN", "DEV", "TEST", "REVIEW", "PLAN"]
    state = {"hops": [{"target_role": role, "kind": "handoff"} for role in roles]}
    state["hops"].insert(3, {"target_role": "TEST", "kind": "route_repair"})
    assert harness().assert_pipeline_role_sequence(state) == roles
