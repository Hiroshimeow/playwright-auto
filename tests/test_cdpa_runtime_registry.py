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
    created_at: str | None = None,
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
        "created_at": created_at or updated_at,
    }


def test_due_tasks_follow_durable_fifo_instead_of_task_id_order():
    registry = CDPARuntimeRegistry.hydrate(
        [
            task("aaa-new", created_at="2026-07-25T00:00:02+00:00"),
            task("zzz-old", created_at="2026-07-25T00:00:01+00:00"),
        ],
        now=1000.0,
    )

    assert registry.due_task_ids(1000.5) == ("zzz-old", "aaa-new")


def test_due_tasks_do_not_starve_old_normal_work_behind_new_urgent_repairs():
    normal = task("normal-old", created_at="2026-07-25T00:00:01+00:00")
    repair_one = task("repair-one", created_at="2026-07-25T00:00:02+00:00")
    repair_two = task("repair-two", created_at="2026-07-25T00:00:03+00:00")
    repair_one["priority"] = "urgent_repair"
    repair_two["priority"] = "urgent_repair"

    registry = CDPARuntimeRegistry.hydrate([repair_two, normal, repair_one], now=1000.0)

    assert registry.due_task_ids(1000.5) == ("normal-old", "repair-one", "repair-two")


def test_restart_hydration_preserves_browser_pool_fifo():
    old = task("zzz-old", status="WAITING", created_at="2026-07-25T00:00:01+00:00")
    new = task("aaa-new", status="WAITING", created_at="2026-07-25T00:00:02+00:00")
    for item in (old, new):
        item["waiting"] = {
            "reason": "browser_pool",
            "since": item["created_at"],
            "resume_status": "INBOX",
            "resume_column": "INBOX",
            "resume_action": "queued",
        }
    registry = CDPARuntimeRegistry.hydrate([new, old], now=1000.0)

    assert registry.due_task_ids(1000.5) == ("zzz-old", "aaa-new")


def test_hydrate_builds_identity_dependency_and_team_indexes():
    registry = CDPARuntimeRegistry.hydrate(
        [task("parent", team="alpha"), task("child", team="beta", dependencies=("parent",))]
    )

    assert set(registry.tasks_by_id) == {"parent", "child"}
    assert registry.paths_by_id["parent"] == Path("/repo/.plan/alpha/parent.json").resolve()
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


def test_cleanup_retry_gate_schedules_persisted_boundary_and_suppression():
    cooling = task("cooling", status="DONE")
    cooling["cleanup"] = {
        "state": "CLEARING",
        "next_retry_at": "1970-01-01T00:16:50+00:00",
        "retry_suppressed": False,
    }
    suppressed = task("suppressed", status="DONE")
    suppressed["cleanup"] = {
        "state": "CLEARING",
        "next_retry_at": None,
        "retry_suppressed": True,
    }

    registry = CDPARuntimeRegistry.hydrate([cooling, suppressed], now=1000.0)

    assert registry.due_task_ids(1000.5) == ()
    assert registry.next_due_at() == 1010.0
    assert registry.due_task_ids(1010.0) == ("cooling",)


def test_cleanup_retry_gate_survives_registry_hydration():
    clearing = task("clearing", status="DONE")
    clearing["cleanup"] = {
        "state": "CLEARING",
        "next_retry_at": "1970-01-01T00:17:00+00:00",
        "failure_fingerprint": "same-failure",
        "same_failure_count": 2,
        "retry_suppressed": False,
    }

    first = CDPARuntimeRegistry.hydrate([clearing], now=1000.0)
    restarted = CDPARuntimeRegistry.hydrate(
        [first.tasks_by_id["clearing"]],
        now=1005.0,
    )

    assert restarted.next_due_at() == 1020.0
    assert restarted.due_task_ids(1019.9) == ()
    assert restarted.due_task_ids(1020.0) == ("clearing",)


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
