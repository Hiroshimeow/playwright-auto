import asyncio

import pytest

from playwright_auto import chatgpt
from playwright_auto import chatgpt_composer


class FakePage:
    def __init__(self, results):
        self.results = list(results)

    async def evaluate(self, script, text):
        assert script is chatgpt_composer.COMPOSER_REPLACE_TEXT_JS
        return self.results.pop(0)


def test_composer_module_owns_atomic_replace_and_facade_error_type():
    page = FakePage([
        {
            "status": "ambiguous",
            "text": "",
            "owner_source": "owned",
            "candidate_count": 2,
        }
    ])

    with pytest.raises(chatgpt.ComposerConflictError, match="multiple eligible"):
        asyncio.run(
            chatgpt_composer.replace_owned_composer_text(
                page, "prompt", timeout_ms=100
            )
        )


def test_chatgpt_facade_uses_composer_owner_primitives():
    assert chatgpt._COMPOSER_RESOLVER_JS is chatgpt_composer.COMPOSER_RESOLVER_JS
    assert chatgpt._COMPOSER_REPLACE_TEXT_JS is chatgpt_composer.COMPOSER_REPLACE_TEXT_JS
    assert chatgpt._replace_owned_composer_text is chatgpt_composer.replace_owned_composer_text


def test_generated_rich_link_icon_is_not_composer_text_but_manual_edit_is():
    """Only generated link chrome is presentation; user-entered characters stay owned."""
    from playwright.async_api import async_playwright

    from playwright_auto.chatgpt import inspect_chatgpt_page
    from playwright_auto.chatgpt_graph import visible_text_matches

    expected = "For new chat (https://chatgpt.com/): treat URL as pending"

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                    <form><div id="prompt-textarea" role="textbox" contenteditable="true">For new chat (<span data-rich-text-generated-autolink text-link-href="https://chatgpt.com/"><span data-inline-url-icon> </span>https://chatgpt.com/</span>): treat URL as pending</div><button data-testid="send-button">Send</button></form>
                ''')
                raw = await page.evaluate("document.querySelector('#prompt-textarea').innerText")
                assert not visible_text_matches(raw, expected), raw
                snapshot = await inspect_chatgpt_page(page)
                assert visible_text_matches(snapshot.composer_text, expected)
                await page.locator('#prompt-textarea').evaluate(
                    "node => node.appendChild(document.createTextNode(' operator edit'))"
                )
                manual = await inspect_chatgpt_page(page)
                assert not visible_text_matches(manual.composer_text, expected)
            finally:
                await browser.close()

    asyncio.run(run())
