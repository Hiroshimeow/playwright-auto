#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from playwright_auto.chatgpt import backend_conversation


UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


def conversation_id(value: str) -> str:
    value = value.strip()
    if "/c/" in value:
        parsed = urlparse(value if "://" in value else f"https://chatgpt.com{value}")
        value = parsed.path.split("/c/", 1)[1].split("/", 1)[0]
    if not UUID_RE.fullmatch(value):
        raise ValueError(f"invalid ChatGPT conversation URL/UUID: {value!r}")
    return value


async def get_graph(cid: str) -> dict:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
        if not browser.contexts:
            raise RuntimeError("CDP 9222 has no browser context")
        return await backend_conversation(browser.contexts[0], cid)
    finally:
        # Attached to an existing browser: never browser.close().
        await playwright.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Get the full ChatGPT conversation graph through an existing CDP 9222 browser."
    )
    parser.add_argument("uuid_url", help="Full https://chatgpt.com/c/<uuid>, /c/<uuid>, or bare UUID")
    args = parser.parse_args()

    cid = conversation_id(args.uuid_url)
    graph = asyncio.run(get_graph(cid))
    text = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
    output = Path.cwd() / f"full_node_graph_{cid}.json"
    output.write_text(text, encoding="utf-8")

    sys.stdout.write(text)
    print(f"saved: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
