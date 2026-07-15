import asyncio
import json
from pathlib import Path

import pytest

from playwright_auto.loop import LoopOptions, LoopStatus, WorkflowLoop
from playwright_auto.workflow import ActionBlock, SetVariableBlock, Workflow


def run(coro):
    return asyncio.run(coro)


def test_loop_runs_fixed_number_and_shares_variables(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    workflow = Workflow(
        "counter",
        [
            SetVariableBlock(
                "count",
                lambda ctx: ctx.variables.get("count", 0) + 1,
            )
        ],
    )
    loop = WorkflowLoop(
        workflow,
        LoopOptions(max_iterations=3, checkpoint_path=checkpoint),
    )

    result = run(loop.run(object(), {}))

    assert result.status is LoopStatus.COMPLETED
    assert result.reason == "max_iterations_reached"
    assert result.context.variables["count"] == 3
    assert [item.iteration for item in result.context.iterations] == [1, 2, 3]
    stored = json.loads(checkpoint.read_text())
    assert stored["status"] == "completed"
    assert stored["variables"]["count"] == 3


def test_infinite_loop_requires_stop_file():
    with pytest.raises(ValueError, match="stop_file"):
        LoopOptions(max_iterations=None)


def test_existing_stop_file_prevents_first_iteration(tmp_path):
    stop_file = tmp_path / "STOP"
    stop_file.touch()
    workflow = Workflow("never", [ActionBlock("fail", lambda ctx: 1 / 0)])
    result = run(
        WorkflowLoop(
            workflow,
            LoopOptions(max_iterations=None, stop_file=stop_file),
        ).run(object())
    )

    assert result.status is LoopStatus.STOPPED
    assert result.reason == "stop_file_exists"
    assert result.context.iterations == []


def test_loop_stops_on_predicate():
    workflow = Workflow(
        "predicate",
        [SetVariableBlock("count", lambda ctx: ctx.variables.get("count", 0) + 1)],
    )
    result = run(
        WorkflowLoop(
            workflow,
            LoopOptions(max_iterations=10),
            stop_when=lambda ctx: ctx.variables["count"] == 2,
        ).run(object())
    )

    assert result.status is LoopStatus.STOPPED
    assert result.reason == "stop_predicate_matched"
    assert len(result.context.iterations) == 2


def test_loop_stops_on_workflow_error_by_default():
    workflow = Workflow(
        "failure",
        [ActionBlock("fail", lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))],
    )
    result = run(WorkflowLoop(workflow, LoopOptions(max_iterations=3)).run(object()))

    assert result.status is LoopStatus.FAILED
    assert result.reason == "workflow_error"
    assert len(result.context.iterations) == 1
    assert "RuntimeError: boom" in result.context.iterations[0].error


def test_loop_can_continue_after_workflow_error():
    attempts = {"count": 0}

    def unstable(_ctx):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("first")
        return attempts["count"]

    workflow = Workflow("continue", [ActionBlock("unstable", unstable)])
    result = run(
        WorkflowLoop(
            workflow,
            LoopOptions(max_iterations=2, continue_on_error=True),
        ).run(object())
    )

    assert result.status is LoopStatus.COMPLETED
    assert [item.status.value for item in result.context.iterations] == [
        "failed",
        "passed",
    ]


def test_fail_fast_false_failed_run_is_not_reported_completed():
    workflow = Workflow(
        "soft-failure",
        [ActionBlock("fail", lambda ctx: 1 / 0)],
        fail_fast=False,
    )
    result = run(WorkflowLoop(workflow, LoopOptions(max_iterations=3)).run(object()))

    assert result.status is LoopStatus.FAILED
    assert result.reason == "workflow_returned_failed"
    assert len(result.context.iterations) == 1


def test_loop_does_not_sleep_after_last_iteration():
    import time

    workflow = Workflow("fast", [ActionBlock("ok", lambda ctx: True)])
    started = time.monotonic()
    result = run(
        WorkflowLoop(
            workflow,
            LoopOptions(max_iterations=1, interval_seconds=0.5),
        ).run(object())
    )
    elapsed = time.monotonic() - started

    assert result.status is LoopStatus.COMPLETED
    assert elapsed < 0.2
