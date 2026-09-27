from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, BranchBootstrapError, BranchTargetUnresolvedError, CDPATabActions
from playwright_auto.cdpa_bootstraps import BootstrapCatalog, normalize_bootstrap_record
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


@pytest.mark.skip(reason="workflow bootstrap branching is temporarily disabled; workflow tasks use Fresh context")
def test_inaccessible_cross_account_donor_continues_same_pristine_hop_with_workspace_context(tmp_path):
    _config, store, state, worker = setup_task(tmp_path, task_id="bootstrap-cross-account")
    endpoint = store.endpoints.add("9202")
    selected = BootstrapCatalog(tmp_path).upsert(
        {**normalize_bootstrap_record(bootstrap_record()), "prewarm_prompt": "FOREIGN_PREWARM_MUST_NOT_BE_REUSED"}
    )
    state["bootstrap"] = selected
    state["context_policy"] = "bootstrap"
    role = state["roles"]["PLAN"]
    role["browser_endpoint_id"] = endpoint["endpoint_id"]
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
    assert "FOREIGN_PREWARM_MUST_NOT_BE_REUSED" not in hop["prompt"]
    assert role["context_source"] == "fresh_fallback"
    recovery = role["bootstrap_context_recovery"]
    assert recovery["endpoint_id"] == endpoint["endpoint_id"]
    assert recovery["bootstrap_id"] == selected["bootstrap_id"]
    assert recovery["reason"] == "donor_unavailable_fresh_context"
    assert recovery["unavailable_donors"][0]["error"] == "new_branch HTTP 404"
    assert "prewarm_context" not in recovery
    actions.fresh_chat.assert_awaited_once_with(
        state, "PLAN", url="https://chatgpt.com/", clean_for_send=True
    )


def test_new_generation_format_repair_omits_substantive_goal_and_handoff(tmp_path):
    from test_cdpa_worker import FakeActions
    _config, _store, state, worker = setup_task(tmp_path, task_id="repair-lost-context")
    hop = _active_hop(state)
    hop.update(kind="route_repair", validation_error="bad route", handoff="Exact previous handoff")
    role = state["roles"]["PLAN"]
    previous = _store.endpoints.resolve()
    next_endpoint = _store.endpoints.add("9202")
    role["last_endpoint_id"] = previous["endpoint_id"]
    role["browser_endpoint_id"] = next_endpoint["endpoint_id"]
    role["context_source"] = "execution_recovery"
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert "bad route" in hop["prompt"]
    assert state["task_text"] not in hop["prompt"]
    assert "Exact previous handoff" not in hop["prompt"]
    assert "RECOVERY_SNAPSHOT" not in hop["prompt"]
