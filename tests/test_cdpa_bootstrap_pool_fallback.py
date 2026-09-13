from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, BranchBootstrapError, BranchTargetUnresolvedError, CDPATabActions
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import setup_task, ready_client, bootstrap_record


@pytest.mark.parametrize("status", [403, 404, 410])
def test_definitively_inaccessible_donor_is_not_canonical_branch_ambiguity(status):
    async def run():
        response = SimpleNamespace(status=status)
        future = asyncio.get_running_loop().create_future()
        future.set_result(response)
        with pytest.raises(BranchBootstrapError) as caught:
            await CDPATabActions._new_branch_conversation_id(future, timeout_ms=500)
        assert not isinstance(caught.value, BranchTargetUnresolvedError)
        assert getattr(caught.value, "donor_unavailable", False)
    asyncio.run(run())


def test_inaccessible_cross_account_donor_continues_same_pristine_hop_with_workspace_context(tmp_path):
    _config, store, state, worker = setup_task(tmp_path, task_id="bootstrap-cross-account")
    state["bootstrap"] = bootstrap_record()
    state["context_policy"] = "bootstrap"
    role = state["roles"]["PLAN"]
    role["browser_endpoint_id"] = store.endpoints.add("9202")["endpoint_id"]
    original = _active_hop(state)["request_id"]
    error = BranchBootstrapError("new_branch HTTP 404")
    error.donor_unavailable = True
    client = ready_client(state, "PLAN")
    acquired = AcquiredRole(client, "new-account-page", "https://chatgpt.com/", True, True)
    actions = SimpleNamespace(locate_owned=AsyncMock(return_value=None),
                              branch_from_anchor=AsyncMock(side_effect=error),
                              fresh_chat=AsyncMock(return_value=acquired))
    asyncio.run(worker._pre_send(state, _active_hop(state), actions))
    hop = _active_hop(state)
    assert state["status"] != "BLOCKED"
    assert hop["state"] == "sending"
    assert hop["request_id"] == original
    assert "RECOVERY_SNAPSHOT" in hop["prompt"]
    assert str(tmp_path) in hop["prompt"]
    assert "Inspect" in hop["prompt"] or "inspect" in hop["prompt"]
    assert role["context_source"] == "fresh_fallback"
    assert role["bootstrap_context_recovery"]["endpoint_id"] == role["browser_endpoint_id"]
    assert actions.fresh_chat.await_count == 1


def test_new_generation_format_repair_contains_task_and_handoff_not_only_compact_nudge(tmp_path):
    from test_cdpa_worker import FakeActions
    _config, _store, state, worker = setup_task(tmp_path, task_id="repair-lost-context")
    hop = _active_hop(state)
    hop.update(kind="route_repair", validation_error="bad route", handoff="Exact previous handoff")
    role = state["roles"]["PLAN"]
    role["last_endpoint_id"] = "previous"
    role["browser_endpoint_id"] = "next"
    role["context_source"] = "execution_recovery"
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert state["task_text"] in hop["prompt"]
    assert "Exact previous handoff" in hop["prompt"]
    assert "RECOVERY_SNAPSHOT" in hop["prompt"]
