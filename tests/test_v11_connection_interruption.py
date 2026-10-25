"""Read-only interrupted-stream observation. No automatic Stop, resend, or reload."""
import asyncio
from datetime import datetime, timedelta, timezone

from playwright_auto.chatgpt import inspect_chatgpt_page, inspect_chatgpt_wait_probe
from playwright_auto.chatgpt_models import ChatGPTState
from playwright_auto.role_runtime import Action, RoleController
from test_chatgpt_mcp_permission import _with_page
from test_operational_contract import receipt, snapshot

T0 = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)


def observed(*, activity="work in progress", retry=False, **attrs):
    return snapshot(
        state=ChatGPTState.RESPONDING,
        stop_visible=True,
        response_activity_text=activity,
        response_activity_length=len(activity),
        connection_interrupted_visible=True,
        retry_visible=retry,
        **attrs,
    )


def decide(snap, wait, seconds):
    return RoleController().decide(
        snap, wait, receipt=receipt(), now=T0 + timedelta(seconds=seconds)
    )


def test_interrupt_banner_detected_in_full_and_sparse_dom_only_when_ui_visible():
    async def run(page):
        full = await inspect_chatgpt_page(page)
        sparse = await inspect_chatgpt_wait_probe(page)
        assert full.connection_interrupted_visible
        assert sparse.connection_interrupted_visible
        await page.evaluate("document.getElementById('banner').remove()")
        sparse2 = await inspect_chatgpt_wait_probe(page, previous_probe=sparse)
        assert not sparse2.connection_interrupted_visible
        assert not await page.evaluate("Boolean(document.getElementById('banner'))")

    asyncio.run(_with_page('''<main>
      <p id="banner" role="status">Connection interrupted. Waiting for the complete answer</p>
      <div contenteditable="true" role="textbox"></div>
    </main>''', run))


def test_quote_in_user_content_never_triggers_banner():
    async def run(page):
        full = await inspect_chatgpt_page(page)
        sparse = await inspect_chatgpt_wait_probe(page)
        assert not full.connection_interrupted_visible
        assert not sparse.connection_interrupted_visible

    asyncio.run(_with_page('''<main>
      <div data-message-author-role="user" data-message-id="u1"><span>Connection interrupted. Waiting for the complete answer</span></div>
      <div contenteditable="true" role="textbox"></div>
    </main>''', run))


def test_ten_minutes_of_no_visible_progress_is_observation_not_new_send():
    current = observed()
    wait = {}
    assert decide(current, wait, 0).action is Action.WAIT
    assert decide(current, wait, 599).action is Action.WAIT
    stalled = decide(current, wait, 600)
    assert (stalled.action, stalled.reason) == (Action.BLOCK, "connection_interrupted_unconfirmed")
    assert wait["connection_interruption"]["since"] == T0.isoformat()
    assert wait["connection_interruption"]["progress_at"] == T0.isoformat()


def test_new_content_restarts_window_even_if_banner_remains_visible():
    wait = {}
    old = observed(activity="stage 1")
    changed = observed(activity="stage 2")
    assert decide(old, wait, 0).action is Action.WAIT
    assert decide(old, wait, 580).action is Action.WAIT
    assert decide(changed, wait, 600).action is Action.WAIT
    assert decide(changed, wait, 1199).action is Action.WAIT
    assert decide(changed, wait, 1200).reason == "connection_interrupted_unconfirmed"
    assert wait["connection_interruption"]["progress_at"] == (T0 + timedelta(seconds=600)).isoformat()


def test_banner_disappearing_clears_timer_and_retry_does_not_force_stop():
    wait = {}
    decide(observed(), wait, 0)
    assert "connection_interruption" in wait
    snap = snapshot(state=ChatGPTState.WAITING_PROMPT, stop_visible=False)
    decide(snap, wait, 1)
    assert "connection_interruption" not in wait
    retry = snapshot(connection_interrupted_visible=True, stop_visible=False, retry_visible=True)
    assert decide(retry, wait, 700).action is Action.WAIT
    assert "connection_interruption" not in wait
    assert decide(retry, wait, 705).action is Action.RETRY


def test_error_banner_in_assistant_quote_never_sets_interrupt_flag():
    async def run(page):
        assert not (await inspect_chatgpt_wait_probe(page)).connection_interrupted_visible

    asyncio.run(_with_page('''<main>
      <article data-turn-key="assistant-1" data-message-author-role="assistant" data-message-id="a1">
        <p>Connection interrupted. Waiting for the complete answer</p>
      </article>
      <div contenteditable="true" role="textbox"></div>
    </main>''', run))


def test_valid_terminal_result_outweighs_interrupt_notice():
    from playwright_auto.chatgpt import MessageSnapshot

    candidate = snapshot(
        connection_interrupted_visible=True,
        error_texts=("Connection interrupted. Waiting for the complete answer",),
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "result-id", "result-turn", "DONE", ("copy",)),
        ),
    )
    wait = {}
    assert decide(candidate, wait, 0).action is Action.WAIT
    result = decide(candidate, wait, 5)
    assert (result.action, result.reason) == (Action.ACCEPT, "valid_bound_result")


def test_new_owned_page_resets_entire_interruption_window():
    wait = {}
    first = observed(page_id="first-page")
    second = observed(page_id="second-page")
    assert decide(first, wait, 0).action is Action.WAIT
    assert decide(second, wait, 590).action is Action.WAIT
    assert wait["connection_interruption"]["since"] == (T0 + timedelta(seconds=590)).isoformat()
    assert decide(second, wait, 1189).action is Action.WAIT


def test_permission_surface_is_handled_before_connection_stall_timer():
    wait = {}
    first = observed(mcp_permission_node_count=1, mcp_permission_allow_count=1,
                     mcp_permission_surface_key="permission-one")
    assert decide(first, wait, 0).action is Action.ALLOW
    # A remaining permission belongs to the permission owner's own bounded window.
    result = decide(first, wait, 601)
    assert result.reason != "connection_interrupted_unconfirmed"


def test_incomplete_route_under_interruption_does_not_repair_after_five_seconds():
    from playwright_auto.chatgpt import MessageSnapshot

    current = snapshot(
        connection_interrupted_visible=True,
        state=ChatGPTState.ERROR,
        stop_visible=False,
        error_texts=("Connection interrupted. Waiting for the complete answer",),
        messages=(
            MessageSnapshot("user", "original-user", "original-turn", "original task", ()),
            MessageSnapshot("assistant", "partial-id", "partial-turn", '{"route":', ()),
        ),
    )

    def invalid(_candidate):
        raise ValueError("invalid route")

    controller = RoleController()
    wait = {}
    first = controller.decide(current, wait, receipt=receipt(), validate=invalid, now=T0)
    second = controller.decide(current, wait, receipt=receipt(), validate=invalid,
                               now=T0 + timedelta(seconds=6))
    assert (first.action, second.action) == (Action.WAIT, Action.WAIT)
    assert second.reason == "connection_interrupted_observing"
    stalled = controller.decide(current, wait, receipt=receipt(), validate=invalid,
                                now=T0 + timedelta(seconds=600))
    assert stalled.reason == "connection_interrupted_unconfirmed"


def test_interruption_is_observable_in_existing_signal_diagnostics():
    from playwright_auto.role_runtime.signals import RoleSignals

    raw = observed()
    diagnostics = RoleSignals.from_sources(raw, {}).debug_values()
    assert diagnostics["dom_connection_interrupted_visible"] is True
    assert not any("work in progress" in str(value) for value in diagnostics.values())
