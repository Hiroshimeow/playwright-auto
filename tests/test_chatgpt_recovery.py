import asyncio
from dataclasses import replace

import pytest

import playwright_auto.chatgpt as chatgpt
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ChoicePromptBlockedError,
    ManualInputPendingError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    SendRecoveryError,
    looks_incomplete_response,
    prompt_digest,
    response_transport_ui_active,
)


def make_snapshot(
    *,
    messages=(),
    composer_text="",
    composer_present=True,
    composer_editable=True,
    stop_visible=False,
    choice_labels=(),
    attachments=(),
    requires_login=False,
    state=ChatGPTState.WAITING_PROMPT,
    response_activity_turn_id=None,
    response_activity_text="",
    response_activity_structure="",
    response_activity_length=0,
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/test-session",
        session_id="test-session",
        page_id="page-1",
        page_role="DEV",
        state=state,
        requires_login=requires_login,
        composer_present=composer_present,
        composer_editable=composer_editable,
        composer_text=composer_text,
        send_visible=False,
        send_enabled=False,
        stop_visible=stop_visible,
        blocking_dialogs=(),
        attachment_markers=tuple(attachments),
        error_texts=(),
        messages=tuple(messages),
        choice_prompt_labels=tuple(choice_labels),
        response_activity_turn_id=response_activity_turn_id,
        response_activity_text=response_activity_text,
        response_activity_structure=response_activity_structure,
        response_activity_length=response_activity_length,
    )


class SequencePage:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0
        self.inspect_calls = 0
        self.reload_calls = 0

    def next_snapshot(self):
        self.inspect_calls += 1
        if self.index < len(self.snapshots) - 1:
            current = self.snapshots[self.index]
            self.index += 1
            return current
        return self.snapshots[-1]


def receipt(*, accepted_via="exact_user_message"):
    return SendReceipt(
        prompt="expected prompt",
        prompt_sha256=prompt_digest("expected prompt"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(
            frozenset(), frozenset(), frozenset(), frozenset()
        ),
        attempts=1,
        accepted_via=accepted_via,
        session_id_before=None,
    )


def conversation(answer: str, *, stop=False, image_count=0):
    return make_snapshot(
        messages=(
            MessageSnapshot("user", "u2", "t2", "expected prompt", ()),
            MessageSnapshot(
                "assistant", "a2", "t2", answer, (), image_count=image_count
            ),
        ),
        stop_visible=stop,
        state=ChatGPTState.RESPONDING if stop else ChatGPTState.WAITING_PROMPT,
    )


def bind(page):
    from unittest.mock import AsyncMock

    client = ChatGPTPage(page, timeout_ms=30)
    client.binding = PageBinding("page-1", "DEV")
    client.wait_for_page_observation = AsyncMock(return_value=False)
    return client


def install_sequence(monkeypatch, page):
    async def fake_inspect(_page):
        value = page.next_snapshot()
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)


def test_incomplete_response_detector_covers_code_fence_and_json():
    assert looks_incomplete_response("```python\nprint('x')") is True
    assert looks_incomplete_response('{"route": "DEV"') is True
    assert looks_incomplete_response('{"route": "DEV"}') is False
    assert looks_incomplete_response("normal final response") is False


def test_transient_marker_inside_completed_report_is_not_transport_activity():
    final_report = (
        "DEV report\n\n"
        "ThinkBook returned 400: We couldn't connect your account. Please try again.\n\n"
        "Implementation and verification are complete.\n"
        '{"route":"PLAN","handoff":"INLINE"}'
    )
    snapshot = conversation(final_report)

    assert looks_incomplete_response(final_report) is False
    assert response_transport_ui_active(snapshot) is False
    assert looks_incomplete_response("We couldn't connect. Please try again.") is True


def test_wait_response_resets_stability_when_text_changes(monkeypatch):
    page = SequencePage(
        [
            conversation("draft answer"),
            conversation("final answer"),
            conversation("final answer"),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(), timeout_ms=40, stable_ms=3, poll_ms=1
        )
    )

    assert result.text == "final answer"
    assert page.inspect_calls >= 3


def test_post_reload_first_stale_snapshot_requires_confirmation(monkeypatch):
    page = SequencePage(
        [
            conversation("stale post-reload answer"),
            conversation("final recovered answer"),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(accepted_via="post_reload:stop_button"),
            timeout_ms=30,
            stable_ms=0,
            poll_ms=1,
        )
    )

    assert result.text == "final recovered answer"
    assert page.inspect_calls == 2






def test_manual_composer_input_never_triggers_recovery_reload(monkeypatch):
    snapshot = conversation("final response")
    snapshot = make_snapshot(
        messages=snapshot.messages,
        composer_text="manual steering",
        state=ChatGPTState.DRAFT,
    )
    page = SequencePage([snapshot])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=8,
            stable_ms=0,
            poll_ms=1,
            active_reload_after_ms=1,
        )
    )
    assert result.text == "final response"
    assert page.reload_calls == 0


@pytest.mark.parametrize("progressing,expected_reloads", [(True, 0), (False, 1)])
def test_active_reload_clock_tracks_bound_assistant_progress(monkeypatch, progressing, expected_reloads):
    clock = [0.0]
    page = SequencePage([conversation("draft", stop=True)])
    client = bind(page)
    texts = iter(["draft", "draft", "draft growing"] if progressing else ["draft"])

    async def read(_receipt, **_kwargs):
        text = next(texts, "draft growing" if progressing else "draft")
        return conversation(text, stop=True)

    async def sleep(_seconds):
        clock[0] += 60

    async def refresh(_page, **_kwargs):
        page.reload_calls += 1

    client.wait_snapshot = read
    monkeypatch.setattr(chatgpt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(chatgpt.asyncio, "sleep", sleep)
    monkeypatch.setattr(chatgpt, "refresh_page", refresh)
    with pytest.raises(TimeoutError, match="remained active"):
        asyncio.run(client.wait_for_response(
            replace(receipt(), user_message_id="u2", user_turn_id="t2"),
            timeout_ms=181_000, stable_ms=0, poll_ms=1,
            active_reload_after_ms=120_000, reload_wait_ms=0,
        ))
    assert page.reload_calls == expected_reloads


def test_choice_prompt_fails_closed_without_explicit_resolution(monkeypatch):
    page = SequencePage(
        [
            make_snapshot(
                composer_present=False,
                composer_editable=False,
                choice_labels=("Continue",),
                state=ChatGPTState.UNKNOWN,
            )
        ]
    )
    install_sequence(monkeypatch, page)

    with pytest.raises(ChoicePromptBlockedError, match="Continue"):
        asyncio.run(
            bind(page).wait_for_response(
                receipt(), timeout_ms=8, stable_ms=0, poll_ms=1
            )
        )


def test_image_only_assistant_response_is_valid(monkeypatch):
    page = SequencePage([conversation("", image_count=1)])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(), timeout_ms=20, stable_ms=0, poll_ms=1
        )
    )

    assert result.image_count == 1
    assert result.text == ""


def test_wait_clean_ready_tolerates_transient_snapshot_error(monkeypatch):
    page = SequencePage(
        [
            RuntimeError("temporary evaluate failure"),
            make_snapshot(state=ChatGPTState.NEW_CHAT),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_until_clean_ready(timeout_ms=20, poll_ms=1)
    )

    assert result.state is ChatGPTState.NEW_CHAT
    assert page.inspect_calls == 2


def test_page_observer_wait_consumes_dom_or_network_wake():
    from playwright_auto.role_runtime.page_observer import PageObserver

    observer = PageObserver(type("Page", (), {"url": "https://chatgpt.com/c/test"})())

    async def exercise():
        observer.network_wake.set()
        assert await observer.wait(0.1) is True
        assert observer.network_wake.is_set() is False
        observer.dom_wake.set()
        assert await observer.wait(0.1) is True
        assert observer.dom_wake.is_set() is False

    asyncio.run(exercise())


def test_send_acceptance_rejects_generic_non_response_dom_change(monkeypatch):
    from unittest.mock import AsyncMock

    page = SequencePage([make_snapshot(composer_text="unrelated draft")])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client.wait_for_page_observation = AsyncMock(return_value=True)

    result = asyncio.run(
        client._wait_send_acceptance(
            receipt().baseline,
            make_snapshot(),
            timeout_ms=5,
        )
    )

    assert result is None
    assert client.wait_for_page_observation.await_count >= 1
    assert all(
        set(call.kwargs) == {"timeout_ms"}
        for call in client.wait_for_page_observation.await_args_list
    )



def test_send_acceptance_observer_wake_does_not_make_assistant_authoritative(monkeypatch):
    from unittest.mock import AsyncMock

    page = SequencePage([
        make_snapshot(
            messages=(MessageSnapshot("assistant", "a-new", "t-new", "working", ()),)
        )
    ])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client.wait_for_page_observation = AsyncMock(return_value=True)

    result = asyncio.run(
        client._wait_send_acceptance(receipt().baseline, make_snapshot(), timeout_ms=20)
    )

    assert result is None
    assert client.wait_for_page_observation.await_count >= 1


def test_send_acceptance_activity_wake_is_not_acceptance_authority(monkeypatch):
    from unittest.mock import AsyncMock

    page = SequencePage([make_snapshot(response_activity_structure="streaming")])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client.wait_for_page_observation = AsyncMock(return_value=False)

    result = asyncio.run(
        client._wait_send_acceptance(receipt().baseline, make_snapshot(), timeout_ms=20)
    )

    assert result is None
    assert client.wait_for_page_observation.await_count >= 1


def test_send_acceptance_rejects_visible_stop_without_exact_user(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot()
    page = SequencePage([make_snapshot(stop_visible=True)])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, pre_send, timeout_ms=20)
    )

    assert result is None

def test_send_acceptance_rejects_preexisting_stop_control(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot(stop_visible=True)
    page = SequencePage([make_snapshot(stop_visible=True)])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(
            baseline,
            pre_send,
            timeout_ms=5,
        )
    )

    assert result is None



def test_send_acceptance_rejects_new_response_activity_without_exact_user(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot(response_activity_turn_id="old-turn")
    page = SequencePage([
        make_snapshot(
            response_activity_turn_id="new-turn",
            response_activity_text="working",
            response_activity_length=7,
        )
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, pre_send, timeout_ms=20)
    )

    assert result is None


def test_send_acceptance_rejects_new_activity_text_without_exact_user(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot(response_activity_text="old", response_activity_length=3)
    page = SequencePage([
        make_snapshot(response_activity_text="new output", response_activity_length=10)
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, pre_send, timeout_ms=20)
    )

    assert result is None

def test_send_acceptance_rejects_disappearing_stale_activity(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot(response_activity_text="old", response_activity_length=3)
    page = SequencePage([make_snapshot()])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, pre_send, timeout_ms=5)
    )

    assert result is None



def test_send_acceptance_rejects_streaming_structure_without_exact_user(monkeypatch):
    baseline = receipt().baseline
    page = SequencePage([make_snapshot(response_activity_structure="streaming")])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, make_snapshot(), timeout_ms=20)
    )

    assert result is None

def test_send_acceptance_rejects_listen_streaming_without_dom_pair(monkeypatch):
    baseline = receipt().baseline
    pre_send = make_snapshot()
    page = SequencePage([make_snapshot(stop_visible=False)])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client.page_observation = lambda: {"status": "IS_STREAMING"}

    result = asyncio.run(
        client._wait_send_acceptance(baseline, pre_send, timeout_ms=5)
    )

    assert result is None



def test_send_acceptance_rejects_response_dom_without_exact_user_match(monkeypatch):
    baseline = receipt().baseline
    page = SequencePage([
        make_snapshot(messages=(
            MessageSnapshot("user", "u-new", "t-user", "rendered differently", ()),
            MessageSnapshot("assistant", "DOM:assistant:0", None, "done", ()),
        ))
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(
            baseline,
            make_snapshot(),
            timeout_ms=20,
            expected_prompt="expected prompt",
        )
    )

    assert result is None


def test_send_acceptance_accepts_exact_frontend_user_identity(monkeypatch):
    page = SequencePage([make_snapshot()])
    install_sequence(monkeypatch, page)
    client = bind(page)

    async def exercise():
        client._frontend_request_user_message_id = "network-user"
        return await client._wait_send_acceptance(
            receipt().baseline,
            make_snapshot(),
            timeout_ms=20,
            expected_prompt="expected prompt",
        )

    accepted_via, accepted_user = asyncio.run(exercise())
    assert accepted_via == "user_message_identity"
    assert accepted_user.message_id == "network-user"


def test_send_acceptance_ignores_body_identity_without_successful_response(monkeypatch):
    page = SequencePage([make_snapshot()])
    install_sequence(monkeypatch, page)
    client = bind(page)

    async def exercise():
        future = asyncio.get_running_loop().create_future()
        future.set_result(
            {
                "observed_user_message_id": "failed-network-user",
                "conversation_id": "failed-conversation",
            }
        )
        client._frontend_identity_task = future
        return await client._wait_send_acceptance(
            receipt().baseline,
            make_snapshot(),
            timeout_ms=20,
            expected_prompt="expected prompt",
        )

    assert asyncio.run(exercise()) is None


def test_page_health_distinguishes_auth_and_empty_snapshot(monkeypatch):
    auth_page = SequencePage([make_snapshot(requires_login=True)])
    install_sequence(monkeypatch, auth_page)
    auth = asyncio.run(bind(auth_page).health())
    assert auth.healthy is False
    assert auth.action == "manual_login"

    empty_page = SequencePage(
        [
            make_snapshot(
                composer_present=False,
                composer_editable=False,
                messages=(),
                state=ChatGPTState.UNKNOWN,
            )
        ]
    )
    install_sequence(monkeypatch, empty_page)
    empty = asyncio.run(bind(empty_page).health())
    assert empty.healthy is False
    assert empty.action == "reload"


def test_post_reload_same_complete_snapshot_needs_second_sample(monkeypatch):
    page = SequencePage([conversation("same recovered answer")])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(accepted_via="post_reload:exact_user_message"),
            timeout_ms=20,
            stable_ms=0,
            poll_ms=1,
        )
    )

    assert result.text == "same recovered answer"
    assert page.inspect_calls == 2


def test_send_receipt_rejects_legacy_weak_acceptance_signal():
    value = receipt().to_dict()
    value["accepted_via"] = "new_assistant_turn"

    with pytest.raises(ValueError, match="unsupported acceptance"):
        SendReceipt.from_dict(value)


def test_unique_new_user_message_closes_acceptance_without_stop(monkeypatch):
    """Fix C: A unique new user message is authoritative acceptance; stop/assistant are supplemental."""
    baseline = receipt().baseline
    pre_send = make_snapshot()
    # Only a user message appears; stop_visible stays False; no assistant activity.
    page = SequencePage([
        make_snapshot(
            stop_visible=False,
            messages=(MessageSnapshot("user", "u-new", "t-new", "expected prompt", ()),),
        )
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(baseline, pre_send, timeout_ms=20)
    )

    assert result is not None, "Exact unique new user message must close acceptance"
    accepted_via, accepted_user = result
    assert accepted_via == "exact_user_message"
    assert accepted_user is not None
    assert accepted_user.message_id == "u-new"


def test_send_error_reports_zero_clicks_when_prompt_preparation_fails(monkeypatch):
    import playwright_auto.chatgpt as chatgpt_module
    from playwright_auto.chatgpt import SendRecoveryError
    from unittest.mock import AsyncMock

    page = SequencePage([make_snapshot()])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client._prepare_prompt_locked = AsyncMock(side_effect=TimeoutError("composer timeout"))
    click = AsyncMock()
    monkeypatch.setattr(chatgpt_module, "click_send_button", click)

    with pytest.raises(SendRecoveryError, match="failed before any click was dispatched; no resend occurred"):
        asyncio.run(client.send("test prompt", max_attempts=1, recovery_reload=False))

    click.assert_not_awaited()


def test_ambiguous_confirmed_click_never_resends_even_when_attempts_remain(monkeypatch):
    import playwright_auto.chatgpt as chatgpt_module
    from playwright_auto.chatgpt import SendRecoveryError
    from unittest.mock import AsyncMock

    page = SequencePage([make_snapshot()])
    install_sequence(monkeypatch, page)
    client = bind(page)
    client._prepare_prompt_locked = AsyncMock()
    client._wait_send_acceptance = AsyncMock(return_value=None)
    click = AsyncMock()
    monkeypatch.setattr(chatgpt_module, "click_send_button", click)

    with pytest.raises(SendRecoveryError, match="ambiguous after 1 confirmed click\\(s\\); no resend occurred"):
        asyncio.run(client.send("test prompt", max_attempts=2, recovery_reload=False))

    assert click.await_count == 1


def test_send_acceptance_expected_prompt_rejects_mismatching_user_turn(monkeypatch):
    """Safety: a unique new user turn whose text does not match expected_prompt is not accepted."""
    baseline = receipt().baseline
    pre_send = make_snapshot()
    page = SequencePage([
        make_snapshot(
            stop_visible=False,
            messages=(MessageSnapshot("user", "u-manual", "t-manual", "manual input", ()),),
        )
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(
            baseline,
            pre_send,
            timeout_ms=20,
            expected_prompt="expected prompt",
        )
    )

    assert result is None, "Mismatching user turn must not be accepted as exact_user_message"


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("expected prompt", "expected prompt"),
        ("run pytest now", "run `pytest` now"),
        ("long prompt … Show more", "long prompt"),
    ],
)
def test_send_acceptance_expected_prompt_accepts_matching_user_turn(
    monkeypatch, rendered, expected
):
    """Rendered transcript variants of the expected prompt are accepted immediately."""
    baseline = receipt().baseline
    pre_send = make_snapshot()
    page = SequencePage([
        make_snapshot(
            stop_visible=False,
            messages=(MessageSnapshot("user", "u-new", "t-new", rendered, ()),),
        )
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(
            baseline,
            pre_send,
            timeout_ms=20,
            expected_prompt=expected,
        )
    )

    assert result is not None
    accepted_via, accepted_user = result
    assert accepted_via == "exact_user_message"
    assert accepted_user is not None
    assert accepted_user.message_id == "u-new"
