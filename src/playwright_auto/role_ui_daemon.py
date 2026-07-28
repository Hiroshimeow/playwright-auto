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
_BINDING_READ_SCRIPT = "() => window.__PLAYWRIGHT_AUTO_ROLE_BINDING_READ__?.() || null"
_BINDING_INSTALL_SCRIPT = """() => {
  window.__PLAYWRIGHT_AUTO_ROLE_BINDING_READ__ = () => {
    const api = window.__PLAYWRIGHT_AUTO_ROLE_INDICATOR__;
    if (!api?.readBinding) return null;
    const value = api.readBinding({createPageId: false}) || {};
    return {
      role: value.role || null,
      pageId: value.pageId || null,
      taskId: value.taskId || null,
      title: document.title || '',
      badgePresent: Boolean(document.getElementById('playwright-auto-role-badge-v3')),
    };
  };
  return true;
}"""


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
        poll_seconds: float = 5.0,
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
        self._registry_signatures: dict[str, tuple[object, ...]] = {}
        self._wake = asyncio.Event()
        self._watched_pages: set[int] = set()
        self.metrics = {
            "registry_publications": 0,
            "registry_noops": 0,
            "binding_reads": 0,
            "indicator_repairs": 0,
            "event_wakes": 0,
            "timer_reconciliations": 0,
        }

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

    def _wake_now(self, *_args: Any) -> None:
        self._wake.set()

    def _watch_page(self, page: Any) -> None:
        identity = id(page)
        if identity in self._watched_pages:
            return
        self._watched_pages.add(identity)
        try:
            page.on("close", self._wake_now)
            page.on("domcontentloaded", self._wake_now)
            page.on(
                "framenavigated",
                lambda frame: self._wake_now()
                if frame == getattr(page, "main_frame", None)
                else None,
            )
        except Exception:
            # The bounded reconciliation timer remains the fail-safe.
            self._watched_pages.discard(identity)

    def _install_event_wakes(self, browser: Any) -> None:
        for context in browser.contexts:
            for page in context.pages:
                self._watch_page(page)
            try:
                context.on(
                    "page",
                    lambda page: (self._watch_page(page), self._wake_now()),
                )
            except Exception:
                continue
        try:
            browser.on("disconnected", self._wake_now)
        except Exception:
            pass

    async def _wait_for_change(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
        except TimeoutError:
            self.metrics["timer_reconciliations"] += 1
        else:
            self.metrics["event_wakes"] += 1

    async def _read_binding(self, page: Any) -> dict[str, Any] | None:
        try:
            result = await page.evaluate(_BINDING_READ_SCRIPT)
            if not isinstance(result, dict):
                await page.evaluate(_BINDING_INSTALL_SCRIPT)
                result = await page.evaluate(_BINDING_READ_SCRIPT)
        except Exception:
            return None
        self.metrics["binding_reads"] += 1
        return result if isinstance(result, dict) else None

    async def _inspect_pages(self, browser: Any) -> list[tuple[Any, VisibleRoleState]]:
        rows: list[tuple[Any, VisibleRoleState]] = []
        for context in browser.contexts:
            for page in context.pages:
                if page.is_closed() or not supported_url(page.url):
                    continue
                result = await self._read_binding(page)
                if not result or not result.get("pageId") or not result.get("badgePresent"):
                    try:
                        result = await ensure_role_indicator(page)
                        self.metrics["indicator_repairs"] += 1
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
        live_ids = {state.page_id for _, state in rows}
        for stale_id in set(self._registry_signatures) - live_ids:
            self._registry_signatures.pop(stale_id, None)
        for page, state in rows:
            duplicates = role_pages.get(state.role or "", []) if state.role else []
            conflict = None
            if len(duplicates) > 1:
                ids = ", ".join(item.page_id[:8] for item in duplicates)
                conflict = f"Duplicate role {state.role}: tabs {ids}. Rename one role, e.g. {state.role}1."
            signature = (tuple(roles), conflict, state.role, state.task_id, state.url)
            if self._registry_signatures.get(state.page_id) == signature:
                self.metrics["registry_noops"] += 1
                continue
            try:
                applied = await page.evaluate(
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
            if applied is not False:
                self._registry_signatures[state.page_id] = signature
                self.metrics["registry_publications"] += 1

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
        self._install_event_wakes(browser)
        try:
            while browser.is_connected():
                self._wake.clear()
                await self.sync_once(browser)
                if self._wake.is_set():
                    self.metrics["event_wakes"] += 1
                    continue
                await self._wait_for_change()
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
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--reconnect", type=float, default=2.0)
    parser.add_argument("--event-log", default=".runtime/role-ui-events.jsonl")
    parser.add_argument("--once", action="store_true")
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
