from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions, RoleOwnershipError
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_worker import _active_hop

from test_cdpa_actions import FakeClient, FakeContext, manifest
from test_cdpa_core import write_config
from test_cdpa_worker import setup_task


def test_recorded_offline_ownership_error_has_canonical_code(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(actions_module, "ChatGPTPage", FakeClient)
    actions = CDPATabActions(FakeContext(), config)
    state = manifest(
        page_id="closed-page",
        page_url="https://chatgpt.com/",
        conversation_url="https://chatgpt.com/c/exact-conversation",
    )

    with pytest.raises(RoleOwnershipError) as captured:
        asyncio.run(actions.acquire(state, "PLAN"))

    assert captured.value.code == "role_offline"


def test_worker_persists_canonical_role_offline_block(tmp_path: Path, monkeypatch):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-canonical-role-offline"
    )
    path = Path(state["manifest_path"])

    class OfflineActions:
        def __init__(self, *_args, **_kwargs):
            pass

        async def acquire(self, _state, _role):
            raise RoleOwnershipError(
                "recorded 'alpha-plan' tab is offline; use Open tab for controlled recovery",
                code="role_offline",
            )

    monkeypatch.setattr(worker_module, "CDPATabActions", OfflineActions)

    blocked = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "role_offline"
    assert blocked["roles"]["PLAN"]["online"] is False
    assert "tab is offline" in blocked["roles"]["PLAN"]["last_error"]
    assert _active_hop(blocked)["state"] == "pre_send"


def test_worker_canonicalizes_legacy_recorded_offline_error(tmp_path: Path, monkeypatch):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-legacy-role-offline"
    )
    path = Path(state["manifest_path"])

    class OfflineActions:
        def __init__(self, *_args, **_kwargs):
            pass

        async def acquire(self, _state, _role):
            raise RoleOwnershipError(
                "recorded 'alpha-plan' tab is offline; use Open tab for controlled recovery"
            )

    monkeypatch.setattr(worker_module, "CDPATabActions", OfflineActions)

    blocked = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "role_offline"


@pytest.mark.parametrize(
    ("role", "physical_role"),
    [("DEV", "unstopable3-dev"), ("REVIEW", "unstopable3-review")],
)
def test_observed_legacy_offline_errors_are_true_list_recoveries(
    tmp_path: Path,
    role: str,
    physical_role: str,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-observed-{role.lower()}-offline"
    )
    hop = _active_hop(state)
    hop["target_role"] = role
    hop["physical_role"] = physical_role
    state["active_role"] = role
    state["roles"][role]["physical_role"] = physical_role
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="unexpected_error",
        block_retryable=False,
        block_reason=(
            f"RoleOwnershipError: recorded {physical_role!r} tab is offline; "
            "use Open tab for controlled recovery"
        ),
    )
    state["roles"][role].update(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        online=True,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": role,
            "reason": "recover the exact owned role tab",
            "status": "requested",
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url="https://chatgpt.com/c/exact-conversation",
        created=True,
        new_chat=False,
    )

    class RecoveryActions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, **_kwargs):
            return acquired

    assert asyncio.run(worker._apply_control(state, RecoveryActions())) is True
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["controls"][0]["result"]["resumed"] is True
    assert hop["state"] == "pre_send"


def test_open_tab_does_not_resume_unlisted_unexpected_error(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-unlisted-open-tab-block"
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="unexpected_error",
        block_retryable=False,
        block_reason="RuntimeError: unrelated operational failure",
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "bring the role tab forward without clearing the block",
            "status": "requested",
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="existing-page",
        url="https://chatgpt.com/c/existing-conversation",
        created=False,
        new_chat=False,
    )

    class ExistingActions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def open_tab(self, *_args, **_kwargs):
            return None

    assert asyncio.run(worker._apply_control(state, ExistingActions())) is True
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "unexpected_error"
    assert state["block_reason"] == "RuntimeError: unrelated operational failure"
    assert "resumed" not in state["controls"][0]["result"]


def test_open_tab_recovery_resumes_safe_block_without_code_coupling(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-decoupled-block-code"
    )
    hop = _active_hop(state)
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="unexpected_error",
        block_retryable=False,
        block_reason=(
            "RoleOwnershipError: recorded 'alpha-plan' tab is offline; "
            "use Open tab for controlled recovery"
        ),
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/exact-conversation",
        online=True,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover the exact owned role tab",
            "status": "requested",
        }
    ]
    acquired = AcquiredRole(
        client=SimpleNamespace(),
        page_id="closed-page",
        url="https://chatgpt.com/c/exact-conversation",
        created=True,
        new_chat=False,
    )

    class RecoveryActions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def reopen(self, *_args, **_kwargs):
            return acquired

    assert asyncio.run(worker._apply_control(state, RecoveryActions())) is True
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["block_reason"] is None
    assert hop["state"] == "pre_send"
    assert hop["hop_id"] == original_hop_id
    assert hop["request_id"] == original_request_id
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"]["resumed"] is True
