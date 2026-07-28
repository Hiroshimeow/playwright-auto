from __future__ import annotations

import asyncio
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

_STORAGE_SCRIPT = """() => {
  const read = (key) => {
    try { return sessionStorage.getItem(key); } catch (_) { return null; }
  };
  return {
    url: location.href,
    title: document.title,
    role: read('playwright-auto:role'),
    page_id: read('playwright-auto:page-id'),
    task_id: read('playwright-auto:task-id'),
    team: read('playwright-auto:team')
  };
}"""


def _safe_url(value: object) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


async def inspect_page_metadata(page: Any) -> dict[str, Any]:
    if page.is_closed():
        return {
            "page_id": None,
            "url": "",
            "title": "",
            "role": None,
            "team": None,
            "task_id": None,
            "online": False,
            "supported": False,
        }
    try:
        raw = await page.evaluate(_STORAGE_SCRIPT)
    except Exception:
        raw = {
            "url": getattr(page, "url", ""),
            "title": "",
            "role": None,
            "page_id": None,
            "task_id": None,
            "team": None,
        }
    if not isinstance(raw, Mapping):
        raw = {}
    url = _safe_url(raw.get("url") or getattr(page, "url", ""))
    return {
        "page_id": str(raw.get("page_id") or "") or None,
        "url": url,
        "title": str(raw.get("title") or "")[:200],
        "role": str(raw.get("role") or "") or None,
        "team": str(raw.get("team") or "") or None,
        "task_id": str(raw.get("task_id") or "") or None,
        "online": True,
        "supported": url.startswith("https://chatgpt.com/") or url == "https://chatgpt.com",
    }


async def build_browser_projection(
    browser_context: Any,
    *,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    pages = await asyncio.gather(
        *(inspect_page_metadata(page) for page in list(getattr(browser_context, "pages", ())))
    )
    pages = sorted(pages, key=lambda item: (str(item.get("page_id") or ""), item["url"]))
    return {
        "connected": True,
        "page_count": len(pages),
        "pages": pages,
    }
