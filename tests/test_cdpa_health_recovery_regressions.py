from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from test_cdpa_core import write_config


def test_required_temporary_endpoint_ownership_persists_before_pre_send_and_restart(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    default = store.endpoints.add("9202", is_default=True)
    required = store.endpoints.add("9212", temporary_default=True)
    task = store.create_task(
        "Temporary required endpoint",
        requested_team="temporary-required-endpoint",
        required_browser_endpoint_id=required["endpoint_id"],
        temporary=True,
    )
    path = Path(task["manifest_path"])
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    seen = []

    async def guarded_pre_send(state, hop, _actions):
        persisted = store.load(path)
        persisted_hop = _active_hop(persisted)
        assert persisted["roles"]["PLAN"]["browser_endpoint_id"] == required["endpoint_id"]
        assert persisted_hop["browser_endpoint_id"] == required["endpoint_id"]

        restarted = CDPAWorker(config, store=TaskStore(config))
        reloaded = restarted.store.load(path)
        reloaded_hop = _active_hop(reloaded)
        assert reloaded["roles"]["PLAN"]["browser_endpoint_id"] == required["endpoint_id"]
        assert reloaded_hop["browser_endpoint_id"] == required["endpoint_id"]
        assert reloaded["conversation_mode"] == "temporary"
        assert reloaded["status"] != "WAITING"
        assert (reloaded.get("waiting") or {}).get("reason") != "browser_pool"
        seen.append((persisted_hop["browser_endpoint_id"], reloaded_hop["browser_endpoint_id"]))

        state["roles"]["PLAN"].update(
            page_id="temporary-page",
            page_url="https://chatgpt.com/?temporary-chat=true",
            online=True,
        )
        hop["conversation_url"] = "https://chatgpt.com/?temporary-chat=true"

    monkeypatch.setattr(worker, "_pre_send", guarded_pre_send)

    result = asyncio.run(
        worker.advance(path, SimpleNamespace(pages=[]), endpoint_id=required["endpoint_id"])
    )

    assert task["browser_endpoint_id"] == required["endpoint_id"]
    assert default["endpoint_id"] != required["endpoint_id"]
    assert seen == [(required["endpoint_id"], required["endpoint_id"])]
    assert result["roles"]["PLAN"]["browser_endpoint_id"] == required["endpoint_id"]
    assert _active_hop(result)["browser_endpoint_id"] == required["endpoint_id"]
    assert result["roles"]["PLAN"]["page_url"] == "https://chatgpt.com/?temporary-chat=true"
