from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.chatgpt import RateLimitBlockedError
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_worker import FakeActions, setup_task


def _scenario(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-presend-resume-open-tab")
    parent = _active_hop(state)
    parent["state"] = "routed"
    parent["receipt"] = {"user_message_id": "previously-accepted-user"}
    hop = worker._append_hop(
        state, source_role="PLAN", target_role="PLAN",
        handoff="repair route format", kind="route_repair",
    )
    role = state["roles"]["PLAN"]
    role.update(page_id="owned-page", page_url="https://chatgpt.com/c/owned", online=False)
    original_request = hop["request_id"]

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.acquire_calls = 0
            self.reopen_calls = []
            self.reopened_page = "owned-page"
            self.reopened_url = "https://chatgpt.com/c/owned"

        async def acquire(self, *_args, **_kwargs):
            self.acquire_calls += 1
            raise TimeoutError("clean-ready acquisition timed out")

        async def locate_owned(self, *_args, **_kwargs):
            if not self.acquire_calls:
                class OwnedClient:
                    async def assert_ownership(self):
                        return SimpleNamespace(
                            state="new_chat",
                            composer_present=True,
                            composer_editable=True,
                            composer_text="",
                            attachment_markers=(),
                            stop_visible=False,
                            manual_input_pending=False,
                            blocking_dialogs=(),
                        )

                return AcquiredRole(
                    client=OwnedClient(), page_id="owned-page",
                    url="https://chatgpt.com/c/owned", created=False, new_chat=False,
                )
            return None

        async def reopen(self, *_args, **kwargs):
            self.reopen_calls.append(kwargs)
            return AcquiredRole(
                client=SimpleNamespace(), page_id=self.reopened_page,
                url=self.reopened_url, created=True, new_chat=False,
            )

    actions = Actions()
    asyncio.run(worker._pre_send(state, hop, actions))
    assert state["block_code"] == "preboundary_acquisition_recovery_required"
    assert hop["preboundary_recovery"]["request_id"] == original_request
    store.save(state["manifest_path"], state)
    state = store.load(state["manifest_path"])
    worker = type(worker)(worker.config, store=store)
    return state, worker, actions, original_request


def _resume_no_progress(state, worker, actions):
    control = {"action": "resume", "status": "recovering", "role": "PLAN", "control_id": 1}
    state["status"] = "RUNNING"  # Resume's locked admission clears the generic block.
    state["block_code"] = None

    async def admit(*_args, **_kwargs):
        return None

    worker._acquire_endpoint_admission = admit
    asyncio.run(worker._recover_resume_control(state, control, actions))
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "resume_progress_unverified"
    assert _active_hop(state)["preboundary_recovery"]["phase"] == "recovery_required"
    assert state["hops"][0]["receipt"]["user_message_id"] == "previously-accepted-user"


def _open(state, worker, actions):
    state["controls"] = [{
        "control_id": 2, "action": "open_tab", "role": "PLAN",
        "reason": "reconcile exact pre-send acquisition", "status": "requested",
    }]
    return asyncio.run(worker._apply_control(state, actions))


def test_open_tab_reconciles_acquisition_marker_after_resume_replaces_block_code(tmp_path):
    state, worker, actions, request_id = _scenario(tmp_path)
    _resume_no_progress(state, worker, actions)
    assert actions.acquire_calls == 1

    assert _open(state, worker, actions) is True
    assert actions.reopen_calls == [{"require_clean_ready": False, "foreground": True}]
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"]["preboundary_acquisition_reconciled"] is True
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert _active_hop(state)["request_id"] == request_id
    assert _active_hop(state)["state"] == "pre_send"
    assert "preboundary_recovery" not in _active_hop(state)
    assert state["hops"][0]["receipt"]["user_message_id"] == "previously-accepted-user"
    assert actions.acquire_calls == 1  # Neither Resume nor Open Tab replays a Send.


def test_open_tab_defers_cloudflare_challenge_without_clearing_recovery(tmp_path, monkeypatch):
    state, worker, actions, request_id = _scenario(tmp_path)
    _resume_no_progress(state, worker, actions)

    async def challenge(*_args, **_kwargs):
        raise RateLimitBlockedError("Cloudflare challenge blocked recorded reopen")

    actions.reopen = challenge
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)

    assert _open(state, worker, actions) is True
    control = state["controls"][0]
    assert control["status"] == "applied"
    assert control["result"] == {"deferred": True, "reason": "rate_limit_cooldown"}
    assert state["active_action"] == "rate_limit_cooldown"
    assert _active_hop(state)["request_id"] == request_id
    assert _active_hop(state)["preboundary_recovery"]["phase"] == "recovery_required"


@pytest.mark.parametrize("change", ["request", "stage", "receipt", "page", "conversation"])
def test_open_tab_does_not_clear_marker_without_exact_provenance(tmp_path, change):
    state, worker, actions, _ = _scenario(tmp_path)
    _resume_no_progress(state, worker, actions)
    hop = _active_hop(state)
    if change == "request":
        hop["preboundary_recovery"]["request_id"] = "other-request"
    elif change == "stage":
        hop["preboundary_recovery"]["stage"] = "other-stage"
    elif change == "receipt":
        hop["receipt"] = {"user_message_id": "accepted-active-hop"}
    elif change == "page":
        actions.reopened_page = "foreign-page"
    else:
        actions.reopened_url = "https://chatgpt.com/c/foreign"

    _open(state, worker, actions)
    assert state["controls"][0]["status"] != "applied"
    assert state["status"] == "BLOCKED"
    assert hop["preboundary_recovery"]["phase"] == "recovery_required"
    assert actions.acquire_calls == 1
