from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import playwright_auto.cdpa_actions as actions_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import CDPATabActions
from playwright_auto.cdpa_config import load_cdpa_config
from test_cdpa_actions import FakeClient, FakeContext, FakePage, FakeWorkspace, manifest
from test_cdpa_core import write_config
from test_cdpa_worker import setup_task


def test_new_generation_closes_only_exact_role_not_team_peers(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    current = manifest()
    current["roles"]["DEV"] = {**current["roles"]["PLAN"], "physical_role": "DEV", "page_id": "dev-page"}
    plan = FakePage(page_id="old-plan", role="PLAN", team=current["team"], task_id=current["task_id"])
    dev = FakePage(page_id="dev-page", role="DEV", team=current["team"], task_id=current["task_id"])
    other = FakePage(page_id="unrelated", role="PLAN", team="other", task_id="other")
    context = FakeContext([plan, dev, other])
    monkeypatch.setattr(actions_module, "ChatGPTWorkspace", FakeWorkspace)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "random_delay", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(actions_module, "action_delay", lambda *_a, **_k: asyncio.sleep(0))
    acquired = asyncio.run(CDPATabActions(context, config).fresh_chat(current, "PLAN", url="https://chatgpt.com/?temporary-chat=true"))
    assert plan.closed
    assert not dev.closed
    assert not other.closed
    assert acquired.client.page not in [plan, dev, other]


def test_history_controller_never_evaluates_non_chatgpt_pages(tmp_path):
    _, _, _state, worker = setup_task(tmp_path)
    evaluated = []

    class Page:
        url = ""

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            evaluated.append(True)
            return ""

    asyncio.run(
        worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()]))
    )

    assert evaluated == []


def test_history_controller_skips_terminal_owned_pages(tmp_path, monkeypatch):
    _, _, state, worker = setup_task(tmp_path)
    state.update(status="DONE", terminal_state="DONE", active_role=None, active_hop_id=None)
    worker.registry = SimpleNamespace(tasks_by_id={state["task_id"]: state})
    maintained = []

    class Page:
        url = "https://chatgpt.com/c/terminal"
        def is_closed(self): return False
        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": state["task_id"], "pageId": "terminal-page", "role": "PLAN"
            })

    class Client:
        def __init__(self, page, **kwargs): self.page = page
        async def install_page_observer(self): pass

    async def maintain(client, wait): maintained.append(client.page)

    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    monkeypatch.setattr(worker.role_controller, "maintain_history", maintain)
    monkeypatch.setattr(
        "playwright_auto.role_runtime.page_observer.observer_for",
        lambda page: SimpleNamespace(dom_wake=asyncio.Event(), network_wake=asyncio.Event()),
    )
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page()])))
    assert maintained == []


def test_history_controller_does_not_approve_foreign_or_retired_execution_tools(tmp_path, monkeypatch):
    _, _, state, worker = setup_task(tmp_path)
    state["hops"][0].update(state="abandoned", receipt={"binding": {"page_id": "retired"}})
    worker.registry = SimpleNamespace(tasks_by_id={state["task_id"]: state})
    maintained = []
    class Page:
        url = "https://chatgpt.com/c/test"
        def __init__(self, task_id, page_id):
            self.task_id, self.page_id = task_id, page_id
        def is_closed(self):
            return False
        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({"taskId": self.task_id, "pageId": self.page_id, "role": "PLAN"})
    class Client:
        def __init__(self, page, **kwargs):
            self.page = page
        async def install_page_observer(self):
            pass
    async def maintain(client, wait):
        maintained.append(client.page.page_id)
    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    monkeypatch.setattr(worker.role_controller, "maintain_history", maintain)
    monkeypatch.setattr("playwright_auto.role_runtime.page_observer.observer_for", lambda page: SimpleNamespace(dom_wake=asyncio.Event(), network_wake=asyncio.Event()))
    asyncio.run(worker._maintain_ambient_page_automation(SimpleNamespace(pages=[Page(None, "unowned"), Page("foreign-task", "foreign"), Page(state["task_id"], "retired")])))
    assert maintained == []


@pytest.mark.parametrize("evidence", ["binding", "missing", "corrupt"])
@pytest.mark.parametrize("same_endpoint", [False, True])
def test_history_controller_skips_abandoned_ambiguous_page_from_durable_binding(tmp_path, monkeypatch, evidence, same_endpoint):
    from test_operator_resume_recovery import _prepare_sending_record
    _store, state, worker, _path, hop, _ledger, binding, _baseline = _prepare_sending_record(
        tmp_path, task_id="retired-ledger-binding"
    )
    hop["state"] = "abandoned"
    hop["receipt"] = None
    hop["browser_endpoint_id"] = "original"
    if evidence == "missing":
        Path(hop["ledger_path"]).unlink()
    elif evidence == "corrupt":
        Path(hop["ledger_path"]).write_text("null", encoding="utf-8")
    worker.registry = SimpleNamespace(tasks_by_id={state["task_id"]: state})
    maintained = []
    class Page:
        url = "https://chatgpt.com/c/test"
        def is_closed(self): return False
        async def evaluate(self, _script):
            return worker_module.WINDOW_NAME_PREFIX + json.dumps({
                "taskId": state["task_id"], "pageId": binding.page_id, "role": "PLAN"
            })
    class Client:
        def __init__(self, page, **kwargs): self.page = page
        async def install_page_observer(self): pass
    async def maintain(client, wait): maintained.append(client.page)
    monkeypatch.setattr(worker_module, "ChatGPTPage", Client)
    monkeypatch.setattr(worker.role_controller, "maintain_history", maintain)
    monkeypatch.setattr("playwright_auto.role_runtime.page_observer.observer_for", lambda page: SimpleNamespace(dom_wake=asyncio.Event(), network_wake=asyncio.Event()))
    page = Page()
    asyncio.run(worker._maintain_ambient_page_automation(
        SimpleNamespace(pages=[page]), endpoint_id="original" if same_endpoint else "peer"))
    assert maintained == ([] if same_endpoint else [page])
