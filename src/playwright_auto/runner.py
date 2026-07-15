from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .chatgpt import ChatGPTPage
from .connection import connected_browser
from .loop import WorkflowLoop
from .workflow_file import load_workflow_file
from .workspace import ChatGPTWorkspace


def failure_payload(stage: str, exc: BaseException, **context: Any) -> dict[str, Any]:
    return {
        "status": "failed",
        "stage": stage,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "context": context,
    }


async def run_chatgpt_loop(
    path: str | Path,
    cdp_url: str = "http://127.0.0.1:9222",
) -> tuple[int, dict[str, Any]]:
    workflow_path = Path(path)
    try:
        definition = load_workflow_file(workflow_path)
    except Exception as exc:
        return 2, failure_payload(
            "load_workflow",
            exc,
            workflow_file=str(workflow_path),
        )

    stage = "connect_cdp"
    try:
        async with connected_browser(cdp_url) as browser:
            stage = "prepare_page"
            if not browser.contexts:
                raise RuntimeError("CDP browser has no persistent context")
            context = browser.contexts[0]
            chatgpt_pages = [
                page
                for page in context.pages
                if page.url.startswith("https://chatgpt.com/")
            ]

            stage = "prepare_client"
            if definition.workspace_roles:
                workspace = ChatGPTWorkspace()
                await workspace.attach_existing(
                    chatgpt_pages,
                    allowed_roles=definition.workspace_roles,
                    timeout_ms=definition.workspace_timeout_ms,
                )
                for role in definition.workspace_roles:
                    if role not in workspace.active_roles:
                        await workspace.open_role(
                            context,
                            role,
                            timeout_ms=definition.workspace_timeout_ms,
                        )
                await workspace.bindings()
                client: Any = workspace
            else:
                if chatgpt_pages:
                    page = chatgpt_pages[0]
                else:
                    page = await context.new_page()
                    try:
                        await page.goto(
                            "https://chatgpt.com/",
                            wait_until="domcontentloaded",
                        )
                    except Exception:
                        await page.close()
                        raise
                client = ChatGPTPage(
                    page,
                    timeout_ms=definition.workspace_timeout_ms,
                )

            stage = "run_workflow"
            runner = WorkflowLoop(definition.workflow, definition.loop_options)
            result = await runner.run(client, definition.variables)
            payload = result.to_dict()
            return (
                0 if result.status.value in {"completed", "stopped"} else 1,
                payload,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return 1, failure_payload(
            stage,
            exc,
            workflow_file=str(definition.path),
            cdp_url=cdp_url,
        )
