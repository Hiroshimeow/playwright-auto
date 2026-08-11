from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    RateLimitBlockedError,
    UnsafePageStateError,
    rate_limit_dialogs,
)
from playwright_auto.team import TeamRoundSpec
from playwright_auto.team_blocks import DurableTeamRoleExecutor, TeamConversationBlock
from playwright_auto.workflow import WorkflowContext



def snapshot(*, dialogs=()):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/",
        session_id=None,
        page_id="page-1",
        page_role="DEV",
        state=ChatGPTState.NEW_CHAT,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=tuple(dialogs),
        attachment_markers=(),
        error_texts=(),
        messages=(),
    )


class RetryClient:
    def __init__(self) -> None:
        self.dismiss_calls = 0
        self.binding = type("Binding", (), {"role": "DEV"})()

    @asynccontextmanager
    async def workflow_guard(self):
        yield

    async def dismiss_known_rate_limit(self, timeout_ms=15_000):
        self.dismiss_calls += 1
        return "Got it"


class RetryWorkspace:
    active_roles = ("DEV",)

    def __init__(self, client):
        self.client = client

    def get(self, role):
        assert role == "DEV"
        return self.client


class PreflightClient:
    def __init__(self) -> None:
        self.preflight_calls = 0
        self.dismiss_calls = 0
        self.prepare_calls = 0

    async def task_preflight(self, task_id):
        self.preflight_calls += 1
        if self.preflight_calls == 1:
            raise RateLimitBlockedError("temporary request rate limit")
        return {"task_id": task_id}

    async def dismiss_known_rate_limit(self, timeout_ms=15_000):
        self.dismiss_calls += 1
        return "Got it"

    @asynccontextmanager
    async def workflow_guard(self):
        yield

    async def prepare_task(self, task_id):
        self.prepare_calls += 1
        return {"task_id": task_id, "reused": True}


class PreflightWorkspace:
    active_roles = ("DEV",)

    def __init__(self, client):
        self.client = client

    def get(self, role):
        assert role == "DEV"
        return self.client


class SuccessExecutor:
    async def execute(self, context, *, role, prompt, round_name, transcript):
        return {
            "record": {"request_id": "request"},
            "response": {
                "role": "assistant",
                "message_id": "message",
                "turn_id": "turn",
                "text": "ok",
                "actions": [],
                "image_count": 0,
            },
        }


def test_clean_ready_raises_known_rate_limit_instead_of_waiting_or_mutating():
    limited = snapshot(
        dialogs=(
            "Too many requests You’re making requests too quickly. "
            "We’ve temporarily limited access. Got it",
        )
    )
    page = ChatGPTPage(object(), timeout_ms=100)

    async def owned():
        return limited

    page.assert_ownership = owned

    with pytest.raises(RateLimitBlockedError, match="rate limit"):
        asyncio.run(page.wait_until_clean_ready(timeout_ms=100))


def test_rate_limit_dialog_is_classified_without_auto_accepting_unknown_dialog():
    limited = snapshot(
        dialogs=(
            "Too many requests You’re making requests too quickly. "
            "We’ve temporarily limited access. Got it",
        )
    )
    assert rate_limit_dialogs(limited) == limited.blocking_dialogs
    with pytest.raises(RateLimitBlockedError, match="rate limit"):
        ChatGPTPage._assert_interaction_safe(limited)

    unknown = snapshot(dialogs=("Delete this conversation? Cancel Delete",))
    assert rate_limit_dialogs(unknown) == ()
    with pytest.raises(UnsafePageStateError, match="blocking dialog"):
        ChatGPTPage._assert_interaction_safe(unknown)


def test_executor_recovers_known_rate_limit_then_reuses_same_durable_block(monkeypatch):
    client = RetryClient()
    workspace = RetryWorkspace(client)
    context = WorkflowContext(client=workspace, variables={"task_id": "TASK", "goal": "goal"})
    calls = 0

    async def fake_run(self, local_context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitBlockedError("limited")
        return {"response": {"text": "ok"}}

    monkeypatch.setattr("playwright_auto.team_blocks.DurableSendBlock.run", fake_run)
    executor = DurableTeamRoleExecutor(
        min_request_interval_seconds=0,
        rate_limit_cooldown_seconds=0.001,
        rate_limit_retries=1,
    )
    result = asyncio.run(
        executor.execute(
            context,
            role="DEV",
            prompt="prompt",
            round_name="implement",
            transcript=type("Transcript", (), {})(),
        )
    )

    assert result == {"response": {"text": "ok"}}
    assert calls == 2
    assert client.dismiss_calls == 1


def test_team_preflight_recovers_rate_limit_before_any_task_mutation(tmp_path):
    client = PreflightClient()
    context = WorkflowContext(
        client=PreflightWorkspace(client),
        variables={"task_id": "TASK", "goal": "goal"},
    )
    block = TeamConversationBlock(
        [TeamRoundSpec("implement", ("DEV",), "prompt")],
        executor=SuccessExecutor(),
        checkpoint_path=tmp_path / "checkpoint.json",
        rate_limit_cooldown_seconds=0.001,
        rate_limit_retries=1,
    )

    result = asyncio.run(block.run(context))

    assert result["rounds"][0]["completed_now"] == ["DEV"]
    assert client.preflight_calls == 2
    assert client.dismiss_calls == 1
    assert client.prepare_calls == 1


def test_rate_limit_policy_validation():
    with pytest.raises(ValueError, match="min_request_interval"):
        DurableTeamRoleExecutor(min_request_interval_seconds=-1)
    with pytest.raises(ValueError, match="cooldown"):
        DurableTeamRoleExecutor(rate_limit_cooldown_seconds=0)
    with pytest.raises(ValueError, match="retries"):
        DurableTeamRoleExecutor(rate_limit_retries=-1)
