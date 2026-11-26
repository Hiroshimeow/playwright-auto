import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from playwright_auto.chatgpt import ChatGPTPage, MessageBaseline, SendReceipt, UnsafePageStateError
from playwright_auto.cdpa_prompts import CONVERSATION_ROUTE_REPAIR_PREFIX
from playwright_auto.role_runtime.response import completed_empty_assistant


@pytest.mark.parametrize("change", [None, "assistant", "later_user", "edit", "edited_text"])
def test_compact_blank_turn_clicks_only_its_native_retry_menu(change):
    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/snap/bin/chromium" if Path("/snap/bin/chromium").is_file() else None,
            )
            try:
                page = await browser.new_page()
                url = "https://chatgpt.com/c/retry-test?temporary-chat=true"
                await page.route(url, lambda route: route.fulfill(content_type="text/html", body="""
                    <main>
                      <div data-turn-key="accepted-user">
                        <div data-chatgpt-search-unit-key="pair:user" data-chatgpt-search-message-ids="accepted-user">
                          <h4 data-conversation-role="user">You said:</h4>Original task
                        </div>
                        <div id="assistant" data-chatgpt-search-unit-key="pair:assistant"
                             data-chatgpt-search-message-ids="answer-id answer-id">
                          <h4 data-conversation-role="assistant">ChatGPT said:</h4>
                        </div>
                        <button aria-label="Copy">Copy</button>
                        <button id="more" aria-label="More actions" aria-haspopup="menu" aria-expanded="false">More</button>
                      </div>
                      <form><div role="textbox" contenteditable="true"></div></form>
                    </main>
                    <div role="menu" id="unrelated"><div role="menuitem" id="wrong">Retry</div></div>
                    <div role="menu" id="right-menu" hidden><div role="menuitem" id="retry">Retry</div></div>
                """))
                await page.goto(url)
                client = ChatGPTPage(page)
                await client.set_role("test-dev")
                await client.bind_task_identity("task", "test-team")
                await page.evaluate("""change => {
                    window.clicks = [];
                    const more = document.getElementById('more');
                    more.onclick = () => {
                      more.setAttribute('aria-expanded', 'true');
                      more.setAttribute('aria-controls', 'right-menu');
                      document.getElementById('right-menu').hidden = false;
                      if (change === 'assistant') document.getElementById('assistant').append('Late response');
                      if (change === 'later_user') document.querySelector('main').insertAdjacentHTML('beforeend',
                        '<div data-turn-key="later"><div data-chatgpt-search-unit-key="late:user">Later input</div></div>');
                    };
                    document.getElementById('wrong').onclick = () => window.clicks.push('wrong');
                    document.getElementById('retry').onclick = () => {
                      window.clicks.push('right');
                      document.querySelector('main').insertAdjacentHTML('beforeend',
                        '<button data-testid="stop-button">Stop generating</button>');
                    };
                }""", change)
                edit = change in {"edit", "edited_text"}
                prompt = CONVERSATION_ROUTE_REPAIR_PREFIX + "Return JSON." if edit else "Original task"
                if edit:
                    await page.evaluate("""([prompt, changed]) => {
                      const group = document.querySelector('main [data-turn-key]');
                      const edit = document.createElement('button');
                      edit.setAttribute('aria-label', 'Edit message');
                      group.append(edit);
                      edit.onclick = () => {
                        const editor = document.createElement('textarea');
                        editor.value = changed ? "operator changed the text" : prompt;
                        const send = document.createElement('button');
                        send.textContent = 'Send';
                        send.onclick = () => {
                          window.clicks.push('edit-send');
                          editor.remove();
                          send.remove();
                          document.querySelector('main').insertAdjacentHTML('beforeend',
                            '<button data-testid="stop-button">Stop generating</button>');
                        };
                        group.append(editor, send);
                      };
                    }""", [prompt, change == "edited_text"])
                receipt = SendReceipt(
                    prompt=prompt, prompt_sha256="digest", binding=client.binding,
                    baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
                    attempts=1, accepted_via="exact_user_message", session_id_before=None,
                    user_message_id="accepted-user", user_turn_id="group:user:accepted-user",
                )
                candidate = completed_empty_assistant(await client.assert_ownership(), receipt)
                assert candidate is not None and candidate.message_id == "answer-id"
                options = dict(expected_task_id="task", expected_team="test-team", blank_assistant=candidate)
                if edit:
                    options["edit_repair"] = True
                if change and change != "edit":
                    with pytest.raises(UnsafePageStateError, match="assistant_changed|accepted_turn_changed|repair_editor_content_changed"):
                        await client.retry_generation(receipt, **options)
                else:
                    assert await client.retry_generation(receipt, **options) == {"progress": True, "transport_active": True}
                expected = ["edit-send"] if change == "edit" else [] if change else ["right"]
                assert await page.evaluate("window.clicks") == expected
            finally:
                await browser.close()

    asyncio.run(run())
