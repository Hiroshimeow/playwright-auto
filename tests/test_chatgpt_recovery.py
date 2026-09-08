import asyncio

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
    client = ChatGPTPage(page, timeout_ms=30)
    client.binding = PageBinding("page-1", "DEV")
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


def test_send_acceptance_does_not_trust_unrelated_assistant_turn(monkeypatch):
    page = SequencePage(
        [
            make_snapshot(
                messages=(
                    MessageSnapshot(
                        "assistant", "a-unrelated", "t-unrelated", "other answer", ()
                    ),
                )
            )
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page)._wait_send_acceptance(
            receipt().baseline,
            "expected prompt",
            timeout_ms=5,
        )
    )

    assert result is None


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
