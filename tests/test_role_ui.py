from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright_auto.role_indicator import (
    ROLE_BADGE_ID,
    ROLE_CONTROL_ID,
    ROLE_INDICATOR_SCRIPT,
)
import playwright_auto.role_ui_daemon as role_ui_daemon
from playwright_auto.role_ui_daemon import RoleUIDaemon, VisibleRoleState, supported_url


class FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def evaluate(self, script: str, arg: dict) -> bool:
        self.calls.append((script, arg))
        return True


class FakeEventSource:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event: str, value=None) -> None:
        for callback in self.handlers.get(event, []):
            callback(value) if value is not None else callback()


class FakeWatchedPage(FakeEventSource):
    def __init__(self) -> None:
        super().__init__()
        self.main_frame = object()


class FakeContext(FakeEventSource):
    def __init__(self, pages) -> None:
        super().__init__()
        self.pages = pages


class FakeBrowser(FakeEventSource):
    def __init__(self, contexts) -> None:
        super().__init__()
        self.contexts = contexts


def test_role_control_script_is_visible_editable_and_non_destructive():
    assert ROLE_BADGE_ID == "playwright-auto-role-badge-v3"
    assert ROLE_CONTROL_ID == "playwright-auto-role-control-v1"
    assert 'pointerEvents: "auto"' in ROLE_INDICATOR_SCRIPT
    assert 'cursor: "pointer"' in ROLE_INDICATOR_SCRIPT
    assert "setRole" in ROLE_INDICATOR_SCRIPT
    assert "releaseRole" in ROLE_INDICATOR_SCRIPT
    assert "Changing role keeps the current chat, task, draft, and page ID" in ROLE_INDICATOR_SCRIPT
    assert "sessionStorage.removeItem(TASK_ID_KEY)" not in ROLE_INDICATOR_SCRIPT
    assert "window.setInterval(apply, 5000)" in ROLE_INDICATOR_SCRIPT
    assert "window.setInterval(apply, 500)" not in ROLE_INDICATOR_SCRIPT


def test_supported_url_is_fail_closed():
    assert supported_url("https://chatgpt.com/")
    assert supported_url("https://auth.openai.com/login")
    assert not supported_url("https://example.com/")
    assert not supported_url("not a url")



def test_binding_reader_installs_once_then_uses_short_reader(tmp_path: Path):
    daemon = RoleUIDaemon(event_log=tmp_path / "events.jsonl")

    class Page:
        def __init__(self):
            self.installed = False
            self.calls = []

        async def evaluate(self, script, argument=None):
            self.calls.append((script, argument))
            if script == role_ui_daemon._BINDING_READ_SCRIPT:
                if not self.installed:
                    return None
                return {
                    "role": "DEV",
                    "pageId": "page-a",
                    "taskId": "task-a",
                    "title": "DEV",
                    "badgePresent": True,
                }
            assert script == role_ui_daemon._BINDING_INSTALL_SCRIPT
            self.installed = True
            return True

    page = Page()
    first = asyncio.run(daemon._read_binding(page))
    second = asyncio.run(daemon._read_binding(page))

    assert first == second
    assert [script for script, _ in page.calls].count(role_ui_daemon._BINDING_INSTALL_SCRIPT) == 1
    assert [script for script, _ in page.calls].count(role_ui_daemon._BINDING_READ_SCRIPT) == 3
    assert len(role_ui_daemon._BINDING_READ_SCRIPT) < 100

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


def test_unchanged_registry_is_not_republished_to_every_page(tmp_path: Path):
    daemon = RoleUIDaemon(event_log=tmp_path / "events.jsonl")
    page = FakePage()
    rows = [
        (page, VisibleRoleState("page-a", "DEV", "task", "https://chatgpt.com/", "a")),
    ]

    asyncio.run(daemon._publish_registry(rows))
    asyncio.run(daemon._publish_registry(rows))

    assert len(page.calls) == 1
    changed = [
        (page, VisibleRoleState("page-a", "REVIEW", "task", "https://chatgpt.com/", "a")),
    ]
    asyncio.run(daemon._publish_registry(changed))
    assert len(page.calls) == 2
    assert daemon.metrics["registry_publications"] == 2
    assert daemon.metrics["registry_noops"] == 1


def test_page_and_navigation_events_wake_before_reconciliation_timeout(tmp_path: Path):
    daemon = RoleUIDaemon(poll_seconds=60, event_log=tmp_path / "events.jsonl")
    page = FakeWatchedPage()
    context = FakeContext([page])
    browser = FakeBrowser([context])

    daemon._install_event_wakes(browser)
    assert not daemon._wake.is_set()

    page.emit("domcontentloaded")
    assert daemon._wake.is_set()
    asyncio.run(daemon._wait_for_change())
    assert daemon.metrics["event_wakes"] == 1

    daemon._wake.clear()
    new_page = FakeWatchedPage()
    context.emit("page", new_page)
    assert daemon._wake.is_set()
    assert id(new_page) in daemon._watched_pages


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
