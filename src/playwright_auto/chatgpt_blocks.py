from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .chatgpt import ChatGPTPage, ChatGPTState, SendReceipt
from .workflow import WorkflowBlock, WorkflowContext, resolve

PromptSource = str | Callable[
    [WorkflowContext[ChatGPTPage]], str | Awaitable[str]
]


async def _resolve_text(
    value: PromptSource | None, context: WorkflowContext[ChatGPTPage]
) -> str | None:
    if value is None:
        return None
    raw = value(context) if callable(value) else value
    return str(await resolve(raw))


class ChatGPTBlock(WorkflowBlock[ChatGPTPage]):
    pass


class NewChatBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        block_id: str = "new_chat",
        *,
        discard_draft: bool = False,
        discard_attachments: bool = False,
        stop_first: bool = False,
    ) -> None:
        super().__init__(block_id)
        self.discard_draft = discard_draft
        self.discard_attachments = discard_attachments
        self.stop_first = stop_first

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> str:
        return await context.client.new_chat(
            discard_draft=self.discard_draft,
            discard_attachments=self.discard_attachments,
            stop_first=self.stop_first,
        )


class RefreshBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        block_id: str = "refresh",
        *,
        allow_responding: bool = True,
    ) -> None:
        super().__init__(block_id)
        self.allow_responding = allow_responding

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        await context.client.refresh(allow_responding=self.allow_responding)
        return (await context.client.snapshot()).to_dict()


class SetRoleBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        role: str,
        block_id: str = "set_role",
        *,
        allow_rebind: bool = False,
        force_new_page_id: bool = False,
    ) -> None:
        super().__init__(block_id)
        self.role = role
        self.allow_rebind = allow_rebind
        self.force_new_page_id = force_new_page_id

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, str]:
        return await context.client.set_role(
            self.role,
            allow_rebind=self.allow_rebind,
            force_new_page_id=self.force_new_page_id,
        )


class PrepareTaskBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        task_id: PromptSource,
        block_id: str = "prepare_task",
        *,
        force_new_chat: bool = False,
    ) -> None:
        super().__init__(block_id)
        self.task_id = task_id
        self.force_new_chat = force_new_chat

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        task_id = await _resolve_text(self.task_id, context)
        assert task_id is not None
        return await context.client.prepare_task(
            task_id,
            force_new_chat=self.force_new_chat,
        )


class SetComposerBlock(ChatGPTBlock):
    def __init__(
        self,
        text: PromptSource,
        block_id: str = "set_composer",
        *,
        overwrite: bool = False,
        expected_existing: PromptSource | None = None,
    ) -> None:
        super().__init__(block_id)
        self.text = text
        self.overwrite = overwrite
        self.expected_existing = expected_existing

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> str:
        text = await _resolve_text(self.text, context)
        assert text is not None
        expected_existing = await _resolve_text(self.expected_existing, context)
        await context.client.set_text(
            text,
            overwrite=self.overwrite,
            expected_existing=expected_existing,
        )
        return text


class ClearComposerBlock(ChatGPTBlock):
    def __init__(
        self,
        block_id: str = "clear_composer",
        *,
        force: bool = False,
        expected_text: PromptSource | None = None,
    ) -> None:
        super().__init__(block_id)
        self.force = force
        self.expected_text = expected_text

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> None:
        expected = await _resolve_text(self.expected_text, context)
        await context.client.clear(force=self.force, expected_text=expected)


class SendPromptBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        prompt: PromptSource,
        *,
        wait_for_stop: bool = True,
        max_attempts: int = 2,
        recovery_reload: bool = True,
        expected_attachment_count: int = 0,
        receipt_key: str = "send_receipt",
        block_id: str = "send_prompt",
    ) -> None:
        super().__init__(block_id)
        self.prompt = prompt
        self.wait_for_stop = wait_for_stop
        self.max_attempts = max_attempts
        self.recovery_reload = recovery_reload
        self.expected_attachment_count = expected_attachment_count
        self.receipt_key = receipt_key

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        prompt = await _resolve_text(self.prompt, context)
        assert prompt is not None
        receipt = await context.client.send(
            prompt,
            wait_for_stop=self.wait_for_stop,
            max_attempts=self.max_attempts,
            recovery_reload=self.recovery_reload,
            expected_attachment_count=self.expected_attachment_count,
        )
        context.variables[self.receipt_key] = receipt
        return receipt.to_dict()


class WaitResponseBlock(ChatGPTBlock):
    def __init__(
        self,
        *,
        receipt_key: str = "send_receipt",
        response_key: str = "response",
        timeout_ms: int | None = None,
        stable_ms: int = 1_000,
        poll_ms: int = 100,
        active_reload_after_ms: int | None = None,
        reload_wait_ms: int = 750,
        skeptical_after_reload: bool = True,
        resolve_choice_prompt: bool = False,
        block_id: str = "wait_response",
    ) -> None:
        super().__init__(block_id)
        self.receipt_key = receipt_key
        self.response_key = response_key
        self.timeout_ms = timeout_ms
        self.stable_ms = stable_ms
        self.poll_ms = poll_ms
        self.active_reload_after_ms = active_reload_after_ms
        self.reload_wait_ms = reload_wait_ms
        self.skeptical_after_reload = skeptical_after_reload
        self.resolve_choice_prompt = resolve_choice_prompt

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        receipt = context.require(self.receipt_key)
        if isinstance(receipt, dict):
            receipt = SendReceipt.from_dict(receipt)
            context.variables[self.receipt_key] = receipt
        if not isinstance(receipt, SendReceipt):
            raise TypeError(
                f"workflow variable {self.receipt_key!r} must be SendReceipt"
            )
        response = await context.client.wait_for_response(
            receipt,
            timeout_ms=self.timeout_ms,
            stable_ms=self.stable_ms,
            poll_ms=self.poll_ms,
            active_reload_after_ms=self.active_reload_after_ms,
            reload_wait_ms=self.reload_wait_ms,
            skeptical_after_reload=self.skeptical_after_reload,
            resolve_choice_prompt=self.resolve_choice_prompt,
        )
        context.variables[self.response_key] = response
        return response.to_dict()


class WaitCleanReadyBlock(ChatGPTBlock):
    def __init__(
        self,
        *,
        timeout_ms: int | None = None,
        poll_ms: int = 100,
        resolve_choice_prompt: bool = False,
        block_id: str = "wait_clean_ready",
    ) -> None:
        super().__init__(block_id)
        self.timeout_ms = timeout_ms
        self.poll_ms = poll_ms
        self.resolve_choice_prompt = resolve_choice_prompt

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        snapshot = await context.client.wait_until_clean_ready(
            timeout_ms=self.timeout_ms,
            poll_ms=self.poll_ms,
            resolve_choice_prompt=self.resolve_choice_prompt,
        )
        return snapshot.to_dict()


class ResolveChoicePromptBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        *,
        timeout_ms: int | None = None,
        block_id: str = "resolve_choice_prompt",
    ) -> None:
        super().__init__(block_id)
        self.timeout_ms = timeout_ms

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> str:
        return await context.client.resolve_choice_prompt(timeout_ms=self.timeout_ms)


class RecoverPageBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(
        self,
        *,
        allow_new_chat: bool = False,
        resolve_choice_prompt: bool = True,
        timeout_ms: int | None = None,
        require_healthy: bool = True,
        block_id: str = "recover_page",
    ) -> None:
        super().__init__(block_id)
        self.allow_new_chat = allow_new_chat
        self.resolve_choice_prompt = resolve_choice_prompt
        self.timeout_ms = timeout_ms
        self.require_healthy = require_healthy

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        health = await context.client.recover_page(
            allow_new_chat=self.allow_new_chat,
            resolve_choice_prompt=self.resolve_choice_prompt,
            timeout_ms=self.timeout_ms,
        )
        if self.require_healthy and not health.healthy:
            raise RuntimeError(
                f"ChatGPT page recovery failed: state={health.state} action={health.action}"
            )
        return health.to_dict()


class StopResponseBlock(ChatGPTBlock):
    retry_safe = False

    def __init__(self, block_id: str = "stop_response") -> None:
        super().__init__(block_id)

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> str:
        return await context.client.stop()


class WaitStateBlock(ChatGPTBlock):
    def __init__(
        self,
        state: ChatGPTState,
        *,
        stable_ms: int = 0,
        block_id: str | None = None,
    ) -> None:
        super().__init__(block_id or f"wait_{state.value}")
        self.state = state
        self.stable_ms = stable_ms

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        snapshot = await context.client.wait_for_state(
            self.state, stable_ms=self.stable_ms
        )
        return snapshot.to_dict()


class CaptureSnapshotBlock(ChatGPTBlock):
    def __init__(self, key: str, block_id: str | None = None) -> None:
        super().__init__(block_id or f"capture_{key}")
        self.key = key

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> dict[str, Any]:
        snapshot = await context.client.snapshot()
        context.variables[self.key] = snapshot
        return snapshot.to_dict()


class AssertBlock(ChatGPTBlock):
    def __init__(
        self,
        predicate: Callable[[WorkflowContext[ChatGPTPage]], bool],
        message: str,
        block_id: str = "assert",
    ) -> None:
        super().__init__(block_id)
        self.predicate = predicate
        self.message = message

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> bool:
        if not bool(await resolve(self.predicate(context))):
            raise AssertionError(self.message)
        return True


class SaveRecentResponsesBlock(ChatGPTBlock):
    """Unbound history read; use WaitResponseBlock for request provenance."""

    def __init__(
        self,
        count: int,
        key: str = "responses",
        *,
        by_turn: bool = True,
        block_id: str = "save_recent_responses",
    ) -> None:
        super().__init__(block_id)
        self.count = count
        self.key = key
        self.by_turn = by_turn

    async def run(self, context: WorkflowContext[ChatGPTPage]) -> list[dict[str, Any]]:
        messages = await context.client.recent_responses(
            self.count, by_turn=self.by_turn
        )
        context.variables[self.key] = messages
        return [message.to_dict() for message in messages]
