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


def test_draining_endpoint_keeps_sending_hop_without_positive_pristine_evidence(tmp_path):
    worker, _store, first, _second, tasks = _setup(tmp_path)
    state = copy.deepcopy(tasks[0])
    hop = _active_hop(state)
    hop["state"] = "sending"
    hop["browser_endpoint_id"] = first["endpoint_id"]
    hop["ledger_path"] = ""
    worker.registry.tasks_by_id[state["task_id"]] = state

    assert worker._browser_endpoint_has_unresolved_execution(first["endpoint_id"]) is True


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

    assert seen == [("9222", first["endpoint_id"])]


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


def test_unbound_control_role_uses_selected_execution_endpoint_gate(tmp_path, monkeypatch):
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
    assert observed == [second["endpoint_id"]]
    assert first["endpoint_id"] != second["endpoint_id"]
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


def test_bootstrap_prewarm_releases_task_admission_gate_before_send(tmp_path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    catalog = BootstrapCatalog(tmp_path)
    bootstrap = catalog.upsert(
        {
            "bootstrap_id": "general-team-bootstrap",
            "name": "General Team Bootstrap",
            "description": "Reusable task-neutral context",
            "source_conversation_id": "11111111-1111-4111-8111-111111111111",
            "prewarm_prompt": "Load reusable bootstrap context only.",
            "max_backups": 7,
            "donors": [],
            "enabled": True,
            "tags": ["general"],
            "created_at": "2026-08-06T00:00:00+00:00",
            "updated_at": "2026-08-06T00:00:00+00:00",
        }
    )
    store = TaskStore(config)
    state = store.create_task(
        "regenerate bootstrap before admission",
        task_id="bootstrap-admission-boundary",
        requested_team="bootstrap-admission-boundary",
        bootstrap=bootstrap,
    )
    state["bootstrap_source_exhausted"] = True
    worker = CDPAWorker(config, store=store)
    donor = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }

    class GlobalClient:
        binding = SimpleNamespace(role="BOOTSTRAP")
        page = SimpleNamespace(url=f"https://chatgpt.com/c/{donor['conversation_id']}")

        async def new_chat(self, **_kwargs):
            return "https://chatgpt.com/"

        async def assert_ownership(self):
            return SimpleNamespace(
                page_role="BOOTSTRAP",
                page_task_id=None,
                page_team=None,
                url=f"https://chatgpt.com/c/{donor['conversation_id']}",
            )

    class Actions:
        async def locate_owned(self, _state, _role):
            return None

        async def acquire_global_role(self, physical_role, *, allow_create=True):
            assert physical_role == "BOOTSTRAP"
            assert allow_create is True
            return AcquiredRole(
                client=GlobalClient(),
                page_id="bootstrap-global",
                url="https://chatgpt.com/",
                created=True,
                new_chat=False,
            )

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            current = asyncio.current_task()
            assert current in worker._endpoint_admission_holds
            assert role == "PLAN"
            assert (source_conversation_id, assistant_message_id) == (
                donor["conversation_id"],
                donor["assistant_message_id"],
            )
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="task-branch",
                url=f"https://chatgpt.com/c/{donor['conversation_id']}",
                created=True,
                new_chat=True,
            )

    class FakeBlock:
        def __init__(self, _prompt, **_kwargs):
            pass

        async def run(self, _context):
            current = asyncio.current_task()
            assert current not in worker._endpoint_admission_holds
            return {
                "receipt": {"conversation_id": donor["conversation_id"]},
                "response": {
                    "message_id": donor["assistant_message_id"],
                    "role": "assistant",
                    "turn_id": "55555555-5555-4555-8555-555555555555",
                    "text": "bootstrap ready",
                    "attachments": [],
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeBlock)

    async def scenario():
        await worker._acquire_endpoint_admission(state, "PLAN")
        try:
            return await worker._acquire_workflow_role(state, "PLAN", Actions())
        finally:
            worker._release_endpoint_admission()

    acquired = asyncio.run(scenario())

    assert acquired is not None
    assert acquired.page_id == "task-branch"
    assert state["roles"]["PLAN"]["context_source"] == "bootstrap_donor"
    assert worker._endpoint_admission_holds == {}


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
    store.endpoints.update(_first["endpoint_id"], max_active_tabs=1)
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
    assert states[first["endpoint_id"]]["capacity"] == 2
    assert states[first["endpoint_id"]]["active_count"] == 0
    assert states[first["endpoint_id"]]["available_slots"] == 2
    assert states[first["endpoint_id"]]["pages"] == []
    assert states[second["endpoint_id"]]["health"] == "offline"


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
