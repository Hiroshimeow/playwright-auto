from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.chatgpt import ComposerConflictError, PageOwnershipError
from test_cdpa_worker import FakeActions, setup_task


@pytest.mark.parametrize("error_type", [TimeoutError, PlaywrightTimeoutError])
def test_ordinary_clean_ready_timeout_fails_closed_without_send_or_reacquisition(tmp_path, error_type):
    _config, store, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    calls = []

    class Actions(FakeActions):
        async def acquire(self, state, role, **kwargs):
            calls.append(kwargs)
            raise error_type("clean-ready check failed after transient timeout")

    original_request = hop.get("request_id")
    actions = Actions()
    asyncio.run(worker._pre_send(state, hop, actions))
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "preboundary_acquisition_recovery_required"
    assert hop["state"] == "pre_send"
    assert hop["preboundary_recovery"]["phase"] == "recovery_required"
    assert hop.get("receipt") is None
    assert hop.get("request_id") == original_request
    assert not Path(hop["ledger_path"]).exists()
    store.save(Path(state["manifest_path"]), state)
    state = store.load(state["manifest_path"])
    restarted = type(worker)(worker.config, store=store)
    asyncio.run(restarted._pre_send(state, _active_hop(state), actions))
    assert calls == [{"clean_for_send": True}]


@pytest.mark.parametrize("error_type", [ComposerConflictError, PageOwnershipError])
def test_ordinary_clean_ready_semantic_errors_are_not_transient_recovery(tmp_path, error_type):
    _config, _store, state, worker = setup_task(tmp_path)
    error = error_type("manual or foreign ownership")

    class Actions(FakeActions):
        async def acquire(self, state, role, **kwargs):
            raise error

    with pytest.raises(error_type) as caught:
        asyncio.run(worker._pre_send(state, _active_hop(state), Actions()))
    assert caught.value is error
    assert "preboundary_recovery" not in _active_hop(state)


def test_transient_navigation_clean_ready_retries_without_blocking(tmp_path):
    _config, _store, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    calls = []

    class Actions(FakeActions):
        async def acquire(self, state, role, **kwargs):
            calls.append(kwargs)
            raise RuntimeError(
                "clean-ready check failed after transient errors: "
                "Error: Page.evaluate: Execution context was destroyed, most likely because of a navigation"
            )

    actions = Actions()
    asyncio.run(worker._pre_send(state, hop, actions))
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "queued"
    assert state.get("block_code") is None
    assert hop["state"] == "pre_send"
    assert hop.get("receipt") is None
    assert hop["wait"].get("pre_send_retry_at")
    assert calls == [{"clean_for_send": True}]

    asyncio.run(worker._pre_send(state, hop, actions))
    assert calls == [{"clean_for_send": True}]


def test_send_boundary_resets_stale_presend_recovery_clock(tmp_path):
    _config, _store, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    sent_at = datetime(2026, 9, 24, 12, 4, 17, tzinfo=timezone.utc)
    hop.setdefault("timestamps", {})["sent_at"] = sent_at.isoformat()
    hop.setdefault("wait", {}).update(
        controller_progress_at="2026-09-23T17:11:09+00:00",
        controller_state="STOP",
        timeout_refreshed=True,
        timeout_status_checked=True,
        timeout_status="COMPLETE",
        refresh_ready_at="2026-09-23T17:12:00+00:00",
    )

    worker._start_wait_budget_from_sent(hop)

    assert hop["wait"]["controller_progress_at"] == sent_at.isoformat()
    assert "controller_state" not in hop["wait"]
    assert "timeout_refreshed" not in hop["wait"]
    assert "timeout_status_checked" not in hop["wait"]
    assert "timeout_status" not in hop["wait"]
    assert "refresh_ready_at" not in hop["wait"]
