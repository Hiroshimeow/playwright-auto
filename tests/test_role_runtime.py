"""Deterministic contract matrix for the single operational controller."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.role_runtime import Action, Decision, Policy, RoleController
from playwright_auto.role_runtime.allow import activity
from test_operational_contract import snapshot, receipt

NOW = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)


def step(current, wait, seconds=0, **kwargs):
    return RoleController().decide(current, wait, receipt=receipt(), now=NOW + timedelta(seconds=seconds), **kwargs)


def current_result(text="result", **kwargs):
    return snapshot(messages=(
        MessageSnapshot("user", "operator-user", "operator-turn", "continue", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", text, ()),
    ), **kwargs)


@pytest.mark.parametrize("source", ["hidden", "visible", "listen"])
def test_allow_requires_five_seconds_for_every_source(source):
    current = snapshot(mcp_permission_node_count=int(source == "hidden"),
                       mcp_permission_allow_count=int(source == "visible"))
    kwargs = {"permission": {"type": "allow", "target_message_id": "target", "remember_answer": True}} if source == "listen" else {}
    wait = {}
    assert step(current, wait, **kwargs).action is Action.WAIT
    assert step(current, wait, 4.9, **kwargs).action is Action.WAIT
    assert step(current, wait, 5, **kwargs).action is Action.ALLOW


def test_stable_valid_result_wins_over_permission_evidence():
    current = current_result(mcp_permission_node_count=1)
    wait = {}
    assert step(current, wait).reason == "mcp_allow_stable"
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.text == "result"


def test_allow_disappearance_alone_still_refreshes_after_five_seconds():
    current = snapshot()
    signature, length = activity(current)
    wait = {"mcp_allow_clicked_at": NOW.isoformat(), "mcp_allow_activity_signature": signature,
            "mcp_allow_activity_length": length}
    assert step(current, wait, 4.9).action is Action.WAIT
    result = step(current, wait, 5)
    assert result.action is Action.REFRESH
    assert result.reason == "mcp_allow_no_ui_progress"


def test_stop_after_allow_is_progress_and_does_not_refresh():
    current = snapshot(stop_visible=True)
    wait = {"mcp_allow_clicked_at": NOW.isoformat()}
    assert step(current, wait, 6).action is Action.WAIT
    assert "mcp_allow_clicked_at" not in wait


def test_failed_allow_handler_cannot_hide_stall_timer():
    current = snapshot(mcp_permission_node_count=1)
    wait = {"controller_state": "ALLOW", "controller_progress_at": NOW.isoformat(),
            "mcp_allow_seen_at": NOW.isoformat(), "mcp_allow_retry_at": NOW.isoformat()}
    assert step(current, wait, 601).action is Action.REFRESH


def test_failed_allow_with_no_dom_permission_clears_stale_network_evidence():
    current = snapshot(mcp_permission_node_count=0, mcp_permission_allow_count=0)
    wait = {"mcp_allow_seen_at": NOW.isoformat(), "mcp_allow_seen_target": "target"}

    class Client:
        cleared = False

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            return None

        def clear_permission_action(self):
            self.cleared = True

    client = Client()
    result = asyncio.run(RoleController().browser_action(
        client,
        current,
        wait,
        {"type": "allow", "target_message_id": "target", "remember_answer": True},
        Decision(Action.ALLOW, "mcp_allow_ready"),
    ))
    assert result.action is Action.WAIT
    assert result.reason == "mcp_allow_stale_cleared"
    assert client.cleared is True
    assert "mcp_allow_seen_at" not in wait
    assert "mcp_allow_retry_at" not in wait


def test_failed_allow_with_real_dom_permission_refreshes_after_five_seconds():
    current = snapshot(mcp_permission_node_count=1)
    wait = {"mcp_allow_seen_at": NOW.isoformat(), "mcp_allow_seen_target": "target"}
    permission = {"type": "allow", "target_message_id": "target", "remember_answer": True}

    class Client:
        async def auto_allow_mcp_permission(self, *, passive_action=None):
            return None

    result = asyncio.run(RoleController().browser_action(
        Client(), current, wait, permission, Decision(Action.ALLOW, "mcp_allow_ready")
    ))
    assert result.action is Action.WAIT
    assert result.reason == "mcp_allow_handler_pending"
    clicked = datetime.fromisoformat(wait["mcp_allow_clicked_at"])
    assert "mcp_allow_retry_at" not in wait

    decision = RoleController().decide(
        current,
        wait,
        receipt=receipt(),
        permission=permission,
        now=clicked + timedelta(seconds=5),
    )
    assert decision.action is Action.REFRESH
    assert decision.reason == "mcp_allow_no_ui_progress"


def test_operator_continuation_accepts_current_result_without_original_user():
    current = current_result()
    wait = {}
    assert step(current, wait).action is Action.WAIT
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.text == "result"


def test_valid_result_can_finish_despite_stuck_stop():
    current = current_result(stop_visible=True)
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is Action.ACCEPT


def invalid(_candidate):
    raise ValueError("route JSON missing")


def test_malformed_result_refreshes_once_then_repairs():
    current = current_result("not JSON")
    wait = {}
    step(current, wait, validate=invalid)
    assert step(current, wait, 5, validate=invalid).action is Action.REFRESH
    wait["invalid_refreshed_key"] = wait["result_seen_key"]
    assert step(current, wait, 10, validate=invalid).action is Action.REPAIR


def test_incomplete_stream_is_not_repaired_until_stop_clears():
    current = current_result("{", stop_visible=True)
    wait = {}
    step(current, wait, validate=invalid)
    assert step(current, wait, 5, validate=invalid).action is Action.WAIT


def test_retry_queues_continuation_but_does_not_block_presend():
    current = snapshot(retry_visible=True)
    assert step(current, {}).action is Action.REPAIR
    assert step(current, {}, phase="pre_send").reason == "ready"


def test_draft_prevents_repair_not_result_observation():
    assert step(snapshot(retry_visible=True, composer_text="my draft"), {}).reason == "manual_draft_preserved"
    current = current_result(composer_text="my draft")
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is Action.ACCEPT


def test_session_expiry_is_not_retry_recovery():
    result = step(snapshot(retry_visible=True, requires_login=True), {})
    assert result.action is Action.BLOCK
    assert result.reason == "authentication_required"


def test_actual_dialog_blocks_but_permission_dialog_uses_allow_handler():
    assert step(snapshot(blocking_dialogs=("Unexpected dialog",)), {}).action is Action.BLOCK
    current = snapshot(blocking_dialogs=("MCP permission",), mcp_permission_node_count=1)
    assert step(current, {}).reason == "mcp_allow_stable"


@pytest.mark.parametrize("stop", [False, True])
def test_stall_refreshes_after_ten_minutes(stop):
    current = snapshot(stop_visible=stop)
    wait = {}
    step(current, wait)
    assert step(current, wait, 599).action is Action.WAIT
    assert step(current, wait, 600).action is Action.REFRESH


def test_stall_does_not_destroy_manual_draft():
    current = snapshot(composer_text="keep this")
    wait = {}
    step(current, wait)
    assert step(current, wait, 601).reason == "manual_draft_preserved"


def test_status_is_only_one_shot_at_ambiguous_deadline():
    wait = {"deadline_at": (NOW + timedelta(seconds=10)).isoformat()}
    assert step(snapshot(), wait).action is Action.WAIT
    assert step(snapshot(), wait, 10).action is Action.REFRESH
    wait["timeout_refreshed"] = True
    assert step(snapshot(), wait, 16).action is Action.STATUS
    wait["timeout_status_checked"] = True
    wait["timeout_status"] = "COMPLETE"
    assert step(snapshot(), wait, 20).action is Action.REPAIR


def test_dom_only_does_not_require_status_or_graph():
    wait = {"deadline_at": NOW.isoformat(), "timeout_refreshed": True}
    assert step(snapshot(), wait, 5, dom_only=True).action is Action.REPAIR


def test_latest_injected_user_invalidates_old_candidate_without_identity_error():
    current = current_result()
    wait = {}
    step(current, wait)
    current = snapshot(messages=(*current.messages, MessageSnapshot("user", "another", "another", "change", ())))
    assert step(current, wait, 5).action is Action.WAIT


def test_history_after_allow_never_refreshes_old_conversation():
    current = snapshot()
    wait = {"mcp_allow_clicked_at": (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()}

    class Client:
        async def read_wait_probe(self):
            return current

        async def refresh(self):
            raise AssertionError("history automation must never refresh")

    result = asyncio.run(RoleController().maintain_history(Client(), wait, dom_only=True))
    assert result.action is Action.WAIT
    assert result.reason == "history_observation"


def test_reload_settle_window_is_shared_by_all_recovery_actions():
    wait = {"refresh_ready_at": (NOW + timedelta(seconds=5)).isoformat()}
    assert step(snapshot(retry_visible=True), wait, 4).reason == "reload_settle"
    assert step(snapshot(retry_visible=True), wait, 5).action is Action.REPAIR
