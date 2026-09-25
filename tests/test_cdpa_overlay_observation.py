"""Own role controls are not remote application dialogs, even when opened."""
import asyncio

from playwright_auto.chatgpt import inspect_chatgpt_page, inspect_chatgpt_wait_probe
from test_chatgpt_mcp_permission import _with_page


def test_role_control_overlay_is_not_a_chatgpt_blocking_dialog():
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert full.blocking_dialogs == ()
        assert probe.blocking_dialogs == ()
        await page.evaluate("document.querySelector('#real-dialog').hidden = false")
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert full.blocking_dialogs == ("Real confirmation required",)
        assert probe.blocking_dialogs == ("Real confirmation required",)
    asyncio.run(_with_page('''
        <main><div contenteditable="true" role="textbox"></div></main>
        <div id="playwright-auto-role-control-v1" role="dialog">Tab role Apply Release</div>
        <div id="real-dialog" role="dialog" hidden>Real confirmation required</div>
    ''', run))


def test_semantic_turn_dom_without_legacy_message_attributes_is_observable():
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)

        assert [(message.role, message.message_id, message.text) for message in full.messages] == [
            ("user", "DOM:user:0", "workflow prompt"),
            ("assistant", "DOM:assistant:0", '{"route":"DEV"}'),
        ]
        assert full.response_activity_text == '{"route":"DEV"}'
        assert full.response_activity_turn_id is None
        assert probe.last_user_message_id == "DOM:user:0"
        assert probe.last_user_text == "workflow prompt"
        assert probe.last_assistant_message_id == "DOM:assistant:0"
        assert probe.assistant_text == '{"route":"DEV"}'
        assert probe.response_activity_text == '{"route":"DEV"}'

    asyncio.run(_with_page('''
        <main>
          <div class="turn-shell">
            <div class="message-block">
              <h4 class="sr-only">You said:</h4>
              <div class="group/user-message">workflow prompt</div>
            </div>
            <div class="message-block">
              <h4 class="sr-only">ChatGPT said:</h4>
              <div><p>{"route":"DEV"}</p></div>
            </div>
          </div>
          <form><div contenteditable="true" role="textbox"></div></form>
        </main>
    ''', run))


def test_wait_probe_reinstalls_current_reader_over_stale_page_function():
    async def run(page):
        await page.evaluate("""() => {
            window.__PLAYWRIGHT_AUTO_WAIT_PROBE__ = () => ({
                url: location.href,
                last_user_message_id: null,
                last_assistant_message_id: null,
                assistant_text: '',
                response_activity_text: '',
            });
        }""")
        probe = await inspect_chatgpt_wait_probe(page)
        assert probe.last_user_message_id == "DOM:user:0"
        assert probe.last_assistant_message_id == "DOM:assistant:0"
        assert probe.assistant_text == '{"route":"DEV"}'

    asyncio.run(_with_page('''
        <main>
          <div class="message-block">
            <h4 class="sr-only">You said:</h4><div>workflow prompt</div>
          </div>
          <div class="message-block">
            <h4 class="sr-only">ChatGPT said:</h4><div>{"route":"DEV"}</div>
          </div>
          <form><div contenteditable="true" role="textbox"></div></form>
        </main>
    ''', run))
