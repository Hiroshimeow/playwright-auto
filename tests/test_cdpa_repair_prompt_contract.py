from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from test_cdpa_core import write_config


def test_route_repair_prompt_is_self_contained_for_new_conversation(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    prompt = PromptBuilder(config).repair(
        task_id="task-x",
        team="team-x",
        physical_role="team-x-audit",
        turn=3,
        validation_error="invalid route JSON",
        expected_report_path=".plan/team-x/team-x-audit_turn3_task-x.md",
        allowed_routes=("PLAN", "DEV", "REVIEW", "TEST", "AUDIT", "PAUSE"),
    )

    assert "invalid route JSON" in prompt
    assert "team-x-audit" in prompt
    assert ".plan/team-x/team-x-audit_turn3_task-x.md" in prompt
    assert '{"route":"PLAN|DEV|REVIEW|TEST|AUDIT|PAUSE","handoff":".plan/team-x/team-x-audit_turn3_task-x.md"}' in prompt
    assert "exactly the two string keys route and handoff" in prompt
    assert "No markdown fence" in prompt
    assert "Do not redo completed substantive work" in prompt
