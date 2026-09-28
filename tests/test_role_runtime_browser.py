"""Browser integration at the actual adapter/controller boundaries, not synthetic counters."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from playwright_auto.chatgpt import (
    ChatGPTPage,
    MessageBaseline,
    PageBinding,
    SendReceipt,
    inspect_chatgpt_page,
    inspect_chatgpt_wait_probe,
    prompt_digest,
)
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


_MODERN_TURN_KEY = "cc4279e0-b3e3-4145-8001-c3ae0449c0d6"
_MODERN_ASSISTANT_ID = "aa95fd61-2031-4502-86f4-b1c149bb9cd1"


def modern_semantic_dom():
    return f"""
        <main>
          <section data-turn-key="{_MODERN_TURN_KEY}">
            <div data-chatgpt-search-unit-key="fallback-turn-0:0:user"
                 data-chatgpt-search-message-ids="{_MODERN_TURN_KEY}"></div>
            <article><h4>You said:</h4><div>original task</div></article>
            <article data-chatgpt-search-unit-key="fallback-turn-0:2:assistant"
                     data-chatgpt-search-message-ids="{_MODERN_ASSISTANT_ID} {_MODERN_ASSISTANT_ID}">
              <h4>ChatGPT said:</h4><div>{{\"route\":\"DONE\",\"handoff\":\"complete\"}}</div>
            </article>
            <div class="turn-action-controls">
              <button aria-label="Copy" style="width:20px;height:20px"></button>
              <button aria-label="Rate response" style="width:20px;height:20px"></button>
              <button aria-label="Regenerate response" style="width:20px;height:20px"></button>
              <button aria-label="Read aloud" style="width:20px;height:20px"></button>
              <button aria-label="Arbitrary future action" style="width:20px;height:20px"></button>
            </div>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """


def semantic_request():
    return SendReceipt(
        prompt="original task",
        prompt_sha256=prompt_digest("original task"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="exact_user_message",
        session_id_before=None,
        user_message_id=_MODERN_TURN_KEY,
        conversation_id="test",
    )


def test_modern_semantic_dom_preserves_exact_identity_completion_actions_and_sparse_probe():
    async def run(page):
        current = await inspect_chatgpt_page(page)
        assert [(item.role, item.message_id, item.turn_id) for item in current.messages] == [
            ("user", _MODERN_TURN_KEY, f"group:user:{_MODERN_TURN_KEY}"),
            ("assistant", _MODERN_ASSISTANT_ID, f"group:assistant:{_MODERN_TURN_KEY}"),
        ]
        assistant = current.messages[-1]
        assert set(assistant.actions) == {"copy", "feedback", "regenerate"}

        probe = await inspect_chatgpt_wait_probe(page)
        assert probe.last_user_message_id == _MODERN_TURN_KEY
        assert probe.last_user_turn_id == f"group:user:{_MODERN_TURN_KEY}"
        assert probe.last_assistant_message_id == _MODERN_ASSISTANT_ID
        assert probe.last_assistant_turn_id == f"group:assistant:{_MODERN_TURN_KEY}"

    asyncio.run(_with_page(modern_semantic_dom(), run))


def test_modern_semantic_bound_dom_terminalizes_despite_stale_listen_streaming():
    async def run(page):
        current = await inspect_chatgpt_page(page)
        wait = {}
        controller = RoleController()
        first = controller.decide(
            current,
            wait,
            receipt=semantic_request(),
            network_status="IS_STREAMING",
            now=datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        assert first.reason == "result_stability"
        result = controller.decide(
            current,
            wait,
            receipt=semantic_request(),
            network_status="IS_STREAMING",
            now=datetime(2026, 9, 28, 0, 0, 5, tzinfo=timezone.utc),
        )
        assert result.action is Action.ACCEPT
        assert result.reason == "valid_bound_result"
        assert result.response.message_id == _MODERN_ASSISTANT_ID
        assert result.response.turn_id == f"group:assistant:{_MODERN_TURN_KEY}"

    asyncio.run(_with_page(modern_semantic_dom(), run))


def test_listen_telemetry_emits_once_per_semantic_signal(monkeypatch):
    import playwright_auto.role_runtime.signals as signal_module

    events = []
    monkeypatch.setattr(
        signal_module,
        "append_live_event",
        lambda source, kind, **kwargs: events.append((source, kind, kwargs)),
    )

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
        MessageSnapshot("user", "user-original", "turn-original", "original task", ()),
        MessageSnapshot(
            "assistant", "bad", "turn-bad", "not JSON",
            ("copy-turn-action-button",),
        ),
    ))
    controller = RoleController()
    asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    hop["wait"]["result_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    result = asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    assert result.action is Action.REPAIR
    assert state["repair"] == "invalid route: missing handoff"
    assert hop["receipt"] == request().to_dict()


def test_no_terminal_result_timeout_queues_route_repair_without_replaying_send():
    class Client:
        async def install_page_observer(self):
            return None

        def page_observation(self):
            return {}

    state, hop, worker, acquired = environment(Client())
    queued = []
    worker._queue_format_repair = lambda *_args: queued.append(_args)
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
    assert len(queued) == 1
    assert queued[0][2] is None
    assert "terminal result" in str(queued[0][3]).lower()
    assert "block_code" not in state
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


def test_no_result_timeout_queues_route_repair_without_retry_click_or_replay(monkeypatch):
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
        assert "block_code" not in state
        assert "terminal result" in state["repair"].lower()
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


def test_page_observer_only_wakes_on_dom_changes_without_semantic_transcript_logging(monkeypatch):
    import playwright_auto.role_runtime.signals as signal_module

    events = []
    monkeypatch.setattr(
        signal_module,
        "append_live_event",
        lambda source, kind, **kwargs: events.append((source, kind, kwargs)),
    )

    async def run(page):
        async def serve(route):
            await route.fulfill(status=200, content_type="text/html", body="""
                <main>
                  <section data-turn-id="turn-1">
                    <div data-message-author-role="assistant" data-message-id="msg-1">
                      <span id="assistant-text">A</span>
                    </div>
                  </section>
                  <div contenteditable="true" role="textbox"></div>
                </main>
            """)

        await page.context.route("https://chatgpt.com/**", serve)
        await page.goto("https://chatgpt.com/c/live-transcript")
        client = ChatGPTPage(page, timeout_ms=5_000)
        await client.install_page_observer()

        await page.evaluate("document.querySelector('#assistant-text').firstChild.data = 'AB'")
        assert await client.wait_for_page_observation(timeout_ms=500) is True
        assert not [event for event in events if event[0] == "TRANSCRIPT"]

    asyncio.run(_with_page("<main></main>", run))


def test_page_observer_replaces_legacy_dom_observer_on_live_page():
    async def run(page):
        async def serve(route):
            await route.fulfill(status=200, content_type="text/html", body="""
                <main>
                  <section data-turn-id="turn-legacy">
                    <div data-message-author-role="assistant" data-message-id="msg-legacy">ready</div>
                  </section>
                  <div contenteditable="true" role="textbox"></div>
                </main>
            """)

        await page.context.route("https://chatgpt.com/**", serve)
        await page.goto("https://chatgpt.com/c/live-legacy")
        await page.evaluate("""() => {
            sessionStorage.setItem('playwright-auto:role', 'DEV');
            sessionStorage.setItem('playwright-auto:page-id', 'page-1');
            sessionStorage.setItem('playwright-auto:task-id', 'task-1');
            sessionStorage.setItem('playwright-auto:team', 'team-1');
            window.__CDPA_ROLE_DOM_V1__ = {disconnect: () => { window.__legacyDisconnected = true; }};
        }""")
        client = ChatGPTPage(page, timeout_ms=5_000)
        client.binding = PageBinding("page-1", "DEV")
        await client.install_page_observer()
        assert await page.evaluate("Boolean(window.__legacyDisconnected)") is True
        assert await page.evaluate("Boolean(window.__CDPA_ROLE_DOM_V2__)") is True

    asyncio.run(_with_page("<main></main>", run))


def test_current_response_projection_preserves_order_and_stable_segment_keys():
    async def run(page):
        first = await inspect_chatgpt_wait_probe(page)
        assert [(item.kind, item.text) for item in first.response_segments] == [
            ("status", "Thinking"),
            ("answer", "answer one"),
            ("status", "Searching"),
            ("answer", "answer two"),
        ]
        assert first.response_projection_active is True
        assert first.last_assistant_message_id is None
        keys = [item.key for item in first.response_segments]

        await page.evaluate("""() => {
            const firstAnswer = document.querySelector('.markdown');
            firstAnswer.className = 'markdown prose churned';
            firstAnswer.textContent = 'answer one updated';
        }""")
        second = await inspect_chatgpt_wait_probe(page, previous_probe=first)
        assert [item.key for item in second.response_segments] == keys
        assert second.response_segments[1].text == "answer one updated"

    asyncio.run(_with_page("""
        <main>
          <section data-turn-id="turn-current">
            <div data-message-author-role="user" data-message-id="user-original">original task</div>
            <div data-testid="status-running">Thinking</div>
            <div class="markdown">answer one</div>
            <div data-testid="status-searching">Searching</div>
            <div class="markdown">answer two</div>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_completed_projection_activity_does_not_veto_but_unresolved_activity_does():
    async def run(page):
        completed = await inspect_chatgpt_wait_probe(page)
        assert [(item.kind, item.active) for item in completed.response_segments] == [
            ("status", False),
            ("tool", False),
            ("answer", False),
        ]
        assert completed.response_projection_active is False

        await page.evaluate("""() => {
            document.querySelector('[data-turn-id="turn-current"]').insertAdjacentHTML(
                'beforeend',
                '<div data-testid="tool-calling" data-state="open">Calling tool</div>'
            );
        }""")
        unresolved = await inspect_chatgpt_wait_probe(page, previous_probe=completed)
        assert unresolved.response_projection_active is True
        assert unresolved.response_segments[-1].kind == "tool"
        assert unresolved.response_segments[-1].active is True

    asyncio.run(_with_page("""
        <main>
          <section data-turn-id="turn-current">
            <div data-message-author-role="user" data-message-id="user-original">original task</div>
            <div data-testid="status-running" data-streaming-response-status="complete">Finished</div>
            <div data-testid="tool-calling" data-state="closed">Tool result</div>
            <div class="markdown">answer</div>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_text_only_retry_ignores_historical_response_before_latest_user():
    async def run(page):
        probe = await inspect_chatgpt_wait_probe(page)
        assert probe.retry_visible is False

    asyncio.run(_with_page("""
        <main>
          <section data-turn-id="turn-old">
            <div data-message-author-role="user" data-message-id="user-old">old task</div>
            <div data-message-author-role="assistant" data-message-id="assistant-old">failed</div>
            <button>Retry</button>
          </section>
          <section data-turn-id="turn-current">
            <div data-message-author-role="user" data-message-id="user-original">current task</div>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_exact_testid_retry_is_scoped_to_current_response():
    async def run(page):
        historical = await inspect_chatgpt_wait_probe(page)
        assert historical.retry_visible is False

        await page.evaluate("""() => {
            document.querySelector('[data-turn-id="turn-current"]').insertAdjacentHTML(
                'beforeend',
                '<div data-message-author-role="assistant" data-message-id="assistant-current">failed</div>' +
                '<button data-testid="regenerate-thread-error-button">Retry</button>'
            );
        }""")
        current = await inspect_chatgpt_wait_probe(page, previous_probe=historical)
        assert current.retry_visible is True

    asyncio.run(_with_page("""
        <main>
          <section data-turn-id="turn-old">
            <div data-message-author-role="user" data-message-id="user-old">old task</div>
            <div data-message-author-role="assistant" data-message-id="assistant-old">failed</div>
            <button data-testid="regenerate-thread-error-button">Retry</button>
          </section>
          <section data-turn-id="turn-current">
            <div data-message-author-role="user" data-message-id="user-original">current task</div>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_text_only_retry_is_scoped_to_current_response():
    async def run(page):
        scoped = await inspect_chatgpt_wait_probe(page)
        assert scoped.retry_visible is True
        await page.evaluate("""() => {
            const button = document.querySelector('[data-turn-id] button');
            document.querySelector('main').appendChild(button);
        }""")
        unrelated = await inspect_chatgpt_wait_probe(page, previous_probe=scoped)
        assert unrelated.retry_visible is False

    asyncio.run(_with_page("""
        <main>
          <section data-turn-id="turn-current">
            <div data-message-author-role="user" data-message-id="user-original">original task</div>
            <div data-message-author-role="assistant" data-message-id="assistant-current">failed</div>
            <button>Retry</button>
          </section>
          <div contenteditable="true" role="textbox"></div>
        </main>
    """, run))
