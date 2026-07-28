from __future__ import annotations

import json
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker

from test_cdpa_core import write_config


def seeded_config(tmp_path: Path):
    path = write_config(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["independent_agents"]["seed_builtins"] = True
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return load_cdpa_config(path, repository_root=tmp_path)


def current_agents(store: TaskStore) -> list[dict]:
    return [
        state
        for state in store.discover()
        if state.get("task_mode") == "independent"
        and state.get("status") != "DONE"
    ]


def test_startup_seeds_builtins_once_and_claims_legacy_block_without_advancing_legacy_state(
    tmp_path: Path,
):
    config = seeded_config(tmp_path)
    store = TaskStore(config)
    target = store.create_task(
        "Legacy blocked task",
        requested_team="legacy-target",
        task_id="legacy-blocked-task",
    )
    incident = {
        "incident_id": "legacy-maint-1",
        "key": "legacy-blocked-task|BLOCKED|role_offline|1|DEV|offline|time",
        "state": "OPEN",
    }

    def block(current: dict) -> dict:
        current.update(
            status="BLOCKED",
            kanban_column="BLOCKED",
            block_code="role_offline",
            block_reason="legacy-target-dev is offline",
            maintenance={
                "active_incident_id": "legacy-maint-1",
                "incidents": [incident],
                "last_resolved_at": None,
            },
        )
        return current

    target = store.update(target["manifest_path"], block)
    legacy_before = json.loads(json.dumps(target["maintenance"]))
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    worker._activate_independent_agents()

    agents = current_agents(store)
    maintainers = next(
        state
        for state in agents
        if state["independent"]["agent_name"] == "Maintainers"
    )
    monitor = next(
        state for state in agents if state["independent"]["agent_name"] == "Monitor"
    )

    assert len(agents) == 2
    assert maintainers["status"] == "RUNNING"
    assert maintainers["independent"]["active_event"]["target_task_id"] == target["task_id"]
    assert monitor["status"] == "WAITING"
    assert store.load(target["manifest_path"])["maintenance"] == legacy_before

    restarted = CDPAWorker(config, store=store)
    restarted.hydrate_runtime()
    restarted._activate_independent_agents()
    agents_after_restart = current_agents(store)

    assert len(agents_after_restart) == 2
    assert [state["task_id"] for state in agents_after_restart] == [
        state["task_id"] for state in agents
    ]
    persisted = next(
        state
        for state in agents_after_restart
        if state["independent"]["agent_name"] == "Maintainers"
    )
    assert persisted["independent"]["active_event"]["event_key"] == maintainers[
        "independent"
    ]["active_event"]["event_key"]
    assert store.load(target["manifest_path"])["maintenance"] == legacy_before


def test_worker_restart_does_not_recreate_explicitly_stopped_builtin(
    tmp_path: Path,
):
    config = seeded_config(tmp_path)
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()
    builtins = {
        state["independent"]["agent_name"]: state
        for state in store.discover()
        if state.get("task_mode") == "independent"
        and state["independent"]["agent_name"] in {"Maintainers", "Monitor"}
    }

    def stop(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "STOPPED"
        current["stopped_at"] = "2026-07-27T00:00:00+00:00"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "stopped"
        current["independent"]["enabled"] = False
        current["independent"]["active_event"] = None
        return current

    stopped = {
        name: store.update(state["manifest_path"], stop)
        for name, state in builtins.items()
    }
    CDPAWorker(config, store=store).hydrate_runtime()

    for name, tombstone in stopped.items():
        matching = [
            state
            for state in store.discover()
            if state.get("task_mode") == "independent"
            and state["independent"]["agent_name"] == name
        ]
        assert len(matching) == 1
        assert matching[0]["task_id"] == tombstone["task_id"]
        assert matching[0]["status"] == "STOPPED"
        assert matching[0]["independent"]["enabled"] is False


def test_repeated_startup_without_incidents_seeds_two_waiting_builtins_only_once(
    tmp_path: Path,
):
    config = seeded_config(tmp_path)
    store = TaskStore(config)

    CDPAWorker(config, store=store).hydrate_runtime()
    first = current_agents(store)
    CDPAWorker(config, store=store).hydrate_runtime()
    second = current_agents(store)

    assert len(first) == len(second) == 2
    assert {state["independent"]["agent_name"] for state in second} == {
        "Maintainers",
        "Monitor",
    }
    assert all(state["status"] == "WAITING" for state in second)
    assert [state["task_id"] for state in second] == [state["task_id"] for state in first]


def test_retired_special_architecture_is_absent_from_active_source():
    repository = Path(__file__).resolve().parents[1]
    roots = (
        repository / "src",
        repository / "prompts",
        repository / "tests",
    )
    retired = (
        "MaintainerCoordinator",
        "MonitorCoordinator",
        "MaintenanceDecision",
        "parse_maintenance_response",
        "ensure_maintenance_incident",
        "create_repair_task",
        "maintenance_incident_id",
        "maintenance_request_id",
    )

    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".js", ".html"}:
                continue
            text = path.read_text(encoding="utf-8")
            if path == Path(__file__):
                continue
            for symbol in retired:
                assert symbol not in text, f"retired symbol {symbol!r} remains in {path}"
