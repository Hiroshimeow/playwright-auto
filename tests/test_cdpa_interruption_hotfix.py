"""Regression for dead transport; browser fixtures are disposable, never live CDP."""
import asyncio
import inspect
from datetime import timedelta

import pytest

from playwright_auto.cdpa_actions import CDPATabActions
from playwright_auto.chatgpt import ChatGPTPage, PageBinding, UnsafePageStateError
from playwright_auto.role_runtime import Action
from test_role_runtime import NOW, current_result, step, invalid
from test_operational_contract import snapshot
from test_chatgpt_mcp_permission import _with_page


NOTICE = "Connection interrupted. Waiting for the complete answer"


def test_interrupted_stale_stop_continues_after_five_minutes_without_content():
    current = snapshot(stop_visible=True, response_activity_text=NOTICE)
    wait = {}
    assert step(current, wait).action is Action.WAIT
    assert step(current, wait, 299).action is Action.WAIT
    decision = step(current, wait, 300)
    assert decision.action is Action.REPAIR
    assert decision.reason == "interrupted_response"
    # A restart loads the same clock, not another five-minute grace.
    assert step(current, dict(wait), 301).reason == "interrupted_response"


def test_empty_response_after_stop_edge_continues_but_complete_result_wins():
    wait = {}
    step(snapshot(stop_visible=True), wait)
    assert step(snapshot(), wait, 1).action is Action.WAIT
    assert step(snapshot(), wait, 6).reason == "inactive_response"
    finished = {}
    step(current_result(stop_visible=True), finished)
    assert step(current_result(), finished, 5).action is Action.ACCEPT


def test_real_content_progress_not_attributes_resets_interruption_clock():
    wait = {}
    step(current_result("{", stop_visible=True, response_activity_text=NOTICE), wait, validate=invalid)
    step(current_result("{growing", stop_visible=True, response_activity_text=NOTICE), wait, 290, validate=invalid)
    assert step(current_result("{growing", stop_visible=True, response_activity_text=NOTICE), wait, 300, validate=invalid).action is Action.WAIT
    assert step(current_result("{growing", stop_visible=True, response_activity_text=NOTICE), wait, 590, validate=invalid).reason == "interrupted_response"


@pytest.mark.parametrize("interrupted", [False, True])
def test_current_permission_suppresses_recovery_without_extending_progress(interrupted):
    wait = {}
    step(snapshot(stop_visible=True), wait)
    current = snapshot(mcp_permission_node_count=1, stop_visible=interrupted,
                       response_activity_text=NOTICE if interrupted else "")
    step(current, wait, 1)
    decision = step(current, wait, 300 if interrupted else 6)
    assert decision.action is Action.ALLOW
    assert wait["controller_progress_at"] == NOW.isoformat()
    wait.update(mcp_allow_clicked_target="__dom__",
                mcp_allow_clicked_at=(NOW + timedelta(seconds=300 if interrupted else 6)).isoformat())
    assert step(current, wait, 301 if interrupted else 7).reason == "mcp_allow_post_click"
    assert step(snapshot(stop_visible=interrupted, response_activity_text=current.response_activity_text), wait,
                305 if interrupted else 11).action is Action.REPAIR


def test_first_bound_content_near_interruption_deadline_restarts_clock():
    wait = {}
    empty = current_result("", stop_visible=True, response_activity_text=NOTICE)
    step(empty, wait)
    partial = current_result("{partial", stop_visible=True, response_activity_text=NOTICE)
    assert step(partial, wait, 290, validate=invalid).action is Action.WAIT
    assert wait["controller_progress_at"] == (NOW + timedelta(seconds=290)).isoformat()
    assert step(partial, wait, 300, validate=invalid).action is Action.WAIT
    assert step(partial, wait, 589, validate=invalid).action is Action.WAIT
    assert step(partial, wait, 590, validate=invalid).reason == "interrupted_response"
    # A legitimate stable terminal result still outranks the old inactive edge.
    terminal = current_result("result")
    assert step(terminal, wait, 591).action is Action.WAIT
    assert step(terminal, wait, 596).action is Action.ACCEPT


def test_permission_on_inactive_edge_executes_allow_without_repair_child(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from test_cdpa_worker import _prepare_sent_waiting_task
    from playwright_auto.role_runtime import RoleController
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="approval-edge")
    controller = RoleController()
    controller.decide(snapshot(stop_visible=True), hop["wait"])
    controller.decide(snapshot(mcp_permission_node_count=1), hop["wait"])
    hop["wait"]["inactive_since"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    hop["wait"]["controller_progress_at"] = hop["wait"]["inactive_since"]
    current = snapshot(mcp_permission_node_count=1)
    original_receipt = dict(hop["receipt"])
    class Client:
        allows = 0
        async def assert_ownership(self):
            return current
        async def auto_allow_mcp_permission(self, *, passive_action=None):
            self.allows += 1
            return {"dispatched": True}
    client = Client()
    before = len(state["hops"])
    result = asyncio.run(controller.run(worker, state, hop,
                        SimpleNamespace(client=client, page_id="page-1"), None,
                        snapshot=current, wait_ms=0))
    assert result.action is Action.ALLOW
    assert client.allows == 1
    assert len(state["hops"]) == before
    assert hop["receipt"] == original_receipt


def test_first_bound_content_in_final_ownership_recheck_cancels_stop(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from test_cdpa_worker import _prepare_sent_waiting_task
    from playwright_auto.chatgpt import MessageSnapshot
    from playwright_auto.role_runtime import RoleController
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="content-before-stop")
    empty = snapshot(stop_visible=True, response_activity_text=NOTICE)
    current = snapshot(stop_visible=True, response_activity_text=NOTICE, messages=(
        MessageSnapshot("user", "u1", "t1", hop["prompt"], ()),
        MessageSnapshot("assistant", "answer", "answer-turn", "{partial", ()),
    ))
    hop["wait"]["controller_progress_at"] = (datetime.now(timezone.utc) - timedelta(seconds=310)).isoformat()
    class Client:
        stops = 0
        async def assert_ownership(self):
            return current
        async def stop(self, *, preserve_manual_input):
            self.stops += 1
    client = Client()
    before = len(state["hops"])
    result = asyncio.run(RoleController().run(worker, state, hop,
                        SimpleNamespace(client=client, page_id="page-1"), None,
                        snapshot=empty, wait_ms=0))
    assert result.action is Action.WAIT
    assert result.reason == "malformed_result_stability"
    assert client.stops == 0
    assert len(state["hops"]) == before
    assert hop["wait"]["bound_assistant_progress"]["text"] == "{partial"


def test_interruption_status_reaches_full_and_sparse_owned_page_readers():
    from playwright_auto.chatgpt import inspect_chatgpt_page, inspect_chatgpt_wait_probe
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert NOTICE in full.response_activity_text
        assert NOTICE in probe.response_activity_tail
    asyncio.run(_with_page(f'<main><div role="status">{NOTICE}</div><button aria-label="Stop"></button><div contenteditable="true" role="textbox"></div></main>', run))


def test_conversation_load_error_without_alert_is_observed_and_blocks():
    from playwright_auto.chatgpt import inspect_chatgpt_page
    async def run(page):
        full = await inspect_chatgpt_page(page)
        assert "Could not load this ChatGPT conversation" in full.error_texts
        assert step(full, {}).reason == "conversation_load_grace"
    asyncio.run(_with_page('<main><h2>Could not load this ChatGPT conversation</h2></main>', run))


def test_conversation_load_error_is_not_interrupted_stream_or_continuation():
    current = snapshot(composer_present=False, composer_editable=False,
                       error_texts=("Could not load this ChatGPT conversation",))
    wait = {}
    assert step(current, wait).reason == "conversation_load_grace"
    assert step(current, wait, 599).action is Action.WAIT
    decision = step(current, dict(wait), 600)
    assert decision.action is Action.REFRESH
    assert decision.reason == "conversation_load_recheck"
    wait["ui_error_refreshed_key"] = "conversation_load_recheck"
    assert step(current, wait, 601).action is Action.BLOCK
    assert step(current, dict(wait), 1200).reason == "conversation_unavailable"
    assert step(snapshot(composer_present=False, composer_editable=False,
                         url="https://chatgpt.com/?temporary-chat=true", error_texts=current.error_texts), {}, 600).reason == "temporary_conversation_unavailable"


def test_load_error_reload_claim_survives_crash_and_never_sends(tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from test_cdpa_worker import _prepare_sent_waiting_task
    from playwright_auto.role_runtime import RoleController
    _, state, worker, path, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="load-error-once")
    current = snapshot(composer_present=False, composer_editable=False,
                       error_texts=("Could not load this ChatGPT conversation",))
    hop["wait"]["ui_error_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    worker.store.save(path, state)
    class Client:
        refreshes = 0
        async def assert_ownership(self):
            return current
        async def refresh(self, *, reason):
            self.refreshes += 1
            saved = worker.store.load(path)
            saved_wait = next(h for h in saved["hops"] if h["hop_id"] == hop["hop_id"])["wait"]
            assert saved_wait["refresh_count"] == 1
            assert saved_wait["ui_error_refreshed_key"] == reason
            raise TimeoutError("crash after reload boundary")
    client = Client()
    acquired = SimpleNamespace(client=client, page_id="page-1")
    controller = RoleController()
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    saved = worker.store.load(path)
    saved_hop = next(h for h in saved["hops"] if h["hop_id"] == hop["hop_id"])
    result = asyncio.run(controller.run(worker, saved, saved_hop, acquired, None, snapshot=current, wait_ms=0))
    assert result.action is Action.BLOCK
    assert client.refreshes == 1
    assert len(saved["hops"]) == 1
    assert saved_hop["receipt"] == hop["receipt"]


def test_automatic_reopen_is_background_by_default():
    assert inspect.signature(CDPATabActions.reopen).parameters["foreground"].default is False


@pytest.mark.parametrize("initial", ["Medium", "Extra High"])
def test_extra_high_selection_is_verified_and_already_correct_does_not_click(initial):
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=1000)
        client.binding = PageBinding("page-1", "DEV")
        await client.ensure_extra_high()
        assert await page.get_by_role("button", name="Select ChatGPT model", exact=True).inner_text() == "Extra High"
        assert await page.evaluate("window.clicks || 0") == int(initial != "Extra High")
    body = f'''<main><div contenteditable="true" role="textbox"></div></main>
    <button aria-label="Select ChatGPT model" onclick="window.clicks=(window.clicks||0)+1;document.querySelector('[role=menu]').hidden=false">{initial}</button>
    <div role="menu" hidden><button role="menuitemradio" onclick="document.querySelector('button[aria-label]').textContent='Extra High';this.parentElement.hidden=true">Extra High</button></div>'''
    asyncio.run(_with_page(body, run))


def test_repair_child_is_idempotent_and_retains_accepted_parent(tmp_path):
    import copy
    from test_cdpa_worker import _prepare_sent_waiting_task
    from playwright_auto.cdpa_worker import _active_hop
    from playwright_auto.cdpa_routes import RouteContractError
    from playwright_auto.durable import RequestLedger, RequestStatus
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="interruption-child")
    original = copy.deepcopy(hop["receipt"])
    error = RouteContractError("Continue from accepted request without replay")
    worker._queue_format_repair(state, hop, None, error)
    child = _active_hop(state)
    worker._queue_format_repair(state, hop, None, error)
    assert _active_hop(state)["hop_id"] == child["hop_id"]
    assert len([h for h in state["hops"] if h["parent_hop_id"] == hop["hop_id"]]) == 1
    assert child["request_id"] != hop["request_id"]
    assert hop["receipt"] == original
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT


@pytest.mark.parametrize("draft", [False, True])
def test_controller_stops_then_verifies_and_queues_one_child(tmp_path, draft):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from test_cdpa_worker import _prepare_sent_waiting_task
    from playwright_auto.cdpa_worker import _active_hop
    from playwright_auto.role_runtime import RoleController
    from dataclasses import replace
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(tmp_path, task_id="stop-verify")
    current = snapshot(stop_visible=True, response_activity_text=NOTICE,
                       composer_text="operator draft" if draft else "")
    # Use the fixture's accepted user so the full ownership read can bind correctly.
    current = replace(current, messages=())
    hop["wait"]["controller_progress_at"] = (datetime.now(timezone.utc) - timedelta(seconds=310)).isoformat()
    class Client:
        stops = 0
        async def assert_ownership(self):
            return current
        async def stop(self, *, preserve_manual_input):
            assert preserve_manual_input
            self.stops += 1
    client = Client()
    acquired = SimpleNamespace(client=client, page_id="page-1")
    controller = RoleController()
    before = len(state["hops"])
    result = asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    assert len(state["hops"]) == before
    assert client.stops == int(not draft)
    if draft:
        assert result.reason == "manual_draft_preserved"
        return
    current = replace(current, stop_visible=False)
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    assert _active_hop(state)["kind"] == "route_repair"
    assert len(state["hops"]) == before + 1
    assert client.stops == 1


def test_atomic_send_refuses_model_downgrade_before_dispatch(monkeypatch):
    import playwright_auto.chatgpt as module
    async def no_delay(*args, **kwargs):
        pass
    monkeypatch.setattr(module, "action_delay", no_delay)
    async def run(page):
        with pytest.raises(UnsafePageStateError, match="model"):
            await module.click_send_button(page, expected_prompt="prompt", require_extra_high=True)
        assert await page.evaluate("window.sent || 0") == 0
    asyncio.run(_with_page('''<button aria-label="Select ChatGPT model">Medium</button>
        <main><form><div contenteditable="true" role="textbox">prompt</div>
        <button aria-label="Send" onclick="window.sent=1">Send</button></form></main>''', run))


@pytest.mark.parametrize("active,draft", [(True, ""), (False, "operator draft")])
def test_model_selection_never_mutates_streaming_or_manual_draft(active, draft):
    from playwright_auto.chatgpt import ComposerConflictError
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=1000)
        client.binding = PageBinding("page-1", "DEV")
        with pytest.raises((UnsafePageStateError, ComposerConflictError)):
            await client.ensure_extra_high()
        assert await page.evaluate("window.clicks || 0") == 0
    body = f'''<button aria-label="Select ChatGPT model" onclick="window.clicks=1">Medium</button>
        <main>{'<button aria-label="Stop" style="width:20px;height:20px"></button>' if active else ''}
        <div contenteditable="true" role="textbox">{draft}</div></main>'''
    asyncio.run(_with_page(body, run))


def test_missing_extra_high_fails_closed_without_send():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=100)
        client.binding = PageBinding("page-1", "DEV")
        with pytest.raises(UnsafePageStateError, match="Extra High"):
            await client.ensure_extra_high()
    asyncio.run(_with_page('<main><div contenteditable="true" role="textbox"></div></main>', run))
