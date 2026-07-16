from __future__ import annotations

import argparse
import asyncio
import json

from .controller import PlaywrightStudioBackend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only smoke test for ChatGPT tabs available on CDP 9222"
    )
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--pretty", action="store_true")
    return parser


async def run_smoke(cdp_url: str) -> list[dict[str, object]]:
    backend = PlaywrightStudioBackend()
    try:
        await backend.connect(cdp_url)
        tabs = await backend.discover()
        return [
            {
                "page_id": tab.page_id,
                "title": tab.title,
                "url": tab.url,
                "role": tab.role,
                "browser_state": tab.browser_state,
                "requires_login": tab.requires_login,
            }
            for tab in tabs
        ]
    finally:
        await backend.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tabs = asyncio.run(run_smoke(args.cdp))
    print(
        json.dumps(
            {"status": "ok", "tab_count": len(tabs), "tabs": tabs},
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=args.pretty,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
