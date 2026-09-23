from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_team import is_active_team_owner
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker_endpoints import _setup, _pool, _noop, _execution


def quiet(worker, monkeypatch):
    monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
    monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
    monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)


def test_direct_create_without_independent_agents_claims_idle_pool_capacity(tmp_path, monkeypatch):
    worker, store, first, second, _ = _setup(tmp_path, task_count=0)
    quiet(worker, monkeypatch)
    assert store.discover() == []

    worker.runtime_db.enqueue_command(
        command_id="cmd-direct-intake",
        idempotency_key="direct-intake",
        kind="create_task",
        task_id="task-direct-intake",
        expected_task_version=None,
        payload={
            "task": "Normal task created directly from clarified chat intake",
            "requested_team": "direct-intake",
            "repository": str(tmp_path),
            "report_mode": "file",
        },
    )

    applied = worker._apply_next_command()
    assert applied["status"] == "applied"
    created = store.load_task_id("task-direct-intake")
    assert created is not None
    assert created.get("task_mode") != "independent"
    assert not any(item.get("task_mode") == "independent" for item in store.discover())
    # Model the next scheduler tick after the registry's bounded 0.5s admission grace.
    worker.registry.update_task(created, now=0.0)

    observed = []
    busy = [
        _execution(first["endpoint_id"], "busy-a"),
        _execution(first["endpoint_id"], "busy-b"),
    ]
    monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: busy)

    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        observed.append((store.load(path)["task_id"], endpoint_id))
        return store.load(path)

    monkeypatch.setattr(worker, "advance", advance)

    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
        finally:
            await pool.close()

    asyncio.run(run())

    assert observed == [("task-direct-intake", second["endpoint_id"])]
    assert store.load(created["manifest_path"])["browser_endpoint_id"] == first["endpoint_id"]


@pytest.mark.parametrize("unavailable", ["full", "offline", "auth", "cooldown"])
def test_resource_pressure_is_durable_waiting_and_resumes_same_execution(tmp_path, monkeypatch, unavailable):
    worker, store, first, second, tasks = _setup(tmp_path)
    task = tasks[0]
    request = _active_hop(task)["request_id"]
    quiet(worker, monkeypatch)
    seen = []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        current = store.load(path)
        seen.append((current["status"], _active_hop(current)["request_id"], endpoint_id))
        return current
    monkeypatch.setattr(worker, "advance", advance)
    async def run():
        pool = await _pool(store)
        try:
            if unavailable == "full":
                running = [_execution(endpoint["endpoint_id"], f"busy-{i}-{j}")
                           for i, endpoint in enumerate((first, second)) for j in range(2)]
                monkeypatch.setattr(worker, "_running_executions", lambda _tasks=None: running)
            elif unavailable == "offline":
                for endpoint in (first, second):
                    await pool.mark_unavailable(endpoint["endpoint_id"], "test partition")
            elif unavailable == "auth":
                for runtime in pool._runtimes.values():
                    runtime.chatgpt_ready = False
                    runtime.auth_state = "login_required"
            else:
                monkeypatch.setattr(worker, "_active_rate_limit_scopes", lambda: {first["rate_limit_scope"], second["rate_limit_scope"]})
            await worker.run_once_pool(pool)
            waiting = store.load(task["manifest_path"])
            assert waiting["status"] == "WAITING"
            assert waiting["waiting"]["reason"] == "browser_pool"
            assert waiting["block_code"] is None
            assert _active_hop(waiting)["request_id"] == request
            assert worker.registry.next_due_at() is not None
            assert is_active_team_owner(waiting)
            assert seen == []
            # Recover capacity without operator resume or creating another task.
            if unavailable == "full":
                running.clear()
            elif unavailable == "cooldown":
                monkeypatch.setattr(worker, "_active_rate_limit_scopes", lambda: set())
            else:
                await pool.refresh(wait=True)
            worker.registry.update_task(waiting, now=0)
            await worker.run_once_pool(pool)
            assert len(seen) == 1
            assert seen[0][:2] == (task["status"], request)
            assert store.load(task["manifest_path"])["waiting"].get("reason") is None
        finally:
            await pool.close()
    asyncio.run(run())


def test_waiting_for_pool_does_not_release_exact_team_queue_ownership(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    quiet(worker, monkeypatch)
    async def run():
        pool = await _pool(store)
        try:
            for endpoint in (first, second):
                await pool.mark_unavailable(endpoint["endpoint_id"], "offline")
            await worker.run_once_pool(pool)
            owner = store.load(tasks[0]["manifest_path"])
            assert owner["status"] == "WAITING"
            queued = store.create_task("next job", reuse_team=owner["team"], task_id="same-team-next")
            current, _ = store.refresh_scheduling(queued["manifest_path"], tasks=[owner, queued])
            assert current["status"] == "WAITING"
            assert current["queue"]["blocked_by_task_id"] == owner["task_id"]
        finally:
            await pool.close()
    asyncio.run(run())


def test_reserved_surface_is_visible_to_drain_before_page_exists_and_releases_on_error(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    quiet(worker, monkeypatch)
    observed = []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        running = worker._running_executions()
        assert len(running) == 1
        assert running[0]["task_id"] == tasks[0]["task_id"]
        assert running[0]["request_id"] == _active_hop(tasks[0])["request_id"]
        assert worker._browser_endpoint_has_running_execution(endpoint_id)
        removed = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint_id})
        assert removed["draining"] and not removed["removed"]
        assert worker._finalize_drained_endpoints() == []
        observed.append(endpoint_id)
        raise ValueError("test acquire failure")
    monkeypatch.setattr(worker, "advance", advance)
    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
            assert observed
            assert worker._running_executions() == []
            assert worker._finalize_drained_endpoints() == observed
        finally:
            await pool.close()
    asyncio.run(run())


def test_capacity_enforced_during_concurrent_page_allocation(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path, task_count=6)
    quiet(worker, monkeypatch)
    counts, peaks, seen = {}, {}, []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        counts[endpoint_id] = counts.get(endpoint_id, 0) + 1
        peaks[endpoint_id] = max(peaks.get(endpoint_id, 0), counts[endpoint_id])
        seen.append(store.load(path)["task_id"])
        await asyncio.sleep(0.01)
        counts[endpoint_id] -= 1
        return store.load(path)
    monkeypatch.setattr(worker, "advance", advance)
    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
        finally:
            await pool.close()
    asyncio.run(run())
    assert len(seen) == 2 and len(set(seen)) == 2
    assert peaks == {first["endpoint_id"]: 2}
    assert sum(store.load(task["manifest_path"])["status"] == "WAITING" for task in tasks) == 4
    assert worker._running_executions() == []


def test_initial_browser_is_preference_not_team_invariant(tmp_path, monkeypatch):
    worker, store, first, second, _ = _setup(tmp_path, task_count=0)
    task = store.create_task("preferred second", task_id="second-browser", browser_endpoint_id=second["endpoint_id"])
    worker.registry.update_task(task, now=0)
    quiet(worker, monkeypatch)
    observed = []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        observed.append(endpoint_id)
        return store.load(path)
    monkeypatch.setattr(worker, "advance", advance)
    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
        finally:
            await pool.close()
    asyncio.run(run())
    assert observed == [second["endpoint_id"]]
