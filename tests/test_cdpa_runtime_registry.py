from __future__ import annotations

from pathlib import Path

from playwright_auto.cdpa_runtime_registry import CDPARuntimeRegistry


def task(
    task_id: str,
    *,
    team: str | None = None,
    status: str = "RUNNING",
    hop_state: str = "waiting",
    dependencies: tuple[str, ...] = (),
    controls: list[dict] | None = None,
    updated_at: str = "2026-07-25T00:00:00+00:00",
) -> dict:
    return {
        "task_id": task_id,
        "team": team or task_id,
        "status": status,
        "manifest_path": f"/repo/.plan/{team or task_id}/{task_id}.json",
        "active_hop_id": 1 if status not in {"DONE", "STOPPED"} else None,
        "hops": ([{"hop_id": 1, "state": hop_state}] if status not in {"DONE", "STOPPED"} else []),
        "depends_on_task_ids": list(dependencies),
        "controls": controls or [],
        "cleanup": {"state": "ACTIVE"},
        "updated_at": updated_at,
    }


def test_hydrate_builds_identity_dependency_and_team_indexes():
    registry = CDPARuntimeRegistry.hydrate(
        [task("parent", team="alpha"), task("child", team="beta", dependencies=("parent",))]
    )

    assert set(registry.tasks_by_id) == {"parent", "child"}
    assert registry.paths_by_id["parent"] == Path("/repo/.plan/alpha/parent.json")
    assert registry.dependency_children == {"parent": {"child"}}
    assert registry.team_members == {"alpha": {"parent"}, "beta": {"child"}}
    assert registry.affected_by("parent") == {"parent", "child"}


def test_due_set_excludes_idle_terminal_paused_blocked_and_dependency_waiters():
    registry = CDPARuntimeRegistry.hydrate(
        [
            task("active", status="RUNNING", hop_state="waiting"),
            task("paused", status="PAUSED"),
            task("blocked", status="BLOCKED"),
            task("done", status="DONE"),
            task("parent", status="RUNNING"),
            task("waiting", status="WAITING", dependencies=("parent",)),
        ],
        now=1000.0,
    )

    assert registry.due_task_ids(1000.0) == ()
    assert registry.due_task_ids(1000.5) == ("active", "parent")


def test_controls_cleanup_and_ready_waiters_are_due():
    parent = task("parent", status="DONE")
    waiting = task("waiting", status="WAITING", dependencies=("parent",))
    paused = task(
        "paused",
        status="PAUSED",
        controls=[{"control_id": 1, "status": "requested", "action": "resume"}],
    )
    clearing = task("clearing", status="DONE")
    clearing["cleanup"] = {"state": "CLEARING"}

    registry = CDPARuntimeRegistry.hydrate([parent, waiting, paused, clearing], now=1000.0)

    assert registry.due_task_ids(1000.0) == ()
    assert registry.due_task_ids(1000.5) == ("clearing", "paused", "waiting")


def test_update_schedules_only_self_children_and_same_team_members():
    registry = CDPARuntimeRegistry.hydrate(
        [
            task("owner", team="alpha"),
            task("queued", team="alpha", status="WAITING"),
            task("child", team="beta", status="WAITING", dependencies=("owner",)),
            task("unrelated", team="gamma", status="PAUSED"),
        ],
        now=1000.0,
    )
    changed = task("owner", team="alpha", status="DONE")

    affected = registry.update_task(changed, now=1001.0)

    assert affected == {"owner", "queued", "child"}
    assert "unrelated" not in affected
    assert registry.due_task_ids(1001.0) == ()
    assert set(registry.due_task_ids(1001.5)) == {"queued", "child"}


def test_next_due_at_tracks_future_terminal_cleanup():
    done = task("done", status="DONE", updated_at="1970-01-01T00:16:40+00:00")
    registry = CDPARuntimeRegistry.hydrate(
        [done],
        now=1000.0,
        cleanup_idle_seconds=60.0,
    )

    assert registry.due_task_ids(1059.0) == ()
    assert registry.next_due_at() == 1060.0
    assert registry.due_task_ids(1060.0) == ("done",)


def test_replaced_history_is_never_due_and_does_not_hold_past_deadline():
    old = task("old", status="STOPPED", updated_at="1970-01-01T00:00:00+00:00")
    replacement = task("replacement", status="DONE")
    replacement["replaces_task_id"] = "old"

    registry = CDPARuntimeRegistry.hydrate(
        [old, replacement], now=1000.0, cleanup_idle_seconds=60.0
    )

    assert registry.immutable_task_ids == {"old"}
    assert registry.due_task_ids(1000.0) == ()
    assert registry.next_due_at() is not None
    assert registry.next_due_at() > 1000.0


def test_independent_standby_waits_for_trigger_and_responded_job_waits_for_command():
    standby = task(
        "agent-g1",
        team="agent-maintainers",
        status="WAITING",
        hop_state="waiting_trigger",
    )
    standby["task_mode"] = "independent"
    standby["waiting_code"] = "trigger"
    standby["independent"] = {"active_event": None}
    registry = CDPARuntimeRegistry.hydrate([standby], now=1000.0)

    assert registry.next_due_at() is None

    active = dict(standby)
    active["status"] = "RUNNING"
    active["hops"] = [{"hop_id": 1, "state": "pre_send"}]
    active["independent"] = {"active_event": {"event_key": "recovery:a"}}
    registry.update_task(active, now=1001.0)
    assert registry.due_task_ids(1001.5) == ("agent-g1",)

    responded = dict(active)
    responded["hops"] = [{"hop_id": 1, "state": "responded"}]
    registry.update_task(responded, now=1002.0)
    assert registry.next_due_at() is None

    pending_completion = dict(responded)
    pending_completion["independent"] = {
        "active_event": {"event_key": "recovery:a"},
        "completion_request": {"outcome": "SUCCESS"},
    }
    registry.update_task(pending_completion, now=1003.0)
    assert registry.due_task_ids(1003.5) == ("agent-g1",)

    pending_continuation = dict(responded)
    pending_continuation["independent"] = {
        "active_event": {"event_key": "recovery:a"},
        "continuation_request": {"cycle": 2, "reason": "recheck"},
    }
    registry.update_task(pending_continuation, now=1004.0)
    assert registry.due_task_ids(1004.5) == ("agent-g1",)


def test_new_replacement_unschedules_existing_parent_immediately():
    old = task("old", status="STOPPED", updated_at="1970-01-01T00:00:00+00:00")
    registry = CDPARuntimeRegistry.hydrate(
        [old], now=1000.0, cleanup_idle_seconds=60.0
    )
    assert registry.due_task_ids(1000.0) == ()
    assert registry.due_task_ids(1000.5) == ("old",)

    replacement = task("replacement", status="RUNNING")
    replacement["replaces_task_id"] = "old"
    affected = registry.update_task(replacement, now=1001.0)

    assert "old" in affected
    assert registry.immutable_task_ids == {"old"}
    assert "old" not in registry.due_task_ids(1001.0)
