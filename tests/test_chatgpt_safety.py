import asyncio
from dataclasses import replace

import pytest

import playwright_auto.chatgpt as chatgpt
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
    receipt_user_message_seen,
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
    team=None,
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
        page_team=team,
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
        if isinstance(arg, list) and len(arg) == 9 and arg[0] == chatgpt.ROLE_STORAGE_KEY:
            self.current = replace(
                self.current,
                page_role=str(arg[5]),
                page_id=str(arg[6]),
                page_task_id=str(arg[7]),
                page_team=str(arg[8]),
            )
            return None
        if isinstance(arg, list) and len(arg) >= 2 and arg[0] == chatgpt.TASK_ID_STORAGE_KEY:
            changes = {"page_task_id": str(arg[1])}
            if len(arg) >= 4 and arg[2] == chatgpt.TEAM_STORAGE_KEY:
                changes["page_team"] = str(arg[3])
            self.current = replace(self.current, **changes)
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


def test_manual_composer_is_not_overwritten(monkeypatch):
    page = DummyPage(snapshot(composer_text="manual draft", state=ChatGPTState.DRAFT))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="manual"):
        asyncio.run(client.set_text("workflow prompt"))


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


def test_send_recovery_rechecks_progress_before_second_click(monkeypatch):
    page = DummyPage(
        snapshot(
            composer_text="",
            send_visible=False,
            send_enabled=False,
            state=ChatGPTState.NEW_CHAT,
        )
    )
    page.raise_click = True
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    async def fake_set_text(_page, text, timeout_ms):
        page.current = snapshot(
            composer_text=text,
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
        )

    async def fake_refresh(_page, timeout_ms):
        page.current = snapshot(
            messages=(
                MessageSnapshot("user", "u2", "t2", "exact prompt", ()),
            ),
            state=ChatGPTState.SUBMITTING,
        )

    async def fake_click_send(_page, timeout_ms):
        page.clicks += 1
        raise RuntimeError("synthetic send failure")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    monkeypatch.setattr(chatgpt, "set_composer_text", fake_set_text)
    monkeypatch.setattr(chatgpt, "refresh_page", fake_refresh)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click_send)

    receipt = asyncio.run(
        client.send(
            "exact prompt",
            timeout_ms=100,
            max_attempts=2,
            recovery_reload=True,
            wait_for_stop=False,
        )
    )

    assert page.clicks == 1
    assert receipt.attempts == 1
    assert receipt.accepted_via == "post_reload:exact_user_message"


def test_wait_response_accepts_new_assistant_after_confirmed_send(monkeypatch):
    user = MessageSnapshot("user", "u2", "t2", "expected prompt", ())
    response = MessageSnapshot("assistant", "a2", "t2", "{\"route\":\"DEV\"}", ())
    page = DummyPage(snapshot(messages=(user, response), state=ChatGPTState.WAITING_PROMPT))
    client = ChatGPTPage(page, timeout_ms=20)
    binding = PageBinding("page-1", "DEV")
    client.binding = binding
    receipt = SendReceipt(
        prompt="expected prompt",
        prompt_sha256="digest",
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u2",
        user_turn_id="t2",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    received = asyncio.run(
        client.wait_for_response(receipt, timeout_ms=20, stable_ms=0, poll_ms=1)
    )

    assert received == response


def test_rehydrated_user_turn_accepts_changed_message_id_without_visible_text_fallback(monkeypatch):
    old_user = MessageSnapshot("user", "old-user-dom", "old-user-turn", "old prompt", ())
    old_assistant = MessageSnapshot("assistant", "old-assistant", "old-assistant-turn", "old answer", ())
    baseline = capture_message_baseline((old_user, old_assistant))
    collapsed = MessageSnapshot(
        "user",
        "new-user-dom",
        "stable-user-turn",
        "expected prompt prefix Show more",
        (),
    )
    final = MessageSnapshot(
        "assistant",
        "new-assistant",
        "new-assistant-turn",
        '{"route":"DEV","handoff":"report.md"}',
        (),
    )
    page = DummyPage(
        snapshot(
            messages=(old_user, old_assistant, collapsed, final),
            state=ChatGPTState.WAITING_PROMPT,
        )
    )
    client = ChatGPTPage(page, timeout_ms=20)
    binding = PageBinding("page-1", "DEV")
    client.binding = binding
    receipt = SendReceipt(
        prompt="expected prompt whose full rendered text is intentionally unavailable",
        prompt_sha256=chatgpt.prompt_digest(
            "expected prompt whose full rendered text is intentionally unavailable"
        ),
        binding=binding,
        baseline=baseline,
        attempts=1,
        accepted_via="post_reload:user_message_identity",
        session_id_before="session-1",
        user_message_id="old-rehydrated-dom-id",
        user_turn_id="stable-user-turn",
    )

    assert receipt_user_message_seen(page.current.messages, receipt) is True

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    received = asyncio.run(
        client.wait_for_response(
            receipt,
            timeout_ms=20,
            stable_ms=0,
            poll_ms=1,
            stale_response_baseline=chatgpt.capture_response_recovery_baseline(
                (old_user, old_assistant), baseline
            ),
        )
    )

    assert received == final


def test_persisted_identity_never_falls_back_to_collapsed_visible_text():
    receipt = SendReceipt(
        prompt="expected full prompt",
        prompt_sha256=chatgpt.prompt_digest("expected full prompt"),
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="missing-message-id",
        user_turn_id="missing-turn-id",
    )
    messages = (
        MessageSnapshot("user", "other-message", "other-turn", "expected full prompt", ()),
    )

    assert receipt_user_message_seen(messages, receipt) is False


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



def test_restore_identity_reuses_exact_page_binding_for_controlled_reopen(monkeypatch):
    page = DummyPage(snapshot(page_id=None, role=None, task_id=None, team=None))
    client = ChatGPTPage(page)

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    restored = asyncio.run(
        client.restore_identity(
            page_id="page-closed",
            role="PLAN2",
            task_id="TASK-1",
            team="alpha2",
        )
    )

    assert client.binding == PageBinding("page-closed", "PLAN2")
    assert restored.page_id == "page-closed"
    assert restored.page_role == "PLAN2"
    assert restored.page_task_id == "TASK-1"
    assert restored.page_team == "alpha2"


def test_bind_task_identity_persists_task_and_team_without_navigation(monkeypatch):
    page = DummyPage(snapshot(task_id=None, team=None))
    client = ChatGPTPage(page)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    result = asyncio.run(client.bind_task_identity("TASK-1", "alpha2"))

    assert result == {"task_id": "TASK-1", "team": "alpha2"}
    assert page.current.page_task_id == "TASK-1"
    assert page.current.page_team == "alpha2"


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
