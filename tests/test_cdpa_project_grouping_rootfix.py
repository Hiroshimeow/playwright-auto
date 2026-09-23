from __future__ import annotations

import asyncio
from pathlib import Path

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from test_cdpa_core import write_config


def worker_for(tmp_path: Path) -> CDPAWorker:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return CDPAWorker(config, store=TaskStore(config))


def test_existing_remote_project_is_reused_and_conversation_is_attached(tmp_path, monkeypatch):
    worker = worker_for(tmp_path)
    repository = str(tmp_path / "private-vault-mobile")
    calls = []

    async def projects(_context):
        return [{"id": "g-p-existing", "name": "private-vault-mobile"}]

    async def create(_context, _name):
        raise AssertionError("existing project must be reused")

    async def attach(context, conversation_id, project_id):
        calls.append((context, conversation_id, project_id))

    monkeypatch.setattr(worker_module, "backend_projects", projects)
    monkeypatch.setattr(worker_module, "backend_create_project", create)
    monkeypatch.setattr(worker_module, "backend_set_conversation_project", attach)

    context = object()
    asyncio.run(worker._group_repository_project(repository, "conversation-1", context))

    assert calls == [(context, "conversation-1", "g-p-existing")]


def test_missing_remote_project_is_created_then_conversation_is_attached(tmp_path, monkeypatch):
    worker = worker_for(tmp_path)
    repository = str(tmp_path / "agent-mcp-device")
    calls = []

    async def projects(_context):
        return []

    async def create(context, name):
        calls.append(("create", context, name))
        return "g-p-created"

    async def attach(context, conversation_id, project_id):
        calls.append(("attach", context, conversation_id, project_id))

    monkeypatch.setattr(worker_module, "backend_projects", projects)
    monkeypatch.setattr(worker_module, "backend_create_project", create)
    monkeypatch.setattr(worker_module, "backend_set_conversation_project", attach)

    context = object()
    asyncio.run(worker._group_repository_project(repository, "conversation-2", context))

    assert calls == [
        ("create", context, "agent-mcp-device"),
        ("attach", context, "conversation-2", "g-p-created"),
    ]
