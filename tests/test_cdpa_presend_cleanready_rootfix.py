from __future__ import annotations

import asyncio
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
