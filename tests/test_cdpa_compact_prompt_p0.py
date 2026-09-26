from __future__ import annotations

import asyncio
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions, setup_task


FINAL_DIRECTIVE = "Work first. Write the report, then return exactly one valid route JSON object."
ROOT = Path(__file__).resolve().parents[1]


PLAN_CLOSURE_CUE = (
    "If acceptance is satisfied after clean REVIEW, route DONE; otherwise route the smallest role with concrete remaining work."
)



def test_first_turn_normal_prompt_is_goal_route_report_only(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).build(
        task_title="Compact first turn",
        task_id="task-first-turn",
        team="alpha",
        logical_role="PLAN",
        physical_role="alpha-plan",
        turn=1,
        allowed_routes=("PLAN", "DEV", "REVIEW", "PAUSE", "DONE"),
        workspace=str(ROOT),
        source_physical_role=None,
        handoff="Initial task intake; no prior role handoff.",
        goal="Implement the compact prompt contract.",
        expected_report_path=".plan/alpha/alpha-plan_turn1_task-first-turn.md",
        constructor_sent_generation=None,
        conversation_generation=0,
    ).text

    assert prompt.startswith("You are PLAN for the team alpha | task-first-turn:")
    assert "Goal: Implement the compact prompt contract." in prompt
    assert "Handoff: Initial task intake; no prior role handoff." in prompt
    assert "Before replying, write a non-empty report to exactly: .plan/alpha/alpha-plan_turn1_task-first-turn.md" in prompt
    assert '"handoff":".plan/alpha/alpha-plan_turn1_task-first-turn.md"' in prompt
    assert "REPOSITORY_SKILLS" not in prompt
    assert "Constructor for PLAN" not in prompt
    assert "TEST_BASE_CONTEXT" not in prompt
    assert "Establish the root cause before changing or judging code" not in prompt
    assert len(prompt) <= 2200


def test_windows_tool_authority_is_session_generic_without_path_guessing(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).build(
        task_title="Tool routing",
        task_id="task-tool-routing",
        team="alpha",
        logical_role="DEV",
        physical_role="alpha-dev",
        turn=2,
        allowed_routes=("PLAN", "DEV", "REVIEW", "DONE"),
        workspace=r"E:\\git-project\\playwright-auto-multicdp",
        source_physical_role="alpha-plan",
        handoff=".plan/alpha/alpha-plan_turn1_task-tool-routing.md",
        goal="Use the configured Windows tool.",
        expected_report_path=".plan/alpha/alpha-dev_turn2_task-tool-routing.md",
        constructor_sent_generation=0,
        conversation_generation=0,
    ).text
    assert "use only tools authorized by the current task/session" in prompt
    assert "@md device g6" not in prompt
    assert "mcp-thinkbook" not in prompt
    assert "mcp-g8" not in prompt
    assert "mcp-docker" not in prompt


def test_linux_tool_authority_is_session_generic_without_path_guessing(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).build(
        task_title="No tools",
        task_id="task-no-tools",
        team="alpha",
        logical_role="DEV",
        physical_role="alpha-dev",
        turn=1,
        allowed_routes=("PLAN", "DEV", "REVIEW"),
        workspace="/srv/playwright-auto-multicdp",
        source_physical_role="alpha-plan",
        handoff=".plan/alpha/plan.md",
        goal="Do not infer tools.",
        expected_report_path=".plan/alpha/dev.md",
        constructor_sent_generation=0,
        conversation_generation=0,
    ).text

    assert "use only tools authorized by the current task/session" in prompt
    assert "mcp-thinkbook" not in prompt
    assert "mcp-g8" not in prompt
    assert "mcp-docker" not in prompt
    assert "md:g6" not in prompt


def test_long_continuation_uses_handoff_without_repeating_goal_tool_literal(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    literal = "@md g8"
    goal = "Continue the prior work. " + ("immutable evidence " * 500) + f" Require {literal}."
    prompt = PromptBuilder(config).build(
        task_title="Continuation",
        task_id="task-long-tool-literal",
        team="alpha",
        logical_role="DEV",
        physical_role="alpha-dev",
        turn=2,
        allowed_routes=("PLAN", "DEV", "REVIEW", "DONE"),
        workspace=str(tmp_path),
        source_physical_role="alpha-dev",
        handoff=".plan/alpha/alpha-dev_turn1_task-long-tool-literal.md",
        goal=goal,
        expected_report_path=".plan/alpha/alpha-dev_turn2_task-long-tool-literal.md",
        constructor_sent_generation=0,
        conversation_generation=1,
    ).text
    assert literal not in prompt
    assert "Goal:" not in prompt
    assert "Handoff: .plan/alpha/alpha-dev_turn1_task-long-tool-literal.md" in prompt
    assert "mcp-thinkbook" not in prompt
    assert "mcp-g6" not in prompt
    assert "mcp-docker" not in prompt


def test_explicit_md_g6_requirement_is_enforced_for_every_workflow_role(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    builder = PromptBuilder(config)

    for role in ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT"):
        prompt = builder.build(
            task_title="Exact G6 authority",
            task_id="task-exact-g6",
            team="alpha",
            logical_role=role,
            physical_role=f"alpha-{role.lower()}",
            turn=1,
            allowed_routes=("PLAN", "DEV", "REVIEW", "TEST", "AUDIT", "DONE"),
            workspace=r"E:\\git-project\\playwright-auto-multicdp",
            source_physical_role="alpha-plan",
            handoff=".plan/alpha/prior.md",
            goal="Use @md g6 for this repository/workspace.",
            expected_report_path=f".plan/alpha/{role.lower()}.md",
            constructor_sent_generation=0,
            conversation_generation=0,
        ).text

        assert "EXPLICIT_TOOL_REQUIREMENTS" in prompt
        assert "Preserve every exact user tool literal (ordered, deduplicated): @md g6" in prompt
        assert "do not substitute another connector, device, host, or path-derived target" in prompt


def test_worker_final_payload_preserves_all_explicit_literals_and_required_scope(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-worker-long-tool-literals"
    )
    state["task_text"] = (
        "Immutable request requires @md g8 and @mcp-thinkbook. "
        + ("immutable task detail " * 500)
    )
    state["goal_revisions"] = [
        {
            "applies_from_hop_id": 1,
            "goal": "Continue with md:g6 and @md device z99. " + ("current goal detail " * 500),
        }
    ]
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    expected = hop["expected_report_path"]
    absolute_report = Path(state["repository"]) / expected

    for literal in ("@md g8", "@mcp-thinkbook", "md:g6", "@md device z99"):
        assert literal in prompt
    assert "EXPLICIT_TOOL_REQUIREMENTS" in prompt
    assert str(absolute_report) in prompt
    assert f"Before replying, write a non-empty report to exactly: {expected}" in prompt
    assert f'"handoff":"{expected}"' in prompt
    assert '"route":"PLAN|DEV|TEST|REVIEW|AUDIT|DONE"' in prompt


def test_explicit_literal_section_preserves_many_exact_tokens(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    literals = " ".join(f"@mcp-device-{index:03d}-" + ("x" * 40) for index in range(40))

    prompt = PromptBuilder(config).build(
        task_title="Bounded literals",
        task_id="task-bounded-literals",
        team="alpha",
        logical_role="DEV",
        physical_role="alpha-dev",
        turn=1,
        allowed_routes=("PLAN", "DEV", "REVIEW"),
        workspace=str(tmp_path),
        source_physical_role=None,
        handoff=".plan/alpha/prior.md",
        goal=literals,
        expected_report_path=".plan/alpha/report.md",
        constructor_sent_generation=0,
        conversation_generation=1,
    ).text

    assert "@mcp-device-000-" in prompt
    assert "@mcp-device-039-" in prompt


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
    assert len(prompt) <= 1800
    assert "Goal:" not in prompt
    assert long_goal not in prompt
    assert long_title not in prompt
    assert "IMMUTABLE_TASK_DETAIL" not in prompt
    assert "Handoff: .plan/alpha/alpha-review_turn1_task-compact.md" in prompt
    assert "Constructor for DEV" not in prompt
    assert "TEST_BASE_CONTEXT" not in prompt
    assert "REPOSITORY_SKILLS" not in prompt
    assert "EXPLICIT_TOOL_REQUIREMENTS" not in prompt
    assert "Establish the root cause before changing or judging code" not in prompt
    assert "TURN_CONTEXT" not in prompt
    assert "allowed-routes" not in prompt
    assert '"route":"PLAN|DEV|REVIEW"' in prompt
    assert "PAUSE is exceptional:" in prompt
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
    assert prompt.endswith(PLAN_CLOSURE_CUE)
