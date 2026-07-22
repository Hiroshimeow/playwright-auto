import asyncio

import pytest

import playwright_auto.chatgpt as chatgpt


class FakeLocator:
    def __init__(self, page, name="locator"):
        self.page = page
        self.name = name
        self.first = self
        self.last = self

    def filter(self, **_kwargs):
        return self

    def get_by_role(self, _role, **_kwargs):
        return FakeLocator(self.page, "delete_button")

    async def wait_for(self, **_kwargs):
        self.page.events.append(f"wait:{self.name}")

    async def is_visible(self):
        return True

    async def click(self, **_kwargs):
        self.page.events.append(f"click:{self.name}")
        if self.name == "delete_button":
            self.page.url = "https://chatgpt.com/"


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, shortcut):
        self.page.events.append(f"shortcut:{shortcut}")


class FakePage:
    def __init__(self, *, evaluate_result=None):
        self.events = []
        self.evaluate_result = evaluate_result
        self.url = "https://chatgpt.com/c/test-session"
        self.keyboard = FakeKeyboard(self)

    async def evaluate(self, *_args, **_kwargs):
        self.events.append("evaluate")
        return self.evaluate_result

    async def bring_to_front(self):
        self.events.append("front")

    async def goto(self, url, **_kwargs):
        self.events.append("goto")
        self.url = url

    async def wait_for_function(self, *_args, **_kwargs):
        self.events.append("wait:function")

    def locator(self, selector):
        if selector == chatgpt.SELECTORS["composer"]:
            return FakeLocator(self, "composer")
        return FakeLocator(self, "dialog")


async def _record_delay(events, multiplier):
    events.append(f"delay:{multiplier}")
    return float(multiplier)


def test_configure_action_delays_updates_shared_range_and_multipliers(monkeypatch):
    monkeypatch.setattr(chatgpt.random, "uniform", lambda low, high: (low + high) / 2)
    try:
        chatgpt.configure_action_delays(
            2.0,
            3.0,
            {"send": 2, "refresh": 6, "open_tab": 7},
        )
        assert chatgpt.sample_random_delay(chatgpt.action_delay_multiplier("send")) == pytest.approx(5.0)
        assert chatgpt.SEND_DELAY_MULTIPLIER == 2
        assert chatgpt.action_delay_multiplier("refresh") == 6
        assert chatgpt.action_delay_multiplier("open_tab") == 7
        assert chatgpt.action_delay_multiplier("new_chat") == 4
    finally:
        chatgpt.configure_action_delays()


def test_configure_action_delays_rejects_unknown_operation():
    with pytest.raises(ValueError, match="unknown action delay"):
        chatgpt.configure_action_delays(multipliers={"mystery": 1})


def test_configure_random_delay_changes_global_range(monkeypatch):
    monkeypatch.setattr(chatgpt.random, "uniform", lambda low, high: (low + high) / 2)
    chatgpt.configure_random_delay(1.0, 1.5)

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(chatgpt.asyncio, "sleep", fake_sleep)
    actual = asyncio.run(chatgpt.random_delay(3))

    assert actual == pytest.approx(3.75)
    assert slept == [pytest.approx(3.75)]


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(0, 1.5), (1.5, 1.0), (1.0, 0), (-1, 1)],
)
def test_configure_random_delay_rejects_invalid_ranges(minimum, maximum):
    with pytest.raises(ValueError):
        chatgpt.configure_random_delay(minimum, maximum)


def test_send_waits_three_units_before_click(monkeypatch):
    page = FakePage(evaluate_result={"ok": True, "method": "dom_click"})

    async def fake_delay(_page, action, multiplier):
        page.events.append(f"delay:{action}:{multiplier}")
        return float(multiplier)

    async def fake_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", fake_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record)
    method = asyncio.run(chatgpt.click_send_button(page))

    assert method == "dom_click"
    assert page.events[:2] == [f"delay:send:{chatgpt.SEND_DELAY_MULTIPLIER}", "evaluate"]


def test_new_chat_waits_three_units_before_navigation(monkeypatch):
    page = FakePage(evaluate_result=False)

    async def fake_delay(_page, action, multiplier):
        page.events.append(f"delay:{action}:{multiplier}")
        return float(multiplier)

    async def fake_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", fake_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record)
    method = asyncio.run(chatgpt.open_new_chat(page))

    assert method == "navigate"
    expected = f"delay:new_chat:{chatgpt.NAVIGATION_DELAY_MULTIPLIER}"
    assert page.events[0] == expected
    assert page.events.index(expected) < page.events.index("evaluate")


def test_delete_chat_is_split_into_open_and_confirm(monkeypatch):
    page = FakePage()

    async def fake_delay(_page, action, multiplier):
        page.events.append(f"delay:{action}:{multiplier}")
        return float(multiplier)

    async def fake_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", fake_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record)

    asyncio.run(chatgpt.open_delete_chat_dialog(page))
    dialog_delay = f"delay:delete_dialog:{chatgpt.DIALOG_DELAY_MULTIPLIER}"
    shortcut = f"shortcut:{chatgpt.SHORTCUTS['delete_chat']}"
    assert dialog_delay in page.events
    assert shortcut in page.events
    assert page.events.index(dialog_delay) < page.events.index(shortcut)
    assert "click:delete_button" not in page.events

    result = asyncio.run(chatgpt.confirm_delete_chat(page))
    delete_delay = f"delay:delete_confirm:{chatgpt.DELETE_DELAY_MULTIPLIER}"
    assert delete_delay in page.events
    assert page.events.index(delete_delay) < page.events.index(
        "click:delete_button"
    )
    assert result == "https://chatgpt.com/"


def test_stop_does_not_use_human_delay(monkeypatch):
    page = FakePage(evaluate_result=True)

    async def forbidden_delay(*_args, **_kwargs):
        raise AssertionError("Stop must remain immediate")

    async def fake_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", forbidden_delay, raising=False)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record)
    assert asyncio.run(chatgpt.stop_response(page)) == "dom"


def test_delete_shortcut_matches_verified_runtime_key():
    assert chatgpt.SHORTCUTS["delete_chat"] == "Control+Shift+Delete"


def test_action_delay_emits_countdown_and_ready(monkeypatch):
    page = FakePage()
    recorded = []
    slept = []
    monkeypatch.setattr(chatgpt.random, "uniform", lambda _low, _high: 1.25)

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def fake_record(_page, action, phase, **extra):
        recorded.append((action, phase, extra))

    monkeypatch.setattr(chatgpt.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record, raising=False)

    actual = asyncio.run(chatgpt.action_delay(page, "send", 3))

    assert actual == pytest.approx(3.75)
    assert slept == [pytest.approx(3.75)]
    assert recorded == [
        ("send", "delay", {"delay_seconds": pytest.approx(3.75)}),
        ("send", "ready", {"delay_seconds": pytest.approx(3.75)}),
    ]


def test_send_records_click_and_complete(monkeypatch):
    page = FakePage(evaluate_result={"ok": True, "method": "dom_click"})
    recorded = []

    async def fake_action_delay(_page, action, multiplier):
        page.events.append(f"action-delay:{action}:{multiplier}")
        return 3.5

    async def fake_record(_page, action, phase, **extra):
        recorded.append((action, phase, extra))

    monkeypatch.setattr(chatgpt, "action_delay", fake_action_delay, raising=False)
    monkeypatch.setattr(chatgpt, "record_page_action", fake_record, raising=False)

    assert asyncio.run(chatgpt.click_send_button(page)) == "dom_click"
    assert page.events[0] == f"action-delay:send:{chatgpt.SEND_DELAY_MULTIPLIER}"
    assert [item[:2] for item in recorded] == [("send", "click"), ("send", "complete")]
    assert recorded[-1][2] == {"detail": "dom_click"}
