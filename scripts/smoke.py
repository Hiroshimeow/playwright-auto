import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("data:text/html,<title>Playwright Auto</title><button id='agent'>Agent action visible</button>")
        await page.locator("#agent").click()
        output = Path(".runtime/smoke.png")
        await page.screenshot(path=str(output))
        print(f"title={await page.title()} screenshot={output}")
        await browser.close()

asyncio.run(main())
