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
    FileSnapshot,
    UploadError,
    UploadReadinessError,
    UploadReceipt,
    collect_file_identities,
    collect_file_snapshots,
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
            expected_names=("file.txt",),
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
                expected_names=("file.txt",),
                timeout_ms=10,
                poll_ms=1,
            )
        )


def test_wait_upload_ready_accepts_collapsed_markerless_prompt_whitespace():
    prompt = "First line\n\nSecond line"
    client = FakeClient(
        [snapshot(text="First line Second line", attachments=("file.txt",))]
    )

    ready = asyncio.run(
        wait_upload_ready(
            client,
            request_marker=prompt,
            exact_prompt=True,
            expected_names=("file.txt",),
            timeout_ms=10,
            poll_ms=1,
        )
    )

    assert ready.attachment_markers == ("file.txt",)


def test_wait_upload_ready_rejects_changed_markerless_prompt_content():
    client = FakeClient(
        [snapshot(text="First line changed", attachments=("file.txt",))]
    )

    with pytest.raises(ComposerConflictError, match="durable request marker"):
        asyncio.run(
            wait_upload_ready(
                client,
                request_marker="First line\n\nSecond line",
                exact_prompt=True,
                expected_names=("file.txt",),
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
                expected_names=("file.txt",),
                timeout_ms=5,
                poll_ms=1,
            )
        )


def test_upload_refuses_partial_existing_attachments(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    client = FakeClient([snapshot(attachments=("a.txt",))])

    with pytest.raises(ComposerConflictError, match="unowned"):
        asyncio.run(
            upload_files(
                client,
                [str(first), str(second)],
                request_marker="ROLE_REQUEST_ID: request-1",
                timeout_ms=10,
            )
        )


def test_fresh_upload_refuses_preexisting_attachment_even_when_name_matches(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("a", encoding="utf-8")
    client = FakeClient([snapshot(attachments=("a.txt",))])

    with pytest.raises(ComposerConflictError, match="unowned attachments"):
        asyncio.run(
            upload_files(
                client,
                [str(path)],
                request_marker="ROLE_REQUEST_ID: request-1",
                timeout_ms=10,
            )
        )



def test_wait_upload_ready_requires_exact_expected_names():
    client = FakeClient([
        snapshot(attachments=("wrong.txt",), send_enabled=True),
    ])

    with pytest.raises(UploadReadinessError, match="upload_attachment_identity_mismatch"):
        asyncio.run(
            wait_upload_ready(
                client,
                request_marker="ROLE_REQUEST_ID: request-1",
                expected_names=("file.txt",),
                timeout_ms=5,
                poll_ms=1,
            )
        )

def test_collect_file_identities_rejects_duplicate_canonical_paths(tmp_path):
    path = tmp_path / "same.txt"
    path.write_text("same", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate upload path"):
        collect_file_identities([path, path.parent / "." / path.name])


def test_upload_expected_identity_mismatch_blocks_before_browser_mutation(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "context.txt"
    path.write_bytes(b"original-bytes")
    expected = collect_file_identities([path])
    path.write_bytes(b"changed-before-snapshot")
    client = FakeClient([snapshot()])
    browser_mutated = False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal browser_mutated
        browser_mutated = True
        return True

    monkeypatch.setattr(upload, "_upload_via_input", fail_if_called)

    with pytest.raises(UploadError, match="source identity changed"):
        asyncio.run(
            upload_files(
                client,
                [str(path)],
                request_marker="ROLE_REQUEST_ID: request-1",
                timeout_ms=10,
                expected_files=expected,
            )
        )

    assert browser_mutated is False


def test_upload_uses_supplied_snapshot_bytes_after_source_path_changes(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "context.txt"
    path.write_bytes(b"authoritative-bytes")
    file_snapshots = collect_file_snapshots([path])
    expected = tuple(item.identity for item in file_snapshots)
    path.write_bytes(b"changed-after-durable-snapshot")
    client = FakeClient(
        [
            snapshot(attachments=()),
            snapshot(attachments=("context.txt",)),
        ]
    )
    captured_payloads = []

    async def capture_input(_page, snapshots):
        captured_payloads.extend(item.data for item in snapshots)
        return True

    async def ownership_token(_page, *, expected_names):
        assert tuple(expected_names) == ("context.txt",)
        return "ownership-token"

    monkeypatch.setattr(upload, "_upload_via_input", capture_input)
    monkeypatch.setattr(upload, "establish_attachment_ownership", ownership_token)

    receipt = asyncio.run(
        upload_files(
            client,
            [str(path)],
            request_marker="ROLE_REQUEST_ID: request-1",
            timeout_ms=10,
            expected_files=expected,
            file_snapshots=file_snapshots,
        )
    )

    assert captured_payloads == [b"authoritative-bytes"]
    assert receipt.files == expected
    assert receipt.ownership_token == "ownership-token"


def test_transient_file_snapshot_bytes_are_not_serialized(tmp_path):
    path = tmp_path / "context.txt"
    path.write_bytes(b"private-snapshot-bytes")
    identity = collect_file_identities([path])[0]
    snapshot_value = FileSnapshot(identity=identity, data=b"private-snapshot-bytes")
    receipt = UploadReceipt(
        request_marker="request",
        method="input",
        files=(snapshot_value.identity,),
        attachment_count=1,
        ownership_token="token",
    )

    serialized = receipt.to_dict()

    assert "data" not in serialized
    assert b"private-snapshot-bytes" not in serialized.values()
    assert serialized["files"][0]["sha256"] == identity.sha256
