from __future__ import annotations

from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder


def test_normal_solo_and_independent_prompts_do_not_serialize_skill_catalog(tmp_path: Path):
    skill = tmp_path / ".skills" / "local-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: local-skill\ndescription: local\n---\nSECRET BODY\n",
        encoding="utf-8",
    )
    builder = PromptBuilder(load_cdpa_config(None, repository_root=tmp_path))

    normal = builder.build(
        task_title="skills", task_id="task-skills", team="skills", logical_role="DEV",
        physical_role="skills-dev", turn=1, allowed_routes=("DEV", "REVIEW"),
        workspace=str(tmp_path), source_physical_role=None, handoff="initial", goal="Use relevant skills.",
        expected_report_path=".plan/report.md",
        constructor_text="# DEV\n\nImplement the assigned change.",
    ).text
    solo = builder.build_solo(task_name="solo", logical_role="DEV", goal="Use relevant skills.", workspace=str(tmp_path)).text
    independent = builder.build_independent(
        agent_name="Agent", system_prompt="Inspect the task.", task_id="agent", team="agent",
        physical_role="agent", workspace=str(tmp_path),
        event={"event_key":"manual:1","trigger_type":"manual","occurred_at":"2026-09-26T00:00:00+00:00"},
        cycle=1, max_cycles=1, constructor_sent_generation=None, conversation_generation=0,
    ).text

    for text in (normal, solo, independent):
        assert "REPOSITORY_SKILLS" not in text
        assert "SECRET BODY" not in text
