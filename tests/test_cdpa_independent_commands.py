from __future__ import annotations

import asyncio
import signal
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config


def setup_worker(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
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


def test_create_independent_agent_command_is_idempotent(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
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
    assert agents[0]["independent"]["trigger_settings"] == {
        "recovery": False,
        "interval_minutes": None,
        "task_done": True,
        "role_completed": [],
        "teams": ["unused-team"],
        "states": ["BLOCKED"],
        "check_all": False,
    }








def test_custom_agent_create_configure_run_complete_respawns_once(tmp_path: Path):
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
    successors = [
        item
        for item in tasks
        if item.get("task_mode") == "independent"
        and item["independent"].get("previous_task_id") == agent["task_id"]
    ]
    assert completed["status"] == "DONE"
    assert complete_command["command_id"] in completed["applied_command_ids"]
    assert len(successors) == 1
    assert successors[0]["status"] == "WAITING"
    assert successors[0]["independent"]["trigger_settings"] == configured["independent"][
        "trigger_settings"
    ]








def test_independent_target_control_is_bound_to_active_event(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    target = store.create_task(
        "Blocked target",
        requested_team="control-target",
        task_id="control-target-task",
    )

    def block(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "control-target-plan is offline"
        return current

    target = store.update(target["manifest_path"], block)
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()
    agent = store.load(agent["manifest_path"])
    event_key = agent["independent"]["active_event"]["event_key"]
    enqueue(
        worker.runtime_db,
        command_id="cmd-agent-open-target",
        kind="independent_task_control",
        task_id=agent["task_id"],
        payload={
            "target_task_id": target["task_id"],
            "action": "open_tab",
            "role": "PLAN",
            "reason": "Restore the exact role tab.",
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
    assert control["action"] == "open_tab"

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


def test_dashboard_normalizes_independent_creation_completion_activation_repair_and_control(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config, db=RuntimeDB(config.runtime_database))

    created = api.normalize_independent_create(
        {
            "name": "Monitor Two",
            "system_prompt": "Inspect progress.",
            "mode": "Independent",
            "trigger_settings": {
                "interval_minutes": 20,
                "role_completed": ["dev"],
            },
        }
    )
    completed = api.normalize_independent_completion(
        {"outcome": "SUCCESS", "summary": "Checks passed."}
    )
    activated = api.normalize_independent_activation(
        {"agent_name": "Maintainers", "target_task_id": "target-a"}
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
            "action": "open_tab",
            "role": "DEV",
            "reason": "Restore ownership.",
            "confirmed": False,
        }
    )

    assert created == {
        "name": "Monitor Two",
        "system_prompt": "Inspect progress.",
        "mode": "Independent",
        "trigger_settings": {
            "recovery": False,
            "interval_minutes": 20,
            "task_done": False,
            "role_completed": ["DEV"],
            "teams": [],
            "states": [],
            "check_all": False,
        },
    }
    assert completed["outcome"] == "SUCCESS"
    assert completed["summary"] == "Checks passed."
    assert activated == {"agent_name": "Maintainers", "target_task_id": "target-a"}
    assert repair["disposition"] == "HOLD_FOR_REPAIR"
    assert repair["source_areas"] == ("cdpa_actions",)
    assert control == {
        "target_task_id": "target-a",
        "action": "open_tab",
        "role": "DEV",
        "reason": "Restore ownership.",
        "confirmed": False,
    }
