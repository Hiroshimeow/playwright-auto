import asyncio
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import playwright_auto.chatgpt as chatgpt
import playwright_auto.upload as upload_module
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
from playwright_auto.durable import RequestLedger, RequestStatus
from playwright_auto.durable_blocks import DurableSendBlock
from playwright_auto.upload import (
    FileIdentity,
    UploadReadinessError,
    UploadReceipt,
    collect_file_identities,
    collect_file_snapshots,
    wait_upload_ready,
)
from playwright_auto.workflow import WorkflowContext


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
        self.before_task_bind = None

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
                if self.before_task_bind is not None:
                    self.before_task_bind()
                if self.current.manual_input_pending:
                    return {"written": False, "reason": "manual_input_pending"}
                changes["page_team"] = str(arg[3])
            self.current = replace(self.current, **changes)
            return {"written": True}
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

    async def fake_click_send(_page, timeout_ms, **_kwargs):
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


def test_wait_response_never_accepts_assistant_after_later_user(monkeypatch):
    user = MessageSnapshot("user", "u1", "user-turn-1", "expected prompt", ())
    malformed = MessageSnapshot(
        "assistant", "a1", "assistant-turn-1", "partial response ``", ()
    )
    later_user = MessageSnapshot("user", "u2", "user-turn-2", "later prompt", ())
    later = MessageSnapshot(
        "assistant", "a2", "assistant-turn-2", "valid later response", ()
    )
    page = DummyPage(
        snapshot(
            messages=(user, malformed, later_user, later),
            state=ChatGPTState.WAITING_PROMPT,
        )
    )
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
        user_message_id="u1",
        user_turn_id="user-turn-1",
    )

    async def fake_inspect(_page):
        return page.current

    def validate(candidate):
        if candidate.message_id == "a1":
            raise ValueError("malformed accepted response")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(chatgpt.StableMalformedResponseError) as captured:
        asyncio.run(
            client.wait_for_response(
                receipt,
                timeout_ms=20,
                stable_ms=0,
                poll_ms=1,
                candidate_validator=validate,
                minimum_samples=2,
                invalid_grace_ms=0,
            )
        )

    assert captured.value.candidate == malformed


def test_wait_response_selects_expected_assistant_before_later_turn(monkeypatch):
    user = MessageSnapshot("user", "u1", "user-turn-1", "expected prompt", ())
    expected = MessageSnapshot(
        "assistant", "a1", "assistant-turn-1", "valid expected response", ()
    )
    later_user = MessageSnapshot("user", "u2", "user-turn-2", "later prompt", ())
    later = MessageSnapshot(
        "assistant", "a2", "assistant-turn-2", "valid later response", ()
    )
    page = DummyPage(
        snapshot(
            messages=(user, expected, later_user, later),
            state=ChatGPTState.WAITING_PROMPT,
        )
    )
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
        user_message_id="u1",
        user_turn_id="user-turn-1",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    received = asyncio.run(
        client.wait_for_response(
            receipt,
            timeout_ms=20,
            stable_ms=0,
            poll_ms=1,
            minimum_samples=2,
            expected_assistant_turn_id="assistant-turn-1",
            expected_assistant_message_id="a1",
        )
    )

    assert received == expected


def test_wait_response_malformed_expected_identity_does_not_fall_through(monkeypatch):
    user = MessageSnapshot("user", "u1", "user-turn-1", "expected prompt", ())
    malformed = MessageSnapshot(
        "assistant", "a1", "assistant-turn-1", "partial response ``", ()
    )
    later_user = MessageSnapshot("user", "u2", "user-turn-2", "later prompt", ())
    later = MessageSnapshot(
        "assistant", "a2", "assistant-turn-2", "valid later response", ()
    )
    page = DummyPage(
        snapshot(
            messages=(user, malformed, later_user, later),
            state=ChatGPTState.WAITING_PROMPT,
        )
    )
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
        user_message_id="u1",
        user_turn_id="user-turn-1",
    )

    async def fake_inspect(_page):
        return page.current

    def validate(candidate):
        if candidate.message_id == "a1":
            raise ValueError("malformed expected response")

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(chatgpt.StableMalformedResponseError) as captured:
        asyncio.run(
            client.wait_for_response(
                receipt,
                timeout_ms=20,
                stable_ms=0,
                poll_ms=1,
                candidate_validator=validate,
                minimum_samples=2,
                invalid_grace_ms=0,
                expected_assistant_turn_id="assistant-turn-1",
                expected_assistant_message_id="a1",
            )
        )

    assert captured.value.candidate == malformed


def test_wait_response_missing_expected_identity_fails_closed(monkeypatch):
    user = MessageSnapshot("user", "u1", "user-turn-1", "expected prompt", ())
    unrelated = MessageSnapshot(
        "assistant", "a2", "assistant-turn-2", "valid unrelated response", ()
    )
    page = DummyPage(
        snapshot(messages=(user, unrelated), state=ChatGPTState.WAITING_PROMPT)
    )
    client = ChatGPTPage(page, timeout_ms=10)
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
        user_message_id="u1",
        user_turn_id="user-turn-1",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(TimeoutError, match="no accepted-user-identity"):
        asyncio.run(
            client.wait_for_response(
                receipt,
                timeout_ms=10,
                stable_ms=0,
                poll_ms=1,
                expected_assistant_turn_id="assistant-turn-1",
                expected_assistant_message_id="a1",
            )
        )


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


def test_bind_task_identity_rejects_draft_inserted_at_write_without_mutation(monkeypatch):
    page = DummyPage(snapshot(task_id="TASK-OLD", team="old-team"))
    client = ChatGPTPage(page, timeout_ms=1)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    def insert_draft_at_identity_write():
        page.current = replace(
            page.current,
            composer_text="manual draft",
            state=ChatGPTState.DRAFT,
        )

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    preflight = asyncio.run(client.task_preflight("TASK-NEW"))
    assert preflight["requires_new_chat"] is True
    page.before_task_bind = insert_draft_at_identity_write

    with pytest.raises(ComposerConflictError, match="manual draft"):
        asyncio.run(client.bind_task_identity("TASK-NEW", "new-team"))

    assert page.current.page_task_id == "TASK-OLD"
    assert page.current.page_team == "old-team"
    assert page.current.composer_text == "manual draft"


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


def test_prepare_prompt_locked_rejects_wrong_same_count_attachment_name(monkeypatch):
    page = DummyPage(
        snapshot(
            composer_text="exact prompt",
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
            attachments=("manual-unowned.txt",),
        )
    )
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="attachment"):
        asyncio.run(
            client._prepare_prompt_locked(
                "exact prompt",
                100,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        )


def test_send_locked_gate_accepts_exact_attachment_names_once(monkeypatch):
    page = DummyPage(
        snapshot(
            composer_text="exact prompt",
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
            attachments=("context.txt",),
        )
    )
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = PageBinding("page-1", "DEV")
    accepted = MessageSnapshot("user", "u1", "t1", "exact prompt", ())

    async def fake_inspect(_page):
        return page.current

    async def fake_click(_page, timeout_ms, **_kwargs):
        page.clicks += 1
        return "dom_click"

    async def fake_acceptance(_baseline, _prompt, *, timeout_ms):
        return "exact_user_message", accepted

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click)
    monkeypatch.setattr(client, "_wait_send_acceptance", fake_acceptance)

    receipt = asyncio.run(
        client.send(
            "exact prompt",
            timeout_ms=100,
            max_attempts=1,
            recovery_reload=False,
            wait_for_stop=False,
            expected_attachment_ownership_token="fake-upload-token",
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
    )

    assert page.clicks == 1
    assert receipt.user_message_id == "u1"
    assert receipt.user_turn_id == "t1"


def test_send_reload_recovery_rejects_wrong_same_count_before_second_click(monkeypatch):
    page = DummyPage(
        snapshot(
            composer_text="exact prompt",
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
            attachments=("context.txt",),
        )
    )
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = PageBinding("page-1", "DEV")

    async def fake_inspect(_page):
        return page.current

    async def fake_click(_page, timeout_ms, **_kwargs):
        page.clicks += 1
        raise RuntimeError("synthetic click uncertainty")

    async def fake_refresh(_page, timeout_ms):
        page.current = snapshot(
            composer_text="exact prompt",
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
            attachments=("manual-unowned.txt",),
        )

    async def no_acceptance(_baseline, _prompt, *, timeout_ms):
        return None

    monkeypatch.setattr(chatgpt, "inspect_chatgpt_page", fake_inspect)
    monkeypatch.setattr(chatgpt, "click_send_button", fake_click)
    monkeypatch.setattr(chatgpt, "refresh_page", fake_refresh)
    monkeypatch.setattr(client, "_wait_send_acceptance", no_acceptance)

    with pytest.raises(ComposerConflictError, match="attachment"):
        asyncio.run(
            client.send(
                "exact prompt",
                timeout_ms=100,
                max_attempts=2,
                recovery_reload=True,
                wait_for_stop=False,
                expected_attachment_ownership_token="fake-upload-token",
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        )

    assert page.clicks == 1


def test_atomic_send_callback_rejects_wrong_same_count_attachment(monkeypatch):
    class AtomicPage(DummyPage):
        async def evaluate(self, _expression, arg=None):
            if isinstance(arg, list) and len(arg) >= 3:
                _expected_prompt, expected_names, expected_count, *_rest = arg
                actual = tuple(self.current.attachment_markers)
                matches = (
                    actual == tuple(expected_names)
                    if expected_names is not None
                    else len(actual) == expected_count
                )
                if not matches:
                    return {
                        "ok": False,
                        "method": "attachment_conflict",
                        "actual": list(actual),
                    }
                self.clicks += 1
                return {"ok": True, "method": "dom_click"}
            return await super().evaluate(_expression, arg)

    page = AtomicPage(
        snapshot(
            composer_text="exact prompt",
            send_visible=True,
            send_enabled=True,
            state=ChatGPTState.DRAFT,
            attachments=("manual-unowned.txt",),
        )
    )

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    with pytest.raises(ComposerConflictError, match="attachment"):
        asyncio.run(
            chatgpt.click_send_button(
                page,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        )

    assert page.clicks == 0


ATTACHMENT_DOM_HTML = """
<!doctype html>
<html>
<body>
  <form id="composer-form" style="display:block;width:800px;height:300px">
    <div id="prompt-textarea" contenteditable="true" role="textbox"
         style="display:block;width:500px;height:60px">exact prompt</div>
    <div id="attachments"></div>
    <button type="button" data-testid="send-button" aria-label="Send prompt"
            onclick="window.sendClicks += 1">Send</button>
  </form>
  <script>window.sendClicks = 0;</script>
</body>
</html>
"""


def _attachment_html(name: str) -> str:
    return f"""
    <div data-testid="file-attachment" style="display:block;width:300px;height:40px">
      <span>{name}</span>
      <button type="button" aria-label="Remove file {name}">Remove</button>
    </div>
    """


def _test_file_identity(
    data: bytes,
    *,
    name: str = "context.txt",
    mime_type: str = "text/plain",
) -> FileIdentity:
    return FileIdentity(
        path=f"/tmp/{name}",
        name=name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        mime_type=mime_type,
    )


async def _bind_real_task_identity(
    page,
    client: ChatGPTPage,
    *,
    task_id: str = "task-a",
    team: str = "alpha",
) -> None:
    await page.locator("#prompt-textarea").evaluate(
        "element => { element.innerText = ''; }"
    )
    await client.bind_task_identity(task_id, team, timeout_ms=500)
    await page.locator("#prompt-textarea").evaluate(
        "element => { element.innerText = 'exact prompt'; }"
    )


async def _with_real_attachment_page(
    callback,
    *,
    url: str | None = "https://chatgpt.com/c/attachment-test",
):
    async with async_playwright() as playwright:
        bundled = Path(playwright.chromium.executable_path)
        executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
        assert executable.is_file(), "Chromium executable is required for attachment DOM regression"
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(executable),
        )
        try:
            page = await browser.new_page(viewport={"width": 1000, "height": 700})
            if url is None:
                await page.set_content(ATTACHMENT_DOM_HTML)
            else:
                async def serve(route):
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=ATTACHMENT_DOM_HTML,
                    )

                await page.route("https://chatgpt.com/**", serve)
                await page.goto(url, wait_until="domcontentloaded")
            return await callback(page)
        finally:
            await browser.close()


async def _prepare_real_owned_attachment(page, file_value) -> str:
    if isinstance(file_value, dict):
        data = bytes(file_value["buffer"])
        identity = _test_file_identity(
            data,
            name=str(file_value.get("name") or "context.txt"),
            mime_type=str(file_value.get("mimeType") or "application/octet-stream"),
        )
    else:
        path = Path(file_value)
        identity = collect_file_identities([path])[0]
        file_value = {
            "name": identity.name,
            "mimeType": identity.mime_type,
            "buffer": path.read_bytes(),
        }
    await page.locator("#composer-form").evaluate(
        """form => {
          const existing = document.querySelector('#owned-file-input');
          if (existing) existing.remove();
          const input = document.createElement('input');
          input.id = 'owned-file-input';
          input.type = 'file';
          form.appendChild(input);
        }"""
    )
    await page.locator("#owned-file-input").set_input_files(file_value)
    await page.locator("#attachments").evaluate(
        "(root, html) => { root.innerHTML = html; }",
        _attachment_html("context.txt"),
    )
    await page.evaluate(
        "window.ownedFile = document.querySelector('#owned-file-input').files[0]"
    )
    return await upload_module.establish_attachment_ownership(
        page,
        expected_files=(identity,),
    )


async def _install_trusted_send_handler(
    page,
    *,
    consume_attachment: bool = False,
    append_message: bool = True,
    clear_composer: bool = True,
    show_stop: bool = False,
    transition_path: str | None = None,
    replace_attachment_without_acceptance: bool = False,
) -> None:
    await page.locator("[data-testid='send-button']").evaluate(
        """(button, args) => {
          button.onclick = null;
          window.clickedOwnedFile = null;
          window.inputReplacements = 0;
          button.addEventListener('click', () => {
            window.sendClicks += 1;
            const input = document.querySelector('#owned-file-input');
            if (args.replaceAttachmentWithoutAcceptance && input) {
              const replacement = document.createElement('input');
              replacement.id = 'owned-file-input';
              replacement.type = 'file';
              const transfer = new DataTransfer();
              transfer.items.add(new File(['persistent replacement'], 'context.txt', {type: 'text/plain'}));
              replacement.files = transfer.files;
              input.replaceWith(replacement);
              window.inputReplacements += 1;
              window.clickedOwnedFile = replacement.files[0] === window.ownedFile;
              return;
            }
            if (args.consumeAttachment && input) {
              window.clickedOwnedFile = input.files[0] === window.ownedFile;
            }
            if (args.transitionPath) {
              history.pushState({}, '', args.transitionPath);
            }
            if (args.appendMessage) {
              const turn = document.createElement('div');
              turn.setAttribute('data-turn-id', 't1');
              const message = document.createElement('div');
              message.setAttribute('data-message-author-role', 'user');
              message.setAttribute('data-message-id', 'u1');
              message.innerText = document.querySelector('#prompt-textarea').innerText;
              turn.appendChild(message);
              document.body.appendChild(turn);
            }
            if (args.clearComposer) {
              document.querySelector('#prompt-textarea').innerText = '';
            }
            if (args.consumeAttachment) {
              document.querySelector('#attachments').innerHTML = '';
              if (input) input.remove();
            }
            if (args.showStop) {
              const stop = document.createElement('button');
              stop.type = 'button';
              stop.setAttribute('data-testid', 'stop-button');
              stop.setAttribute('aria-label', 'Stop generating');
              stop.textContent = 'Stop';
              document.body.appendChild(stop);
            }
          }, {once: true});
        }""",
        {
            "consumeAttachment": consume_attachment,
            "appendMessage": append_message,
            "clearComposer": clear_composer,
            "showStop": show_stop,
            "transitionPath": transition_path,
            "replaceAttachmentWithoutAcceptance": replace_attachment_without_acceptance,
        },
    )


def test_real_browser_indexed_remove_file_label_uses_exact_filename(tmp_path):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-bytes")
    expected = collect_file_identities([attachment])

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'upload-files';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#upload-files").set_input_files(
            {
                "name": expected[0].name,
                "mimeType": expected[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root) => { root.innerHTML = '<div data-testid=\"file-attachment\" style=\"display:block;width:300px;height:40px\"><button type=\"button\" aria-label=\"Remove file 1: context.txt\">Remove</button></div>'; }"
        )
        inspected = await chatgpt.inspect_chatgpt_page(page)

        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.txt",),
            timeout_ms=100,
            poll_ms=10,
        )
        token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=expected,
        )
        persisted = await upload_module.current_attachment_ownership_token(
            page,
            expected_files=expected,
        )
        return inspected.attachment_markers, ready.attachment_markers, token, persisted

    inspected, ready, token, persisted = asyncio.run(_with_real_attachment_page(probe))
    assert inspected == ("context.txt",)
    assert ready == ("context.txt",)
    assert token
    assert persisted == token


def test_real_browser_platform_duplicate_suffix_maps_to_expected_attachment_identity(
    tmp_path,
    monkeypatch,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)
    attachment = tmp_path / "context.md"
    attachment.write_bytes(b"owned-bytes")
    expected = collect_file_identities([attachment])

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'upload-files';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#upload-files").set_input_files(
            {
                "name": expected[0].name,
                "mimeType": expected[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root) => { root.innerHTML = '<div data-testid=\"file-attachment\" style=\"display:block;width:300px;height:40px\"><button type=\"button\" aria-label=\"Remove file 1: context(1).md\">Remove</button></div>'; }"
        )

        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.md",),
            timeout_ms=100,
            poll_ms=10,
        )
        token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=expected,
        )
        persisted = await upload_module.current_attachment_ownership_token(
            page,
            expected_files=expected,
        )
        method = await chatgpt.click_send_button(
            page,
            expected_attachment_ownership_token=token,
            expected_attachment_count=1,
            expected_attachment_names=("context.md",),
        )
        clicks = await page.evaluate("window.sendClicks")
        return ready.attachment_markers, token, persisted, method, clicks

    markers, token, persisted, method, clicks = asyncio.run(
        _with_real_attachment_page(probe)
    )
    assert markers == ("context(1).md",)
    assert token
    assert persisted == token
    assert method == "dom_click"
    assert clicks == 1


def test_real_browser_current_ownership_token_rejects_silent_same_name_replacement(
    tmp_path,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    expected = collect_file_identities([attachment])

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'upload-files';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#upload-files").set_input_files(
            {
                "name": expected[0].name,
                "mimeType": expected[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=expected,
        )
        await page.locator("#upload-files").evaluate(
            """input => {
              const transfer = new DataTransfer();
              transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
              input.files = transfer.files;
            }"""
        )
        persisted = await upload_module.current_attachment_ownership_token(
            page,
            expected_files=expected,
        )
        return token, persisted

    token, persisted = asyncio.run(_with_real_attachment_page(probe))

    assert token
    assert persisted is None


def test_real_browser_attachment_ownership_requires_secure_hashing_context(tmp_path):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    expected = collect_file_identities([attachment])

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'upload-files';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#upload-files").set_input_files(
            {
                "name": expected[0].name,
                "mimeType": expected[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        with pytest.raises(UploadReadinessError, match="secure browser SHA-256"):
            await upload_module.establish_attachment_ownership(
                page,
                expected_files=expected,
            )

    asyncio.run(_with_real_attachment_page(probe, url=None))


def test_real_browser_upload_chooses_general_composer_input_before_camera(
    tmp_path,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"authoritative-bytes")
    snapshots = collect_file_snapshots([attachment])

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              for (const [id, accept] of [
                ['upload-files', ''],
                ['upload-photos', 'image/*'],
                ['upload-camera', 'image/*'],
              ]) {
                const input = document.createElement('input');
                input.type = 'file';
                input.id = id;
                input.accept = accept;
                input.multiple = true;
                form.appendChild(input);
              }
            }"""
        )
        assert await upload_module._upload_via_input(page, snapshots) is True
        return await page.evaluate(
            """() => Object.fromEntries(
              ['upload-files', 'upload-photos', 'upload-camera'].map(id => [
                id,
                [...document.getElementById(id).files].map(file => file.name),
              ])
            )"""
        )

    assert asyncio.run(_with_real_attachment_page(probe)) == {
        "upload-files": ["context.txt"],
        "upload-photos": [],
        "upload-camera": [],
    }


def test_real_browser_attachment_filename_contract(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        inspected = await chatgpt.inspect_chatgpt_page(page)

        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.txt",),
            timeout_ms=500,
            poll_ms=10,
        )
        valid_method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        valid_clicks = await page.evaluate("window.sendClicks")

        await page.evaluate("window.sendClicks = 0")
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("manual-unowned.txt"),
        )
        wrong_error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            wrong_error = type(exc).__name__
        wrong_clicks = await page.evaluate("window.sendClicks")

        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt") + _attachment_html("logs.txt"),
        )
        multiple = await chatgpt.inspect_chatgpt_page(page)
        await page.evaluate("window.sendClicks = 0")
        multi_method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=2,
            expected_attachment_names=("context.txt", "logs.txt"),
        )
        multi_clicks = await page.evaluate("window.sendClicks")

        await page.locator("#attachments").evaluate(
            "(root) => { root.innerHTML = '<div data-testid=\"file-attachment\" style=\"display:block;width:300px;height:40px\"><button type=\"button\" aria-label=\"Remove file\">Remove</button></div>'; }"
        )
        unidentified = await chatgpt.inspect_chatgpt_page(page)
        await page.evaluate("window.sendClicks = 0")
        unidentified_error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            unidentified_error = type(exc).__name__
        unidentified_clicks = await page.evaluate("window.sendClicks")
        return {
            "inspected": inspected.attachment_markers,
            "ready": ready.attachment_markers,
            "valid_method": valid_method,
            "valid_clicks": valid_clicks,
            "wrong_error": wrong_error,
            "wrong_clicks": wrong_clicks,
            "multiple": multiple.attachment_markers,
            "multi_method": multi_method,
            "multi_clicks": multi_clicks,
            "unidentified": unidentified.attachment_markers,
            "unidentified_error": unidentified_error,
            "unidentified_clicks": unidentified_clicks,
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "inspected": ("context.txt",),
        "ready": ("context.txt",),
        "valid_method": "dom_click",
        "valid_clicks": 1,
        "wrong_error": "ComposerConflictError",
        "wrong_clicks": 0,
        "multiple": ("context.txt", "logs.txt"),
        "multi_method": "dom_click",
        "multi_clicks": 1,
        "unidentified": ("\x00unidentified attachment",),
        "unidentified_error": "ComposerConflictError",
        "unidentified_clicks": 0,
    }


def _shared_attachment_wrapper_html(names: tuple[str, ...]) -> str:
    chips = "".join(
        f"""
        <div class="chip" style="display:block;width:300px;height:40px">
          <span>{name}</span>
          <button type="button" aria-label="Remove file {name}">Remove</button>
        </div>
        """
        for name in names
    )
    return f"""
    <div data-testid="file-upload-container" style="display:block;width:400px;height:120px">
      {chips}
    </div>
    <button type="button" data-testid="accounts-profile-button" aria-label="Profile">Profile</button>
    """


def test_real_browser_shared_wrapper_preserves_each_file_identity(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _shared_attachment_wrapper_html(("context.txt", "logs.txt")),
        )
        inspected = await chatgpt.inspect_chatgpt_page(page)

        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.txt", "logs.txt"),
            timeout_ms=500,
            poll_ms=10,
        )
        method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=2,
            expected_attachment_names=("context.txt", "logs.txt"),
        )
        clicks = await page.evaluate("window.sendClicks")

        await page.evaluate("window.sendClicks = 0")
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _shared_attachment_wrapper_html(("context.txt", "manual-unowned.txt")),
        )
        wrong = await chatgpt.inspect_chatgpt_page(page)
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_attachment_count=2,
                expected_attachment_names=("context.txt", "logs.txt"),
            )
        except Exception as exc:
            error = type(exc).__name__
        wrong_clicks = await page.evaluate("window.sendClicks")

        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _shared_attachment_wrapper_html(("same.txt", "same.txt")),
        )
        duplicate_names = await chatgpt.inspect_chatgpt_page(page)
        return {
            "inspected": inspected.attachment_markers,
            "ready": ready.attachment_markers,
            "method": method,
            "clicks": clicks,
            "wrong": wrong.attachment_markers,
            "error": error,
            "wrong_clicks": wrong_clicks,
            "duplicate_names": duplicate_names.attachment_markers,
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "inspected": ("context.txt", "logs.txt"),
        "ready": ("context.txt", "logs.txt"),
        "method": "dom_click",
        "clicks": 1,
        "wrong": ("context.txt", "manual-unowned.txt"),
        "error": "ComposerConflictError",
        "wrong_clicks": 0,
        "duplicate_names": ("same.txt", "same.txt"),
    }


@pytest.mark.parametrize(
    "hidden_evidence",
    [
        '<span data-filename="stale.txt" style="display:none"></span>',
        '<button type="button" aria-label="Remove file stale.txt" style="display:none">Remove</button>',
        '<span data-filename="stale.txt" style="visibility:hidden;width:10px;height:10px;display:inline-block"></span>',
        '<button type="button" aria-label="Remove file stale.txt" style="visibility:hidden;width:10px;height:10px;display:inline-block">Remove</button>',
        '<span data-filename="stale.txt" style="visibility:collapse;width:10px;height:10px;display:inline-block"></span>',
    ],
    ids=(
        "display-none-metadata",
        "display-none-aria",
        "visibility-hidden-metadata",
        "visibility-hidden-aria",
        "visibility-collapse-metadata",
    ),
)
def test_real_browser_hidden_attachment_evidence_does_not_suppress_visible_leaf(
    monkeypatch,
    hidden_evidence: str,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        html = f"""
        <div data-testid="file-attachment" style="display:block;width:300px;height:60px">
          <span>context.txt</span>
          {hidden_evidence}
          <button type="button" aria-label="Remove file">Remove</button>
        </div>
        """
        await page.locator("#attachments").evaluate(
            "(root, value) => { root.innerHTML = value; }",
            html,
        )
        inspected = await chatgpt.inspect_chatgpt_page(page)

        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.txt",),
            timeout_ms=500,
            poll_ms=10,
        )
        method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        clicks = await page.evaluate("window.sendClicks")
        return inspected.attachment_markers, ready.attachment_markers, method, clicks

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == (("context.txt",), ("context.txt",), "dom_click", 1)


def test_real_browser_css_hidden_expected_name_cannot_mask_visible_wrong_file(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        html = """
        <div data-testid="file-attachment" style="display:block;width:300px;height:60px">
          <span>manual-unowned.txt</span>
          <span data-filename="context.txt"
                style="visibility:hidden;width:10px;height:10px;display:inline-block"></span>
          <button type="button" aria-label="Remove file">Remove</button>
        </div>
        """
        await page.locator("#attachments").evaluate(
            "(root, value) => { root.innerHTML = value; }",
            html,
        )
        inspected = await chatgpt.inspect_chatgpt_page(page)
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            error = type(exc).__name__
        clicks = await page.evaluate("window.sendClicks")
        return inspected.attachment_markers, error, clicks

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == (("manual-unowned.txt",), "ComposerConflictError", 0)


def test_real_browser_hidden_explicit_ancestor_never_supplies_attachment_identity(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        class InspectClient:
            async def assert_ownership(self):
                return await chatgpt.inspect_chatgpt_page(page)

        await page.locator("#attachments").evaluate(
            "(root, value) => { root.innerHTML = value; }",
            """
            <div data-testid="file-attachment" style="display:block;width:300px;height:80px">
              <span>context.txt</span>
              <div data-filename="stale.txt"
                   style="visibility:hidden;width:200px;height:30px">
                <button type="button" aria-label="Remove file"
                        style="visibility:visible;width:20px;height:20px">Remove</button>
              </div>
            </div>
            """,
        )
        visible_leaf = await chatgpt.inspect_chatgpt_page(page)
        ready = await wait_upload_ready(
            InspectClient(),
            request_marker="exact prompt",
            expected_names=("context.txt",),
            timeout_ms=500,
            poll_ms=10,
        )
        visible_leaf_method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        visible_leaf_clicks = await page.evaluate("window.sendClicks")

        await page.evaluate("window.sendClicks = 0")
        await page.locator("#attachments").evaluate(
            "(root, value) => { root.innerHTML = value; }",
            """
            <div style="display:block;width:300px;height:80px">
              <div data-filename="context.txt"
                   style="visibility:hidden;width:200px;height:30px">
                <button type="button" aria-label="Remove file"
                        style="visibility:visible;width:20px;height:20px">Remove</button>
              </div>
            </div>
            """,
        )
        hidden_only = await chatgpt.inspect_chatgpt_page(page)
        hidden_only_error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            hidden_only_error = type(exc).__name__
        hidden_only_clicks = await page.evaluate("window.sendClicks")

        await page.locator("#attachments").evaluate(
            "(root, value) => { root.innerHTML = value; }",
            """
            <div style="display:block;width:300px;height:80px">
              <div data-filename="stale.txt"
                   style="visibility:hidden;width:200px;height:30px">
                <button type="button" aria-label="Remove file context.txt"
                        style="visibility:visible;width:20px;height:20px">Remove</button>
              </div>
            </div>
            """,
        )
        visible_child = await chatgpt.inspect_chatgpt_page(page)
        visible_child_method = await chatgpt.click_send_button(
            page,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        visible_child_clicks = await page.evaluate("window.sendClicks")

        return {
            "visible_leaf": visible_leaf.attachment_markers,
            "ready": ready.attachment_markers,
            "visible_leaf_method": visible_leaf_method,
            "visible_leaf_clicks": visible_leaf_clicks,
            "hidden_only": hidden_only.attachment_markers,
            "hidden_only_error": hidden_only_error,
            "hidden_only_clicks": hidden_only_clicks,
            "visible_child": visible_child.attachment_markers,
            "visible_child_method": visible_child_method,
            "visible_child_clicks": visible_child_clicks,
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "visible_leaf": ("context.txt",),
        "ready": ("context.txt",),
        "visible_leaf_method": "dom_click",
        "visible_leaf_clicks": 1,
        "hidden_only": (),
        "hidden_only_error": "ComposerConflictError",
        "hidden_only_clicks": 0,
        "visible_child": ("context.txt",),
        "visible_child_method": "dom_click",
        "visible_child_clicks": 1,
    }


def test_real_browser_explicit_filename_promotion_stays_inside_composer_host(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        async def attempt():
            inspected = await chatgpt.inspect_chatgpt_page(page)
            error = None
            try:
                await chatgpt.click_send_button(
                    page,
                    expected_attachment_count=1,
                    expected_attachment_names=("context.txt",),
                )
            except Exception as exc:
                error = type(exc).__name__
            clicks = await page.evaluate("window.sendClicks")
            return inspected.attachment_markers, error, clicks

        await page.locator("#composer-form").evaluate(
            "form => form.setAttribute('data-filename', 'context.txt')"
        )
        host_result = await attempt()

        await page.evaluate(
            """() => {
              window.sendClicks = 0;
              const form = document.querySelector('#composer-form');
              form.removeAttribute('data-filename');
              const outer = document.createElement('div');
              outer.id = 'outer-composer-wrapper';
              outer.setAttribute('data-filename', 'context.txt');
              outer.style.cssText = 'display:block;width:900px;height:350px';
              form.parentNode.insertBefore(outer, form);
              outer.appendChild(form);
            }"""
        )
        outer_result = await attempt()
        return host_result, outer_result

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == (
        ((), "ComposerConflictError", 0),
        ((), "ComposerConflictError", 0),
    )


@pytest.mark.parametrize(
    "mutation_script",
    [
        "element => { element.innerText = 'manual changed prompt'; }",
        "element => { element.setAttribute('contenteditable', 'false'); }",
        (
            "element => { element.removeAttribute('contenteditable'); "
            "element.parentElement.setAttribute('contenteditable', 'false'); }"
        ),
    ],
)
def test_real_browser_atomic_send_rejects_composer_changed_during_delay(
    monkeypatch, mutation_script: str
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.locator("#prompt-textarea").evaluate(mutation_script)
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        attached_error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_prompt="exact prompt",
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            attached_error = type(exc).__name__
        attached_clicks = await page.evaluate("window.sendClicks")

        await page.evaluate(
            """() => {
              window.sendClicks = 0;
              document.querySelector('#prompt-textarea').innerText = 'exact prompt';
              document.querySelector('#prompt-textarea').setAttribute('contenteditable', 'true');
              document.querySelector('#attachments').innerHTML = '';
            }"""
        )
        plain_error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_prompt="exact prompt",
            )
        except Exception as exc:
            plain_error = type(exc).__name__
        plain_clicks = await page.evaluate("window.sendClicks")
        return attached_error, attached_clicks, plain_error, plain_clicks

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == ("ComposerConflictError", 0, "ComposerConflictError", 0)


@pytest.mark.parametrize(
    "mutation_script",
    [
        "element => { element.innerText = 'manual changed prompt'; }",
        "element => { element.setAttribute('contenteditable', 'false'); }",
        (
            "element => { element.removeAttribute('contenteditable'); "
            "element.parentElement.setAttribute('contenteditable', 'false'); }"
        ),
    ],
)
def test_real_browser_send_returns_no_receipt_when_composer_changes_during_delay(
    monkeypatch, mutation_script: str
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.locator("#prompt-textarea").evaluate(mutation_script)
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              button.onclick = () => {
                window.sendClicks += 1;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              };
            }"""
        )
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        ownership_token = await _prepare_real_owned_attachment(
            page,
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            },
        )
        error = None
        receipt = None
        try:
            receipt = await client.send(
                "exact prompt",
                timeout_ms=500,
                max_attempts=1,
                recovery_reload=False,
                wait_for_stop=False,
                expected_attachment_ownership_token=ownership_token,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            error = type(exc).__name__
        snapshot_after = await chatgpt.inspect_chatgpt_page(page)
        return {
            "error": error,
            "receipt": receipt,
            "clicks": await page.evaluate("window.sendClicks"),
            "messages": tuple((item.role, item.text) for item in snapshot_after.messages),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "ComposerConflictError",
        "receipt": None,
        "clicks": 0,
        "messages": (),
    }


@pytest.mark.parametrize("with_attachment", [False, True])
@pytest.mark.parametrize(
    ("mutation_kind", "expected_error"),
    [
        ("conversation", "PageOwnershipError"),
        ("page_id", "PageOwnershipError"),
        ("role", "PageOwnershipError"),
        ("dialog", "UnsafePageStateError"),
        ("stop", "UnsafePageStateError"),
    ],
)
def test_real_browser_atomic_send_rejects_ownership_or_page_state_changed_during_delay(
    monkeypatch,
    mutation_kind: str,
    expected_error: str,
    with_attachment: bool,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action != "send":
            return 0.0
        if mutation_kind == "conversation":
            await page.evaluate("history.pushState({}, '', '/c/other')")
        elif mutation_kind == "page_id":
            await page.evaluate(
                "([key, value]) => sessionStorage.setItem(key, value)",
                [chatgpt.PAGE_ID_STORAGE_KEY, "other-page"],
            )
        elif mutation_kind == "role":
            await page.evaluate(
                "([key, value]) => sessionStorage.setItem(key, value)",
                [chatgpt.ROLE_STORAGE_KEY, "OTHER"],
            )
        elif mutation_kind == "dialog":
            await page.evaluate(
                """() => {
                  const dialog = document.createElement('div');
                  dialog.setAttribute('role', 'dialog');
                  dialog.style.cssText = 'position:fixed;inset:0;display:block';
                  dialog.innerText = 'Confirm something';
                  document.body.appendChild(dialog);
                }"""
            )
        else:
            await page.evaluate(
                """() => {
                  const stop = document.createElement('button');
                  stop.setAttribute('data-testid', 'stop-button');
                  stop.setAttribute('aria-label', 'Stop generating');
                  stop.style.cssText = 'display:block;width:80px;height:30px';
                  document.body.appendChild(stop);
                }"""
            )
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        assert client.binding is not None
        expected_names = None
        expected_count = 0
        if with_attachment:
            await page.locator("#attachments").evaluate(
                "(root, html) => { root.innerHTML = html; }",
                _attachment_html("context.txt"),
            )
            expected_names = ("context.txt",)
            expected_count = 1
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_url="https://chatgpt.com/c/original",
                expected_page_id=client.binding.page_id,
                expected_role=client.binding.role,
                expected_prompt="exact prompt",
                expected_attachment_count=expected_count,
                expected_attachment_names=expected_names,
            )
        except Exception as exc:
            error = type(exc).__name__
        return error, await page.evaluate("window.sendClicks")

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == (expected_error, 0)


@pytest.mark.parametrize("with_attachment", [False, True])
@pytest.mark.parametrize(
    ("mutation_kind", "expected_error"),
    [
        ("task_id", "PageOwnershipError"),
        ("team", "PageOwnershipError"),
        ("task_team", "PageOwnershipError"),
        ("retry", "UnsafePageStateError"),
        ("error_alert", "UnsafePageStateError"),
    ],
)
def test_real_browser_atomic_send_rejects_task_team_or_error_state_changed_during_delay(
    monkeypatch,
    mutation_kind: str,
    expected_error: str,
    with_attachment: bool,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action != "send":
            return 0.0
        if mutation_kind in {"task_id", "team", "task_team"}:
            await page.evaluate(
                """([taskKey, teamKey, prefix, kind]) => {
                  const taskId = kind === 'team' ? 'task-a' : 'task-b';
                  const team = kind === 'task_id' ? 'alpha' : 'beta';
                  sessionStorage.setItem(taskKey, taskId);
                  sessionStorage.setItem(teamKey, team);
                  const current = window.name.startsWith(prefix)
                    ? JSON.parse(window.name.slice(prefix.length))
                    : {};
                  window.name = prefix + JSON.stringify({
                    role: current.role,
                    pageId: current.pageId,
                    taskId,
                    team,
                  });
                }""",
                [
                    chatgpt.TASK_ID_STORAGE_KEY,
                    chatgpt.TEAM_STORAGE_KEY,
                    chatgpt.WINDOW_NAME_PREFIX,
                    mutation_kind,
                ],
            )
        elif mutation_kind == "retry":
            await page.evaluate(
                """() => {
                  const retry = document.createElement('button');
                  retry.setAttribute('data-testid', 'regenerate-thread-error-button');
                  retry.style.cssText = 'display:block;width:80px;height:30px';
                  retry.innerText = 'Retry';
                  document.body.appendChild(retry);
                }"""
            )
        else:
            await page.evaluate(
                """() => {
                  const alert = document.createElement('div');
                  alert.setAttribute('role', 'alert');
                  alert.style.cssText = 'display:block;width:300px;height:40px';
                  alert.innerText = 'Something went wrong. Try again.';
                  document.body.appendChild(alert);
                }"""
            )
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        assert client.binding is not None
        expected_names = None
        expected_count = 0
        if with_attachment:
            await page.locator("#attachments").evaluate(
                "(root, html) => { root.innerHTML = html; }",
                _attachment_html("context.txt"),
            )
            expected_names = ("context.txt",)
            expected_count = 1
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_url="https://chatgpt.com/c/original",
                expected_page_id=client.binding.page_id,
                expected_role=client.binding.role,
                expected_task_id="task-a",
                expected_team="alpha",
                expected_prompt="exact prompt",
                expected_attachment_count=expected_count,
                expected_attachment_names=expected_names,
            )
        except Exception as exc:
            error = type(exc).__name__
        return error, await page.evaluate("window.sendClicks")

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == (expected_error, 0)


@pytest.mark.parametrize(
    ("binding_mode", "expected_error", "expected_clicks"),
    [
        ("window_fallback", None, 1),
        ("malformed_fallback", "PageOwnershipError", 0),
        ("conflicting_fallback", "PageOwnershipError", 0),
    ],
)
def test_real_browser_atomic_send_task_team_window_name_fallback_is_exact(
    monkeypatch,
    binding_mode: str,
    expected_error: str | None,
    expected_clicks: int,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        assert client.binding is not None
        if binding_mode == "window_fallback":
            await page.evaluate(
                "([taskKey, teamKey]) => { sessionStorage.removeItem(taskKey); sessionStorage.removeItem(teamKey); }",
                [chatgpt.TASK_ID_STORAGE_KEY, chatgpt.TEAM_STORAGE_KEY],
            )
        elif binding_mode == "malformed_fallback":
            await page.evaluate(
                """([taskKey, teamKey, prefix]) => {
                  sessionStorage.removeItem(taskKey);
                  sessionStorage.removeItem(teamKey);
                  window.name = prefix + '{malformed';
                }""",
                [
                    chatgpt.TASK_ID_STORAGE_KEY,
                    chatgpt.TEAM_STORAGE_KEY,
                    chatgpt.WINDOW_NAME_PREFIX,
                ],
            )
        else:
            await page.evaluate(
                """([taskKey, prefix]) => {
                  sessionStorage.removeItem(taskKey);
                  const current = JSON.parse(window.name.slice(prefix.length));
                  current.team = 'beta';
                  window.name = prefix + JSON.stringify(current);
                }""",
                [chatgpt.TASK_ID_STORAGE_KEY, chatgpt.WINDOW_NAME_PREFIX],
            )
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_url="https://chatgpt.com/c/original",
                expected_page_id=client.binding.page_id,
                expected_role=client.binding.role,
                expected_task_id="task-a",
                expected_team="alpha",
                expected_prompt="exact prompt",
            )
        except Exception as exc:
            error = type(exc).__name__
        return error, await page.evaluate("window.sendClicks")

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == (expected_error, expected_clicks)


def test_real_browser_atomic_send_ignores_hidden_stale_error_evidence(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        assert client.binding is not None
        await page.evaluate(
            """() => {
              const alert = document.createElement('div');
              alert.setAttribute('role', 'alert');
              alert.style.display = 'none';
              alert.innerText = 'Something went wrong. Try again.';
              document.body.appendChild(alert);
              const retry = document.createElement('button');
              retry.setAttribute('data-testid', 'regenerate-thread-error-button');
              retry.style.display = 'none';
              retry.innerText = 'Retry';
              document.body.appendChild(retry);
            }"""
        )
        method = await chatgpt.click_send_button(
            page,
            expected_url="https://chatgpt.com/c/original",
            expected_page_id=client.binding.page_id,
            expected_role=client.binding.role,
            expected_task_id="task-a",
            expected_team="alpha",
            expected_prompt="exact prompt",
        )
        return method, await page.evaluate("window.sendClicks")

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == ("dom_click", 1)


def test_real_browser_send_returns_no_receipt_when_task_team_changes_during_delay(
    monkeypatch,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.evaluate(
                """([taskKey, teamKey, prefix]) => {
                  sessionStorage.setItem(taskKey, 'task-b');
                  sessionStorage.setItem(teamKey, 'beta');
                  const current = JSON.parse(window.name.slice(prefix.length));
                  window.name = prefix + JSON.stringify({
                    role: current.role,
                    pageId: current.pageId,
                    taskId: 'task-b',
                    team: 'beta',
                  });
                }""",
                [
                    chatgpt.TASK_ID_STORAGE_KEY,
                    chatgpt.TEAM_STORAGE_KEY,
                    chatgpt.WINDOW_NAME_PREFIX,
                ],
            )
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              button.onclick = () => {
                window.sendClicks += 1;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              };
            }"""
        )
        ownership_token = await _prepare_real_owned_attachment(
            page,
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            },
        )
        error = None
        receipt = None
        try:
            receipt = await client.send(
                "exact prompt",
                timeout_ms=500,
                max_attempts=1,
                recovery_reload=False,
                wait_for_stop=False,
                expected_attachment_ownership_token=ownership_token,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            error = type(exc).__name__
        snapshot_after = await chatgpt.inspect_chatgpt_page(page)
        return {
            "error": error,
            "receipt": receipt,
            "clicks": await page.evaluate("window.sendClicks"),
            "task_id": snapshot_after.page_task_id,
            "team": snapshot_after.page_team,
            "messages": tuple((item.role, item.text) for item in snapshot_after.messages),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "PageOwnershipError",
        "receipt": None,
        "clicks": 0,
        "task_id": "task-b",
        "team": "beta",
        "messages": (),
    }


@pytest.mark.parametrize(
    "mutation_kind",
    ["different_bytes", "same_size", "remove_readd"],
)
def test_real_browser_atomic_send_rejects_same_name_attachment_instance_replacement(
    monkeypatch,
    mutation_kind: str,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action != "send":
            return 0.0
        file_input = page.locator("#owned-file-input")
        if mutation_kind == "different_bytes":
            await file_input.set_input_files(
                {
                    "name": "context.txt",
                    "mimeType": "text/plain",
                    "buffer": b"manual-replacement",
                }
            )
        elif mutation_kind == "same_size":
            await file_input.set_input_files(
                {
                    "name": "context.txt",
                    "mimeType": "text/plain",
                    "buffer": b"manual-bbbb",
                }
            )
        else:
            await file_input.set_input_files([])
            await file_input.set_input_files(
                {
                    "name": "context.txt",
                    "mimeType": "text/plain",
                    "buffer": b"owned-aaaa",
                }
            )
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=(_test_file_identity(b"owned-aaaa"),),
        )
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_prompt="exact prompt",
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
                expected_attachment_ownership_token=ownership_token,
            )
        except Exception as exc:
            error = type(exc).__name__
        return error, await page.evaluate("window.sendClicks")

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == ("ComposerConflictError", 0)


def test_real_browser_durable_send_rejects_same_name_attachment_replacement(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    identities = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"

    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.locator("#owned-file-input").set_input_files(
                {
                    "name": "context.txt",
                    "mimeType": "text/plain",
                    "buffer": b"manual-bbbb",
                }
            )
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": identities[0].name,
                "mimeType": identities[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=identities,
        )
        ledger = RequestLedger(ledger_path)
        record = ledger.begin(
            role="alpha-plan",
            prompt="exact prompt",
            source_context={"task_id": "task-a", "team": "alpha"},
            files=identities,
            render_request_marker=False,
        )
        record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
        record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
        ledger.update(
            record.request_id,
            status=RequestStatus.UPLOAD_READY,
            upload_receipt=UploadReceipt(
                request_marker=record.rendered_prompt,
                method="input",
                files=identities,
                attachment_count=1,
                ownership_token=ownership_token,
            ).to_dict(),
        )
        error = None
        try:
            await DurableSendBlock(
                "exact prompt",
                ledger_path=ledger_path,
                files=[str(attachment)],
                source_context={"task_id": "task-a", "team": "alpha"},
                render_request_marker=False,
                wait_for_response=False,
                stable_ms=0,
            ).run(WorkflowContext(client))
        except Exception as exc:
            error = type(exc).__name__
        persisted = ledger.get(record.request_id)
        assert persisted is not None
        snapshot_after = await chatgpt.inspect_chatgpt_page(page)
        return {
            "error": error,
            "status": persisted.status.value,
            "receipt": persisted.receipt,
            "clicks": await page.evaluate("window.sendClicks"),
            "messages": tuple((item.role, item.text) for item in snapshot_after.messages),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "ComposerConflictError",
        "status": "sending",
        "receipt": None,
        "clicks": 0,
        "messages": (),
    }


def test_real_browser_controlled_upload_establishes_send_ownership(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-upload")

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'controlled-file-input';
              input.type = 'file';
              input.addEventListener('change', () => {
                const name = input.files?.[0]?.name || '';
                const root = document.querySelector('#attachments');
                root.innerHTML = name ? `
                  <div data-testid="file-attachment" style="display:block;width:300px;height:40px">
                    <span>${name}</span>
                    <button type="button" aria-label="Remove file ${name}">Remove</button>
                  </div>` : '';
              });
              form.appendChild(input);
            }"""
        )
        receipt = await client.upload_files(
            [str(attachment)],
            request_marker="exact prompt",
            timeout_ms=500,
        )
        snapshot_before = await client.assert_ownership()
        method = await chatgpt.click_send_button(
            page,
            expected_url=snapshot_before.url,
            expected_page_id=client.binding.page_id if client.binding else None,
            expected_role=client.binding.role if client.binding else None,
            expected_task_id="task-a",
            expected_team="alpha",
            expected_prompt="exact prompt",
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
            expected_attachment_ownership_token=receipt.ownership_token,
        )
        return {
            "token": receipt.ownership_token,
            "persisted_token": (
                await client.current_attachment_ownership_token(
                    expected_files=receipt.files
                )
            ),
            "method": method,
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result["token"]
    assert result["persisted_token"] == result["token"]
    assert result["method"] == "dom_click"
    assert result["clicks"] == 1


def test_real_browser_atomic_send_allows_trusted_attachment_consumption(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        ownership_token = await _prepare_real_owned_attachment(
            page,
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-original",
            },
        )
        await _install_trusted_send_handler(page, consume_attachment=True)
        method = await chatgpt.click_send_button(
            page,
            expected_prompt="exact prompt",
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
            expected_attachment_ownership_token=ownership_token,
        )
        snapshot_after = await chatgpt.inspect_chatgpt_page(page)
        return {
            "method": method,
            "clicks": await page.evaluate("window.sendClicks"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
            "messages": tuple((item.role, item.text) for item in snapshot_after.messages),
            "composer": snapshot_after.composer_text,
            "attachments": snapshot_after.attachment_markers,
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "method": "dom_click",
        "clicks": 1,
        "clicked_owned_file": True,
        "messages": (("user", "exact prompt"),),
        "composer": "",
        "attachments": (),
    }


def test_real_browser_send_accepts_trusted_attachment_consumption(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        ownership_token = await _prepare_real_owned_attachment(
            page,
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-original",
            },
        )
        await _install_trusted_send_handler(page, consume_attachment=True)
        receipt = await client.send(
            "exact prompt",
            timeout_ms=500,
            max_attempts=1,
            recovery_reload=False,
            wait_for_stop=False,
            expected_task_id="task-a",
            expected_team="alpha",
            expected_attachment_ownership_token=ownership_token,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        return {
            "accepted_via": receipt.accepted_via,
            "user_message_id": receipt.user_message_id,
            "clicks": await page.evaluate("window.sendClicks"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "accepted_via": "exact_user_message",
        "user_message_id": "u1",
        "clicks": 1,
        "clicked_owned_file": True,
    }


def test_real_browser_durable_send_persists_trusted_attachment_consumption(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-original")
    identities = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        ownership_token = await _prepare_real_owned_attachment(page, str(attachment))
        await _install_trusted_send_handler(page, consume_attachment=True)
        ledger = RequestLedger(ledger_path)
        record = ledger.begin(
            role="alpha-plan",
            prompt="exact prompt",
            source_context={"task_id": "task-a", "team": "alpha"},
            files=identities,
            render_request_marker=False,
        )
        record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
        record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
        ledger.update(
            record.request_id,
            status=RequestStatus.UPLOAD_READY,
            upload_receipt=UploadReceipt(
                request_marker=record.rendered_prompt,
                method="input",
                files=identities,
                attachment_count=1,
                ownership_token=ownership_token,
            ).to_dict(),
        )
        result = await DurableSendBlock(
            "exact prompt",
            ledger_path=ledger_path,
            files=[str(attachment)],
            source_context={"task_id": "task-a", "team": "alpha"},
            render_request_marker=False,
            wait_for_response=False,
            wait_for_stop=False,
            max_attempts=1,
            recovery_reload=False,
            stable_ms=0,
        ).run(WorkflowContext(client))
        persisted = ledger.get(record.request_id)
        assert persisted is not None
        return {
            "status": persisted.status.value,
            "receipt": result["receipt"]["user_message_id"],
            "persisted_receipt": persisted.receipt["user_message_id"] if persisted.receipt else None,
            "clicks": await page.evaluate("window.sendClicks"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "status": "sent",
        "receipt": "u1",
        "persisted_receipt": "u1",
        "clicks": 1,
        "clicked_owned_file": True,
    }


@pytest.mark.parametrize(
    ("mode", "url", "expected_via", "expected_message_id", "expected_path"),
    [
        ("cleanup", "https://chatgpt.com/c/original", "exact_user_message", "u1", "/c/original"),
        ("stop", "https://chatgpt.com/c/original", "stop_button", None, "/c/original"),
        ("new_chat", "https://chatgpt.com/", "exact_user_message", "u1", "/c/new-session"),
    ],
)
def test_real_browser_send_accepts_trusted_post_click_progress(
    monkeypatch,
    mode: str,
    url: str,
    expected_via: str,
    expected_message_id: str | None,
    expected_path: str,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=300)
        await client.set_role("DEV")
        await _install_trusted_send_handler(
            page,
            append_message=mode != "stop",
            clear_composer=mode != "stop",
            show_stop=mode == "stop",
            transition_path="/c/new-session" if mode == "new_chat" else None,
        )
        receipt = await client.send(
            "exact prompt",
            timeout_ms=300,
            max_attempts=1,
            recovery_reload=False,
            wait_for_stop=False,
        )
        return {
            "accepted_via": receipt.accepted_via,
            "user_message_id": receipt.user_message_id,
            "clicks": await page.evaluate("window.sendClicks"),
            "path": await page.evaluate("location.pathname"),
        }

    result = asyncio.run(_with_real_attachment_page(probe, url=url))

    assert result == {
        "accepted_via": expected_via,
        "user_message_id": expected_message_id,
        "clicks": 1,
        "path": expected_path,
    }


def test_real_browser_persistent_attachment_drift_without_acceptance_stays_sending(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-original")
    identities = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=200)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        ownership_token = await _prepare_real_owned_attachment(page, str(attachment))
        await _install_trusted_send_handler(
            page,
            append_message=False,
            clear_composer=False,
            replace_attachment_without_acceptance=True,
        )
        ledger = RequestLedger(ledger_path)
        record = ledger.begin(
            role="alpha-plan",
            prompt="exact prompt",
            source_context={"task_id": "task-a", "team": "alpha"},
            files=identities,
            render_request_marker=False,
        )
        record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
        record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
        ledger.update(
            record.request_id,
            status=RequestStatus.UPLOAD_READY,
            upload_receipt=UploadReceipt(
                request_marker=record.rendered_prompt,
                method="input",
                files=identities,
                attachment_count=1,
                ownership_token=ownership_token,
            ).to_dict(),
        )
        error = None
        try:
            await DurableSendBlock(
                "exact prompt",
                ledger_path=ledger_path,
                files=[str(attachment)],
                source_context={"task_id": "task-a", "team": "alpha"},
                render_request_marker=False,
                wait_for_response=False,
                wait_for_stop=False,
                max_attempts=1,
                recovery_reload=False,
                stable_ms=0,
            ).run(WorkflowContext(client))
        except Exception as exc:
            error = type(exc).__name__
        persisted = ledger.get(record.request_id)
        assert persisted is not None
        return {
            "error": error,
            "status": persisted.status.value,
            "receipt": persisted.receipt,
            "clicks": await page.evaluate("window.sendClicks"),
            "input_replacements": await page.evaluate("window.inputReplacements"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "SendRecoveryError",
        "status": "sending",
        "receipt": None,
        "clicks": 1,
        "input_replacements": 1,
        "clicked_owned_file": False,
    }


def test_real_browser_atomic_send_does_not_dispatch_focus_before_attachment_click(
    monkeypatch,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              const input = document.querySelector('#owned-file-input');
              window.focusEvents = 0;
              window.ownedFile = input.files[0];
              window.clickedOwnedFile = null;
              button.addEventListener('focus', () => {
                window.focusEvents += 1;
                const transfer = new DataTransfer();
                transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }, {once: true});
              button.addEventListener('click', () => {
                window.clickedOwnedFile = input.files[0] === window.ownedFile;
              }, {once: true});
            }"""
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=(_test_file_identity(b"owned-aaaa"),),
        )
        method = await chatgpt.click_send_button(
            page,
            expected_prompt="exact prompt",
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
            expected_attachment_ownership_token=ownership_token,
        )
        return {
            "method": method,
            "clicks": await page.evaluate("window.sendClicks"),
            "focus_events": await page.evaluate("window.focusEvents"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "method": "dom_click",
        "clicks": 1,
        "focus_events": 0,
        "clicked_owned_file": True,
    }


def test_real_browser_send_does_not_dispatch_focus_before_attachment_click(
    monkeypatch,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              const input = document.querySelector('#owned-file-input');
              window.focusEvents = 0;
              window.ownedFile = input.files[0];
              window.clickedOwnedFile = null;
              button.addEventListener('focus', () => {
                window.focusEvents += 1;
                const transfer = new DataTransfer();
                transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }, {once: true});
              button.onclick = () => {
                window.sendClicks += 1;
                window.clickedOwnedFile = input.files[0] === window.ownedFile;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              };
            }"""
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=(_test_file_identity(b"owned-aaaa"),),
        )
        receipt = await client.send(
            "exact prompt",
            timeout_ms=500,
            max_attempts=1,
            recovery_reload=False,
            wait_for_stop=False,
            expected_attachment_ownership_token=ownership_token,
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
        )
        return {
            "accepted_via": receipt.accepted_via,
            "user_message_id": receipt.user_message_id,
            "clicks": await page.evaluate("window.sendClicks"),
            "focus_events": await page.evaluate("window.focusEvents"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "accepted_via": "exact_user_message",
        "user_message_id": "u1",
        "clicks": 1,
        "focus_events": 0,
        "clicked_owned_file": True,
    }


@pytest.mark.parametrize("via_client", [False, True])
def test_real_browser_external_focus_attachment_mutation_is_rejected_before_click(
    monkeypatch,
    via_client: bool,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              const input = document.querySelector('#owned-file-input');
              window.focusEvents = 0;
              button.addEventListener('focus', () => {
                window.focusEvents += 1;
                const transfer = new DataTransfer();
                transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }, {once: true});
            }"""
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=(_test_file_identity(b"owned-aaaa"),),
        )
        await page.locator("[data-testid='send-button']").focus()
        error = None
        receipt = None
        try:
            if via_client:
                receipt = await client.send(
                    "exact prompt",
                    timeout_ms=500,
                    max_attempts=1,
                    recovery_reload=False,
                    wait_for_stop=False,
                    expected_attachment_ownership_token=ownership_token,
                    expected_attachment_count=1,
                    expected_attachment_names=("context.txt",),
                )
            else:
                await chatgpt.click_send_button(
                    page,
                    expected_prompt="exact prompt",
                    expected_attachment_count=1,
                    expected_attachment_names=("context.txt",),
                    expected_attachment_ownership_token=ownership_token,
                )
        except Exception as exc:
            error = type(exc).__name__
        return {
            "error": error,
            "receipt": receipt,
            "clicks": await page.evaluate("window.sendClicks"),
            "focus_events": await page.evaluate("window.focusEvents"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "ComposerConflictError",
        "receipt": None,
        "clicks": 0,
        "focus_events": 1,
    }


def test_real_browser_durable_send_does_not_focus_before_owned_attachment_click(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    identities = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": identities[0].name,
                "mimeType": identities[0].mime_type,
                "buffer": attachment.read_bytes(),
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              const input = document.querySelector('#owned-file-input');
              window.focusEvents = 0;
              window.ownedFile = input.files[0];
              window.clickedOwnedFile = null;
              button.addEventListener('focus', () => {
                window.focusEvents += 1;
                const transfer = new DataTransfer();
                transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
                input.files = transfer.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }, {once: true});
              button.onclick = () => {
                window.sendClicks += 1;
                window.clickedOwnedFile = input.files[0] === window.ownedFile;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              };
            }"""
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=identities,
        )
        ledger = RequestLedger(ledger_path)
        record = ledger.begin(
            role="DEV",
            prompt="exact prompt",
            files=identities,
            render_request_marker=False,
        )
        record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
        record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
        ledger.update(
            record.request_id,
            status=RequestStatus.UPLOAD_READY,
            upload_receipt=UploadReceipt(
                request_marker=record.rendered_prompt,
                method="input",
                files=identities,
                attachment_count=1,
                ownership_token=ownership_token,
            ).to_dict(),
        )
        result = await DurableSendBlock(
            "exact prompt",
            ledger_path=ledger_path,
            files=[str(attachment)],
            render_request_marker=False,
            wait_for_response=False,
            wait_for_stop=False,
            stable_ms=0,
        ).run(WorkflowContext(client))
        persisted = ledger.get(record.request_id)
        assert persisted is not None
        return {
            "status": persisted.status.value,
            "receipt": result["receipt"]["user_message_id"],
            "clicks": await page.evaluate("window.sendClicks"),
            "focus_events": await page.evaluate("window.focusEvents"),
            "clicked_owned_file": await page.evaluate("window.clickedOwnedFile"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "status": "sent",
        "receipt": "u1",
        "clicks": 1,
        "focus_events": 0,
        "clicked_owned_file": True,
    }


def test_real_browser_no_attachment_send_does_not_dispatch_focus(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              window.focusEvents = 0;
              button.addEventListener('focus', () => {
                window.focusEvents += 1;
                document.querySelector('#prompt-textarea').innerText = 'focus mutation';
              }, {once: true});
            }"""
        )
        method = await chatgpt.click_send_button(
            page,
            expected_prompt="exact prompt",
        )
        return {
            "method": method,
            "clicks": await page.evaluate("window.sendClicks"),
            "focus_events": await page.evaluate("window.focusEvents"),
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "method": "dom_click",
        "clicks": 1,
        "focus_events": 0,
    }


def test_real_browser_atomic_send_click_exception_does_not_fallback_submit(
    monkeypatch,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              window.submitEvents = 0;
              window.clickAttempts = 0;
              form.addEventListener('submit', event => {
                event.preventDefault();
                window.submitEvents += 1;
              });
              const button = form.querySelector('[data-testid="send-button"]');
              button.click = () => {
                window.clickAttempts += 1;
                throw new Error('synthetic click failure');
              };
            }"""
        )
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_prompt="exact prompt",
            )
        except Exception as exc:
            error = type(exc).__name__
        return {
            "error": error,
            "click_attempts": await page.evaluate("window.clickAttempts"),
            "submit_events": await page.evaluate("window.submitEvents"),
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == {
        "error": "UnsafePageStateError",
        "click_attempts": 1,
        "submit_events": 0,
        "clicks": 0,
    }


def test_real_browser_atomic_send_accepts_unchanged_attachment_ownership(monkeypatch):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#composer-form").evaluate(
            """form => {
              const input = document.createElement('input');
              input.id = 'owned-file-input';
              input.type = 'file';
              form.appendChild(input);
            }"""
        )
        await page.locator("#owned-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            }
        )
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        ownership_token = await upload_module.establish_attachment_ownership(
            page,
            expected_files=(_test_file_identity(b"owned-aaaa"),),
        )
        method = await chatgpt.click_send_button(
            page,
            expected_prompt="exact prompt",
            expected_attachment_count=1,
            expected_attachment_names=("context.txt",),
            expected_attachment_ownership_token=ownership_token,
        )
        return method, await page.evaluate("window.sendClicks")

    result = asyncio.run(_with_real_attachment_page(probe))

    assert result == ("dom_click", 1)


def test_real_browser_atomic_send_distinguishes_new_chat_from_saved_conversation(
    monkeypatch,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.evaluate("history.pushState({}, '', '/c/other')")
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        assert client.binding is not None
        error = None
        try:
            await chatgpt.click_send_button(
                page,
                expected_url="https://chatgpt.com/",
                expected_page_id=client.binding.page_id,
                expected_role=client.binding.role,
                expected_prompt="exact prompt",
            )
        except Exception as exc:
            error = type(exc).__name__
        return {
            "error": error,
            "path": await page.evaluate("location.pathname"),
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/",
        )
    )

    assert result == {
        "error": "PageOwnershipError",
        "path": "/c/other",
        "clicks": 0,
    }


@pytest.mark.parametrize("with_attachment", [False, True])
def test_real_browser_atomic_send_accepts_unchanged_owned_page(
    monkeypatch,
    with_attachment: bool,
):
    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        assert client.binding is not None
        expected_names = None
        expected_count = 0
        if with_attachment:
            await page.locator("#attachments").evaluate(
                "(root, html) => { root.innerHTML = html; }",
                _attachment_html("context.txt"),
            )
            expected_names = ("context.txt",)
            expected_count = 1
        method = await chatgpt.click_send_button(
            page,
            expected_url="https://chatgpt.com/c/original",
            expected_page_id=client.binding.page_id,
            expected_role=client.binding.role,
            expected_prompt="exact prompt",
            expected_attachment_count=expected_count,
            expected_attachment_names=expected_names,
        )
        return method, await page.evaluate("window.sendClicks")

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == ("dom_click", 1)


def test_real_browser_send_returns_no_receipt_when_conversation_changes_during_delay(
    monkeypatch,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.evaluate("history.pushState({}, '', '/c/other')")
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        await page.locator("#attachments").evaluate(
            "(root, html) => { root.innerHTML = html; }",
            _attachment_html("context.txt"),
        )
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              button.onclick = () => {
                window.sendClicks += 1;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              };
            }"""
        )
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("DEV")
        ownership_token = await _prepare_real_owned_attachment(
            page,
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"owned-aaaa",
            },
        )
        error = None
        receipt = None
        try:
            receipt = await client.send(
                "exact prompt",
                timeout_ms=500,
                max_attempts=1,
                recovery_reload=False,
                wait_for_stop=False,
                expected_attachment_ownership_token=ownership_token,
                expected_attachment_count=1,
                expected_attachment_names=("context.txt",),
            )
        except Exception as exc:
            error = type(exc).__name__
        snapshot_after = await chatgpt.inspect_chatgpt_page(page)
        return {
            "error": error,
            "receipt": receipt,
            "path": await page.evaluate("location.pathname"),
            "clicks": await page.evaluate("window.sendClicks"),
            "messages": tuple((item.role, item.text) for item in snapshot_after.messages),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "PageOwnershipError",
        "receipt": None,
        "path": "/c/other",
        "clicks": 0,
        "messages": (),
    }


def test_real_browser_send_prompt_rejects_conversation_change_during_delay(
    monkeypatch,
):
    async def no_record(*_args, **_kwargs):
        return None

    async def mutate_during_send(page, action, _multiplier):
        if action == "send":
            await page.evaluate("history.pushState({}, '', '/c/other')")
        return 0.0

    monkeypatch.setattr(chatgpt, "action_delay", mutate_during_send)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        error = None
        try:
            await chatgpt.send_prompt(
                page,
                "exact prompt",
                timeout_ms=500,
                wait_for_stop=False,
            )
        except Exception as exc:
            error = type(exc).__name__
        return {
            "error": error,
            "path": await page.evaluate("location.pathname"),
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "PageOwnershipError",
        "path": "/c/other",
        "clicks": 0,
    }


async def _install_snapshot_upload_input(page) -> None:
    await page.locator("#composer-form").evaluate(
        """form => {
          const existing = document.querySelector('#snapshot-file-input');
          if (existing) existing.remove();
          const input = document.createElement('input');
          input.id = 'snapshot-file-input';
          input.type = 'file';
          input.addEventListener('change', () => {
            const name = input.files?.[0]?.name || '';
            const root = document.querySelector('#attachments');
            root.innerHTML = name ? `
              <div data-testid="file-attachment" style="display:block;width:300px;height:40px">
                <span>${name}</span>
                <button type="button" aria-label="Remove file ${name}">Remove</button>
              </div>` : '';
          });
          form.appendChild(input);
        }"""
    )


def test_real_browser_input_upload_uses_same_byte_snapshot(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"original-bytes")
    expected = collect_file_identities([attachment])
    original_upload = upload_module._upload_via_input

    async def mutate_after_snapshot(page, snapshots):
        attachment.write_bytes(b"changed-after-snapshot")
        return await original_upload(page, snapshots)

    monkeypatch.setattr(upload_module, "_upload_via_input", mutate_after_snapshot)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await _install_snapshot_upload_input(page)
        receipt = await client.upload_files(
            [str(attachment)],
            request_marker="exact prompt",
            timeout_ms=500,
            expected_files=expected,
        )
        browser_text = await page.locator("#snapshot-file-input").evaluate(
            "input => input.files[0].text()"
        )
        return {
            "method": receipt.method,
            "receipt_sha": receipt.files[0].sha256,
            "browser_text": browser_text,
            "disk_text": attachment.read_text(encoding="utf-8"),
            "token": bool(receipt.ownership_token),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "method": "input",
        "receipt_sha": expected[0].sha256,
        "browser_text": "original-bytes",
        "disk_text": "changed-after-snapshot",
        "token": True,
    }


def test_real_browser_upload_rejects_same_name_same_size_replacement_before_ownership(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    expected = collect_file_identities([attachment])
    original_wait = upload_module.wait_upload_ready

    async def replace_after_ready(client, **kwargs):
        ready = await original_wait(client, **kwargs)
        await client.page.locator("#snapshot-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"manual-bbb",
            }
        )
        return ready

    monkeypatch.setattr(upload_module, "wait_upload_ready", replace_after_ready)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await _install_snapshot_upload_input(page)
        with pytest.raises(UploadReadinessError, match="browser file identity"):
            await client.upload_files(
                [str(attachment)],
                request_marker="exact prompt",
                timeout_ms=500,
                expected_files=expected,
            )
        ownership = await page.evaluate(
            "key => window[key] || null",
            chatgpt.ATTACHMENT_OWNERSHIP_WINDOW_KEY,
        )
        return ownership and ownership.get("phase")

    phase = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert phase != "owned"


def test_real_browser_durable_send_rejects_preownership_same_name_replacement(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    expected = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"
    original_wait = upload_module.wait_upload_ready

    async def replace_after_ready(client, **kwargs):
        ready = await original_wait(client, **kwargs)
        await client.page.locator("#snapshot-file-input").set_input_files(
            {
                "name": "context.txt",
                "mimeType": "text/plain",
                "buffer": b"manual-bbb",
            }
        )
        return ready

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(upload_module, "wait_upload_ready", replace_after_ready)
    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await _install_snapshot_upload_input(page)
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              button.onclick = null;
              button.addEventListener('click', () => {
                window.sendClicks += 1;
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
              }, {once: true});
            }"""
        )
        error = None
        try:
            await DurableSendBlock(
                "exact prompt",
                ledger_path=ledger_path,
                files=[str(attachment)],
                source_context={"task_id": "task-a", "team": "alpha"},
                render_request_marker=False,
                wait_for_response=False,
                wait_for_stop=False,
                max_attempts=1,
                recovery_reload=False,
                stable_ms=0,
            ).run(WorkflowContext(client))
        except Exception as exc:
            error = type(exc).__name__
        record = next(iter(json.loads(ledger_path.read_text())["records"].values()))
        return {
            "error": error,
            "status": record["status"],
            "attempts": record["attempts"],
            "receipt": record["receipt"],
            "upload_receipt": record["upload_receipt"],
            "ledger_sha": record["files"][0]["sha256"],
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "error": "UploadReadinessError",
        "status": "uploading",
        "attempts": 0,
        "receipt": None,
        "upload_receipt": None,
        "ledger_sha": expected[0].sha256,
        "clicks": 0,
    }


def test_real_browser_drop_upload_rejects_later_unowned_same_name_drop(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"owned-aaaa")
    expected = collect_file_identities([attachment])
    original_wait = upload_module.wait_upload_ready

    async def force_drop(_page, _snapshots):
        return False

    async def replace_after_ready(client, **kwargs):
        ready = await original_wait(client, **kwargs)
        await client.page.locator("#composer-form").evaluate(
            """form => {
              const transfer = new DataTransfer();
              transfer.items.add(new File(['manual-bbb'], 'context.txt', {type: 'text/plain'}));
              form.dispatchEvent(new DragEvent('drop', {
                bubbles: true,
                cancelable: true,
                dataTransfer: transfer,
              }));
            }"""
        )
        return ready

    monkeypatch.setattr(upload_module, "_upload_via_input", force_drop)
    monkeypatch.setattr(upload_module, "wait_upload_ready", replace_after_ready)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await page.locator("#composer-form").evaluate(
            """form => {
              form.addEventListener('dragover', event => event.preventDefault());
              form.addEventListener('drop', event => {
                event.preventDefault();
                const file = event.dataTransfer.files[0];
                window.droppedTextPromise = file.text();
                document.querySelector('#attachments').innerHTML = `
                  <div data-testid="file-attachment" style="display:block;width:300px;height:40px">
                    <span>${file.name}</span>
                    <button type="button" aria-label="Remove file ${file.name}">Remove</button>
                  </div>`;
              });
            }"""
        )
        with pytest.raises(UploadReadinessError, match="drop provenance"):
            await client.upload_files(
                [str(attachment)],
                request_marker="exact prompt",
                timeout_ms=500,
                expected_files=expected,
            )
        return await page.evaluate("window.droppedTextPromise")

    dropped_text = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert dropped_text == "manual-bbb"


def test_real_browser_drop_upload_uses_same_byte_snapshot(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"original-drop-bytes")
    expected = collect_file_identities([attachment])

    async def mutate_then_fallback(_page, _snapshots):
        attachment.write_bytes(b"changed-before-drop")
        return False

    monkeypatch.setattr(upload_module, "_upload_via_input", mutate_then_fallback)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await page.locator("#composer-form").evaluate(
            """form => {
              form.addEventListener('dragover', event => event.preventDefault());
              form.addEventListener('drop', event => {
                event.preventDefault();
                const file = event.dataTransfer.files[0];
                window.droppedTextPromise = file.text();
                const root = document.querySelector('#attachments');
                root.innerHTML = `
                  <div data-testid="file-attachment" style="display:block;width:300px;height:40px">
                    <span>${file.name}</span>
                    <button type="button" aria-label="Remove file ${file.name}">Remove</button>
                  </div>`;
              }, {once: true});
            }"""
        )
        receipt = await client.upload_files(
            [str(attachment)],
            request_marker="exact prompt",
            timeout_ms=500,
            expected_files=expected,
        )
        dropped_text = await page.evaluate("window.droppedTextPromise")
        return {
            "method": receipt.method,
            "receipt_sha": receipt.files[0].sha256,
            "browser_text": dropped_text,
            "disk_text": attachment.read_text(encoding="utf-8"),
            "token": bool(receipt.ownership_token),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "method": "drop",
        "receipt_sha": expected[0].sha256,
        "browser_text": "original-drop-bytes",
        "disk_text": "changed-before-drop",
        "token": True,
    }


def test_real_browser_durable_send_certifies_snapshot_bytes_not_later_path(
    tmp_path,
    monkeypatch,
):
    attachment = tmp_path / "context.txt"
    attachment.write_bytes(b"original-durable-bytes")
    expected = collect_file_identities([attachment])
    ledger_path = tmp_path / "ledger.json"
    original_upload = upload_module._upload_via_input

    async def mutate_after_snapshot(page, snapshots):
        attachment.write_bytes(b"changed-after-final-hash")
        return await original_upload(page, snapshots)

    monkeypatch.setattr(upload_module, "_upload_via_input", mutate_after_snapshot)

    async def no_delay(*_args, **_kwargs):
        return 0.0

    async def no_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chatgpt, "action_delay", no_delay)
    monkeypatch.setattr(chatgpt, "record_page_action", no_record)

    async def probe(page):
        client = ChatGPTPage(page, timeout_ms=500)
        await client.set_role("alpha-plan")
        await _bind_real_task_identity(page, client)
        await _install_snapshot_upload_input(page)
        await page.locator("[data-testid='send-button']").evaluate(
            """button => {
              button.onclick = null;
              button.addEventListener('click', () => {
                const input = document.querySelector('#snapshot-file-input');
                window.sendClicks += 1;
                window.clickedFileTextPromise = input.files[0].text();
                const turn = document.createElement('div');
                turn.setAttribute('data-turn-id', 't1');
                const message = document.createElement('div');
                message.setAttribute('data-message-author-role', 'user');
                message.setAttribute('data-message-id', 'u1');
                message.innerText = document.querySelector('#prompt-textarea').innerText;
                turn.appendChild(message);
                document.body.appendChild(turn);
                document.querySelector('#prompt-textarea').innerText = '';
                document.querySelector('#attachments').innerHTML = '';
                input.remove();
              }, {once: true});
            }"""
        )
        result = await DurableSendBlock(
            "exact prompt",
            ledger_path=ledger_path,
            files=[str(attachment)],
            source_context={"task_id": "task-a", "team": "alpha"},
            render_request_marker=False,
            wait_for_response=False,
            wait_for_stop=False,
            max_attempts=1,
            recovery_reload=False,
            stable_ms=0,
        ).run(WorkflowContext(client))
        clicked_text = await page.evaluate("window.clickedFileTextPromise")
        record = result["record"]
        ledger_text = ledger_path.read_text(encoding="utf-8")
        return {
            "status": record["status"],
            "receipt_user": result["receipt"]["user_message_id"],
            "ledger_sha": record["files"][0]["sha256"],
            "clicked_text": clicked_text,
            "disk_text": attachment.read_text(encoding="utf-8"),
            "raw_snapshot_persisted": "original-durable-bytes" in ledger_text,
            "clicks": await page.evaluate("window.sendClicks"),
        }

    result = asyncio.run(
        _with_real_attachment_page(
            probe,
            url="https://chatgpt.com/c/original",
        )
    )

    assert result == {
        "status": "sent",
        "receipt_user": "u1",
        "ledger_sha": expected[0].sha256,
        "clicked_text": "original-durable-bytes",
        "disk_text": "changed-after-final-hash",
        "raw_snapshot_persisted": False,
        "clicks": 1,
    }
