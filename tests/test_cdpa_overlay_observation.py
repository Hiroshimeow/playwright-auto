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
