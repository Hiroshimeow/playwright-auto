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
    async def inner_text(self, **kwargs):
        return "Thinking effort" if self.page.menu_open else self.page.model
    async def click(self):
        self.page.clicks += 1
        self.page.menu_open = True


class Power:
    def __init__(self, page):
        self.page = page
    async def count(self): return self.page.power_count
    def locator(self, selector):
        assert selector == '[role="slider"]'
        return self
    async def press(self, key):
        if key == "Escape":
            self.page.menu_open = False
            return
        assert key == "ArrowRight"
        self.page.arrows += 1
        self.page.model = self.page.after_arrow


class Page:
    def __init__(self, model, *, count=1, power_count=1, after_arrow="Thinking effort High"):
        self.model, self.count, self.power_count = model, count, power_count
        self.after_arrow = after_arrow
        self.clicks = self.arrows = 0
        self.menu_open = False
    def get_by_role(self, role, *, name):
        assert (role, name) == ("button", "Select ChatGPT model")
        return Picker(self)
    def locator(self, selector):
        assert selector == '[role="menuitem"][aria-label="Power"]'
        return Power(self)


def probe(page, snapshot=None, *, gate_required=True, allow_change=True):
    client = ChatGPTPage(page)
    client.model_effort_gate_required = gate_required
    current = snapshot or owned()
    async def ownership(): return current
    client.assert_ownership = ownership
    result = asyncio.run(
        client._ensure_selfrepair_model_effort_locked(current, allow_change=allow_change)
    )
    return result, page


@pytest.mark.parametrize("value", [
    "Thinking effort High", "Thinking effort 5.6 Extra High",
    "Thinking effort Extra High", "Thinking effort Pro",
])
def test_high_or_extra_high_is_accepted_without_click(value):
    result, page = probe(Page(value))
    assert result in {"High", "Extra High", "Pro"}
    assert page.clicks == page.arrows == 0


@pytest.mark.parametrize("effort", ["High", "Extra High", "Pro"])
@pytest.mark.parametrize("version", ["", "5.6 ", "6 "])
def test_compact_picker_verifies_without_changing_model_at_send(effort, version):
    result, page = probe(Page(version + effort), owned(text="prepared prompt"), allow_change=False)
    assert result == effort
    assert page.clicks == page.arrows == 0


@pytest.mark.parametrize("prefix", ["Thinking effort ", ""])
def test_medium_switches_to_high_before_any_composer_fill(prefix):
    result, page = probe(Page(prefix + "Medium", after_arrow=prefix + "High"))
    assert result == "High"
    assert page.clicks == page.arrows == 1


@pytest.mark.parametrize("value,count", [
    ("Thinking effort Medium", 0),
    ("Thinking effort Medium", 2),
    ("Thinking effort Auto", 1),
    ("Unknown UI", 1),
    ("Not Extra High", 1),
    ("Thinking effort Not High", 1),
])
def test_model_unobservable_or_unknown_blocks_send(value,count):
    with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
        probe(Page(value, count=count))


def test_compact_below_high_fails_closed_at_send():
    page = Page("Medium", after_arrow="High")
    with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
        probe(page, allow_change=False)
    assert page.clicks == page.arrows == 0


@pytest.mark.parametrize("label, expected", [("6 Extra High", "Extra High"), ("Medium", None)])
def test_resume_effort_verification_reads_without_clicking(label, expected):
    page = Page(label)
    client = ChatGPTPage(page)
    client.model_effort_gate_required = True

    async def ownership():
        return owned(text="operator draft")

    client.assert_ownership = ownership
    if expected is None:
        with pytest.raises(UnsafePageStateError, match="model_verification_blocked"):
            asyncio.run(client.verify_model_effort())
    else:
        assert asyncio.run(client.verify_model_effort()) == expected
    assert page.clicks == page.arrows == 0


@pytest.mark.parametrize(
    "page, reason",
    [
        (Page("Medium", power_count=0), "Power menu unavailable"),
        (Page("Medium", after_arrow="Medium"), "High selection not observable"),
    ],
)
def test_model_selection_failure_identifies_which_check_failed(page, reason):
    with pytest.raises(UnsafePageStateError, match=reason):
        probe(page)


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
