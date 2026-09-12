from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import ipaddress
import re
from urllib.parse import urlparse, urlsplit, urlunsplit

from playwright.async_api import Browser, Playwright, async_playwright


def is_cdp_disconnect(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    needles = (
        "browser has been closed",
        "browser closed",
        "connection closed",
        "connection is closed",
        "target page, context or browser has been closed",
        "browser context has been closed",
        "websocket is not open",
    )
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in {"TargetClosedError", "BrowserDisconnectedError"}:
            return True
        if any(needle in str(current).lower() for needle in needles):
            return True
        current = current.__cause__ or current.__context__
    return False


def validate_cdp_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("CDP endpoint must be loopback HTTP")
    return url.rstrip("/")


def normalize_cdp_endpoint(value: str) -> str:
    """Normalize operator input without deriving browser identity from its address."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("CDP endpoint must be a port, host:port, or HTTP/WebSocket URL")
    text = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise ValueError("CDP endpoint must not contain whitespace")
    if text.isascii() and text.isdigit():
        text = f"http://127.0.0.1:{text}"
    shorthand = "://" not in text
    if shorthand:
        text = "http://" + text
    try:
        parsed = urlsplit(text)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("invalid CDP endpoint address") from exc
    if (
        parsed.scheme not in {"http", "https", "ws", "wss"}
        or not host or parsed.username is not None or parsed.password is not None
        or parsed.fragment or (shorthand and port is None)
        or parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("invalid CDP endpoint address")
    if ":" in host:
        try:
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        except ValueError as exc:
            raise ValueError("invalid CDP IPv6 address") from exc
    elif re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host) is None:
        raise ValueError("invalid CDP hostname")
    elif host.lower() == "localhost":
        host = "127.0.0.1"
    authority = host.lower() + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), parsed.query, ""))


def is_local_cdp_endpoint(value: str) -> bool:
    host = urlsplit(normalize_cdp_endpoint(value)).hostname
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def connect(url: str) -> tuple[Playwright, Browser]:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(
            normalize_cdp_endpoint(url),
            is_local=is_local_cdp_endpoint(url),
            no_defaults=True,
            timeout=10_000,
        )
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
