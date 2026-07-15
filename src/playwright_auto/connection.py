from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from playwright.async_api import Browser, Playwright, async_playwright


def validate_cdp_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("CDP endpoint must be loopback HTTP")
    return url.rstrip("/")


async def connect(url: str) -> tuple[Playwright, Browser]:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(validate_cdp_url(url))
    except Exception:
        await playwright.stop()
        raise
    return playwright, browser


@asynccontextmanager
async def connected_browser(url: str) -> AsyncIterator[Browser]:
    """Attach to persistent Chromium and disconnect without closing Chromium."""
    playwright, browser = await connect(url)
    try:
        yield browser
    finally:
        # browser.close() sends Browser.close over CDP and kills the persistent process.
        await playwright.stop()
