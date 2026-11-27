"""Unready provider surfaces must not leave exact Open Tab controls pending."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker_endpoints import _noop, _pool, _setup


def _fixture(tmp_path, monkeypatch, *, outcome="owned", action="open_tab", status="BLOCKED"):
    worker, store, home, peer, tasks = _setup(tmp_path)
    store.endpoints.update(home["endpoint_id"], max_active_tabs=1)
    path = tasks[0]["manifest_path"]

    def configure(state):
        parent = _active_hop(state)
        parent["state"] = "routed"
        parent["receipt"] = {"user_message_id": "previously-accepted-user"}
        hop = worker._append_hop(state, source_role="PLAN", target_role="PLAN", handoff="continue")
        hop["browser_endpoint_id"] = home["endpoint_id"]
        state["roles"]["PLAN"].update(
            page_id="owned-page", page_url="https://chatgpt.com/c/owned",
            conversation_generation=1, online=True, status="pending",
        )
        hop["preboundary_recovery"] = {
            "phase": "recovery_required", "stage": "acquisition",
            "request_id": hop["request_id"], "attempts": 0,
        }
        state.update(
            status=status, kanban_column=status, active_action="blocked",
            block_code="preboundary_acquisition_recovery_required",
            block_reason="exact acquisition requires reconciliation",
        )
        if status == "PAUSED":
            state["pause_reason"] = "operator pause"

    store.update(path, configure)
    state = store.request_control(path, action, role="PLAN", reason="recover exact original tab",
                                  external_command_id="unready-tab-control")
    worker.registry.update_task(state, now=0.0)
    before = copy.deepcopy(state)
    calls = {"open": [], "allocation": 0, "send": 0, "close": 0}

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace()

    class Actions:
        def __init__(self, context, _config):
            self.browser_context = context

        async def locate_owned(self, _state, _role):
            if outcome == "missing":
                return None
            return AcquiredRole(
                Client(), "foreign-page" if outcome == "page_changed" else "owned-page",
                "https://chatgpt.com/c/foreign" if outcome == "conversation_changed"
                else "https://chatgpt.com/c/owned", False, False,
            )

        async def open_tab(self, acquired):
            await acquired.client.assert_ownership()
            calls["open"].append((self.browser_context.name, acquired.page_id))

        async def reopen(self, *_args, **_kwargs):
            calls["allocation"] += 1
            raise AssertionError("an unready Open Tab must not allocate a page")

        fresh_chat = reopen
        acquire = reopen
        restart = reopen
        new_chat = reopen

        async def close_team(self, *_args, **_kwargs):
            calls["close"] += 1
            raise AssertionError("diagnosis must preserve existing pages")

        async def preflight_team(self, *_args, **_kwargs):
            return []

    async def forbidden_send(*_args, **_kwargs):
        calls["send"] += 1
        raise AssertionError("Open Tab cannot advance Send while provider is unready")

    monkeypatch.setattr(worker_module, "CDPATabActions", Actions)
    monkeypatch.setattr(worker, "_pre_send", forbidden_send)
    monkeypatch.setattr(worker, "_sending", forbidden_send)
    monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
    monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
    monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
    return worker, store, home, peer, path, before, calls


async def _unready_pool(store, home, *, auth_state="provider_challenge", gate=None):
    pool = await _pool(store)

    async def auth(context):
        unready = context.name == home["cdp_url"].rsplit(":", 1)[-1]
        return {"auth_state": auth_state if unready else "authenticated",
                "chatgpt_ready": not unready}

    pool.auth_probe = auth
    await pool.refresh(wait=True)
    if gate == "draining":
        store.endpoints.remove(home["endpoint_id"], has_nonterminal_tasks=True)
    elif gate == "offline":
        await pool.mark_unavailable(home["endpoint_id"], ConnectionError("fixture offline"))
    return pool


@pytest.mark.parametrize("auth_state", ["provider_challenge", "login_required", "unknown"])
def test_unready_open_tab_finishes_on_exact_owned_page_without_send(tmp_path, monkeypatch, auth_state):
    worker, store, home, peer, path, before, calls = _fixture(tmp_path, monkeypatch)

    async def run():
        pool = await _unready_pool(store, home, auth_state=auth_state)
        await worker.run_once_pool(pool)
        record = next(e for e in pool.snapshot()["endpoints"] if e["endpoint_id"] == home["endpoint_id"])
        assert record["chatgpt_ready"] is False
        await pool.close()

    asyncio.run(run())
    after = store.load(path)
    control = after["controls"][-1]
    assert control["status"] == "applied", "connected exact Open Tab must not remain PENDING"
    assert calls["open"] == [(home["cdp_url"].rsplit(":", 1)[-1], "owned-page")]
    assert after["browser_endpoint_id"] == home["endpoint_id"] != peer["endpoint_id"]
    assert _active_hop(after)["request_id"] == _active_hop(before)["request_id"]
    assert _active_hop(after)["state"] == "pre_send"
    assert after["hops"][0]["receipt"] == before["hops"][0]["receipt"]
    assert calls["send"] == calls["allocation"] == calls["close"] == 0


@pytest.mark.parametrize("outcome", ["missing", "page_changed", "conversation_changed"])
def test_unready_open_tab_missing_identity_gets_terminal_diagnosis(tmp_path, monkeypatch, outcome):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch, outcome=outcome)

    async def run():
        pool = await _unready_pool(store, home)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(run())
    after = store.load(path)
    control = after["controls"][-1]
    assert control["status"] == "ineffective", "unsafe Open Tab needs a terminal diagnosis, not PENDING"
    assert "OPEN_ROLE_TAB" in str(control["result"])
    assert after["status"] == "BLOCKED"
    assert _active_hop(after) == _active_hop(before)
    assert after["hops"][0]["receipt"] == before["hops"][0]["receipt"]
    assert calls == {"open": [], "allocation": 0, "send": 0, "close": 0}


@pytest.mark.parametrize("action", ["new_chat", "restart_role"])
def test_unready_open_tab_exception_does_not_admit_other_browser_actions(tmp_path, monkeypatch, action):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch, action=action)

    async def run():
        pool = await _unready_pool(store, home)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(run())
    after = store.load(path)
    assert after["controls"][-1]["status"] == "requested"
    assert _active_hop(after) == _active_hop(before)
    assert calls == {"open": [], "allocation": 0, "send": 0, "close": 0}


@pytest.mark.parametrize("gate", ["draining", "offline", "capacity", "cooldown"])
def test_unready_open_tab_keeps_endpoint_gates_and_never_uses_peer(tmp_path, monkeypatch, gate):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch)
    if gate == "capacity":
        worker._execution_leases["another-execution"] = {
            "endpoint_id": home["endpoint_id"], "task_id": "another-execution",
            "team": "another-team", "role": "DEV", "request_id": "other-request",
            "page_id": "another-owned-page",
        }
    elif gate == "cooldown":
        monkeypatch.setattr(worker, "_active_rate_limit_scopes",
                            lambda: [home.get("rate_limit_scope") or "shared"])

    async def run():
        pool = await _unready_pool(store, home, gate=gate)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(run())
    assert store.load(path)["controls"][-1]["status"] == "requested"
    assert calls == {"open": [], "allocation": 0, "send": 0, "close": 0}


def test_unready_open_tab_cannot_release_operator_pause(tmp_path, monkeypatch):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch, status="PAUSED")

    async def run():
        pool = await _unready_pool(store, home)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(run())
    after = store.load(path)
    assert after["status"] == "PAUSED"
    assert after["pause_reason"] == "operator pause"
    assert calls["send"] == calls["allocation"] == 0
    assert _active_hop(after) == _active_hop(before)


def test_stale_unready_open_tab_dispatch_cannot_overtake_stop(tmp_path, monkeypatch):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch)
    expected_control = before["controls"][-1]["control_id"]
    stop_state = store.request_control(path, "stop", reason="operator stop",
                                       external_command_id="stop-wins")

    asyncio.run(worker.advance(
        path, SimpleNamespace(name="home", pages=[]),
        scheduling_tasks=[stop_state], endpoint_id=home["endpoint_id"],
        expected_existing_open_tab_control=expected_control,
    ))

    after = store.load(path)
    assert after["controls"][-1]["action"] == "stop"
    assert after["controls"][-1]["status"] == "requested"
    assert after["controls"][0]["status"] == "requested"
    assert _active_hop(after) == _active_hop(before)
    assert calls == {"open": [], "allocation": 0, "send": 0, "close": 0}


@pytest.mark.parametrize("draining", [True, False])
def test_unready_open_tab_dispatch_rechecks_endpoint_lifecycle(tmp_path, monkeypatch, draining):
    worker, store, home, _peer, path, before, calls = _fixture(tmp_path, monkeypatch)
    expected_control = before["controls"][-1]["control_id"]
    store.endpoints.remove(home["endpoint_id"], has_nonterminal_tasks=draining)

    asyncio.run(worker.advance(
        path, SimpleNamespace(name="home", pages=[]),
        scheduling_tasks=[before], endpoint_id=home["endpoint_id"],
        expected_existing_open_tab_control=expected_control,
    ))

    after = store.load(path)
    assert after["controls"][-1]["status"] == "requested"
    assert _active_hop(after) == _active_hop(before)
    assert calls == {"open": [], "allocation": 0, "send": 0, "close": 0}
