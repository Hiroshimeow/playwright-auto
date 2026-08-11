import asyncio
from pathlib import Path

import pytest

from playwright_auto.chatgpt import (
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
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
from playwright_auto.upload import UploadReceipt, collect_file_identities
from playwright_auto.workflow import Workflow


def snapshot(
    *,
    text="",
    attachments=(),
    messages=(),
    state=ChatGPTState.NEW_CHAT,
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/session-1",
        session_id="session-1",
        page_id="page-1",
        page_role="DEV",
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

    async def assert_ownership(self):
        return self.current

    async def set_text(self, text):
        self.set_calls.append(text)
        self.current = snapshot(
            text=text,
            attachments=self.current.attachment_markers,
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
        )

    async def upload_files(self, paths, *, request_marker, **_options):
        self.upload_calls.append((tuple(paths), request_marker))
        identities = collect_file_identities(paths)
        self.current = snapshot(
            text=self.current.composer_text,
            attachments=tuple(item.name for item in identities),
            messages=self.current.messages,
            state=ChatGPTState.DRAFT,
        )
        return UploadReceipt(
            request_marker=request_marker,
            method="input",
            files=identities,
            attachment_count=len(identities),
        )

    async def send(
        self,
        text,
        *,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_attachment_count=0,
    ):
        self.send_calls.append(
            (
                text,
                wait_for_stop,
                max_attempts,
                recovery_reload,
                expected_attachment_count,
            )
        )
        baseline = capture_message_baseline(self.current.messages)
        user = MessageSnapshot("user", "u1", "t1", text, ())
        self.current = snapshot(
            text="",
            messages=(*self.current.messages, user),
            state=ChatGPTState.SUBMITTING,
        )
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=baseline,
            attempts=1,
            accepted_via="exact_user_message",
            session_id_before="session-1",
        )

    async def wait_for_response(self, receipt, **options):
        self.wait_calls.append((receipt, options))
        response = MessageSnapshot(
            "assistant", "a1", "t1", "durable answer", ()
        )
        self.current = snapshot(
            messages=(*self.current.messages, response),
            state=ChatGPTState.WAITING_PROMPT,
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
    assert RequestLedger(ledger_path).get(record.request_id).status is (
        RequestStatus.COMPLETED
    )


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






def test_authorized_failed_upload_before_ready_reuses_same_request_and_sends_once(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload recoverable", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(
        record.request_id,
        status=RequestStatus.UPLOADING,
        error="upload_retry_authorized:1",
    )
    original_request_id = record.request_id
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None
    assert record.upload_receipt is None

    class RecoveryClient(FakeDurableClient):
        async def upload_files(self, paths, *, request_marker, **options):
            receipt = await super().upload_files(
                paths, request_marker=request_marker, **options
            )
            return UploadReceipt(
                request_marker=receipt.request_marker,
                method=receipt.method,
                files=receipt.files,
                attachment_count=receipt.attachment_count,
                ownership_token="owned-retry",
            )

        async def send(self, text, **options):
            assert options.pop("expected_attachment_ownership_token") == "owned-retry"
            options.pop("expected_attachment_names", None)
            return await super().send(text, **options)

    client = RecoveryClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )
    block = DurableSendBlock(
        "upload recoverable",
        ledger_path=ledger_path,
        files=[str(attachment)],
        wait_for_response=False,
        stable_ms=0,
    )

    first = run_block(block, client)
    second = run_block(block, client)

    current = ledger.get(original_request_id)
    assert current is not None
    assert first.context.results["durable_send"]["record"]["request_id"] == original_request_id
    assert second.context.results["durable_send"]["record"]["request_id"] == original_request_id
    assert current.request_id == original_request_id
    assert current.status is RequestStatus.SENT
    assert current.attempts == 1
    assert len(client.upload_calls) == 1
    assert len(client.send_calls) == 1


def test_interrupted_upload_without_retry_authorization_fails_closed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload interrupted", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING, error=None)
    client = FakeDurableClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload interrupted",
                ledger_path=ledger_path,
                files=[str(attachment)],
                stable_ms=0,
            ),
            client,
        )

    assert isinstance(captured.value.cause, DurableRequestError)
    assert client.upload_calls == []
    assert client.send_calls == []


def test_consumed_upload_retry_authorization_crash_is_not_replayed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload retry crash", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(
        record.request_id,
        status=RequestStatus.UPLOADING,
        error="upload_retry_authorized:7",
    )

    class UploadCrash(BaseException):
        pass

    class CrashClient(FakeDurableClient):
        async def upload_files(self, paths, *, request_marker, **_options):
            self.upload_calls.append((tuple(paths), request_marker))
            consumed = ledger.get(record.request_id)
            assert consumed is not None and consumed.error is None
            raise UploadCrash()

    crash_client = CrashClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )
    block = DurableSendBlock(
        "upload retry crash",
        ledger_path=ledger_path,
        files=[str(attachment)],
        wait_for_response=False,
        stable_ms=0,
    )

    with pytest.raises(UploadCrash):
        run_block(block, crash_client)

    interrupted = ledger.get(record.request_id)
    assert interrupted is not None and interrupted.error is None
    assert len(crash_client.upload_calls) == 1
    assert crash_client.send_calls == []

    restarted = FakeDurableClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )
    with pytest.raises(Exception) as captured:
        run_block(block, restarted)

    assert isinstance(captured.value.cause, DurableRequestError)
    assert restarted.upload_calls == []
    assert restarted.send_calls == []


def test_failed_upload_with_unproven_attachment_fails_closed_without_discard(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ambiguous", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(
        record.request_id,
        status=RequestStatus.UPLOADING,
        error="RuntimeError: synthetic upload failure before readiness",
    )
    client = FakeDurableClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=("manual.txt",),
            state=ChatGPTState.DRAFT,
        )
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
    assert client.current.composer_text == record.rendered_prompt
    assert client.current.attachment_markers == ("manual.txt",)
    assert client.upload_calls == []
    assert client.send_calls == []


def test_uploading_ready_attachment_reconciles_live_ownership_without_reupload(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload ready", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(record.request_id, status=RequestStatus.UPLOADING)

    class ReadyClient(FakeDurableClient):
        async def current_attachment_ownership_token(self, *, expected_files):
            assert tuple(expected_files) == identities
            return "live-ready-owner"

        async def send(self, text, **options):
            assert options.pop("expected_attachment_ownership_token") == "live-ready-owner"
            options.pop("expected_attachment_names", None)
            return await super().send(text, **options)

    client = ReadyClient(
        snapshot(
            text=record.rendered_prompt,
            attachments=(attachment.name,),
            state=ChatGPTState.DRAFT,
        )
    )

    run_block(
        DurableSendBlock(
            "upload ready",
            ledger_path=ledger_path,
            files=[str(attachment)],
            wait_for_response=False,
            stable_ms=0,
        ),
        client,
    )

    current = ledger.get(record.request_id)
    assert current is not None
    assert current.status is RequestStatus.SENT
    assert current.attempts == 1
    assert client.upload_calls == []
    assert len(client.send_calls) == 1
    recovered = UploadReceipt.from_dict(current.upload_receipt)
    assert recovered.method == "recovered"
    assert recovered.ownership_token == "live-ready-owner"


def test_uploading_with_send_boundary_evidence_never_reuploads(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    attachment = tmp_path / "context.txt"
    attachment.write_text("context", encoding="utf-8")
    identities = collect_file_identities([attachment])
    ledger = RequestLedger(ledger_path)
    record = ledger.begin(role="DEV", prompt="upload crossed", files=identities)
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    record = ledger.update(
        record.request_id,
        status=RequestStatus.UPLOADING,
        binding=PageBinding("page-1", "DEV"),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        session_id_before="session-1",
    )
    client = FakeDurableClient(
        snapshot(text=record.rendered_prompt, state=ChatGPTState.DRAFT)
    )

    with pytest.raises(Exception) as captured:
        run_block(
            DurableSendBlock(
                "upload crossed",
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


def test_async_frontend_identity_enriches_only_exact_sent_ledger_without_resend(tmp_path):
    class AsyncIdentityClient(FakeDurableClient):
        def __init__(self, evidence):
            super().__init__()
            self.evidence = evidence
            self.identity_task = None
            self.gate = asyncio.Event()

        async def send(self, text, **kwargs):
            self.send_calls.append(text)
            baseline = capture_message_baseline(self.current.messages)
            user = MessageSnapshot("user", "u1", "t1", text, ())
            self.current = snapshot(messages=(*self.current.messages, user), state=ChatGPTState.SUBMITTING)

            async def identity():
                await self.gate.wait()
                return self.evidence

            self.identity_task = asyncio.create_task(identity())
            return SendReceipt(
                prompt=text,
                prompt_sha256=prompt_digest(text),
                binding=self.binding,
                baseline=baseline,
                attempts=1,
                accepted_via="exact_user_message",
                session_id_before="session-1",
                user_message_id="u1",
                user_turn_id="t1",
            )

        def take_frontend_identity_task(self):
            task = self.identity_task
            self.identity_task = None
            return task

    async def run(evidence):
        ledger_path = tmp_path / f"ledger-{evidence['observed_user_message_id']}.json"
        client = AsyncIdentityClient(evidence)
        await Workflow(
            "durable",
            [DurableSendBlock(
                "async identity",
                ledger_path=ledger_path,
                render_request_marker=False,
                wait_for_response=False,
            )],
        ).run(client)
        request_id = build_idempotency_key(role="DEV", prompt="async identity")[:24]
        before = RequestLedger(ledger_path).get(request_id)
        assert before is not None and before.status is RequestStatus.SENT
        assert before.receipt["conversation_id"] is None
        client.gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        after = RequestLedger(ledger_path).get(request_id)
        assert after is not None
        return client, after

    matching_client, matching = asyncio.run(run({
        "observed_user_message_id": "u1", "conversation_id": "conversation-1"
    }))
    assert matching.receipt["conversation_id"] == "conversation-1"
    assert matching.attempts == 1
    assert len(matching_client.send_calls) == 1

    mismatching_client, mismatching = asyncio.run(run({
        "observed_user_message_id": "other-user", "conversation_id": "conversation-wrong"
    }))
    assert mismatching.receipt["conversation_id"] is None
    assert mismatching.attempts == 1
    assert len(mismatching_client.send_calls) == 1


def test_frontend_identity_callback_does_not_mutate_completed_record(tmp_path):
    class LateIdentityClient(FakeDurableClient):
        def __init__(self):
            super().__init__()
            self.identity_task = None
            self.gate = asyncio.Event()

        async def send(self, text, **kwargs):
            baseline = capture_message_baseline(self.current.messages)
            user = MessageSnapshot("user", "u1", "t1", text, ())
            self.current = snapshot(messages=(*self.current.messages, user), state=ChatGPTState.SUBMITTING)

            async def identity():
                await self.gate.wait()
                return {"observed_user_message_id": "u1", "conversation_id": "too-late"}

            self.identity_task = asyncio.create_task(identity())
            return SendReceipt(
                prompt=text,
                prompt_sha256=prompt_digest(text),
                binding=self.binding,
                baseline=baseline,
                attempts=1,
                accepted_via="exact_user_message",
                session_id_before="session-1",
                user_message_id="u1",
                user_turn_id="t1",
            )

        def take_frontend_identity_task(self):
            task = self.identity_task
            self.identity_task = None
            return task

    async def run():
        ledger_path = tmp_path / "late-ledger.json"
        client = LateIdentityClient()
        result = await Workflow(
            "durable",
            [DurableSendBlock("late identity", ledger_path=ledger_path, render_request_marker=False)],
        ).run(client)
        request_id = build_idempotency_key(role="DEV", prompt="late identity")[:24]
        record = RequestLedger(ledger_path).get(request_id)
        assert record is not None and record.status is RequestStatus.COMPLETED
        client.gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        after = RequestLedger(ledger_path).get(request_id)
        assert after is not None
        assert after.status is RequestStatus.COMPLETED
        assert after.receipt["conversation_id"] is None
        return result

    asyncio.run(run())
