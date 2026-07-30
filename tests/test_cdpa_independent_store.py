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
    assert state["independent"]["trigger_settings"] == {
        "recovery": False,
        "interval_minutes": None,
        "task_done": False,
        "role_completed": [],
        "teams": [],
        "states": [],
        "check_all": False,
    }
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
        trigger_settings={"task_done": True, "interval_minutes": 20},
    )

    assert second["task_id"] == first["task_id"]
    assert second["team"] == "agent-monitor"
    assert second["independent"]["system_prompt"] == "Inspect progress."
    assert second["independent"]["trigger_settings"]["task_done"] is False
    assert second["independent"]["trigger_settings"]["interval_minutes"] is None
    assert len([item for item in store.discover() if item.get("task_mode") == "independent"]) == 1




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
