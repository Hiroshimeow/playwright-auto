from __future__ import annotations

import asyncio
import signal
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
from playwright_auto.cdpa_independent import canonical_independent_events, claim_oldest_event
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.dashboard_api import APIError, DashboardAPI
from playwright_auto.durable import RequestLedger

from test_cdpa_core import write_config


def setup_worker(tmp_path: Path):
    config = replace(
        load_cdpa_config(write_config(tmp_path), repository_root=tmp_path),
        independent_activation_enabled=True,
    )
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    return config, store, worker


def enqueue(db: RuntimeDB, *, command_id: str, kind: str, task_id: str | None, payload: dict):
    return db.enqueue_command(
        command_id=command_id,
        idempotency_key=f"idem-{command_id}",
        kind=kind,
        task_id=task_id,
        expected_task_version=None,
        payload=payload,
    )


@contextmanager
def fail_after(seconds: float):
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        started = time.monotonic()
        yield
        if time.monotonic() - started > seconds:
            raise TimeoutError("command dispatch exceeded bounded test timeout")
        return

    previous = signal.getsignal(signal.SIGALRM)

    def timeout(_signum, _frame):
        raise TimeoutError("command dispatch exceeded bounded test timeout")

    signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _claim_targetless_periodic_event(
    store: TaskStore,
    agent: dict,
    *,
    check_all: bool,
) -> dict:
    now = datetime.now(timezone.utc)

    def claim(current: dict) -> dict:
        interval = int(current["independent"]["trigger_settings"]["interval_minutes"])
        current_slot = int(now.timestamp() // (interval * 60))
        current["independent"]["watermarks"]["last_interval_slot"] = current_slot - 1
        events = canonical_independent_events(current, [current], now=now)
        return claim_oldest_event(current, events, all_tasks=[current])

    claimed = store.update(agent["manifest_path"], claim)
    active = claimed["independent"]["active_event"]
    assert active["target_task_id"] is None
    assert active["target_hop_id"] is None
    assert active["trigger_type"] == ("check_all" if check_all else "interval")
    return claimed


def _create_blocked_target(
    store: TaskStore,
    *,
    task_id: str,
    team: str,
    boundary: str | None = None,
) -> dict:
    target = store.create_task(
        "Blocked periodic target",
        requested_team=team,
        task_id=task_id,
    )

    def block(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "periodic target role is offline"
        current["blocked_at"] = datetime.now(timezone.utc).isoformat()
        hop = next(
            item
            for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        if boundary == "accepted":
            hop["state"] = "waiting"
            hop["receipt"] = {"user_message_id": "accepted-periodic-target"}
        elif boundary == "ambiguous":
            hop["state"] = "sending"
        return current

    return store.update(target["manifest_path"], block)


def test_create_independent_agent_command_is_idempotent(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    pinned = store.endpoints.add("9212")
    enqueue(
        worker.runtime_db,
        command_id="cmd-create-agent",
        kind="create_independent_agent",
        task_id=None,
        payload={
            "name": "Release Watcher",
            "system_prompt": "Review releases.",
            "mode": "Independent",
            "trigger_settings": {
                "task_done": True,
                "teams": ["unused-team"],
                "states": ["BLOCKED"],
            },
            "browser_endpoint_id": pinned["endpoint_id"],
        },
    )

    result = worker.dispatch_command_once()
    replay = worker.dispatch_command_once()
    agents = [
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"]["agent_name"] == "Release Watcher"
    ]

    assert result["status"] == "applied"
    assert replay is None
    assert len(agents) == 1
    assert agents[0]["status"] == "WAITING"
    assert agents[0]["independent"]["system_prompt"] == "Review releases."
    assert agents[0]["independent"]["temporary_chat"] is True
    assert agents[0]["independent"]["browser_endpoint_id"] == pinned["endpoint_id"]
    assert agents[0]["independent"]["trigger_settings"] == {
        "recovery": False,
        "interval_minutes": None,
        "daily_at": None,
        "task_done": True,
        "role_completed": [],
        "teams": ["unused-team"],
        "states": ["BLOCKED"],
        "check_all": False,
    }








def test_independent_settings_command_distinguishes_omitted_affinity_from_clear(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    pinned = store.endpoints.add("9212")
    agent = store.create_independent_agent(
        "Pinned Watcher",
        system_prompt="Inspect only.",
        browser_endpoint_id=pinned["endpoint_id"],
    )
    worker.hydrate_runtime(startup=False)

    enqueue(
        worker.runtime_db,
        command_id="cmd-keep-affinity",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload={"display_name": "Pinned Watcher Renamed"},
    )
    assert worker.dispatch_command_once()["status"] == "applied"
    kept = store.load(agent["manifest_path"])
    assert kept["independent"]["browser_endpoint_id"] == pinned["endpoint_id"]

    enqueue(
        worker.runtime_db,
        command_id="cmd-clear-affinity",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload={"browser_endpoint_id": None},
    )
    assert worker.dispatch_command_once()["status"] == "applied"
    cleared = store.load(agent["manifest_path"])
    assert cleared["independent"]["browser_endpoint_id"] is None


def test_custom_agent_create_configure_run_complete_reuses_identity(tmp_path: Path):
    config, store, worker = setup_worker(tmp_path)
    api = DashboardAPI(config, db=worker.runtime_db)
    created_command = api.enqueue(
        idempotency_key="custom-create-key",
        kind="create_independent_agent",
        task_id=None,
        payload=api.normalize_independent_create(
            {
                "name": "Custom Watcher",
                "system_prompt": "Review the requested trigger and report.",
                "mode": "Independent",
            }
        ),
    )
    with fail_after(2):
        assert worker.dispatch_command_once()["status"] == "applied"
    agent = next(
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"]["agent_name"] == "Custom Watcher"
    )
    assert created_command["command_id"] in agent["applied_command_ids"]

    settings_command = api.enqueue(
        idempotency_key="custom-settings-key",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload=api.normalize_independent_settings(
            {"trigger_settings": {"interval_minutes": 60, "check_all": True}}
        ),
    )
    with fail_after(2):
        assert worker.dispatch_command_once()["status"] == "applied"
    configured = store.load(agent["manifest_path"])
    assert configured["independent"]["trigger_settings"]["interval_minutes"] == 60
    assert configured["independent"]["trigger_settings"]["check_all"] is True
    assert settings_command["command_id"] in configured["applied_command_ids"]

    run_command = api.enqueue(
        idempotency_key="custom-run-key",
        kind="independent_run_now",
        task_id=agent["task_id"],
        payload={"trigger_type": "check_all"},
    )
    with fail_after(2):
        assert worker.dispatch_command_once()["status"] == "applied"
    running = store.load(agent["manifest_path"])
    assert running["status"] == "RUNNING"
    assert running["independent"]["active_event"]["trigger_type"] == "check_all"
    assert run_command["command_id"] in running["applied_command_ids"]

    def respond(current: dict) -> dict:
        hop = next(
            item
            for item in current["hops"]
            if item.get("hop_id") == current.get("active_hop_id")
        )
        hop["state"] = "responded"
        hop["response"] = "Checked all current work and verified the result."
        hop["response_sha256"] = "c" * 64
        return current

    running = store.update(agent["manifest_path"], respond)
    worker.hydrate_runtime(startup=False)
    complete_command = api.enqueue(
        idempotency_key="custom-complete-key",
        kind="independent_complete",
        task_id=agent["task_id"],
        payload=api.normalize_independent_completion(
            {"outcome": "SUCCESS", "summary": "Custom check completed."}
        ),
    )
    with fail_after(2):
        assert worker.dispatch_command_once()["status"] == "applied"

    tasks = store.discover()
    completed = next(item for item in tasks if item["task_id"] == agent["task_id"])
    identities = [
        item
        for item in tasks
        if item.get("task_mode") == "independent"
        and item["independent"].get("agent_key")
        == completed["independent"].get("agent_key")
    ]
    assert completed["status"] == "WAITING"
    assert completed["independent"]["enabled"] is True
    assert completed["independent"]["active_event"] is None
    assert completed["independent"]["job_history"][-1]["disposition"] == "COMPLETED"
    assert complete_command["command_id"] in completed["applied_command_ids"]
    assert len(identities) == 1
    assert completed["independent"]["trigger_settings"] == configured["independent"][
        "trigger_settings"
    ]








@pytest.mark.parametrize(
    ("check_all", "trigger_type"),
    [(False, "interval"), (True, "check_all")],
)
def test_targetless_periodic_bind_is_durable_and_reuses_exact_target(
    tmp_path: Path,
    check_all: bool,
    trigger_type: str,
):
    config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        f"Periodic Binder {trigger_type}",
        system_prompt="Inspect blocked workflows and act only after binding one target.",
        trigger_settings={
            "interval_minutes": 1_000_000,
            "check_all": check_all,
        },
        max_cycles=5,
    )
    agent = _claim_targetless_periodic_event(store, agent, check_all=check_all)

    def mark_source_accepted(current: dict) -> dict:
        hop = next(
            item
            for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        hop["state"] = "waiting"
        hop["receipt"] = {"user_message_id": f"accepted-{trigger_type}-agent"}
        return current

    agent = store.update(agent["manifest_path"], mark_source_accepted)
    target = _create_blocked_target(
        store,
        task_id=f"periodic-{trigger_type}-target",
        team=f"periodic-{trigger_type}",
        boundary="ambiguous",
    )
    worker.hydrate_runtime(startup=False)
    source_before = store.load(agent["manifest_path"])
    target = store.load(target["manifest_path"])
    target_bytes_before = Path(target["manifest_path"]).read_bytes()
    event_before = dict(source_before["independent"]["active_event"])
    source_hops_before = source_before["hops"]
    source_roles_before = source_before["roles"]
    bind_command_id = f"cmd-bind-{trigger_type}"

    enqueue(
        worker.runtime_db,
        command_id=bind_command_id,
        kind="independent_bind_target",
        task_id=agent["task_id"],
        payload={
            "event_key": event_before["event_key"],
            "target_task_id": target["task_id"],
        },
    )
    command = worker.dispatch_command_once()
    bound = store.load(agent["manifest_path"])
    active = bound["independent"]["active_event"]

    assert command["status"] == "applied"
    assert command["result"] == {
        "task_id": agent["task_id"],
        "event_key": event_before["event_key"],
        "target_task_id": target["task_id"],
        "target_hop_id": target["active_hop_id"],
    }
    assert {
        key: active[key]
        for key in ("event_key", "trigger_type", "occurred_at", "check_count")
    } == {
        key: event_before[key]
        for key in ("event_key", "trigger_type", "occurred_at", "check_count")
    }
    assert active["target_task_id"] == target["task_id"]
    assert active["target_hop_id"] == target["active_hop_id"]
    assert active["target_team"] == target["team"]
    assert active["target_role"] == target["active_role"]
    assert bound["hops"] == source_hops_before
    assert bound["roles"] == source_roles_before
    assert bound["active_hop_id"] == source_before["active_hop_id"]
    assert bound["browser_endpoint_id"] == source_before["browser_endpoint_id"]
    assert Path(target["manifest_path"]).read_bytes() == target_bytes_before
    assert bound["applied_command_ids"].count(bind_command_id) == 1

    replayed = store.bind_independent_event_target(
        agent["manifest_path"],
        event_key=event_before["event_key"],
        target_task_id=target["task_id"],
        external_command_id=bind_command_id,
    )
    assert replayed["updated_at"] == bound["updated_at"]
    assert replayed["applied_command_ids"].count(bind_command_id) == 1
    replay_state = worker._command_replay_state(command)
    assert replay_state is not None
    assert replay_state["independent"]["active_event"]["target_task_id"] == target["task_id"]

    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            (bind_command_id,),
        )
    mailbox_replay = worker.dispatch_command_once()
    rebound = store.load(agent["manifest_path"])
    assert mailbox_replay["status"] == "applied"
    assert mailbox_replay["result"]["reconciled"] is True
    assert rebound["updated_at"] == bound["updated_at"]
    assert rebound["applied_command_ids"].count(bind_command_id) == 1
    assert rebound["independent"]["active_event"] == bound["independent"]["active_event"]

    restarted = CDPAWorker(config, store=store)
    restarted.hydrate_runtime(startup=False)
    restarted_source = store.load(agent["manifest_path"])
    assert restarted_source["independent"]["active_event"]["event_key"] == event_before["event_key"]
    assert restarted_source["independent"]["active_event"]["target_task_id"] == target["task_id"]
    assert restarted_source["independent"]["active_event"]["target_hop_id"] == target["active_hop_id"]

    control_payload = {
        "target_task_id": target["task_id"],
        "action": "resume",
        "role": "PLAN",
        "reason": "Resume only the bound periodic target.",
        "confirmed": False,
    }
    enqueue(
        restarted.runtime_db,
        command_id=f"cmd-control-{trigger_type}",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload=control_payload,
    )
    control_command = restarted.dispatch_command_once()
    controlled = store.load(target["manifest_path"])
    assert control_command["status"] == "applied"
    assert len(controlled["controls"]) == 1
    assert controlled["controls"][0]["source_task_id"] == agent["task_id"]
    assert controlled["controls"][0]["source_event_key"] == event_before["event_key"]

    enqueue(
        restarted.runtime_db,
        command_id=f"cmd-control-{trigger_type}-duplicate",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload=control_payload,
    )
    assert restarted.dispatch_command_once()["status"] == "applied"
    assert len(store.load(target["manifest_path"])["controls"]) == 1

    repair_payload = {
        "root_cause": "The bound workflow needs one durable orchestration repair.",
        "disposition": "CONTINUE_IN_PARALLEL",
        "reason": "Repair the exact workflow selected by the periodic event.",
        "reproduction": "Run the targetless periodic check, bind the blocked target, and inspect its failure.",
        "source_areas": ["cdpa_store"],
        "required_tests": ["Preserve the same event and exact target across restart."],
        "lesson": None,
    }
    enqueue(
        restarted.runtime_db,
        command_id=f"cmd-repair-{trigger_type}",
        kind="independent_create_repair",
        task_id=agent["task_id"],
        payload=repair_payload,
    )
    repair_command = restarted.dispatch_command_once()
    assert repair_command["status"] == "applied"
    assert repair_command["result"]["affected_task_id"] == target["task_id"]

    enqueue(
        restarted.runtime_db,
        command_id=f"cmd-repair-{trigger_type}-duplicate",
        kind="independent_create_repair",
        task_id=agent["task_id"],
        payload=repair_payload,
    )
    duplicate_repair = restarted.dispatch_command_once()
    assert duplicate_repair["status"] == "applied"
    assert duplicate_repair["result"]["repair_task_id"] == repair_command["result"]["repair_task_id"]


def test_run_now_check_all_bind_uses_durable_active_event_for_control(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Run Now Binder",
        system_prompt="Inspect all workflows and bind before control.",
        trigger_settings={},
        max_cycles=5,
    )
    agent = store.run_independent_now(
        agent["manifest_path"],
        trigger_type="check_all",
        external_command_id="cmd-run-now-check-all",
    )
    event_key = agent["independent"]["active_event"]["event_key"]
    target = _create_blocked_target(
        store,
        task_id="run-now-check-all-target",
        team="run-now-check-all",
    )
    worker.hydrate_runtime(startup=False)

    enqueue(
        worker.runtime_db,
        command_id="cmd-bind-run-now-check-all",
        kind="independent_bind_target",
        task_id=agent["task_id"],
        payload={"event_key": event_key, "target_task_id": target["task_id"]},
    )
    assert worker.dispatch_command_once()["status"] == "applied"

    enqueue(
        worker.runtime_db,
        command_id="cmd-control-run-now-check-all",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Resume the target selected by this CHECK_ALL run.",
            "confirmed": False,
        },
    )
    control = worker.dispatch_command_once()
    assert control["status"] == "applied"
    persisted = store.load(target["manifest_path"])
    assert len(persisted["controls"]) == 1
    assert persisted["controls"][0]["source_event_key"] == event_key


def test_periodic_bind_rejects_stale_rebind_and_ineligible_targets(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Periodic Bind Guard",
        system_prompt="Bind exactly one blocked workflow target.",
        trigger_settings={"interval_minutes": 1_000_000, "check_all": True},
        max_cycles=5,
    )
    agent = _claim_targetless_periodic_event(store, agent, check_all=True)
    event_key = agent["independent"]["active_event"]["event_key"]

    ineligible = store.create_task(
        "Not blocked",
        requested_team="periodic-ineligible",
        task_id="periodic-ineligible-task",
    )
    terminal = store.create_task(
        "Already done",
        requested_team="periodic-terminal",
        task_id="periodic-terminal-task",
    )
    terminal = store.update(
        terminal["manifest_path"],
        lambda current: {
            **current,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    independent_target = store.create_independent_agent(
        "Other Periodic Agent",
        system_prompt="Do unrelated independent work.",
        trigger_settings={"interval_minutes": 1_000_000},
    )
    selected = _create_blocked_target(
        store,
        task_id="periodic-selected-task",
        team="periodic-selected",
    )
    other = _create_blocked_target(
        store,
        task_id="periodic-other-task",
        team="periodic-other",
    )
    worker.hydrate_runtime(startup=False)

    def dispatch_bind(command_id: str, candidate_event_key: str, target_task_id: str):
        enqueue(
            worker.runtime_db,
            command_id=command_id,
            kind="independent_bind_target",
            task_id=agent["task_id"],
            payload={
                "event_key": candidate_event_key,
                "target_task_id": target_task_id,
            },
        )
        return worker.dispatch_command_once()

    stale = dispatch_bind("cmd-bind-stale-event", f"{event_key}:stale", selected["task_id"])
    assert stale["status"] == "failed"
    assert "source event is stale" in stale["error"]

    not_blocked = dispatch_bind("cmd-bind-ineligible", event_key, ineligible["task_id"])
    assert not_blocked["status"] == "failed"
    assert "not currently blocked" in not_blocked["error"]

    terminal_result = dispatch_bind("cmd-bind-terminal", event_key, terminal["task_id"])
    assert terminal_result["status"] == "failed"
    assert "not currently blocked" in terminal_result["error"]

    independent_result = dispatch_bind(
        "cmd-bind-independent",
        event_key,
        independent_target["task_id"],
    )
    assert independent_result["status"] == "failed"
    assert "must be a workflow task" in independent_result["error"]

    applied = dispatch_bind("cmd-bind-selected", event_key, selected["task_id"])
    assert applied["status"] == "applied"
    other_bytes_before = Path(other["manifest_path"]).read_bytes()
    rebind = dispatch_bind("cmd-bind-rebind", event_key, other["task_id"])
    assert rebind["status"] == "failed"
    assert "already bound" in rebind["error"]
    assert Path(other["manifest_path"]).read_bytes() == other_bytes_before
    persisted = store.load(agent["manifest_path"])
    assert persisted["independent"]["active_event"]["target_task_id"] == selected["task_id"]

    def advance_target_hop(current: dict) -> dict:
        role = str(current.get("active_role") or "PLAN")
        worker._append_hop(
            current,
            source_role=role,
            target_role=role,
            handoff="A later workflow hop must not inherit the periodic bind.",
        )
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "later hop is independently blocked"
        return current

    selected = store.update(selected["manifest_path"], advance_target_hop)
    enqueue(
        worker.runtime_db,
        command_id="cmd-control-stale-periodic-hop",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": selected["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Do not transfer bind authority to a later hop.",
            "confirmed": False,
        },
    )
    stale_hop = worker.dispatch_command_once()
    assert stale_hop["status"] == "failed"
    assert "target hop is stale" in stale_hop["error"]

    selected = store.update(
        selected["manifest_path"],
        lambda current: {
            **current,
            "status": "RUNNING",
            "kanban_column": "ACTIVE",
            "block_code": None,
            "block_reason": None,
        },
    )
    enqueue(
        worker.runtime_db,
        command_id="cmd-control-unblocked-periodic-target",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": selected["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Do not control a target that already recovered.",
            "confirmed": False,
        },
    )
    stale_control = worker.dispatch_command_once()
    assert stale_control["status"] == "failed"
    assert "no longer blocked" in stale_control["error"]

    enqueue(
        worker.runtime_db,
        command_id="cmd-repair-unblocked-periodic-target",
        kind="independent_create_repair",
        task_id=agent["task_id"],
        payload={
            "root_cause": "The periodic target already recovered before repair admission.",
            "disposition": "CONTINUE_IN_PARALLEL",
            "reason": "Reject stale repair authority.",
            "reproduction": "Bind a blocked target and let it recover before repair.",
            "source_areas": ["cdpa_store"],
            "required_tests": ["Reject repair after the bound target stops being blocked."],
            "lesson": None,
        },
    )
    stale_repair = worker.dispatch_command_once()
    assert stale_repair["status"] == "failed"
    assert "no longer blocked" in stale_repair["error"]

    def advance_event(current: dict) -> dict:
        active = current["independent"]["active_event"]
        active["event_key"] = f"{event_key}:later"
        active["occurred_at"] = datetime.now(timezone.utc).isoformat()
        active["target_task_id"] = None
        active["target_hop_id"] = None
        active["target_team"] = None
        active["target_role"] = None
        return current

    store.update(agent["manifest_path"], advance_event)
    with pytest.raises(ValueError, match="replayed independent bind"):
        store.bind_independent_event_target(
            agent["manifest_path"],
            event_key=event_key,
            target_task_id=selected["task_id"],
            external_command_id="cmd-bind-selected",
        )


def test_bound_periodic_control_revalidates_target_hop_inside_store(
    tmp_path: Path, monkeypatch
):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Periodic Control Hop Race",
        system_prompt="Bind one blocked target before control.",
        trigger_settings={"interval_minutes": 1_000_000},
        max_cycles=5,
    )
    agent = _claim_targetless_periodic_event(store, agent, check_all=False)
    target = _create_blocked_target(
        store,
        task_id="periodic-control-hop-race-target",
        team="periodic-control-hop-race",
    )
    event_key = agent["independent"]["active_event"]["event_key"]
    store.bind_independent_event_target(
        agent["manifest_path"],
        event_key=event_key,
        target_task_id=target["task_id"],
        external_command_id="cmd-bind-control-hop-race",
    )
    worker.hydrate_runtime(startup=False)

    original = store.queue_bound_independent_control

    def advance_hop_before_store_admission(*args, **kwargs):
        def advance(current: dict) -> dict:
            role = str(current.get("active_role") or "PLAN")
            worker._append_hop(
                current,
                source_role=role,
                target_role=role,
                handoff="Race to a later workflow hop before durable control admission.",
            )
            current["status"] = "BLOCKED"
            current["kanban_column"] = "BLOCKED"
            current["block_code"] = "role_offline"
            current["block_reason"] = "later hop is independently blocked"
            return current

        store.update(target["manifest_path"], advance)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "queue_bound_independent_control",
        advance_hop_before_store_admission,
    )
    enqueue(
        worker.runtime_db,
        command_id="cmd-control-hop-race",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Do not authorize a later hop.",
            "confirmed": False,
        },
    )

    result = worker.dispatch_command_once()
    persisted = store.load(target["manifest_path"])
    assert result["status"] == "failed"
    assert "target hop is stale" in result["error"]
    assert persisted["controls"] == []


def test_bound_periodic_control_revalidates_source_event_inside_store(
    tmp_path: Path, monkeypatch
):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Periodic Control Event Race",
        system_prompt="Bind one blocked target before control.",
        trigger_settings={"interval_minutes": 1_000_000, "check_all": True},
        max_cycles=5,
    )
    agent = _claim_targetless_periodic_event(store, agent, check_all=True)
    target = _create_blocked_target(
        store,
        task_id="periodic-control-event-race-target",
        team="periodic-control-event-race",
    )
    event_key = agent["independent"]["active_event"]["event_key"]
    store.bind_independent_event_target(
        agent["manifest_path"],
        event_key=event_key,
        target_task_id=target["task_id"],
        external_command_id="cmd-bind-control-event-race",
    )
    worker.hydrate_runtime(startup=False)

    original = store.queue_bound_independent_control

    def advance_event_before_store_admission(*args, **kwargs):
        def advance(current: dict) -> dict:
            active = current["independent"]["active_event"]
            active["event_key"] = f"{event_key}:later"
            active["target_task_id"] = None
            active["target_hop_id"] = None
            active["target_team"] = None
            active["target_role"] = None
            return current

        store.update(agent["manifest_path"], advance)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "queue_bound_independent_control",
        advance_event_before_store_admission,
    )
    enqueue(
        worker.runtime_db,
        command_id="cmd-control-event-race",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Do not authorize a later independent event.",
            "confirmed": False,
        },
    )

    result = worker.dispatch_command_once()
    persisted = store.load(target["manifest_path"])
    assert result["status"] == "failed"
    assert "source event is stale" in result["error"]
    assert persisted["controls"] == []


def test_bound_periodic_repair_revalidates_blocked_state_inside_store(
    tmp_path: Path, monkeypatch
):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Periodic Repair State Race",
        system_prompt="Bind one blocked target before repair.",
        trigger_settings={"interval_minutes": 1_000_000},
        max_cycles=5,
    )
    agent = _claim_targetless_periodic_event(store, agent, check_all=False)
    target = _create_blocked_target(
        store,
        task_id="periodic-repair-state-race-target",
        team="periodic-repair-state-race",
    )
    event_key = agent["independent"]["active_event"]["event_key"]
    store.bind_independent_event_target(
        agent["manifest_path"],
        event_key=event_key,
        target_task_id=target["task_id"],
        external_command_id="cmd-bind-repair-state-race",
    )
    worker.hydrate_runtime(startup=False)

    original = store.create_or_gate_repair

    def recover_before_store_admission(request, **kwargs):
        store.update(
            target["manifest_path"],
            lambda current: {
                **current,
                "status": "RUNNING",
                "kanban_column": "ACTIVE",
                "block_code": None,
                "block_reason": None,
            },
        )
        return original(request, **kwargs)

    monkeypatch.setattr(store, "create_or_gate_repair", recover_before_store_admission)
    enqueue(
        worker.runtime_db,
        command_id="cmd-repair-state-race",
        kind="independent_create_repair",
        task_id=agent["task_id"],
        payload={
            "root_cause": "The workflow recovered during periodic repair admission.",
            "disposition": "CONTINUE_IN_PARALLEL",
            "reason": "Reject stale periodic repair authority.",
            "reproduction": "Recover the bound target between worker snapshot and store admission.",
            "source_areas": ["cdpa_store"],
            "required_tests": ["Reject repair unless the bound target remains blocked."],
            "lesson": None,
        },
    )

    result = worker.dispatch_command_once()
    persisted = store.load(target["manifest_path"])
    assert result["status"] == "failed"
    assert "must remain BLOCKED" in result["error"]
    assert persisted.get("repair_links") in (None, [])


def test_independent_target_control_is_bound_to_active_event(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    repair = store.create_task(
        "Completed repair",
        requested_team="control-repair",
        task_id="control-repair-task",
    )
    repair = store.update(
        repair["manifest_path"],
        lambda current: {
            **current,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": "2026-07-26T00:00:00+00:00",
        },
    )
    target = store.create_task(
        "Blocked target",
        requested_team="control-target",
        task_id="control-target-task",
        depends_on_task_ids=(repair["task_id"],),
    )

    def block(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "control-target-plan is offline"
        return current

    target = store.update(target["manifest_path"], block)
    target = store.update(
        target["manifest_path"],
        lambda current: {
            **current,
            "blocked_at": "2026-07-26T00:00:00+00:00",
        },
    )
    with pytest.raises(ValueError, match="unsupported control action"):
        store.request_control(
            target["manifest_path"],
            "safe_restore",
            reason="operator must not acquire watchdog-only restore authority",
        )
    agent = store.update(
        agent["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    event_key = agent["independent"]["active_event"]["event_key"]
    enqueue(
        worker.runtime_db,
        command_id="cmd-agent-safe-restore-target",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Restore the repaired workflow safely.",
            "confirmed": False,
        },
    )

    command = worker.dispatch_command_once()
    persisted = store.load(target["manifest_path"])
    control = persisted["controls"][-1]

    assert command["status"] == "applied"
    assert control["origin"] == "independent_agent"
    assert control["source_task_id"] == agent["task_id"]
    assert control["source_event_key"] == event_key
    assert control["action"] == "safe_restore"

    enqueue(
        worker.runtime_db,
        command_id="cmd-agent-safe-restore-target-duplicate",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Restore the repaired workflow safely.",
            "confirmed": False,
        },
    )
    assert worker.dispatch_command_once()["status"] == "applied"
    persisted = store.load(target["manifest_path"])
    assert len(persisted["controls"]) == 1

    before_hop_id = persisted["active_hop_id"]
    before_dependencies = list(persisted["depends_on_task_ids"])
    before_endpoint = persisted["browser_endpoint_id"]
    applied = asyncio.run(
        worker._apply_control(
            persisted,
            SimpleNamespace(),
            Path(target["manifest_path"]),
            scheduling_tasks=store.discover(),
        )
    )
    restored = store.load(target["manifest_path"])
    assert applied is True
    assert restored["task_id"] == target["task_id"]
    assert restored["status"] == "RUNNING"
    assert restored["active_hop_id"] == before_hop_id
    assert restored["depends_on_task_ids"] == before_dependencies
    assert restored["browser_endpoint_id"] == before_endpoint
    assert restored["controls"][-1]["status"] == "applied"
    assert restored["controls"][-1]["result"]["postcondition"] == (
        "same_task_pristine_pre_send_restored"
    )

    other = store.create_task(
        "Other target", requested_team="other-target", task_id="other-target-task"
    )
    enqueue(
        worker.runtime_db,
        command_id="cmd-agent-control-other",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": other["task_id"],
            "action": "resume",
            "role": "PLAN",
            "reason": "Wrong target.",
            "confirmed": False,
        },
    )

    rejected = worker.dispatch_command_once()
    assert rejected["status"] == "failed"
    assert store.load(other["manifest_path"])["controls"] == []

def test_safe_restore_replaces_stopped_target_with_preserved_lineage(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Stopped Restore Watcher",
        system_prompt="Restore stopped work safely.",
        trigger_settings={
            "teams": ["safe-restore-stopped"],
            "states": ["STOPPED"],
        },
        max_cycles=5,
    )
    parent = store.create_task(
        "Stopped logical goal",
        requested_team="safe-restore-stopped",
        task_id="safe-restore-stopped-task",
    )
    parent = store.update(
        parent["manifest_path"],
        lambda current: {
            **current,
            "status": "RUNNING",
            "kanban_column": "PLAN",
            "required_browser_endpoint_id": current["browser_endpoint_id"],
        },
    )
    parent = store.change_goal(
        parent["manifest_path"],
        "Revised stopped logical goal",
        external_command_id="revise-before-safe-restore",
    )
    child = store.create_task(
        "Dependent child",
        requested_team="safe-restore-child",
        task_id="safe-restore-child-task",
        depends_on_task_ids=(parent["task_id"],),
    )

    def stop(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "DONE_STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["stopped_at"] = "2026-07-26T00:00:00+00:00"
        current["stop_reason"] = "verified orchestration defect"
        return current

    parent = store.update(parent["manifest_path"], stop)
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    event_key = agent["independent"]["active_event"]["event_key"]
    with pytest.raises(ValueError, match="source event is stale"):
        store.replace_task_and_rewire(
            parent["task_id"],
            "Do not restore from stale provenance.",
            reuse_team=True,
            rewire_children=True,
            incident_id="stale-safe-restore",
            source_task_id=agent["task_id"],
            source_event_key=f"{event_key}:stale",
        )

    enqueue(
        worker.runtime_db,
        command_id="cmd-safe-restore-stopped",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": parent["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Continue the exact logical work after repair.",
            "confirmed": False,
        },
    )
    result = worker.dispatch_command_once()
    assert result["status"] == "applied"
    replacement_id = result["result"]["replacement_task_id"]
    replacement = store.load_task_id(replacement_id)
    assert replacement is not None
    assert replacement["replaces_task_id"] == parent["task_id"]
    assert replacement["replacement_incident_id"] == (
        f"independent-safe-restore:{agent['task_id']}:{event_key}"
    )
    assert replacement["depends_on_task_ids"] == parent["depends_on_task_ids"]
    assert replacement["repository"] == parent["repository"]
    assert replacement["browser_endpoint_id"] == parent["browser_endpoint_id"]
    assert replacement["required_browser_endpoint_id"] == parent["required_browser_endpoint_id"]
    assert "Revised stopped logical goal" in replacement["task_text"]
    assert store.load(child["manifest_path"])["depends_on_task_ids"] == [replacement_id]

    enqueue(
        worker.runtime_db,
        command_id="cmd-safe-restore-stopped-repeat",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": parent["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Continue the exact logical work after repair.",
            "confirmed": False,
        },
    )
    repeated = worker.dispatch_command_once()
    assert repeated["status"] == "applied"
    assert repeated["result"]["replacement_task_id"] == replacement_id


@pytest.mark.parametrize("boundary", ["accepted", "ambiguous", "pending_durable"])
def test_safe_restore_replacement_denies_unresolved_send_boundaries(
    tmp_path: Path, boundary: str
):
    _config, store, worker = setup_worker(tmp_path)
    team = f"safe-restore-stopped-{boundary}"
    agent = store.create_independent_agent(
        f"Stopped Boundary Watcher {boundary}",
        system_prompt="Restore stopped work only across a safe boundary.",
        trigger_settings={"teams": [team], "states": ["STOPPED"]},
        max_cycles=5,
    )
    target = store.create_task(
        f"Stopped boundary target {boundary}",
        requested_team=team,
        task_id=f"safe-restore-stopped-{boundary}-task",
    )

    def stop_with_boundary(current: dict) -> dict:
        hop = next(
            item for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        if boundary == "accepted":
            hop["state"] = "responded"
            hop["receipt"] = {"user_message_id": "accepted-unresolved"}
        elif boundary == "ambiguous":
            hop["state"] = "sending"
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "DONE_STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["stopped_at"] = "2026-07-26T00:00:00+00:00"
        current["stop_reason"] = "orchestration recovery boundary"
        return current

    target = store.update(target["manifest_path"], stop_with_boundary)
    if boundary == "pending_durable":
        hop = target["hops"][0]
        RequestLedger(hop["ledger_path"]).begin(
            role=hop["physical_role"],
            prompt="pending replacement request",
            source_context={
                "task_id": target["task_id"],
                "team": target["team"],
                "hop_id": hop["hop_id"],
                "manifest": target["manifest_path"],
            },
            role_prompt_hash="b" * 64,
            request_id=hop["request_id"],
            render_request_marker=False,
        )

    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    assert agent["independent"]["active_event"]["target_task_id"] == target["task_id"]
    enqueue(
        worker.runtime_db,
        command_id=f"cmd-safe-restore-stopped-{boundary}",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Do not cross unresolved Send evidence.",
            "confirmed": False,
        },
    )
    rejected = worker.dispatch_command_once()
    assert rejected["status"] == "failed"
    assert not any(
        item.get("replaces_task_id") == target["task_id"]
        for item in store.discover()
    )


def test_safe_restore_does_not_override_explicit_operator_stop(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Operator Stop Watcher",
        system_prompt="Do not override operator intent.",
        trigger_settings={
            "teams": ["safe-restore-operator-stop"],
            "states": ["STOPPED"],
        },
        max_cycles=5,
    )
    target = store.create_task(
        "Operator stopped target",
        requested_team="safe-restore-operator-stop",
        task_id="safe-restore-operator-stop-task",
    )
    queued = store.request_control(
        target["manifest_path"],
        "stop",
        reason="operator chose to stop",
        external_command_id="operator-stop-before-watchdog",
    )
    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(target["manifest_path"]),
            scheduling_tasks=store.discover(),
        )
    ) is True
    stopped = store.load(target["manifest_path"])
    assert stopped["status"] == "STOPPED"
    assert stopped["controls"][-1]["origin"] == "operator"
    assert stopped["controls"][-1]["status"] == "applied"

    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    assert agent["independent"]["active_event"]["target_task_id"] == stopped["task_id"]
    enqueue(
        worker.runtime_db,
        command_id="cmd-safe-restore-operator-stop",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": stopped["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Should remain stopped.",
            "confirmed": False,
        },
    )
    rejected = worker.dispatch_command_once()
    assert rejected["status"] == "failed"
    replacements = [
        item
        for item in store.discover()
        if item.get("replaces_task_id") == stopped["task_id"]
    ]
    assert replacements == []


@pytest.mark.parametrize("boundary", ["accepted", "ambiguous", "pending_durable"])
def test_safe_restore_denies_non_pristine_send_boundaries(tmp_path: Path, boundary: str):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Boundary Watcher",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    target = store.create_task(
        f"Boundary target {boundary}",
        requested_team=f"safe-restore-{boundary}",
        task_id=f"safe-restore-{boundary}-task",
    )

    def prepare(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "role offline"
        current["blocked_at"] = "2026-07-26T00:00:00+00:00"
        hop = next(
            item for item in current["hops"]
            if item["hop_id"] == current["active_hop_id"]
        )
        if boundary == "accepted":
            hop["receipt"] = {"user_message_id": "accepted-once"}
        elif boundary == "ambiguous":
            hop["state"] = "sending"
        return current

    target = store.update(target["manifest_path"], prepare)
    if boundary == "pending_durable":
        hop = next(
            item for item in target["hops"]
            if item["hop_id"] == target["active_hop_id"]
        )
        RequestLedger(hop["ledger_path"]).begin(
            role=hop["physical_role"],
            prompt="pending durable request",
            source_context={
                "task_id": target["task_id"],
                "team": target["team"],
                "hop_id": hop["hop_id"],
                "manifest": target["manifest_path"],
            },
            role_prompt_hash="a" * 64,
            request_id=hop["request_id"],
            render_request_marker=False,
        )

    store.update(
        agent["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    enqueue(
        worker.runtime_db,
        command_id=f"cmd-safe-restore-{boundary}",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Restore only before Send.",
            "confirmed": False,
        },
    )
    assert worker._execution_is_pristine_pre_send(
        store.load(target["manifest_path"])
    ) is False
    rejected = worker.dispatch_command_once()
    assert rejected["status"] == "failed"
    assert store.load(target["manifest_path"])["controls"] == []


def test_safe_restore_rejects_send_boundary_race_at_persistence(
    tmp_path: Path, monkeypatch
):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    target = store.create_task(
        "Race target",
        requested_team="safe-restore-race",
        task_id="safe-restore-race-task",
    )

    def block(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "role offline"
        return current

    target = store.update(target["manifest_path"], block)
    target = store.update(
        target["manifest_path"],
        lambda current: {
            **current,
            "blocked_at": "2026-07-26T00:00:00+00:00",
        },
    )
    store.update(
        agent["manifest_path"],
        lambda current: {
            **current,
            "independent": {
                **current["independent"],
                "watermarks": {
                    **current["independent"]["watermarks"],
                    "recovery_enabled_at": "2026-07-26T00:00:00+00:00",
                },
            },
        },
    )
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    enqueue(
        worker.runtime_db,
        command_id="cmd-agent-safe-restore-race",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "safe_restore",
            "role": "PLAN",
            "reason": "Restore only if the Send boundary stays pristine.",
            "confirmed": False,
        },
    )
    assert worker.dispatch_command_once()["status"] == "applied"
    queued = store.load(target["manifest_path"])

    original_update_for_control = store.update_for_control

    def cross_send_boundary(path, action, mutator):
        if action == "safe_restore":
            def mark_message_identity(current: dict) -> dict:
                active_hop_id = current["active_hop_id"]
                hop = next(
                    item for item in current["hops"] if item["hop_id"] == active_hop_id
                )
                hop["message_identity"] = {"user_message_id": "accepted-race"}
                return current

            store.update(path, mark_message_identity)
        return original_update_for_control(path, action, mutator)

    monkeypatch.setattr(store, "update_for_control", cross_send_boundary)
    assert asyncio.run(
        worker._apply_control(
            queued,
            SimpleNamespace(),
            Path(target["manifest_path"]),
            scheduling_tasks=store.discover(),
        )
    ) is True

    saved = store.load(target["manifest_path"])
    control = saved["controls"][-1]
    assert saved["status"] == "BLOCKED"
    assert control["status"] == "rejected"
    assert control["command_state"] == "REJECTED"
    assert "pristine pre-Send" in control["result"]


def test_dashboard_normalizes_independent_creation_completion_activation_repair_and_control(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config, db=RuntimeDB(config.runtime_database))
    endpoint = BrowserEndpointRegistry(config).resolve()

    created = api.normalize_independent_create(
        {
            "name": "Monitor Two",
            "system_prompt": "Inspect progress.",
            "mode": "Independent",
            "trigger_settings": {
                "interval_minutes": 20,
                "role_completed": ["dev"],
            },
            "browser_endpoint_id": endpoint["endpoint_id"],
        }
    )
    persistent = api.normalize_independent_create(
        {
            "name": "Persistent Agent",
            "system_prompt": "Keep context.",
            "mode": "Independent",
            "temporary_chat": False,
        }
    )
    with pytest.raises(APIError, match="temporary_chat"):
        api.normalize_independent_create(
            {
                "name": "Invalid Agent",
                "system_prompt": "Reject invalid flag.",
                "mode": "Independent",
                "temporary_chat": "false",
            }
        )
    completed = api.normalize_independent_completion(
        {"outcome": "SUCCESS", "summary": "Checks passed."}
    )
    activated = api.normalize_independent_activation(
        {"agent_name": "Maintainers", "target_task_id": "target-a"}
    )
    bound = api.normalize_independent_bind_target(
        {"event_key": "check-all:monitor:42", "target_task_id": "target-a"}
    )
    repair = api.normalize_independent_repair(
        {
            "root_cause": "A durable role binding is not restored.",
            "disposition": "HOLD_FOR_REPAIR",
            "reason": "Continuation is unsafe.",
            "reproduction": "Close the exact role tab and resume the blocked task.",
            "source_areas": ["cdpa_actions"],
            "required_tests": ["Reopen the exact role tab and preserve the request identity."],
            "lesson": None,
        }
    )

    control = api.normalize_independent_task_control(
        {
            "target_task_id": "target-a",
            "action": "safe_restore",
            "role": "DEV",
            "reason": "Restore ownership.",
            "confirmed": False,
        }
    )

    assert created == {
        "name": "Monitor Two",
        "system_prompt": "Inspect progress.",
        "mode": "Independent",
        "temporary_chat": True,
        "browser_endpoint_id": endpoint["endpoint_id"],
        "trigger_settings": {
            "recovery": False,
            "interval_minutes": 20,
            "daily_at": None,
            "task_done": False,
            "role_completed": ["DEV"],
            "teams": [],
            "states": [],
            "check_all": False,
        },
    }
    assert created["browser_endpoint_id"] == endpoint["endpoint_id"]
    settings = api.normalize_independent_settings(
        {"browser_endpoint_id": endpoint["endpoint_id"]}
    )
    cleared_settings = api.normalize_independent_settings(
        {"browser_endpoint_id": None}
    )
    assert settings["browser_endpoint_id"] == endpoint["endpoint_id"]
    assert cleared_settings["browser_endpoint_id"] is None
    assert persistent["temporary_chat"] is False
    assert completed["outcome"] == "SUCCESS"
    assert completed["summary"] == "Checks passed."
    assert activated == {"agent_name": "Maintainers", "target_task_id": "target-a"}
    assert bound == {
        "event_key": "check-all:monitor:42",
        "target_task_id": "target-a",
    }
    assert repair["disposition"] == "HOLD_FOR_REPAIR"
    assert repair["source_areas"] == ("cdpa_actions",)
    assert control == {
        "target_task_id": "target-a",
        "action": "safe_restore",
        "role": "DEV",
        "reason": "Restore ownership.",
        "confirmed": False,
    }


def test_independent_board_clear_preserves_durable_agent_and_run_recreates_projection(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Off Board Watcher",
        system_prompt="Keep durable state.",
        trigger_settings={"interval_minutes": 30},
    )
    task_id = agent["task_id"]
    path = agent["manifest_path"]
    before = store.load(path)
    before_watermarks = dict(before["independent"]["watermarks"])
    before_settings = dict(before["independent"]["trigger_settings"])

    worker.hydrate_runtime(startup=False)
    assert task_id in {item["task_id"] for item in worker.runtime_db.get_board()["items"]}

    store.request_control(path, "clear_team", confirmed=True, reason="remove board projection")
    cleared = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    assert cleared["status"] == "WAITING"
    assert cleared["independent"]["deleted_at"] is None
    assert cleared["independent"]["board_projection_hidden_at"]
    assert cleared["independent"]["system_prompt"] == "Keep durable state."
    assert cleared["independent"]["trigger_settings"] == before_settings
    assert cleared["independent"]["watermarks"] == before_watermarks

    worker.hydrate_runtime(startup=False)
    assert task_id not in {item["task_id"] for item in worker.runtime_db.get_board()["items"]}
    assert worker.runtime_db.get_task_detail(task_id)["agent"]["board_visible"] is False
    agents = worker.runtime_db.get_snapshot("agents")["payload"]["independent"]
    assert task_id in {item["task_id"] for item in agents}

    running = store.run_independent_now(path, instruction="Run after board cleanup")
    assert running["task_id"] == task_id
    assert running["independent"]["board_projection_hidden_at"] is None
    worker.hydrate_runtime(startup=False)
    assert task_id in {item["task_id"] for item in worker.runtime_db.get_board()["items"]}


def test_legacy_cleared_independent_agent_migrates_to_off_board_durable_identity(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Legacy Cleared Watcher",
        system_prompt="Preserve me.",
    )
    path = agent["manifest_path"]

    def legacy_clear(state):
        state["independent"].pop("board_projection_hidden_at", None)
        state["status"] = "STOPPED"
        state["kanban_column"] = "STOPPED"
        state["terminal_state"] = "STOPPED"
        state["active_role"] = None
        state["active_hop_id"] = None
        state["stopped_at"] = "2026-09-01T00:00:00+00:00"
        state["cleanup"] = {"state": "CLEARED", "cleared_at": "2026-09-01T00:00:00+00:00"}
        return state

    store.update(path, legacy_clear)
    store.normalize_legacy_independent_agents()
    migrated = store.load(path)
    assert migrated["status"] == "WAITING"
    assert migrated["independent"]["deleted_at"] is None
    assert migrated["independent"]["board_projection_hidden_at"] == "2026-09-01T00:00:00+00:00"

    worker.hydrate_runtime(startup=False)
    task_id = migrated["task_id"]
    assert task_id not in {item["task_id"] for item in worker.runtime_db.get_board()["items"]}
    agents = worker.runtime_db.get_snapshot("agents")["payload"]["independent"]
    assert task_id in {item["task_id"] for item in agents}

    deleted = store.delete_independent_agent(path)
    assert deleted["independent"]["deleted_at"]
    worker.hydrate_runtime(startup=False)
    agents = worker.runtime_db.get_snapshot("agents")["payload"]["independent"]
    assert task_id not in {item["task_id"] for item in agents}