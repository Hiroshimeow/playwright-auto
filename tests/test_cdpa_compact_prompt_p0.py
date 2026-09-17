from __future__ import annotations

from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from test_cdpa_core import write_config


FINAL_DIRECTIVE = "Work first. Write the report, then return exactly one valid route JSON object."
PLAN_CLOSURE_CUE = (
    "PLAN_CLOSURE: Treat the handoff and accepted role reports as closure evidence. After a clean REVIEW and satisfied task acceptance with no concrete remaining in-scope work, "
    "route DONE now. Do not route DEV merely because PLAN itself did not run tests, or for continued progression, closure preparation, or redundant validation. "
    "If not routing DONE, identify a concrete unmet acceptance criterion or fixable defect from the current evidence."
)


def test_later_normal_prompt_is_human_first_and_compact(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    long_goal = "Fix compact prompt P0 now.\n\n" + ("IMMUTABLE_TASK_DETAIL " * 900)
    long_title = "Prompt recovery " + ("TITLE_DETAIL " * 100)
    prompt = PromptBuilder(config).build(
        task_title=long_title,
        task_id="task-compact",
        team="alpha",
        logical_role="DEV",
        physical_role="alpha-dev",
        turn=2,
        allowed_routes=("PLAN", "DEV", "REVIEW", "DONE"),
        workspace=str(tmp_path),
        source_physical_role="alpha-review",
        handoff=".plan/alpha/alpha-review_turn1_task-compact.md",
        goal=long_goal,
        expected_report_path=".plan/alpha/alpha-dev_turn2_task-compact.md",
        constructor_sent_generation=0,
        conversation_generation=0,
    ).text

    assert prompt.startswith("You are DEV for the team alpha | task-compact:")
    assert len(prompt) <= 2400
    assert long_goal not in prompt
    assert long_title not in prompt
    assert prompt.count("TITLE_DETAIL") < 20
    assert prompt.count("IMMUTABLE_TASK_DETAIL") < 40
    assert "Constructor for DEV" not in prompt
    assert "TEST_BASE_CONTEXT" not in prompt
    assert '"task-id"' not in prompt
    assert '"team"' not in prompt
    assert '"role"' not in prompt
    assert '"source-role"' not in prompt
    assert "Use @mcp-g8 or @mcp-docker on Linux, and @mcp-thinkbook on Windows." in prompt
    assert "Do not claim tool or workspace access is unavailable until an actual tool call fails." in prompt
    assert "Establish the root cause before changing or judging code; prefer the least invasive complete solution, never a smaller patch that leaves the cause intact." in prompt
    assert "TURN_CONTEXT" not in prompt
    assert "allowed-routes" not in prompt
    assert '"route":"PLAN|DEV|REVIEW"' in prompt
    assert '"route":"PLAN|DEV|REVIEW|PAUSE|DONE"' not in prompt
    assert "PAUSE is exceptional, not a normal route" in prompt
    assert prompt.count('{"route":"') == 1
    assert prompt.endswith(FINAL_DIRECTIVE)


def test_followup_plan_prompt_has_late_explicit_closure_cue(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).build(
        task_title="Release F closure",
        task_id="task-plan-close",
        team="alpha",
        logical_role="PLAN",
        physical_role="alpha-plan",
        turn=12,
        allowed_routes=("PLAN", "DEV", "REVIEW", "DONE"),
        workspace=str(tmp_path),
        source_physical_role="alpha-review",
        handoff=".plan/alpha/alpha-review_turn9_task-plan-close.md",
        goal="Close the task after clean review when acceptance is satisfied.",
        expected_report_path=".plan/alpha/alpha-plan_turn12_task-plan-close.md",
        constructor_sent_generation=0,
        conversation_generation=0,
    ).text

    assert "Constructor for PLAN" not in prompt
    assert "TEST_BASE_CONTEXT" not in prompt
    assert PLAN_CLOSURE_CUE in prompt
    assert prompt.count('{"route":"') == 1
    assert len(prompt) <= 3200
    assert prompt.index(FINAL_DIRECTIVE) < prompt.index("PLAN_CLOSURE")
    assert prompt.endswith(PLAN_CLOSURE_CUE)
