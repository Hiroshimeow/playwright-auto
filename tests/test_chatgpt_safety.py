import asyncio
from dataclasses import replace

import pytest

import playwright_auto.chatgpt as chatgpt
from playwright_auto.chatgpt_graph import rendered_user_text_matches
from playwright_auto.chatgpt import (
    AuthenticationRequiredError,
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    PageOwnershipError,
    SendReceipt,
    SendRecoveryError,
    UnsafePageStateError,
    capture_message_baseline,
    exact_prompt_seen,
    new_assistant_turns,
    normalize_visible_text,
    visible_text_matches,
)


def snapshot(
    *,
    role="DEV",
    page_id="page-1",
    composer_text="",
    send_visible=False,
    send_enabled=False,
    stop_visible=False,
    messages=(),
    state=ChatGPTState.NEW_CHAT,
    dialogs=(),
    attachments=(),
    task_id=None,
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/",
        session_id=None,
        page_id=page_id,
        page_role=role,
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=composer_text,
        send_visible=send_visible,
        send_enabled=send_enabled,
        stop_visible=stop_visible,
        blocking_dialogs=tuple(dialogs),
        attachment_markers=tuple(attachments),
        error_texts=(),
        messages=tuple(messages),
        page_task_id=task_id,
    )


class DummyLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self.page.clicks += 1
        if self.page.raise_click:
            raise RuntimeError("synthetic send failure")

    async def wait_for(self, **_kwargs):
        return None


class DummyPage:
    def __init__(self, current):
        self.current = current
        self.clicks = 0
        self.raise_click = False

    def locator(self, _selector):
        return DummyLocator(self)

    async def evaluate(self, _expression, arg=None):
        if isinstance(arg, list) and len(arg) >= 2 and arg[0] == chatgpt.TASK_ID_STORAGE_KEY:
            self.current = replace(self.current, page_task_id=str(arg[1]))
            return None
        return None


def test_provenance_helpers_reject_old_and_non_exact_messages():
    old = [
        MessageSnapshot("user", "u1", "t1", "old prompt", ()),
        MessageSnapshot("assistant", "a1", "t1", "old answer", ()),
    ]
    baseline = capture_message_baseline(old)
    current = [
        *old,
        MessageSnapshot("user", "u2", "t2", "different prompt", ()),
        MessageSnapshot("assistant", "a2", "t2", "new answer", ()),
    ]

    assert exact_prompt_seen(current, baseline, "expected prompt") is False
    assert [item.message_id for item in new_assistant_turns(current, baseline)] == ["a2"]



def test_send_acceptance_ignores_stale_response_activity_without_current_dom_evidence():
    import types

    stale = replace(
        snapshot(state=ChatGPTState.RESPONDING),
        response_activity_turn_id="turn-old",
        response_activity_text="old stream",
        response_activity_length=len("old stream"),
        response_activity_structure="old-structure",
    )
    page = DummyPage(stale)
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_wait(self, **_kwargs):
        return None

    async def fake_assert(self):
        return stale

    client.wait_for_page_observation = types.MethodType(fake_wait, client)
    client.assert_ownership = types.MethodType(fake_assert, client)

    baseline = capture_message_baseline(stale.messages)
    accepted = asyncio.run(
        client._wait_send_acceptance(
            baseline,
            stale,
            timeout_ms=5,
            expected_prompt="prompt",
        )
    )

    assert accepted is None


def test_send_acceptance_accepts_markerless_current_stop_button_without_listen():
    import types

    before = snapshot()
    current = replace(before, stop_visible=True, state=ChatGPTState.RESPONDING)
    page = DummyPage(before)
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_wait(self, **_kwargs):
        return None

    async def fake_assert(self):
        return current

    client.wait_for_page_observation = types.MethodType(fake_wait, client)
    client.assert_ownership = types.MethodType(fake_assert, client)

    baseline = capture_message_baseline(before.messages)
    accepted = asyncio.run(
        client._wait_send_acceptance(
            baseline,
            before,
            timeout_ms=50,
            expected_prompt="prompt",
        )
    )

    assert accepted == ("stop_button", None)


def test_send_acceptance_timeout_does_not_inherit_client_timeout(monkeypatch):
    import types

    before = snapshot(send_visible=True, send_enabled=True)
    accepted = MessageSnapshot("user", "u-new", "t-new", "prompt", ())
    page = DummyPage(before)
    client = ChatGPTPage(page, timeout_ms=7_200_000)
    client.binding = PageBinding("page-1", "DEV")
    observed_timeouts = []

    async def fake_assert(self):
        return before

    async def fake_prepare(self, *_args, **_kwargs):
        return None

    async def fake_accept(self, *_args, timeout_ms, **_kwargs):
        observed_timeouts.append(timeout_ms)
        return ("exact_user_message", accepted)

    async def fake_click(target, **_kwargs):
        target.clicks += 1

    client.assert_ownership = types.MethodType(fake_assert, client)
    client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
    client._wait_send_acceptance = types.MethodType(fake_accept, client)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

    receipt = asyncio.run(client.send("prompt", wait_for_stop=False, max_attempts=1))

    assert receipt.accepted_via == "exact_user_message"
    assert observed_timeouts == [60_000]


def test_send_reuses_hydrated_existing_conversation_baseline(monkeypatch):
    import types

    history = (
        MessageSnapshot("user", "u-old", "t-old", "older prompt", ()),
        MessageSnapshot("assistant", "a-old", "t-old", "older answer", ()),
    )
    empty = replace(
        snapshot(task_id="task-a"),
        url="https://chatgpt.com/c/reused",
        session_id="reused",
        page_team="alpha",
    )
    hydrated = replace(empty, messages=history)
    accepted = MessageSnapshot("user", "u-new", "t-new", "prompt", ())
    page = DummyPage(empty)
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")
    reads = 0

    async def fake_assert(self):
        nonlocal reads
        reads += 1
        return empty if reads == 1 else hydrated

    async def fake_prepare(self, *_args, **_kwargs):
        return None

    async def fake_accept(self, *_args, **_kwargs):
        return ("exact_user_message", accepted)

    async def fake_click(target, **_kwargs):
        target.clicks += 1

    client.assert_ownership = types.MethodType(fake_assert, client)
    client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
    client._wait_send_acceptance = types.MethodType(fake_accept, client)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

    receipt = asyncio.run(
        client.send(
            "prompt",
            wait_for_stop=False,
            max_attempts=1,
            expected_task_id="task-a",
            expected_team="alpha",
            require_existing_conversation_baseline=True,
        )
    )

    assert receipt.baseline.message_ids == frozenset({"u-old", "a-old"})
    assert page.clicks == 1


def test_send_refuses_persistently_empty_existing_conversation_without_click(monkeypatch):
    import types

    empty = replace(
        snapshot(task_id="task-a"),
        url="https://chatgpt.com/c/reused",
        session_id="reused",
        page_team="alpha",
    )
    page = DummyPage(empty)
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_assert(self):
        return empty

    async def fake_click(target, **_kwargs):
        target.clicks += 1

    client.assert_ownership = types.MethodType(fake_assert, client)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

    with pytest.raises(UnsafePageStateError, match="transcript.*hydrated"):
        asyncio.run(
            client.send(
                "prompt",
                wait_for_stop=False,
                max_attempts=1,
                expected_task_id="task-a",
                expected_team="alpha",
                require_existing_conversation_baseline=True,
            )
        )

    assert page.clicks == 0


def test_manual_composer_is_not_overwritten(monkeypatch):
    page = DummyPage(snapshot(composer_text="manual draft", state=ChatGPTState.DRAFT))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="manual"):
        asyncio.run(client.set_text("workflow prompt"))


def test_set_composer_text_targets_actionable_composer_not_hidden_duplicate(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await chatgpt.set_composer_text(page, "new prompt", timeout_ms=300)
        assert await page.locator("#live-composer").inner_text() == "new prompt"
        assert await page.locator("#hidden-composer").inner_text() == "hidden draft"

    asyncio.run(_with_page("""
        <main>
          <div id="hidden-composer" contenteditable="true" role="textbox" style="display:none">hidden draft</div>
          <div id="live-composer" contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_set_composer_text_ignores_visibility_hidden_duplicate(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await chatgpt.set_composer_text(page, "new prompt", timeout_ms=800)
        assert await page.locator("#prompt-textarea").inner_text() == "new prompt"
        assert await page.locator("#hidden-composer").text_content() == "operator draft"

    asyncio.run(_with_page("""
        <main>
          <div id="hidden-composer" contenteditable="true" role="textbox" style="visibility:hidden">operator draft</div>
          <form><div id="prompt-textarea" contenteditable="true" role="textbox"></div><button data-testid="send-button">Send</button></form>
        </main>
    """, run))


def test_set_composer_text_prefers_owned_live_form_over_visible_unrelated_editor(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await chatgpt.set_composer_text(page, "workflow prompt", timeout_ms=800)
        assert await page.locator("#prompt-textarea").inner_text() == "workflow prompt"
        assert await page.locator("#other-editor").inner_text() == "manual note"

    asyncio.run(_with_page("""
        <main>
          <div id="other-editor" contenteditable="true" role="textbox">manual note</div>
          <form id="live-form"><div id="prompt-textarea" contenteditable="true" role="textbox"></div><button data-testid="send-button">Send</button></form>
        </main>
    """, run))


def test_composer_fill_and_send_use_same_live_form_with_visible_unrelated_editor(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await chatgpt.set_composer_text(page, "workflow prompt", timeout_ms=800)
        method = await chatgpt.click_send_button(
            page,
            timeout_ms=800,
            expected_prompt="workflow prompt",
        )
        assert method == "dom_click"
        assert await page.evaluate("window.__liveSubmits || 0") == 1
        assert await page.evaluate("window.__otherSubmits || 0") == 0
        assert await page.locator("#other-editor").inner_text() == "manual note"

    asyncio.run(_with_page("""
        <main>
          <form id="other-form" onsubmit="event.preventDefault();window.__otherSubmits=(window.__otherSubmits||0)+1">
            <div id="other-editor" contenteditable="true" role="textbox">manual note</div>
            <button type="submit" aria-label="Send">Send</button>
          </form>
          <form id="live-form" onsubmit="event.preventDefault();window.__liveSubmits=(window.__liveSubmits||0)+1">
            <div id="prompt-textarea" contenteditable="true" role="textbox"></div>
            <button type="submit" data-testid="send-button">Send</button>
          </form>
        </main>
    """, run))


def test_set_composer_text_fails_closed_on_two_eligible_live_owners(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        with pytest.raises(ComposerConflictError, match="multiple eligible"):
            await chatgpt.set_composer_text(page, "workflow prompt", timeout_ms=800)
        assert await page.locator("#prompt-textarea").inner_text() == ""
        assert await page.locator("#second-live").inner_text() == ""

    asyncio.run(_with_page("""
        <main>
          <form><div id="prompt-textarea" contenteditable="true" role="textbox"></div><button data-testid="send-button">Send</button></form>
          <form><div data-testid="composer"><div id="second-live" contenteditable="true" role="textbox"></div></div><button data-testid="send-button">Send</button></form>
        </main>
    """, run))


def test_set_composer_text_recovers_from_detach_before_input_without_duplicate(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await page.evaluate("""() => {
          const composer = document.querySelector('#prompt-textarea');
          composer.addEventListener('click', () => {
            if (window.__replaced) return;
            window.__replaced = true;
            const next = composer.cloneNode(false);
            next.id = 'prompt-textarea';
            composer.replaceWith(next);
          }, {once: true});
        }""")
        await chatgpt.set_composer_text(page, "workflow prompt", timeout_ms=800)
        assert await page.locator("#prompt-textarea").inner_text() == "workflow prompt"
        assert await page.evaluate("window.__replaced") is True

    asyncio.run(_with_page("""
        <form><div id="prompt-textarea" contenteditable="true" role="textbox"></div><button data-testid="send-button">Send</button></form>
    """, run))


def test_set_composer_text_does_not_type_into_focus_stealing_editor(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await page.evaluate("""() => {
          document.querySelector('#prompt-textarea').addEventListener('click', () => {
            document.querySelector('#thief').focus();
          });
        }""")
        with pytest.raises(ComposerConflictError, match="focus was stolen"):
            await chatgpt.set_composer_text(page, "workflow prompt", timeout_ms=800)
        assert await page.locator("#thief").inner_text() == "manual note"
        assert await page.locator("#prompt-textarea").inner_text() == ""

    asyncio.run(_with_page("""
        <main>
          <div id="thief" contenteditable="true">manual note</div>
          <form><div id="prompt-textarea" contenteditable="true" role="textbox"></div><button data-testid="send-button">Send</button></form>
        </main>
    """, run))


def test_set_composer_text_hidden_first_real_actionability_and_exact_readback(monkeypatch):
    from test_chatgpt_mcp_permission import _with_page

    async def no_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_delay)

    async def run(page):
        await chatgpt.set_composer_text(page, "line one\nline two", timeout_ms=800)
        assert await page.locator("#live-composer").inner_text() == "line one\nline two"
        assert await page.locator("#hidden-composer").inner_text() == "operator draft"

    asyncio.run(_with_page("""
        <main>
          <div id="hidden-composer" contenteditable="true" role="textbox" style="display:none">operator draft</div>
          <div id="live-composer" contenteditable="true" role="textbox"></div>
        </main>
    """, run))


def test_prepare_prompt_refills_empty_before_send(monkeypatch):
    page = DummyPage(snapshot())
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")
    states = iter(
        (
            snapshot(composer_text="", send_visible=True, send_enabled=True),
            snapshot(composer_text="prompt", send_visible=True, send_enabled=True),
        )
    )
    fills = []

    async def interaction_snapshot(*, timeout_ms, allow_attachments=False):
        return next(states)

    async def fill(_page, text, timeout_ms=5_000):
        fills.append(text)

    client._interaction_snapshot = interaction_snapshot
    monkeypatch.setattr(chatgpt, "set_composer_text", fill)

    asyncio.run(
        client._prepare_prompt_locked(
            "prompt",
            timeout_ms=1_000,
            stable_seconds=0,
        )
    )

    assert fills == ["prompt"]
    assert client._owned_composer_text == "prompt"


def test_prepare_prompt_pauses_on_different_composer():
    page = DummyPage(snapshot())
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def interaction_snapshot(*, timeout_ms, allow_attachments=False):
        return snapshot(
            composer_text="different text",
            send_visible=True,
            send_enabled=True,
        )

    client._interaction_snapshot = interaction_snapshot

    with pytest.raises(ComposerConflictError, match="manual input"):
        asyncio.run(
            client._prepare_prompt_locked(
                "prompt",
                timeout_ms=1_000,
                stable_seconds=0,
            )
        )


def test_force_clear_can_discard_target_text_and_attachments(monkeypatch):
    dirty = snapshot(
        composer_text="stale target draft",
        state=ChatGPTState.DRAFT,
        attachments=("stale.txt",),
    )
    page = DummyPage(dirty)
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")
    clear_calls = []
    evaluate_calls = []

    async def interaction_snapshot(*, timeout_ms, allow_attachments=False):
        assert allow_attachments is True
        return dirty

    async def clean_ownership():
        return replace(dirty, composer_text="", attachment_markers=(), state=ChatGPTState.NEW_CHAT)

    async def discard_attachments(_expression, arg=None):
        evaluate_calls.append(arg)
        return 1

    async def clear_text(_page, **_kwargs):
        clear_calls.append(True)

    client._interaction_snapshot = interaction_snapshot
    client.assert_ownership = clean_ownership
    page.evaluate = discard_attachments
    monkeypatch.setattr(chatgpt, "clear_composer", clear_text)

    asyncio.run(client.clear(force=True, discard_attachments=True))

    assert evaluate_calls == [None]
    assert clear_calls == [True]


def test_new_chat_refuses_to_discard_draft(monkeypatch):
    page = DummyPage(snapshot(composer_text="manual draft", state=ChatGPTState.DRAFT))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="discard"):
        asyncio.run(client.new_chat())


def test_role_binding_is_immutable_without_explicit_rebind(monkeypatch):
    page = DummyPage(snapshot())
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    with pytest.raises(PageOwnershipError, match="already bound"):
        asyncio.run(client.set_role("PLAN"))






def test_send_receipt_round_trip_preserves_provenance_and_rejects_tamper():
    receipt = SendReceipt(
        prompt="exact prompt",
        prompt_sha256=chatgpt.prompt_digest("exact prompt"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(
            frozenset({"m1"}),
            frozenset({"t1"}),
            frozenset({"t1"}),
            frozenset(),
        ),
        attempts=2,
        accepted_via="post_reload:exact_user_message",
        session_id_before="session-1",
    )

    restored = SendReceipt.from_dict(receipt.to_dict())
    assert restored == receipt

    tampered = receipt.to_dict()
    tampered["prompt"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        SendReceipt.from_dict(tampered)


def test_manual_attachment_blocks_composer_mutation_and_new_chat(monkeypatch):
    page = DummyPage(
        snapshot(
            composer_text="",
            state=ChatGPTState.NEW_CHAT,
            attachments=("manual-file.txt",),
        )
    )
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="attachments"):
        asyncio.run(client.set_text("workflow prompt"))
    with pytest.raises(ComposerConflictError, match="discard"):
        asyncio.run(client.new_chat())


def test_new_client_respects_persisted_role_binding(monkeypatch):
    page = DummyPage(snapshot(role="DEV", page_id="page-1"))
    client = ChatGPTPage(page)

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(PageOwnershipError, match="persistently bound"):
        asyncio.run(client.set_role("PLAN"))

    assigned = asyncio.run(client.set_role("DEV"))
    assert assigned == {"page_id": "page-1", "page_role": "DEV"}
    assert client.binding == PageBinding("page-1", "DEV")



def test_prepare_task_reuses_same_task_without_new_chat(monkeypatch):
    page = DummyPage(snapshot(task_id="TASK-1"))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    async def unexpected_new_chat(_page, timeout_ms):
        raise AssertionError("same task must not open a new chat")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    monkeypatch.setattr(chatgpt, "open_new_chat", unexpected_new_chat)

    preflight = asyncio.run(client.task_preflight("TASK-1"))
    result = asyncio.run(client.prepare_task("TASK-1"))

    assert preflight["requires_new_chat"] is False
    assert result["reused"] is True
    assert result["new_chat_method"] is None


def test_prepare_task_switches_once_and_persists_task(monkeypatch):
    page = DummyPage(snapshot(task_id="TASK-OLD"))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")
    calls = []

    async def fake_inspect(_page):
        return page.current

    async def fake_new_chat(_page, timeout_ms):
        calls.append(timeout_ms)
        page.current = replace(
            page.current,
            url="https://chatgpt.com/",
            session_id=None,
            composer_text="",
            messages=(),
            state=ChatGPTState.NEW_CHAT,
        )
        return "dom"

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    monkeypatch.setattr(chatgpt, "open_new_chat", fake_new_chat)

    result = asyncio.run(client.prepare_task("TASK-NEW"))

    assert result == {
        "task_id": "TASK-NEW",
        "previous_task_id": "TASK-OLD",
        "reused": False,
        "new_chat_method": "dom",
    }
    assert len(calls) == 1
    assert page.current.page_task_id == "TASK-NEW"


def test_task_switch_refuses_manual_input_and_active_response(monkeypatch):
    async def fake_inspect(page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    draft_page = DummyPage(
        snapshot(
            task_id="TASK-OLD",
            composer_text="manual draft",
            state=ChatGPTState.DRAFT,
        )
    )
    draft_client = ChatGPTPage(draft_page)
    draft_client.binding = PageBinding("page-1", "DEV")
    with pytest.raises(ComposerConflictError, match="task switch"):
        asyncio.run(draft_client.task_preflight("TASK-NEW"))

    active_page = DummyPage(
        snapshot(
            task_id="TASK-OLD",
            stop_visible=True,
            state=ChatGPTState.RESPONDING,
        )
    )
    active_client = ChatGPTPage(active_page)
    active_client.binding = PageBinding("page-1", "DEV")
    with pytest.raises(UnsafePageStateError, match="responding"):
        asyncio.run(active_client.task_preflight("TASK-NEW"))



def test_visible_text_normalization_only_collapses_whitespace():
    assert normalize_visible_text("line one\n\nline two") == "line one line two"
    assert visible_text_matches("line one  line two", "line one\n\nline two") is True
    assert visible_text_matches("line one CHANGED", "line one line two") is False


def test_exact_prompt_seen_accepts_dom_collapse_with_unique_durable_marker():
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    messages = (
        MessageSnapshot(
            "user",
            "u1",
            "t1",
            "Collapsed visible text ROLE_REQUEST_ID: abc Show more",
            (),
        ),
    )
    assert exact_prompt_seen(
        messages,
        baseline,
        "Full prompt body\n\nROLE_REQUEST_ID: abc",
    ) is True
    assert exact_prompt_seen(
        messages,
        baseline,
        "Full prompt body\n\nROLE_REQUEST_ID: different",
    ) is False


def test_exact_prompt_seen_rejects_old_or_non_user_marker():
    old_user = MessageSnapshot(
        "user", "u1", "t1", "ROLE_REQUEST_ID: abc Show more", ()
    )
    baseline = capture_message_baseline((old_user,))
    messages = (
        old_user,
        MessageSnapshot("assistant", "a2", "t2", "ROLE_REQUEST_ID: abc", ()),
    )
    assert exact_prompt_seen(
        messages,
        baseline,
        "Full prompt body\n\nROLE_REQUEST_ID: abc",
    ) is False



def test_bound_tab_reports_auth_redirect_explicitly(monkeypatch):
    page = DummyPage(
        replace(
            snapshot(),
            url="https://auth.openai.com/choose-an-account",
            page_id=None,
            page_role=None,
            requires_login=True,
            composer_present=False,
            composer_editable=False,
            state=ChatGPTState.AUTH_REQUIRED,
        )
    )
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "PLAN")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(AuthenticationRequiredError, match="requires authentication"):
        asyncio.run(client.assert_ownership())


def test_send_receipt_conversation_id_is_strict_and_allowlisted():
    receipt = SendReceipt(
        prompt="exact prompt",
        prompt_sha256=chatgpt.prompt_digest("exact prompt"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="exact_user_message",
        session_id_before=None,
        user_message_id="user-1",
        user_turn_id="turn-1",
        conversation_id="conversation-1",
    )
    payload = receipt.to_dict()
    assert SendReceipt.from_dict(payload) == receipt
    assert set(payload) == {
        "prompt", "prompt_sha256", "binding", "baseline", "attempts",
        "accepted_via", "session_id_before", "user_message_id", "user_turn_id",
        "conversation_id",
    }
    legacy = dict(payload)
    legacy.pop("conversation_id")
    assert SendReceipt.from_dict(legacy).conversation_id is None
    for invalid in ("", "bad\nvalue", 123, {"id": "conversation-1"}):
        bad = dict(payload)
        bad["conversation_id"] = invalid
        with pytest.raises(ValueError, match="conversation ID"):
            SendReceipt.from_dict(bad)


def test_send_arms_nonblocking_frontend_identity_observer_before_single_click(monkeypatch):
    import types

    accepted = MessageSnapshot("user", "u1", "t1", "prompt", ())
    before = snapshot(send_visible=True, send_enabled=True)

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class Response:
        url = "https://chatgpt.com/backend-api/f/conversation"
        request = Request()
        def __init__(self, gate): self.gate = gate
        async def body(self):
            await self.gate.wait()
            return b'data: {"conversation_id":"c1"}\n'

    class EventPage(DummyPage):
        def __init__(self, current):
            super().__init__(current)
            self.listeners = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            if callback in self.listeners.get(event, []):
                self.listeners[event].remove(callback)
        def emit(self, event, value):
            for callback in tuple(self.listeners.get(event, [])):
                callback(value)

    async def run():
        gate = asyncio.Event()
        page = EventPage(before)
        client = ChatGPTPage(page)
        client.binding = PageBinding("page-1", "DEV")

        async def fake_assert(self): return before
        async def fake_prepare(self, *_args, **_kwargs): return None
        async def fake_accept(self, *_args, **_kwargs): return ("exact_user_message", accepted)
        client.assert_ownership = types.MethodType(fake_assert, client)
        client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
        client._wait_send_acceptance = types.MethodType(fake_accept, client)

        async def fake_click(target, **_kwargs):
            target.clicks += 1
            assert target.listeners.get("response")
            target.emit("response", Response(gate))
        monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

        receipt = await client.send("prompt", wait_for_stop=False, max_attempts=1)
        task = client.take_frontend_identity_task()
        assert receipt.user_message_id == "u1"
        assert receipt.conversation_id is None
        assert page.clicks == 1
        assert task is not None and not task.done()
        gate.set()
        assert await task == {"observed_user_message_id": "u1", "conversation_id": "c1"}

    asyncio.run(run())


def test_sequential_send_clears_previous_frontend_identity_task(monkeypatch):
    import types

    before = snapshot(send_visible=True, send_enabled=True)
    accepted = [
        MessageSnapshot("user", "u1", "t1", "first", ()),
        MessageSnapshot("user", "u2", "t2", "second", ()),
    ]

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class Response:
        url = "https://chatgpt.com/backend-api/f/conversation"
        request = Request()
        async def body(self):
            return b'data: {"conversation_id":"conversation-1"}\n'

    class EventPage(DummyPage):
        def __init__(self, current):
            super().__init__(current)
            self.listeners = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            if callback in self.listeners.get(event, []):
                self.listeners[event].remove(callback)
        def emit(self, event, value):
            for callback in tuple(self.listeners.get(event, [])):
                callback(value)

    async def run():
        page = EventPage(before)
        client = ChatGPTPage(page)
        client.binding = PageBinding("page-1", "DEV")
        acceptance_index = 0

        async def fake_assert(self): return before
        async def fake_prepare(self, *_args, **_kwargs): return None
        async def fake_accept(self, *_args, **_kwargs):
            nonlocal acceptance_index
            await asyncio.sleep(0)
            value = accepted[acceptance_index]
            acceptance_index += 1
            return ("exact_user_message", value)
        client.assert_ownership = types.MethodType(fake_assert, client)
        client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
        client._wait_send_acceptance = types.MethodType(fake_accept, client)

        async def fake_click(target, **_kwargs):
            target.clicks += 1
            if target.clicks == 1:
                target.emit("response", Response())
        monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

        first = await client.send("first", wait_for_stop=False, max_attempts=1)
        assert first.conversation_id == "conversation-1"
        first_task = client._frontend_identity_task
        assert first_task is not None and first_task.done()

        second = await client.send("second", wait_for_stop=False, max_attempts=1)
        assert second.user_message_id == "u2"
        assert second.conversation_id is None
        second_task = client.take_frontend_identity_task()
        assert second_task is not None and not second_task.done()
        assert page.clicks == 2
        assert first_task.done()

    asyncio.run(run())


def test_post_reload_acceptance_reuses_same_frontend_identity_without_second_click(monkeypatch):
    import types

    before = snapshot(send_visible=True, send_enabled=True)
    accepted = MessageSnapshot("user", "u1", "t1", "prompt", ())

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class Response:
        url = "https://chatgpt.com/backend-api/f/conversation"
        request = Request()
        async def body(self):
            return b'data: {"conversation_id":"conversation-reload"}\n'

    class EventPage(DummyPage):
        def __init__(self, current):
            super().__init__(current)
            self.listeners = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            if callback in self.listeners.get(event, []):
                self.listeners[event].remove(callback)
        def emit(self, event, value):
            for callback in tuple(self.listeners.get(event, [])):
                callback(value)

    async def run():
        page = EventPage(before)
        client = ChatGPTPage(page)
        client.binding = PageBinding("page-1", "DEV")
        acceptance_calls = 0
        refreshed = False

        async def fake_assert(self):
            return replace(before, messages=(accepted,)) if refreshed else before
        async def fake_prepare(self, *_args, **_kwargs): return None
        async def fake_accept(self, *_args, **_kwargs):
            nonlocal acceptance_calls
            acceptance_calls += 1
            await asyncio.sleep(0)
            if acceptance_calls == 1:
                raise RuntimeError("synthetic acceptance timeout after click")
            return ("exact_user_message", accepted)
        client.assert_ownership = types.MethodType(fake_assert, client)
        client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
        client._wait_send_acceptance = types.MethodType(fake_accept, client)

        async def fake_click(target, **_kwargs):
            target.clicks += 1
            target.emit("response", Response())
        async def fake_refresh(_page, **_kwargs):
            nonlocal refreshed
            refreshed = True
            await asyncio.sleep(0)
        monkeypatch.setattr(chatgpt, "click_send_button", fake_click)
        monkeypatch.setattr(chatgpt, "refresh_page", fake_refresh)

        receipt = await client.send("prompt", wait_for_stop=False, max_attempts=2)
        assert receipt.accepted_via == "post_reload:exact_user_message"
        assert receipt.conversation_id == "conversation-reload"
        assert receipt.attempts == 1
        assert page.clicks == 1
        assert acceptance_calls == 1

    asyncio.run(run())


def test_frontend_identity_parser_failure_cannot_retry_accepted_dom_send(monkeypatch):
    import types

    before = snapshot(send_visible=True, send_enabled=True)
    accepted = MessageSnapshot("user", "u1", "t1", "prompt", ())

    class Request:
        method = "POST"
        post_data_json = {"messages": [{"id": "u1", "author": {"role": "user"}}]}

    class Response:
        url = "https://chatgpt.com/backend-api/f/conversation"
        request = Request()
        async def body(self):
            raise RuntimeError("synthetic raw body failure")

    class EventPage(DummyPage):
        def __init__(self, current):
            super().__init__(current)
            self.listeners = {}
        def on(self, event, callback):
            self.listeners.setdefault(event, []).append(callback)
        def remove_listener(self, event, callback):
            if callback in self.listeners.get(event, []):
                self.listeners[event].remove(callback)
        def emit(self, event, value):
            for callback in tuple(self.listeners.get(event, [])):
                callback(value)

    async def run():
        page = EventPage(before)
        client = ChatGPTPage(page)
        client.binding = PageBinding("page-1", "DEV")

        async def fake_assert(self): return before
        async def fake_prepare(self, *_args, **_kwargs): return None
        async def fake_accept(self, *_args, **_kwargs):
            await asyncio.sleep(0)
            return ("exact_user_message", accepted)
        client.assert_ownership = types.MethodType(fake_assert, client)
        client._prepare_prompt_locked = types.MethodType(fake_prepare, client)
        client._wait_send_acceptance = types.MethodType(fake_accept, client)

        async def fake_click(target, **_kwargs):
            target.clicks += 1
            target.emit("response", Response())
        monkeypatch.setattr(chatgpt, "click_send_button", fake_click)

        receipt = await client.send("prompt", wait_for_stop=False, max_attempts=2)
        task = client.take_frontend_identity_task()
        assert receipt.user_message_id == "u1"
        assert receipt.conversation_id is None
        assert receipt.attempts == 1
        assert page.clicks == 1
        assert task is not None and await task is None

    asyncio.run(run())



def test_rendered_user_text_match_allows_markdown_backticks_and_show_more_only():
    expected = (
        "PAUSE only after writing a valid report with "
        "`block_code=...` and `resume_condition=...`. "
        "Work first."
    )
    rendered = (
        "PAUSE only after writing a valid report with "
        "block_code=... and resume_condition=.... Work first. Show more"
    )
    assert rendered_user_text_matches(rendered, expected) is True
    assert rendered_user_text_matches(
        rendered.replace("Work first.", "Do something else."),
        expected,
    ) is False
