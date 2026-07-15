import asyncio
from pathlib import Path

from playwright_auto.runner import run_chatgpt_loop


def test_runner_returns_structured_loader_failure(tmp_path):
    path = tmp_path / "invalid.py"
    path.write_bytes(b"\xff\xfe\x00")

    code, payload = asyncio.run(run_chatgpt_loop(path))

    assert code == 2
    assert payload["status"] == "failed"
    assert payload["stage"] == "load_workflow"
    assert payload["error_type"] in {"SyntaxError", "UnicodeDecodeError"}
    assert payload["context"]["workflow_file"] == str(path)


def test_runner_fails_closed_when_workflow_contract_missing(tmp_path):
    path = tmp_path / "missing.py"
    path.write_text("VARIABLES = {}", encoding="utf-8")

    code, payload = asyncio.run(run_chatgpt_loop(path))

    assert code == 2
    assert payload["stage"] == "load_workflow"
    assert payload["error_type"] == "TypeError"
    assert "WORKFLOW" in payload["error"]


class FakePage:
    def __init__(self, url):
        self.url = url
        self.goto_calls = []
        self.closed = False

    async def goto(self, url, wait_until="domcontentloaded"):
        self.goto_calls.append((url, wait_until))
        self.url = url

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = []

    async def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        self.created.append(page)
        return page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]


def test_runner_never_navigates_unrelated_existing_tab(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    import playwright_auto.runner as runner_module

    workflow = tmp_path / "flow.py"
    workflow.write_text(
        """
from playwright_auto.workflow_api import *
WORKFLOW = Workflow('safe', [SetVariableBlock('done', True)])
LOOP = {'max_iterations': 1}
""",
        encoding="utf-8",
    )
    unrelated = FakePage("https://example.com/important")
    context = FakeContext([unrelated])

    @asynccontextmanager
    async def fake_connected(_url):
        yield FakeBrowser(context)

    monkeypatch.setattr(runner_module, "connected_browser", fake_connected)

    code, payload = asyncio.run(runner_module.run_chatgpt_loop(workflow))

    assert code == 0
    assert payload["status"] == "completed"
    assert unrelated.url == "https://example.com/important"
    assert unrelated.goto_calls == []
    assert len(context.created) == 1
    assert context.created[0].url == "https://chatgpt.com/"


def test_runner_reuses_existing_chatgpt_tab_without_touching_other_tabs(
    monkeypatch, tmp_path
):
    from contextlib import asynccontextmanager

    import playwright_auto.runner as runner_module

    workflow = tmp_path / "flow.py"
    workflow.write_text(
        """
from playwright_auto.workflow_api import *
WORKFLOW = Workflow('safe', [SetVariableBlock('done', True)])
LOOP = {'max_iterations': 1}
""",
        encoding="utf-8",
    )
    unrelated = FakePage("https://example.com/important")
    chatgpt = FakePage("https://chatgpt.com/c/existing")
    context = FakeContext([unrelated, chatgpt])

    @asynccontextmanager
    async def fake_connected(_url):
        yield FakeBrowser(context)

    monkeypatch.setattr(runner_module, "connected_browser", fake_connected)

    code, _payload = asyncio.run(runner_module.run_chatgpt_loop(workflow))

    assert code == 0
    assert context.created == []
    assert unrelated.goto_calls == []
    assert chatgpt.goto_calls == []
