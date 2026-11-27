from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_endpoint_pool import EndpointPool
from playwright_auto.cdpa_projection import build_task_projection
from test_cdpa_worker import setup_task
from test_stopped_writer_proof import evidence
from test_stopped_writer_seal_owner import owned


CURRENT_BROWSER = "devtools:33333333-3333-4333-8333-333333333333"


class ReadOnlyTarget:
    def __init__(self, target_id, url):
        self.target_id, self.url, self.closed = target_id, url, False

    def is_closed(self):
        return self.closed


class ReadOnlySession:
    def __init__(self, context, page=None):
        self.context, self.page = context, page

    async def send(self, method, params=None):
        assert method == "Target.getTargetInfo", f"unexpected browser command: {method}"
        target = (params or {}).get("targetId")
        if self.page is not None:
            target = self.page.target_id
        self.context.reads.append(target)
        matches = [page for page in self.context.pages if page.target_id == target and not page.closed]
        if not matches:
            raise RuntimeError("target does not exist")
        return {"targetInfo": {"targetId": target, "type": "page", "url": matches[0].url}}

    async def detach(self):
        self.context.detaches += 1


class ReadOnlyContext:
    def __init__(self, pages):
        self.pages = pages
        self.reads, self.detaches = [], 0
        self.browser = self
        self.connected = True

    def is_connected(self):
        return self.connected

    async def new_browser_cdp_session(self):
        return ReadOnlySession(self)

    async def new_cdp_session(self, page):
        return ReadOnlySession(self, page)


class ReadOnlyPool:
    _physical_identity = staticmethod(EndpointPool._physical_identity)

    def __init__(self, context, endpoint):
        self.contexts = {endpoint: context}
        self.endpoint, self.physical, self.health = endpoint, CURRENT_BROWSER, "online"
        self.probes = []

    def snapshot(self, **_kwargs):
        return {"endpoints": [{"endpoint_id": self.endpoint, "health": self.health,
                              "physical_browser_id": self.physical, "chatgpt_ready": False}]}

    async def version_probe(self, address):
        self.probes.append(address)
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9202/devtools/browser/" +
                self.physical.removeprefix("devtools:")}


@pytest.fixture
def configured(tmp_path, owned, monkeypatch):
    owner, data, _, lease = owned
    (tmp_path / "workspace").mkdir()
    _, store, state, worker = setup_task(
        tmp_path / "workspace", task_id="task-stopped", roles=("DEV",),
        conversation_mode="persistent",
    )
    worker.runtime_db.close()
    worker.runtime_db = owner.db
    worker._writer_maintenance_lease_owner = owner
    store.endpoints.add("http://127.0.0.1:9202", endpoint_id="endpoint-original")
    data["task"]["team"] = state["team"]
    physical_role = state["roles"]["DEV"]["physical_role"]
    data["task"]["roles"]["DEV"]["physical_role"] = physical_role
    for record in data["request_records"].values():
        record["source_context"]["team"] = state["team"]
        record["role"] = physical_role
        record["binding"]["role"] = physical_role
        record["receipt"]["binding"]["role"] = physical_role
    for hop in data["task"]["hops"]:
        hop["physical_role"] = physical_role
        hop["receipt"]["binding"]["role"] = physical_role
    data["task"]["hops"] = [
        {**copy.deepcopy(state["hops"][0]), **hop, "turn": hop["hop_id"]}
        for hop in data["task"]["hops"]
    ]
    raw = copy.deepcopy(state)
    raw.update(copy.deepcopy(data["task"]))
    raw["roles"]["DEV"] = {**state["roles"]["DEV"], **raw["roles"]["DEV"], "turn": 2}
    raw["role_order"] = ["DEV"]
    raw["active_role"] = None
    raw["terminal_state"] = "STOPPED"
    raw["kanban_column"] = "DONE_STOPPED"
    state = store.save(state["manifest_path"], raw)
    path = Path(state["manifest_path"])
    ledger = path.with_name("requests.json")
    ledger.write_text(json.dumps({"version": 1, "records": data["request_records"]}), encoding="utf-8")
    worker.hydrate_runtime(startup=False)
    data["task"] = store.load(path)
    data["immutable_task"] = False
    data["command"]["expected_task_version"] = owner.db.get_task_version("task-stopped")
    data["command"]["payload"]["physical_browser_id"] = CURRENT_BROWSER
    for observation in data["observations"]:
        observation["physical_browser_id"] = CURRENT_BROWSER
    target = ReadOnlyTarget("existing-diagnostic-target", "https://chatgpt.com/c/saved-chat")
    other = ReadOnlyTarget("private-other-target", "https://chatgpt.com/c/unrelated")
    context = ReadOnlyContext([other, target])
    pool = ReadOnlyPool(context, "endpoint-original")
    worker._endpoint_pool = pool
    worker._runtime_ready = True
    seen = []

    async def observe(page, payload):
        assert page is target
        value = copy.deepcopy(data["observations"][min(len(seen), 1)])
        value["observed_at"] = time.time()
        seen.append(page.target_id)
        return value

    monkeypatch.setattr(worker, "_observe_stopped_writer_target", observe, raising=False)
    monkeypatch.setattr(worker, "_refresh_source_drift", lambda **_kwargs: {"drifted": False})
    return SimpleNamespace(
        worker=worker, store=store, owner=owner, data=data, lease=lease, state=state,
        path=path, ledger=ledger, target=target, other=other, context=context, pool=pool,
        seen=seen, observe=observe,
    )


def enqueue(case, key="maintenance-test", version=None):
    case.owner.db.enqueue_command(
        command_id=key, idempotency_key=key, kind="reconcile_stopped_writer",
        task_id="task-stopped",
        expected_task_version=(case.owner.db.get_task_version("task-stopped") if version is None else version),
        payload=case.data["command"]["payload"],
    )
    return key


def execute(case):
    return case.worker.dispatch_command_once()


def test_dispatch_releases_only_controller_authority_and_finishes_normal_stop(configured):
    c = configured
    ledger_before = c.ledger.read_bytes()
    before_pages = tuple(c.context.pages)
    requests_before = {hop["send_attempt_id"]: c.owner.db.get_command(hop["send_attempt_id"])
                       for hop in c.data["task"]["hops"]}
    key = enqueue(c)
    result = execute(c)
    assert result["status"] == "applied", result
    assert result["command_id"] == key and result["result"]["controller_writer_released"] is True
    assert c.owner.get_binding("binding-old")["binding_status"] == "sealed"
    saved = c.store.load(c.path)
    assert saved["status"] == "STOPPED"
    assert saved["cleanup"]["state"] == "STOP_RELEASED"
    assert saved["cleanup"]["phase"] == "writer_released"
    assert saved["controls"][0]["result"]["writer_cleanup_pending"] is False
    assert c.ledger.read_bytes() == ledger_before
    assert saved["hops"][-1]["state"] == "waiting" and saved["hops"][-1]["receipt"]
    assert all(c.owner.db.get_command(key) == value for key, value in requests_before.items())
    assert tuple(c.context.pages) == before_pages and all(not page.closed for page in before_pages)
    assert c.seen == [c.target.target_id, c.target.target_id]
    assert c.other.target_id not in c.context.reads
    assert len(c.pool.probes) >= 2
    assert not c.worker._endpoint_admission_holds
    detail = c.owner.db.get_task_detail("task-stopped")
    assert detail["writer_maintenance"]["released"] is True


def test_immutable_historical_task_is_projected_without_manifest_catalog_or_ledger_save(configured):
    c = configured
    historical_bytes = c.path.read_bytes()
    released = copy.deepcopy(c.state)
    released["cleanup"].update(state="STOP_RELEASED", phase="writer_released")
    released["controls"][0]["result"]["writer_cleanup_pending"] = False
    c.store.save(c.path, released)
    c.store.replace_task_and_rewire(
        "task-stopped", "Existing historical successor", reuse_team=False,
        rewire_children=True, restart_team=True, incident_id="historical-fixture",
    )
    # Reproduce the already-existing historical orphan from the old replacement bug.
    c.path.write_bytes(historical_bytes)
    catalog = json.loads(c.store.catalog_path.read_text(encoding="utf-8"))
    catalog["entries"][c.store._catalog_key(c.path)].update(
        c.store._catalog_entry(json.loads(historical_bytes))
    )
    c.store.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    c.worker.hydrate_runtime(startup=False)
    assert c.worker.runtime_degraded is False
    before = {p: p.read_bytes() for p in (c.path, c.ledger, c.store.catalog_path)}
    enqueue(c)
    result = execute(c)
    assert result["status"] == "applied", result
    assert result["result"]["proof"]["immutable_history"] is True
    assert all(p.read_bytes() == value for p, value in before.items())
    detail = c.owner.db.get_task_detail("task-stopped")
    assert detail["writer_maintenance"]["released"] is True
    assert detail["cleanup"]["phase"] == "writer_stop_pending"


@pytest.mark.parametrize("change", ["physical", "target", "context", "canonical", "disconnected"])
def test_browser_identity_race_after_observation_never_seals(configured, monkeypatch, change):
    c = configured

    async def race(page, payload):
        value = await c.observe(page, payload)
        if len(c.seen) == 2:
            if change == "physical":
                c.pool.physical = "devtools:44444444-4444-4444-8444-444444444444"
            elif change == "target":
                c.target.target_id = "replacement-target"
            elif change == "context":
                c.pool.contexts["endpoint-original"] = ReadOnlyContext([c.target])
            elif change == "canonical":
                c.target.url = "https://chatgpt.com/c/another-chat"
            else:
                c.context.connected = False
        return value

    monkeypatch.setattr(c.worker, "_observe_stopped_writer_target", race)
    before = c.ledger.read_bytes()
    enqueue(c)
    result = execute(c)
    assert result["status"] == "recovery_required", result
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
    assert c.store.load(c.path)["cleanup"]["phase"] == "writer_stop_pending"
    assert c.ledger.read_bytes() == before and not c.target.closed and not c.other.closed
    assert not c.worker._endpoint_admission_holds


@pytest.mark.parametrize("change", ["manifest", "stop", "ledger", "version", "accepted_attempt"])
def test_final_locked_evidence_detects_concurrent_changes(configured, monkeypatch, change):
    c = configured

    async def race(page, payload):
        value = await c.observe(page, payload)
        if len(c.seen) == 2:
            if change in {"manifest", "stop"}:
                def change_state(current):
                    if change == "manifest":
                        current["stop_reason"] = "Changed after observation"
                    else:
                        current["controls"][0]["confirmed"] = False
                    return current
                c.store.update(c.path, change_state)
            elif change == "ledger":
                saved = json.loads(c.ledger.read_text(encoding="utf-8"))
                saved["records"]["task-stopped-hop2"]["response"] = {"changed": True}
                c.ledger.write_text(json.dumps(saved), encoding="utf-8")
            elif change == "version":
                projection = build_task_projection(c.store.load(c.path), tasks=[c.store.load(c.path)])
                projection.detail["test_version_change"] = True
                c.owner.db.upsert_task_projections([projection])
            else:
                attempt, _ = c.owner.record_send_intent(
                    c.lease, idempotency_key="new-concurrent-accepted", text="new accepted work",
                    baseline_last_user_message_id="accepted-user-2", baseline_user_turn_count=2,
                )
                c.owner.transition_attempt(attempt["attempt_id"], "accepted")
        return value

    monkeypatch.setattr(c.worker, "_observe_stopped_writer_target", race)
    enqueue(c)
    result = execute(c)
    assert result["status"] == "recovery_required", result
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
    assert not c.target.closed and not c.other.closed


@pytest.mark.parametrize("problem", ["not_ready", "degraded", "source_drift", "endpoint_missing", "target_missing"])
def test_unavailable_admission_is_terminal_without_browser_fallback(configured, monkeypatch, problem):
    c = configured
    if problem == "not_ready":
        c.worker._runtime_ready = False
    elif problem == "degraded":
        c.worker.runtime_degraded = True
    elif problem == "source_drift":
        monkeypatch.setattr(c.worker, "_refresh_source_drift", lambda **_kwargs: {"drifted": True})
    elif problem == "endpoint_missing":
        c.pool.contexts.clear()
    else:
        c.context.pages.remove(c.target)
    enqueue(c)
    result = execute(c)
    assert result is not None and result["status"] in {"failed", "recovery_required"}, result
    assert not c.seen and c.owner.get_binding("binding-old")["binding_status"] == "active"
    assert not c.target.closed and not c.other.closed


def test_partial_coverage_never_calls_stop_cleanup_for_other_roles(configured, monkeypatch):
    c = configured
    c.owner.bind(
        binding_id="another-generation", provider_id="chatgpt_web", endpoint_id="endpoint-original",
        endpoint_cdp_url="http://127.0.0.1:9202", physical_browser_id="devtools:old-browser",
        page_target_identity="another-old-page", conversation_url="https://chatgpt.com/c/another-history",
        verified_new_chat=False, task_id="task-stopped", role_key="DEV",
    )

    async def forbidden(*_args, **_kwargs):
        pytest.fail("partial maintenance invoked Stop cleanup")

    monkeypatch.setattr(c.worker, "_continue_stop_writer_cleanup", forbidden)
    before = c.path.read_bytes()
    enqueue(c)
    result = execute(c)
    assert result["status"] == "applied", result
    assert c.owner.get_binding("binding-old")["binding_status"] == "sealed"
    assert c.owner.get_binding("another-generation")["binding_status"] != "sealed"
    assert c.path.read_bytes() == before
    detail = c.owner.db.get_task_detail("task-stopped")
    assert detail["writer_maintenance"]["state"] == "partial"


def test_crash_after_seal_replays_exact_command_audit_without_browser_or_manifest(configured, monkeypatch):
    c = configured
    key = enqueue(c)
    original_finish = c.owner.db.finish_command

    class SimulatedProcessCrash(BaseException):
        pass

    def crash(command_id, **kwargs):
        if command_id == key:
            raise SimulatedProcessCrash()
        return original_finish(command_id, **kwargs)

    monkeypatch.setattr(c.owner.db, "finish_command", crash)
    with pytest.raises(SimulatedProcessCrash):
        execute(c)
    assert c.owner.get_binding("binding-old")["binding_status"] == "sealed"
    before = {p: p.read_bytes() for p in (c.path, c.ledger, c.store.catalog_path)}
    reads = list(c.context.reads)
    # A projection update after the durable seal must not strand its audit replay.
    c.worker._publish_command_state(c.store.load(c.path))
    c.owner.db.requeue_running_commands()
    monkeypatch.setattr(c.owner.db, "finish_command", original_finish)
    c.pool.contexts.clear()
    result = execute(c)
    assert result["status"] == "applied" and result["result"]["reconciled"] is True, result
    assert c.context.reads == reads
    assert all(p.read_bytes() == value for p, value in before.items())


def test_new_command_cannot_reuse_another_commands_seal(configured):
    c = configured
    enqueue(c, "first-seal")
    first = execute(c)
    assert first["status"] == "applied", first
    reads = list(c.context.reads)
    enqueue(c, "second-different-command")
    result = execute(c)
    assert result["status"] == "recovery_required", result
    assert c.context.reads == reads
    assert c.owner.get_binding("binding-old")["stopped_writer_release"]["command_id"] == "first-seal"


def test_crash_before_seal_needs_original_version_and_new_observation(configured, monkeypatch):
    c = configured

    async def cancelled(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(c.worker, "_observe_stopped_writer_target", cancelled)
    key = enqueue(c)
    with pytest.raises(asyncio.CancelledError):
        execute(c)
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
    assert not c.worker._endpoint_admission_holds
    c.owner.db.requeue_running_commands()
    c.worker._publish_command_state(c.store.load(c.path))
    # Force a new actual task projection version, not merely a wall-clock refresh.
    projection = build_task_projection(c.store.load(c.path), tasks=[c.store.load(c.path)])
    projection.detail["concurrent_projection"] = "different"
    c.owner.db.upsert_task_projections([projection])
    monkeypatch.setattr(c.worker, "_observe_stopped_writer_target", c.observe)
    result = execute(c)
    assert result["status"] == "failed" and "stale task version" in result["error"], result
    assert not c.seen and c.owner.get_binding("binding-old")["binding_status"] == "active"


def test_release_only_cleanup_fails_closed_before_any_new_browser_target(configured):
    c = configured
    before = c.path.read_bytes()

    class NoBrowserActions:
        def __getattr__(self, name):
            pytest.fail(f"release-only cleanup attempted browser action {name}")

    async def run():
        return await c.worker._continue_stop_writer_cleanup(
            c.store.load(c.path), NoBrowserActions(), c.path,
            force_retry=True, release_only=True,
        )

    assert asyncio.run(run()) is False
    assert c.path.read_bytes() == before
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"


def test_late_collector_result_cannot_seal_after_overall_deadline(configured, monkeypatch):
    from playwright_auto import cdpa_worker as worker_module

    c = configured
    clock = [100.0]
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: clock[0],
    ))

    async def late(page, payload):
        value = await c.observe(page, payload)
        if len(c.seen) == 2:
            clock[0] += 46.0
        return value

    monkeypatch.setattr(c.worker, "_observe_stopped_writer_target", late)
    enqueue(c)
    result = execute(c)
    assert result["status"] == "recovery_required", result
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
    assert not c.worker._endpoint_admission_holds


def test_unknown_authority_projects_pending_without_normalizing_or_breaking_publisher(configured):
    c = configured
    state = c.owner.db.get_snapshot("conversation_runtime")["payload"]
    state["leases"] = None
    c.owner.db.put_snapshot("conversation_runtime", state)
    before = c.owner.db.get_snapshot("conversation_runtime")
    c.worker._publish_command_state(c.store.load(c.path))
    assert c.owner.db.get_snapshot("conversation_runtime") == before
    assert c.owner.db.get_task_detail("task-stopped")["writer_maintenance"]["state"] == "pending"


def test_maintenance_dispatch_uses_existing_admission_during_browser_cycle(configured):
    c = configured
    c.worker._browser_cycle_active = True
    enqueue(c)
    result = execute(c)
    assert result["status"] == "applied", result
    assert len(c.seen) == 2 and not c.worker._endpoint_admission_holds
