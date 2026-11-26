import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

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
    assert chatgpt._COMPOSER_TEXT_JS is chatgpt_composer.COMPOSER_TEXT_JS


_COMPOSER_PROMPT = 'Before\n\n{"source":"https://example.invalid/ab"}\nAfter line\nhard break'
_URL_CHIP = (
    '<span data-rich-text-generated-autolink="" style="display:inline;white-space:nowrap">'
    '<span class="url-label" style="display:inline;white-space:normal">'
    '<span data-inline-url-icon="" aria-hidden="true" contenteditable="false">'
    '<span style="display:inline-flex"><svg width="20" height="20" style="display:flex">'
    '<path style="display:block" d="M0 0h20v20H0z"></path></svg></span></span>'
    'https://example.invalid/ab</span></span>'
)
_COMPOSER_BODY = (
    '<main><form><div id="prompt-textarea" class="ProseMirror" contenteditable="true" '
    'role="textbox" style="white-space:break-spaces;width:600px">'
    '<p>Before</p><p><br></p><p>{"source":"' + _URL_CHIP + '"}</p>'
    '<p>After line<br>hard break</p></div>'
    '<button type="button" data-testid="send-button" onclick="window.sends++">Send</button>'
    '</form><input id="other-input"></main>'
)


async def _with_composer(check):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/snap/bin/chromium" if Path("/snap/bin/chromium").is_file() else None,
            headless=True,
        )
        try:
            context = await browser.new_context()
            url = "https://chatgpt.com/composer-fixture"

            async def route(request):
                if request.request.url == url:
                    await request.fulfill(content_type="text/html", body=_COMPOSER_BODY)
                else:
                    await request.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            await page.goto(url)
            composer = page.locator("#prompt-textarea")
            draft = await composer.inner_html()
            await composer.evaluate("element => { element.textContent = ''; }")
            client = chatgpt.ChatGPTPage(page)
            await client.set_role("composer-test")
            await client.bind_task_identity("composer-task", "composer-team")
            await composer.evaluate("(element, html) => { element.innerHTML = html; }", draft)
            await page.evaluate("""() => {
                window.nativeEdits = 0; window.sends = 0;
                document.execCommand = () => {
                    window.nativeEdits++;
                    throw Error('An existing draft must not be edited');
                };
            }""")
            await check(page, client)
            assert await page.evaluate("({edits:window.nativeEdits,sends:window.sends})") == {
                "edits": 0, "sends": 0,
            }
        finally:
            await browser.close()


def test_url_chip_layout_does_not_change_owned_composer_text():
    async def check(page, client):
        composer = page.locator("#prompt-textarea")
        before = await composer.inner_html()
        # The observed ChatGPT icon layout inserts a CSS-only newline before the URL.
        assert not chatgpt.visible_text_matches(await composer.inner_text(), _COMPOSER_PROMPT)
        assert await composer.evaluate(chatgpt_composer.COMPOSER_TEXT_JS) == _COMPOSER_PROMPT
        assert chatgpt.visible_text_matches((await client.snapshot()).composer_text, _COMPOSER_PROMPT)
        probe = await chatgpt.inspect_chatgpt_wait_probe(page)
        assert chatgpt.visible_text_matches(probe.composer_text, _COMPOSER_PROMPT)

        await client.set_text(_COMPOSER_PROMPT)
        await chatgpt_composer.replace_owned_composer_text(page, _COMPOSER_PROMPT, timeout_ms=1000)
        assert await composer.inner_html() == before

    asyncio.run(_with_composer(check))


@pytest.mark.parametrize("change", ["url_space", "manual_draft"])
def test_real_composer_changes_are_preserved_and_rejected(change):
    async def check(page, client):
        await page.evaluate("""change => {
            const composer = document.getElementById('prompt-textarea');
            if (change === 'url_space') {
                composer.querySelector('.url-label').lastChild.nodeValue = 'https://example.invalid/a b';
            } else {
                composer.textContent = 'My unsaved operator draft';
            }
        }""", change)
        before = await page.locator("#prompt-textarea").inner_html()
        assert not chatgpt.visible_text_matches((await client.snapshot()).composer_text, _COMPOSER_PROMPT)
        with pytest.raises(chatgpt.ComposerConflictError, match="unowned/manual input"):
            await client.set_text(_COMPOSER_PROMPT)
        assert await page.locator("#prompt-textarea").inner_html() == before

    asyncio.run(_with_composer(check))


@pytest.mark.parametrize("change", ["focus_stolen", "ambiguous_owner"])
def test_structured_composer_read_keeps_atomic_owner_checks(change):
    async def check(page, _client):
        await page.evaluate("""change => {
            const composer = document.getElementById('prompt-textarea');
            if (change === 'focus_stolen') {
                composer.addEventListener('focus', () => document.getElementById('other-input').focus());
            } else {
                composer.after(composer.cloneNode(true));
            }
        }""", change)
        before = await page.locator("#prompt-textarea").evaluate_all("(nodes) => nodes.map(n => n.innerHTML)")
        with pytest.raises(chatgpt.ComposerConflictError, match="focus was stolen|multiple eligible"):
            await chatgpt_composer.replace_owned_composer_text(page, _COMPOSER_PROMPT, timeout_ms=1000)
        assert await page.locator("#prompt-textarea").evaluate_all("(nodes) => nodes.map(n => n.innerHTML)") == before

    asyncio.run(_with_composer(check))
