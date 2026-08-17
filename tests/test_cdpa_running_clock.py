from __future__ import annotations

from copy import deepcopy

from playwright_auto.cdpa_store import TaskStore


def _state(status: str = "RUNNING") -> dict:
    return {
        "status": status,
        "started_at": "2026-08-17T00:00:00+00:00",
        "updated_at": "2026-08-17T00:00:00+00:00",
        "active_role": "DEV",
        "active_hop_id": 2,
        "hops": [
            {
                "hop_id": 2,
                "target_role": "DEV",
                "timestamps": {"created_at": "2026-08-17T00:00:00+00:00"},
            }
        ],
        "controls": [],
    }


def test_running_clock_excludes_pause_from_task_and_role_elapsed() -> None:
    running = _state()

    paused = deepcopy(running)
    paused["status"] = "PAUSED"
    TaskStore._apply_running_clocks(
        running,
        paused,
        at="2026-08-17T00:06:00+00:00",
    )
    assert paused["running_elapsed_seconds"] == 360.0
    assert paused["running_since"] is None
    assert paused["active_role_running_elapsed_seconds"] == 360.0
    assert paused["active_role_running_since"] is None

    resumed = deepcopy(paused)
    resumed["status"] = "RUNNING"
    TaskStore._apply_running_clocks(
        paused,
        resumed,
        at="2026-08-17T00:36:00+00:00",
    )
    assert resumed["running_elapsed_seconds"] == 360.0
    assert resumed["running_since"] == "2026-08-17T00:36:00+00:00"
    assert resumed["active_role_running_elapsed_seconds"] == 360.0
    assert resumed["active_role_running_since"] == "2026-08-17T00:36:00+00:00"

    paused_again = deepcopy(resumed)
    paused_again["status"] = "PAUSED"
    TaskStore._apply_running_clocks(
        resumed,
        paused_again,
        at="2026-08-17T00:40:00+00:00",
    )
    assert paused_again["running_elapsed_seconds"] == 600.0
    assert paused_again["active_role_running_elapsed_seconds"] == 600.0


def test_running_clock_excludes_waiting_interval() -> None:
    running = _state()
    waiting = deepcopy(running)
    waiting["status"] = "WAITING"
    TaskStore._apply_running_clocks(
        running,
        waiting,
        at="2026-08-17T00:05:00+00:00",
    )
    assert waiting["running_elapsed_seconds"] == 300.0
    assert waiting["running_since"] is None

    resumed = deepcopy(waiting)
    resumed["status"] = "RUNNING"
    TaskStore._apply_running_clocks(
        waiting,
        resumed,
        at="2026-08-17T00:25:00+00:00",
    )
    stopped = deepcopy(resumed)
    stopped["status"] = "PAUSED"
    TaskStore._apply_running_clocks(
        resumed,
        stopped,
        at="2026-08-17T00:30:00+00:00",
    )
    assert stopped["running_elapsed_seconds"] == 600.0
