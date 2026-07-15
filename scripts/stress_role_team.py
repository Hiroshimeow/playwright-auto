#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from playwright_auto.connection import connected_browser
from playwright_auto.roles import expand_role_team
from playwright_auto.workspace import ChatGPTWorkspace

DEFAULT_STAGES = (
    {"PLAN": 1, "DEV": 1},
    {"PLAN": 1, "DEV": 2, "REVIEW": 1, "TEST": 1},
    {"PLAN": 1, "DEV": 3, "REVIEW": 4, "TEST": 2},
)


async def inspect_visible_role(client, role: str, task_id: str) -> dict[str, Any]:
    snapshot = await client.assert_ownership()
    visible = await client.page.evaluate(
        """() => {
          const badge = document.getElementById('playwright-auto-role-badge-v3');
          return {
            title: document.title,
            badge: badge?.textContent || null,
            badgeRole: badge?.dataset.role || null,
            badgePageId: badge?.dataset.pageId || null,
            badgeTaskId: badge?.dataset.taskId || null,
          };
        }"""
    )
    if not visible["title"].startswith(f"⟦{role}⟧ "):
        raise AssertionError(f"tab title for {role} is not visible: {visible!r}")
    if not str(visible["badge"] or "").startswith(f"{role} · "):
        raise AssertionError(f"badge for {role} is not visible: {visible!r}")
    if visible["badgePageId"] != snapshot.page_id:
        raise AssertionError(f"badge page id mismatch for {role}")
    if visible["badgeTaskId"] != task_id or snapshot.page_task_id != task_id:
        raise AssertionError(f"task binding mismatch for {role}: {visible!r}")
    return {
        "role": role,
        "page_id": snapshot.page_id,
        "url": snapshot.url,
        **visible,
    }


async def exercise_role(client, role: str, marker: str) -> dict[str, Any]:
    await client.set_text(marker)
    draft = await client.snapshot()
    if draft.composer_text.strip() != marker:
        raise AssertionError(f"composer mismatch for {role}")
    await client.clear()
    ready = await client.snapshot()
    if not ready.composer_empty:
        raise AssertionError(f"composer did not clear for {role}")
    return {"role": role, "draft_state": draft.state.value, "ready_state": ready.state.value}


async def run_stage(
    context,
    team: dict[str, int],
    *,
    cycles: int,
    dwell_ms: int,
) -> dict[str, Any]:
    slots = expand_role_team(team)
    roles = tuple(slot.display_name for slot in slots)
    task_id = f"ROLE-STRESS-{len(roles)}"
    workspace = ChatGPTWorkspace()
    await workspace.attach_existing(context.pages, allowed_roles=roles)
    opened: list[str] = []
    for role in roles:
        if role not in workspace.active_roles:
            await workspace.open_role(context, role)
            opened.append(role)

    preflights = await asyncio.gather(
        *(workspace.get(role).task_preflight(task_id) for role in roles)
    )
    preparations = await asyncio.gather(
        *(workspace.get(role).prepare_task(task_id) for role in roles)
    )
    baseline = {
        role: (await workspace.get(role).assert_ownership()).page_id for role in roles
    }
    cycle_results = []
    for cycle in range(1, cycles + 1):
        marker_prefix = f"ROLE_TEAM_STRESS_{len(roles)}_{cycle}"
        exercises = await asyncio.gather(
            *(
                exercise_role(
                    workspace.get(role),
                    role,
                    f"{marker_prefix}_{role}",
                )
                for role in roles
            )
        )
        await asyncio.gather(*(workspace.get(role).refresh() for role in roles))
        visible = await asyncio.gather(
            *(inspect_visible_role(workspace.get(role), role, task_id) for role in roles)
        )
        current = {item["role"]: item["page_id"] for item in visible}
        if current != baseline:
            raise AssertionError(
                f"page identity changed during stage {len(roles)}: "
                f"expected={baseline!r} actual={current!r}"
            )
        cycle_results.append(
            {
                "cycle": cycle,
                "exercises": exercises,
                "visible": visible,
            }
        )

    for role in roles:
        page = workspace.get(role).page
        await page.bring_to_front()
        if dwell_ms:
            await page.wait_for_timeout(dwell_ms)

    return {
        "team": team,
        "task_id": task_id,
        "roles": list(roles),
        "preflights": preflights,
        "preparations": preparations,
        "opened": opened,
        "page_ids": baseline,
        "cycles": cycle_results,
    }


async def main_async(args) -> dict[str, Any]:
    started = time.monotonic()
    stages = []
    async with connected_browser(args.cdp) as browser:
        if not browser.contexts:
            raise RuntimeError("persistent CDP context is missing")
        context = browser.contexts[0]
        for team in DEFAULT_STAGES:
            stages.append(
                await run_stage(
                    context,
                    team,
                    cycles=args.cycles,
                    dwell_ms=args.dwell_ms,
                )
            )
        plan_pages = [
            page
            for page in context.pages
            if await page.evaluate(
                "sessionStorage.getItem('playwright-auto:role') === 'PLAN'"
            )
            if page.url.startswith("https://chatgpt.com/")
        ]
        if plan_pages:
            await plan_pages[0].bring_to_front()
        chat_pages = [page for page in context.pages if page.url.startswith("https://chatgpt.com/")]
        return {
            "status": "passed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "cycles_per_stage": args.cycles,
            "stages": stages,
            "chatgpt_page_count": len(chat_pages),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress visible multi-role ChatGPT tabs")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--dwell-ms", type=int, default=350)
    parser.add_argument("--output", type=Path, default=Path(".runtime/role-team-stress.json"))
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles must be at least 1")
    result = asyncio.run(main_async(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
