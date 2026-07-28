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


def test_create_command_recreates_stopped_agent_without_defaulting_saved_cycles(
    tmp_path: Path,
):
    _config, store, worker = setup_worker(tmp_path)
    original = store.create_independent_agent(
        "Release Watcher",
        system_prompt="Review releases.",
        trigger_settings={"task_done": True},
        max_cycles=4,
    )

    def stop(current: dict) -> dict:
        current.update(
            status="STOPPED",
            terminal_state="STOPPED",
            kanban_column="STOPPED",
            active_role=None,
            active_hop_id=None,
            active_action="stopped",
            stopped_at="2026-07-27T00:00:00+00:00",
        )
        current["roles"]["AGENT"].update(
            page_id="page-release-watcher",
            page_url="https://chatgpt.com/c/exact-release-watcher",
            conversation_generation=3,
            constructor_sent_generation=3,
        )
        current["independent"]["enabled"] = False
        return current

    stopped = store.update(original["manifest_path"], stop)
    worker.hydrate_runtime(startup=False)
    enqueue(
        worker.runtime_db,
        command_id="cmd-recreate-agent",
        kind="create_independent_agent",
        task_id=None,
        payload={
            "name": "Release Watcher",
            "system_prompt": "Review releases.",
            "mode": "Independent",
        },
    )

    result = worker.dispatch_command_once()
    recreated = next(
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"].get("previous_task_id") == stopped["task_id"]
    )

    assert result["status"] == "applied"
    assert recreated["independent"]["max_cycles"] == 4
    assert recreated["independent"]["trigger_settings"]["task_done"] is True
    assert recreated["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact-release-watcher"
    assert recreated["roles"]["AGENT"]["conversation_generation"] == 3


def test_settings_mailbox_applies_pause_resume_projection_and_restart_idempotency(
    tmp_path: Path,
):
    config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Release Watcher",
        system_prompt="Review releases.",
    )
    worker.hydrate_runtime(startup=False)
    api = DashboardAPI(config, db=worker.runtime_db)
    paused_payload = api.normalize_independent_settings(
        {
            "enabled": False,
            "trigger_settings": {
                "interval_minutes": 60,
                "task_done": True,
            },
            "new_chat_next_job": True,
        }
    )
    queued = api.enqueue(
        idempotency_key="settings-pause-key",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload=paused_payload,
    )

    with fail_after(2):
        paused_command = worker.dispatch_command_once()

    paused = store.load(agent["manifest_path"])
    paused_projection = worker.runtime_db.get_task_detail(agent["task_id"])
    assert paused_command["status"] == "applied"
    assert worker.runtime_db.get_command(queued["command_id"])["status"] == "applied"
    assert paused["status"] == "PAUSED"
    assert paused["independent"]["enabled"] is False
    assert paused["independent"]["trigger_settings"]["interval_minutes"] == 60
    assert paused["independent"]["trigger_settings"]["task_done"] is True
    assert paused["independent"]["new_chat_next_job"] is True
    assert paused["applied_command_ids"].count(queued["command_id"]) == 1
    assert paused_projection is not None
    assert paused_projection["status"] == "PAUSED"
    assert paused_projection["agent"]["enabled"] is False
    assert paused_projection["agent"]["trigger_settings"]["interval_minutes"] == 60

    resume_payload = api.normalize_independent_settings({"enabled": True})
    resumed_queued = api.enqueue(
        idempotency_key="settings-resume-key",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload=resume_payload,
    )
    with fail_after(2):
        resumed_command = worker.dispatch_command_once()

    resumed = store.load(agent["manifest_path"])
    assert resumed_command["status"] == "applied"
    assert resumed["status"] == "WAITING"
    assert resumed["independent"]["enabled"] is True
    assert resumed["applied_command_ids"].count(resumed_queued["command_id"]) == 1

    restarted = CDPAWorker(config, store=store)
    restarted.hydrate_runtime(startup=False)
    with fail_after(2):
        assert restarted.dispatch_command_once() is None
    persisted = store.load(agent["manifest_path"])
    assert persisted["applied_command_ids"].count(queued["command_id"]) == 1
    assert persisted["applied_command_ids"].count(resumed_queued["command_id"]) == 1


def test_settings_mailbox_rejects_second_recovery_owner_atomically(tmp_path: Path):
    config, store, worker = setup_worker(tmp_path)
    store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    agent = store.create_independent_agent(
        "Recovery Two",
        system_prompt="Stand by.",
    )
    worker.hydrate_runtime(startup=False)
    api = DashboardAPI(config, db=worker.runtime_db)
    payload = api.normalize_independent_settings(
        {"enabled": True, "trigger_settings": {"recovery": True}}
    )
    queued = api.enqueue(
        idempotency_key="settings-conflict-key",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload=payload,
    )

    with fail_after(2):
        command = worker.dispatch_command_once()

    persisted = store.load(agent["manifest_path"])
    terminal = worker.runtime_db.get_command(queued["command_id"])
    assert command["status"] == "failed"
    assert terminal["status"] == "failed"
    assert "exclusive recovery trigger" in terminal["error"]
    assert persisted["independent"]["trigger_settings"]["recovery"] is False
    assert queued["command_id"] not in persisted.get("applied_command_ids", [])


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


def test_explicit_completion_command_finishes_and_respawns_once(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    state = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )

    def active(current: dict) -> dict:
        current["status"] = "RUNNING"
        current["waiting"] = None
        current["waiting_reason"] = None
        current["waiting_code"] = None
        current["independent"]["active_event"] = {
            "event_key": "recovery:target-a:1",
            "trigger_type": "recovery",
            "occurred_at": "2026-07-26T00:00:00+00:00",
            "target_team": "alpha",
            "target_task_id": "target-a",
            "target_role": "DEV",
            "target_hop_id": 1,
            "failure_signature": "role_offline:abc",
            "occurrence_count": 1,
            "check_count": 1,
        }
        current["independent"]["cycle"] = 1
        current["hops"][0]["state"] = "responded"
        current["hops"][0]["response"] = "Recovered and verified."
        current["hops"][0]["response_sha256"] = "a" * 64
        return current

    state = store.update(state["manifest_path"], active)
    worker.hydrate_runtime(startup=False)
    enqueue(
        worker.runtime_db,
        command_id="cmd-complete-agent",
        kind="independent_complete",
        task_id=state["task_id"],
        payload={
            "outcome": "SUCCESS",
            "summary": "Target stable.",
            "target_task_id": "target-a",
        },
    )

    result = worker.dispatch_command_once()
    commands_before = len(store.discover())
    worker.runtime_db.requeue_running_commands()
    replay = worker.dispatch_command_once()
    tasks = store.discover()
    completed = next(item for item in tasks if item["task_id"] == state["task_id"])
    successors = [
        item
        for item in tasks
        if item.get("task_mode") == "independent"
        and item.get("status") == "WAITING"
        and item["independent"].get("previous_task_id") == state["task_id"]
    ]

    assert result["status"] == "applied"
    assert replay is None
    assert completed["status"] == "DONE"
    assert len(successors) == 1
    assert len(tasks) == commands_before


def test_completion_command_queues_before_response_then_respawns_after_response(tmp_path: Path):
    _config, store, worker = setup_worker(tmp_path)
    state = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )

    def activate(current: dict) -> dict:
        current["status"] = "RUNNING"
        current["waiting"] = None
        current["waiting_reason"] = None
        current["waiting_code"] = None
        current["independent"]["active_event"] = {
            "event_key": "recovery:target-queued:1",
            "trigger_type": "recovery",
            "occurred_at": "2026-07-26T00:00:00+00:00",
            "target_team": "alpha",
            "target_task_id": "target-queued",
            "target_role": "DEV",
            "target_hop_id": 1,
            "failure_signature": "role_offline:queued",
            "occurrence_count": 1,
            "check_count": 1,
        }
        current["independent"]["cycle"] = 1
        current["hops"][0]["state"] = "waiting"
        return current

    state = store.update(state["manifest_path"], activate)
    worker.hydrate_runtime(startup=False)
    enqueue(
        worker.runtime_db,
        command_id="cmd-complete-before-response",
        kind="independent_complete",
        task_id=state["task_id"],
        payload={
            "outcome": "SUCCESS",
            "summary": "Target stable.",
            "target_task_id": "target-queued",
        },
    )

    command = worker.dispatch_command_once()
    queued = store.load(state["manifest_path"])

    assert command["status"] == "applied"
    assert queued["status"] == "RUNNING"
    assert queued["independent"]["completion_request"]["outcome"] == "SUCCESS"
    assert not queued["independent"].get("successor_task_id")

    def respond(current: dict) -> dict:
        current["hops"][0]["state"] = "responded"
        current["hops"][0]["response"] = "Recovered and verified."
        current["hops"][0]["response_sha256"] = "b" * 64
        return current

    store.update(state["manifest_path"], respond)
    worker.hydrate_runtime(startup=False)
    result = asyncio.run(
        worker.advance(
            state["manifest_path"],
            SimpleNamespace(pages=[]),
            scheduling_tasks=store.discover(),
        )
    )
    tasks = store.discover()
    successors = [
        item
        for item in tasks
        if item.get("task_mode") == "independent"
        and item["independent"].get("previous_task_id") == state["task_id"]
    ]

    assert result["status"] == "DONE"
    assert len(successors) == 1


def test_agent_activation_and_direct_repair_commands_use_active_context(tmp_path: Path):
    config, store, worker = setup_worker(tmp_path)
    maintainers = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    monitor = store.create_independent_agent(
        "Monitor",
        system_prompt="Inspect progress.",
        max_cycles=1,
    )
    product_root = tmp_path / "product-repository"
    product_root.mkdir()
    target = store.create_task(
        "Blocked target",
        requested_team="repair-target",
        task_id="repair-target-task",
        repository=product_root,
    )

    def block(current: dict) -> dict:
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "repair-target-dev is offline"
        return current

    target = store.update(target["manifest_path"], block)
    monitor = store.run_independent_now(monitor["manifest_path"], trigger_type="check_all")
    worker.hydrate_runtime(startup=False)
    enqueue(
        worker.runtime_db,
        command_id="cmd-activate-maintainers",
        kind="independent_activate_agent",
        task_id=monitor["task_id"],
        payload={"agent_name": "Maintainers", "target_task_id": target["task_id"]},
    )

    activated_command = worker.dispatch_command_once()
    maintainers = store.load(maintainers["manifest_path"])

    assert activated_command["status"] == "applied"
    assert maintainers["independent"]["active_event"]["target_task_id"] == target["task_id"]

    enqueue(
        worker.runtime_db,
        command_id="cmd-create-independent-repair",
        kind="independent_create_repair",
        task_id=maintainers["task_id"],
        payload={
            "root_cause": "Role ownership is not reopened after an offline tab.",
            "disposition": "HOLD_FOR_REPAIR",
            "reason": "The affected task cannot continue safely until the ownership defect is fixed.",
            "reproduction": "Block the active role with role_offline and observe that no exact tab is restored.",
            "source_areas": ["cdpa_actions", "cdpa_worker"],
            "required_tests": ["Verify the exact offline role reopens without resending the accepted request."],
            "lesson": None,
            "repair_repository": str(product_root),
        },
    )

    repair_command = worker.dispatch_command_once()
    tasks = store.discover()
    repairs = [item for item in tasks if isinstance(item.get("repair"), dict)]
    affected = next(item for item in tasks if item["task_id"] == target["task_id"])

    assert repair_command["status"] == "applied"
    assert len(repairs) == 1
    assert affected["status"] == "WAITING"
    assert repairs[0]["task_id"] in affected["depends_on_task_ids"]
    assert affected["repository"] == str(product_root.resolve())
    assert repairs[0]["repository"] == str(config.repository_root)
    assert repairs[0]["repair"]["repository"] == str(product_root.resolve())
    assert repairs[0]["repair"]["repair_repository"] == str(config.repository_root)


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



def test_settings_prompt_only_update_preserves_stopped_maintainers_state(tmp_path: Path):
    config, store, worker = setup_worker(tmp_path)
    agent = store.create_independent_agent(
        "Maintainers",
        system_prompt="Old recovery prompt.",
        trigger_settings={"recovery": True},
        enabled=False,
        max_cycles=5,
    )
    def stop(current: dict) -> dict:
        current.update(
            status="STOPPED",
            terminal_state="STOPPED",
            kanban_column="STOPPED",
            active_role=None,
            active_hop_id=None,
            active_action="stopped",
            stopped_at="2026-07-27T00:00:00+00:00",
        )
        current["independent"]["enabled"] = False
        return current

    before = store.update(agent["manifest_path"], stop)
    worker.hydrate_runtime(startup=False)
    api = DashboardAPI(config, db=worker.runtime_db)
    payload = api.normalize_independent_settings(
        {"system_prompt": "Upgraded recovery and learning prompt."}
    )
    queued = api.enqueue(
        idempotency_key="maintainers-prompt-only-update",
        kind="independent_settings",
        task_id=agent["task_id"],
        payload=payload,
    )

    with fail_after(2):
        command = worker.dispatch_command_once()

    after = store.load(agent["manifest_path"])
    assert command["status"] == "applied"
    assert after["task_id"] == before["task_id"]
    assert after["team"] == before["team"]
    assert after["status"] == "STOPPED"
    assert after["independent"]["enabled"] is False
    assert after["independent"]["trigger_settings"] == before["independent"]["trigger_settings"]
    assert after["independent"]["active_event"] is None
    assert after["independent"]["successor_task_id"] is None
    assert after["independent"]["system_prompt"] == "Upgraded recovery and learning prompt."
    assert after["roles"]["AGENT"]["conversation_generation"] == before["roles"]["AGENT"]["conversation_generation"]
    assert after["roles"]["AGENT"]["page_url"] == before["roles"]["AGENT"]["page_url"]
    assert queued["command_id"] in after["applied_command_ids"]
