"""Exact accepted-message view expansion; isolated synthetic DOM only."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from playwright_auto.chatgpt import ChatGPTPage
from playwright_auto.chatgpt_graph import rendered_user_text_matches

CANONICAL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER = "11111111-2222-3333-4444-555555555555"
URL = "https://chatgpt.com/c/" + CANONICAL
OWNER = {
    "page_id": "fixture-page", "page_role": "fixture-test",
    "page_task_id": "fixture-task", "page_team": "fixture-team",
}
PROMPT = ("Approved instructions with " + chr(96) + "code" + chr(96) + "\n"
          + "Exact accepted body and persistent provenance. " * 550)
BODY = """<!doctype html><html><head><meta charset="utf-8"></head><body><main style="width:800px">
<div data-turn-key="11111111-2222-3333-4444-555555555555">
 <div data-chatgpt-search-unit-key="fixture:user"
      data-chatgpt-search-message-ids="11111111-2222-3333-4444-555555555555">
  <h3 data-conversation-role="user">You said:</h3>
  <div data-user-message-bubble data-user-message-has-prompt>
   <div data-search-result-target style="overflow:hidden;max-height:100px;white-space:pre-wrap"></div>
   <span id="ellipsis" aria-hidden="true" style="display:block">…</span>
   <button type="button" aria-expanded="false" data-thread-find-skip data-markdown-copy
     onclick="window.viewClicks++; document.querySelector('[data-search-result-target]').textContent=window.expected;
     this.setAttribute('aria-expanded','true'); this.textContent='Show less';
     document.getElementById('ellipsis').remove(); window.afterExpand?.();">Show more</button>
  </div>
 </div>
 <div data-chatgpt-search-unit-key="fixture:assistant" data-chatgpt-search-message-ids="assistant-id"><h4 data-conversation-role="assistant">ChatGPT said:</h4>Saved answer</div>
</div>
<form><div id="prompt-textarea" contenteditable="true" role="textbox" style="min-height:20px;width:600px"></div>
<button type="button" data-testid="send-button" onclick="window.sends++">Send</button></form>
</main></body></html>"""


@asynccontextmanager
async def transcript_fixture():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/snap/bin/chromium" if Path("/snap/bin/chromium").is_file() else None,
            headless=True,
        )
        try:
            context = await browser.new_context()

            async def route(request):
                if request.request.url == URL:
                    await request.fulfill(content_type="text/html", body=BODY)
                else:
                    await request.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            await page.goto(URL)
            await page.evaluate("""([owner,prompt]) => {
                for(const [field,key] of [
                    ['page_id','playwright-auto:page-id'], ['page_role','playwright-auto:role'],
                    ['page_task_id','playwright-auto:task-id'], ['page_team','playwright-auto:team']
                ]) sessionStorage.setItem(key,owner[field]);
                window.expected=prompt; window.viewClicks=0; window.sends=0;
                document.querySelector('[data-search-result-target]').textContent=prompt.slice(0,10240);
            }""", [OWNER, PROMPT])
            client = ChatGPTPage(page)
            cdp = await context.new_cdp_session(page)
            pinned = (await cdp.send("Target.getTargetInfo"))["targetInfo"]["targetId"]

            async def identity_guard():
                actual = (await cdp.send("Target.getTargetInfo"))["targetInfo"]
                assert actual["targetId"] == pinned
                assert actual["url"] == URL

            yield page, client, identity_guard
            assert await page.evaluate("window.sends") == 0
            await cdp.detach()
        finally:
            await browser.close()


async def invoke(client, guard, *, timeout=1.0):
    from playwright_auto.chatgpt_transcript_view import expand_saved_user_message
    return await expand_saved_user_message(
        client, expected_user_id=USER, expected_prompt=PROMPT,
        expected_owner=OWNER, canonical=CANONICAL,
        identity_guard=guard, timeout_seconds=timeout,
    )


def test_collapsed_exact_user_expands_once_then_proves_full_text():
    async def run():
        async with transcript_fixture() as (page, client, guard):
            before = await client.snapshot()
            assert not rendered_user_text_matches(before.messages[0].text, PROMPT)
            result = await invoke(client, guard)
            assert result is not None
            assert rendered_user_text_matches(result.messages[0].text, PROMPT)
            assert result.messages[0].message_id == USER
            assert result.composer_text == ""
            assert await page.evaluate("window.viewClicks") == 1
            again = await invoke(client, guard)
            assert again is not None
            assert await page.evaluate("window.viewClicks") == 1
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    "document.querySelector('button[aria-expanded]').textContent='Continue'",
    "document.querySelector('button[aria-expanded]').type='submit'",
    "document.querySelector('button[aria-expanded]').disabled=true",
    "document.querySelector('button[aria-expanded]').setAttribute('aria-disabled','true')",
    "document.querySelector('button[aria-expanded]').setAttribute('aria-expanded','true')",
    "document.querySelector('button[aria-expanded]').removeAttribute('data-thread-find-skip')",
    "document.querySelector('button[aria-expanded]').removeAttribute('data-markdown-copy')",
    "document.querySelector('button[aria-expanded]').style.display='none'",
    "document.querySelector('button[aria-expanded]').after(document.querySelector('button[aria-expanded]').cloneNode(true))",
    "document.querySelector('[data-user-message-bubble]').removeAttribute('data-user-message-has-prompt')",
    "document.querySelector('[data-search-result-target]').textContent='A different prompt'",
    "document.querySelector('[data-chatgpt-search-unit-key]').setAttribute('data-chatgpt-search-message-ids','foreign-id')",
    "document.querySelector('[data-chatgpt-search-unit-key]').after(document.querySelector('[data-chatgpt-search-unit-key]').cloneNode(true))",
    "document.querySelector('[data-turn-key]').setAttribute('data-turn-key','different-turn')",
    "document.querySelector('[data-search-result-target]').setAttribute('aria-hidden','true')",
])
def test_nonexact_or_unclassified_view_control_is_never_clicked(change):
    async def run():
        from playwright_auto.chatgpt_transcript_view import SavedTranscriptViewError
        async with transcript_fixture() as (page, client, guard):
            await page.evaluate("change => { eval(change); }", change)
            before = await page.content()
            try:
                assert await invoke(client, guard) is None
            except SavedTranscriptViewError:
                pass
            assert await page.evaluate("window.viewClicks") == 0
            assert await page.content() == before
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    "sessionStorage.setItem('playwright-auto:page-id','foreign-page')",
    "sessionStorage.setItem('playwright-auto:role','foreign-role')",
    "sessionStorage.setItem('playwright-auto:task-id','foreign-task')",
    "sessionStorage.setItem('playwright-auto:team','foreign-team')",
    "document.getElementById('prompt-textarea').textContent='Operator draft'",
    "document.querySelector('form').insertAdjacentHTML('beforeend','<div data-filename=\"draft.txt\">draft.txt</div>')",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<button data-testid=\"stop-button\">Stop</button>')",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<div role=\"dialog\">Permission requested</div>')",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<div role=\"alert\">Unavailable</div>')",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<button data-testid=\"login-button\">Log in</button>')",
    "document.querySelector('main').insertAdjacentHTML('beforeend','<div data-codex-approval-surface=\"true\"><button>Allow once</button></div>')",
    "document.getElementById('prompt-textarea').after(document.getElementById('prompt-textarea').cloneNode(true))",
    "window._cf_chl_opt={}",
])
def test_unsafe_or_changed_page_blocks_view_expansion(change):
    async def run():
        from playwright_auto.chatgpt_transcript_view import SavedTranscriptViewError
        async with transcript_fixture() as (page, client, guard):
            await page.evaluate("change => { eval(change); }", change)
            before = await page.content()
            with pytest.raises(SavedTranscriptViewError):
                await invoke(client, guard)
            assert await page.evaluate("window.viewClicks") == 0
            assert await page.content() == before
    asyncio.run(run())


def test_identity_change_immediately_before_click_preserves_page():
    async def run():
        from playwright_auto.chatgpt_transcript_view import SavedTranscriptViewError
        async with transcript_fixture() as (page, client, guard):
            calls = 0
            async def changing_guard():
                nonlocal calls
                calls += 1
                await guard()
                if calls == 3:
                    await page.evaluate("sessionStorage.setItem('playwright-auto:task-id','foreign')")
            with pytest.raises(SavedTranscriptViewError):
                await invoke(client, changing_guard)
            assert await page.evaluate("window.viewClicks") == 0
    asyncio.run(run())


@pytest.mark.parametrize("effect", [
    "sessionStorage.setItem('playwright-auto:task-id','foreign')",
    "document.querySelector('[data-search-result-target]').textContent='Wrong full result'",
    "document.querySelector('[data-search-result-target]').textContent=window.expected.slice(0,10240)",
    "document.getElementById('prompt-textarea').textContent='New operator draft'",
])
def test_post_click_change_or_incomplete_text_never_claims_success(effect):
    async def run():
        from playwright_auto.chatgpt_transcript_view import SavedTranscriptViewError
        async with transcript_fixture() as (page, client, guard):
            await page.evaluate("effect=>{window.afterExpand=()=>{eval(effect)}}", effect)
            with pytest.raises(SavedTranscriptViewError):
                await invoke(client, guard, timeout=0.5)
            assert await page.evaluate("window.viewClicks") == 1
    asyncio.run(run())


def test_timeout_before_view_click_is_bounded():
    async def run():
        from playwright_auto.chatgpt_transcript_view import SavedTranscriptViewError
        async with transcript_fixture() as (page, client, _guard):
            async def slow_guard():
                await asyncio.sleep(1)
            with pytest.raises(SavedTranscriptViewError):
                await invoke(client, slow_guard, timeout=0.02)
            assert await page.evaluate("window.viewClicks") == 0
    asyncio.run(run())


def test_cancellation_after_view_click_does_not_retry_or_return_success():
    async def run():
        async with transcript_fixture() as (page, client, guard):
            await page.evaluate("() => { window.afterExpand=()=>{document.querySelector('[data-search-result-target]').textContent='incomplete'}; }")
            task = asyncio.create_task(invoke(client, guard, timeout=2))
            for _ in range(100):
                if await page.evaluate("window.viewClicks") == 1:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("view was never expanded")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await page.evaluate("window.viewClicks") == 1
    asyncio.run(run())
