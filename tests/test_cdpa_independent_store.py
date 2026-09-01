from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import canonical_independent_events, validate_independent_object
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
        "daily_at": None,
        "task_done": False,
        "role_completed": [],
        "teams": [],
        "states": [],
        "check_all": False,
    }
    assert store.load(state["manifest_path"])["task_mode"] == "independent"


def test_independent_temporary_chat_defaults_true_explicit_false_and_legacy_missing(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)

    temporary = store.create_independent_agent(
        "Temporary Watcher",
        system_prompt="Inspect once.",
        task_id="agent-temporary-watcher-g1",
    )
    persistent = store.create_independent_agent(
        "Persistent Watcher",
        system_prompt="Keep the conversation.",
        task_id="agent-persistent-watcher-g1",
        temporary_chat=False,
    )
    legacy = dict(persistent["independent"])
    legacy.pop("temporary_chat", None)

    assert temporary["independent"]["temporary_chat"] is True
    assert persistent["independent"]["temporary_chat"] is False
    assert validate_independent_object(legacy) is None
    assert legacy.get("temporary_chat", False) is False


def test_independent_recreation_preserves_legacy_persistent_mode(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_independent_agent(
        "Legacy Persistent",
        system_prompt="Keep the conversation.",
        task_id="agent-legacy-persistent-g1",
        temporary_chat=False,
    )
    first = store.update(
        first["manifest_path"],
        lambda current: (
            current["independent"].pop("temporary_chat", None) or current
        ),
    )
    store.delete_independent_agent(first["manifest_path"])

    recreated = store.create_independent_agent(
        "Legacy Persistent",
        system_prompt="Keep the conversation.",
    )

    assert recreated["independent"]["temporary_chat"] is False


def test_builtin_seed_stays_persistent(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)

    seeded = store.seed_independent_agent(
        "Maintainers",
        system_prompt="Recover tasks.",
        trigger_settings={"recovery": True},
    )

    assert seeded["independent"]["temporary_chat"] is False


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




@pytest.mark.parametrize(
    ("old_interval", "new_interval", "release"),
    [
        (30, 60, "complete"),
        (30, 60, "reset"),
        (60, 30, "complete"),
        (60, 30, "reset"),
    ],
)
def test_cadence_change_release_keeps_new_interval_watermark(
    tmp_path: Path,
    old_interval: int,
    new_interval: int,
    release: str,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        task_id="agent-cadence-watcher-g1",
        trigger_settings={"interval_minutes": old_interval, "check_all": True},
    )
    current = datetime.now(timezone.utc)
    old_slot = int(current.timestamp() // (old_interval * 60))
    old_event = {
        "event_key": f"check-all:cadence watcher:{old_slot}",
        "trigger_type": "check_all",
        "occurred_at": datetime.fromtimestamp(
            old_slot * old_interval * 60, timezone.utc
        ).isoformat(),
        "check_count": old_slot,
    }

    def activate(value: dict) -> dict:
        value["status"] = "RUNNING"
        value["waiting"] = None
        value["waiting_reason"] = None
        value["waiting_code"] = None
        value["independent"]["active_event"] = old_event
        value["independent"]["cycle"] = 1
        value["hops"][0]["state"] = "responded" if release == "complete" else "waiting"
        if release == "complete":
            value["hops"][0]["response"] = "Checked."
            value["hops"][0]["response_sha256"] = "a" * 64
        return value

    active = store.update(state["manifest_path"], activate)
    changed = store.update_independent_agent(
        active["manifest_path"],
        trigger_settings={"interval_minutes": new_interval, "check_all": True},
    )
    rebased_slot = changed["independent"]["watermarks"]["last_interval_slot"]
    assert rebased_slot == int(datetime.now(timezone.utc).timestamp() // (new_interval * 60))

    if release == "complete":
        released = store.complete_independent_task(
            changed["manifest_path"], outcome="SUCCESS", summary="Checked."
        )
    else:
        released = store.reset_independent_task(
            changed["manifest_path"], reason="Release old cadence event."
        )

    assert released["independent"]["watermarks"]["last_interval_slot"] == rebased_slot
    next_boundary = datetime.fromtimestamp(
        (rebased_slot + 1) * new_interval * 60, timezone.utc
    )
    events = canonical_independent_events(released, [released], now=next_boundary)
    assert [event["check_count"] for event in events] == [rebased_slot + 1]

    deleted = store.delete_independent_agent(released["manifest_path"])
    regenerated = store.create_independent_agent(
        "Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        trigger_settings={"interval_minutes": new_interval, "check_all": True},
    )
    assert deleted["status"] == "STOPPED"
    assert regenerated["independent"]["agent_generation"] == 2
    assert regenerated["independent"]["watermarks"]["last_interval_slot"] == rebased_slot
    regenerated_events = canonical_independent_events(
        regenerated, [deleted, regenerated], now=next_boundary
    )
    assert [event["check_count"] for event in regenerated_events] == [rebased_slot + 1]


@pytest.mark.parametrize(("old_interval", "new_interval"), [(30, 60), (60, 30)])
def test_generation_recreation_rebases_changed_interval_watermark(
    tmp_path: Path,
    old_interval: int,
    new_interval: int,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    first = store.create_independent_agent(
        "Generation Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        task_id="agent-generation-cadence-watcher-g1",
        trigger_settings={"interval_minutes": old_interval, "check_all": True},
    )
    old_slot = first["independent"]["watermarks"]["last_interval_slot"]

    def preserve_history(value: dict) -> dict:
        value["independent"]["watermarks"]["seen_event_keys"] = ["task_done:history:1"]
        value["independent"]["watermarks"]["event_cursors"] = {
            "task_done": {
                "occurred_at": "2026-07-31T00:00:00+00:00",
                "event_key": "task_done:history:1",
            }
        }
        return value

    first = store.update(first["manifest_path"], preserve_history)
    deleted = store.delete_independent_agent(first["manifest_path"])
    recreated = store.create_independent_agent(
        "Generation Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        trigger_settings={"interval_minutes": new_interval, "check_all": True},
    )

    expected_slot = int(datetime.now(timezone.utc).timestamp() // (new_interval * 60))
    watermarks = recreated["independent"]["watermarks"]
    assert deleted["status"] == "STOPPED"
    assert recreated["independent"]["agent_generation"] == 2
    assert watermarks["last_interval_slot"] == expected_slot
    assert watermarks["last_interval_slot"] != old_slot
    assert watermarks["seen_event_keys"] == ["task_done:history:1"]
    assert watermarks["event_cursors"]["task_done"]["event_key"] == "task_done:history:1"

    next_boundary = datetime.fromtimestamp(
        (expected_slot + 1) * new_interval * 60, timezone.utc
    )
    events = canonical_independent_events(recreated, [deleted, recreated], now=next_boundary)
    assert [event["check_count"] for event in events] == [expected_slot + 1]


def test_same_cadence_generation_recreation_preserves_watermark_and_history(
    tmp_path: Path,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    interval = 30
    first = store.create_independent_agent(
        "Generation Same Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        task_id="agent-generation-same-cadence-watcher-g1",
        trigger_settings={"interval_minutes": interval, "check_all": True},
    )
    slot = first["independent"]["watermarks"]["last_interval_slot"]

    def preserve_history(value: dict) -> dict:
        value["independent"]["watermarks"]["seen_event_keys"] = ["task_done:history:2"]
        value["independent"]["watermarks"]["event_cursors"] = {
            "task_done": {
                "occurred_at": "2026-07-31T01:00:00+00:00",
                "event_key": "task_done:history:2",
            }
        }
        return value

    first = store.update(first["manifest_path"], preserve_history)
    deleted = store.delete_independent_agent(first["manifest_path"])
    recreated = store.create_independent_agent(
        "Generation Same Cadence Watcher",
        system_prompt="Check all workflow tasks.",
        trigger_settings={"interval_minutes": interval, "check_all": True},
    )

    watermarks = recreated["independent"]["watermarks"]
    assert watermarks["last_interval_slot"] == slot
    assert watermarks["seen_event_keys"] == ["task_done:history:2"]
    assert watermarks["event_cursors"]["task_done"]["event_key"] == "task_done:history:2"
    same_boundary = datetime.fromtimestamp(slot * interval * 60, timezone.utc)
    assert canonical_independent_events(recreated, [deleted, recreated], now=same_boundary) == []
    next_boundary = datetime.fromtimestamp((slot + 1) * interval * 60, timezone.utc)
    events = canonical_independent_events(recreated, [deleted, recreated], now=next_boundary)
    assert [event["check_count"] for event in events] == [slot + 1]


def test_same_cadence_completion_consumes_current_interval_without_duplicate(
    tmp_path: Path,
):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    interval = 30
    state = store.create_independent_agent(
        "Interval Watcher",
        system_prompt="Check on the interval.",
        task_id="agent-interval-watcher-g1",
        trigger_settings={"interval_minutes": interval},
    )
    current_slot = state["independent"]["watermarks"]["last_interval_slot"]
    event = {
        "event_key": f"interval:interval watcher:{current_slot}",
        "trigger_type": "interval",
        "occurred_at": datetime.fromtimestamp(
            current_slot * interval * 60, timezone.utc
        ).isoformat(),
        "check_count": current_slot,
    }

    def activate(value: dict) -> dict:
        value["status"] = "RUNNING"
        value["waiting"] = None
        value["waiting_reason"] = None
        value["waiting_code"] = None
        value["independent"]["active_event"] = event
        value["independent"]["cycle"] = 1
        value["hops"][0]["state"] = "responded"
        value["hops"][0]["response"] = "Checked."
        value["hops"][0]["response_sha256"] = "b" * 64
        return value

    active = store.update(state["manifest_path"], activate)
    released = store.complete_independent_task(
        active["manifest_path"], outcome="SUCCESS", summary="Checked."
    )
    same_boundary = datetime.fromtimestamp(current_slot * interval * 60, timezone.utc)
    assert canonical_independent_events(released, [released], now=same_boundary) == []

    next_boundary = datetime.fromtimestamp(
        (current_slot + 1) * interval * 60, timezone.utc
    )
    events = canonical_independent_events(released, [released], now=next_boundary)
    assert [event["check_count"] for event in events] == [current_slot + 1]


def test_manual_run_completion_keeps_interval_agent_enabled(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "Interval Manual Watcher",
        system_prompt="Check periodically and on demand.",
        task_id="agent-interval-manual-watcher-g1",
        trigger_settings={"interval_minutes": 30, "check_all": True},
    )
    original_settings = dict(state["independent"]["trigger_settings"])
    active = store.run_independent_now(
        state["manifest_path"],
        trigger_type="manual",
        instruction="Run one explicit check now.",
    )

    def respond(current: dict) -> dict:
        hop = next(
            item
            for item in current["hops"]
            if item.get("hop_id") == current.get("active_hop_id")
        )
        hop["state"] = "responded"
        hop["response"] = "Manual check complete."
        hop["response_sha256"] = "c" * 64
        return current

    active = store.update(active["manifest_path"], respond)
    completed = store.complete_independent_task(
        active["manifest_path"], outcome="SUCCESS", summary="Manual check complete."
    )

    assert completed["independent"]["enabled"] is True
    assert completed["status"] == "WAITING"
    assert completed["waiting_code"] == "trigger"
    assert completed["independent"]["trigger_settings"] == original_settings


def test_manual_run_completion_pauses_nonrecurring_agent(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_independent_agent(
        "One Shot Manual Agent",
        system_prompt="Run only when explicitly requested.",
        task_id="agent-one-shot-manual-g1",
    )
    active = store.run_independent_now(
        state["manifest_path"],
        trigger_type="manual",
        instruction="Run once.",
    )

    def respond(current: dict) -> dict:
        hop = next(
            item
            for item in current["hops"]
            if item.get("hop_id") == current.get("active_hop_id")
        )
        hop["state"] = "responded"
        hop["response"] = "One-shot check complete."
        hop["response_sha256"] = "d" * 64
        return current

    active = store.update(active["manifest_path"], respond)
    completed = store.complete_independent_task(
        active["manifest_path"], outcome="SUCCESS", summary="One-shot check complete."
    )

    assert completed["independent"]["enabled"] is False
    assert completed["status"] == "PAUSED"
    assert completed["waiting"] is None


def test_completion_reuses_long_lived_identity_and_conversation(tmp_path: Path):
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
        current["independent"]["cycle"] = 1
        current["roles"]["AGENT"]["page_url"] = "https://chatgpt.com/c/exact-conversation"
        current["roles"]["AGENT"]["conversation_generation"] = 4
        current["roles"]["AGENT"]["constructor_sent_generation"] = 4
        current["hops"][0]["state"] = "responded"
        current["hops"][0]["response"] = "Recovered and verified."
        current["hops"][0]["response_sha256"] = "a" * 64
        return current

    active = store.update(state["manifest_path"], ready)
    completed = store.complete_independent_task(
        active["manifest_path"],
        outcome="SUCCESS",
        summary="Target recovered and stable.",
    )
    replay = store.complete_independent_task(
        active["manifest_path"],
        outcome="SUCCESS",
        summary="Target recovered and stable.",
    )

    assert completed["task_id"] == active["task_id"]
    assert completed["status"] == "WAITING"
    assert completed["independent"]["agent_generation"] == 1
    assert completed["independent"]["successor_task_id"] is None
    assert completed["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact-conversation"
    assert completed["roles"]["AGENT"]["conversation_generation"] == 4
    assert completed["roles"]["AGENT"]["constructor_sent_generation"] == 4
    assert replay["task_id"] == completed["task_id"]
    assert replay["independent"]["job_history"] == completed["independent"]["job_history"]
    identities = [
        item
        for item in store.discover()
        if item.get("task_mode") == "independent"
        and item.get("team") == "agent-maintainers"
    ]
    assert len(identities) == 1
