import asyncio
import json
from pathlib import Path

import pytest

from playwright_auto.chatgpt import (
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    PageOwnershipError,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    UnsafePageStateError,
    capture_message_baseline,
    prompt_digest,
)
from playwright_auto.durable import (
    DurableRecoveryState,
    DurableRequestError,
    RequestLedger,
    RequestStatus,
    build_idempotency_key,
    classify_recovery_state,
)
from playwright_auto.durable_blocks import DurableSendBlock
from playwright_auto.upload import UploadError, UploadReceipt, collect_file_identities
from playwright_auto.workflow import Workflow, WorkflowContext


def snapshot(
    *,
    text="",
    attachments=(),
    messages=(),
    state=ChatGPTState.NEW_CHAT,
    task_id=None,
    team=None,
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/session-1",
        session_id="session-1",
        page_id="page-1",
        page_role="DEV",
        page_task_id=task_id,
        page_team=team,
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=text,
        send_visible=bool(text),
        send_enabled=bool(text),
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=tuple(attachments),
        error_texts=(),
        messages=tuple(messages),
    )


class FakeDurableClient:
    def __init__(self, current=None):
        self.binding = PageBinding("page-1", "DEV")
        self.current = current or snapshot()
        self.set_calls = []
        self.upload_calls = []
        self.send_calls = []
        self.wait_calls = []
        self.attachment_ownership_token = "fake-upload-token"

    async def assert_ownership(self):
        return self.current

    async def set_text(self, text):
        self.set_calls.append(text)
        self.current = snapshot(
            text=text,
            attachments=self.current.attachment_markers,
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )

    async def upload_files(self, paths, *, request_marker, **_options):
        self.upload_calls.append((tuple(paths), request_marker))
        identities = collect_file_identities(paths)
        self.current = snapshot(
            text=self.current.composer_text,
            attachments=tuple(item.name for item in identities),
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )
        return UploadReceipt(
            request_marker=request_marker,
            method="input",
            files=identities,
            attachment_count=len(identities),
            ownership_token=self.attachment_ownership_token,
        )

    async def current_attachment_ownership_token(self, *, expected_names):
        if tuple(expected_names) != tuple(self.current.attachment_markers):
            return None
        return self.attachment_ownership_token

    async def send(
        self,
        text,
        *,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_task_id=None,
        expected_team=None,
        expected_attachment_ownership_token=None,
        expected_attachment_count=0,
        expected_attachment_names=None,
    ):
        self.send_calls.append(
            (
                text,
                wait_for_stop,
                max_attempts,
                recovery_reload,
                expected_attachment_ownership_token,
                expected_attachment_count,
            )
        )
        baseline = capture_message_baseline(self.current.messages)
        user = MessageSnapshot("user", "u1", "t1", text, ())
        self.current = snapshot(
            text="",
            messages=(*self.current.messages, user),
            state=ChatGPTState.SUBMITTING,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=baseline,
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="session-1",
            user_message_id="u1",
            user_turn_id="t1",
        )

    async def wait_for_response(self, receipt, **options):
        self.wait_calls.append((receipt, options))
        response = MessageSnapshot(
            "assistant", "a1", "t1", "durable answer", ()
        )
        self.current = snapshot(
            messages=(*self.current.messages, response),
            state=ChatGPTState.WAITING_PROMPT,
            task_id=self.current.page_task_id,
            team=self.current.page_team,
        )
        return response


def run_block(block, client, variables=None):
    return asyncio.run(
        Workflow("durable", [block]).run(client, variables or {})
    )


def test_ledger_begin_is_stable_for_same_normalized_request(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")

    first = ledger.begin(role="DEV", prompt="hello  \n")
    second = ledger.begin(role="DEV", prompt="hello")

    assert first.request_id == second.request_id
    assert first.marker in first.rendered_prompt
    assert ledger.get(first.request_id) == first


def test_ledger_can_keep_request_marker_internal(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")

    record = ledger.begin(
        role="DEV",
        prompt="hello",
        request_id="agent-request-internal",
        render_request_marker=False,
    )

    assert record.marker == "ROLE_REQUEST_ID: agent-request-internal"
    assert record.rendered_prompt == "hello"
    assert record.marker not in record.rendered_prompt
    assert ledger.begin(
        role="DEV",
        prompt="hello",
        request_id="agent-request-internal",
        render_request_marker=True,
    ) == record


def test_ledger_accepts_stable_explicit_request_id(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")

    first = ledger.begin(role="DEV", prompt="hello", request_id="agent-request-1")
    second = ledger.begin(role="DEV", prompt="hello", request_id="agent-request-1")

    assert first.request_id == "agent-request-1"
    assert second == first
    assert "ROLE_REQUEST_ID: agent-request-1" in first.rendered_prompt

    with pytest.raises(DurableRequestError, match="already belongs"):
        ledger.begin(role="DEV", prompt="different", request_id="agent-request-1")


def test_file_content_changes_idempotency_key(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("one", encoding="utf-8")
    first_file = collect_file_identities([path])
    first = build_idempotency_key(
        role="DEV", prompt="task", files=first_file
    )
    path.write_text("two", encoding="utf-8")
    second_file = collect_file_identities([path])
    second = build_idempotency_key(
        role="DEV", prompt="task", files=second_file
    )

    assert first != second


def test_ledger_rejects_invalid_state_transition(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="DEV", prompt="task")

    with pytest.raises(DurableRequestError, match="invalid durable transition"):
        ledger.update(record.request_id, status=RequestStatus.COMPLETED)


def test_markerless_recovery_uses_exact_new_user_prompt_and_never_resends(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(
        role="DEV",
        prompt="markerless task",
        render_request_marker=False,
    )
    old_user = MessageSnapshot("user", "old-u", "old-t", "older prompt", ())
    baseline = capture_message_baseline((old_user,))
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
    )

    accepted = snapshot(
        messages=(
            old_user,
            MessageSnapshot("user", "new-u", "new-t", "markerless task", ()),
        ),
        state=ChatGPTState.SUBMITTING,
    )
    assert classify_recovery_state(record, accepted) is DurableRecoveryState.SENT_WAITING_RESPONSE
    assert classify_recovery_state(record, snapshot(messages=(old_user,))) is DurableRecoveryState.SENT_MARKER_MISSING


def test_markerless_collapsed_transcript_upgrades_receipt_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = RequestLedger(ledger_path)
    prompt = "x" * 5262
    record = ledger.begin(
        role="DEV",
        prompt=prompt,
        request_id="collapsed-hop",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
    )
    client = FakeDurableClient(
        snapshot(
            messages=(MessageSnapshot("user", "u-long", "t-long", "x" * 5080 + " Show more", ()),),
            state=ChatGPTState.SUBMITTING,
        )
    )

    result = run_block(
        DurableSendBlock(
            prompt,
            ledger_path=ledger_path,
            request_id="collapsed-hop",
            render_request_marker=False,
            wait_for_response=False,
        ),
        client,
    )

    assert client.send_calls == []
    receipt = result.context.results["durable_send"]["receipt"]
    assert receipt["user_message_id"] == "u-long"
    assert receipt["user_turn_id"] == "t-long"
    assert RequestLedger(ledger_path).get(record.request_id).receipt == receipt


def test_markerless_send_without_user_identity_stays_sending(tmp_path):
    class IdentitylessClient(FakeDurableClient):
        async def send(self, *args, **kwargs):
            receipt = await super().send(*args, **kwargs)
            return SendReceipt(
                prompt=receipt.prompt,
                prompt_sha256=receipt.prompt_sha256,
                binding=receipt.binding,
                baseline=receipt.baseline,
                attempts=receipt.attempts,
                accepted_via="stop_button",
                session_id_before=receipt.session_id_before,
            )

    ledger_path = tmp_path / "ledger.json"
    block = DurableSendBlock(
        "markerless prompt",
        ledger_path=ledger_path,
        request_id="identityless-markerless",
        render_request_marker=False,
        wait_for_response=False,
    )

    with pytest.raises(DurableRequestError, match="accepted user-message identity"):
        asyncio.run(block.run(WorkflowContext(IdentitylessClient())))

    record = RequestLedger(ledger_path).get("identityless-markerless")
    assert record is not None
    assert record.status is RequestStatus.SENDING
    assert record.receipt is None
    assert record.attempts == 1


def test_persisted_receipt_recovers_without_transcript_rendering(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(
        role="DEV",
        prompt="markerless accepted",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt="markerless accepted",
        prompt_sha256=prompt_digest("markerless accepted"),
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        attempts=1,
        accepted_via="stop_button",
        session_id_before="session-1",
    )
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
        session_id_before="session-1",
    )
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        receipt=receipt.to_dict(),
    )

    assert classify_recovery_state(record, snapshot()) is DurableRecoveryState.SENT_WAITING_RESPONSE


def test_recovery_classifier_never_resends_after_send_boundary(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="DEV", prompt="task")
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(
            frozenset(), frozenset(), frozenset(), frozenset()
        ),
    )

    assert (
        classify_recovery_state(record, snapshot(text=record.rendered_prompt))
        is DurableRecoveryState.SENT_MARKER_MISSING
    )
    transcript = snapshot(
        messages=(
            MessageSnapshot("user", "u1", "t1", record.rendered_prompt, ()),
        ),
        state=ChatGPTState.SUBMITTING,
    )
    assert (
        classify_recovery_state(record, transcript)
        is DurableRecoveryState.SENT_WAITING_RESPONSE
    )


def test_markerless_durable_send_recovers_exact_transcript_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="markerless resume",
        request_id="markerless-hop-1",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
    )
    client = FakeDurableClient(
        snapshot(
            messages=(
                MessageSnapshot("user", "u1", "t1", "markerless resume", ()),
            ),
            state=ChatGPTState.SUBMITTING,
        )
    )

    result = run_block(
        DurableSendBlock(
            "markerless resume",
            ledger_path=ledger_path,
            request_id="markerless-hop-1",
            render_request_marker=False,
            wait_for_response=False,
        ),
        client,
    )

    assert client.send_calls == []
    assert result.context.results["durable_send"]["receipt"]["prompt"] == "markerless resume"
    assert RequestLedger(ledger_path).get(record.request_id).status is RequestStatus.SENT


def test_markerless_durable_send_uses_persisted_receipt_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="markerless persisted",
        request_id="markerless-hop-2",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt="markerless persisted",
        prompt_sha256=prompt_digest("markerless persisted"),
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        attempts=1,
        accepted_via="stop_button",
        session_id_before="session-1",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
        session_id_before="session-1",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        receipt=receipt.to_dict(),
    )
    client = FakeDurableClient(snapshot())

    result = run_block(
        DurableSendBlock(
            "markerless persisted",
            ledger_path=ledger_path,
            request_id="markerless-hop-2",
            render_request_marker=False,
            wait_for_response=False,
        ),
        client,
    )

    assert client.send_calls == []
    assert result.context.results["durable_send"]["receipt"] == receipt.to_dict()


def test_durable_send_uses_explicit_request_id(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    client = FakeDurableClient()

    result = run_block(
        DurableSendBlock(
            "perform durable task",
            ledger_path=ledger_path,
            request_id="agent-hop-1",
            stable_ms=0,
        ),
        client,
    )

    assert result.context.variables["durable_request"].request_id == "agent-hop-1"
    assert "ROLE_REQUEST_ID: agent-hop-1" in client.send_calls[0][0]


def test_durable_send_persists_first_acceptance_timestamp(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    client = FakeDurableClient()

    result = run_block(
        DurableSendBlock(
            "accepted task",
            ledger_path=ledger_path,
            wait_for_response=False,
        ),
        client,
    )

    record = result.context.results["durable_send"]["record"]
    assert record["status"] == "sent"
    assert record["created_at"] <= record["accepted_at"] <= record["updated_at"]
    assert RequestLedger(ledger_path).get(record["request_id"]).accepted_at == (
        record["accepted_at"]
    )


def test_durable_send_completes_once_then_returns_cached_response(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    client = FakeDurableClient()
    block = DurableSendBlock(
        "perform durable task",
        ledger_path=ledger_path,
        stable_ms=0,
    )

    first = run_block(block, client)
    second = run_block(block, client)

    assert first.context.results["durable_send"]["cached"] is False
    assert second.context.results["durable_send"]["cached"] is True
    assert len(client.send_calls) == 1
    assert len(client.wait_calls) == 1
    stored = RequestLedger(ledger_path).get(
        first.context.variables["durable_request"].request_id
    )
    assert stored.status is RequestStatus.COMPLETED
    assert stored.response["text"] == "durable answer"


def test_durable_validator_keeps_partial_response_sent_then_accepts_same_turn(
    tmp_path,
):
    ledger_path = tmp_path / "ledger.json"
    partial = "report JSON {\"action\":\"WAIT\"} ``"
    final = "report JSON {\"action\":\"WAIT\"}"

    class FinalizingClient(FakeDurableClient):
        def __init__(self):
            super().__init__()
            self.response_text = partial

        async def wait_for_response(self, receipt, **options):
            self.wait_calls.append((receipt, options))
            candidate = MessageSnapshot(
                "assistant", "a1", "assistant-turn-1", self.response_text, ()
            )
            messages = tuple(
                item
                for item in self.current.messages
                if not (item.role == "assistant" and item.message_id == "a1")
            )
            self.current = snapshot(
                messages=(*messages, candidate),
                state=ChatGPTState.WAITING_PROMPT,
            )
            options["candidate_validator"](candidate)
            return candidate

    def validate(candidate):
        if candidate.text.endswith("``"):
            raise ValueError("partial maintenance response")

    client = FinalizingClient()
    block = DurableSendBlock(
        "validated durable task",
        ledger_path=ledger_path,
        stable_ms=0,
        candidate_validator=validate,
        minimum_samples=2,
        invalid_grace_ms=1_000,
    )

    with pytest.raises(Exception) as captured:
        run_block(block, client)
    assert isinstance(captured.value.cause, ValueError)
    request_id = next(iter(json.loads(ledger_path.read_text())["records"]))
    stored = RequestLedger(ledger_path).get(request_id)
    assert stored.status is RequestStatus.SENT
    assert stored.attempts == 1

    client.response_text = final
    result = run_block(block, client)
    stored = RequestLedger(ledger_path).get(stored.request_id)

    assert result.context.results["durable_send"]["response"]["text"] == final
    assert stored.status is RequestStatus.COMPLETED
    assert stored.response["message_id"] == "a1"
    assert stored.response["turn_id"] == "assistant-turn-1"
    assert stored.attempts == 1
    assert len(client.send_calls) == 1
    assert len(client.wait_calls) == 2
    assert client.wait_calls[-1][1]["minimum_samples"] == 2
    assert client.wait_calls[-1][1]["invalid_grace_ms"] == 1_000


def test_completed_invalid_cache_is_reread_and_upgraded_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    partial = "report JSON {\"action\":\"WAIT\"} ``"
    final = "report JSON {\"action\":\"WAIT\"}"

    class MutableResponseClient(FakeDurableClient):
        def __init__(self):
            super().__init__()
            self.response_text = partial

        async def wait_for_response(self, receipt, **options):
            self.wait_calls.append((receipt, options))
            candidate = MessageSnapshot(
                "assistant", "a1", "assistant-turn-1", self.response_text, ()
            )
            messages = tuple(
                item
                for item in self.current.messages
                if not (item.role == "assistant" and item.message_id == "a1")
            )
            self.current = snapshot(
                messages=(*messages, candidate),
                state=ChatGPTState.WAITING_PROMPT,
            )
            validator = options.get("candidate_validator")
            if validator is not None:
                validator(candidate)
            return candidate

    def validate(candidate):
        if candidate.text.endswith("``"):
            raise ValueError("partial maintenance response")

    client = MutableResponseClient()
    initial = run_block(
        DurableSendBlock(
            "cached durable task",
            ledger_path=ledger_path,
            stable_ms=0,
        ),
        client,
    )
    request_id = initial.context.variables["durable_request"].request_id
    cached = RequestLedger(ledger_path).get(request_id)
    assert cached.status is RequestStatus.COMPLETED
    assert cached.response["text"] == partial

    client.response_text = final
    upgraded = run_block(
        DurableSendBlock(
            "cached durable task",
            ledger_path=ledger_path,
            stable_ms=0,
            candidate_validator=validate,
            minimum_samples=2,
            invalid_grace_ms=1_000,
        ),
        client,
    )
    stored = RequestLedger(ledger_path).get(request_id)

    assert upgraded.context.results["durable_send"]["cached"] is True
    assert upgraded.context.results["durable_send"]["response"]["text"] == final
    assert stored.response["text"] == final
    assert stored.response["message_id"] == cached.response["message_id"]
    assert stored.response["turn_id"] == cached.response["turn_id"]
    assert stored.attempts == 1
    assert len(client.send_calls) == 1
    assert len(client.wait_calls) == 2


def test_completed_invalid_cache_rejects_different_assistant_identity(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    partial = "report JSON {\"action\":\"WAIT\"} ``"
    final = "report JSON {\"action\":\"WAIT\"}"

    class IdentityAwareClient(FakeDurableClient):
        def __init__(self):
            super().__init__()
            self.response = MessageSnapshot(
                "assistant", "old-message", "old-turn", partial, ()
            )

        async def wait_for_response(self, receipt, **options):
            self.wait_calls.append((receipt, options))
            candidate = self.response
            expected_turn = options.get("expected_assistant_turn_id")
            expected_message = options.get("expected_assistant_message_id")
            if expected_turn:
                if candidate.turn_id != expected_turn:
                    raise TimeoutError("expected assistant turn not found")
            elif expected_message and candidate.message_id != expected_message:
                raise TimeoutError("expected assistant message not found")
            validator = options.get("candidate_validator")
            if validator is not None:
                validator(candidate)
            return candidate

    def validate(candidate):
        if candidate.text.endswith("``"):
            raise ValueError("partial maintenance response")

    client = IdentityAwareClient()
    initial = run_block(
        DurableSendBlock(
            "identity-bound durable task",
            ledger_path=ledger_path,
            stable_ms=0,
        ),
        client,
    )
    request_id = initial.context.variables["durable_request"].request_id
    cached = RequestLedger(ledger_path).get(request_id)
    assert cached.response["message_id"] == "old-message"
    assert cached.response["turn_id"] == "old-turn"

    client.response = MessageSnapshot(
        "assistant", "different-message", "different-turn", final, ()
    )
    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "identity-bound durable task",
                ledger_path=ledger_path,
                stable_ms=0,
                candidate_validator=validate,
                minimum_samples=2,
                invalid_grace_ms=1_000,
            ),
            client,
        )

    assert isinstance(captured.value.cause, TimeoutError)
    stored = RequestLedger(ledger_path).get(request_id)
    assert stored.status is RequestStatus.COMPLETED
    assert stored.response == cached.response
    assert stored.attempts == 1
    assert len(client.send_calls) == 1
    assert len(client.wait_calls) == 2
    assert client.wait_calls[-1][1]["expected_assistant_turn_id"] == "old-turn"
    assert client.wait_calls[-1][1]["expected_assistant_message_id"] == "old-message"


def test_completed_invalid_cache_without_identity_fails_closed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    client = FakeDurableClient()
    initial = run_block(
        DurableSendBlock(
            "identityless cached task",
            ledger_path=ledger_path,
            stable_ms=0,
        ),
        client,
    )
    request_id = initial.context.variables["durable_request"].request_id
    ledger = RequestLedger(ledger_path)
    cached = ledger.get(request_id)
    identityless = dict(cached.response)
    identityless["message_id"] = ""
    identityless["turn_id"] = None
    identityless["text"] = "partial response ``"
    ledger.update(request_id, response=identityless)

    def validate(candidate):
        if candidate.text.endswith("``"):
            raise ValueError("partial response")

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "identityless cached task",
                ledger_path=ledger_path,
                stable_ms=0,
                candidate_validator=validate,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "no assistant identity" in str(captured.value.cause)
    stored = RequestLedger(ledger_path).get(request_id)
    assert stored.response == identityless
    assert stored.attempts == 1
    assert len(client.send_calls) == 1
    assert len(client.wait_calls) == 1


def test_crash_resume_with_transcript_marker_waits_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="resume task")
    baseline = MessageBaseline(
        frozenset(), frozenset(), frozenset(), frozenset()
    )
    record = ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
    )
    client = FakeDurableClient(
        snapshot(
            messages=(
                MessageSnapshot(
                    "user", "u1", "t1", record.rendered_prompt, ()
                ),
            ),
            state=ChatGPTState.SUBMITTING,
        )
    )

    result = run_block(
        DurableSendBlock(
            "resume task",
            ledger_path=ledger_path,
            stable_ms=0,
        ),
        client,
    )

    assert client.send_calls == []
    assert len(client.wait_calls) == 1
    assert result.context.results["durable_send"]["response"]["text"] == (
        "durable answer"
    )
    stored = RequestLedger(ledger_path).get(record.request_id)
    assert stored.status is RequestStatus.COMPLETED
    assert stored.accepted_at is not None


def test_sending_without_marker_fails_closed_and_never_resends(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="ambiguous task")
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(
            frozenset(), frozenset(), frozenset(), frozenset()
        ),
    )
    client = FakeDurableClient(snapshot())

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "ambiguous task",
                ledger_path=ledger_path,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "refusing to send again" in str(captured.value.cause)
    assert client.send_calls == []


def test_durable_upload_is_hashed_uploaded_and_sent_once(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    client = FakeDurableClient()

    result = run_block(
        DurableSendBlock(
            "task with context",
            ledger_path=ledger_path,
            files=[str(attachment)],
            stable_ms=0,
        ),
        client,
    )

    record = result.context.variables["durable_request"]
    assert len(client.upload_calls) == 1
    assert len(client.send_calls) == 1
    assert client.send_calls[0][-1] == 1
    assert record.status is RequestStatus.COMPLETED
    assert record.files[0].name == "context.txt"
    assert record.upload_receipt["attachment_count"] == 1


def test_uploading_crash_resumes_only_when_ui_proves_all_attachments_ready(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload resume", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    result = run_block(
        DurableSendBlock(
            "upload resume",
            ledger_path=ledger_path,
            files=[str(attachment)],
            stable_ms=0,
        ),
        client,
    )

    assert client.upload_calls == []
    assert len(client.send_calls) == 1
    assert result.context.variables["durable_request"].status is (
        RequestStatus.COMPLETED
    )


def test_uploading_crash_without_ready_attachments_fails_closed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ambiguous", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    client = FakeDurableClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload ambiguous",
                ledger_path=ledger_path,
                files=[str(attachment)],
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert client.upload_calls == []
    assert client.send_calls == []


def test_per_request_lock_rejects_concurrent_owner(tmp_path):
    from playwright_auto.durable import DurableRequestBusyError

    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="DEV", prompt="exclusive")

    with ledger.request_lock(record.request_id):
        with pytest.raises(DurableRequestBusyError, match="already running"):
            with RequestLedger(tmp_path / "ledger.json").request_lock(
                record.request_id
            ):
                pass


def test_source_context_is_canonical_json_in_ledger(tmp_path):
    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(
        role="DEV",
        prompt="canonical",
        source_context={"path": Path("/tmp/example"), "values": (1, 2)},
    )

    assert record.source_context == {
        "path": "/tmp/example",
        "values": [1, 2],
    }



def test_durable_composer_whitespace_collapse_keeps_exact_semantic_prompt(tmp_path):
    from playwright_auto.chatgpt import visible_text_matches
    from playwright_auto.durable import RequestLedger

    ledger = RequestLedger(tmp_path / "ledger.json")
    record = ledger.begin(role="DEV", prompt="body")
    collapsed = record.rendered_prompt.replace("\n\n", " ")

    assert visible_text_matches(collapsed, record.rendered_prompt) is True
    assert visible_text_matches(
        collapsed.replace("body", "changed"),
        record.rendered_prompt,
    ) is False


def test_markerless_durable_upload_uses_exact_prompt_as_composer_anchor(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    client = FakeDurableClient()

    result = run_block(
        DurableSendBlock(
            "markerless task with context",
            ledger_path=ledger_path,
            files=[str(attachment)],
            render_request_marker=False,
            stable_ms=0,
        ),
        client,
    )

    assert result.context.variables["durable_request"].status is RequestStatus.COMPLETED
    assert client.upload_calls == [
        ((str(attachment.resolve()),), "markerless task with context")
    ]
    assert "ROLE_REQUEST_ID" not in client.send_calls[0][0]


def test_authoritative_upload_identity_mismatch_fails_before_ledger_or_browser(
    tmp_path,
):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    expected = collect_file_identities([attachment])
    attachment.write_text("changed after worker preflight", encoding="utf-8")
    client = FakeDurableClient()

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "markerless task with authoritative context",
                ledger_path=ledger_path,
                files=[str(attachment)],
                expected_file_identities=expected,
                render_request_marker=False,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, UploadError)
    assert "source identity changed" in str(captured.value.cause)
    assert not ledger_path.exists()
    assert client.set_calls == []
    assert client.upload_calls == []
    assert client.send_calls == []


@pytest.mark.parametrize("visible_name", ["manual-unowned.txt", "context.txt"])
def test_prompt_set_request_rejects_unowned_attachment_even_with_expected_count(
    tmp_path,
    visible_name,
):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="exact automated prompt",
        files=identities,
        render_request_marker=False,
    )
    ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=(visible_name,),
            state=ChatGPTState.DRAFT,
        )
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "exact automated prompt",
                ledger_path=ledger_path,
                files=[str(attachment)],
                render_request_marker=False,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert client.upload_calls == []
    assert client.send_calls == []
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.PROMPT_SET
    assert persisted.upload_receipt is None


def test_uploading_recovery_persists_recovered_upload_receipt(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload resume proof", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    run_block(
        DurableSendBlock(
            "upload resume proof",
            ledger_path=ledger_path,
            files=[str(attachment)],
            stable_ms=0,
        ),
        client,
    )

    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.upload_receipt is not None
    assert persisted.upload_receipt["method"] == "recovered"
    assert persisted.upload_receipt["files"] == [item.to_dict() for item in identities]
    assert persisted.upload_receipt["attachment_count"] == 1
    assert client.upload_calls == []
    assert len(client.send_calls) == 1


@pytest.mark.parametrize(
    "markers",
    [
        ("wrong.txt",),
        (),
        ("context.txt", "extra.txt"),
    ],
)
def test_uploading_recovery_rejects_wrong_partial_or_extra_markers(tmp_path, markers):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload recovery mismatch", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=markers,
            state=ChatGPTState.DRAFT,
        )
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload recovery mismatch",
                ledger_path=ledger_path,
                files=[str(attachment)],
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert client.upload_calls == []
    assert client.send_calls == []
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.UPLOADING
    assert persisted.upload_receipt is None


def test_upload_ready_requires_valid_persisted_receipt(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ready receipt", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    ledger.update(record.request_id, status=RequestStatus.UPLOAD_READY)
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload ready receipt",
                ledger_path=ledger_path,
                files=[str(attachment)],
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "upload receipt" in str(captured.value.cause)
    assert client.upload_calls == []
    assert client.send_calls == []


def test_upload_ready_with_valid_receipt_resumes_without_reupload(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ready valid", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    upload_receipt = UploadReceipt(
        request_marker=record.marker,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.UPLOAD_READY,
        upload_receipt=upload_receipt.to_dict(),
    )
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    run_block(
        DurableSendBlock(
            "upload ready valid",
            ledger_path=ledger_path,
            files=[str(attachment)],
            stable_ms=0,
        ),
        client,
    )

    assert client.upload_calls == []
    assert len(client.send_calls) == 1


def test_upload_ready_rejects_receipt_for_different_files(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    other = tmp_path / "other.txt"
    attachment.write_text("context", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    identities = collect_file_identities([attachment])
    wrong_identities = collect_file_identities([other])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ready forged", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    wrong_receipt = UploadReceipt(
        request_marker=record.marker,
        method="input",
        files=wrong_identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.UPLOAD_READY,
        upload_receipt=wrong_receipt.to_dict(),
    )
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload ready forged",
                ledger_path=ledger_path,
                files=[str(attachment)],
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "upload receipt" in str(captured.value.cause)
    assert client.upload_calls == []
    assert client.send_calls == []


def test_sending_upload_recovers_accepted_transcript_without_resend(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="accepted upload crash",
        files=identities,
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
        upload_receipt=upload_receipt.to_dict(),
    )
    client = FakeDurableClient(
        snapshot(
            messages=(
                MessageSnapshot(
                    "user", "accepted-user", "accepted-turn", record.rendered_prompt, ()
                ),
            ),
            state=ChatGPTState.SUBMITTING,
        )
    )

    result = run_block(
        DurableSendBlock(
            "accepted upload crash",
            ledger_path=ledger_path,
            files=[str(attachment)],
            render_request_marker=False,
            stable_ms=0,
        ),
        client,
    )

    assert client.upload_calls == []
    assert client.send_calls == []
    assert len(client.wait_calls) == 1
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.COMPLETED
    assert persisted.upload_receipt == upload_receipt.to_dict()
    assert result.context.results["durable_send"]["response"]["text"] == "durable answer"


def test_sending_upload_recovery_ignores_source_change_after_acceptance(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="accepted upload source drift",
        files=identities,
        request_id="accepted-upload-source-drift",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
        upload_receipt=upload_receipt.to_dict(),
    )
    attachment.write_text("changed after accepted send", encoding="utf-8")
    client = FakeDurableClient(
        snapshot(
            messages=(
                MessageSnapshot(
                    "user", "accepted-user", "accepted-turn", record.rendered_prompt, ()
                ),
            ),
            state=ChatGPTState.SUBMITTING,
        )
    )

    result = run_block(
        DurableSendBlock(
            "accepted upload source drift",
            ledger_path=ledger_path,
            files=[str(attachment)],
            expected_file_identities=identities,
            request_id=record.request_id,
            render_request_marker=False,
            wait_for_response=False,
            stable_ms=0,
        ),
        client,
    )

    output = result.context.results["durable_send"]
    assert output["receipt"]["user_message_id"] == "accepted-user"
    assert ledger.get(record.request_id).status is RequestStatus.SENT
    assert client.upload_calls == []
    assert client.send_calls == []


def test_sending_upload_source_change_stays_ambiguous_without_acceptance(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("original", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="ambiguous upload source drift",
        files=identities,
        request_id="ambiguous-upload-source-drift",
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-1", "DEV"),
        baseline=baseline,
        session_id_before="session-1",
        upload_receipt=upload_receipt.to_dict(),
    )
    attachment.write_text("changed after ambiguous send", encoding="utf-8")
    client = FakeDurableClient(snapshot(state=ChatGPTState.SUBMITTING))

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "ambiguous upload source drift",
                ledger_path=ledger_path,
                files=[str(attachment)],
                expected_file_identities=identities,
                request_id=record.request_id,
                render_request_marker=False,
                wait_for_response=False,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "ambiguous state" in str(captured.value.cause)
    assert ledger.get(record.request_id).status is RequestStatus.SENDING
    assert client.upload_calls == []
    assert client.send_calls == []


def test_durable_cdpa_source_context_requires_exact_browser_task_team(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    client = FakeDurableClient(snapshot())

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "task-owned prompt",
                ledger_path=ledger_path,
                source_context={"task_id": "task-a", "team": "alpha"},
                render_request_marker=False,
                wait_for_response=False,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert "task/team ownership" in str(captured.value.cause)
    assert client.send_calls == []
    records = json.loads(ledger_path.read_text(encoding="utf-8"))["records"]
    persisted = next(iter(records.values()))
    assert persisted["status"] == RequestStatus.PROMPT_SET.value
    assert persisted["receipt"] is None


@pytest.mark.parametrize(
    ("race_kind", "expected_error_type"),
    [
        ("attachment", ComposerConflictError),
        ("prompt", ComposerConflictError),
        ("ownership", PageOwnershipError),
        ("page_state", UnsafePageStateError),
    ],
)
def test_durable_send_rejects_locked_send_boundary_conflict(
    tmp_path,
    race_kind,
    expected_error_type,
):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(
        role="DEV",
        prompt="locked boundary race",
        files=identities,
        render_request_marker=False,
    )
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)
    upload_receipt = UploadReceipt(
        request_marker=record.rendered_prompt,
        method="input",
        files=identities,
        attachment_count=1,
        ownership_token="fake-upload-token",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.UPLOAD_READY,
        upload_receipt=upload_receipt.to_dict(),
    )

    class BoundaryRaceClient(FakeDurableClient):
        async def send(
            self,
            text,
            *,
            wait_for_stop=True,
            max_attempts=2,
            recovery_reload=True,
            expected_task_id=None,
            expected_team=None,
            expected_attachment_ownership_token=None,
            expected_attachment_count=0,
            expected_attachment_names=None,
        ):
            if race_kind == "prompt":
                self.current = snapshot(
                    text="manual changed prompt",
                    attachments=self.current.attachment_markers,
                    messages=self.current.messages,
                    state=ChatGPTState.DRAFT,
                )
                raise ComposerConflictError("composer text changed in locked send")
            if race_kind == "ownership":
                raise PageOwnershipError("conversation changed in locked send")
            if race_kind == "page_state":
                raise UnsafePageStateError("blocking dialog appeared in locked send")
            self.current = snapshot(
                text=self.current.composer_text,
                attachments=("manual-unowned.txt",),
                messages=self.current.messages,
                state=ChatGPTState.DRAFT,
            )
            if expected_attachment_names is not None and tuple(
                self.current.attachment_markers
            ) != tuple(expected_attachment_names):
                raise ComposerConflictError("attachment identity changed in locked send")
            return await super().send(
                text,
                wait_for_stop=wait_for_stop,
                max_attempts=max_attempts,
                recovery_reload=recovery_reload,
                expected_task_id=expected_task_id,
                expected_team=expected_team,
                expected_attachment_ownership_token=expected_attachment_ownership_token,
                expected_attachment_count=expected_attachment_count,
                expected_attachment_names=expected_attachment_names,
            )

    client = BoundaryRaceClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("context.txt",),
            state=ChatGPTState.DRAFT,
        )
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "locked boundary race",
                ledger_path=ledger_path,
                files=[str(attachment)],
                render_request_marker=False,
                wait_for_response=False,
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, expected_error_type)
    assert client.send_calls == []
    persisted = ledger.get(record.request_id)
    assert persisted is not None
    assert persisted.status is RequestStatus.SENDING
    assert persisted.receipt is None
    assert persisted.upload_receipt == upload_receipt.to_dict()
