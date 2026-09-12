from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoint_pool import EndpointPool
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from test_cdpa_core import write_config


async def _noop(*_args, **_kwargs):
    return None


def _setup(tmp_path: Path, *, task_count: int = 1):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    second = store.endpoints.add("9202")
    tasks = []
    for index in range(task_count):
        tasks.append(
            store.create_task(
                f"task {index}",
                task_id=f"task-{index}",
                requested_team=f"team-{index}",
                browser_endpoint_id=first["endpoint_id"],
            )
        )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for task in tasks:
        worker.registry.update_task(task, now=0.0)
    return worker, store, first, second, tasks


async def _pool(store: TaskStore):
    class Browser:
        def __init__(self, context):
            self.contexts = [context]
            self.connected = True

        def is_connected(self):
            return self.connected

    class Playwright:
        async def stop(self):
            return None

    async def connector(address):
        return Playwright(), Browser(SimpleNamespace(name=address.rsplit(":", 1)[-1], pages=[]))

    async def version(address):
        return {"webSocketDebuggerUrl": f"ws://test/{address.rsplit(':', 1)[-1]}"}

    async def auth(_context):
        return {"auth_state": "authenticated", "chatgpt_ready": True}

    pool = EndpointPool(
        store.endpoints,
        connector=connector,
        version_probe=version,
        auth_probe=auth,
        clock=lambda: 100.0,
    )
    await pool.refresh(wait=True)
    return pool


def _execution(endpoint_id, task_id):
    return {
        "endpoint_id": endpoint_id,
        "task_id": task_id,
        "team": task_id,
        "role": "DEV",
        "request_id": f"{task_id}-request",
        "page_id": f"{task_id}-page",
    }


def test_running_execution_set_counts_only_current_protected_role(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path, task_count=2)
    active = copy.deepcopy(tasks[0])
    idle = copy.deepcopy(tasks[1])
    active.update(status="RUNNING", active_role="PLAN")
    active["roles"]["PLAN"].update(
        browser_endpoint_id=second["endpoint_id"],
        page_id="page-plan",
        page_url="https://chatgpt.com/c/abc",
        online=True,
        status="active",
    )
    _active_hop(active)["state"] = "waiting"
    idle["roles"]["PLAN"].update(page_id="historical", online=False)

    running = worker._running_executions([active, idle])

    assert running == [
        {
            "endpoint_id": second["endpoint_id"],
            "task_id": active["task_id"],
            "team": active["team"],
            "role": "PLAN",
            "request_id": _active_hop(active)["request_id"],
            "page_id": "page-plan",
        }
    ]


def test_pre_send_relocation_keeps_same_request_and_clears_old_page_binding(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=first["endpoint_id"],
        page_id="old-page",
        page_url="https://chatgpt.com/",
        online=True,
        status="active",
    )
    request_id = _active_hop(state)["request_id"]

    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is True
    assert _active_hop(state)["request_id"] == request_id
    assert role["browser_endpoint_id"] == second["endpoint_id"]
    assert role["page_id"] is None
    assert role["page_url"] is None
    assert role["online"] is False


def test_accepted_or_ambiguous_send_is_not_relocated_as_pristine_work(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    state["roles"]["PLAN"]["browser_endpoint_id"] = first["endpoint_id"]
    hop = _active_hop(state)
    hop["state"] = "waiting"
    hop["receipt"] = {"accepted_via": "test"}

    assert worker._execution_can_relocate(state, second["endpoint_id"]) is False
    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is False
    assert state["roles"]["PLAN"]["browser_endpoint_id"] == first["endpoint_id"]


def test_pool_cycle_uses_free_peer_when_initial_endpoint_capacity_is_full(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    seen = []

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(
            worker,
            "_running_executions",
            lambda _tasks=None: [
                _execution(first["endpoint_id"], "busy-a"),
                _execution(first["endpoint_id"], "busy-b"),
            ],
        )

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            seen.append((Path(path).name, context.name, endpoint_id))
            return store.load(path)

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert len(seen) == 1
    assert seen[0][1:] == ("9202", second["endpoint_id"])
    assert store.load(tasks[0]["manifest_path"])["browser_endpoint_id"] == first["endpoint_id"]


def test_pool_exhaustion_waits_without_semantic_block(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    calls = []

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(
            worker,
            "_running_executions",
            lambda _tasks=None: [
                _execution(first["endpoint_id"], "a1"),
                _execution(first["endpoint_id"], "a2"),
                _execution(second["endpoint_id"], "b1"),
                _execution(second["endpoint_id"], "b2"),
            ],
        )

        async def advance(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert calls == []
    assert store.load(tasks[0]["manifest_path"])["status"] != "BLOCKED"


def test_endpoint_disconnect_does_not_abort_peer_execution(tmp_path, monkeypatch):
    worker, store, _first, _second, _tasks = _setup(tmp_path, task_count=2)
    seen = []

    async def scenario():
        pool = await _pool(store)

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            state = store.load(path)
            seen.append((state["task_id"], context.name))
            if context.name == "9222":
                raise ConnectionError("browser has been closed")
            return state

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert {task_id for task_id, _context in seen} == {"task-0", "task-1"}
    assert {context for _task_id, context in seen} == {"9222", "9202"}


def test_same_team_roles_can_record_different_execution_endpoints(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    state["roles"]["PLAN"]["browser_endpoint_id"] = first["endpoint_id"]
    state["active_role"] = "DEV"
    _active_hop(state)["target_role"] = "DEV"

    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is True
    assert state["roles"]["PLAN"]["browser_endpoint_id"] == first["endpoint_id"]
    assert state["roles"]["DEV"]["browser_endpoint_id"] == second["endpoint_id"]


def test_pool_inventory_keeps_capacity_and_flattened_compatibility_view(tmp_path):
    worker, _store, first, second, _tasks = _setup(tmp_path)
    ctx_a = SimpleNamespace(pages=[])
    runtime = {
        "default_endpoint_id": first["endpoint_id"],
        "endpoints": [
            {
                **first,
                "health": "online",
                "auth_state": "logged_in",
                "chatgpt_ready": True,
                "active_count": 2,
                "capacity": 2,
                "available_slots": 0,
                "running_executions": [_execution(first["endpoint_id"], "a")],
            },
            {
                **second,
                "health": "offline",
                "auth_state": "unknown",
                "chatgpt_ready": False,
                "active_count": 0,
                "capacity": 2,
                "available_slots": 2,
                "running_executions": [],
            },
        ],
    }

    asyncio.run(worker._publish_browser_pool_inventory({first["endpoint_id"]: ctx_a}, runtime))

    projection = worker._browser_projection
    assert projection is not None
    assert projection["connected"] is True
    assert projection["default_endpoint_id"] == first["endpoint_id"]
    states = {item["endpoint_id"]: item for item in projection["endpoints"]}
    assert states[first["endpoint_id"]]["active_count"] == 2
    assert states[first["endpoint_id"]]["available_slots"] == 0
    assert states[first["endpoint_id"]]["pages"] == []
    assert states[second["endpoint_id"]]["health"] == "offline"



def test_pool_forever_refreshes_one_shared_pool_instead_of_reconnecting_one_url(tmp_path, monkeypatch):
    worker, _store, _first, _second, _tasks = _setup(tmp_path)
    calls = []

    class Pool:
        def __init__(self):
            self.contexts = {}
            self.refresh_count = 0
        async def refresh(self, *, wait=False):
            self.refresh_count += 1
            calls.append(("refresh", wait))
            if self.refresh_count >= 2:
                self.contexts = {"endpoint": SimpleNamespace(pages=[])}
            return {"default_endpoint_id": None, "endpoints": []}
        def snapshot(self, **_kwargs):
            return {"default_endpoint_id": None, "endpoints": []}

    pool = Pool()

    async def run_once_pool(_pool):
        calls.append(("cycle", bool(_pool.contexts)))
        raise asyncio.CancelledError()

    monkeypatch.setattr(worker, "run_once_pool", run_once_pool)
    monkeypatch.setattr(worker, "_publish_browser_pool_inventory", _noop)

    try:
        asyncio.run(worker.run_pool_forever(pool))
    except asyncio.CancelledError:
        pass

    assert pool.refresh_count >= 2
    assert calls[0] == ("refresh", True)
    assert ("cycle", True) in calls



def test_rate_limit_cooldown_is_scoped_to_configured_account(tmp_path):
    worker, store, first, second, tasks = _setup(tmp_path)
    store.endpoints.update(first["endpoint_id"], rate_limit_scope="account-x")
    store.endpoints.update(second["endpoint_id"], rate_limit_scope="account-y")
    state_x = copy.deepcopy(tasks[0])
    state_x["roles"]["PLAN"]["browser_endpoint_id"] = first["endpoint_id"]
    state_y = copy.deepcopy(tasks[0])
    state_y["roles"]["PLAN"]["browser_endpoint_id"] = second["endpoint_id"]

    asyncio.run(worker._enter_rate_limit_cooldown(state_x, SimpleNamespace(), "rate limited"))

    assert worker._active_rate_limit_scopes() == {"account-x"}
    assert worker._rate_limit_gate_active(state=state_x) is True
    assert worker._rate_limit_gate_active(state=state_y) is False
