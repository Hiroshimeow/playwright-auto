from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playwright_auto.cdpa_browser_projection import build_browser_projection, inspect_page_metadata


class FakePage:
    def __init__(self, *, page_id="page-1", url="https://chatgpt.com/c/abc?token=secret"):
        self.page_id = page_id
        self.url = url
        self.scripts = []

    def is_closed(self):
        return False

    async def evaluate(self, script):
        self.scripts.append(script)
        return {
            "url": self.url,
            "title": "ChatGPT",
            "role": "DEV",
            "page_id": self.page_id,
            "task_id": "task-a",
            "team": "alpha",
        }


def test_metadata_inventory_uses_one_minimal_evaluation():
    page = FakePage()

    result = asyncio.run(inspect_page_metadata(page))

    assert len(page.scripts) == 1
    script = page.scripts[0]
    assert "data-message-author-role" not in script
    assert "composer" not in script.lower()
    assert "querySelector" not in script
    assert result == {
        "page_id": "page-1",
        "url": "https://chatgpt.com/c/abc",
        "title": "ChatGPT",
        "role": "DEV",
        "team": "alpha",
        "task_id": "task-a",
        "online": True,
        "supported": True,
    }


def test_browser_projection_is_stable_and_contains_no_chat_content():
    page = FakePage()
    context = SimpleNamespace(pages=[page])

    first = asyncio.run(build_browser_projection(context, previous=None))
    second = asyncio.run(build_browser_projection(context, previous=first))

    assert first == second
    serialized = str(first).lower()
    assert "message" not in serialized
    assert "composer" not in serialized
    assert "dialog" not in serialized
    assert "button" not in serialized
    assert first["page_count"] == 1
