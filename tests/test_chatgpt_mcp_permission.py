import asyncio

import pytest
from playwright.async_api import async_playwright

import playwright_auto.chatgpt as chatgpt_module
from playwright_auto.chatgpt import (
    ChatGPTPage,
    MessageBaseline,
    PageBinding,
    SendReceipt,
    UnsafePageStateError,
    inspect_chatgpt_wait_probe,
    prompt_digest,
)


async def _with_page(body: str, callback):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/snap/bin/chromium",
            headless=True,
        )
        try:
            page = await browser.new_page()
            await page.set_content(body)
            await page.evaluate(
                """() => {
                    window.name = '__PLAYWRIGHT_AUTO_BINDING__:' + JSON.stringify({
                        role: 'DEV',
                        pageId: 'page-1',
                        taskId: 'task-1',
                        team: 'team-1'
                    });
                }"""
            )
            return await callback(page)
        finally:
            await browser.close()


def test_auto_allow_uses_listen_action_without_dom_permission_button():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        await page.evaluate(
            """() => {
              const node = document.querySelector('[data-message-id="tool-1"]');
              node.__reactProps$test = {
                onSelectOption: (_event, action) => { window.__mcpAction = action; },
                options: [{
                  action: {
                    type: 'allow',
                    target_message_id: 'call-1',
                    remember_answer: true,
                  }
                }],
              };
            }"""
        )
        result = await client.auto_allow_mcp_permission(
            passive_action={
                "type": "allow",
                "target_message_id": "call-1",
                "remember_answer": True,
                "label": "Allow mcp-g8 for this conversation",
            }
        )
        return result, await page.evaluate("window.__mcpAction || null")

    result, action = asyncio.run(
        _with_page(
            """
            <main>
              <div data-message-id="tool-1">tool approval payload</div>
              <div contenteditable="true" role="textbox"></div>
            </main>
            """,
            run,
        )
    )

    assert result["method"] == "react_handler"
    assert result["target_message_id"] == "call-1"
    assert result["remember_answer"] == "true"
    assert action == {
        "type": "allow",
        "target_message_id": "call-1",
        "remember_answer": True,
    }


def test_auto_allow_falls_back_to_plain_visible_allow_button():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        result = await client.auto_allow_mcp_permission()
        return result, await page.evaluate("window.__mcpAllowClicks || 0")

    result, clicks = asyncio.run(
        _with_page(
            """
            <main>
              <button onclick="window.__mcpAllowClicks = (window.__mcpAllowClicks || 0) + 1">Allow</button>
              <div contenteditable="true" role="textbox"></div>
            </main>
            """,
            run,
        )
    )

    assert result["method"] == "dom_click"
    assert clicks == 1


def test_sparse_probe_detects_valid_mcp_allow_group_even_with_composer_present():
    async def run(page):
        probe = await inspect_chatgpt_wait_probe(page)
        return probe

    probe = asyncio.run(
        _with_page(
            """
            <main>
              <div contenteditable="true" role="textbox">draft</div>
              <div id="permission-actions">
                <button class="btn-primary" onclick="window.__mcpAllowClicks = (window.__mcpAllowClicks || 0) + 1"><span>Allow</span></button>
                <button aria-label="Allow mcp-thinkbook for this conversation"></button>
              </div>
            </main>
            """,
            run,
        )
    )

    assert probe.mcp_permission_allow_count == 1


@pytest.mark.parametrize(
    "body",
    [
        """
        <main>
          <div><button class="btn-primary"><span>Allow</span></button></div>
        </main>
        """,
        """
        <main>
          <div>
            <button class="btn-primary"><span>Allow</span></button>
            <button aria-label="Allow calendar access"></button>
          </div>
        </main>
        """,
        """
        <main>
          <div>
            <button class="btn-primary"><span>Allow</span></button>
            <button aria-label="Allow mcp-one for this conversation"></button>
          </div>
          <div>
            <button class="btn-primary"><span>Allow</span></button>
            <button aria-label="Allow mcp-two for this conversation"></button>
          </div>
        </main>
        """,
    ],
)
def test_sparse_probe_rejects_unrelated_or_ambiguous_allow(body):
    async def run(page):
        return await inspect_chatgpt_wait_probe(page)

    probe = asyncio.run(_with_page(body, run))

    assert probe.mcp_permission_allow_count != 1


def test_sparse_probe_accepts_generic_conversation_allow_label():
    async def run(page):
        return await inspect_chatgpt_wait_probe(page)

    probe = asyncio.run(
        _with_page(
            """
            <main>
              <div id="permission-actions">
                <button class="btn-primary"><span>Allow</span></button>
                <button aria-label="Allow Gmail for this conversation"></button>
              </div>
            </main>
            """,
            run,
        )
    )

    assert probe.mcp_permission_allow_count == 1
    assert probe.mcp_permission_node_count == 1


def test_wait_snapshot_observes_mcp_allow_without_dispatching_it():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        client.binding = PageBinding("page-1", "DEV")
        receipt = SendReceipt(
            prompt="probe",
            prompt_sha256=prompt_digest("probe"),
            binding=client.binding,
            baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
            attempts=1,
            accepted_via="exact_user_message",
            session_id_before=None,
            user_message_id="user-1",
        )

        await client.wait_snapshot(receipt)
        click_count = await page.evaluate("window.__mcpAllowClicks || 0")
        probe = await client.current_wait_probe()
        return (
            click_count,
            client.wait_metrics["sparse_probes"],
            probe.mcp_permission_allow_count,
            probe.mcp_permission_node_count,
        )

    click_count, sparse_probes, visible_count, node_count = asyncio.run(
        _with_page(
            """
            <main>
              <div contenteditable="true" role="textbox"></div>
              <div id="permission-actions">
                <button class="btn-primary" onclick="window.__mcpAllowClicks = (window.__mcpAllowClicks || 0) + 1"><span>Allow</span></button>
                <button aria-label="Allow mcp-thinkbook for this conversation"></button>
              </div>
            </main>
            """,
            run,
        )
    )

    assert click_count == 0
    assert sparse_probes == 1
    assert visible_count == 1
    assert node_count == 1


def test_sparse_probe_distinguishes_hidden_permission_node_from_visible_offer():
    async def run(page):
        return await inspect_chatgpt_wait_probe(page)

    probe = asyncio.run(
        _with_page(
            """
            <main>
              <div contenteditable="true" role="textbox"></div>
              <div style="display:none">
                <button><span>Allow</span></button>
                <button aria-label="Allow mcp-g8 for this conversation"></button>
              </div>
            </main>
            """,
            run,
        )
    )

    assert probe.mcp_permission_allow_count == 0
    assert probe.mcp_permission_node_count == 1


def test_auto_allow_does_not_click_unrelated_allow_label():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        result = await client.auto_allow_mcp_permission()
        clicks = await page.evaluate("window.__unrelatedAllowClicks || 0")
        return result, clicks

    result, clicks = asyncio.run(
        _with_page(
            """
            <main>
              <button aria-label="Allow camera access"
                onclick="window.__unrelatedAllowClicks=(window.__unrelatedAllowClicks||0)+1"></button>
            </main>
            """,
            run,
        )
    )

    assert result is None
    assert clicks == 0


def test_preferred_allow_finds_generic_remembered_react_action_at_depth_six():
    async def run(page):
        client = ChatGPTPage(page, timeout_ms=5_000)
        await page.evaluate(
            """() => {
              const permission = document.querySelector('#permission');
              permission.__reactProps$fixture = {
                onSelectOption: (_event, action) => { window.__mcpAction = action; },
                a: {b: {c: {d: {e: {f: {
                  action: {
                    type: 'allow',
                    target_message_id: 'depth-six-call',
                    remember_answer: true,
                  }
                }}}}}},
              };
              permission.addEventListener('click', () => {
                window.__domAllowClicks = (window.__domAllowClicks || 0) + 1;
              });
            }"""
        )
        result = await client.auto_allow_mcp_permission()
        return (
            result,
            await page.evaluate("window.__mcpAction || null"),
            await page.evaluate("window.__domAllowClicks || 0"),
        )

    result, action, clicks = asyncio.run(
        _with_page(
            """
            <main>
              <button id="permission" aria-label="Allow Gmail for this conversation"></button>
            </main>
            """,
            run,
        )
    )

    assert result["method"] == "react_handler"
    assert result["target_message_id"] == "depth-six-call"
    assert result["remember_answer"] == "true"
    assert action == {
        "type": "allow",
        "target_message_id": "depth-six-call",
        "remember_answer": True,
    }
    assert clicks == 0
