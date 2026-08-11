from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_independent import (
    RECOVERY_BLOCKED_SECONDS,
    RECOVERY_INTER_TARGET_SECONDS,
    RECOVERY_TARGET_COOLDOWN_SECONDS,
    RECOVERY_WARMUP_SECONDS,
    canonical_independent_events,
    independent_cycle_limit_reached,
    independent_tags,
    load_trigger_learning,
    record_recovery_release,
    trigger_learning_path,
    validate_independent_object,
)
from playwright_auto.cdpa_learning import LearningEditError, update_trigger_learning
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.dashboard_api import DashboardAPI


UTC = timezone.utc


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(load_cdpa_config(None, repository_root=tmp_path))


def _activate(store: TaskStore, state: dict, *, trigger_type: str = "recovery") -> dict:
    def mutate(current: dict) -> dict:
        current["status"] = "RUNNING"
        current["kanban_column"] = "INDEPENDENT_AGENTS"
        current["waiting"] = None
        current["waiting_reason"] = None
        current["waiting_code"] = None
        current["independent"]["active_event"] = {
            "event_key": f"{trigger_type}:task-a:episode-1",
            "trigger_type": trigger_type,
            "occurred_at": "2026-07-31T00:00:00+00:00",
            "target_team": "alpha" if trigger_type == "recovery" else None,
            "target_task_id": "task-a" if trigger_type == "recovery" else None,
            "target_role": "DEV" if trigger_type == "recovery" else None,
            "target_hop_id": 2 if trigger_type == "recovery" else None,
            "failure_signature": "role_offline:abc" if trigger_type == "recovery" else None,
            "occurrence_count": 1,
            "check_count": 1,
        }
        current["independent"]["cycle"] = 1
        current["hops"][-1]["state"] = "responded"
        current["hops"][-1]["response"] = "Verified result."
        current["hops"][-1]["response_sha256"] = "a" * 64
        current["roles"]["AGENT"]["page_url"] = "https://chatgpt.com/c/exact"
        return current

    return store.update(state["manifest_path"], mutate)


def _blocked(task_id: str, *, blocked_at: datetime) -> dict:
    return {
        "task_id": task_id,
        "team": task_id,
        "task_mode": "workflow",
        "status": "BLOCKED",
        "blocked_at": blocked_at.isoformat(),
        "updated_at": blocked_at.isoformat(),
        "active_role": "DEV",
        "active_hop_id": 2,
        "block_code": "role_offline",
        "block_reason": "DEV tab is offline",
        "hops": [{"hop_id": 2, "target_role": "DEV"}],
        "controls": [],
    }


def test_max_cycles_zero_is_valid_default_and_unlimited(tmp_path: Path):
    store = _store(tmp_path)
    assert store.config.independent_idle_close_seconds == 60
    state = store.create_independent_agent(
        "Unlimited",
        system_prompt="Continue until the job is finished.",
        task_id="agent-unlimited-g1",
    )

    assert state["independent"]["max_cycles"] == 0
    assert validate_independent_object(state["independent"]) is None
    assert independent_cycle_limit_reached(cycle=999, max_cycles=0) is False
    assert independent_cycle_limit_reached(cycle=2, max_cycles=2) is True

    api = DashboardAPI(load_cdpa_config(None, repository_root=tmp_path))
    assert api.normalize_independent_create(
        {
            "name": "Zero",
            "system_prompt": "Run.",
            "mode": "Independent",
            "max_cycles": 0,
        }
    )["max_cycles"] == 0
    assert api.normalize_independent_settings({"max_cycles": 0}) == {"max_cycles": 0}
    with pytest.raises(Exception, match="max_cycles"):
        api.normalize_independent_settings({"max_cycles": -1})


def test_completion_reuses_same_identity_and_recurring_vs_one_shot(tmp_path: Path):
    store = _store(tmp_path)
    recovery = store.create_independent_agent(
        "Recovery",
        system_prompt="Recover.",
        task_id="agent-recovery-g1",
        trigger_settings={"recovery": True},
    )
    recovery = _activate(store, recovery, trigger_type="recovery")
    completed = store.complete_independent_task(
        recovery["manifest_path"], outcome="SUCCESS", summary="Recovered."
    )

    assert completed["task_id"] == recovery["task_id"]
    assert completed["status"] == "WAITING"
    assert completed["independent"]["enabled"] is True
    assert completed["independent"]["active_event"] is None
    assert completed["independent"]["cycle"] == 0
    assert completed["independent"].get("successor_task_id") is None
    assert completed["independent"]["job_history"][-1]["disposition"] == "COMPLETED"
    assert completed["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact"
    idle = datetime.fromisoformat(completed["independent"]["idle_since"])
    keep_open = datetime.fromisoformat(
        completed["independent"]["tab_keep_open_until"]
    )
    assert (keep_open - idle).total_seconds() == 60
    assert len([x for x in store.discover() if x.get("task_mode") == "independent"]) == 1

    manual = store.create_independent_agent(
        "Manual",
        system_prompt="Run once.",
        task_id="agent-manual-g1",
    )
    manual = _activate(store, manual, trigger_type="manual")
    completed_manual = store.complete_independent_task(
        manual["manifest_path"], outcome="SUCCESS", summary="Done."
    )
    assert completed_manual["status"] == "PAUSED"
    assert completed_manual["independent"]["enabled"] is False


def test_reset_releases_job_without_terminal_state_or_send_replay(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Resettable",
        system_prompt="Recover.",
        task_id="agent-resettable-g1",
        trigger_settings={"recovery": True},
    )
    state = _activate(store, state)

    def accepted(current: dict) -> dict:
        hop = current["hops"][-1]
        hop["state"] = "waiting"
        hop["receipt"] = {"attempt": 1, "accepted": True}
        hop["conversation_url"] = "https://chatgpt.com/c/exact"
        return current

    state = store.update(state["manifest_path"], accepted)
    reset = store.reset_independent_task(
        state["manifest_path"], reason="Operator reset", external_command_id="reset-1"
    )
    replay = store.reset_independent_task(
        state["manifest_path"], reason="Operator reset", external_command_id="reset-1"
    )

    assert reset["task_id"] == state["task_id"]
    assert reset["status"] == "WAITING"
    assert reset["terminal_state"] is None
    assert reset["independent"]["active_event"] is None
    assert reset["independent"]["job_history"][-1]["disposition"] == "RESET"
    assert reset["hops"][-2]["state"] == "abandoned"
    assert reset["hops"][-2]["receipt"]["attempt"] == 1
    assert reset["roles"]["AGENT"]["page_url"] == "https://chatgpt.com/c/exact"
    assert replay["independent"]["job_history"] == reset["independent"]["job_history"]


def test_recovery_warmup_eligibility_fifo_and_cooldowns(tmp_path: Path):
    now = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    store = _store(tmp_path)
    agent = store.create_independent_agent(
        "Recovery",
        system_prompt="Recover.",
        task_id="agent-recovery-g1",
        trigger_settings={"recovery": True},
    )

    def stamp(current: dict) -> dict:
        current["independent"]["watermarks"]["recovery_enabled_at"] = (
            now - timedelta(seconds=RECOVERY_WARMUP_SECONDS - 1)
        ).isoformat()
        return current

    agent = store.update(agent["manifest_path"], stamp)
    old = _blocked("old", blocked_at=now - timedelta(seconds=RECOVERY_BLOCKED_SECONDS + 60))
    new = _blocked("new", blocked_at=now - timedelta(seconds=RECOVERY_BLOCKED_SECONDS))
    assert canonical_independent_events(agent, [agent, old, new], now=now) == []

    def warmed(current: dict) -> dict:
        current["independent"]["watermarks"]["recovery_enabled_at"] = (
            now - timedelta(seconds=RECOVERY_WARMUP_SECONDS)
        ).isoformat()
        return current

    agent = store.update(agent["manifest_path"], warmed)
    events = canonical_independent_events(agent, [agent, new, old], now=now)
    assert [event["target_task_id"] for event in events] == ["old", "new"]

    record_recovery_release(agent["independent"], events[0], now=now)
    assert canonical_independent_events(
        agent,
        [agent, old],
        now=now + timedelta(seconds=RECOVERY_TARGET_COOLDOWN_SECONDS - 1),
    ) == []
    assert canonical_independent_events(
        agent,
        [agent, new],
        now=now + timedelta(seconds=RECOVERY_INTER_TARGET_SECONDS - 1),
    ) == []
    assert canonical_independent_events(
        agent,
        [agent, new],
        now=now + timedelta(seconds=RECOVERY_INTER_TARGET_SECONDS),
    )


def test_trigger_learning_creation_is_shared_bounded_and_incident_free(tmp_path: Path):
    (tmp_path / "LEARNING.md").write_text("# LEARNING.md\n", encoding="utf-8")
    evidence = update_trigger_learning(
        tmp_path,
        trigger="recovery",
        disposition="ADDED",
        old_text="# Recovery trigger learning",
        new_text=(
            "# Recovery trigger learning\n\n"
            "- Preserve exact accepted-send and conversation identity while releasing a blocked task."
        ),
    )
    assert evidence.path == ".learning/learning_recovery.md"
    assert "accepted-send" in (tmp_path / evidence.path).read_text(encoding="utf-8")
    with pytest.raises(LearningEditError, match="sensitive or transient"):
        update_trigger_learning(
            tmp_path,
            trigger="recovery",
            disposition="ADDED",
            old_text=(
                "- Preserve exact accepted-send and conversation identity while releasing a blocked task."
            ),
            new_text=(
                "- Preserve exact accepted-send and conversation identity while releasing a blocked task.\n"
                "  Incident cdpa-idem-12345678 failed on 2026-07-31."
            ),
        )


def test_recovery_tag_learning_and_projection_contract(tmp_path: Path):
    learning = tmp_path / ".learning" / "learning_recovery.md"
    learning.parent.mkdir()
    learning.write_text(
        "# Recovery trigger learning\n\n- Principle: preserve accepted-send identity.\n",
        encoding="utf-8",
    )
    assert trigger_learning_path(tmp_path, "recovery") == learning
    assert trigger_learning_path(tmp_path, "check_all").name == "learning_interval.md"
    loaded = load_trigger_learning(tmp_path, "recovery")
    assert loaded is not None and "preserve accepted-send identity" in loaded[1]
    assert independent_tags({"recovery": True}) == ["Recovery"]
    assert independent_tags({"recovery": False}) == []

    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Recovery",
        system_prompt="Recover.",
        task_id="agent-recovery-g1",
        trigger_settings={"recovery": True},
    )
    projection = build_task_projection(state, tasks=[state])
    agent = projection.summary["agent"]
    assert agent["tags"] == ["Recovery"]
    assert agent["max_cycles"] == 0
    assert agent["cycle"] == 0
    assert isinstance(agent["tab_open"], bool)
    assert agent["tab_keep_open_until"] is None


def test_max_cycle_setting_resets_same_event_at_durable_boundary(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Cycle reset",
        system_prompt="Continue.",
        task_id="agent-cycle-reset-g1",
        max_cycles=5,
    )
    state = _activate(store, state, trigger_type="manual")

    def accepted(current: dict) -> dict:
        hop = current["hops"][-1]
        hop["state"] = "waiting"
        hop["receipt"] = {"attempt": 1, "accepted": True}
        current["independent"]["cycle"] = 3
        return current

    state = store.update(state["manifest_path"], accepted)
    changed = store.update_independent_agent(
        state["manifest_path"], max_cycles=2
    )
    assert changed["independent"]["max_cycles"] == 2
    assert changed["independent"]["cycle"] == 3
    assert changed["independent"]["settings_reset_request"]["event_key"] == (
        changed["independent"]["active_event"]["event_key"]
    )

    def responded(current: dict) -> dict:
        current["hops"][-1]["state"] = "responded"
        current["hops"][-1]["response"] = "Boundary reached."
        current["hops"][-1]["response_sha256"] = "b" * 64
        return current

    changed = store.update(changed["manifest_path"], responded)
    reset = store.finalize_independent_settings_reset(changed["manifest_path"])
    assert reset["independent"]["active_event"]["event_key"] == (
        state["independent"]["active_event"]["event_key"]
    )
    assert reset["independent"]["cycle"] == 1
    assert reset["hops"][-1]["state"] == "pre_send"
    assert reset["independent"]["job_history"][-1]["disposition"] == (
        "RESET_BY_SETTINGS"
    )


def test_legacy_terminal_generation_migrates_to_same_paused_identity(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Legacy",
        system_prompt="Legacy.",
        task_id="agent-legacy-g1",
        enabled=False,
    )

    def legacy_terminal(current: dict) -> dict:
        current["status"] = "STOPPED"
        current["kanban_column"] = "STOPPED"
        current["terminal_state"] = "STOPPED"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["stopped_at"] = "2026-07-30T00:00:00+00:00"
        current["independent"]["successor_task_id"] = None
        return current

    terminal = store.update(state["manifest_path"], legacy_terminal)
    migrated = store.normalize_legacy_independent_agents()
    assert [item["task_id"] for item in migrated] == [terminal["task_id"]]
    current = store.load(terminal["manifest_path"])
    assert current["task_id"] == terminal["task_id"]
    assert current["status"] == "PAUSED"
    assert current["terminal_state"] is None
    assert current["active_hop_id"] is not None
    assert current["independent"]["job_history"][-1]["disposition"] == (
        "MIGRATED_LEGACY"
    )


def test_hydration_fails_closed_with_two_enabled_recovery_owners(tmp_path: Path):
    from playwright_auto.cdpa_worker import CDPAWorker

    store = _store(tmp_path)
    first = store.create_independent_agent(
        "Recovery one",
        system_prompt="Recover.",
        task_id="agent-recovery-one-g1",
        trigger_settings={"recovery": True},
    )
    second = store.create_independent_agent(
        "Recovery two",
        system_prompt="Recover.",
        task_id="agent-recovery-two-g1",
        enabled=False,
    )

    def corrupt_owner(current: dict) -> dict:
        current["independent"]["enabled"] = True
        current["independent"]["trigger_settings"]["recovery"] = True
        return current

    store.update(second["manifest_path"], corrupt_owner)
    worker = CDPAWorker(store.config, store=store)
    catalog = worker.hydrate_runtime(read_only=True)
    assert first["task_id"]
    assert catalog["complete"] is False
    assert any(
        "multiple enabled Independent Agents own the Recovery trigger"
        in item["error"]
        for item in catalog["errors"]
    )


def test_runtime_projection_collapses_legacy_generations(tmp_path: Path):
    from playwright_auto.cdpa_worker import CDPAWorker

    store = _store(tmp_path)
    first = store.create_independent_agent(
        "Legacy chain",
        system_prompt="First.",
        task_id="agent-legacy-chain-g1",
        enabled=False,
    )

    def terminal(current: dict) -> dict:
        current["status"] = "DONE"
        current["kanban_column"] = "DONE"
        current["terminal_state"] = "DONE"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["completed_at"] = "2026-07-30T00:00:00+00:00"
        return current

    first = store.update(first["manifest_path"], terminal)
    second = store.create_independent_agent(
        "Legacy chain",
        system_prompt="Second.",
        enabled=False,
    )
    worker = CDPAWorker(store.config, store=store)
    catalog = worker.hydrate_runtime(read_only=True)
    assert catalog["complete"] is True
    assert first["task_id"] not in worker.registry.tasks_by_id
    assert second["task_id"] in worker.registry.tasks_by_id
    detail = worker.runtime_db.get_task_detail(second["task_id"])
    assert detail is not None
    assert detail["independent_history"][0]["generation"] == 2
    assert any(
        item["generation"] == 1
        for item in detail["independent_history"]
    )


def test_enabling_recovery_trigger_restarts_warmup(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Warmup transition",
        system_prompt="Recover.",
        task_id="agent-warmup-transition-g1",
        trigger_settings={"recovery": False},
    )
    changed = store.update_independent_agent(
        state["manifest_path"],
        trigger_settings={"recovery": True},
    )
    enabled_at = datetime.fromisoformat(
        changed["independent"]["watermarks"]["recovery_enabled_at"]
    )
    target = _blocked(
        "warmup-target",
        blocked_at=enabled_at - timedelta(seconds=RECOVERY_BLOCKED_SECONDS + 60),
    )
    assert canonical_independent_events(
        changed,
        [changed, target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS - 1),
    ) == []
    assert canonical_independent_events(
        changed,
        [changed, target],
        now=enabled_at + timedelta(seconds=RECOVERY_WARMUP_SECONDS),
    )


def test_nonterminal_identity_wins_over_newer_terminal_legacy_generation(tmp_path: Path):
    from playwright_auto.cdpa_worker import CDPAWorker

    store = _store(tmp_path)
    first = store.create_independent_agent(
        "Split legacy",
        system_prompt="First.",
        task_id="agent-split-legacy-g1",
        enabled=False,
    )

    def terminalize(current: dict) -> dict:
        current["status"] = "DONE"
        current["kanban_column"] = "DONE"
        current["terminal_state"] = "DONE"
        current["active_role"] = None
        current["active_hop_id"] = None
        current["completed_at"] = "2026-07-30T00:00:00+00:00"
        return current

    first = store.update(first["manifest_path"], terminalize)
    second = store.create_independent_agent(
        "Split legacy",
        system_prompt="Second.",
        enabled=False,
    )
    second = store.update(second["manifest_path"], terminalize)

    def restore_older_identity(current: dict) -> dict:
        current["status"] = "PAUSED"
        current["kanban_column"] = "PAUSED"
        current["terminal_state"] = None
        current["active_role"] = "AGENT"
        current["active_hop_id"] = current["hops"][-1]["hop_id"]
        current.pop("completed_at", None)
        return current

    first = store.update(first["manifest_path"], restore_older_identity)
    worker = CDPAWorker(store.config, store=store)
    catalog = worker.hydrate_runtime(read_only=True)
    assert catalog["complete"] is True
    assert first["task_id"] in worker.registry.tasks_by_id
    assert second["task_id"] not in worker.registry.tasks_by_id


def test_legacy_default_max_cycles_migrates_to_unlimited(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Legacy default",
        system_prompt="Run.",
        task_id="agent-legacy-default-g1",
        enabled=False,
        max_cycles=1,
    )

    def remove_explicit_marker(current: dict) -> dict:
        current["independent"].pop("max_cycles_explicit", None)
        return current

    legacy = store.update(state["manifest_path"], remove_explicit_marker)
    assert legacy["independent"]["max_cycles"] == 1
    migrated = store.normalize_legacy_independent_agents()
    current = store.load(state["manifest_path"])
    assert any(item["task_id"] == state["task_id"] for item in migrated)
    assert current["independent"]["max_cycles"] == 0
    assert current["independent"]["max_cycles_explicit"] is False


def test_explicit_nondefault_legacy_limit_is_preserved(tmp_path: Path):
    store = _store(tmp_path)
    state = store.create_independent_agent(
        "Legacy explicit",
        system_prompt="Run.",
        task_id="agent-legacy-explicit-g1",
        enabled=False,
        max_cycles=3,
    )

    def remove_explicit_marker(current: dict) -> dict:
        current["independent"].pop("max_cycles_explicit", None)
        return current

    store.update(state["manifest_path"], remove_explicit_marker)
    store.normalize_legacy_independent_agents()
    current = store.load(state["manifest_path"])
    assert current["independent"]["max_cycles"] == 3
    assert current["independent"]["max_cycles_explicit"] is True


def test_independent_reset_command_kind_is_durable(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite")
    db.ensure_schema()
    command = db.enqueue_command(
        command_id="cmd-reset-1",
        idempotency_key="reset-1",
        kind="independent_reset",
        task_id="agent-reset-g1",
        expected_task_version=None,
        payload={"reason": "Operator reset"},
    )
    assert command["status"] == "queued"
    assert command["kind"] == "independent_reset"


def test_consecutive_self_route_guard_is_not_canonical_recovery_eligible(tmp_path: Path):
    now = datetime(2026, 8, 10, 3, 30, tzinfo=UTC)
    store = _store(tmp_path)
    agent = store.create_independent_agent(
        "Recovery guard exclusion",
        system_prompt="Recover eligible operational blocks.",
        task_id="agent-guard-exclusion-g1",
        trigger_settings={"recovery": True},
    )

    def warmed(current: dict) -> dict:
        current["independent"]["watermarks"]["recovery_enabled_at"] = (
            now - timedelta(seconds=RECOVERY_WARMUP_SECONDS + 1)
        ).isoformat()
        return current

    agent = store.update(agent["manifest_path"], warmed)
    guarded = _blocked(
        "guarded-self-route",
        blocked_at=now - timedelta(seconds=RECOVERY_BLOCKED_SECONDS + 1),
    )
    guarded["block_code"] = "consecutive_self_route_limit"
    guarded["block_reason"] = "PLAN self-route streak 3 requires explicit operator Resume"

    assert canonical_independent_events(agent, [agent, guarded], now=now) == []
