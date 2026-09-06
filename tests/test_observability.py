from pathlib import Path

from playwright_auto.observability import (
    append_action_event,
    configure_action_event_log,
    mcp_allow_click_summary,
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
