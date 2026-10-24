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


@pytest.mark.parametrize("actionable", [False, True])
def test_allow_requires_an_actionable_dom_surface(actionable):
    current = snapshot(mcp_permission_node_count=1,
                       mcp_permission_allow_count=int(actionable),
                       mcp_permission_surface_key="test-surface")
    result = step(current, {})
    assert (result.action, result.reason) == (
        (Action.ALLOW, "mcp_allow_ready") if actionable
        else (Action.BLOCK, "permission_surface_unactionable")
    )


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


def test_valid_result_wins_over_permission_evidence_after_five_second_stability():
    current = current_result(mcp_permission_node_count=1, mcp_permission_allow_count=1,
                             mcp_permission_surface_key="test-surface")
    wait = {}
    assert step(current, wait).action is Action.ALLOW
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


def test_stuck_allow_cannot_hide_timeout_timer():
    current = snapshot(mcp_permission_node_count=1, mcp_permission_allow_count=1,
                       mcp_permission_surface_key="test-surface")
    wait = {
        "controller_state": "ALLOW",
        "controller_progress_at": NOW.isoformat(),
        "mcp_allow_clicked_target": "test-surface",
    }
    assert step(current, wait, 599).action is Action.WAIT
    assert step(current, wait, 600).action is Action.BLOCK


def test_failed_allow_with_no_dom_permission_clears_stale_network_evidence():
    current = snapshot(mcp_permission_node_count=0, mcp_permission_allow_count=0)
    wait = {"mcp_allow_seen_at": NOW.isoformat(), "mcp_allow_seen_target": "target"}

    class Client:
        cleared = False

        async def auto_allow_mcp_permission(self, *, passive_action=None, **_identity):
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


def test_failed_allow_with_real_dom_permission_waits_until_normal_timeout():
    current = snapshot(mcp_permission_node_count=1, mcp_permission_allow_count=1,
                       mcp_permission_surface_key="target")
    wait = {}
    permission = {"type": "allow", "target_message_id": "untrusted-listen-id", "remember_answer": True}

    class Client:
        async def auto_allow_mcp_permission(self, *, passive_action=None, **_identity):
            return None

    result = asyncio.run(RoleController().browser_action(
        Client(), current, wait, permission, Decision(Action.ALLOW, "mcp_allow_ready")
    ))
    assert result.action is Action.WAIT
    assert result.reason == "mcp_allow_handler_pending"
    assert "mcp_allow_clicked_target" not in wait
    assert "mcp_allow_clicked_at" not in wait
    pending = datetime.fromisoformat(wait["mcp_allow_pending_at"])
    assert wait["mcp_allow_pending_target"] == "target"
    clicked = pending
    wait["controller_progress_at"] = clicked.isoformat()

    decision = RoleController().decide(
        current,
        wait,
        receipt=receipt(),
        permission=permission,
        now=clicked + timedelta(seconds=599),
    )
    assert decision.action is Action.WAIT
    decision = RoleController().decide(
        current,
        wait,
        receipt=receipt(),
        permission=permission,
        now=clicked + timedelta(seconds=600),
    )
    assert (decision.action, decision.reason) == (Action.BLOCK, "mcp_allow_unresolved")


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


def test_valid_result_waits_while_stop_is_visible_then_finishes():
    current = current_result(stop_visible=True)
    wait = {}
    step(current, wait)
    assert step(current, wait, 5).action is Action.WAIT
    assert step(current_result(), wait, 5).action is Action.ACCEPT


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


def test_explicit_cloudflare_challenge_blocks_without_retry_or_reload():
    decision = step(snapshot(error_texts=("cloudflare_challenge Retry",)), {})

    assert (decision.action, decision.reason) == (Action.BLOCK, "provider_challenge")


def test_no_terminal_result_ui_error_is_not_format_repair():
    current = snapshot(error_texts=("Something went wrong",))
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 5).action is Action.WAIT
    decision = step(current, wait, 605)
    assert (decision.action, decision.reason) == (Action.BLOCK, "response_terminal_missing")
    assert "timeout_refreshed" not in wait


def test_incomplete_stream_is_not_repaired_until_stop_clears():
    current = current_result("{", stop_visible=True)
    wait = {}
    step(current, wait, validate=invalid)
    assert step(current, wait, 5, validate=invalid).action is Action.WAIT
    assert step(current_result("{"), wait, 5, validate=invalid).action is Action.REPAIR


def test_retry_ui_invokes_provider_retry_after_five_stable_seconds_and_does_not_block_presend():
    current = snapshot(retry_visible=True)
    wait = {}
    assert step(current, wait).reason == "wait_response"
    assert step(current, wait, 4.9).action is Action.WAIT
    decision = step(current, wait, 5)
    assert decision.action is Action.RETRY
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
    assert step(snapshot(retry_visible=True), wait, 9).action is Action.RETRY


def test_post_send_composer_does_not_block_retry_or_result_observation():
    draft_wait = {}
    step(snapshot(retry_visible=True, composer_text="my draft"), draft_wait)
    decision = step(snapshot(retry_visible=True, composer_text="my draft"), draft_wait, 5)
    assert decision.action is Action.RETRY
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
    current = snapshot(blocking_dialogs=("MCP permission",), mcp_permission_node_count=1,
                       mcp_permission_allow_count=1, mcp_permission_surface_key="test-surface")
    assert step(current, {}).reason == "mcp_allow_ready"


@pytest.mark.parametrize("stop", [False, True])
def test_timeout_checks_fresh_dom_and_never_auto_reload(stop):
    current = snapshot(stop_visible=stop)
    wait = {}
    step(current, wait)
    assert step(current, wait, 599).action is Action.WAIT
    decision = step(current, wait, 600)
    assert (decision.action, decision.reason) == (
        (Action.WAIT, "response_still_active") if stop
        else (Action.BLOCK, "response_terminal_missing")
    )


def test_post_send_composer_is_never_cleared_during_terminal_timeout():
    current = snapshot(composer_text="keep this", attachment_markers=("file.txt",))
    wait = {}
    step(current, wait)
    decision = step(current, wait, 605)
    assert (decision.action, decision.reason) == (Action.BLOCK, "response_terminal_missing")
    assert wait["manual_draft_present"] is True
    assert "timeout_refreshed" not in wait


def test_timeout_clock_rearms_only_from_fresh_dom_activity():
    wait = {}
    assert step(snapshot(), wait).action is Action.WAIT
    assert step(snapshot(), wait, 590).action is Action.WAIT
    assert step(snapshot(stop_visible=True), wait, 600).reason == "response_still_active"
    assert step(snapshot(), wait, 610).action is Action.WAIT
    assert step(snapshot(), wait, 1200).action is Action.BLOCK


def test_timeout_stale_listener_status_cannot_prolong_waiting():
    wait = {
        "controller_progress_at": (NOW - timedelta(seconds=605)).isoformat(),
        "timeout_refreshed": True,
        "timeout_status_checked": True,
        "timeout_status": "IS_STREAMING",
    }
    result = step(snapshot(), wait)
    assert (result.action, result.reason) == (Action.BLOCK, "response_terminal_missing")


def test_timeout_fresh_stop_wins_over_old_listener_failure():
    wait = {
        "controller_progress_at": (NOW - timedelta(seconds=605)).isoformat(),
        "timeout_status": "FAILURE",
    }
    assert step(snapshot(stop_visible=True), wait).reason == "response_still_active"
    assert wait["controller_progress_at"] == NOW.isoformat()


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


def test_duplicate_receipt_user_identity_blocks():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "one", "original task", ()),
        MessageSnapshot("user", "original-user", "two", "original task", ()),
    ))
    result = step(current, {})
    assert result.action is Action.BLOCK
    assert result.reason == "duplicate_receipt_user_identity"


def test_completed_action_is_required_for_bound_terminal_result():
    wait = {}
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "done", ()),
    ))
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 5).action is Action.WAIT
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
        async def refresh(self):
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


def test_correlated_listen_result_never_accepts_without_dom_completion():
    current = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "partial", ()),
    ))
    listen = MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"REVIEW"}', ("Copy",))
    wait = {}
    assert step(current, wait, network_response=listen).action is Action.WAIT
    assert step(current, wait, 5, network_response=listen).action is Action.WAIT
    assert "listen_result_samples" not in wait


def test_choice_prompt_waits_instead_of_blocking_task():
    result = step(snapshot(choice_prompt_labels=("Continue",)), {})
    assert result.action is Action.WAIT
    assert result.reason == "choice_prompt_wait"


def test_correlated_listen_result_cannot_bypass_dialog_and_choice_gates():
    current = snapshot(
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", "partial", ()),
        ),
        blocking_dialogs=("Unexpected dialog",),
        choice_prompt_labels=("Continue",),
    )
    listen = MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"REVIEW"}', ("Copy",))
    wait = {}
    assert step(current, wait, network_response=listen).reason == "result_stability"
    assert step(current, wait, 5, network_response=listen).reason == "blocking_dialog_wait"
    assert "listen_result_samples" not in wait


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


def test_bound_dom_response_finishes_without_listen_response():
    dom = current_result('{"route":"DEV"}')
    wait = {}
    assert step(dom, wait).reason == "result_stability"
    result = step(dom, wait, 5)
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"


def test_stale_unrelated_listen_streaming_cannot_block_terminal_bound_dom():
    dom = current_result('{"route":"DEV"}')
    wait = {}
    assert step(dom, wait, network_status="IS_STREAMING").reason == "result_stability"
    result = step(dom, wait, 5, network_status="IS_STREAMING")
    assert result.action is Action.ACCEPT
    assert result.response.message_id == "answer"


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
