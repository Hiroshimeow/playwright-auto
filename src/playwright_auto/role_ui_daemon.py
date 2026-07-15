from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .connection import connect
from .role_indicator import ensure_role_indicator

SUPPORTED_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com", "auth.openai.com"})


def supported_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in SUPPORTED_HOSTS
    except ValueError:
        return False


@dataclass(frozen=True)
class VisibleRoleState:
    page_id: str
    role: str | None
    task_id: str | None
    url: str
    title: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "page_id": self.page_id,
            "role": self.role,
            "task_id": self.task_id,
            "url": self.url,
            "title": self.title,
        }


class RoleUIDaemon:
    def __init__(
        self,
        *,
        cdp_url: str = "http://127.0.0.1:9222",
        poll_seconds: float = 0.5,
        reconnect_seconds: float = 2.0,
        event_log: Path = Path(".runtime/role-ui-events.jsonl"),
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be positive")
        self.cdp_url = cdp_url
        self.poll_seconds = poll_seconds
        self.reconnect_seconds = reconnect_seconds
        self.event_log = event_log
        self._known: dict[str, VisibleRoleState] = {}

    def _append_event(self, event_type: str, state: VisibleRoleState, **extra: Any) -> None:
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "event": event_type,
            "at": datetime.now(timezone.utc).isoformat(),
            **state.to_dict(),
            **extra,
        }
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    async def _inspect_pages(self, browser: Any) -> list[tuple[Any, VisibleRoleState]]:
        rows: list[tuple[Any, VisibleRoleState]] = []
        for context in browser.contexts:
            for page in context.pages:
                if page.is_closed() or not supported_url(page.url):
                    continue
                try:
                    result = await ensure_role_indicator(page)
                except Exception:
                    # Navigation can replace the document between URL filtering and evaluate.
                    continue
                page_id = str(result.get("pageId") or "").strip()
                if not page_id:
                    continue
                state = VisibleRoleState(
                    page_id=page_id,
                    role=(str(result["role"]) if result.get("role") else None),
                    task_id=(str(result["taskId"]) if result.get("taskId") not in {None, "idle"} else None),
                    url=page.url,
                    title=str(result.get("title") or ""),
                )
                rows.append((page, state))
        return rows

    async def _publish_registry(self, rows: list[tuple[Any, VisibleRoleState]]) -> None:
        role_pages: dict[str, list[VisibleRoleState]] = {}
        for _, state in rows:
            if state.role:
                role_pages.setdefault(state.role, []).append(state)
        roles = sorted(role_pages)
        for page, state in rows:
            duplicates = role_pages.get(state.role or "", []) if state.role else []
            conflict = None
            if len(duplicates) > 1:
                ids = ", ".join(item.page_id[:8] for item in duplicates)
                conflict = f"Duplicate role {state.role}: tabs {ids}. Rename one role, e.g. {state.role}1."
            try:
                await page.evaluate(
                    """({roles, conflict}) => {
                      const api = window.__PLAYWRIGHT_AUTO_ROLE_INDICATOR__;
                      if (!api) return false;
                      api.setRegistry({roles});
                      api.setConflict(conflict);
                      api.apply();
                      return true;
                    }""",
                    {"roles": roles, "conflict": conflict},
                )
            except Exception:
                continue

    def _record_changes(self, rows: list[tuple[Any, VisibleRoleState]]) -> None:
        current = {state.page_id: state for _, state in rows}
        for page_id, state in current.items():
            previous = self._known.get(page_id)
            if previous is None:
                self._append_event("page_seen", state)
            elif previous.role != state.role:
                self._append_event(
                    "role_changed",
                    state,
                    previous_role=previous.role,
                    previous_task_id=previous.task_id,
                )
            elif previous.task_id != state.task_id:
                self._append_event(
                    "task_changed",
                    state,
                    previous_task_id=previous.task_id,
                )
            elif previous.url != state.url:
                self._append_event("page_navigated", state, previous_url=previous.url)
        for page_id, previous in self._known.items():
            if page_id not in current:
                self._append_event("page_missing", previous)
        self._known = current

    async def sync_once(self, browser: Any) -> list[VisibleRoleState]:
        rows = await self._inspect_pages(browser)
        await self._publish_registry(rows)
        self._record_changes(rows)
        return [state for _, state in rows]

    async def _serve_connection(self) -> None:
        playwright, browser = await connect(self.cdp_url)
        try:
            while browser.is_connected():
                await self.sync_once(browser)
                await asyncio.sleep(self.poll_seconds)
        finally:
            await playwright.stop()

    async def run_forever(self) -> None:
        while True:
            try:
                await self._serve_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.event_log.parent.mkdir(parents=True, exist_ok=True)
                with self.event_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "event": "connection_error",
                                "at": datetime.now(timezone.utc).isoformat(),
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
            await asyncio.sleep(self.reconnect_seconds)


async def async_main(args: argparse.Namespace) -> int:
    daemon = RoleUIDaemon(
        cdp_url=args.cdp,
        poll_seconds=args.poll,
        reconnect_seconds=args.reconnect,
        event_log=Path(args.event_log),
    )
    if args.once:
        playwright, browser = await connect(args.cdp)
        try:
            states = await daemon.sync_once(browser)
        finally:
            await playwright.stop()
        print(json.dumps([state.to_dict() for state in states], ensure_ascii=False, indent=2))
        return 0
    await daemon.run_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep visible manual role controls on persistent browser tabs")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--reconnect", type=float, default=2.0)
    parser.add_argument("--event-log", default=".runtime/role-ui-events.jsonl")
    parser.add_argument("--once", action="store_true")
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
