from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto.chatgpt import (
    AuthenticationRequiredError,
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    ManualInputPendingError,
    MessageSnapshot,
    PageBinding,
    PageOwnershipError,
    RateLimitBlockedError,
)


def snapshot(
    *,
    url: str = "https://chatgpt.com/c/conversation-1",
    session_id: str = "conversation-1",
    page_id: str = "page-1",
    role: str = "DEV",
    task_id: str = "task-1",
    team: str = "team-1",
    composer_text: str = "",
    attachments: tuple[str, ...] = (),
    composer_present: bool = True,
) -> ChatGPTSnapshot:
    return ChatGPTSnapshot(
        url=url,
        session_id=session_id,
        page_id=page_id,
        page_role=role,
        page_task_id=task_id,
        page_team=team,
        state=ChatGPTState.NEW_CHAT,
        requires_login=False,
        composer_present=composer_present,
        composer_editable=True,
        composer_text=composer_text,
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=attachments,
        error_texts=(),
        messages=(MessageSnapshot("assistant", "a-1", "t-1", "ready", ()),),
    )


class FakePage:
    pass


def fake_page(**values):
    page = FakePage()
    for key, value in values.items():
        setattr(page, key, value)
    return page


def client_with_page(page) -> ChatGPTPage:
    client = ChatGPTPage(page, timeout_ms=20)
    client.binding = PageBinding("page-1", "DEV")
    return client


@pytest.mark.parametrize("timeout_type", [TimeoutError, pytest.importorskip("playwright.async_api").TimeoutError])
def test_clean_ready_preserves_timeout_type_and_chains_original_error(timeout_type):
    page = SimpleNamespace()
    client = client_with_page(page)
    original = timeout_type("ownership probe timed out")
    client.assert_ownership = AsyncMock(side_effect=original)

    with pytest.raises(timeout_type, match="clean-ready") as caught:
        asyncio.run(client.wait_until_clean_ready(timeout_ms=5, poll_ms=1))

    assert caught.value.__cause__ is original


@pytest.mark.parametrize(
    "semantic_error",
    [
        PageOwnershipError("wrong owner"),
        AuthenticationRequiredError("login required"),
        ManualInputPendingError("manual input"),
    ],
)
def test_clean_ready_never_wraps_semantic_errors_as_transient(semantic_error):
    page = SimpleNamespace()
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=semantic_error)

    with pytest.raises(type(semantic_error), match=str(semantic_error)) as caught:
        asyncio.run(client.wait_until_clean_ready(timeout_ms=5, poll_ms=1))

    assert caught.value is semantic_error


def test_clean_ready_classifies_canonical_cloudflare_challenge(monkeypatch):
    page = SimpleNamespace(url="https://chatgpt.com/c/conversation-1")
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=AssertionError("must not probe ownership"))

    monkeypatch.setattr(
        "playwright_auto.chatgpt.cloudflare_challenge_visible",
        AsyncMock(return_value=True),
    )

    with pytest.raises(RateLimitBlockedError, match="Cloudflare challenge"):
        asyncio.run(client.wait_until_clean_ready(timeout_ms=5, poll_ms=1))

    client.assert_ownership.assert_not_awaited()


def test_refresh_baseline_document_probe_is_bounded_and_never_reloads_on_timeout():
    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    before = snapshot()
    page = fake_page(reload=AsyncMock(), evaluate=AsyncMock(side_effect=hang))
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(return_value=before)
    client.send = AsyncMock()

    with pytest.raises(TimeoutError):
        asyncio.run(client.refresh(timeout_ms=5))

    page.reload.assert_not_awaited()
    client.send.assert_not_awaited()


def test_refresh_timeout_preserves_original_when_post_ownership_probe_hangs():
    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    before = snapshot()
    reload_error = TimeoutError("reload timed out")
    page = fake_page(
        reload=AsyncMock(side_effect=reload_error),
        evaluate=AsyncMock(
            return_value={
                "url": before.url,
                "time_origin": 100.0,
                "ready_state": "complete",
            }
        ),
    )
    ownership_calls = 0

    async def ownership(*_args, **_kwargs):
        nonlocal ownership_calls
        ownership_calls += 1
        if ownership_calls == 1:
            return before
        return await hang()

    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=ownership)
    client.send = AsyncMock()

    with pytest.raises(TimeoutError, match="reload timed out") as caught:
        asyncio.run(client.refresh(timeout_ms=5))

    assert caught.value is reload_error
    assert page.reload.await_count == 1
    client.send.assert_not_awaited()


@pytest.mark.parametrize("prior_origin", [None, float("nan"), float("inf"), "100"])
def test_refresh_timeout_rethrows_when_prior_document_origin_is_untrustworthy(prior_origin):
    before = snapshot()
    after = snapshot()
    page = fake_page(
        reload=AsyncMock(side_effect=TimeoutError("reload timed out")),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": prior_origin, "ready_state": "complete"},
                {"url": after.url, "time_origin": 200.0, "ready_state": "complete"},
            ]
        ),
    )
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=[before, after])

    with pytest.raises(TimeoutError, match="reload timed out"):
        asyncio.run(client.refresh())

    assert page.reload.await_count == 1


def test_refresh_timeout_rethrows_when_document_marker_is_unchanged():
    before = snapshot()
    page = fake_page(
        reload=AsyncMock(side_effect=TimeoutError("reload timed out")),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
            ]
        ),
    )
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=[before, before])
    client.send = AsyncMock()

    with pytest.raises(TimeoutError, match="reload timed out"):
        asyncio.run(client.refresh())

    assert page.reload.await_count == 1
    client.send.assert_not_awaited()


def test_refresh_timeout_accepts_proven_new_usable_document_with_exact_identity():
    before = snapshot()
    after = snapshot()
    page = fake_page(
        reload=AsyncMock(side_effect=TimeoutError("reload timed out")),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
                {"url": after.url, "time_origin": 200.0, "ready_state": "interactive"},
            ]
        ),
    )
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=[before, after])
    client.send = AsyncMock()

    asyncio.run(client.refresh())

    assert page.reload.await_count == 1
    assert client._owned_composer_text is None
    client.send.assert_not_awaited()


def test_refresh_timeout_rethrows_on_exact_identity_mismatch_even_after_commit():
    before = snapshot()
    after = snapshot(task_id="other-task")
    page = fake_page(
        reload=AsyncMock(side_effect=TimeoutError("reload timed out")),
        evaluate=AsyncMock(
            side_effect=[
                {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
                {"url": after.url, "time_origin": 200.0, "ready_state": "complete"},
            ]
        ),
    )
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(side_effect=[before, after])

    with pytest.raises(TimeoutError, match="reload timed out"):
        asyncio.run(client.refresh())

    assert page.reload.await_count == 1


@pytest.mark.parametrize("case", ["exact", "canonicalized", "unchanged", "mismatch", "no_acceptance"])
def test_send_reload_timeout_reconciles_committed_document_without_second_click(monkeypatch, case):
    from dataclasses import replace
    import playwright_auto.chatgpt as module

    before = snapshot(
        url="https://chatgpt.com/" if case == "canonicalized" else "https://chatgpt.com/c/conversation-1",
        session_id="WEB:page-1" if case == "canonicalized" else "conversation-1",
    )
    accepted = MessageSnapshot("user", "u-new", "t-new", "exact prompt", ())
    after = replace(
        snapshot(task_id="other" if case == "mismatch" else "task-1"),
        messages=before.messages + (() if case == "no_acceptance" else (accepted,)),
    )
    page = fake_page(
        reload=AsyncMock(side_effect=TimeoutError("reload timed out")),
        evaluate=AsyncMock(side_effect=[
            {"url": before.url, "time_origin": 100.0, "ready_state": "complete"},
            {"url": after.url, "time_origin": 100.0 if case == "unchanged" else 200.0, "ready_state": "complete"},
        ]),
    )
    client = client_with_page(page)
    # recovery_before (call 3) is the pre-reload state (same as before);
    # reconcile_committed_acceptance (call 4) gets the post-reload state.
    client.assert_ownership = AsyncMock(side_effect=[before, before, before, after])
    client._prepare_prompt_locked = AsyncMock()
    client._arm_frontend_identity_observer = lambda: None
    client._captured_conversation_id = lambda _user: None
    client._wait_send_acceptance = AsyncMock(return_value=None)
    click = AsyncMock()
    monkeypatch.setattr(module, "click_send_button", click)

    if case in {"exact", "canonicalized"}:
        receipt = asyncio.run(client.send("exact prompt", wait_for_stop=False))
        assert receipt.user_message_id == "u-new"
        assert receipt.accepted_via == "post_reload:exact_user_message"
        assert receipt.attempts == 1
    else:
        with pytest.raises(TimeoutError, match="reload timed out"):
            asyncio.run(client.send("exact prompt", wait_for_stop=False))
    assert click.await_count == 1
    assert page.reload.await_count == 1


def test_send_successful_reload_without_acceptance_never_clicks_again(monkeypatch):
    import playwright_auto.chatgpt as module

    before = snapshot()
    page = fake_page(
        reload=AsyncMock(),
        evaluate=AsyncMock(return_value={"url": before.url, "time_origin": 100.0}),
    )
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(return_value=before)
    client._prepare_prompt_locked = AsyncMock()
    client._arm_frontend_identity_observer = lambda: None
    client._wait_send_acceptance = AsyncMock(return_value=None)
    click = AsyncMock()
    monkeypatch.setattr(module, "click_send_button", click)

    with pytest.raises(module.SendRecoveryError):
        asyncio.run(client.send("exact prompt", wait_for_stop=False))
    assert click.await_count == 1
    assert page.reload.await_count == 1


def test_refresh_draft_protection_prevents_reload_and_send():
    before = snapshot(composer_text="operator draft")
    page = fake_page(reload=AsyncMock(), evaluate=AsyncMock())
    client = client_with_page(page)
    client.assert_ownership = AsyncMock(return_value=before)
    client.send = AsyncMock()

    with pytest.raises(ComposerConflictError, match="manual draft"):
        asyncio.run(client.refresh())

    page.reload.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    client.send.assert_not_awaited()
