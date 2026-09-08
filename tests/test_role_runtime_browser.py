"""Browser integration at the actual adapter/controller boundaries, not synthetic counters."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from playwright_auto.chatgpt import ChatGPTPage, PageBinding, SendReceipt, MessageBaseline, prompt_digest
from playwright_auto.role_runtime import Action, RoleController
from test_chatgpt_mcp_permission import _with_page


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
        _dom_only_enabled=lambda: True,
        _validate_response_candidate=lambda *_: None,
        _queue_format_repair=lambda _state, _hop, _response, error: _state.update(repair=str(error)),
    )
    acquired = SimpleNamespace(client=client, page_id="page-1")
    return state, hop, worker, acquired


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
        assert result.reason == "mcp_allow_stable"
        assert await page.evaluate("window.approved || null") is None
        hop["wait"]["mcp_allow_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
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


def test_retry_repair_goes_through_binding_and_real_send_without_retry_click(monkeypatch):
    import playwright_auto.chatgpt as module
    async def no_delay(*args, **kwargs):
        return 0
    monkeypatch.setattr(module, "action_delay", no_delay)
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        client.binding = PageBinding("page-1", "DEV")
        state, hop, worker, acquired = environment(client)
        controller = RoleController()
        result = await controller.run(worker, state, hop, acquired, None, wait_ms=0)
        assert result.action is Action.REPAIR
        assert "Retry UI" in state["repair"]
        # Exercise exactly the pre-send binding path that blocked the production repair.
        await client.prepare_task("task-1")
        await client.bind_task_identity("task-1", "team-1")
        result = await controller.run(worker, state, hop, acquired, None, phase="pre_send", wait_ms=0)
        assert result.reason == "ready"
        guide = "Continue from this state. Do not repeat prior actions. Return the required route JSON."
        receipt = await client.send(guide, wait_for_stop=False, max_attempts=1, recovery_reload=False,
                                    expected_task_id="task-1", expected_team="team-1")
        assert receipt.user_message_id == "repair-user"
        assert await page.evaluate("window.retryClicks || 0") == 0
        assert await page.evaluate("window.sentPrompts") == [guide]
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
          ">Send</button>
        </form></main>
    """, run))
