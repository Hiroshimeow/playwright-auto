import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from playwright_auto.team import (
    TeamRoundSpec,
    TeamTranscript,
    resolve_role_selectors,
)
from playwright_auto.team_blocks import (
    TeamCheckpointMismatchError,
    TeamConversationBlock,
    TeamRoundError,
)
from playwright_auto.workflow import WorkflowContext


class FakeRoleClient:
    def __init__(self, role):
        self.role = role
        self.task_id = None
        self.preflight_calls = []
        self.prepare_calls = []

    async def task_preflight(self, task_id):
        self.preflight_calls.append(task_id)
        return {
            "task_id": task_id,
            "previous_task_id": self.task_id,
            "requires_new_chat": self.task_id != task_id,
        }

    @asynccontextmanager
    async def workflow_guard(self):
        yield

    async def prepare_task(self, task_id):
        previous = self.task_id
        self.task_id = task_id
        self.prepare_calls.append(task_id)
        return {
            "task_id": task_id,
            "previous_task_id": previous,
            "reused": previous == task_id,
            "new_chat_method": None if previous == task_id else "dom",
        }


class FakeWorkspace:
    def __init__(self, roles):
        self._roles = tuple(roles)
        self.clients = {role: FakeRoleClient(role) for role in self._roles}

    @property
    def active_roles(self):
        return self._roles

    def get(self, role):
        return self.clients[role]


class FakeExecutor:
    def __init__(self, *, fail_roles=()):
        self.fail_roles = set(fail_roles)
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def execute(self, context, *, role, prompt, round_name, transcript):
        self.calls.append((round_name, role, prompt))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        if role in self.fail_roles:
            raise RuntimeError(f"synthetic failure for {role}")
        return {
            "cached": False,
            "record": {"request_id": f"{round_name}-{role}"},
            "receipt": {"role": role},
            "response": {
                "role": "assistant",
                "message_id": f"message-{round_name}-{role}",
                "turn_id": f"turn-{round_name}-{role}",
                "text": f"answer from {role} in {round_name}",
                "actions": [],
                "image_count": 0,
            },
        }


def test_role_selectors_expand_arbitrary_team_groups():
    active = (
        "PLAN",
        "DEV",
        "DEV1",
        "DEV2",
        "REVIEW",
        "REVIEW1",
        "REVIEW2",
        "REVIEW3",
        "TEST",
        "TEST1",
    )
    assert resolve_role_selectors(active, ("DEV*",)) == ("DEV", "DEV1", "DEV2")
    assert resolve_role_selectors(active, ("REVIEW*", "TEST*")) == (
        "REVIEW",
        "REVIEW1",
        "REVIEW2",
        "REVIEW3",
        "TEST",
        "TEST1",
    )
    assert resolve_role_selectors(active, ("*",)) == active
    with pytest.raises(ValueError, match="matched no active roles"):
        resolve_role_selectors(active, ("AUDIT*",))


def test_team_conversation_runs_plan_devs_reviews_tests_and_closeout(tmp_path):
    roles = (
        "PLAN",
        "DEV",
        "DEV1",
        "DEV2",
        "REVIEW",
        "REVIEW1",
        "REVIEW2",
        "REVIEW3",
        "TEST",
        "TEST1",
    )
    executor = FakeExecutor()

    def plan_prompt(ctx, role, transcript):
        return f"{role}: plan {ctx.require('goal')}"

    def implement_prompt(ctx, role, transcript):
        return f"{role}: implement using {transcript.response_text('plan', 'PLAN')}"

    def review_prompt(ctx, role, transcript):
        return f"{role}: review\n{transcript.render(round_names=('implement',))}"

    def closeout_prompt(ctx, role, transcript):
        return f"{role}: closeout\n{transcript.render()}"

    block = TeamConversationBlock(
        [
            TeamRoundSpec("plan", ("PLAN",), plan_prompt, parallel=False),
            TeamRoundSpec("implement", ("DEV*",), implement_prompt),
            TeamRoundSpec("review", ("REVIEW*", "TEST*"), review_prompt),
            TeamRoundSpec("closeout", ("PLAN",), closeout_prompt, parallel=False),
        ],
        executor=executor,
        checkpoint_path=tmp_path / "team.json",
    )
    context = WorkflowContext(
        client=FakeWorkspace(roles),
        variables={"goal": "build stable workflow"},
    )

    result = asyncio.run(block.run(context))

    transcript = context.variables["team_transcript"]
    assert isinstance(transcript, TeamTranscript)
    assert transcript.completed_roles("implement") == ("DEV", "DEV1", "DEV2")
    assert set(transcript.completed_roles("review")) == {
        "REVIEW",
        "REVIEW1",
        "REVIEW2",
        "REVIEW3",
        "TEST",
        "TEST1",
    }
    assert transcript.completed_roles("closeout") == ("PLAN",)
    assert len(executor.calls) == 11
    assert executor.max_active >= 3
    assert result["rounds"][-1]["round"] == "closeout"


def test_partial_round_resume_uses_exact_persisted_prompt(tmp_path):
    checkpoint = tmp_path / "resume.json"
    first_executor = FakeExecutor(fail_roles={"DEV1"})
    prompt_calls = []

    def prompt_builder(ctx, role, transcript):
        prompt = f"initial-{role}-rounds={len(transcript.rounds)}"
        prompt_calls.append((role, prompt))
        return prompt

    first = TeamConversationBlock(
        [TeamRoundSpec("implement", ("DEV*",), prompt_builder)],
        executor=first_executor,
        checkpoint_path=checkpoint,
    )
    first_context = WorkflowContext(
        client=FakeWorkspace(("DEV", "DEV1")),
        variables={"task_id": "resume-task", "goal": "resume goal"},
    )

    with pytest.raises(TeamRoundError, match="DEV1"):
        asyncio.run(first.run(first_context))

    persisted = TeamTranscript.from_dict(
        __import__("json").loads(checkpoint.read_text())["transcript"]
    )
    expected_dev1_prompt = persisted.get_prompt("implement", "DEV1")
    assert expected_dev1_prompt == "initial-DEV1-rounds=0"
    assert persisted.has_success("implement", "DEV") is True
    assert persisted.has_success("implement", "DEV1") is False

    second_executor = FakeExecutor()

    def changed_builder(ctx, role, transcript):
        return f"CHANGED-{role}-rounds={len(transcript.rounds)}"

    second = TeamConversationBlock(
        [TeamRoundSpec("implement", ("DEV*",), changed_builder)],
        executor=second_executor,
        checkpoint_path=checkpoint,
    )
    second_context = WorkflowContext(
        client=FakeWorkspace(("DEV", "DEV1")),
        variables={"task_id": "resume-task", "goal": "resume goal"},
    )
    result = asyncio.run(second.run(second_context))

    assert second_executor.calls == [
        ("implement", "DEV1", expected_dev1_prompt)
    ]
    assert result["rounds"][0]["resumed"] == ["DEV"]
    assert result["rounds"][0]["completed_now"] == ["DEV1"]


def test_transcript_round_trip_and_prompt_immutability():
    transcript = TeamTranscript()
    transcript.record_prompt("plan", "PLAN", "exact prompt")
    transcript.record_success(
        "plan",
        "PLAN",
        prompt="exact prompt",
        result={"response": {"text": "answer", "image_count": 0}},
    )
    restored = TeamTranscript.from_dict(transcript.to_dict())
    assert restored.to_dict() == transcript.to_dict()
    assert restored.response_text("plan", "PLAN") == "answer"
    with pytest.raises(ValueError, match="persisted prompt changed"):
        restored.record_prompt("plan", "PLAN", "different prompt")



def test_checkpoint_identity_rejects_same_path_with_changed_goal(tmp_path):
    checkpoint = tmp_path / "fixed.json"
    first = TeamConversationBlock(
        [TeamRoundSpec("plan", ("PLAN",), "plan")],
        executor=FakeExecutor(),
        checkpoint_path=checkpoint,
    )
    first_context = WorkflowContext(
        client=FakeWorkspace(("PLAN",)),
        variables={"task_id": "TASK-1", "goal": "goal one"},
    )
    asyncio.run(first.run(first_context))

    second = TeamConversationBlock(
        [TeamRoundSpec("plan", ("PLAN",), "plan")],
        executor=FakeExecutor(),
        checkpoint_path=checkpoint,
    )
    second_context = WorkflowContext(
        client=FakeWorkspace(("PLAN",)),
        variables={"task_id": "TASK-1", "goal": "goal changed"},
    )
    with pytest.raises(TeamCheckpointMismatchError, match="another task"):
        asyncio.run(second.run(second_context))


def test_checkpoint_template_isolates_tasks_and_sanitizes_path(tmp_path):
    template = tmp_path / "{task_id}.json"
    first_context = WorkflowContext(
        client=FakeWorkspace(("PLAN",)),
        variables={"task_id": "../TASK A", "goal": "goal a"},
    )
    second_context = WorkflowContext(
        client=FakeWorkspace(("PLAN",)),
        variables={"task_id": "TASK B", "goal": "goal b"},
    )
    for context in (first_context, second_context):
        block = TeamConversationBlock(
            [TeamRoundSpec("plan", ("PLAN",), "plan")],
            executor=FakeExecutor(),
            checkpoint_path=template,
        )
        asyncio.run(block.run(context))

    first_path = Path(first_context.variables["team_transcript_checkpoint"])
    second_path = Path(second_context.variables["team_transcript_checkpoint"])
    assert first_path != second_path
    assert first_path.parent == tmp_path.resolve()
    assert second_path.parent == tmp_path.resolve()
    assert first_path.exists() and second_path.exists()


def test_goal_only_generates_stable_task_id(tmp_path):
    context = WorkflowContext(
        client=FakeWorkspace(("PLAN",)),
        variables={"goal": "goal-only workflow"},
    )
    block = TeamConversationBlock(
        [TeamRoundSpec("plan", ("PLAN",), "plan")],
        executor=FakeExecutor(),
        checkpoint_path=tmp_path / "{task_id}.json",
    )
    asyncio.run(block.run(context))
    generated = context.variables["task_id"]
    assert generated.startswith("goal-")
    assert context.client.clients["PLAN"].prepare_calls == [generated]


def test_checkpoint_identity_ignores_workspace_attach_order(tmp_path):
    checkpoint = tmp_path / "order.json"
    first_context = WorkflowContext(
        client=FakeWorkspace(("PLAN", "DEV", "REVIEW")),
        variables={"task_id": "ORDER-1", "goal": "same goal"},
    )
    first = TeamConversationBlock(
        [TeamRoundSpec("plan", ("PLAN",), "plan")],
        executor=FakeExecutor(),
        checkpoint_path=checkpoint,
    )
    asyncio.run(first.run(first_context))

    second_executor = FakeExecutor()
    second_context = WorkflowContext(
        client=FakeWorkspace(("DEV", "REVIEW", "PLAN")),
        variables={"task_id": "ORDER-1", "goal": "same goal"},
    )
    second = TeamConversationBlock(
        [TeamRoundSpec("plan", ("PLAN",), "changed but should resume")],
        executor=second_executor,
        checkpoint_path=checkpoint,
    )
    result = asyncio.run(second.run(second_context))

    assert second_executor.calls == []
    assert result["rounds"][0]["resumed"] == ["PLAN"]
