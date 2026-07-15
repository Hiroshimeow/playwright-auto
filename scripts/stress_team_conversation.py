#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
import shutil
import time
from pathlib import Path

from playwright_auto.team import TeamRoundSpec, TeamTranscript
from playwright_auto.team_blocks import TeamConversationBlock, TeamRoundError
from playwright_auto.workflow import WorkflowContext

ROLES = (
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
EXPECTED_BY_ROUND = {
    "plan": {"PLAN"},
    "implement": {"DEV", "DEV1", "DEV2"},
    "review": {"REVIEW", "REVIEW1", "REVIEW2", "REVIEW3", "TEST", "TEST1"},
    "revise": {"DEV", "DEV1", "DEV2"},
    "closeout": {"PLAN"},
}
BASE_EXECUTIONS = sum(len(items) for items in EXPECTED_BY_ROUND.values())


class FakeRoleClient:
    def __init__(self):
        self.task_id = None

    async def task_preflight(self, task_id):
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
        return {
            "task_id": task_id,
            "previous_task_id": previous,
            "reused": previous == task_id,
            "new_chat_method": None if previous == task_id else "dom",
        }


class FakeWorkspace:
    def __init__(self):
        self.clients = {role: FakeRoleClient() for role in ROLES}

    @property
    def active_roles(self):
        return ROLES

    def get(self, role):
        return self.clients[role]


class StressExecutor:
    def __init__(self, fail_once=()):
        self.fail_once = set(fail_once)
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def execute(self, context, *, role, prompt, round_name, transcript):
        self.calls.append((round_name, role, prompt))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.0005)
        self.active -= 1
        key = (round_name, role)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise RuntimeError(f"injected-once:{round_name}:{role}")
        return {
            "cached": False,
            "record": {"request_id": f"{context.require('task_id')}:{round_name}:{role}"},
            "receipt": {"role": role, "round": round_name},
            "response": {
                "role": "assistant",
                "message_id": f"m-{round_name}-{role}",
                "turn_id": f"t-{round_name}-{role}",
                "text": f"{context.require('task_id')} answer {round_name} {role}",
                "actions": [],
                "image_count": 0,
            },
        }


def rounds():
    return [
        TeamRoundSpec(
            "plan",
            ("PLAN",),
            lambda ctx, role, tx: f"{role} plan {ctx.require('goal')}",
            parallel=False,
        ),
        TeamRoundSpec(
            "implement",
            ("DEV*",),
            lambda ctx, role, tx: f"{role} implement\n{tx.render(round_names=('plan',))}",
        ),
        TeamRoundSpec(
            "review",
            ("REVIEW*", "TEST*"),
            lambda ctx, role, tx: f"{role} verify\n{tx.render(round_names=('implement',))}",
        ),
        TeamRoundSpec(
            "revise",
            ("DEV*",),
            lambda ctx, role, tx: f"{role} revise\n{tx.render(round_names=('review',))}",
        ),
        TeamRoundSpec(
            "closeout",
            ("PLAN",),
            lambda ctx, role, tx: f"{role} close\n{tx.render()}",
            parallel=False,
        ),
    ]


async def run_task(index: int, root: Path, semaphore: asyncio.Semaphore):
    async with semaphore:
        checkpoint = root / f"task-{index}.json"
        injected = set()
        if index % 7 == 0:
            injected.add(("implement", "DEV1"))
        elif index % 11 == 0:
            injected.add(("review", "REVIEW2"))
        executor = StressExecutor(injected)
        variables = {"task_id": f"TASK-{index}", "goal": f"stress goal {index}"}
        block = TeamConversationBlock(
            rounds(),
            executor=executor,
            checkpoint_path=checkpoint,
        )
        context = WorkflowContext(client=FakeWorkspace(), variables=dict(variables))
        resumed = False
        try:
            await block.run(context)
        except TeamRoundError:
            resumed = True
            # Simulate a fresh process: new block and new workflow context.
            block = TeamConversationBlock(
                rounds(),
                executor=executor,
                checkpoint_path=checkpoint,
            )
            context = WorkflowContext(client=FakeWorkspace(), variables=dict(variables))
            await block.run(context)

        transcript = context.variables["team_transcript"]
        if not isinstance(transcript, TeamTranscript):
            raise AssertionError("team transcript object missing")
        for round_name, expected in EXPECTED_BY_ROUND.items():
            actual = set(transcript.completed_roles(round_name))
            if actual != expected:
                raise AssertionError(
                    f"task {index} round {round_name}: expected={expected} actual={actual}"
                )
        expected_calls = BASE_EXECUTIONS + len(injected)
        if len(executor.calls) != expected_calls:
            raise AssertionError(
                f"task {index}: expected {expected_calls} executions, got {len(executor.calls)}"
            )
        return {
            "task": index,
            "resumed": resumed,
            "calls": len(executor.calls),
            "max_parallel": executor.max_active,
            "digest": transcript.digest(),
        }


async def main_async(args):
    root = args.output.parent / "team-conversation-stress-checkpoints"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    started = time.monotonic()
    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(
        *(run_task(index, root, semaphore) for index in range(1, args.tasks + 1))
    )
    duration = time.monotonic() - started
    summary = {
        "status": "passed",
        "tasks": args.tasks,
        "concurrency": args.concurrency,
        "base_executions_per_task": BASE_EXECUTIONS,
        "total_role_executions": sum(item["calls"] for item in results),
        "resumed_tasks": sum(1 for item in results if item["resumed"]),
        "max_parallel_within_task": max(item["max_parallel"] for item in results),
        "duration_ms": round(duration * 1000, 3),
        "tasks_per_second": round(args.tasks / duration, 3),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Stress durable multi-role team orchestration")
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/team-conversation-stress.json"),
    )
    args = parser.parse_args()
    if args.tasks < 1 or args.concurrency < 1:
        parser.error("tasks and concurrency must be positive")
    print(json.dumps(asyncio.run(main_async(args)), indent=2))


if __name__ == "__main__":
    main()
