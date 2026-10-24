"""V11 architecture checks: exact page semantics, no implicit browser mutation."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.role_runtime import Action, RoleController
from test_cdpa_worker import setup_task, _active_hop
from test_operational_contract import snapshot


NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def test_reopen_pre_send_background_waits_for_clean_composer_without_focus(tmp_path):
    _, _, state, worker = setup_task(tmp_path, task_id="v11-recovery-ready")
    hop = _active_hop(state)
    url = "https://chatgpt.com/c/exact-reopen"
    hop.update(state="pre_send", conversation_url=url, receipt=None)
    state["roles"]["PLAN"].update(page_id="existing-page", page_url=url, online=False)
    client = SimpleNamespace(wait_until_clean_ready=AsyncMock())
    acquired = AcquiredRole(client, "existing-page", url, True, False)

    class Actions:
        def __init__(self):
            self.calls = []
        async def reopen(self, *_args, **kwargs):
            self.calls.append(kwargs)
            return acquired
    actions = Actions()
    result = asyncio.run(worker._recover_active_role(state, "PLAN", actions, foreground=False))
    assert result is acquired
    assert actions.calls == [{"require_clean_ready": False, "foreground": False}]
    client.wait_until_clean_ready.assert_awaited_once()


def test_history_observation_must_never_click_even_if_permission_is_visible():
    current = snapshot(mcp_permission_node_count=1, mcp_permission_allow_count=1)
    decision = RoleController().decide(current, {}, phase="history", now=NOW)
    assert (decision.action, decision.reason) == (Action.WAIT, "history_observation")


def test_stale_error_banner_does_not_reload_or_block_safe_presend():
    current = snapshot(
        url="https://chatgpt.com/c/reused?temporary-chat=true",
        composer_present=True,
        composer_editable=True,
        composer_text="",
        error_texts=("Message delivery timed out. Please try again. Retry",),
        retry_visible=True,
    )
    wait = {}
    decision = RoleController().decide(current, wait, phase="pre_send", now=NOW)
    assert (decision.action, decision.reason) == (Action.WAIT, "ready")
    assert not any("refreshed" in name for name in wait)


def test_stale_banner_never_clears_manual_presend_input():
    current = snapshot(
        composer_present=True, composer_editable=True, composer_text="operator draft",
        attachment_markers=("manual.zip",), error_texts=("Message delivery timed out. Retry",),
    )
    decision = RoleController().decide(current, {}, phase="pre_send", now=NOW)
    assert decision.action is not Action.REFRESH
    assert decision.reason != "ui_error_waiting_for_composer"


def test_reopened_background_tab_activates_page_lifecycle_without_focus(tmp_path, monkeypatch):
    import playwright_auto.cdpa_actions as module
    from playwright_auto.cdpa_actions import CDPATabActions
    from playwright_auto.cdpa_config import load_cdpa_config
    from test_cdpa_actions import FakeClient, FakeContext, manifest
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    monkeypatch.setattr(module, "ChatGPTPage", FakeClient)

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "random_delay", no_delay)
    context = FakeContext()
    actions = CDPATabActions(context, config)
    task = manifest(
        page_id="owned-id", page_url="https://chatgpt.com/c/exact",
        conversation_url="https://chatgpt.com/c/exact",
    )
    role = asyncio.run(actions.reopen(task, "PLAN", require_clean_ready=False, foreground=False))
    assert role.client.page.front is False
    assert [(kind, args) for _, kind, args, *_ in context.lifecycle_calls] == [
        ("Page.setWebLifecycleState", {"state": "active"})
    ]


def test_stuck_stop_does_not_hide_completed_bound_terminal_report():
    from playwright_auto.chatgpt import MessageSnapshot
    from test_operational_contract import receipt
    user = MessageSnapshot("user", "original-user", "original-turn", "request", ())
    assistant = MessageSnapshot(
        "assistant", "answer-1", "answer-turn", '{"route":"DONE"}',
        ("copy-turn-action-button",),
    )
    current = snapshot(stop_visible=True, messages=(user, assistant))
    wait = {}
    controller = RoleController()
    first = controller.decide(current, wait, receipt=receipt(),
                              reconcile=lambda _candidate: {"route": "DONE", "report_size": 10},
                              now=NOW)
    assert first.action is Action.WAIT
    completed = controller.decide(current, wait, receipt=receipt(),
                                  reconcile=lambda _candidate: {"route": "DONE", "report_size": 10},
                                  now=NOW.replace(minute=NOW.minute + 1, second=1))
    assert (completed.action, completed.reason) == (Action.ACCEPT, "terminal_route_reconciled")


def test_stuck_stop_without_completion_control_must_not_route():
    from playwright_auto.chatgpt import MessageSnapshot
    from test_operational_contract import receipt
    current = snapshot(stop_visible=True, messages=(
        MessageSnapshot("user", "original-user", "original-turn", "request", ()),
        MessageSnapshot("assistant", "answer-1", "answer-turn", '{"route":"DONE"}', ()),
    ))
    wait = {}
    controller = RoleController()
    controller.decide(current, wait, receipt=receipt(), reconcile=lambda _: {"route": "DONE"}, now=NOW)
    after = controller.decide(current, wait, receipt=receipt(),
                              reconcile=lambda _: {"route": "DONE"},
                              now=NOW.replace(minute=NOW.minute + 1, second=1))
    assert after.action is Action.WAIT


def test_stop_signal_without_any_dom_progress_cannot_rearm_forever():
    from datetime import timedelta
    controller = RoleController()
    wait = {}
    active = snapshot(stop_visible=True)
    assert controller.decide(active, wait, phase="waiting", now=NOW).action is Action.WAIT
    assert controller.decide(active, wait, phase="waiting", now=NOW + timedelta(seconds=600)).action is Action.WAIT
    expired = controller.decide(active, wait, phase="waiting", now=NOW + timedelta(seconds=1801))
    assert (expired.action, expired.reason) == (Action.BLOCK, "response_stuck_active")
