"""Architecture regressions: only the owned page DOM may decide a role turn."""
from datetime import datetime, timedelta, timezone

from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.role_runtime import Action, RoleController
from playwright_auto.role_runtime.signals import RoleSignals
from test_operational_contract import receipt, snapshot

BASE = datetime(2026, 10, 14, tzinfo=timezone.utc)


def decide(current, wait, elapsed=0, **kwargs):
    return RoleController().decide(
        current, wait, receipt=receipt(), now=BASE + timedelta(seconds=elapsed), **kwargs
    )


def test_a1_inactive_missing_terminal_does_not_wait_forever_or_reload():
    wait = {}
    assert decide(snapshot(), wait).action is Action.WAIT
    terminal = decide(snapshot(), wait, 600)
    assert (terminal.action, terminal.reason) == (Action.BLOCK, "response_terminal_missing")
    assert "timeout_refreshed" not in wait


def test_a1_visible_dom_active_is_not_overridden_by_old_timeout_status():
    wait = {
        "controller_progress_at": BASE.isoformat(),
        "timeout_status": "FAILURE",  # stale network observation
        "timeout_refreshed": True,
        "timeout_status_checked": True,
    }
    active = decide(snapshot(stop_visible=True), wait, 600)
    assert active.action is Action.WAIT
    assert active.reason == "response_still_active"
    assert decide(snapshot(), wait, 610).action is Action.WAIT
    assert decide(snapshot(), wait, 1210).action is Action.BLOCK


def test_a5_listen_result_cannot_replace_dom_assistant_or_synthesize_terminal():
    dom = snapshot(messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "still working", ()),
    ))
    forged = MessageSnapshot("assistant", "answer", "answer-turn", '{"route":"DONE"}', ("Copy",))
    wait = {}
    assert decide(dom, wait, network_response=forged).action is Action.WAIT
    second = decide(dom, wait, 6, network_response=forged)
    assert second.action is not Action.ACCEPT
    assert "listen_result_seen_key" not in wait


def test_a5_streaming_network_status_cannot_claim_dom_active():
    signals = RoleSignals.from_sources(snapshot(), {"status": "IS_STREAMING"})
    assert not signals.transport_active


def test_a5_stale_passive_network_permission_cannot_click_without_dom():
    wait = {}
    decision = decide(snapshot(), wait, permission={"target_message_id": "old", "type": "allow"})
    assert decision.action is Action.WAIT
    assert decision.reason == "wait_response"


def test_a5_visible_mcp_permission_precedes_retry_and_invalid_repair():
    current = snapshot(
        mcp_permission_allow_count=1,
        mcp_permission_surface_key="fingerprint-current-turn",
        retry_visible=True,
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "answer", "answer-turn", "not JSON", ()),
        ),
    )
    def invalid(_message):
        raise ValueError("invalid route")
    wait = {}
    assert decide(current, wait, validate=invalid).action is Action.ALLOW
    assert decide(current, wait, 6, validate=invalid).action is Action.ALLOW
    assert "retry_seen_at" not in wait


def test_a4_empty_active_shell_never_consumes_repair():
    current = snapshot(stop_visible=True, messages=(
        MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "", ()),
    ))
    wait = {}
    def invalid(_message):
        raise ValueError("not route JSON")
    assert decide(current, wait, validate=invalid).action is Action.WAIT
    assert decide(current, wait, 30, validate=invalid).action is Action.WAIT
    assert "result_seen_key" not in wait


def test_a5_pending_allow_cannot_fall_through_to_retry_or_repair():
    current = snapshot(
        retry_visible=True, mcp_permission_node_count=1,
        mcp_permission_allow_count=1, mcp_permission_surface_key="fingerprint-current-turn",
    )
    wait = {
        "mcp_allow_clicked_target": "fingerprint-current-turn",
        "retry_seen_at": (BASE - timedelta(seconds=30)).isoformat(),
        "controller_progress_at": BASE.isoformat(),
    }
    assert decide(current, wait, 6).reason == "mcp_allow_pending"
    assert decide(current, wait, 600).action is Action.BLOCK


def test_a2_route_prompt_requires_substantive_completion_not_self_continuation():
    from playwright_auto.cdpa_prompts import PromptBuilder

    guidance = PromptBuilder.current_role_execution(
        "DEV", physical_role="DEV", source_physical_role=None, closing_role="REVIEW"
    )
    assert "stay in this same turn" in guidance
    assert "do not route to yourself" in guidance
    explicit = PromptBuilder.current_role_execution(
        "DEV", physical_role="DEV", source_physical_role=None,
        closing_role="REVIEW", allow_self_route=True,
    )
    assert "do not route to yourself" not in explicit
