import asyncio

import pytest

import playwright_auto.chatgpt as chatgpt
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ChoicePromptBlockedError,
    IncompleteResponseTimeoutError,
    ManualInputPendingError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    StableMalformedResponseError,
    looks_incomplete_response,
    prompt_digest,
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
    error_texts=(),
    response_activity_text="",
    response_activity_structure="",
    response_activity_turn_id=None,
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
        error_texts=tuple(error_texts),
        messages=tuple(messages),
        choice_prompt_labels=tuple(choice_labels),
        response_activity_text=response_activity_text,
        response_activity_structure=response_activity_structure,
        response_activity_turn_id=response_activity_turn_id,
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


def conversation(
    answer: str,
    *,
    stop=False,
    image_count=0,
    assistant_message_id="a2",
    assistant_turn_id="t2",
):
    return make_snapshot(
        messages=(
            MessageSnapshot("user", "u2", "t2", "expected prompt", ()),
            MessageSnapshot(
                "assistant",
                assistant_message_id,
                assistant_turn_id,
                answer,
                (),
                image_count=image_count,
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


def test_incomplete_response_detector_covers_transient_placeholder_code_fence_and_json():
    assert looks_incomplete_response("Thinking") is True
    assert looks_incomplete_response("Analyzing…") is True
    assert looks_incomplete_response("```python\nprint('x')") is True
    assert looks_incomplete_response('{"route": "DEV"') is True
    assert looks_incomplete_response('{"route": "DEV"}') is False
    assert looks_incomplete_response("normal final response") is False


def test_wait_response_ignores_transient_thinking_placeholder(monkeypatch):
    page = SequencePage(
        [
            conversation("Thinking", stop=True),
            conversation("Thinking", stop=True),
            conversation('{"route":"DONE","handoff":"report.md"}'),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(), timeout_ms=30, stable_ms=0, poll_ms=1
        )
    )

    assert result.text == '{"route":"DONE","handoff":"report.md"}'
    assert page.inspect_calls == 3


def _require_route_json(message):
    if not message.text.startswith('{"route":'):
        raise ValueError("route JSON not ready")


def test_streaming_progress_dom_changes_response_activity_without_assistant_message():
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    first = make_snapshot(
        messages=(MessageSnapshot("user", "u2", "t2", "expected prompt", ()),),
        stop_visible=True,
        response_activity_text="Inspecting repository",
        response_activity_structure="cot-v5-pinned-row|cot-v5-pinned-row-content",
        response_activity_turn_id="request-live-0",
        state=ChatGPTState.RESPONDING,
    )
    second = make_snapshot(
        messages=first.messages,
        stop_visible=True,
        response_activity_text="Inspecting durable block and searching for functions",
        response_activity_structure="cot-v5-pinned-row|cot-v5-pinned-row-content",
        response_activity_turn_id="request-live-0",
        state=ChatGPTState.RESPONDING,
    )

    first_signature, first_length = chatgpt.response_activity_signature(first, baseline)
    second_signature, second_length = chatgpt.response_activity_signature(second, baseline)

    assert first_signature != second_signature
    assert first_length == len("Inspecting repository")
    assert second_length == len("Inspecting durable block and searching for functions")


def test_invalid_progress_with_stop_visible_waits_for_valid_route(monkeypatch):
    final = '{"route":"TEST","handoff":"report.md"}'
    page = SequencePage(
        [
            conversation("Inspecting durable block and searching for functions", stop=True),
            conversation("Inspecting durable block and searching for functions", stop=True),
            conversation(final),
            conversation(final),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=40,
            stable_ms=0,
            poll_ms=1,
            candidate_validator=_require_route_json,
            minimum_samples=2,
            invalid_grace_ms=0,
        )
    )

    assert result.text == final
    assert page.inspect_calls == 4


def test_changing_progress_under_timeout_banner_remains_nonfinal(monkeypatch):
    final = '{"route":"TEST","handoff":"report.md"}'
    page = SequencePage(
        [
            make_snapshot(
                messages=conversation("Inspecting repository").messages,
                error_texts=("Message delivery timed out. Please try again.",),
                state=ChatGPTState.ERROR,
            ),
            make_snapshot(
                messages=conversation("Inspecting durable block").messages,
                error_texts=("Message delivery timed out. Please try again.",),
                state=ChatGPTState.ERROR,
            ),
            make_snapshot(
                messages=conversation("Inspecting durable block").messages,
                error_texts=("Message delivery timed out. Please try again.",),
                state=ChatGPTState.ERROR,
            ),
            conversation(final),
            conversation(final),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=50,
            stable_ms=0,
            poll_ms=1,
            candidate_validator=_require_route_json,
            minimum_samples=2,
            invalid_grace_ms=0,
        )
    )

    assert result.text == final
    assert page.inspect_calls == 5


def test_recent_activity_after_stop_disappears_gets_grace_before_repair(monkeypatch):
    final = '{"route":"TEST","handoff":"report.md"}'
    page = SequencePage(
        [
            conversation("Thinking", stop=True),
            conversation("Inspecting final report"),
            conversation(final),
            conversation(final),
        ]
    )
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=50,
            stable_ms=0,
            poll_ms=1,
            candidate_validator=_require_route_json,
            minimum_samples=2,
            invalid_grace_ms=5,
        )
    )

    assert result.text == final


def test_stable_malformed_final_raises_only_after_two_clean_samples(monkeypatch):
    page = SequencePage([conversation("final prose without route")])
    install_sequence(monkeypatch, page)

    with pytest.raises(StableMalformedResponseError, match="route JSON not ready") as caught:
        asyncio.run(
            bind(page).wait_for_response(
                receipt(),
                timeout_ms=20,
                stable_ms=0,
                poll_ms=1,
                candidate_validator=_require_route_json,
                minimum_samples=2,
                invalid_grace_ms=0,
            )
        )

    assert caught.value.candidate.text == "final prose without route"
    assert page.inspect_calls == 2


def test_valid_route_requires_two_samples_when_validator_is_active(monkeypatch):
    final = '{"route":"TEST","handoff":"report.md"}'
    page = SequencePage([conversation(final)])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=20,
            stable_ms=0,
            poll_ms=1,
            candidate_validator=_require_route_json,
            minimum_samples=2,
        )
    )

    assert result.text == final
    assert page.inspect_calls == 2


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


def test_active_response_reloads_once_then_rejects_first_stale_done(monkeypatch):
    page = SequencePage(
        [
            conversation("partial", stop=True),
            conversation("partial growing", stop=True),
            conversation("stale after reload"),
            conversation(
                "final after reload",
                assistant_message_id="a3",
                assistant_turn_id="t3",
            ),
        ]
    )
    install_sequence(monkeypatch, page)

    async def fake_refresh(_page, timeout_ms):
        page.reload_calls += 1

    monkeypatch.setattr(chatgpt, "refresh_page", fake_refresh)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(accepted_via="stop_button"),
            timeout_ms=50,
            stable_ms=0,
            poll_ms=1,
            active_reload_after_ms=1,
            reload_wait_ms=0,
        )
    )

    assert result.text == "final after reload"
    assert page.reload_calls == 1



def test_pre_refresh_response_remains_stale_by_content_even_with_new_dom_identity(monkeypatch):
    stale = conversation("completed before refresh")
    page = SequencePage(
        [
            conversation(
                "completed before refresh",
                assistant_message_id="a9",
                assistant_turn_id="t9",
            ),
            conversation(
                "new response after refresh",
                assistant_message_id="a10",
                assistant_turn_id="t10",
            ),
        ]
    )
    install_sequence(monkeypatch, page)
    baseline = chatgpt.capture_response_recovery_baseline(
        stale.messages,
        receipt().baseline,
    )

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(accepted_via="post_reload:stop_button"),
            timeout_ms=30,
            stable_ms=0,
            poll_ms=1,
            stale_response_baseline=baseline,
        )
    )

    assert result.text == "new response after refresh"
    assert result.message_id == "a10"


def test_only_pre_refresh_response_repeated_after_reload_is_rejected(monkeypatch):
    stale = conversation("completed before refresh")
    page = SequencePage(
        [
            conversation("completed before refresh"),
            conversation("completed before refresh"),
            conversation("completed before refresh"),
        ]
    )
    install_sequence(monkeypatch, page)
    baseline = chatgpt.capture_response_recovery_baseline(
        stale.messages,
        receipt().baseline,
    )

    with pytest.raises(IncompleteResponseTimeoutError, match="pre-refresh"):
        asyncio.run(
            bind(page).wait_for_response(
                receipt(accepted_via="post_reload:stop_button"),
                timeout_ms=8,
                stable_ms=0,
                poll_ms=1,
                stale_response_baseline=baseline,
            )
        )

def test_structurally_incomplete_exact_response_is_retryable_timeout(monkeypatch):
    page = SequencePage([conversation('{"PLAN": "continue"')])
    install_sequence(monkeypatch, page)

    with pytest.raises(IncompleteResponseTimeoutError, match="structurally complete"):
        asyncio.run(
            bind(page).wait_for_response(
                receipt(), timeout_ms=8, stable_ms=0, poll_ms=1
            )
        )


def test_manual_composer_input_blocks_completion_and_recovery(monkeypatch):
    snapshot = conversation("final response")
    snapshot = make_snapshot(
        messages=snapshot.messages,
        composer_text="manual steering",
        state=ChatGPTState.DRAFT,
    )
    page = SequencePage([snapshot])
    install_sequence(monkeypatch, page)

    with pytest.raises(ManualInputPendingError, match="manual composer"):
        asyncio.run(
            bind(page).wait_for_response(
                receipt(),
                timeout_ms=8,
                stable_ms=0,
                poll_ms=1,
                active_reload_after_ms=1,
            )
        )
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


def test_stable_exact_response_can_complete_while_stop_remains_visible(monkeypatch):
    page = SequencePage([
        conversation("final route object", stop=True),
        conversation("final route object", stop=True),
    ])
    install_sequence(monkeypatch, page)

    result = asyncio.run(
        bind(page).wait_for_response(
            receipt(),
            timeout_ms=20,
            stable_ms=0,
            poll_ms=1,
            active_reload_after_ms=10_000,
        )
    )

    assert result.text == "final route object"
    assert page.reload_calls == 0
