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
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot(
            "assistant", "answer", "answer-turn", text, ("copy-turn-action-button",)
        ),
    ), **kwargs)


@pytest.mark.parametrize("source", ["hidden", "visible", "listen"])
def test_allow_is_handled_immediately_for_every_source(source):
    current = snapshot(mcp_permission_node_count=int(source == "hidden"),
                       mcp_permission_allow_count=int(source == "visible"))
    kwargs = {"permission": {"type": "allow", "target_message_id": "target", "remember_answer": True}} if source == "listen" else {}
    assert step(current, {}, **kwargs).action is Action.ALLOW


def test_temporary_save_offer_does_not_block_consuming_completed_response():
    current = current_result(blocking_dialogs=("Want to keep this chat? You can now save it to your history.",),
                             url="https://chatgpt.com/?temporary-chat=true")
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 5).action is Action.ACCEPT
    # This exception authorizes reading a result, never sending through a dialog or saving a chat.
    assert step(current, {}, phase="pre_send").reason == "blocking_dialog_wait"
    other = current_result(blocking_dialogs=("Want to keep this chat? You can now save it to your history.", "Delete chat?"),
                           url="https://chatgpt.com/?temporary-chat=true")
    other_wait = {}
    assert step(other, other_wait).reason == "result_stability"
    assert step(other, other_wait, 5).action is Action.ACCEPT


def test_stop_only_receipt_without_user_id_binds_subsequent_assistant_response():
    from dataclasses import replace

    stop_receipt = replace(
        receipt(), accepted_via="stop_button", user_message_id=None, user_turn_id=None
    )
    current = current_result()
    wait = {}
    controller = RoleController()
    assert controller.decide(current, wait, receipt=stop_receipt, now=NOW).action is Action.WAIT
    completed = controller.decide(
        current, wait, receipt=stop_receipt, now=NOW + timedelta(seconds=5)
    )
    assert completed.action is Action.ACCEPT
    assert completed.response.message_id == "answer"


def test_valid_result_wins_over_permission_evidence_after_five_second_stability():
    current = current_result(mcp_permission_node_count=1)
    wait = {}
    assert step(current, wait).action is Action.WAIT
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.reason == "valid_bound_result"
    assert result.response.text == "result"


def test_allow_disappearance_returns_to_normal_wait_without_refresh():
    current = snapshot()
    wait = {"mcp_allow_clicked_at": NOW.isoformat(), "mcp_allow_clicked_target": "target"}
    result = step(current, wait, 5)
    assert result.action is Action.WAIT
    assert result.reason == "wait_response"
    assert "mcp_allow_clicked_target" not in wait


def test_stop_after_allow_does_not_trigger_early_refresh():
    current = snapshot(stop_visible=True)
    wait = {"mcp_allow_clicked_at": NOW.isoformat(), "mcp_allow_clicked_target": "target"}
    result = step(current, wait, 6)
    assert result.action is Action.WAIT
    assert result.reason == "wait_response"


def test_stale_clicked_allow_marker_retries_when_permission_is_still_visible():
    current = snapshot(mcp_permission_node_count=1)
    wait = {
        "controller_state": "ALLOW",
        "controller_progress_at": NOW.isoformat(),
        "mcp_allow_clicked_target": "__dom__",
    }
    decision = step(current, wait)
    assert decision.action is Action.ALLOW
    assert decision.reason == "mcp_allow_ready"
    assert "mcp_allow_clicked_target" not in wait


def test_successful_allow_gets_short_post_click_window_then_retries_if_still_visible():
    current = snapshot(mcp_permission_node_count=1)
    wait = {
        "mcp_allow_clicked_at": NOW.isoformat(),
        "mcp_allow_clicked_target": "__dom__",
    }
    before = step(current, wait, 4)
    assert before.action is Action.WAIT
    assert before.reason == "mcp_allow_post_click"
    retry = step(current, wait, 5)
    assert retry.action is Action.ALLOW
    assert retry.reason == "mcp_allow_ready"


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


def test_failed_allow_with_real_dom_permission_remains_retryable():
    current = snapshot(mcp_permission_node_count=1)
    wait = {}
    permission = {"type": "allow", "target_message_id": "target", "remember_answer": True}

    class Client:
        async def auto_allow_mcp_permission(self, *, passive_action=None):
            return None

    controller = RoleController()
    result = asyncio.run(controller.browser_action(
        Client(), current, wait, permission, Decision(Action.ALLOW, "mcp_allow_ready")
    ))
    assert result.action is Action.WAIT
    assert result.reason == "mcp_allow_retry_pending"
    assert "mcp_allow_clicked_at" not in wait
    assert "mcp_allow_clicked_target" not in wait

    retry_at = datetime.fromisoformat(wait["mcp_allow_retry_at"])
    before = controller.decide(
        current,
        wait,
        receipt=receipt(),
        permission=permission,
        now=retry_at - timedelta(milliseconds=1),
        phase="pre_send",
    )
    assert before.action is Action.WAIT
    assert before.reason == "mcp_allow_retry_backoff"

    retry = controller.decide(
        current,
        wait,
        receipt=receipt(),
        permission=permission,
        now=retry_at,
        phase="pre_send",
    )
    assert retry.action is Action.ALLOW
    assert retry.reason == "mcp_allow_ready"


def test_operator_continuation_without_rendered_receipt_user_is_valid_steering():
    current = snapshot(messages=(
        MessageSnapshot("user", "operator-user", "operator-turn", "continue", ()),
        MessageSnapshot(
            "assistant", "operator-answer", "operator-answer-turn", "result",
            ("copy-turn-action-button",),
        ),
    ))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "operator-answer"
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "operator-answer"


def test_duplicate_receipt_dom_nodes_do_not_block_manual_user_steering():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "receipt-a", "original task", ()),
        MessageSnapshot("user", "original-user", "receipt-b", "original task", ()),
        MessageSnapshot("assistant", "pre-steer", "pre-steer-turn", "old result", ()),
        MessageSnapshot("user", "operator-user", "operator-turn", "continue current dev work", ()),
        MessageSnapshot(
            "assistant", "operator-answer", "operator-answer-turn", "result",
            ("copy-turn-action-button",),
        ),
    ))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "operator-answer"
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "operator-answer"


def test_bound_result_waits_while_current_page_generation_is_active():
    current = current_result(stop_visible=True)
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is Action.WAIT


def invalid(_candidate):
    raise ValueError("route JSON missing")


def test_stable_malformed_result_repairs_with_exact_validation_error_without_reload():
    current = current_result("not JSON")
    wait = {}
    assert step(current, wait, validate=invalid).action is Action.WAIT
    decision = step(current, wait, 5, validate=invalid)
    assert decision.action is Action.REPAIR
    assert decision.reason == "malformed_result"
    assert decision.response is not None
    assert decision.response.text == "not JSON"
    assert decision.validation_error == "route JSON missing"
    assert "timeout_refreshed" not in wait


def test_stale_listen_stream_status_cannot_hide_stable_dom_malformed_result():
    current = current_result("not JSON")
    wait = {}
    assert step(current, wait, validate=invalid, network_status="IS_STREAMING").action is Action.WAIT
    decision = step(current, wait, 5, validate=invalid, network_status="IS_STREAMING")
    assert decision.action is Action.REPAIR
    assert decision.validation_error == "route JSON missing"


def test_no_terminal_result_ui_error_is_not_format_repair():
    current = snapshot(error_texts=("Something went wrong",))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 5).action is Action.WAIT
    assert step(current, wait, 605).action is Action.REFRESH
    wait["timeout_refreshed"] = True
    wait["timeout_status_checked"] = True
    wait["timeout_status"] = "COMPLETE"
    decision = step(current, wait, 605)
    assert decision.action is Action.REPAIR
    assert decision.reason == "state_repair"
    assert decision.response is None
    assert decision.validation_error is None


def test_incomplete_stream_is_not_repaired_until_stop_clears():
    current = current_result("{", stop_visible=True)
    wait = {}
    step(current, wait, validate=invalid)
    assert step(current, wait, 5, validate=invalid).action is Action.WAIT
    assert step(current_result("{"), wait, 5, validate=invalid).action is Action.REPAIR


def test_retry_ui_repairs_after_five_stable_seconds_and_does_not_block_presend():
    current = snapshot(retry_visible=True)
    wait = {}
    assert step(current, wait).reason == "wait_response"
    assert step(current, wait, 4.9).action is Action.WAIT
    decision = step(current, wait, 5)
    assert decision.action is Action.REPAIR
    assert decision.reason == "retry_visible_stable"
    assert step(current, {}, phase="pre_send").reason == "ready"
    dirty = snapshot(composer_text="stale target draft", attachment_markers=("stale.txt",))
    assert step(dirty, {}, phase="pre_send").reason == "ready"


def test_retry_stability_resets_when_retry_disappears():
    wait = {}
    step(snapshot(retry_visible=True), wait)
    step(snapshot(), wait, 3)
    assert "retry_seen_at" not in wait
    assert step(snapshot(retry_visible=True), wait, 4).action is Action.WAIT
    assert step(snapshot(retry_visible=True), wait, 9).action is Action.REPAIR


def test_post_send_composer_does_not_block_retry_or_result_observation():
    draft_wait = {}
    step(snapshot(retry_visible=True, composer_text="my draft"), draft_wait)
    decision = step(snapshot(retry_visible=True, composer_text="my draft"), draft_wait, 5)
    assert decision.action is Action.REPAIR
    assert decision.reason == "retry_visible_stable"
    current = current_result(composer_text="my draft")
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is Action.ACCEPT


def test_session_expiry_is_not_retry_recovery():
    result = step(snapshot(retry_visible=True, requires_login=True), {})
    assert result.action is Action.BLOCK
    assert result.reason == "authentication_required"


def test_unknown_dialog_waits_but_permission_dialog_uses_allow_handler():
    assert step(snapshot(blocking_dialogs=("Unexpected dialog",)), {}).reason == "blocking_dialog_wait"
    current = snapshot(blocking_dialogs=("MCP permission",), mcp_permission_node_count=1)
    assert step(current, {}).reason == "mcp_allow_ready"


@pytest.mark.parametrize("stop", [False, True])
def test_timeout_refreshes_at_ten_minutes_regardless_of_stop(stop):
    current = snapshot(stop_visible=stop)
    wait = {}
    step(current, wait)
    assert step(current, wait, 599).action is Action.WAIT
    assert step(current, wait, 600).action is Action.REFRESH


def test_bound_dom_progress_restarts_stall_clock_but_not_on_first_observation():
    wait = {"started_at": NOW.isoformat(), "controller_progress_at": NOW.isoformat()}
    def streaming(text):
        return snapshot(messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", text, ()),
        ), stop_visible=True)

    assert step(streaming("draft"), wait).action is Action.WAIT
    assert wait["controller_progress_at"] == NOW.isoformat()
    assert step(streaming("draft growing"), wait, 590).action is Action.WAIT
    assert wait["controller_progress_at"] == (NOW + timedelta(seconds=590)).isoformat()
    assert step(streaming("draft growing"), wait, 600).action is Action.WAIT
    assert step(streaming("draft growing"), wait, 1190).action is Action.REFRESH


@pytest.mark.parametrize("changed", [True, False])
def test_bound_dom_virtualization_keeps_progress_history_without_stability(changed):
    wait = {"started_at": NOW.isoformat(), "controller_progress_at": NOW.isoformat()}

    def streaming(text):
        return snapshot(messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", text, ()),
        ), stop_visible=True)

    assert step(streaming("draft"), wait).action is Action.WAIT
    assert step(snapshot(stop_visible=True), wait, 590).action is Action.WAIT
    assert "result_seen_at" not in wait
    assert "result_samples" not in wait
    assert step(streaming("draft growing" if changed else "draft"), wait, 599).action is Action.WAIT
    assert wait["result_seen_at"] == (NOW + timedelta(seconds=599)).isoformat()
    assert wait["result_samples"] == 1
    assert wait["controller_progress_at"] == (NOW + timedelta(seconds=599 if changed else 0)).isoformat()
    assert step(streaming("draft growing" if changed else "draft"), wait, 600).action is Action.WAIT
    assert step(streaming("draft growing" if changed else "draft"), wait, 604).action is (
        Action.WAIT if changed else Action.REFRESH
    )


def test_unbound_output_cannot_extend_exact_response_stall():
    wait = {"started_at": NOW.isoformat(), "controller_progress_at": NOW.isoformat()}
    step(current_result("draft", stop_visible=True), wait)
    unrelated = snapshot(messages=(
        MessageSnapshot("user", "unrelated-user", "other-turn", "other", ()),
        MessageSnapshot("assistant", "other-answer", "other-turn", "unrelated", ()),
    ), stop_visible=True)
    assert step(unrelated, wait, 590).action is Action.WAIT
    assert step(unrelated, wait, 600).action is Action.REFRESH


def test_post_send_composer_does_not_block_timeout_recovery():
    current = snapshot(composer_text="keep this")
    wait = {}
    step(current, wait)
    decision = step(current, wait, 605)
    assert decision.action is Action.REFRESH
    assert decision.reason == "response_timeout_recheck"


def test_timeout_clock_does_not_reset_when_operational_state_changes():
    wait = {}
    assert step(snapshot(), wait).action is Action.WAIT
    assert step(snapshot(), wait, 590).action is Action.WAIT
    assert step(snapshot(stop_visible=True), wait, 590).action is Action.WAIT
    assert step(snapshot(stop_visible=True), wait, 600).action is Action.REFRESH


def test_timeout_after_refresh_keeps_waiting_when_stop_is_still_visible():
    wait = {
        "controller_progress_at": (NOW - timedelta(seconds=605)).isoformat(),
        "controller_state": "ALLOW",
        "timeout_refreshed": True,
        "timeout_status_checked": True,
        "timeout_status": "COMPLETE",
    }
    result = step(snapshot(stop_visible=True), wait)
    assert result.action is Action.WAIT
    assert result.reason == "response_still_active"


def test_timeout_after_refresh_keeps_waiting_when_listener_still_streams_without_stop():
    wait = {
        "controller_progress_at": (NOW - timedelta(seconds=605)).isoformat(),
        "controller_state": "IDLE",
        "timeout_refreshed": True,
        "timeout_status_checked": True,
        "timeout_status": "IS_STREAMING",
    }
    result = step(snapshot(), wait)
    assert result.action is Action.WAIT
    assert result.reason == "response_still_active"


def test_timeout_after_refresh_keeps_waiting_and_rearms_when_stop_and_listener_are_active():
    wait = {
        "controller_progress_at": (NOW - timedelta(seconds=605)).isoformat(),
        "controller_state": "STOP",
        "timeout_refreshed": True,
        "timeout_status_checked": True,
        "timeout_status": "IS_STREAMING",
    }
    assert step(snapshot(stop_visible=True), wait).action is Action.WAIT
    assert wait["controller_progress_at"] == NOW.isoformat()
    assert "timeout_refreshed" not in wait
    assert "timeout_status_checked" not in wait
    assert "timeout_status" not in wait


def test_later_human_turn_reanchors_already_bound_assistant():
    current = current_result()
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "answer"
    interleaved = snapshot(messages=(
        *current.messages,
        MessageSnapshot("user", "another", "another", "change", ()),
        MessageSnapshot(
            "assistant", "human-answer", "human-answer-turn", "other result",
            ("copy-turn-action-button",),
        ),
    ))
    first = step(interleaved, wait, 5)
    assert first.action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "human-answer"
    assert wait["bound_assistant_turn_id"] == "human-answer-turn"
    assert wait["user_steering_rebind_count"] == 1
    result = step(interleaved, wait, 10)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "human-answer"


def test_user_injection_before_bound_assistant_is_valid_steering():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("user", "manual-user", "manual-turn", "manual", ()),
        MessageSnapshot(
            "assistant", "manual-answer", "manual-answer-turn", "later",
            ("copy-turn-action-button",),
        ),
    ))
    wait = {}
    first = step(current, wait)
    assert first.action is Action.WAIT
    assert "response_binding_error" not in wait
    assert wait["bound_assistant_message_id"] == "manual-answer"
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "manual-answer"


def test_multiple_user_injections_follow_latest_user_without_blocking():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("user", "manual-user-1", "manual-turn-1", "first steer", ()),
        MessageSnapshot("user", "manual-user-2", "manual-turn-2", "second steer", ()),
        MessageSnapshot(
            "assistant", "manual-answer", "manual-answer-turn", "final after steering",
            ("copy-turn-action-button",),
        ),
    ))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert "response_binding_error" not in wait
    assert wait["bound_assistant_message_id"] == "manual-answer"
    assert step(current, wait, 5).action is Action.ACCEPT


def test_bound_assistant_remount_same_identity_keeps_binding():
    wait = {}
    first = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "part", ()),
    ))
    assert step(first, wait).action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "answer"
    remounted = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot(
            "assistant", "answer-remounted", "answer-turn", "done",
            ("copy-turn-action-button",),
        ),
    ))
    assert step(remounted, wait, 1).action is Action.WAIT
    result = step(remounted, wait, 6)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer-remounted"
    assert wait["bound_assistant_turn_id"] == "answer-turn"


def test_bound_assistant_survives_receipt_user_virtualization():
    wait = {}
    first = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "part", ()),
    ))
    assert step(first, wait).action is Action.WAIT

    virtualized = snapshot(messages=(
        MessageSnapshot(
            "assistant", "answer-remounted", "answer-turn", "done",
            ("copy-turn-action-button",),
        ),
    ))
    assert step(virtualized, wait, 1).action is Action.WAIT
    result = step(virtualized, wait, 6)

    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer-remounted"
    assert wait["bound_assistant_turn_id"] == "answer-turn"


def test_bound_assistant_rejects_incompatible_reuse_of_persisted_message_node():
    wait = {}
    first = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "part", ()),
    ))
    assert step(first, wait).action is Action.WAIT

    incompatible = snapshot(messages=(
        MessageSnapshot(
            "assistant", "answer", "different-turn", "wrong",
            ("copy-turn-action-button",),
        ),
    ))
    result = step(incompatible, wait, 1)

    assert result.action is Action.BLOCK
    assert result.reason == "incompatible_bound_assistant_identity"


def test_duplicate_bound_assistant_message_identity_blocks_explicitly():
    wait = {}
    first = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "part", ()),
    ))
    assert step(first, wait).action is Action.WAIT

    duplicated = snapshot(messages=(
        MessageSnapshot("assistant", "answer", "answer-turn", "one", ()),
        MessageSnapshot(
            "assistant", "answer", "answer-turn", "two",
            ("copy-turn-action-button",),
        ),
    ))
    result = step(duplicated, wait, 1)

    assert result.action is Action.BLOCK
    assert result.reason == "duplicate_bound_assistant_identity"


def test_duplicate_bound_logical_turn_blocks_even_with_distinct_message_nodes():
    wait = {}
    first = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "part", ()),
    ))
    assert step(first, wait).action is Action.WAIT

    duplicated = snapshot(messages=(
        MessageSnapshot("assistant", "answer-remount-1", "answer-turn", "one", ()),
        MessageSnapshot(
            "assistant", "answer-remount-2", "answer-turn", "two",
            ("copy-turn-action-button",),
        ),
    ))
    result = step(duplicated, wait, 1)

    assert result.action is Action.BLOCK
    assert result.reason == "duplicate_bound_assistant_identity"


def test_duplicate_receipt_dom_nodes_collapse_to_latest_logical_anchor():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "one", "original task", ()),
        MessageSnapshot("user", "original-user", "two", "original task", ()),
        MessageSnapshot(
            "assistant", "answer", "answer-turn", "done",
            ("copy-turn-action-button",),
        ),
    ))
    wait = {}
    first = step(current, wait)
    assert first.action is Action.WAIT
    assert wait["bound_assistant_message_id"] == "answer"
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"


def test_completed_action_is_not_required_for_bound_terminal_result():
    wait = {}
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "done", ()),
    ))
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 5).action is Action.ACCEPT
    completed = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot(
            "assistant", "answer", "answer-turn", "done",
            ("copy-turn-action-button",),
        ),
    ))
    assert step(completed, wait, 5).action is Action.ACCEPT


def test_history_after_allow_never_refreshes_old_conversation():
    current = snapshot()
    wait = {"mcp_allow_clicked_at": (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()}

    class Client:
        async def read_wait_probe(self):
            return current

        def page_observation(self):
            return {}

        async def refresh(self):
            raise AssertionError("history automation must never refresh")

    result = asyncio.run(RoleController().maintain_history(Client(), wait))
    assert result.action is Action.WAIT
    assert result.reason == "history_observation"


def test_timeout_refresh_preserves_clock_until_post_reload_state_changes():
    class Client:
        async def refresh(self, *, reason=None):
            return None

    wait = {"controller_progress_at": NOW.isoformat()}
    decision = Decision(Action.REFRESH, "response_timeout_recheck")
    asyncio.run(RoleController().browser_action(Client(), snapshot(), wait, None, decision))

    assert wait["controller_progress_at"] == NOW.isoformat()
    assert wait["timeout_refreshed"] is True


def test_valid_result_is_read_after_reload_settle_then_stabilized():
    current = current_result()
    wait = {"refresh_ready_at": (NOW + timedelta(seconds=20)).isoformat()}
    assert step(current, wait).reason == "reload_settle"
    assert step(current, wait, 20).reason == "result_stability"
    assert step(current, wait, 25).action is Action.ACCEPT


def test_reload_settle_window_is_twenty_seconds():
    wait = {"refresh_ready_at": (NOW + timedelta(seconds=20)).isoformat()}
    assert step(snapshot(retry_visible=True), wait, 19).reason == "reload_settle"
    assert step(snapshot(retry_visible=True), wait, 20).reason == "wait_response"


def test_empty_assistant_placeholder_does_not_hide_completed_result():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot(
            "assistant", "answer", "answer-turn", '{"route":"REVIEW"}',
            ("copy-turn-action-button",),
        ),
        MessageSnapshot("assistant", "placeholder", "answer-turn", "", ()),
    ))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    result = step(current, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"


def test_correlated_listen_result_waits_five_seconds_before_accept():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
    ))
    listen = MessageSnapshot(
        "assistant", "answer", "answer-turn", '{"route":"REVIEW"}',
        ("copy-turn-action-button",),
    )
    wait = {
        "bound_assistant_message_id": "answer",
        "bound_assistant_turn_id": "answer-turn",
    }
    first = step(current, wait, network_response=listen)
    assert first.action is Action.WAIT
    assert wait["listen_result_samples"] == 1
    result = step(current, wait, 5, network_response=listen)
    assert result.action is Action.ACCEPT
    assert result.reason == "valid_bound_result"
    assert wait["terminal_response_source"] == "listen"



def test_correlated_listen_result_wins_before_dialog_wait_gate():
    current = snapshot(
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", "partial", ()),
        ),
        blocking_dialogs=("Unexpected dialog",),
    )
    listen = MessageSnapshot(
        "assistant", "answer", "answer-turn", '{"route":"REVIEW"}',
        ("copy-turn-action-button",),
    )
    wait = {}
    assert step(current, wait, network_response=listen).action is Action.WAIT
    result = step(current, wait, 5, network_response=listen)
    assert result.action is Action.ACCEPT
    assert result.reason == "valid_bound_result"


def test_stale_network_streaming_does_not_block_pre_send():
    result = RoleController().decide(
        snapshot(stop_visible=False),
        {},
        receipt=receipt(),
        phase="pre_send",
        network_status="IS_STREAMING",
        now=NOW,
    )
    assert result.action is Action.WAIT
    assert result.reason == "ready"


def test_uncorrelated_listen_response_cannot_replace_bound_dom_response():
    dom = current_result('{"route":"DEV"}')
    listen = MessageSnapshot(
        "assistant", "listen-answer", "listen-turn", '{"route":"REVIEW"}',
        ("copy-turn-action-button",),
    )
    wait = {}
    assert step(dom, wait, network_response=listen).reason == "result_stability"
    result = step(dom, wait, 5, network_response=listen)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"
    assert wait["terminal_response_source"] == "dom"


def test_bound_dom_response_finishes_without_listen_response():
    dom = current_result('{"route":"DEV"}')
    wait = {}
    assert step(dom, wait).reason == "result_stability"
    result = step(dom, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"
    assert wait["terminal_response_source"] == "dom"


def test_stale_unrelated_listen_streaming_cannot_block_terminal_bound_dom():
    dom = current_result('{"route":"DEV"}')
    wait = {}
    assert step(dom, wait, network_status="IS_STREAMING").reason == "result_stability"
    result = step(dom, wait, 5, network_status="IS_STREAMING")
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"
    assert wait["terminal_response_source"] == "dom"


def test_terminal_bound_dom_does_not_require_completion_action_buttons():
    dom = snapshot(
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"DEV"}', ()),
        ),
        stop_visible=False,
        response_activity_turn_id=None,
    )
    wait = {}
    assert step(dom, wait, network_status="IS_STREAMING").reason == "result_stability"
    result = step(dom, wait, 5, network_status="IS_STREAMING")
    assert result.action is Action.ACCEPT
    assert result.reason == "valid_bound_result"


@pytest.mark.parametrize(
    "current",
    (
        snapshot(
            messages=(
                MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
                MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"DEV"}', ()),
            ),
            stop_visible=False,
            response_activity_turn_id="answer-turn",
        ),
        snapshot(
            messages=(
                MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
                MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"DEV"}', ()),
            ),
            stop_visible=False,
            retry_visible=True,
        ),
    ),
)
def test_terminal_bound_dom_rejects_active_generation_or_retry(current):
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is not Action.ACCEPT


def test_stale_response_activity_turn_id_is_audit_evidence_not_presend_gate():
    current = snapshot(response_activity_turn_id="stale-turn")
    result = RoleController().decide(
        current,
        {},
        receipt=receipt(),
        phase="pre_send",
        network_status="COMPLETE",
        now=NOW,
    )
    assert result.action is Action.WAIT
    assert result.reason == "ready"
