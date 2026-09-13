from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task
from test_cdpa_worker_endpoints import _setup, _pool, _noop
from test_cdpa_temporary_workflow import TemporaryWorkflowActions
from test_operator_resume_recovery import _prepare_pristine_new_sending_record


def test_explicit_fresh_context_survives_manifest_round_trip(tmp_path):
    worker, store, _, _, _ = _setup(tmp_path)
    task = store.create_task("fresh roundtrip", task_id="fresh-roundtrip", context_policy="fresh")
    task["roles"]["PLAN"]["context_source"] = "fresh"
    saved = store.save(task["manifest_path"], task)
    assert store.load(saved["manifest_path"])["context_policy"] == "fresh"


@pytest.mark.parametrize("temporary", [False, True])
def test_cross_endpoint_recovery_survives_store_and_worker_restart(tmp_path, temporary):
    store, state, worker, path, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="recovery-roundtrip")
    # Simulate the creation-time policy, without mutating a persisted policy.
    if temporary:
        state["conversation_mode"] = "temporary"
        Path(path).write_text(__import__("json").dumps(state), encoding="utf-8")
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    state["roles"]["PLAN"].update(browser_endpoint_id=old_endpoint, online=False)
    original = hop["request_id"]
    assert worker._prepare_cross_endpoint_recovery(state, endpoint, reason="endpoint lost")
    saved = store.save(path, state)
    restored = store.load(path)
    assert restored["task_id"] == state["task_id"]
    assert _active_hop(restored)["recovery_of_request_id"] == original
    assert restored["conversation_mode"] == ("temporary" if temporary else "persistent")
    assert worker.hydrate_runtime(startup=False)["complete"]
    assert RequestLedger(hop["ledger_path"]).get(original).attempts == 1


def test_legacy_accepted_execution_cannot_be_misclassified_as_unassigned(tmp_path):
    store, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="legacy-accepted")
    state.pop("browser_endpoint_id", None)
    state["roles"]["PLAN"].pop("browser_endpoint_id", None)
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    before = copy.deepcopy(hop["receipt"])
    assert not worker._prepare_execution_endpoint(state, endpoint)
    assert hop["receipt"] == before
    assert worker._prepare_cross_endpoint_recovery(state, endpoint, reason="legacy endpoint lost")
    assert _active_hop(state)["request_id"] != hop["request_id"]


def test_pristine_new_ledger_request_can_relocate_without_changing_request(tmp_path):
    store, state, worker, _, hop, ledger, _ = _prepare_pristine_new_sending_record(tmp_path, task_id="pristine-relocation")
    state["roles"]["PLAN"]["browser_endpoint_id"] = store.endpoints.resolve()["endpoint_id"]
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    request = hop["request_id"]
    assert worker._prepare_execution_endpoint(state, endpoint)
    assert hop["request_id"] == request
    assert ledger.get(request).attempts == 0
    assert ledger.get(request).receipt is None


def test_lost_temporary_tab_creates_recovery_not_saved_conversation_reopen(tmp_path):
    store, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="temporary-lost")
    state["conversation_mode"] = "temporary"
    state["roles"]["PLAN"]["browser_endpoint_id"] = store.endpoints.resolve()["endpoint_id"]
    request = hop["request_id"]
    actions = TemporaryWorkflowActions()
    assert asyncio.run(worker._owned_or_block(state, "PLAN", actions)) is None
    assert state["status"] != "BLOCKED"
    assert _active_hop(state)["kind"] == "execution_recovery"
    assert _active_hop(state)["request_id"] != request
    assert RequestLedger(hop["ledger_path"]).get(request).attempts == 1


@pytest.mark.parametrize("unavailable", ["draining", "cooldown"])
def test_accepted_response_keeps_existing_endpoint_while_draining_or_cooling(tmp_path, monkeypatch, unavailable):
    worker, store, first, second, tasks = _setup(tmp_path)
    task = copy.deepcopy(tasks[0])
    task.update(status="RUNNING", active_role="PLAN")
    task["roles"]["PLAN"].update(browser_endpoint_id=first["endpoint_id"], page_id="owned", page_url="https://chatgpt.com/c/owned", online=True)
    _active_hop(task).update(state="waiting", receipt={"accepted_via": "test"})
    worker.registry.update_task(task, now=0)
    if unavailable == "draining":
        store.endpoints.remove(first["endpoint_id"], has_nonterminal_tasks=True)
    else:
        monkeypatch.setattr(worker, "_active_rate_limit_scopes", lambda: {first["rate_limit_scope"]})
    seen = []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        seen.append(endpoint_id)
        return task
    monkeypatch.setattr(worker, "advance", advance)
    monkeypatch.setattr(worker, "_maintain_ambient_page_automation", _noop)
    monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
    monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
        finally:
            await pool.close()
    asyncio.run(run())
    assert seen == [first["endpoint_id"]]


def test_ambient_disconnect_does_not_prevent_peer_work(tmp_path, monkeypatch):
    worker, store, first, second, tasks = _setup(tmp_path)
    async def maintain(context, *, endpoint_id=None):
        if endpoint_id == first["endpoint_id"]:
            raise ConnectionError("browser has been closed")
    seen = []
    async def advance(path, context, *, scheduling_tasks=None, endpoint_id=None):
        seen.append(endpoint_id)
        return store.load(path)
    monkeypatch.setattr(worker, "_maintain_ambient_page_automation", maintain)
    monkeypatch.setattr(worker, "advance", advance)
    monkeypatch.setattr(worker, "_refresh_rate_limit_cooldown", _noop)
    monkeypatch.setattr(worker, "_close_idle_independent_tabs", _noop)
    async def run():
        pool = await _pool(store)
        try:
            await worker.run_once_pool(pool)
        finally:
            await pool.close()
    asyncio.run(run())
    assert seen == [second["endpoint_id"]]


def test_pristine_prompt_set_ledger_request_can_relocate_without_replay(tmp_path):
    from playwright_auto.durable import RequestStatus
    store, state, worker, _, hop, ledger, _ = _prepare_pristine_new_sending_record(
        tmp_path, task_id="pristine-prompt-set-relocation"
    )
    original_endpoint = store.endpoints.resolve()["endpoint_id"]
    state["roles"]["PLAN"]["browser_endpoint_id"] = original_endpoint
    endpoint = store.endpoints.add("9202")["endpoint_id"]
    request = hop["request_id"]
    ledger.update(request, status=RequestStatus.PROMPT_SET)
    assert worker._prepare_execution_endpoint(state, endpoint)
    current = ledger.get(request)
    assert hop["request_id"] == request
    assert current.status is RequestStatus.PROMPT_SET
    assert current.attempts == 0
    assert current.receipt is None
