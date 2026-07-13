from urllib.parse import urlparse
from playwright.async_api import Browser, async_playwright

def validate_cdp_url(url:str)->str:
    parsed=urlparse(url)
    if parsed.scheme not in {"http","https"} or parsed.hostname not in {"127.0.0.1","localhost"}:
        raise ValueError("CDP endpoint must be loopback HTTP")
    return url.rstrip("/")

async def connect(url:str)->tuple[object,Browser]:
    manager=async_playwright()
    playwright=await manager.start()
    browser=await playwright.chromium.connect_over_cdp(validate_cdp_url(url))
    return manager,browser
