from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import canonical_independent_events, claim_oldest_event
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop

from test_cdpa_core import write_config
from test_cdpa_worker import FakeActions


def setup_agent(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    blocked = store.create_task(
        "Blocked target",
        requested_team="alpha",
        task_id="target-a",
    )
    blocked = store.update(
        blocked["manifest_path"],
        lambda state: {
            **state,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "role_offline",
            "block_reason": "alpha-dev is offline",
            "updated_at": "2026-07-26T00:00:00+00:00",
        },
    )
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Use @mcp-g8 and recover the affected task directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    claimed = claim_oldest_event(
        standby,
        canonical_independent_events(standby, [blocked, standby]),
        all_tasks=[blocked, standby],
    )
    claimed = store.save(claimed["manifest_path"], claimed)
    return config, store, blocked, claimed, CDPAWorker(config, store=store)


def test_independent_pre_send_uses_shared_prompt_without_route_contract(tmp_path: Path):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert hop["state"] == "sending"
    assert hop.get("expected_report_path") is None
    assert "Use @mcp-g8" in hop["prompt"]
    assert "INDEPENDENT_AGENT_OPERATING_RULE" in hop["prompt"]
    assert '"trigger_type": "recovery"' in hop["prompt"]
    assert '"target_task_id": "target-a"' in hop["prompt"]
    assert "allowed-routes" not in hop["prompt"]
    assert ".plan/" not in hop["prompt"]
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"


def test_independent_responded_waits_for_explicit_completion_without_route_parse(
    tmp_path: Path,
):
    _config, _store, _blocked, state, worker = setup_agent(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["state"] = "responded"
    hop["response"] = "Recovered target and verified the postcondition."
    hop["response_sha256"] = "a" * 64

    worker._responded(state, hop)

    assert hop["state"] == "responded"
    assert hop["route"] is None
    assert state["status"] == "RUNNING"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["active_action"] == "await_completion"
    assert len(state["hops"]) == 1




def test_independent_pause_and_resume_preserve_active_event(tmp_path: Path):
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    event_key = state["independent"]["active_event"]["event_key"]
    state = store.request_control(state["manifest_path"], "pause", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["status"] == "PAUSED"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["independent"]["enabled"] is False
    assert state["independent"]["active_event"]["event_key"] == event_key

    state = store.save(state["manifest_path"], state)
    state = store.request_control(state["manifest_path"], "resume", role="AGENT")
    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["status"] == "RUNNING"
    assert state["kanban_column"] == "INDEPENDENT_AGENTS"
    assert state["independent"]["enabled"] is True
    assert state["independent"]["active_event"]["event_key"] == event_key




def test_operator_stop_disables_independent_agent_without_respawn(tmp_path: Path):
    _config, store, _blocked, state, worker = setup_agent(tmp_path)
    state = store.request_control(state["manifest_path"], "stop", role="AGENT")

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    state = store.save(state["manifest_path"], state)
    worker.hydrate_runtime(startup=False)
    worker._activate_independent_agents()

    assert state["status"] == "STOPPED"
    assert state["independent"]["enabled"] is False
    assert state["independent"]["active_event"] is None
    assert state["independent"].get("successor_task_id") is None
    assert not [
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item["independent"].get("previous_task_id") == state["task_id"]
    ]










def test_interval_agent_stays_waiting_until_first_interval_is_due(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    monitor = store.create_independent_agent(
        "Monitor",
        system_prompt="Inspect progress.",
        trigger_settings={"interval_minutes": 30, "check_all": True},
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()

    changed = worker._activate_independent_agents()
    persisted = store.load(monitor["manifest_path"])

    assert monitor["task_id"] not in changed
    assert persisted["status"] == "WAITING"
    assert persisted["independent"]["active_event"] is None


def test_worker_activates_oldest_independent_event_and_persists_claim(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    older = store.create_task("Older", requested_team="alpha", task_id="older")
    newer = store.create_task("Newer", requested_team="beta", task_id="newer")
    for state, at in (
        (older, "2026-07-26T00:00:00+00:00"),
        (newer, "2026-07-26T00:01:00+00:00"),
    ):
        store.update(
            state["manifest_path"],
            lambda current, at=at: {
                **current,
                "status": "BLOCKED",
                "kanban_column": "BLOCKED",
                "block_code": "role_offline",
                "block_reason": "role is offline",
                "updated_at": at,
            },
        )
    standby = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks directly.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
        max_cycles=5,
    )
    worker = CDPAWorker(config, store=store)
    worker.hydrate_runtime()

    changed = worker._activate_independent_agents()
    persisted = store.load(standby["manifest_path"])

    assert persisted["status"] == "RUNNING"
    assert persisted["independent"]["active_event"]["target_task_id"] == "older"
    assert persisted["hops"][0]["state"] == "pre_send"
    assert persisted["task_id"] in changed
