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
