import asyncio
import importlib.util
from pathlib import Path

import pytest

from playwright_auto.team import TeamTranscript
from playwright_auto.workflow import WorkflowContext
from playwright_auto.workflow_file import load_workflow_file


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "two_role_review_flow.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("two_role_review_flow_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_uses_exact_two_roles_and_three_alternating_rounds(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_ROLES_JSON", '["REVIEW", "REVIEW1"]')
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_TASK", "Review QMH")
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_REPO", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_TASK_ID", "qmh-review-test")

    loaded = load_workflow_file(SCRIPT)

    assert loaded.workspace_roles == ("REVIEW", "REVIEW1")
    assert loaded.variables["goal"] == "Review QMH"
    assert loaded.variables["repo_path"] == str(tmp_path.resolve())
    block = loaded.workflow.blocks[0]
    assert [item.name for item in block.rounds] == [
        "initial_review",
        "challenge_review",
        "final_review",
    ]
    assert [item.targets for item in block.rounds] == [
        ("REVIEW",),
        ("REVIEW1",),
        ("REVIEW",),
    ]
    assert all(item.parallel is False for item in block.rounds)


def test_later_prompts_include_prior_review_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_ROLES_JSON", '["REVIEW", "REVIEW1"]')
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_TASK", "Review QMH")
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_REPO", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_AUTO_REVIEW_TASK_ID", "qmh-review-test")
    loaded = load_workflow_file(SCRIPT)
    block = loaded.workflow.blocks[0]
    transcript = TeamTranscript()
    transcript.record_success(
        "initial_review",
        "REVIEW",
        prompt="initial",
        result={"response": {"text": "INITIAL_FINDING"}},
    )
    context = WorkflowContext(client=object(), variables=dict(loaded.variables))

    challenge = asyncio.run(block.rounds[1].prompt(context, "REVIEW1", transcript))
    assert "INITIAL_FINDING" in challenge
    assert "independently verify" in challenge.lower()

    transcript.record_success(
        "challenge_review",
        "REVIEW1",
        prompt=challenge,
        result={"response": {"text": "CHALLENGE_FINDING"}},
    )
    final = asyncio.run(block.rounds[2].prompt(context, "REVIEW", transcript))
    assert "INITIAL_FINDING" in final
    assert "CHALLENGE_FINDING" in final
    assert "FINAL_VERDICT:" in final


def test_roles_must_be_two_distinct_valid_names():
    module = _load_script_module()

    with pytest.raises(ValueError, match="exactly two"):
        module.validate_roles(("REVIEW",))
    with pytest.raises(ValueError, match="distinct"):
        module.validate_roles(("REVIEW", "REVIEW"))

    assert module.validate_roles(("REVIEW", "REVIEW1")) == ("REVIEW", "REVIEW1")
