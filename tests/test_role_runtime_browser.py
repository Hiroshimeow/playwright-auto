"""Browser integration at the actual adapter/controller boundaries, not synthetic counters."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from playwright_auto.chatgpt import ChatGPTPage, PageBinding, SendReceipt, MessageBaseline, prompt_digest
from playwright_auto.role_runtime import Action, Decision, RoleController
from test_chatgpt_mcp_permission import _with_page
from test_operational_contract import snapshot


def request():
    return SendReceipt(
        prompt="original task", prompt_sha256=prompt_digest("original task"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1, accepted_via="user_message_identity", session_id_before=None,
        user_message_id="user-original", conversation_id="test",
    )


def environment(client):
    state = {"task_id": "task-1", "team": "team-1", "status": "RUNNING", "active_action": "wait_response"}
    hop = {"receipt": request().to_dict(), "request_id": "task-hop1", "physical_role": "DEV", "target_role": "DEV", "wait": {}}
    worker = SimpleNamespace(
        _validate_response_candidate=lambda *_: None,
        _queue_format_repair=lambda _state, _hop, _response, error: _state.update(repair=str(error)),
        _capture_passive_remote_report_if_available=lambda *_: None,
        _record_response=lambda _state, _hop, response: _hop.update(response=response.text, state="responded"),
        _release_stream_status_slot=lambda *_: None,
    )
    acquired = SimpleNamespace(client=client, page_id="page-1")
    return state, hop, worker, acquired


def test_listen_telemetry_emits_once_per_semantic_signal(monkeypatch):
    import playwright_auto.observability as observability

    events = []
    monkeypatch.setattr(observability, "append_live_event", lambda source, kind, **kwargs: events.append((source, kind, kwargs)))

    class Client:
        evidence = {
            "status": "IS_STREAMING",
            "response": {"role": "assistant", "message_id": "net-1", "turn_id": "turn-1", "text": "working", "image_count": 0},
        }

        async def install_page_observer(self):
            return None

        def page_observation(self):
            return dict(self.evidence)

    client = Client()
    state, hop, worker, acquired = environment(client)
    controller = RoleController()
    current = snapshot()

    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    assert [event[:2] for event in events].count(("LISTEN", "network_signal")) == 1

    client.evidence["status"] = "COMPLETE"
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    listen = [event for event in events if event[:2] == ("LISTEN", "network_signal")]
    assert len(listen) == 2
    assert listen[-1][2]["values"]["status"] == "COMPLETE"


def test_malformed_result_queues_exact_parser_error_with_original_receipt_intact():
    from playwright_auto.chatgpt import MessageSnapshot

    class Client:
        async def install_page_observer(self):
            return None

        def page_observation(self):
            return {}

    state, hop, worker, acquired = environment(Client())
    worker._validate_response_candidate = lambda *_: (_ for _ in ()).throw(ValueError("invalid route: missing handoff"))
    current = snapshot(messages=(
        MessageSnapshot("user", "operator", "turn-operator", "continue", ()),
        MessageSnapshot("assistant", "bad", "turn-bad", "not JSON", ()),
    ))
    controller = RoleController()
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    hop["wait"]["result_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    result = asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    assert result.action is Action.REPAIR
    assert state["repair"] == "invalid route: missing handoff"
    assert hop["receipt"] == request().to_dict()


def test_no_terminal_result_timeout_does_not_queue_format_route_repair():
    class Client:
        async def install_page_observer(self):
            return None

        def page_observation(self):
            return {}

    state, hop, worker, acquired = environment(Client())
    queued = []
    worker._queue_format_repair = lambda *_: queued.append(True)
    worker._block = lambda _state, reason, **kwargs: _state.update(block_reason=reason, block_code=kwargs.get("code"))
    hop["wait"].update(
        controller_progress_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(),
        timeout_refreshed=True,
        timeout_status_checked=True,
        timeout_status="COMPLETE",
    )
    result = asyncio.run(RoleController().run(
        worker, state, hop, acquired, None, snapshot=snapshot(error_texts=("provider failed",)), wait_ms=0,
    ))
    assert result.reason == "state_repair"
    assert queued == []
    assert state["block_code"] == "response_without_terminal_result"
    assert hop["receipt"] == request().to_dict()


def test_genuine_retry_and_stream_failure_remain_retryable_without_format_repair():
    class Client:
        async def install_page_observer(self):
            return None

        def page_observation(self):
            return {}

    for reason, evidence in (
        ("retry_visible_stable", {"retry_visible": True}),
        ("stream_failure_recovery", {}),
    ):
        state, hop, worker, acquired = environment(Client())
        queued = []
        worker._queue_format_repair = lambda *_: queued.append(True)
        worker._block = lambda _state, message, **kwargs: _state.update(
            block_reason=message, block_code=kwargs.get("code"), retryable=kwargs.get("retryable")
        )
        if reason == "retry_visible_stable":
            hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
        else:
            hop["wait"].update(
                controller_progress_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(),
                timeout_refreshed=True, timeout_status_checked=True, timeout_status="FAILURE",
            )
        result = asyncio.run(RoleController().run(
            worker, state, hop, acquired, None, snapshot=snapshot(**evidence), wait_ms=0,
        ))
        assert result.reason == reason
        assert state["block_code"] == reason
        assert state["retryable"] is True
        assert queued == []
        assert hop["receipt"] == request().to_dict()


def test_hidden_allow_real_dom_snapshot_reaches_dispatch_after_five_seconds():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        client.binding = PageBinding("page-1", "DEV")
        await page.evaluate("""() => {
            const button = document.querySelector('button[aria-label]');
            button.__reactProps$test = {
                onSelectOption: (_event, action) => {window.approved = action;},
                splitActionOptions: [{action:{type:'allow',target_message_id:'hidden',remember_answer:true}}],
            };
        }""")
        state, hop, worker, acquired = environment(client)
        controller = RoleController()
        result = await controller.run(worker, state, hop, acquired, None, wait_ms=0)
        assert result.action is Action.ALLOW
        approved = await page.evaluate("window.approved")
        assert approved["target_message_id"] == "hidden"
        assert approved["remember_answer"] is True
        assert hop["wait"].get("mcp_allow_clicked_at")
    asyncio.run(_with_page("""
        <main><div data-message-author-role="user" data-message-id="user-original">original task</div>
        <div style="display:none"><button>Allow</button><button aria-label="Allow mcp-g8 for this conversation"></button></div>
        <div contenteditable="true" role="textbox"> </div></main>
    """, run))


def test_no_result_timeout_blocks_without_retry_click_or_new_send(monkeypatch):
    import playwright_auto.chatgpt as module
    async def no_delay(*args, **kwargs):
        return 0
    monkeypatch.setattr(module, "action_delay", no_delay)
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=7_000)
        client.binding = PageBinding("page-1", "DEV")
        state, hop, worker, acquired = environment(client)
        controller = RoleController()
        hop["wait"].update(
            controller_progress_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(),
            timeout_refreshed=True,
            timeout_status_checked=True,
            timeout_status="COMPLETE",
        )
        worker._block = lambda target, reason, **kwargs: target.update(
            block_reason=reason, block_code=kwargs.get("code")
        )
        result = await controller.run(worker, state, hop, acquired, None, wait_ms=0)
        assert result.action is Action.REPAIR
        assert state["block_code"] == "response_without_terminal_result"
        assert "repair" not in state
        assert hop["receipt"] == request().to_dict()
        assert await page.evaluate("window.retryClicks || 0") == 0
        assert await page.evaluate("window.sentPrompts || []") == []
    asyncio.run(_with_page("""
        <main>
        <div data-message-author-role="user" data-message-id="user-original">original task</div>
        <button data-testid="regenerate-thread-error-button" onclick="window.retryClicks=(window.retryClicks||0)+1">Retry</button>
        <form onsubmit="return false">
          <div id="prompt-textarea" contenteditable="true" role="textbox"></div>
          <button type="button" data-testid="send-button" aria-label="Send prompt" onclick="
            const input=document.querySelector('[contenteditable]');
            window.sentPrompts=[...(window.sentPrompts||[]),input.innerText];
            const message=document.createElement('div');
            message.dataset.messageAuthorRole='user';message.dataset.messageId='repair-user';
            message.innerText=input.innerText;document.querySelector('main').appendChild(message);input.innerText='';
            const stop=document.createElement('button');stop.dataset.testid='stop-button';stop.innerText='Stop';
            document.querySelector('main').appendChild(stop);
            const assistant=document.createElement('div');assistant.dataset.messageAuthorRole='assistant';
            assistant.dataset.messageId='repair-assistant';assistant.innerText='working';
            document.querySelector('main').appendChild(assistant);
          ">Send</button>
        </form></main>
    """, run))



def test_temporary_workflow_never_executes_automatic_refresh(monkeypatch):
    class Client:
        refreshed = False

        async def install_page_observer(self):
            return None

        def page_observation(self):
            return {}

        async def refresh(self):
            self.refreshed = True
            raise AssertionError("Temporary Chat must never auto-refresh")

    client = Client()
    state, hop, worker, acquired = environment(client)
    state["conversation_mode"] = "temporary"
    controller = RoleController()
    monkeypatch.setattr(
        controller,
        "decide",
        lambda *_args, **_kwargs: Decision(Action.REFRESH, "response_timeout_recheck"),
    )

    result = asyncio.run(
        controller.run(
            worker,
            state,
            hop,
            acquired,
            None,
            snapshot=snapshot(),
            wait_ms=0,
        )
    )

    assert result.action is Action.WAIT
    assert result.reason == "temporary_no_refresh:response_timeout_recheck"
    assert client.refreshed is False
    assert hop["wait"]["timeout_refreshed"] is True
