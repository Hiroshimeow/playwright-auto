from pathlib import Path

from playwright_auto.observability import (
    append_action_event,
    append_live_event,
    configure_action_event_log,
    configure_live_event_log,
    live_event_log_path,
    live_event_logging_enabled,
    mcp_allow_click_summary,
    read_recent_live_events,
)


def test_mcp_allow_click_summary_counts_only_live_chatgpt_clicks(tmp_path: Path):
    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)

    append_action_event(
        "mcp_allow",
        "complete",
        page_url="about:blank",
        detail="Allow mcp-thinkbook for this conversation",
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/11111111-1111-4111-8111-111111111111",
        task_id="task-1",
        role="team-dev",
        detail="Allow mcp-thinkbook for this conversation",
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/22222222-2222-4222-8222-222222222222",
        task_id="task-2",
        role="team-test",
        detail="Allow mcp-docker for this conversation",
    )
    append_action_event(
        "send",
        "complete",
        page_url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333",
    )

    summary = mcp_allow_click_summary()

    assert summary["clicks"] == 2
    assert summary["by_tool"] == {"mcp-docker": 1, "mcp-thinkbook": 1}
    assert summary["by_task"] == {"task-1": 1, "task-2": 1}
    assert summary["latest_at"]


def test_live_event_log_filters_by_task_and_role(tmp_path: Path):
    old_log = live_event_log_path()
    old_enabled = live_event_logging_enabled()
    log = tmp_path / "live.jsonl"
    configure_live_event_log(log)
    try:
        append_live_event(
            "DOM",
            "wait_probe",
            page_url="https://chatgpt.com/c/one",
            page_id="page-1",
            role="DEV",
            task_id="task-1",
            team="alpha",
            changes={"stop_visible": {"from": False, "to": True}},
        )
        append_live_event(
            "LISTEN",
            "response_observation",
            page_url="https://chatgpt.com/c/two",
            page_id="page-2",
            role="alpha-review",
            task_id="task-1",
            team="alpha",
            request_id="req-2",
            generation=3,
            values={"coverage": "partial", "event_count": 2},
        )
        append_live_event(
            "CTRL",
            "state_transition",
            task_id="task-1",
            role="REVIEW",
            values={"active_action": "validate_route"},
        )
        append_live_event(
            "CTRL",
            "state_transition",
            task_id="task-2",
            role="DEV",
            values={"active_action": "wait_response"},
        )

        dev = read_recent_live_events(task_id="task-1", role="DEV", limit=20)
        review = read_recent_live_events(task_id="task-1", role="REVIEW", limit=20)
        all_task = read_recent_live_events(task_id="task-1", limit=20)

        assert [item["source"] for item in dev] == ["DOM"]
        assert [item["source"] for item in review] == ["LISTEN", "CTRL"]
        assert [item["source"] for item in all_task] == ["DOM", "LISTEN", "CTRL"]
        assert review[0]["request_id"] == "req-2"
        assert review[0]["generation"] == 3
        assert review[0]["team"] == "alpha"
    finally:
        configure_live_event_log(old_log, enabled=old_enabled)
