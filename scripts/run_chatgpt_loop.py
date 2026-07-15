#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright_auto.runner import run_chatgpt_loop


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one file-defined ChatGPT workflow in a controlled loop"
    )
    parser.add_argument(
        "workflow_file",
        type=Path,
        nargs="?",
        default=Path("workflows/chatgpt_loop.py"),
    )
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    args = parser.parse_args()
    exit_code, payload = asyncio.run(
        run_chatgpt_loop(args.workflow_file, args.cdp)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
