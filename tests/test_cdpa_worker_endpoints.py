from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_bootstraps import BootstrapCatalog
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
            "endpoint_id": first["endpoint_id"],
            "task_id": active["task_id"],
            "team": active["team"],
            "role": "PLAN",
            "request_id": _active_hop(active)["request_id"],
            "page_id": "page-plan",
        }
    ]


def test_pre_send_relocation_without_ledger_fails_closed_and_preserves_binding(tmp_path):
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

    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is False
    assert _active_hop(state)["request_id"] == request_id
    assert role["browser_endpoint_id"] == first["endpoint_id"]
    assert role["page_id"] == "old-page"
    assert role["page_url"] == "https://chatgpt.com/"
    assert role["online"] is True


def test_initial_unbound_pre_send_stays_on_team_endpoint(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    hop = _active_hop(state)
    role = state["roles"]["PLAN"]

    assert hop["state"] == "pre_send"
    assert not Path(hop["ledger_path"]).exists()
    assert worker._execution_endpoint_id(state) == first["endpoint_id"]
    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is False
    assert worker._prepare_execution_endpoint(state, first["endpoint_id"]) is True
    assert role["browser_endpoint_id"] == first["endpoint_id"]
    assert role.get("page_id") is None
    assert hop["request_id"]


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


def test_draining_endpoint_keeps_sending_hop_without_positive_pristine_evidence(tmp_path):
    worker, _store, first, _second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    hop = _active_hop(state)
    hop["state"] = "sending"
    hop["browser_endpoint_id"] = first["endpoint_id"]
    hop["ledger_path"] = ""
    worker.registry.tasks_by_id[state["task_id"]] = state

    assert worker._browser_endpoint_has_unresolved_execution(first["endpoint_id"]) is True


def test_pool_cycle_waits_when_team_endpoint_capacity_is_full(tmp_path, monkeypatch):
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

    assert seen == []
    blocked = store.load(tasks[0]["manifest_path"])
    assert blocked["browser_endpoint_id"] == first["endpoint_id"]
    assert blocked["status"] == "WAITING"
    assert blocked["waiting"]["reason"] == "browser_pool"


def test_required_workflow_endpoint_waits_instead_of_falling_back_to_free_peer(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    required = store.endpoints.add("9202")
    task = store.create_task(
        "Gmail child",
        requested_team="gmail-child",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    calls = []

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(
            worker,
            "_running_executions",
            lambda _tasks=None: [
                _execution(required["endpoint_id"], "busy-a"),
                _execution(required["endpoint_id"], "busy-b"),
            ],
        )

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            calls.append((Path(path).name, context.name, endpoint_id))
            return store.load(path)

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    saved = store.load(task["manifest_path"])
    assert calls == []
    assert saved["status"] == "WAITING"
    assert saved["waiting"]["reason"] == "browser_pool"
    assert saved["required_browser_endpoint_id"] == required["endpoint_id"]
    assert first["endpoint_id"] != required["endpoint_id"]


def test_release_required_endpoint_control_allows_normal_allocator_peer_fallback(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    peer = store.endpoints.resolve()
    preferred = store.endpoints.add("9202")
    task = store.create_task(
        "release hard pin",
        requested_team="release-hard-pin",
        browser_endpoint_id=preferred["endpoint_id"],
        required_browser_endpoint_id=preferred["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)

    async def scenario():
        pool = await _pool(store)
        running = [
            _execution(preferred["endpoint_id"], "busy-a"),
            _execution(preferred["endpoint_id"], "busy-b"),
        ]
        assert pool.select_endpoint(
            running,
            affinity_endpoint_id=preferred["endpoint_id"],
            hard_affinity=True,
        ) is None

        queued = store.request_control(
            task["manifest_path"],
            "release_required_browser_endpoint",
            reason="preferred endpoint unavailable",
        )
        assert await worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(task["manifest_path"]),
        ) is True
        saved = store.load(task["manifest_path"])
        assert saved["browser_endpoint_id"] == preferred["endpoint_id"]
        assert "required_browser_endpoint_id" not in saved

        assert pool.select_endpoint(
            running,
            affinity_endpoint_id=saved["browser_endpoint_id"],
            hard_affinity=False,
        ) == peer["endpoint_id"]
        await pool.close()

    asyncio.run(scenario())


def test_pool_cycle_applies_required_endpoint_release_from_browser_pool_wait_without_allocation(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    peer = store.endpoints.resolve()
    required = store.endpoints.add("9202")
    task = store.create_task(
        "release blocked hard pin",
        requested_team="release-blocked-hard-pin",
        browser_endpoint_id=required["endpoint_id"],
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    running = [
        _execution(required["endpoint_id"], "busy-a"),
        _execution(required["endpoint_id"], "busy-b"),
    ]
    advance_calls = []
    original_advance = worker.advance

    async def recording_advance(
        path, context, *, scheduling_tasks=None, endpoint_id=None, **kwargs
    ):
        advance_calls.append((getattr(context, "name", None), endpoint_id))
        return await original_advance(
            path,
            context,
            scheduling_tasks=scheduling_tasks,
            endpoint_id=endpoint_id,
            **kwargs,
        )

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: running)
        monkeypatch.setattr(worker, "advance", recording_advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        await worker.run_once_pool(pool)
        waiting = store.load(task["manifest_path"])
        assert waiting["status"] == "WAITING"
        assert waiting["waiting"]["reason"] == "browser_pool"
        assert waiting["required_browser_endpoint_id"] == required["endpoint_id"]
        assert _active_hop(waiting)["state"] == "pre_send"
        assert worker._execution_is_pristine_pre_send(waiting) is True
        assert advance_calls == []

        queued = store.request_control(
            task["manifest_path"],
            "release_required_browser_endpoint",
            reason="required endpoint has no capacity",
        )
        worker.registry.update_task(queued, now=0.0)
        await worker.run_once_pool(pool)

        saved = store.load(task["manifest_path"])
        control = saved["controls"][-1]
        assert advance_calls == [(None, None)]
        assert saved["browser_endpoint_id"] == required["endpoint_id"]
        assert "required_browser_endpoint_id" not in saved
        assert control["status"] == "applied"
        assert control["command_state"] == "APPLIED"
        assert control["result"]["postcondition"] == "required_browser_endpoint_released"
        assert pool.select_endpoint(
            running,
            affinity_endpoint_id=saved["browser_endpoint_id"],
            hard_affinity=False,
        ) == peer["endpoint_id"]
        await pool.close()

    asyncio.run(scenario())


def test_browser_dependent_control_does_not_bypass_required_endpoint_pool_wait(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    required = store.endpoints.add("9202")
    task = store.create_task(
        "keep browser control gated",
        requested_team="browser-control-gated",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    running = [
        _execution(required["endpoint_id"], "busy-a"),
        _execution(required["endpoint_id"], "busy-b"),
    ]
    advance_calls = []

    async def recording_advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        advance_calls.append((Path(path).name, getattr(context, "name", None), endpoint_id))
        return store.load(path)

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: running)
        monkeypatch.setattr(worker, "advance", recording_advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        await worker.run_once_pool(pool)
        waiting = store.load(task["manifest_path"])
        assert waiting["status"] == "WAITING"
        assert waiting["waiting"]["reason"] == "browser_pool"

        queued = store.request_control(
            task["manifest_path"],
            "open_tab",
            role="PLAN",
            reason="browser-dependent control remains gated",
        )
        worker.registry.update_task(queued, now=0.0)
        await worker.run_once_pool(pool)

        saved = store.load(task["manifest_path"])
        control = saved["controls"][-1]
        assert advance_calls == []
        assert saved["status"] == "WAITING"
        assert saved["waiting"]["reason"] == "browser_pool"
        assert saved["required_browser_endpoint_id"] == required["endpoint_id"]
        assert control["status"] == "requested"
        assert control["command_state"] == "PENDING"
        await pool.close()

    asyncio.run(scenario())


def test_later_release_does_not_bypass_earlier_browser_dependent_control(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    required = store.endpoints.add("9202")
    task = store.create_task(
        "preserve mixed control order",
        requested_team="mixed-control-order",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    running = [
        _execution(required["endpoint_id"], "busy-a"),
        _execution(required["endpoint_id"], "busy-b"),
    ]
    advance_calls = []
    original_advance = worker.advance

    async def recording_advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        advance_calls.append((getattr(context, "name", None), endpoint_id))
        return await original_advance(
            path,
            context,
            scheduling_tasks=scheduling_tasks,
            endpoint_id=endpoint_id,
        )

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: running)
        monkeypatch.setattr(worker, "advance", recording_advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        await worker.run_once_pool(pool)
        waiting = store.load(task["manifest_path"])
        assert waiting["status"] == "WAITING"
        assert waiting["waiting"]["reason"] == "browser_pool"

        first = store.request_control(
            task["manifest_path"],
            "open_tab",
            role="PLAN",
            reason="browser-dependent control queued first",
        )
        queued = store.request_control(
            first["manifest_path"],
            "release_required_browser_endpoint",
            reason="release queued second",
        )
        worker.registry.update_task(queued, now=0.0)
        await worker.run_once_pool(pool)

        saved = store.load(task["manifest_path"])
        open_control, release_control = saved["controls"][-2:]
        assert advance_calls == []
        assert saved["status"] == "WAITING"
        assert saved["waiting"]["reason"] == "browser_pool"
        assert saved["required_browser_endpoint_id"] == required["endpoint_id"]
        assert (open_control["status"], open_control["command_state"]) == (
            "requested",
            "PENDING",
        )
        assert (release_control["status"], release_control["command_state"]) == (
            "requested",
            "PENDING",
        )
        await pool.close()

    asyncio.run(scenario())


def test_stale_endpointless_release_admission_cannot_execute_later_browser_control(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    required = store.endpoints.add("9202")
    task = store.create_task(
        "bind endpointless admission to release control",
        requested_team="endpointless-control-race",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(task, now=0.0)
    running = [
        _execution(required["endpoint_id"], "busy-a"),
        _execution(required["endpoint_id"], "busy-b"),
    ]
    original_advance = worker.advance
    scheduled = []
    raced = False

    async def racing_advance(path, context, *, scheduling_tasks=None, endpoint_id=None, **kwargs):
        nonlocal raced
        scheduled.append((getattr(context, "name", None), endpoint_id))
        if not raced:
            raced = True

            def reject_release(current):
                release = next(
                    item
                    for item in current["controls"]
                    if item.get("action") == "release_required_browser_endpoint"
                    and item.get("status") == "requested"
                )
                release["status"] = "rejected"
                release["command_state"] = "REJECTED"
                release["result"] = "race: release withdrawn"
                release["applied_at"] = "2026-09-18T00:00:00+00:00"
                return current

            updated = store.update(path, reject_release)
            worker._remember_manifest(Path(path), updated)
            worker.registry.update_task(updated, now=0.0)
        return await original_advance(
            path,
            context,
            scheduling_tasks=scheduling_tasks,
            endpoint_id=endpoint_id,
            **kwargs,
        )

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: running)
        monkeypatch.setattr(worker, "advance", racing_advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        await worker.run_once_pool(pool)
        waiting = store.load(task["manifest_path"])
        assert waiting["status"] == "WAITING"
        assert waiting["waiting"]["reason"] == "browser_pool"

        release = store.request_control(
            task["manifest_path"],
            "release_required_browser_endpoint",
            reason="release queued first",
        )
        queued = store.request_control(
            release["manifest_path"],
            "open_tab",
            role="PLAN",
            reason="browser-dependent control queued second",
        )
        worker.registry.update_task(queued, now=0.0)
        await worker.run_once_pool(pool)

        saved = store.load(task["manifest_path"])
        release_control, open_control = saved["controls"][-2:]
        assert scheduled == [(None, None)]
        assert saved["status"] == "WAITING"
        assert saved["waiting"]["reason"] == "browser_pool"
        assert saved["required_browser_endpoint_id"] == required["endpoint_id"]
        assert (release_control["status"], release_control["command_state"]) == (
            "rejected",
            "REJECTED",
        )
        assert (open_control["status"], open_control["command_state"]) == (
            "requested",
            "PENDING",
        )
        await pool.close()

    asyncio.run(scenario())


def test_required_endpoint_preserves_accepted_send_exact_execution(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    required = store.endpoints.add("9202")
    task = store.create_task(
        "Gmail child",
        requested_team="gmail-accepted",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    state = copy.deepcopy(task)
    state.update(status="RUNNING", kanban_column="RUNNING", active_action="waiting_response")
    state["roles"]["PLAN"].update(
        browser_endpoint_id=first["endpoint_id"],
        page_id="accepted-page",
        page_url="https://chatgpt.com/c/accepted",
        online=True,
        status="active",
    )
    hop = _active_hop(state)
    hop["state"] = "waiting"
    hop["receipt"] = {"accepted_via": "test"}
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(state, now=0.0)
    seen = []

    async def scenario():
        pool = await _pool(store)

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            seen.append((context.name, endpoint_id))
            return state

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert seen == [("9202", required["endpoint_id"])]


def test_manual_job_required_endpoint_waits_without_pinning_agent_identity(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    required = store.endpoints.add("9202")
    agent = store.create_independent_agent(
        "Pool manager",
        system_prompt="Inspect only.",
    )
    agent = store.run_independent_now(
        agent["manifest_path"],
        instruction="Use Gmail.",
        required_browser_endpoint_id=required["endpoint_id"],
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(agent, now=0.0)
    calls = []

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(
            worker,
            "_running_executions",
            lambda _tasks=None: [
                _execution(required["endpoint_id"], "busy-a"),
                _execution(required["endpoint_id"], "busy-b"),
            ],
        )

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            calls.append((Path(path).name, context.name, endpoint_id))
            return store.load(path)

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    saved = store.load(agent["manifest_path"])
    assert calls == []
    assert saved["status"] == "WAITING"
    assert saved["waiting"]["reason"] == "browser_pool"
    assert saved["independent"]["browser_endpoint_id"] is None
    assert saved["independent"]["active_event"]["required_browser_endpoint_id"] == required["endpoint_id"]
    assert first["endpoint_id"] != required["endpoint_id"]


def test_pinned_independent_agent_waits_instead_of_relocating_to_free_peer(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    second = store.endpoints.add("9202")
    agent = store.create_independent_agent(
        "Pinned manager",
        system_prompt="Inspect only.",
        browser_endpoint_id=first["endpoint_id"],
    )
    agent = store.run_independent_now(agent["manifest_path"], instruction="Inspect now.")
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    worker.registry.update_task(agent, now=0.0)
    calls = []

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
            calls.append((Path(path).name, context.name, endpoint_id))
            return store.load(path)

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    saved = store.load(agent["manifest_path"])
    assert calls == []
    assert saved["status"] == "WAITING"
    assert saved["waiting"]["reason"] == "browser_pool"
    assert saved["independent"]["browser_endpoint_id"] == first["endpoint_id"]
    assert second["endpoint_id"] != first["endpoint_id"]


def test_same_endpoint_serializes_admission_and_overflow_auto_wakes(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    store.endpoints.update(endpoint["endpoint_id"], max_active_tabs=2)
    tasks = [
        store.create_task(
            f"serialized {index}",
            task_id=f"serialized-{index}",
            requested_team=f"serialized-team-{index}",
            required_browser_endpoint_id=endpoint["endpoint_id"],
        )
        for index in range(3)
    ]
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for task in tasks:
        worker.registry.update_task(task, now=0.0)

    in_admission = 0
    peak_admission = 0
    admitted = []
    persisted_with_gate = []
    original_persist_transport_result = worker._persist_transport_result

    def persist_transport_result(path, before, after):
        before_hop = _active_hop(before)
        after_hop = _active_hop(after)
        if (
            str(after.get("task_id") or "").startswith("serialized-")
            and before_hop.get("state") == "pre_send"
            and after_hop.get("state") == "sending"
        ):
            current = asyncio.current_task()
            assert current in worker._endpoint_admission_holds
            persisted_with_gate.append(str(after["task_id"]))
        return original_persist_transport_result(path, before, after)

    async def fake_pre_send(state, hop, _actions):
        nonlocal in_admission, peak_admission
        role = str(hop["target_role"])
        endpoint_id = state["roles"][role].get("browser_endpoint_id")
        in_admission += 1
        peak_admission = max(peak_admission, in_admission)
        try:
            await asyncio.sleep(0.03)
            record = state["roles"][role]
            record.update(
                browser_endpoint_id=endpoint_id,
                page_id=f"{state['task_id']}-page",
                page_url=f"https://chatgpt.com/c/{state['task_id']}",
                online=True,
                status="active",
            )
            hop["browser_endpoint_id"] = endpoint_id
            hop["conversation_url"] = record["page_url"]
            hop["state"] = "sending"
            state.update(status="RUNNING", kanban_column="PLAN", active_action="send")
            admitted.append(state["task_id"])
        finally:
            in_admission -= 1

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_pre_send", fake_pre_send)
        monkeypatch.setattr(worker, "_persist_transport_result", persist_transport_result)
        monkeypatch.setattr(worker, "_sending", _noop)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        await worker.run_once_pool(pool)
        first_pass = [store.load(task["manifest_path"]) for task in tasks]
        active = [state for state in first_pass if state["status"] == "RUNNING"]
        waiting = [state for state in first_pass if state["status"] == "WAITING"]
        assert len(active) == 2
        assert len(waiting) == 1
        assert waiting[0]["waiting"]["reason"] == "browser_pool"
        assert all(
            state["roles"]["PLAN"]["browser_endpoint_id"] == endpoint["endpoint_id"]
            for state in active
        )

        released_id = active[0]["task_id"]
        released = store.update(
            active[0]["manifest_path"],
            lambda current: current.update(
                status="PAUSED",
                kanban_column="PAUSED",
                active_action="paused",
            ),
        )
        worker.registry.update_task(released, now=0.0)

        await asyncio.sleep(0.55)
        await worker.run_once_pool(pool)
        after_wake = store.load(waiting[0]["manifest_path"])
        await pool.close()
        return released_id, waiting[0]["task_id"], after_wake

    released_id, waiting_id, after_wake = asyncio.run(scenario())

    assert peak_admission == 1
    assert admitted[:2] == ["serialized-0", "serialized-1"]
    assert persisted_with_gate[:2] == ["serialized-0", "serialized-1"]
    assert waiting_id == "serialized-2"
    assert released_id in {"serialized-0", "serialized-1"}
    assert after_wake["status"] == "RUNNING"
    assert after_wake["waiting"]["reason"] is None
    assert admitted[-1] == waiting_id


def test_midcycle_rescan_admits_browser_pool_waiter_before_running_set_drains(
    tmp_path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    store.endpoints.update(endpoint["endpoint_id"], max_active_tabs=2)
    tasks = [
        store.create_task(
            f"midcycle {index}",
            task_id=f"midcycle-{index}",
            requested_team=f"midcycle-team-{index}",
            required_browser_endpoint_id=endpoint["endpoint_id"],
        )
        for index in range(3)
    ]
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for task in tasks:
        worker.registry.update_task(task, now=0.0)

    releases = {task["task_id"]: asyncio.Event() for task in tasks}
    started = {task["task_id"]: asyncio.Event() for task in tasks}
    start_counts = {task["task_id"]: 0 for task in tasks}
    active = set()
    peak_active = 0

    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        nonlocal peak_active
        state = store.load(path)
        task_id = state["task_id"]
        start_counts[task_id] += 1
        active.add(task_id)
        peak_active = max(peak_active, len(active))
        started[task_id].set()
        try:
            await releases[task_id].wait()
            return store.load(path)
        finally:
            active.remove(task_id)

    async def wait_for_pool_wait(task):
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            state = store.load(task["manifest_path"])
            if (
                state["status"] == "WAITING"
                and (state.get("waiting") or {}).get("reason") == "browser_pool"
            ):
                return state
            await asyncio.sleep(0.01)
        raise AssertionError("overflow task did not enter WAITING/browser_pool")

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        cycle = asyncio.create_task(worker.run_once_pool(pool))
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    started["midcycle-0"].wait(),
                    started["midcycle-1"].wait(),
                ),
                timeout=2.0,
            )
            await wait_for_pool_wait(tasks[2])
            assert not started["midcycle-2"].is_set()

            releases["midcycle-0"].set()
            await asyncio.wait_for(started["midcycle-2"].wait(), timeout=1.5)

            assert not releases["midcycle-1"].is_set()
            assert not cycle.done()
            assert active == {"midcycle-1", "midcycle-2"}
        finally:
            for release in releases.values():
                release.set()
            await asyncio.wait_for(cycle, timeout=2.0)
            await pool.close()

    asyncio.run(scenario())

    assert start_counts == {
        "midcycle-0": 1,
        "midcycle-1": 1,
        "midcycle-2": 1,
    }
    assert peak_active == 2


def test_different_endpoints_admit_concurrently(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    second = store.endpoints.add("9202")
    store.endpoints.update(first["endpoint_id"], max_active_tabs=1)
    store.endpoints.update(second["endpoint_id"], max_active_tabs=1)
    tasks = [
        store.create_task(
            "first endpoint",
            task_id="parallel-first",
            requested_team="parallel-first",
            required_browser_endpoint_id=first["endpoint_id"],
        ),
        store.create_task(
            "second endpoint",
            task_id="parallel-second",
            requested_team="parallel-second",
            required_browser_endpoint_id=second["endpoint_id"],
        ),
    ]
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for task in tasks:
        worker.registry.update_task(task, now=0.0)

    global_active = 0
    global_peak = 0
    endpoint_active = {first["endpoint_id"]: 0, second["endpoint_id"]: 0}
    endpoint_peak = {first["endpoint_id"]: 0, second["endpoint_id"]: 0}

    async def fake_pre_send(state, hop, _actions):
        nonlocal global_active, global_peak
        role = str(hop["target_role"])
        endpoint_id = state["roles"][role]["browser_endpoint_id"]
        global_active += 1
        endpoint_active[endpoint_id] += 1
        global_peak = max(global_peak, global_active)
        endpoint_peak[endpoint_id] = max(
            endpoint_peak[endpoint_id], endpoint_active[endpoint_id]
        )
        try:
            await asyncio.sleep(0.05)
            state["roles"][role].update(
                page_id=f"{state['task_id']}-page",
                page_url=f"https://chatgpt.com/c/{state['task_id']}",
                online=True,
                status="active",
            )
            hop["state"] = "sending"
            state.update(status="RUNNING", kanban_column="PLAN", active_action="send")
        finally:
            endpoint_active[endpoint_id] -= 1
            global_active -= 1

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_pre_send", fake_pre_send)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert global_peak == 2
    assert endpoint_peak == {first["endpoint_id"]: 1, second["endpoint_id"]: 1}
    assert all(store.load(task["manifest_path"])["status"] == "RUNNING" for task in tasks)


def test_open_tab_reconciliation_uses_team_home_endpoint_not_stale_role_endpoint(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    task = tasks[0]

    def bind_lost_plan_page(state):
        role = state["roles"]["PLAN"]
        role.update(
            browser_endpoint_id=second["endpoint_id"],
            page_id="lost-plan-page",
            page_url="https://chatgpt.com/c/persisted-plan",
            online=False,
            status="pending",
        )
        state.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            active_action="blocked",
            block_code="execution_recovery_requires_reconciliation",
        )

    state = store.update(task["manifest_path"], bind_lost_plan_page)
    state = store.request_control(
        task["manifest_path"],
        "open_tab",
        role="PLAN",
        reason="reconcile persisted PLAN conversation",
        external_command_id="open-tab-original-endpoint",
    )
    worker.registry.update_task(state, now=0.0)
    selected = []

    async def fake_advance(path, _context, **kwargs):
        selected.append(kwargs.get("endpoint_id"))
        return store.load(path)

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "advance", fake_advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert first["endpoint_id"] != second["endpoint_id"]
    assert selected == [first["endpoint_id"]]


def test_open_tab_controls_share_endpoint_admission_gate(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    store.endpoints.update(endpoint["endpoint_id"], max_active_tabs=2)
    tasks = [
        store.create_task(
            f"control {index}",
            task_id=f"control-{index}",
            requested_team=f"control-team-{index}",
            required_browser_endpoint_id=endpoint["endpoint_id"],
        )
        for index in range(2)
    ]
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for index, task in enumerate(tasks):
        state = store.request_control(
            task["manifest_path"],
            "open_tab",
            role="PLAN",
            reason="admission test",
            external_command_id=f"control-command-{index}",
        )
        worker.registry.update_task(state, now=0.0)

    active = 0
    peak = 0

    async def fake_apply_control(
        state,
        _actions,
        _manifest_path=None,
        *,
        scheduling_tasks=None,
        persisted_result=None,
    ):
        nonlocal active, peak
        requested = next(
            item for item in state["controls"] if item.get("status") == "requested"
        )
        assert requested["action"] == "open_tab"
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.04)
        finally:
            active -= 1
        if persisted_result is not None:
            persisted_result.append(True)
        return True

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_apply_control", fake_apply_control)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert peak == 1


def test_unbound_control_role_cannot_escape_team_home_endpoint(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    controlled = store.request_control(
        tasks[0]["manifest_path"],
        "open_tab",
        role="DEV",
        reason="recover DEV on selected endpoint",
        external_command_id="control-selected-endpoint-gate",
    )
    worker.registry.update_task(controlled, now=0.0)
    observed = []

    async def fake_apply_control(
        state,
        _actions,
        manifest_path=None,
        *,
        scheduling_tasks=None,
        persisted_result=None,
    ):
        current = asyncio.current_task()
        assert current is not None
        held = worker._endpoint_admission_holds[current]
        observed.append(held[0])
        assert held[0] == second["endpoint_id"]
        control = next(
            item for item in state["controls"] if item.get("status") == "requested"
        )
        control["status"] = "applied"
        control["result"] = {"page_id": "dev-selected-endpoint"}
        control["applied_at"] = "2026-09-15T00:00:00+00:00"
        assert manifest_path is not None
        store.save(manifest_path, state)
        if persisted_result is not None:
            persisted_result.append(True)
        return True

    monkeypatch.setattr(worker, "_apply_control", fake_apply_control)

    result = asyncio.run(
        worker.advance(
            tasks[0]["manifest_path"],
            SimpleNamespace(pages=[]),
            endpoint_id=second["endpoint_id"],
        )
    )

    assert result is not None
    assert observed == []
    assert first["endpoint_id"] != second["endpoint_id"]
    assert result["browser_endpoint_id"] == first["endpoint_id"]
    assert worker._endpoint_admission_holds == {}


def test_independent_jobs_share_endpoint_admission_gate(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    store.endpoints.update(endpoint["endpoint_id"], max_active_tabs=2)
    agents = []
    for index in range(2):
        agent = store.create_independent_agent(
            f"Admission Agent {index}",
            system_prompt="Inspect only.",
        )
        agents.append(
            store.run_independent_now(
                agent["manifest_path"],
                instruction="Run one bounded check.",
                required_browser_endpoint_id=endpoint["endpoint_id"],
            )
        )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    assert worker.registry is not None
    for agent in agents:
        worker.registry.update_task(agent, now=0.0)

    active = 0
    peak = 0

    async def fake_pre_send(state, hop, _actions):
        nonlocal active, peak
        role = str(hop["target_role"])
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.04)
            state["roles"][role].update(
                page_id=f"{state['task_id']}-page",
                page_url="https://chatgpt.com/?temporary-chat=true",
                online=True,
                status="active",
            )
            hop["state"] = "sending"
            state.update(status="RUNNING", active_action="send")
        finally:
            active -= 1

    async def scenario():
        pool = await _pool(store)
        monkeypatch.setattr(worker, "_pre_send", fake_pre_send)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
        await worker.run_once_pool(pool)
        await pool.close()

    asyncio.run(scenario())

    assert peak == 1
    assert all(store.load(agent["manifest_path"])["status"] == "RUNNING" for agent in agents)


def test_lost_page_recovery_reopen_shares_endpoint_admission_gate(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    worker = CDPAWorker(config, store=store)
    states = []
    for index in range(2):
        task = store.create_task(
            f"recovery {index}",
            task_id=f"recovery-{index}",
            requested_team=f"recovery-team-{index}",
            required_browser_endpoint_id=endpoint["endpoint_id"],
        )
        state = copy.deepcopy(task)
        state.update(status="RUNNING", kanban_column="PLAN", active_action="wait_response")
        role = state["roles"]["PLAN"]
        role.update(
            browser_endpoint_id=endpoint["endpoint_id"],
            page_id=f"lost-{index}",
            page_url=f"https://chatgpt.com/c/recovery-{index}",
            online=False,
        )
        hop = _active_hop(state)
        hop["state"] = "waiting"
        hop["conversation_url"] = role["page_url"]
        hop["receipt"] = {"accepted_via": "test"}
        states.append(state)

    active = 0
    peak = 0

    class RecoveryActions:
        async def locate_owned(self, _state, _role):
            return None

        async def reopen(
            self,
            state,
            role,
            *,
            require_clean_ready=True,
            foreground=False,
        ):
            nonlocal active, peak
            assert require_clean_ready is False
            assert foreground is False
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.04)
                return AcquiredRole(
                    client=SimpleNamespace(),
                    page_id=f"reopened-{state['task_id']}",
                    url=state["roles"][role]["page_url"],
                    created=True,
                    new_chat=False,
                )
            finally:
                active -= 1

        async def wake(self, _acquired):
            return None

    async def recover(state):
        admitted = []
        try:
            acquired = await worker._owned_or_block(
                state, "PLAN", RecoveryActions(), admitted=admitted
            )
            assert acquired is not None
            assert admitted == [True]
            await asyncio.sleep(0.02)
        finally:
            worker._release_endpoint_admission()

    async def scenario():
        await asyncio.gather(*(recover(state) for state in states))

    asyncio.run(scenario())

    assert peak == 1
    assert worker._endpoint_admission_holds == {}


def test_legacy_bootstrap_metadata_is_ignored_for_new_role_allocation(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    bootstrap = BootstrapCatalog(tmp_path).upsert(
        {
            "bootstrap_id": "legacy-bootstrap",
            "name": "Legacy Bootstrap",
            "description": "Historical metadata only",
            "source_conversation_id": "11111111-1111-4111-8111-111111111111",
            "prewarm_prompt": "Do not run",
            "max_backups": 7,
            "donors": [],
            "enabled": True,
            "tags": [],
            "created_at": "2026-08-06T00:00:00+00:00",
            "updated_at": "2026-08-06T00:00:00+00:00",
        }
    )
    state = store.create_task(
        "legacy bootstrap metadata",
        task_id="legacy-bootstrap-allocation",
        requested_team="legacy-bootstrap-allocation",
        bootstrap=bootstrap,
    )
    worker = CDPAWorker(config, store=store)
    calls = []

    class Actions:
        async def locate_owned(self, _state, _role):
            return None

        async def fresh_chat(self, _state, role, *, url, clean_for_send=False):
            calls.append((role, url, clean_for_send))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="fresh-plan",
                url="https://chatgpt.com/c/fresh-plan",
                created=True,
                new_chat=True,
            )

    acquired = asyncio.run(worker._acquire_workflow_role(state, "PLAN", Actions()))

    assert acquired is not None
    assert acquired.page_id == "fresh-plan"
    assert calls == [("PLAN", "https://chatgpt.com/", True)]
    assert state["roles"]["PLAN"]["context_source"] == "fresh"


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
    worker, store, _first, _second, _tasks = _setup(tmp_path, task_count=1)
    store.endpoints.update(_first["endpoint_id"], max_active_tabs=1)
    peer = store.create_task(
        "peer task",
        task_id="task-1",
        requested_team="team-1",
        browser_endpoint_id=_second["endpoint_id"],
    )
    worker.registry.update_task(peer, now=0.0)
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


def test_same_team_roles_cannot_record_different_execution_endpoints(tmp_path):
    worker, _store, first, second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    state["roles"]["PLAN"]["browser_endpoint_id"] = first["endpoint_id"]
    state["active_role"] = "DEV"
    _active_hop(state)["target_role"] = "DEV"

    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is False
    assert worker._prepare_execution_endpoint(state, first["endpoint_id"]) is True
    assert state["browser_endpoint_id"] == first["endpoint_id"]
    assert state["roles"]["PLAN"]["browser_endpoint_id"] == first["endpoint_id"]
    assert state["roles"]["DEV"]["browser_endpoint_id"] == first["endpoint_id"]


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
                "running_executions": [
                    {
                        **_execution(first["endpoint_id"], "a"),
                        "page_id": None,
                    }
                ],
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
    assert states[first["endpoint_id"]]["capacity"] == 2
    assert states[first["endpoint_id"]]["active_count"] == 1
    assert states[first["endpoint_id"]]["available_slots"] == 1
    assert states[first["endpoint_id"]]["running_executions"][0]["task_id"] == "a"
    assert states[first["endpoint_id"]]["pages"] == []
    assert states[second["endpoint_id"]]["health"] == "offline"


def test_pool_inventory_keeps_durable_execution_when_page_observation_flickers(tmp_path):
    worker, _store, first, _second, _tasks = _setup(tmp_path)
    ctx = SimpleNamespace(pages=[])
    endpoint_id = first["endpoint_id"]
    worker._execution_leases["reserved"] = {
        **_execution(endpoint_id, "reserved"),
        "page_id": None,
    }
    stale = _execution(endpoint_id, "stale")

    def runtime():
        executions = worker._running_executions([])
        executions.append(stale)
        return {
            "default_endpoint_id": endpoint_id,
            "endpoints": [{
                **first,
                "health": "online",
                "auth_state": "logged_in",
                "chatgpt_ready": True,
                "active_count": len(executions),
                "capacity": 2,
                "available_slots": max(0, 2 - len(executions)),
                "running_executions": executions,
            }],
        }

    asyncio.run(worker._publish_browser_pool_inventory({endpoint_id: ctx}, runtime()))
    state = worker._browser_projection["endpoints"][0]
    assert [item["task_id"] for item in state["running_executions"]] == ["reserved", "stale"]
    assert state["running_executions"][0]["page_observed"] is True
    assert state["running_executions"][1]["page_observed"] is False
    assert state["active_count"] == 2
    assert state["available_slots"] == 0

    worker._execution_leases.pop("reserved")
    asyncio.run(worker._publish_browser_pool_inventory({}, runtime()))
    state = worker._browser_projection["endpoints"][0]
    assert [item["task_id"] for item in state["running_executions"]] == ["stale"]
    assert state["running_executions"][0]["page_observed"] is False
    assert state["active_count"] == 1
    assert state["available_slots"] == 1


def test_same_task_page_less_lease_is_not_masked_by_stale_recorded_page(tmp_path):
    worker, _store, first, _second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    state.update(status="RUNNING", active_role="PLAN")
    state["roles"]["PLAN"].update(
        browser_endpoint_id=first["endpoint_id"],
        page_id="stale-page",
        page_url="https://chatgpt.com/c/stale",
        online=True,
        status="active",
    )
    _active_hop(state)["state"] = "waiting"
    worker._execution_leases[state["task_id"]] = {
        "endpoint_id": first["endpoint_id"],
        "task_id": state["task_id"],
        "team": state["team"],
        "role": "PLAN",
        "request_id": _active_hop(state)["request_id"],
        "page_id": None,
    }
    executions = worker._running_executions([state])
    runtime = {
        "default_endpoint_id": first["endpoint_id"],
        "endpoints": [{
            **first,
            "health": "online",
            "auth_state": "logged_in",
            "chatgpt_ready": True,
            "active_count": len(executions),
            "capacity": 2,
            "available_slots": max(0, 2 - len(executions)),
            "running_executions": executions,
        }],
    }

    asyncio.run(
        worker._publish_browser_pool_inventory(
            {first["endpoint_id"]: SimpleNamespace(pages=[])},
            runtime,
        )
    )

    endpoint = worker._browser_projection["endpoints"][0]
    assert endpoint["active_count"] == 1
    assert endpoint["available_slots"] == 1
    assert [item["task_id"] for item in endpoint["running_executions"]] == [
        state["task_id"]
    ]
    assert endpoint["running_executions"][0]["page_id"] is None


def test_pool_cycle_publishes_page_less_lease_until_scheduling_task_finishes(
    tmp_path, monkeypatch
):
    worker, store, first, second, _tasks = _setup(tmp_path, task_count=2)
    store.endpoints.update(first["endpoint_id"], max_active_tabs=2)
    store.endpoints.remove(second["endpoint_id"], has_nonterminal_tasks=True)
    both_started = asyncio.Event()
    releases = {"task-0": asyncio.Event(), "task-1": asyncio.Event()}
    started = set()

    async def scenario():
        pool = await _pool(store)

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            state = store.load(path)
            task_id = state["task_id"]
            started.add(task_id)
            if len(started) == 2:
                both_started.set()
            await releases[task_id].wait()
            return state

        async def wait_for_active_count(expected):
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                snapshot = worker.runtime_db.get_snapshot("browser")
                if snapshot is not None:
                    endpoint = next(
                        item
                        for item in snapshot["payload"]["endpoints"]
                        if item["endpoint_id"] == first["endpoint_id"]
                    )
                    if endpoint["active_count"] == expected:
                        return endpoint
                await asyncio.sleep(0.01)
            raise AssertionError(f"browser projection did not reach active_count={expected}")

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        cycle = asyncio.create_task(worker.run_once_pool(pool))
        await both_started.wait()

        active_endpoint = await wait_for_active_count(2)
        assert active_endpoint["available_slots"] == 0
        assert sorted(
            item["task_id"] for item in active_endpoint["running_executions"]
        ) == ["task-0", "task-1"]
        assert all(
            item["page_id"] is None for item in active_endpoint["running_executions"]
        )

        releases["task-0"].set()
        partially_released = await wait_for_active_count(1)
        assert partially_released["available_slots"] == 1
        assert [item["task_id"] for item in partially_released["running_executions"]] == [
            "task-1"
        ]
        assert set(worker._execution_leases) == {"task-1"}

        releases["task-1"].set()
        await cycle

        released_endpoint = await wait_for_active_count(0)
        assert released_endpoint["available_slots"] == 2
        assert released_endpoint["running_executions"] == []
        assert worker._execution_leases == {}
        await pool.close()

    asyncio.run(scenario())


def test_pool_cycle_cancellation_clears_lease_and_marks_browser_capacity_unknown(
    tmp_path, monkeypatch
):
    worker, store, _first, _second, _tasks = _setup(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def scenario():
        pool = await _pool(store)

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            started.set()
            await release.wait()
            return store.load(path)

        async def wait_for_active_lease():
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                snapshot = worker.runtime_db.get_snapshot("browser")
                if snapshot is not None and worker._execution_leases:
                    payload = snapshot["payload"]
                    for endpoint in payload.get("endpoints") or []:
                        if endpoint.get("active_count") == 1:
                            return payload
                await asyncio.sleep(0.01)
            raise AssertionError("browser projection did not publish active lease")

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        cycle = asyncio.create_task(worker.run_once_pool(pool))
        await started.wait()
        active_payload = await wait_for_active_lease()
        assert active_payload["connected"] is True

        cycle.cancel()
        try:
            await cycle
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled pool cycle must propagate cancellation")

        assert worker._execution_leases == {}
        cancelled_snapshot = worker.runtime_db.get_snapshot("browser")
        assert cancelled_snapshot is not None
        cancelled_payload = cancelled_snapshot["payload"]
        assert cancelled_payload["connected"] is False
        assert cancelled_payload["page_count"] is None
        assert cancelled_payload["pages"] == []
        assert not cancelled_payload.get("endpoints")
        await pool.close()

    asyncio.run(scenario())


def test_pool_inventory_contains_unverified_managed_process_state(tmp_path):
    worker, _store, first, _second, _tasks = _setup(tmp_path)
    ctx = SimpleNamespace(pages=[])
    runtime = {
        "default_endpoint_id": first["endpoint_id"],
        "endpoints": [{
            **first,
            "management_mode": "managed-local",
            "health": "online",
            "auth_state": "logged_in",
            "chatgpt_ready": True,
            "active_count": 0,
            "capacity": 2,
            "available_slots": 2,
            "running_executions": [],
        }],
    }

    class Managed:
        def read_pid(self, _endpoint_id):
            raise AssertionError("inventory must not adopt process ownership directly")

        def process_state(self, _endpoint_id):
            return {
                "managed_running": None,
                "process_ownership": "unverified",
                "process_error": "stale receipt",
            }

    worker.managed_browsers = Managed()
    asyncio.run(worker._publish_browser_pool_inventory({first["endpoint_id"]: ctx}, runtime))
    state = worker._browser_projection["endpoints"][0]
    assert state["managed_running"] is None
    assert state["process_ownership"] == "unverified"
    assert state["process_error"] == "stale receipt"


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
        if _pool.contexts:
            raise asyncio.CancelledError()

    monkeypatch.setattr(worker, "run_once_pool", run_once_pool)
    monkeypatch.setattr(worker, "_publish_browser_pool_inventory", _noop)

    try:
        asyncio.run(worker.run_pool_forever(pool))
    except asyncio.CancelledError:
        pass

    assert pool.refresh_count >= 2
    assert calls[0] == ("refresh", True)
    assert ("cycle", False) in calls  # Offline pools still publish resource waiting.
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



def test_unpinned_team_first_placement_pins_endpoint_and_inherits_temporary_default(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    first = store.endpoints.resolve()
    second = store.endpoints.add("9202", temporary_default=True)
    state = store.create_task(
        "default placement",
        task_id="default-placement",
        requested_team="default-placement",
    )
    worker = CDPAWorker(config, store=store)

    assert state["endpoint_pinned"] is False
    assert state["conversation_mode_explicit"] is False
    assert worker._execution_endpoint_id(state) is None

    assert worker._prepare_execution_endpoint(state, second["endpoint_id"]) is True
    assert state["endpoint_pinned"] is True
    assert state["browser_endpoint_id"] == second["endpoint_id"]
    assert state["conversation_mode"] == "temporary"
    assert state["context_policy"] == "fresh"

    assert worker._prepare_execution_endpoint(state, first["endpoint_id"]) is False


def test_v11_first_endpoint_pin_is_atomic_before_send_without_context_policy_mutation(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    original = store.endpoints.resolve()
    selected = store.endpoints.add("9202", temporary_default=True)
    state = store.create_task("unpinned", task_id="v11-first-placement", requested_team="v11-first-placement")
    path = Path(state["manifest_path"])
    assert state["endpoint_pinned"] is False
    assert state["context_policy"] == "fresh"

    bound = store.pin_workflow_endpoint(path, selected["endpoint_id"], expected_updated_at=state["updated_at"])
    assert bound["endpoint_pinned"] is True
    assert bound["browser_endpoint_id"] == selected["endpoint_id"]
    assert bound["conversation_mode"] == "temporary"
    assert bound["context_policy"] == "fresh"
    assert bound["roles"]["PLAN"]["browser_endpoint_id"] == selected["endpoint_id"]
    assert store.load(path)["browser_endpoint_id"] == selected["endpoint_id"]
    assert _active_hop(bound)["browser_endpoint_id"] == selected["endpoint_id"]
    try:
        store.pin_workflow_endpoint(path, original["endpoint_id"])
    except ValueError:
        pass
    else:
        raise AssertionError("a pinned task cannot move to another endpoint")
    assert store.load(path)["browser_endpoint_id"] == selected["endpoint_id"]


def test_v11_first_endpoint_pin_rejects_page_and_durable_request(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    selected = store.endpoints.add("9202")
    state = store.create_task("pre-send", task_id="v11-pin-guard", requested_team="v11-pin-guard")
    path = Path(state["manifest_path"])
    state["roles"]["PLAN"]["page_id"] = "existing-page"
    state = store.save(path, state)
    try:
        store.pin_workflow_endpoint(path, selected["endpoint_id"])
    except ValueError:
        pass
    else:
        raise AssertionError("page identity must prevent relocation")
    assert store.load(path)["endpoint_pinned"] is False


def test_v11_endpoint_pin_admits_only_unattempted_prepared_ledger(tmp_path):
    from playwright_auto.durable import RequestLedger, RequestStatus

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.add("9202")
    state = store.create_task("prepared before endpoint admission", task_id="pin-prepared", requested_team="pin-prepared")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(role=hop["physical_role"], prompt="immutable prepared prompt", request_id=hop["request_id"])
    ledger.update(hop["request_id"], status=RequestStatus.PROMPT_SET)
    bound = store.pin_workflow_endpoint(path, endpoint["endpoint_id"])
    assert bound["endpoint_pinned"] is True
    assert bound["browser_endpoint_id"] == endpoint["endpoint_id"]
    assert ledger.get(hop["request_id"]).attempts == 0
    assert ledger.get(hop["request_id"]).status is RequestStatus.PROMPT_SET


def test_v11_endpoint_pin_does_not_cross_durable_send_intent(tmp_path):
    from playwright_auto.durable import RequestLedger

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    selected = store.endpoints.add("9202")
    state = store.create_task("send once", task_id="v11-ledger-pin", requested_team="v11-ledger-pin")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(role=hop["physical_role"], prompt="durable pre-send", request_id=hop["request_id"])
    from playwright_auto.durable import RequestStatus
    ledger.update(hop["request_id"], status=RequestStatus.SENDING, attempts=1)
    try:
        store.pin_workflow_endpoint(path, selected["endpoint_id"])
    except ValueError as exc:
        assert "Send intent" in str(exc)
    else:
        raise AssertionError("endpoint relocation must not cross durable Send identity")
    reloaded = store.load(path)
    assert reloaded["endpoint_pinned"] is False
    assert reloaded["browser_endpoint_id"] == state["browser_endpoint_id"]
    assert ledger.peek(hop["request_id"]) is not None


def test_v11_scheduler_commits_unpinned_endpoint_before_browser_pre_send(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    selected = store.endpoints.add("9202")
    state = store.create_task("work", task_id="v11-dispatch-atomic", requested_team="v11-dispatch-atomic")
    worker = CDPAWorker(config, store=store)
    path = Path(state["manifest_path"])
    admitted = []

    async def pre_send(task, hop, _actions):
        persisted = store.load(path)
        assert persisted["endpoint_pinned"] is True
        assert persisted["browser_endpoint_id"] == selected["endpoint_id"]
        assert persisted["context_policy"] == "fresh"
        assert persisted["roles"]["PLAN"]["browser_endpoint_id"] == selected["endpoint_id"]
        assert worker._execution_endpoint_id(task, "PLAN") == selected["endpoint_id"]
        admitted.append(hop["request_id"])
        task["active_action"] = "observed_pre_send"

    monkeypatch.setattr(worker, "_pre_send", pre_send)
    saved = asyncio.run(worker.advance(path, SimpleNamespace(pages=[]), endpoint_id=selected["endpoint_id"]))
    assert admitted == ["v11-dispatch-atomic-hop1"]
    assert saved["browser_endpoint_id"] == selected["endpoint_id"]
    assert saved["active_action"] == "observed_pre_send"


def test_pool_cycle_stale_registry_terminal_manifest_never_gets_capacity_lease(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    store.endpoints.remove(second["endpoint_id"], has_nonterminal_tasks=True)
    path = Path(tasks[0]["manifest_path"])

    def stop(current):
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "DONE_STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "stopped"
        return current

    stopped = store.update(path, stop)
    assert stopped["status"] == "STOPPED"
    assert worker.registry.tasks_by_id["task-0"]["status"] != "STOPPED"
    started = asyncio.Event()
    release = asyncio.Event()

    async def scenario():
        pool = await _pool(store)

        async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
            started.set()
            await release.wait()
            return store.load(path)

        monkeypatch.setattr(worker, "advance", advance)
        monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
        monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
        monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)

        cycle = asyncio.create_task(worker.run_once_pool(pool))
        await started.wait()
        await asyncio.sleep(0.05)
        assert worker._execution_leases == {}
        snapshot = worker.runtime_db.get_snapshot("browser")
        assert snapshot is not None
        endpoint = next(
            item for item in snapshot["payload"]["endpoints"]
            if item["endpoint_id"] == first["endpoint_id"]
        )
        assert endpoint["active_count"] == 0
        assert endpoint["running_executions"] == []

        release.set()
        await cycle
        await pool.close()

    asyncio.run(scenario())
