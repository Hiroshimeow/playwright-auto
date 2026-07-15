#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json

from playwright_auto.chatgpt import assign_page_role, inspect_chatgpt_page
from playwright_auto.connection import connected_browser


def parse_role_assignment(value: str) -> tuple[int, str]:
    try:
        index_text, role = value.split("=", 1)
        return int(index_text), role
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("expected PAGE_INDEX=ROLE") from exc


async def run(cdp_url: str, assignments: list[tuple[int, str]]) -> int:
    async with connected_browser(cdp_url) as browser:
        pages = [page for context in browser.contexts for page in context.pages]
        for page_index, role in assignments:
            if page_index < 0 or page_index >= len(pages):
                raise ValueError(f"page index {page_index} is out of range")
            await assign_page_role(pages[page_index], role)

        result = []
        for index, page in enumerate(pages):
            try:
                snapshot = await inspect_chatgpt_page(page)
                item = snapshot.to_dict()
            except Exception as exc:  # Probe should report per-page failures, not hide other tabs.
                item = {"url": page.url, "error": f"{type(exc).__name__}: {exc}"}
            item["page_index"] = index
            result.append(item)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect ChatGPT tabs through the local CDP browser")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument(
        "--set-role",
        action="append",
        default=[],
        type=parse_role_assignment,
        metavar="PAGE_INDEX=ROLE",
        help="persist a role in sessionStorage for one tab",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.cdp, args.set_role))


if __name__ == "__main__":
    raise SystemExit(main())
