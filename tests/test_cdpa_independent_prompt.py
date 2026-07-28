from __future__ import annotations

from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder


def event() -> dict:
    return {
        "event_key": "recovery:task-a:2:role_offline:abc",
        "trigger_type": "recovery",
        "occurred_at": "2026-07-26T00:00:00+00:00",
        "target_team": "alpha",
        "target_task_id": "task-a",
        "target_role": "DEV",
        "target_hop_id": 2,
        "failure_signature": "role_offline:abc",
        "occurrence_count": 3,
        "check_count": 1,
    }


def test_independent_prompt_constructor_rule_once_and_context_every_cycle(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    builder = PromptBuilder(config)

    first = builder.build_independent(
        agent_name="Maintainers",
        system_prompt="Use @mcp-g8 and recover the affected task directly.",
        task_id="agent-maintainers-g1",
        team="agent-maintainers",
        physical_role="agent-maintainers-agent",
        workspace=str(tmp_path),
        event=event(),
        cycle=1,
        max_cycles=5,
        constructor_sent_generation=None,
        conversation_generation=0,
    )
    later = builder.build_independent(
        agent_name="Maintainers",
        system_prompt="Use @mcp-g8 and recover the affected task directly.",
        task_id="agent-maintainers-g2",
        team="agent-maintainers",
        physical_role="agent-maintainers-agent",
        workspace=str(tmp_path),
        event=event(),
        cycle=2,
        max_cycles=5,
        constructor_sent_generation=0,
        conversation_generation=0,
    )

    assert first.constructor_included is True
    assert "Use @mcp-g8" in first.text
    assert "INDEPENDENT_AGENT_OPERATING_RULE" in first.text
    assert later.constructor_included is False
    assert "Use @mcp-g8" not in later.text
    assert "INDEPENDENT_AGENT_OPERATING_RULE" not in later.text
    assert '"trigger_type": "recovery"' in first.text
    assert '"target_team": "alpha"' in later.text
    assert '"occurrence_count": 3' in later.text
    assert '"cycle": 2' in later.text
    assert "allowed-routes" not in first.text
    assert "route JSON" not in first.text
    assert "role-report" not in first.text
    assert ".plan/" not in first.text


def test_independent_prompt_resends_constructor_after_generation_change(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    built = PromptBuilder(config).build_independent(
        agent_name="Monitor",
        system_prompt="Inspect all active work.",
        task_id="agent-monitor-g2",
        team="agent-monitor",
        physical_role="agent-monitor-agent",
        workspace=str(tmp_path),
        event={**event(), "trigger_type": "check_all"},
        cycle=1,
        max_cycles=1,
        constructor_sent_generation=0,
        conversation_generation=1,
    )

    assert built.constructor_included is True
    assert "Inspect all active work." in built.text
    assert "INDEPENDENT_AGENT_OPERATING_RULE" in built.text
