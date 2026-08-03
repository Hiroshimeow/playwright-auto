import asyncio
from contextlib import asynccontextmanager

import pytest

import playwright_auto.upload as upload
from playwright_auto.chatgpt import (
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
)
from playwright_auto.upload import (
    UploadReadinessError,
    collect_file_identities,
    upload_files,
    wait_upload_ready,
)


def snapshot(
    *,
    text="ROLE_REQUEST_ID: request-1",
    attachments=(),
    send_visible=True,
    send_enabled=True,
    stop=False,
    composer=True,
):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/",
        session_id=None,
        page_id="page-1",
        page_role="DEV",
        state=ChatGPTState.RESPONDING if stop else ChatGPTState.DRAFT,
        requires_login=False,
        composer_present=composer,
        composer_editable=composer,
        composer_text=text,
        send_visible=send_visible,
        send_enabled=send_enabled,
        stop_visible=stop,
        blocking_dialogs=(),
        attachment_markers=tuple(attachments),
        error_texts=(),
        messages=(),
    )


class FakeClient:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0
        self.page = object()

    async def assert_ownership(self):
        if self.index < len(self.snapshots) - 1:
            current = self.snapshots[self.index]
            self.index += 1
            return current
        return self.snapshots[-1]

    @asynccontextmanager
    async def mutation_guard(self):
        yield


def test_collect_file_identities_hashes_content(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text("hello", encoding="utf-8")

    identity = collect_file_identities([path])[0]

    assert identity.name == "prompt.txt"
    assert identity.size == 5
    assert len(identity.sha256) == 64
    assert identity.mime_type == "text/plain"


def test_collect_file_identities_enforces_total_limit(tmp_path):
    path = tmp_path / "large.bin"
    path.write_bytes(b"1234")

    with pytest.raises(ValueError, match="max_total_bytes"):
        collect_file_identities([path], max_total_bytes=3)


def test_wait_upload_ready_requires_marker_attachments_and_send():
    client = FakeClient(
        [
            snapshot(attachments=(), send_enabled=False),
            snapshot(attachments=("file.txt",), send_enabled=True),
        ]
    )

    ready = asyncio.run(
        wait_upload_ready(
            client,
            request_marker="ROLE_REQUEST_ID: request-1",
            expected_count=1,
            timeout_ms=20,
            poll_ms=1,
        )
    )

    assert ready.attachment_markers == ("file.txt",)


def test_wait_upload_ready_rejects_marker_loss():
    client = FakeClient([snapshot(text="manual text", attachments=("file.txt",))])

    with pytest.raises(ComposerConflictError, match="durable request marker"):
        asyncio.run(
            wait_upload_ready(
                client,
                request_marker="ROLE_REQUEST_ID: request-1",
                expected_count=1,
                timeout_ms=10,
                poll_ms=1,
            )
        )


def test_wait_upload_ready_reports_missing_attachments():
    client = FakeClient([snapshot(attachments=())])

    with pytest.raises(UploadReadinessError, match="upload_attachments_missing"):
        asyncio.run(
            wait_upload_ready(
                client,
                request_marker="ROLE_REQUEST_ID: request-1",
                expected_count=1,
                timeout_ms=5,
                poll_ms=1,
            )
        )
