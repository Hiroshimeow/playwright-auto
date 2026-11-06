"""G6 self-repair: model effort must be observed on the exact task tab."""
import asyncio
from types import SimpleNamespace

import pytest

from playwright_auto.chatgpt import ChatGPTPage, UnsafePageStateError


def owned(team="g6-selfrepair-09-07-regression-retry1", text=""):
    return SimpleNamespace(
        page_team=team, page_task_id="task-g6", page_id="tab-g6",
        page_role="DEV", composer_text=text, attachment_markers=(),
    )


class Picker:
    def __init__(self, page):
        self.page = page
    async def count(self): return self.page.count
    async def is_visible(self): return self.page.count == 1
    async def inner_text(self, **kwargs): return self.page.model
    async def click(self): self.page.clicks += 1


class Power:
    def __init__(self, page):
        self.page = page
    async def count(self): return self.page.power_count
    def locator(self, selector):
        assert selector == '[role="slider"]'
        return self
    async def press(self, key):
        assert key == "ArrowRight"
        self.page.arrows += 1
        self.page.model = "Thinking effort High"


class Page:
    def __init__(self, model, *, count=1, power_count=1):
        self.model, self.count, self.power_count = model, count, power_count
        self.clicks = self.arrows = 0
    def get_by_role(self, role, *, name):
        assert (role, name) == ("button", "Select ChatGPT model")
        return Picker(self)
    def locator(self, selector):
        assert selector == '[role="menuitem"][aria-label="Power"]'
        return Power(self)


def probe(page, snapshot=None, *, gate_required=True):
    client = ChatGPTPage(page)
    client.model_effort_gate_required = gate_required
    current = snapshot or owned()
    async def ownership(): return current
    client.assert_ownership = ownership
    return asyncio.run(client._ensure_selfrepair_model_effort_locked(current)), page


@pytest.mark.parametrize("value", [
    "Thinking effort High", "Thinking effort 5.6 Extra High",
    "Thinking effort Extra High",
])
def test_high_or_extra_high_is_accepted_without_click(value):
    result, page = probe(Page(value))
    assert result in {"High", "Extra High"}
    assert page.clicks == page.arrows == 0


def test_medium_switches_to_high_before_any_composer_fill():
    result, page = probe(Page("Thinking effort Medium"))
    assert result == "High"
    assert page.clicks == page.arrows == 1


@pytest.mark.parametrize("value,count", [
    ("Thinking effort Medium", 0),
    ("Thinking effort Medium", 2),
    ("Thinking effort Auto", 1),
    ("Unknown UI", 1),
])
def test_model_unobservable_or_unknown_blocks_send(value,count):
    with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
        probe(Page(value, count=count))


def test_model_change_refused_with_unowned_draft():
    with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
        probe(Page("Thinking effort Medium"), owned(text="human draft"))


def test_runtime_without_model_gate_is_untouched_even_for_g6_team():
    result, page = probe(
        Page("Thinking effort Medium"),
        owned(team="g6-jobs-fresh-smoke-20261029"),
        gate_required=False,
    )
    assert result is None
    assert page.clicks == page.arrows == 0


def test_g6_runtime_gates_manual_nonprefixed_team():
    result, page = probe(Page("Thinking effort Medium"), owned(team="jobs-free-freelance-20261028"))
    assert result == "High"
    assert page.clicks == page.arrows == 1


def test_g6_jobs_solo_requires_model_gate_before_composer_fill():
    result, page = probe(Page("Thinking effort Medium"), owned(team="g6-jobs-fresh-smoke-20261029"))
    assert result == "High"
    assert page.clicks == page.arrows == 1


def test_g6_jobs_unverifiable_model_fails_closed():
    with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
        probe(Page("Unknown UI"), owned(team="g6-jobs-fresh-smoke-20261029"))
