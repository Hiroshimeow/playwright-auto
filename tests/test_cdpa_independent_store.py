from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import canonical_independent_events
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker


def test_independent_agent_is_one_waiting_task_with_one_agent_role(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)

    state = store.create_independent_agent(
        "Release Watcher",
        system_prompt="Watch releases and report material changes.",
        task_id="agent-release-watcher-g1",
    )

    assert state["task_mode"] == "independent"
    assert state["team"] == "agent-release-watcher"
    assert state["status"] == "WAITING"
    assert state["waiting"]["reason"] == "trigger"
    assert set(state["roles"]) == {"AGENT"}
    assert state["roles"]["AGENT"]["physical_role"] == "agent-release-watcher-agent"
    assert state["hops"][0]["kind"] == "independent_job"
    assert state["hops"][0]["state"] == "waiting_trigger"
    assert state["independent"]["agent_name"] == "Release Watcher"
    assert state["independent"]["agent_generation"] == 1
    assert state["independent"]["active_event"] is None
    assert store.load(state["manifest_path"])["task_mode"] == "independent"


def test_same_agent_name_reuses_current_identity_and_exact_team(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_independent_agent(
        "Monitor",
        system_prompt="Inspect progress.",
        task_id="agent-monitor-g1",
    )

    second = store.create_independent_agent(
        " monitor ",
        system_prompt="Ignored because settings are changed explicitly.",
        task_id="different-id-must-not-be-created",
    )

    assert second["task_id"] == first["task_id"]
    assert second["team"] == "agent-monitor"
    assert second["independent"]["system_prompt"] == "Inspect progress."
    assert len([item for item in store.discover() if item.get("task_mode") == "independent"]) == 1


def test_terminal_same_name_recreation_preserves_durable_identity_and_settings(
    tmp_path: Path,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    original = store.create_independent_agent(
        "Release Watcher",
        system_prompt="Watch releases.",
        trigger_settings={"task_done": True, "interval_minutes": 60},
        max_cycles=4,
    )

    def stop(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["kanban_column"] = "STOPPED"
        current["stopped_at"] = "2026-07-27T00:00:00+00:00"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["active_action"] = "stopped"
        current["roles"]["AGENT"].update(
            page_id="page-release-watcher",
            page_url="https://chatgpt.com/c/exact-release-watcher",
            conversation_generation=7,
            constructor_sent_generation=7,
        )
        current["independent"].update(
            enabled=False,
            active_event=None,
            occurrence_counts={"release-team:signature": 3},
            new_chat_next_job=True,
            new_chat_deferred_task_id=current["task_id"],
            last_outcome={
                "outcome": "SUCCESS",
                "summary": "Previous job completed.",
                "target_task_id": "release-task",
                "repair_task_id": None,
                "requested_at": "2026-07-27T00:00:00+00:00",
            },
        )
        current["independent"]["watermarks"]["last_interval_slot"] = 123
        return current

    stopped = store.update(original["manifest_path"], stop)
    recreated = store.create_independent_agent(
        " release watcher ",
        system_prompt="Watch releases.",
    )

    assert recreated["independent"]["agent_generation"] == 2
    assert recreated["independent"]["previous_task_id"] == stopped["task_id"]
    assert recreated["team"] == stopped["team"]
    assert recreated["roles"]["AGENT"]["page_id"] == "page-release-watcher"
    assert recreated["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact-release-watcher"
    assert recreated["roles"]["AGENT"]["conversation_generation"] == 7
    assert recreated["roles"]["AGENT"]["constructor_sent_generation"] == 7
    assert recreated["independent"]["trigger_settings"] == stopped["independent"]["trigger_settings"]
    assert recreated["independent"]["max_cycles"] == 4
    assert recreated["independent"]["watermarks"] == stopped["independent"]["watermarks"]
    assert recreated["independent"]["occurrence_counts"] == stopped["independent"]["occurrence_counts"]
    assert recreated["independent"]["last_outcome"] == stopped["independent"]["last_outcome"]
    assert recreated["independent"]["new_chat_next_job"] is True
    assert recreated["independent"]["new_chat_deferred_task_id"] == stopped["task_id"]


def test_recovery_trigger_has_one_enabled_owner_but_nonexclusive_triggers_are_shared(
    tmp_path: Path,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True, "task_done": True},
    )

    with pytest.raises(ValueError, match="exclusive recovery trigger"):
        store.create_independent_agent(
            "Recovery Two",
            system_prompt="Also recover tasks.",
            task_id="agent-recovery-two-g1",
            trigger_settings={"recovery": True},
        )

    shared = store.create_independent_agent(
        "Done Watcher",
        system_prompt="Review completed tasks.",
        task_id="agent-done-watcher-g1",
        trigger_settings={"task_done": True, "interval_minutes": 30},
    )
    assert shared["independent"]["trigger_settings"]["task_done"] is True


def test_501st_completion_uses_trigger_cursor_and_does_not_replay_after_restart(
    tmp_path: Path,
):
    config = replace(
        load_cdpa_config(None, repository_root=tmp_path),
        independent_seed_builtins=False,
    )
    store = TaskStore(config)
    source = store.create_task(
        "Completed source",
        requested_team="source-team",
        task_id="source-completed",
    )

    def finish_source(current: dict) -> dict:
        current.update(
            status="DONE",
            terminal_state="DONE",
            kanban_column="DONE",
            active_role=None,
            active_hop_id=None,
            active_action="completed",
            completed_at="2026-07-27T00:00:00+00:00",
        )
        return current

    source = store.update(source["manifest_path"], finish_source)
    agent = store.create_independent_agent(
        "Done Watcher",
        system_prompt="Review completed tasks.",
        trigger_settings={"task_done": True},
    )
    event = canonical_independent_events(agent, [source, agent])[0]

    def ready(current: dict) -> dict:
        current["status"] = "RUNNING"
        current["waiting"] = None
        current["waiting_reason"] = None
        current["waiting_code"] = None
        current["independent"]["watermarks"]["seen_event_keys"] = [
            f"legacy:{index}" for index in range(500)
        ]
        current["independent"]["active_event"] = event
        current["independent"]["cycle"] = 1
        current["hops"][0]["state"] = "responded"
        current["hops"][0]["response"] = "Reviewed and verified."
        current["hops"][0]["response_sha256"] = "b" * 64
        return current

    active = store.update(agent["manifest_path"], ready)
    completed, successor = store.complete_independent_task(
        active["manifest_path"],
        outcome="SUCCESS",
        summary="Completed task reviewed.",
        target_task_id=source["task_id"],
    )

    assert completed["status"] == "DONE"
    assert successor["status"] == "WAITING"
    assert len(successor["independent"]["watermarks"]["seen_event_keys"]) == 500
    assert successor["independent"]["watermarks"]["event_cursors"]["task_done"] == {
        "occurred_at": event["occurred_at"],
        "event_key": event["event_key"],
    }

    restarted = CDPAWorker(config, store=store)
    restarted.hydrate_runtime()
    restarted._activate_independent_agents()
    persisted = store.load(successor["manifest_path"])

    assert persisted["status"] == "WAITING"
    assert persisted["independent"]["active_event"] is None
    assert canonical_independent_events(persisted, [source, persisted]) == []


def test_complete_and_respawn_is_deterministic_and_copies_conversation(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        task_id="agent-maintainers-g1",
        trigger_settings={"recovery": True},
    )

    def ready(current: dict) -> dict:
        current["status"] = "RUNNING"
        current["kanban_column"] = "INDEPENDENT_AGENTS"
        current["waiting"] = None
        current["waiting_reason"] = None
        current["waiting_code"] = None
        current["independent"]["active_event"] = {
            "event_key": "recovery:task-a:1",
            "trigger_type": "recovery",
            "occurred_at": "2026-07-26T00:00:00+00:00",
            "target_task_id": "task-a",
            "target_team": "alpha",
            "occurrence_count": 1,
        }
        current["roles"]["AGENT"]["page_url"] = "https://chatgpt.com/c/exact-conversation"
        current["roles"]["AGENT"]["conversation_generation"] = 4
        current["roles"]["AGENT"]["constructor_sent_generation"] = 4
        current["hops"][0]["state"] = "responded"
        current["hops"][0]["response"] = "Recovered and verified."
        current["hops"][0]["response_sha256"] = "a" * 64
        return current

    active = store.update(state["manifest_path"], ready)
    completed, successor = store.complete_independent_task(
        active["manifest_path"],
        outcome="SUCCESS",
        summary="Target recovered and stable.",
    )
    replay_completed, replay_successor = store.complete_independent_task(
        active["manifest_path"],
        outcome="SUCCESS",
        summary="Target recovered and stable.",
    )

    assert completed["status"] == "DONE"
    assert successor["status"] == "WAITING"
    assert successor["team"] == completed["team"]
    assert successor["independent"]["agent_generation"] == 2
    assert successor["independent"]["previous_task_id"] == completed["task_id"]
    assert completed["independent"]["successor_task_id"] == successor["task_id"]
    assert successor["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact-conversation"
    assert successor["roles"]["AGENT"]["conversation_generation"] == 4
    assert successor["roles"]["AGENT"]["constructor_sent_generation"] == 4
    assert replay_completed["task_id"] == completed["task_id"]
    assert replay_successor["task_id"] == successor["task_id"]
    assert len(
        [
            item
            for item in store.discover()
            if item.get("task_mode") == "independent"
            and item.get("team") == "agent-maintainers"
            and item.get("status") != "DONE"
        ]
    ) == 1
