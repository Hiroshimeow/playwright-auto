"""V11 read-only lifecycle normalization must not mutate legacy manifests."""
from copy import deepcopy

import pytest
from playwright_auto.cdpa_projection import build_task_projection


@pytest.mark.parametrize("status,waiting,expected,reason,outcome", [
    ("INBOX", None, "QUEUED", None, None),
    ("RUNNING", None, "RUNNING", None, None),
    ("WAITING", {"reason": "dependency"}, "WAITING", "dependency", None),
    ("WAITING", {"reason": "async_callback"}, "WAITING", "async_callback", None),
    ("WAITING", {"reason": "browser_pool"}, "WAITING", "browser_pool", None),
    ("WAITING", {"reason": "role_reconnect"}, "PAUSED", "migration_unresolved", None),
    ("PAUSED", None, "PAUSED", "legacy_paused", None),
    ("BLOCKED", None, "PAUSED", "legacy_blocked", None),
    ("DONE", None, "TERMINAL", None, "done"),
    ("STOPPED", None, "TERMINAL", None, "stopped"),
])
def test_v11_lifecycle_is_derived_without_writing_existing_status(
    tmp_path, status, waiting, expected, reason, outcome
):
    from test_cdpa_projection import raw_task
    state = raw_task(tmp_path)
    state["status"] = status
    if waiting is not None:
        state["waiting"] = waiting
    before = deepcopy(state)
    projection = build_task_projection(state, tasks=[state])
    for public in (projection.summary, projection.detail):
        assert public["v11_lifecycle"] == {
            "state": expected,
            "reason_code": reason,
            "terminal_outcome": outcome,
        }
        assert public["status"] == status
    assert state == before
