from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .chatgpt import ChatGPTPage
from .upload import UploadReceipt
from .workflow import WorkflowBlock, WorkflowContext, resolve

PathsSource = Sequence[str] | Callable[
    [WorkflowContext[ChatGPTPage]], Sequence[str] | Awaitable[Sequence[str]]
]
TextSource = str | Callable[
    [WorkflowContext[ChatGPTPage]], str | Awaitable[str]
]


class UploadFilesBlock(WorkflowBlock[ChatGPTPage]):
    retry_safe = False

    def __init__(
        self,
        paths: PathsSource,
        request_marker: TextSource,
        *,
        receipt_key: str = "upload_receipt",
        timeout_ms: int | None = None,
        max_total_bytes: int = 20 * 1024 * 1024,
        block_id: str = "upload_files",
    ) -> None:
        super().__init__(block_id)
        self.paths = paths
        self.request_marker = request_marker
        self.receipt_key = receipt_key
        self.timeout_ms = timeout_ms
        self.max_total_bytes = max_total_bytes

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        raw_paths = self.paths(context) if callable(self.paths) else self.paths
        paths = [str(path) for path in await resolve(raw_paths)]
        raw_marker = (
            self.request_marker(context)
            if callable(self.request_marker)
            else self.request_marker
        )
        marker = str(await resolve(raw_marker)).strip()
        if not marker:
            raise ValueError("request marker must not be empty")
        receipt = await context.client.upload_files(
            paths,
            request_marker=marker,
            timeout_ms=self.timeout_ms,
            max_total_bytes=self.max_total_bytes,
        )
        context.variables[self.receipt_key] = receipt
        return receipt.to_dict()


class WaitUploadReadyBlock(WorkflowBlock[ChatGPTPage]):
    def __init__(
        self,
        request_marker: TextSource,
        expected_count: int | Callable[[WorkflowContext[ChatGPTPage]], int],
        *,
        snapshot_key: str = "upload_ready_snapshot",
        timeout_ms: int | None = None,
        poll_ms: int = 100,
        block_id: str = "wait_upload_ready",
    ) -> None:
        super().__init__(block_id)
        self.request_marker = request_marker
        self.expected_count = expected_count
        self.snapshot_key = snapshot_key
        self.timeout_ms = timeout_ms
        self.poll_ms = poll_ms

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        raw_marker = (
            self.request_marker(context)
            if callable(self.request_marker)
            else self.request_marker
        )
        marker = str(await resolve(raw_marker)).strip()
        raw_count = (
            self.expected_count(context)
            if callable(self.expected_count)
            else self.expected_count
        )
        expected_count = int(await resolve(raw_count))
        snapshot = await context.client.wait_upload_ready(
            request_marker=marker,
            expected_count=expected_count,
            timeout_ms=self.timeout_ms,
            poll_ms=self.poll_ms,
        )
        context.variables[self.snapshot_key] = snapshot
        return snapshot.to_dict()
