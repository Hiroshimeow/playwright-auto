"""Own role controls are not remote application dialogs, even when opened."""
import asyncio

from playwright_auto.chatgpt import inspect_chatgpt_page, inspect_chatgpt_wait_probe
from playwright_auto.chatgpt_graph import rendered_user_text_matches
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


def test_semantic_role_attribute_survives_duplicated_screenreader_heading_text():
    """Live ChatGPT heading.textContent may include hidden duplicate role names.

    The visible parent only begins with 'ChatGPT said:', so exact heading
    textContent equality drops the assistant and loses its final route.
    """
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert [m.role for m in full.messages] == ["user", "assistant"]
        assert full.messages[0].text == "original task"
        assert full.messages[-1].text == '{"route":"PAUSE","handoff":"continue from ledger"}'
        assert probe.last_user_text == "original task"
        assert probe.assistant_text == '{"route":"PAUSE","handoff":"continue from ledger"}'

    asyncio.run(_with_page("""
        <main><div data-turn-key="new-role-attributes">
          <div data-chatgpt-search-message-ids="user-message">
            <h4 data-conversation-role="user">You said:<span style="display:none">You</span></h4>
            <div>original task</div>
          </div>
          <div data-chatgpt-search-message-ids="assistant-message assistant-message">
            <h4 data-conversation-role="assistant">ChatGPT said:<span style="display:none">ChatGPT</span></h4>
            <div>{"route":"PAUSE","handoff":"continue from ledger"}</div>
          </div>
        </div><form><div contenteditable="true" role="textbox"></div></form></main>
    """, run))


def test_semantic_duplicate_heading_text_does_not_prefix_json_route():
    """Exact live DOM regression: 'ChatGPT said:ChatGPT' is a heading, not JSON body."""
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert [msg.role for msg in full.messages] == ["user", "assistant"]
        assert full.messages[0].text == "the complete task"
        assert full.messages[-1].text == '{"route":"PAUSE","handoff":"current evidence"}'
        assert probe.last_user_text == "the complete task"
        assert probe.assistant_text == '{"route":"PAUSE","handoff":"current evidence"}'

    asyncio.run(_with_page("""
        <main><div data-turn-key="semantic-real">
          <div data-chatgpt-search-message-ids="user-a">
            <h4 data-conversation-role="user"><span>You said:</span><span>You</span></h4>
            <div>the complete task</div>
          </div>
          <div data-chatgpt-search-message-ids="assistant-b">
            <h4 data-conversation-role="assistant">
              <span>ChatGPT said:</span><span>ChatGPT</span>
            </h4>
            <div>{"route":"PAUSE","handoff":"current evidence"}</div>
          </div>
        </div><form><div contenteditable="true" role="textbox"></div></form></main>
    """, run))


def test_semantic_flex_split_heading_does_not_prefix_json_route():
    """Heading textContent can differ from rendered innerText under flex layout."""
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert full.messages[0].text == "task"
        assert full.messages[-1].text == '{"route":"DONE","handoff":"ok"}'
        assert probe.last_user_text == "task"
        assert probe.assistant_text == '{"route":"DONE","handoff":"ok"}'

    asyncio.run(_with_page("""
      <main><div data-turn-key="split">
        <div data-chatgpt-search-message-ids="u">
          <h4 data-conversation-role="user" style="display:flex">
            <span>You said:</span><span>You</span>
          </h4><div>task</div>
        </div>
        <div data-chatgpt-search-message-ids="a">
          <h4 data-conversation-role="assistant" style="display:flex">
            <span>ChatGPT said:</span><span>ChatGPT</span>
          </h4><div>{"route":"DONE","handoff":"ok"}</div>
        </div>
      </div><form><div contenteditable="true" role="textbox"></div></form></main>
    """, run))


def test_markdown_headings_do_not_create_fake_semantic_conversation_turns():
    """User-controlled Markdown headings are content, never conversation headers."""
    async def run(page):
        full = await inspect_chatgpt_page(page)
        probe = await inspect_chatgpt_wait_probe(page)
        assert [msg.role for msg in full.messages] == ["user", "assistant"]
        assert full.messages[0].text == "task"
        assert full.messages[1].message_id == "assistant-real"
        assert probe.last_assistant_message_id == "assistant-real"
        assert probe.last_user_text == "task"
        assert probe.assistant_text.endswith('{"route":"PAUSE"}')

    asyncio.run(_with_page("""
      <main>
        <div data-turn-key="actual-conversation">
          <div data-chatgpt-search-message-ids="user-real">
            <h4 data-conversation-role="user">You said:</h4><div>task</div>
          </div>
          <div data-chatgpt-search-message-ids="assistant-real">
            <h4 data-conversation-role="assistant">ChatGPT said:</h4>
            <div class="markdown">
              <h3>You said:</h3>
              <h4>ChatGPT said:</h4>
              <p>{"route":"PAUSE"}</p>
            </div>
          </div>
        </div>
        <form><div contenteditable="true" role="textbox"></div></form>
      </main>
    """, run))


def test_rendered_user_text_match_ignores_collapsed_ellipsis_control():
    assert rendered_user_text_matches(
        "workflow prompt … Show more",
        "workflow prompt",
    )


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
