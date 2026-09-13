from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_worker import CDPAWorker
from test_cdpa_core import write_config
from test_cdpa_endpoint_pool import make_pool


def worker_at(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    worker = CDPAWorker(config)
    worker.runtime_db.ensure_schema()
    return worker


def enqueue(worker, payload):
    worker.runtime_db.enqueue_command(command_id="add-probe", idempotency_key="add-probe", kind="browser_endpoint_add",
                                      task_id=None, expected_task_version=None, payload=payload)


def test_external_add_connects_and_probes_before_registration(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    events = []
    async def probe(address):
        assert not any(item["cdp_url"] == address for item in worker.store.endpoints.list())
        events.append("probe")
        return {"health": "online", "auth_state": "authenticated", "chatgpt_ready": True}
    monkeypatch.setattr(worker, "_probe_browser_endpoint", probe)
    apply = worker._apply_browser_endpoint_add
    def register(payload):
        events.append("register")
        return apply(payload)
    monkeypatch.setattr(worker, "_apply_browser_endpoint_add", register)
    enqueue(worker, {"address": "9202"})
    result = worker._apply_next_command()
    assert result["status"] == "applied"
    assert events == ["probe", "register"]
    assert result["result"]["probe"]["auth_state"] == "authenticated"


def test_offline_external_add_preserves_registry_and_offers_local_creation(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    before = worker.store.endpoints.snapshot()
    monkeypatch.setattr(worker, "_probe_browser_endpoint", AsyncMock(side_effect=ConnectionError("connection refused")))
    enqueue(worker, {"address": "9202"})
    result = worker._apply_next_command()
    assert result["status"] == "failed"
    assert "managed-local" in result["error"]
    assert worker.store.endpoints.snapshot() == before


def test_admission_probe_does_not_block_other_worker_coroutines(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(address):
            entered.set()
            await release.wait()
            return {"health": "online", "auth_state": "login_required", "chatgpt_ready": False}
        monkeypatch.setattr(worker, "_probe_browser_endpoint", probe)
        enqueue(worker, {"address": "9202"})
        command = worker._apply_next_command()
        assert command["status"] == "running"
        await entered.wait()
        assert worker.runtime_db.get_command("add-probe")["status"] == "running"
        release.set()
        await asyncio.gather(*worker._browser_command_tasks)
        command = worker.runtime_db.get_command("add-probe")
        assert command["status"] == "applied"
        assert command["result"]["probe"]["chatgpt_ready"] is False
    asyncio.run(run())


def test_remote_managed_add_has_no_filesystem_side_effects(tmp_path):
    worker = worker_at(tmp_path)
    worker.managed_browsers.prepare_profile = Mock()
    with pytest.raises(ValueError, match="local"):
        worker._apply_browser_endpoint_add({"address": "remote.example:9222", "management_mode": "managed-local",
                                           "profile_path": str(tmp_path / "unwanted")})
    worker.managed_browsers.prepare_profile.assert_not_called()
    assert not (tmp_path / "unwanted").exists()


def test_running_execution_prevents_address_retarget_under_same_endpoint_id(tmp_path):
    worker = worker_at(tmp_path)
    endpoint = worker.store.endpoints.resolve()
    worker._running_executions = lambda: [{"endpoint_id": endpoint["endpoint_id"]}]
    with pytest.raises(ValueError, match="running|drain"):
        worker._apply_browser_endpoint_update({"endpoint_id": endpoint["endpoint_id"], "cdp_url": "9302"})
    assert worker.store.endpoints.get(endpoint["endpoint_id"])["cdp_url"] == endpoint["cdp_url"]


def test_pool_probe_uses_existing_transport_without_registering_or_closing_browser(tmp_path):
    async def run():
        pool, registry, first, second, connections, _ = make_pool(tmp_path)
        await pool.refresh(wait=True)
        before = registry.snapshot()
        contexts = pool.contexts
        result = await pool.probe_address("http://127.0.0.1:9302")
        assert result["health"] == "online"
        assert result["auth_state"] == "authenticated"
        assert registry.snapshot() == before
        assert pool.contexts == contexts
        assert connections[-1][0].stops == 1
        assert all(pw.stops == 0 for pw, _ in connections[:-1])
        await pool.close()
    asyncio.run(run())
