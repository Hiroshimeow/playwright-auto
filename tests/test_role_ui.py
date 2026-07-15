from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright_auto.role_indicator import (
    ROLE_BADGE_ID,
    ROLE_CONTROL_ID,
    ROLE_INDICATOR_SCRIPT,
)
from playwright_auto.role_ui_daemon import RoleUIDaemon, VisibleRoleState, supported_url


class FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def evaluate(self, script: str, arg: dict) -> bool:
        self.calls.append((script, arg))
        return True


def test_role_control_script_is_visible_editable_and_non_destructive():
    assert ROLE_BADGE_ID == "playwright-auto-role-badge-v3"
    assert ROLE_CONTROL_ID == "playwright-auto-role-control-v1"
    assert 'pointerEvents: "auto"' in ROLE_INDICATOR_SCRIPT
    assert 'cursor: "pointer"' in ROLE_INDICATOR_SCRIPT
    assert "setRole" in ROLE_INDICATOR_SCRIPT
    assert "releaseRole" in ROLE_INDICATOR_SCRIPT
    assert "Changing role keeps the current chat, task, draft, and page ID" in ROLE_INDICATOR_SCRIPT
    assert "sessionStorage.removeItem(TASK_ID_KEY)" not in ROLE_INDICATOR_SCRIPT


def test_supported_url_is_fail_closed():
    assert supported_url("https://chatgpt.com/")
    assert supported_url("https://auth.openai.com/login")
    assert not supported_url("https://example.com/")
    assert not supported_url("not a url")


def test_duplicate_role_is_marked_as_conflict(tmp_path: Path):
    daemon = RoleUIDaemon(event_log=tmp_path / "events.jsonl")
    first = FakePage()
    second = FakePage()
    rows = [
        (first, VisibleRoleState("page-a", "DEV", "task", "https://chatgpt.com/", "a")),
        (second, VisibleRoleState("page-b", "DEV", "task", "https://chatgpt.com/", "b")),
    ]

    asyncio.run(daemon._publish_registry(rows))

    for page in (first, second):
        assert len(page.calls) == 1
        payload = page.calls[0][1]
        assert payload["roles"] == ["DEV"]
        assert "Duplicate role DEV" in payload["conflict"]
        assert "page-a" in payload["conflict"]
        assert "page-b" in payload["conflict"]


def test_role_changes_are_written_to_audit_log(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    daemon = RoleUIDaemon(event_log=path)
    before = VisibleRoleState("page-a", "DEV", "task-1", "https://chatgpt.com/", "DEV")
    after = VisibleRoleState("page-a", "REVIEW", "task-1", "https://chatgpt.com/", "REVIEW")

    daemon._record_changes([(object(), before)])
    daemon._record_changes([(object(), after)])

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["page_seen", "role_changed"]
    assert events[-1]["previous_role"] == "DEV"
    assert events[-1]["role"] == "REVIEW"
    assert events[-1]["task_id"] == "task-1"
