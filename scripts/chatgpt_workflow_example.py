#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json

from playwright_auto.chatgpt import ChatGPTPage
from playwright_auto.chatgpt_blocks import (
    CaptureSnapshotBlock,
    ClearComposerBlock,
    NewChatBlock,
    SetComposerBlock,
    SetRoleBlock,
)
from playwright_auto.connection import connected_browser
from playwright_auto.workflow import Workflow


async def main() -> None:
    async with connected_browser("http://127.0.0.1:9222") as browser:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        if not page.url.startswith("https://chatgpt.com/"):
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")

        workflow = Workflow(
            "prepare-plan-tab",
            [
                SetRoleBlock("PLAN"),
                NewChatBlock(),
                SetComposerBlock(lambda ctx: ctx.require("prompt")),
                CaptureSnapshotBlock("draft", block_id="capture_draft"),
                ClearComposerBlock(),
                CaptureSnapshotBlock("ready", block_id="capture_ready"),
            ],
        )

        run = await workflow.run(
            ChatGPTPage(page),
            variables={"prompt": "Workflow runtime probe"},
        )
        print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
