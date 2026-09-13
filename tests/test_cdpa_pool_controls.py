from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import CDPATabActions, RoleOwnershipError
from test_cdpa_actions import FakeClient, FakeContext, FakePage
from test_cdpa_worker_endpoints import _setup, _pool, _noop


def test_selected_historical_role_control_uses_its_endpoint_not_active_role_endpoint(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = tasks[0]
    state["roles"]["PLAN"].update(browser_endpoint_id=first["endpoint_id"], page_id="plan")
    state["roles"]["DEV"].update(browser_endpoint_id=second["endpoint_id"], page_id="dev")
    state["active_role"] = "DEV"
    a, b = FakeContext([]), FakeContext([])
    worker._endpoint_pool = SimpleNamespace(contexts={first["endpoint_id"]: a, second["endpoint_id"]: b})
    selected = worker._control_actions(state, "PLAN", CDPATabActions(b, worker.config))
    assert selected.browser_context is a
    assert state["roles"]["PLAN"]["browser_endpoint_id"] == first["endpoint_id"]


def test_team_cleanup_preflights_both_endpoints_and_preserves_foreign_pages(tmp_path, monkeypatch):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = tasks[0]
    plan = FakePage(page_id="same-id", role=state["roles"]["PLAN"]["physical_role"], team=state["team"], task_id=state["task_id"])
    dev = FakePage(page_id="same-id", role=state["roles"]["DEV"]["physical_role"], team=state["team"], task_id=state["task_id"])
    foreign = FakePage(page_id="foreign", role="PLAN", team="foreign", task_id="unrelated")
    state["roles"]["PLAN"].update(browser_endpoint_id=first["endpoint_id"], page_id="same-id")
    state["roles"]["DEV"].update(browser_endpoint_id=second["endpoint_id"], page_id="same-id")
    a, b = FakeContext([plan, foreign]), FakeContext([dev])
    worker._endpoint_pool = SimpleNamespace(contexts={first["endpoint_id"]: a, second["endpoint_id"]: b})
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    monkeypatch.setattr(actions_module, "action_delay", _noop)
    async def run():
        actions = CDPATabActions(a, worker.config)
        pages = await worker._cleanup_preflight(state, actions)
        assert set(pages) == {plan, dev}
        assert await actions.close_team(state, preflighted_pages=pages) == 2
        assert await worker._cleanup_preflight(state, actions) == []
    asyncio.run(run())
    assert plan.closed and dev.closed
    assert not foreign.closed


def test_cleanup_never_claims_empty_when_a_recorded_endpoint_is_uninspectable(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = tasks[0]
    state["roles"]["PLAN"].update(browser_endpoint_id=first["endpoint_id"], page_id="owned")
    worker._endpoint_pool = SimpleNamespace(contexts={second["endpoint_id"]: FakeContext([])})
    with pytest.raises(RoleOwnershipError, match="unavailable"):
        asyncio.run(worker._cleanup_preflight(state, CDPATabActions(FakeContext([]), worker.config)))


def test_operator_stop_is_not_stranded_by_total_pool_outage(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    state = store.request_control(tasks[0]["manifest_path"], "stop", reason="operator stop during outage")
    worker.registry.update_task(state, now=0)
    for method in ("_maintain_ambient_page_automation", "_refresh_rate_limit_cooldown", "_close_idle_independent_tabs"):
        monkeypatch.setattr(worker, method, _noop)
    async def run():
        pool = await _pool(store)
        try:
            for endpoint in (first, second):
                await pool.mark_unavailable(endpoint["endpoint_id"], "injected outage")
            await worker.run_once_pool(pool)
        finally:
            await pool.close()
    asyncio.run(run())
    saved = store.load(state["manifest_path"])
    assert saved["status"] == "STOPPED"
    assert saved["controls"][-1]["status"] == "applied"
    assert saved["hops"][0]["request_id"] == state["hops"][0]["request_id"]
