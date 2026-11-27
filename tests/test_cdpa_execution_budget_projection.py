from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_runtime_db import RuntimeDB
from test_cdpa_core import _prompt_kwargs, write_config
from test_cdpa_projection import raw_task


def budget_task(tmp_path: Path) -> dict:
    raw = raw_task(tmp_path)
    raw.update(
        workflow_mode="team", route_seq=8, max_routes=8,
        execution_budget={
            "epoch": 1, "limit": 109,
            "charged_hop_ids": [1, 2, 3, 4, 5, 6, 7, 8, 11, 13],
            "renewed_at": None,
        },
    )
    return raw


def test_legacy_route_cap_does_not_hide_current_execution_budget(tmp_path: Path):
    raw = budget_task(tmp_path)
    before = deepcopy(raw)
    projection = build_task_projection(raw, tasks=[raw])
    for public in (projection.summary, projection.detail):
        assert public["route_seq"] == public["max_routes"] == 8
        assert public["max_routes_is_historical"] is True
        assert public["execution_budget"] == {
            "epoch": 1, "limit": 109, "unit": "charged_hops",
            "charged_hops": 10, "remaining_hops": 99, "limit_reached": False,
            "enforced_for": ["async_wake"],
        }
    assert raw == before


def test_budget_changes_reach_persisted_task_detail_and_version(tmp_path: Path):
    raw = budget_task(tmp_path)
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    db.upsert_task_projections([build_task_projection(raw, tasks=[raw])])
    first = db.get_task_detail(raw["task_id"])
    assert first["execution_budget"]["remaining_hops"] == 99
    raw["execution_budget"]["charged_hop_ids"].append(14)
    db.upsert_task_projections([build_task_projection(raw, tasks=[raw])])
    second = db.get_task_detail(raw["task_id"])
    assert second["version"] > first["version"]
    assert second["execution_budget"]["charged_hops"] == 11
    assert second["execution_budget"]["remaining_hops"] == 98
    assert second["max_routes"] == first["max_routes"] == 8


def test_renewed_epoch_counts_only_its_charged_hops(tmp_path: Path):
    raw = budget_task(tmp_path)
    raw["route_seq"] = 39
    raw["execution_budget"].update(epoch=3, limit=12, charged_hop_ids=[41])
    public = build_task_projection(raw, tasks=[raw]).detail
    assert public["execution_budget"]["epoch"] == 3
    assert public["execution_budget"]["charged_hops"] == 1
    assert public["execution_budget"]["remaining_hops"] == 11
    assert public["route_seq"] == 39
    assert public["max_routes"] == 8


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("team", ["async_wake"]), ("solo", ["role_execution", "async_wake"]),
     ("independent", ["role_execution", "async_wake"])],
)
def test_exhausted_count_reports_actual_enforcement_scope(tmp_path: Path, mode, expected):
    raw = budget_task(tmp_path)
    raw["workflow_mode"] = "solo" if mode == "solo" else "team"
    if mode == "independent":
        raw["task_mode"] = "independent"
        raw["independent"] = {}
    raw["execution_budget"].update(limit=2, charged_hop_ids=[1, 2, 3])
    public = build_task_projection(raw, tasks=[raw]).detail
    assert public["execution_budget"]["limit_reached"] is True
    assert public["execution_budget"]["remaining_hops"] == 0
    assert public["execution_budget"]["enforced_for"] == expected
    assert public["status"] == "RUNNING"


@pytest.mark.parametrize("budget", [
    None, {},
    {"epoch": True, "limit": 12, "charged_hop_ids": [1]},
    {"epoch": 1, "limit": False, "charged_hop_ids": [1]},
    {"epoch": 1, "limit": 12, "charged_hop_ids": [1, 1]},
    {"epoch": 1, "limit": 12, "charged_hop_ids": [0]},
])
def test_missing_or_invalid_saved_budget_stays_unknown_without_mutation(tmp_path: Path, budget):
    raw = budget_task(tmp_path)
    if budget is None:
        raw.pop("execution_budget")
    else:
        raw["execution_budget"] = budget
    before = deepcopy(raw)
    public = build_task_projection(raw, tasks=[raw]).detail
    assert public["execution_budget"] is None
    assert public["max_routes_is_historical"] is True
    assert raw == before


@pytest.mark.parametrize("turn", [1, 4])
def test_normal_role_prompt_explains_historical_route_counts(tmp_path: Path, turn):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).build(**_prompt_kwargs(
        config, tmp_path, logical_role="DEV", physical_role="alpha-dev",
        turn=turn, allowed_routes=("PLAN", "REVIEW", "PAUSE"),
        handoff=".plan/alpha/alpha-plan.md",
    )).text
    assert "route_seq counts past role transitions" in prompt
    assert "max_routes is historical metadata, not an enforced cap" in prompt
    assert "Do not hand off or stop solely because these counts meet" in prompt
    assert '"route":"PLAN|REVIEW|PAUSE"' in prompt
    assert prompt.endswith("DONE authority belongs to PLAN; semantic completion returns control to that closing role.")
