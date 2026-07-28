from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import playwright_auto.chatgpt as chatgpt
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    WaitProbe,
)


def snapshot(*, stop: bool = True, assistant_text: str = "") -> ChatGPTSnapshot:
    messages = (
        MessageSnapshot("user", "user-1", "turn-user", "prompt", ()),
        MessageSnapshot("assistant", "assistant-1", "turn-assistant", assistant_text, ()),
    )
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/test",
        session_id="test",
        page_id="page-1",
        page_role="DEV",
        state=ChatGPTState.RESPONDING if stop else ChatGPTState.WAITING_PROMPT,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=not stop,
        send_enabled=not stop,
        stop_visible=stop,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=messages,
        response_activity_text=assistant_text,
        response_activity_length=len(assistant_text),
        response_activity_turn_id="turn-assistant",
    )


def probe(*, stop: bool = True, length: int = 12, transport_active: bool = True) -> WaitProbe:
    return WaitProbe(
        url="https://chatgpt.com/c/test",
        session_id="test",
        page_id="page-1",
        page_role="DEV",
        page_task_id="task-1",
        page_team="alpha",
        requires_login=False,
        composer_present=True,
        composer_text="",
        attachment_count=0,
        stop_visible=stop,
        transport_active=transport_active,
        error_texts=(),
        blocking_dialogs=(),
        choice_prompt_labels=(),
        last_user_message_id="user-1",
        last_user_turn_id="turn-user",
        last_assistant_message_id="assistant-1",
        last_assistant_turn_id="turn-assistant",
        assistant_text_length=length,
        assistant_text_tail="x" * min(length, 32),
        response_activity_length=length,
        response_activity_tail="x" * min(length, 32),
        response_activity_turn_id="turn-assistant",
    )


def receipt() -> SendReceipt:
    return SendReceipt(
        prompt="prompt",
        prompt_sha256=chatgpt.prompt_digest("prompt"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="test",
        user_message_id="user-1",
        user_turn_id="turn-user",
    )


def test_wait_probe_installs_once_and_reuses_tiny_reader():
    raw = {
        "url": "https://chatgpt.com/c/session",
        "page_role": "DEV",
        "page_id": "page-1",
        "page_task_id": "task-1",
        "page_team": "team-1",
        "requires_login": False,
        "composer_present": True,
        "composer_text": "",
        "attachment_count": 0,
        "stop_visible": True,
        "transport_active": True,
        "error_texts": [],
        "blocking_dialogs": [],
        "choice_prompt_labels": [],
        "last_user_message_id": "u1",
        "last_user_turn_id": "ut1",
        "last_assistant_message_id": "a1",
        "last_assistant_turn_id": "at1",
        "assistant_text_length": 10,
        "assistant_text_tail": "working",
        "response_activity_length": 10,
        "response_activity_tail": "working",
        "response_activity_turn_id": "at1",
    }

    class Page:
        url = raw["url"]

        def __init__(self):
            self.installed = False
            self.calls = []

        async def evaluate(self, script, argument=None):
            self.calls.append((script, argument))
            if script == chatgpt._WAIT_PROBE_READ_SCRIPT:
                return raw if self.installed else None
            if script == chatgpt._WAIT_PROBE_WAIT_SCRIPT:
                assert argument == [first.transition_signature, 12_000, first.to_raw()]
                return raw if self.installed else None
            assert script == chatgpt._WAIT_PROBE_INSTALL_SCRIPT
            self.installed = True
            return True

    page = Page()
    first = asyncio.run(chatgpt.inspect_chatgpt_wait_probe(page))
    second = asyncio.run(chatgpt.inspect_chatgpt_wait_probe(page))
    waited = asyncio.run(
        chatgpt.inspect_chatgpt_wait_probe(
            page,
            previous_transition_signature=first.transition_signature,
            previous_probe=first,
            wait_ms=12_000,
        )
    )

    assert first == second == waited
    assert [script for script, _argument in page.calls].count(chatgpt._WAIT_PROBE_INSTALL_SCRIPT) == 1
    assert [script for script, _argument in page.calls].count(chatgpt._WAIT_PROBE_READ_SCRIPT) == 3
    assert [script for script, _argument in page.calls].count(chatgpt._WAIT_PROBE_WAIT_SCRIPT) == 1
    assert len(chatgpt._WAIT_PROBE_READ_SCRIPT) < 100
    assert len(chatgpt._WAIT_PROBE_WAIT_SCRIPT) < 160


def test_unchanged_wait_ticks_reuse_one_full_snapshot(monkeypatch: pytest.MonkeyPatch):
    page = object()
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")
    probes = [probe(), probe(), probe()]
    full_calls = 0

    async def fake_probe(_page, **_kwargs):
        return probes.pop(0)

    async def fake_full(_snapshot=None, *, require_binding=True):
        nonlocal full_calls
        full_calls += 1
        return snapshot(stop=True, assistant_text="partial")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)
    monkeypatch.setattr(client, "assert_ownership", fake_full)

    values = asyncio.run(_collect(client, receipt(), 3))

    assert full_calls == 1
    assert all(value.messages[-1].text == "partial" for value in values)
    assert client.wait_metrics["sparse_probes"] == 3
    assert client.wait_metrics["full_snapshots"] == 1
    assert client.wait_metrics["unchanged_ticks"] == 2


async def _collect(client: ChatGPTPage, value: SendReceipt, count: int):
    return [await client.wait_snapshot(value) for _ in range(count)]



def test_growing_streaming_dom_stays_bounded_and_reuses_one_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    client = ChatGPTPage(object())
    client.binding = PageBinding("page-1", "DEV")
    probes = [probe(length=step) for step in range(256, 16_385, 256)]
    full_calls = 0

    async def fake_probe(_page, **_kwargs):
        return probes.pop(0)

    async def fake_full(_snapshot=None, *, require_binding=True):
        nonlocal full_calls
        full_calls += 1
        return snapshot(stop=True, assistant_text="initial bounded snapshot")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)
    monkeypatch.setattr(client, "assert_ownership", fake_full)

    values = asyncio.run(_collect(client, receipt(), 64))

    assert full_calls == 1
    assert client.wait_metrics["full_snapshots"] == 1
    assert client.wait_metrics["unchanged_ticks"] == 63
    assert max(len(value.response_activity_text) for value in values) <= 160
    assert values[-1].response_activity_length == 16_384
    assert all(value.messages[-1].text == "initial bounded snapshot" for value in values)

def test_wait_cache_is_shared_across_wrappers_for_same_physical_page(
    monkeypatch: pytest.MonkeyPatch,
):
    class Page:
        pass

    page = Page()
    first = ChatGPTPage(page)
    second = ChatGPTPage(page)
    first.binding = second.binding = PageBinding("page-1", "DEV")
    probes = [probe(), probe()]
    full_calls = 0
    observed = []

    async def fake_probe(_page, **kwargs):
        observed.append(kwargs)
        return probes.pop(0)

    async def first_full(_snapshot=None, *, require_binding=True):
        nonlocal full_calls
        full_calls += 1
        return snapshot(stop=True, assistant_text="partial")

    async def unexpected_full(_snapshot=None, *, require_binding=True):
        raise AssertionError("shared physical-page cache should avoid a second full snapshot")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)
    monkeypatch.setattr(first, "assert_ownership", first_full)
    monkeypatch.setattr(second, "assert_ownership", unexpected_full)

    first_value = asyncio.run(first.wait_snapshot(receipt()))
    second_value = asyncio.run(second.wait_snapshot(receipt(), probe_wait_ms=12_000))

    assert first_value.messages[-1].text == second_value.messages[-1].text == "partial"
    assert full_calls == 1
    assert observed[1]["previous_probe"] is not None
    assert observed[1]["wait_ms"] == 12_000


def test_steady_wait_reconciles_full_snapshot_only_after_600_seconds(
    monkeypatch: pytest.MonkeyPatch,
):
    client = ChatGPTPage(object())
    client.binding = PageBinding("page-1", "DEV")
    probes = [probe(), probe(), probe()]
    full_calls = 0
    clock = iter((0.0, 300.0, 601.0, 601.0))

    async def fake_probe(_page, **_kwargs):
        return probes.pop(0)

    async def fake_full(_snapshot=None, *, require_binding=True):
        nonlocal full_calls
        full_calls += 1
        return snapshot(stop=True, assistant_text="partial")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)
    monkeypatch.setattr(client, "assert_ownership", fake_full)
    monkeypatch.setattr(chatgpt, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    asyncio.run(_collect(client, receipt(), 3))

    assert full_calls == 2
    assert client.wait_metrics["full_snapshot_reasons"]["forced"] == 1
    assert client.wait_metrics["full_snapshot_reasons"]["safety_interval"] == 1
    assert client.wait_metrics["unchanged_ticks"] == 1


def test_completion_transition_forces_full_snapshot(monkeypatch: pytest.MonkeyPatch):
    client = ChatGPTPage(object())
    client.binding = PageBinding("page-1", "DEV")
    probes = [probe(), probe(stop=False, length=20, transport_active=False)]
    full_values = [
        snapshot(stop=True, assistant_text="partial"),
        snapshot(stop=False, assistant_text="complete answer"),
    ]

    async def fake_probe(_page, **_kwargs):
        return probes.pop(0)

    async def fake_full(_snapshot=None, *, require_binding=True):
        return full_values.pop(0)

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)
    monkeypatch.setattr(client, "assert_ownership", fake_full)

    first, second = asyncio.run(_collect(client, receipt(), 2))

    assert first.stop_visible is True
    assert second.stop_visible is False
    assert second.messages[-1].text == "complete answer"
    assert client.wait_metrics["full_snapshots"] == 2
    assert client.wait_metrics["full_snapshot_reasons"]["transport_completed"] == 1


def test_wait_poll_backoff_is_bounded_and_resets_on_progress():
    assert ChatGPTPage._adaptive_wait_seconds(100, 0) == pytest.approx(0.1)
    assert ChatGPTPage._adaptive_wait_seconds(100, 3) == pytest.approx(0.1)
    assert ChatGPTPage._adaptive_wait_seconds(100, 4) == pytest.approx(0.15)
    assert ChatGPTPage._adaptive_wait_seconds(100, 20) == pytest.approx(0.5)


def test_sparse_probe_ownership_change_fails_closed(monkeypatch: pytest.MonkeyPatch):
    client = ChatGPTPage(object())
    client.binding = PageBinding("page-1", "DEV")
    wrong = replace(probe(), page_role="REVIEW")

    async def fake_probe(_page, **_kwargs):
        return wrong

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_wait_probe", fake_probe)

    with pytest.raises(chatgpt.PageOwnershipError, match="logical role changed"):
        asyncio.run(client.wait_snapshot(receipt()))
