import pytest
from playwright_auto.connection import is_cdp_disconnect, validate_cdp_url
from playwright_auto.inspect import locator_candidates
from playwright_auto.screenshot import screenshot_options

def test_rejects_non_loopback_cdp():
    assert validate_cdp_url("http://127.0.0.1:9222")=="http://127.0.0.1:9222"
    with pytest.raises(ValueError): validate_cdp_url("http://0.0.0.0:9222")

def test_locator_priority():
    element={"role":"button","name":"Save","label":"Save changes","testid":"save","text":"Save"}
    assert locator_candidates(element)==[
        'get_by_role("button", name="Save")',
        'get_by_label("Save changes")',
        'get_by_test_id("save")',
        'get_by_text("Save")',
    ]

def test_screenshot_options():
    assert screenshot_options(True,None)=={"full_page":True}
    assert screenshot_options(False,"#main")=={"selector":"#main","full_page":False}


@pytest.mark.parametrize(
    "message",
    [
        "browser has been closed",
        "browser closed",
        "connection closed",
        "connection is closed",
        "target page, context or browser has been closed",
        "browser context has been closed",
        "websocket is not open",
    ],
)
def test_cdp_disconnect_message_variants_are_shared(message):
    inner = RuntimeError(message)
    outer = RuntimeError("wrapper")
    outer.__cause__ = inner
    assert is_cdp_disconnect(outer) is True


def test_cdp_disconnect_recognizes_nested_playwright_exception_names():
    target_closed = type("TargetClosedError", (RuntimeError,), {})("opaque")
    disconnected = type("BrowserDisconnectedError", (RuntimeError,), {})("opaque")
    outer = RuntimeError("wrapper")
    outer.__context__ = target_closed
    assert is_cdp_disconnect(outer) is True
    assert is_cdp_disconnect(disconnected) is True
    assert is_cdp_disconnect(RuntimeError("ordinary failure")) is False


def test_connected_browser_disconnects_without_closing_remote(monkeypatch):
    import asyncio
    import playwright_auto.connection as connection

    events = []

    class FakePlaywright:
        async def stop(self):
            events.append("playwright.stop")

    class FakeBrowser:
        async def close(self):
            events.append("browser.close")

    fake_browser = FakeBrowser()

    async def fake_connect(_url):
        return FakePlaywright(), fake_browser

    monkeypatch.setattr(connection, "connect", fake_connect)

    async def scenario():
        async with connection.connected_browser("http://127.0.0.1:9222") as browser:
            assert browser is fake_browser

    asyncio.run(scenario())
    assert events == ["playwright.stop"]
