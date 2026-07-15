from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .chatgpt import SendReceipt
from .chatgpt_blocks import ChatGPTBlock
from .workflow import WorkflowBlock, WorkflowContext, resolve
from .workspace import (
    ChatGPTWorkspace,
    RepairCallback,
    RouteValidationError,
    parse_route_map,
)

TextSource = str | Callable[[WorkflowContext[ChatGPTWorkspace]], str | Awaitable[str]]


class RoleDispatchError(RuntimeError):
    def __init__(self, stage: str, errors: dict[str, str]) -> None:
        self.stage = stage
        self.errors = errors
        super().__init__(
            f"{stage} failed for roles: "
            + ", ".join(f"{role}={error}" for role, error in errors.items())
        )


class RoleSequenceBlock(WorkflowBlock[ChatGPTWorkspace]):
    def __init__(
        self,
        block_id: str,
        role: str | Callable[[WorkflowContext[ChatGPTWorkspace]], str],
        blocks: Iterable[ChatGPTBlock],
    ) -> None:
        super().__init__(block_id)
        self.role = role
        self.blocks = list(blocks)
        self.retry_safe = all(
            getattr(block, "retry_safe", False) for block in self.blocks
        )

    async def run(self, context: WorkflowContext[ChatGPTWorkspace]) -> dict[str, Any]:
        role = self.role(context) if callable(self.role) else self.role
        role = str(await resolve(role))
        client = context.client.get(role)
        subcontext = WorkflowContext(client=client, variables=context.variables)
        outputs: list[dict[str, Any]] = []
        async with client.workflow_guard():
            for block in self.blocks:
                output = await block.run(subcontext)
                subcontext.results[block.block_id] = output
                outputs.append({"block_id": block.block_id, "output": output})
        return {"role": role, "outputs": outputs}


class ParseRouteBlock(WorkflowBlock[ChatGPTWorkspace]):
    retry_safe = False

    def __init__(
        self,
        source: TextSource,
        *,
        route_key: str = "route_map",
        repair: RepairCallback | None = None,
        block_id: str = "parse_route",
    ) -> None:
        super().__init__(block_id)
        self.source = source
        self.route_key = route_key
        self.repair = repair

    async def run(self, context: WorkflowContext[ChatGPTWorkspace]) -> dict[str, str]:
        raw = self.source(context) if callable(self.source) else self.source
        text = str(await resolve(raw))
        try:
            route_map = parse_route_map(text, context.client.active_roles)
        except RouteValidationError as first_error:
            if self.repair is None:
                raise
            repaired = await resolve(self.repair(text, str(first_error)))
            try:
                route_map = parse_route_map(str(repaired), context.client.active_roles)
            except RouteValidationError as second_error:
                raise RouteValidationError(
                    f"route repair failed after one attempt: {second_error}"
                ) from second_error
        context.variables[self.route_key] = route_map
        return route_map


class DispatchRouteBlock(WorkflowBlock[ChatGPTWorkspace]):
    retry_safe = False

    def __init__(
        self,
        *,
        route_key: str = "route_map",
        receipts_key: str = "route_receipts",
        errors_key: str = "route_dispatch_errors",
        wait_for_stop: bool = True,
        max_attempts: int = 2,
        recovery_reload: bool = True,
        parallel: bool = True,
        fail_on_error: bool = True,
        block_id: str = "dispatch_route",
    ) -> None:
        super().__init__(block_id)
        self.route_key = route_key
        self.receipts_key = receipts_key
        self.errors_key = errors_key
        self.wait_for_stop = wait_for_stop
        self.max_attempts = max_attempts
        self.recovery_reload = recovery_reload
        self.parallel = parallel
        self.fail_on_error = fail_on_error

    async def run(self, context: WorkflowContext[ChatGPTWorkspace]) -> dict[str, Any]:
        route_map = context.require(self.route_key)
        if not isinstance(route_map, dict) or not route_map:
            raise TypeError(f"workflow variable {self.route_key!r} must be a route dict")

        async def send_one(role: str, prompt: str):
            client = context.client.get(role)
            try:
                async with client.workflow_guard():
                    receipt = await client.send(
                        str(prompt),
                        wait_for_stop=self.wait_for_stop,
                        max_attempts=self.max_attempts,
                        recovery_reload=self.recovery_reload,
                    )
                return role, receipt, None
            except Exception as exc:
                return role, None, f"{type(exc).__name__}: {exc}"

        items = [(str(role), str(prompt)) for role, prompt in route_map.items()]
        if self.parallel:
            outcomes = await asyncio.gather(
                *(send_one(role, prompt) for role, prompt in items)
            )
        else:
            outcomes = []
            for role, prompt in items:
                outcomes.append(await send_one(role, prompt))

        receipts: dict[str, SendReceipt] = {}
        errors: dict[str, str] = {}
        for role, receipt, error in outcomes:
            if receipt is not None:
                receipts[role] = receipt
            if error is not None:
                errors[role] = error
        context.variables[self.receipts_key] = receipts
        context.variables[self.errors_key] = errors
        if errors and self.fail_on_error:
            raise RoleDispatchError("route dispatch", errors)
        return {
            "receipts": {role: receipt.to_dict() for role, receipt in receipts.items()},
            "errors": errors,
        }


class WaitRouteResponsesBlock(WorkflowBlock[ChatGPTWorkspace]):
    def __init__(
        self,
        *,
        receipts_key: str = "route_receipts",
        responses_key: str = "route_responses",
        errors_key: str = "route_response_errors",
        timeout_ms: int | None = None,
        stable_ms: int = 1_000,
        poll_ms: int = 100,
        active_reload_after_ms: int | None = None,
        parallel: bool = True,
        fail_on_error: bool = True,
        block_id: str = "wait_route_responses",
    ) -> None:
        super().__init__(block_id)
        self.receipts_key = receipts_key
        self.responses_key = responses_key
        self.errors_key = errors_key
        self.timeout_ms = timeout_ms
        self.stable_ms = stable_ms
        self.poll_ms = poll_ms
        self.active_reload_after_ms = active_reload_after_ms
        self.parallel = parallel
        self.fail_on_error = fail_on_error

    async def run(self, context: WorkflowContext[ChatGPTWorkspace]) -> dict[str, Any]:
        receipts = context.require(self.receipts_key)
        if not isinstance(receipts, dict) or not receipts:
            raise TypeError(
                f"workflow variable {self.receipts_key!r} must contain SendReceipt values"
            )

        normalized: dict[str, SendReceipt] = {}
        for role, receipt in receipts.items():
            if isinstance(receipt, dict):
                receipt = SendReceipt.from_dict(receipt)
            if not isinstance(receipt, SendReceipt):
                raise TypeError(f"receipt for role {role!r} is not SendReceipt")
            normalized[str(role)] = receipt
        context.variables[self.receipts_key] = normalized

        async def wait_one(role: str, receipt: SendReceipt):
            client = context.client.get(role)
            try:
                async with client.workflow_guard():
                    response = await client.wait_for_response(
                        receipt,
                        timeout_ms=self.timeout_ms,
                        stable_ms=self.stable_ms,
                        poll_ms=self.poll_ms,
                        active_reload_after_ms=self.active_reload_after_ms,
                    )
                return role, response, None
            except Exception as exc:
                return role, None, f"{type(exc).__name__}: {exc}"

        items = list(normalized.items())
        if self.parallel:
            outcomes = await asyncio.gather(
                *(wait_one(role, receipt) for role, receipt in items)
            )
        else:
            outcomes = []
            for role, receipt in items:
                outcomes.append(await wait_one(role, receipt))

        responses = {}
        errors: dict[str, str] = {}
        for role, response, error in outcomes:
            if response is not None:
                responses[role] = response
            if error is not None:
                errors[role] = error
        context.variables[self.responses_key] = responses
        context.variables[self.errors_key] = errors
        if errors and self.fail_on_error:
            raise RoleDispatchError("route response wait", errors)
        return {
            "responses": {role: response.to_dict() for role, response in responses.items()},
            "errors": errors,
        }
