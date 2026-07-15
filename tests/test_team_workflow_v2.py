from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.team import TeamTranscript
from playwright_auto.workflow_file import load_workflow_file

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "workflows" / "chatgpt_team_loop.py"


def load_symbols(monkeypatch: pytest.MonkeyPatch, version: str = "2"):
    monkeypatch.setenv("PLAYWRIGHT_AUTO_WORKFLOW_VERSION", version)
    monkeypatch.setenv(
        "PLAYWRIGHT_AUTO_TEAM_JSON",
        json.dumps({"PLAN": 1, "DEV": 2, "REVIEW": 1, "TEST": 1}),
    )
    return runpy.run_path(str(WORKFLOW_PATH))


def fake_context(*roles: str):
    return SimpleNamespace(
        client=SimpleNamespace(active_roles=roles),
        require=lambda name: "test goal" if name == "goal" else None,
    )


def plan_transcript(value: dict[str, str]) -> TeamTranscript:
    transcript = TeamTranscript()
    transcript.record_success(
        "plan",
        "PLAN",
        prompt="plan prompt",
        result={"response": {"text": json.dumps(value), "image_count": 0}},
    )
    return transcript


def test_workflow_versions_preserve_v1_and_default_to_v2(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_AUTO_WORKFLOW_VERSION", "1")
    v1 = load_workflow_file(WORKFLOW_PATH)
    assert v1.variables["workflow_version"] == "1"
    assert [item.name for item in v1.workflow.blocks[0].rounds] == [
        "plan",
        "implement",
        "review",
        "revise",
        "closeout",
    ]

    monkeypatch.setenv("PLAYWRIGHT_AUTO_WORKFLOW_VERSION", "2")
    v2 = load_workflow_file(WORKFLOW_PATH)
    assert v2.variables["workflow_version"] == "2"
    assert [item.name for item in v2.workflow.blocks[0].rounds] == [
        "plan",
        "implement",
        "review",
        "revise",
        "reverify",
        "closeout",
    ]


def test_v2_plan_assigns_every_dev_role_distinct_work(monkeypatch):
    symbols = load_symbols(monkeypatch)
    context = fake_context("PLAN", "DEV", "DEV1", "REVIEW", "TEST")
    transcript = plan_transcript(
        {
            "DEV": "Implement parser and provide parser tests",
            "DEV1": "Implement transport and provide transport tests",
        }
    )

    assignments = symbols["_assignment_map"](context, transcript)
    assert assignments == {
        "DEV": "Implement parser and provide parser tests",
        "DEV1": "Implement transport and provide transport tests",
    }
    dev_prompt = symbols["implement_prompt_v2"](context, "DEV", transcript)
    dev1_prompt = symbols["implement_prompt_v2"](context, "DEV1", transcript)
    assert "Implement parser" in dev_prompt
    assert "Implement transport" not in dev_prompt
    assert "Implement transport" in dev1_prompt
    assert "Implement parser" not in dev1_prompt


def test_v2_assignment_map_rejects_missing_duplicate_or_unknown_roles(monkeypatch):
    symbols = load_symbols(monkeypatch)
    context = fake_context("PLAN", "DEV", "DEV1", "REVIEW", "TEST")

    with pytest.raises(ValueError, match="every DEV role"):
        symbols["_assignment_map"](
            context,
            plan_transcript({"DEV": "only one assignment"}),
        )

    with pytest.raises(ValueError, match="duplicate implementation work"):
        symbols["_assignment_map"](
            context,
            plan_transcript({"DEV": "same assignment", "DEV1": "same assignment"}),
        )

    with pytest.raises(ValueError, match="unknown route target"):
        symbols["_assignment_map"](
            context,
            plan_transcript(
                {
                    "DEV": "parser",
                    "DEV1": "transport",
                    "DEV2": "unallocated extra role",
                }
            ),
        )


def test_v2_review_and_reverify_include_plan_assignments(monkeypatch):
    symbols = load_symbols(monkeypatch)
    context = fake_context("PLAN", "DEV", "DEV1", "REVIEW", "TEST")
    transcript = plan_transcript(
        {"DEV": "ALPHA assignment", "DEV1": "BETA assignment"}
    )
    transcript.record_success(
        "implement",
        "DEV",
        prompt="alpha",
        result={"response": {"text": "ALPHA_EVIDENCE", "image_count": 0}},
    )
    transcript.record_success(
        "review",
        "REVIEW",
        prompt="review",
        result={"response": {"text": "VERDICT: ACCEPTED", "image_count": 0}},
    )
    transcript.record_success(
        "revise",
        "DEV",
        prompt="revise",
        result={"response": {"text": "REVISION_STATUS: COMPLETE", "image_count": 0}},
    )

    review = symbols["review_prompt_v2"](context, "REVIEW", transcript)
    reverify = symbols["reverify_prompt_v2"](context, "REVIEW", transcript)
    assert '"DEV": "ALPHA assignment"' in review
    assert '"DEV1": "BETA assignment"' in review
    assert '"DEV": "ALPHA assignment"' in reverify
    assert "ALPHA_EVIDENCE" in reverify
    assert "REVISION_STATUS: COMPLETE" in reverify


def test_v2_reverify_gate_controls_closeout_status(monkeypatch):
    symbols = load_symbols(monkeypatch)
    context = fake_context("PLAN", "DEV", "DEV1", "REVIEW", "TEST")
    transcript = TeamTranscript()
    for role in ("REVIEW", "TEST"):
        transcript.record_success(
            "reverify",
            role,
            prompt=f"verify {role}",
            result={
                "response": {
                    "text": "Evidence checked\nVERDICT: ACCEPTED",
                    "image_count": 0,
                }
            },
        )

    assert symbols["_reverify_gate"](context, transcript) == {
        "REVIEW": "ACCEPTED",
        "TEST": "ACCEPTED",
    }
    completed = symbols["closeout_prompt_v2"](context, "PLAN", transcript)
    assert "required final task status is COMPLETED" in completed
    assert completed.rstrip().endswith("TASK_STATUS: COMPLETED")

    transcript.rounds["reverify"]["TEST"]["result"]["response"]["text"] = (
        "Missing runtime evidence\nVERDICT: BLOCKED"
    )
    blocked = symbols["closeout_prompt_v2"](context, "PLAN", transcript)
    assert "required final task status is BLOCKED" in blocked
    assert blocked.rstrip().endswith("TASK_STATUS: BLOCKED")
