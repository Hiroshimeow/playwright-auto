import asyncio
from dataclasses import dataclass

import pytest

from playwright_auto.workflow import (
    ActionBlock,
    BlockStatus,
    RetryBlock,
    WhenBlock,
    Workflow,
    WorkflowExecutionError,
)


def run(coro):
    return asyncio.run(coro)


def test_workflow_runs_in_order_and_records_results():
    events = []
    workflow = Workflow(
        "ordered",
        [
            ActionBlock("one", lambda ctx: events.append("one") or 1),
            ActionBlock("two", lambda ctx: events.append("two") or ctx.result("one") + 1),
        ],
    )

    result = run(workflow.run(object()))

    assert events == ["one", "two"]
    assert result.status is BlockStatus.PASSED
    assert result.context.results == {"one": 1, "two": 2}
    assert [item.block_id for item in result.context.trace] == ["one", "two"]


def test_workflow_can_insert_replace_remove_and_clone():
    workflow = Workflow(
        "editable",
        [ActionBlock("a", lambda ctx: "a"), ActionBlock("c", lambda ctx: "c")],
    )
    workflow.insert_after("a", ActionBlock("b", lambda ctx: "b"))
    workflow.replace("c", ActionBlock("c2", lambda ctx: "c2"))
    workflow.insert_before("a", ActionBlock("start", lambda ctx: "start"))
    workflow.remove("b")

    clone = workflow.clone("editable-copy")

    assert [block.block_id for block in workflow.blocks] == ["start", "a", "c2"]
    assert [block.block_id for block in clone.blocks] == ["start", "a", "c2"]
    assert clone.name == "editable-copy"


def test_workflow_rejects_duplicate_block_ids():
    with pytest.raises(ValueError, match="duplicate"):
        Workflow(
            "bad",
            [ActionBlock("same", lambda ctx: None), ActionBlock("same", lambda ctx: None)],
        )


def test_failed_mutation_does_not_corrupt_workflow():
    workflow = Workflow(
        "safe-edit",
        [ActionBlock("one", lambda ctx: 1), ActionBlock("two", lambda ctx: 2)],
    )

    with pytest.raises(ValueError, match="duplicate"):
        workflow.replace("two", ActionBlock("one", lambda ctx: "duplicate"))

    assert [block.block_id for block in workflow.blocks] == ["one", "two"]


def test_run_to_dict_serializes_objects_with_to_dict():
    class Value:
        def to_dict(self):
            return {"nested": (1, 2)}

    result = run(
        Workflow("serialize", [ActionBlock("value", lambda ctx: Value())]).run(
            object(), {"input": Value()}
        )
    )

    assert result.to_dict()["variables"] == {"input": {"nested": [1, 2]}}
    assert result.to_dict()["results"] == {"value": {"nested": [1, 2]}}


def test_fail_fast_preserves_trace_and_cause():
    def fail(_ctx):
        raise RuntimeError("boom")

    workflow = Workflow(
        "failure",
        [ActionBlock("ok", lambda ctx: 1), ActionBlock("bad", fail)],
    )

    with pytest.raises(WorkflowExecutionError) as captured:
        run(workflow.run(object()))

    error = captured.value
    assert error.block_id == "bad"
    assert isinstance(error.cause, RuntimeError)
    assert [item.status for item in error.context.trace] == [
        BlockStatus.PASSED,
        BlockStatus.FAILED,
    ]


def test_non_fail_fast_continues_after_failure():
    def fail(_ctx):
        raise RuntimeError("boom")

    workflow = Workflow(
        "continue",
        [
            ActionBlock("bad", fail),
            ActionBlock("after", lambda ctx: "continued"),
        ],
        fail_fast=False,
    )

    result = run(workflow.run(object()))

    assert result.status is BlockStatus.FAILED
    assert result.context.results == {"after": "continued"}
    assert [item.status for item in result.context.trace] == [
        BlockStatus.FAILED,
        BlockStatus.PASSED,
    ]


def test_when_block_selects_one_branch():
    workflow = Workflow(
        "branch",
        [
            WhenBlock(
                "choose",
                lambda ctx: ctx.require("enabled"),
                then_blocks=[ActionBlock("yes", lambda ctx: "yes")],
                else_blocks=[ActionBlock("no", lambda ctx: "no")],
            )
        ],
    )

    yes = run(workflow.run(object(), {"enabled": True}))
    no = run(workflow.run(object(), {"enabled": False}))

    assert yes.context.results["choose"] == {"matched": True, "outputs": ["yes"]}
    assert no.context.results["choose"] == {"matched": False, "outputs": ["no"]}


def test_retry_block_retries_until_success():
    attempts = {"count": 0}

    def unstable(_ctx):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("not yet")
        return "done"

    workflow = Workflow(
        "retry",
        [RetryBlock(
            "retry_unstable",
            ActionBlock("unstable", unstable, retry_safe=True),
            attempts=3,
        )],
    )

    result = run(workflow.run(object()))

    assert result.context.results["retry_unstable"] == {
        "attempt": 3,
        "output": "done",
    }


def test_set_variable_repeat_wait_until_and_delay_blocks():
    from playwright_auto.workflow import (
        DelayBlock,
        RepeatBlock,
        SetVariableBlock,
        WaitUntilBlock,
    )

    workflow = Workflow(
        "control-blocks",
        [
            SetVariableBlock("count", 0),
            RepeatBlock(
                "increment",
                [
                    SetVariableBlock(
                        "count",
                        lambda ctx: ctx.require("count") + 1,
                        block_id="increment_once",
                    )
                ],
                times=3,
            ),
            WaitUntilBlock(
                "wait_count",
                lambda ctx: ctx.require("count") == 3,
                timeout_seconds=0.1,
                poll_seconds=0.001,
            ),
            DelayBlock(0, block_id="no_delay"),
        ],
    )

    result = run(workflow.run(object()))

    assert result.context.variables["count"] == 3
    assert result.context.results["wait_count"] is True


def test_timeout_block_stops_hanging_action():
    import asyncio
    from playwright_auto.workflow import TimeoutBlock

    async def slow(_ctx):
        await asyncio.sleep(0.1)

    workflow = Workflow(
        "timeout",
        [TimeoutBlock("limited", ActionBlock("slow", slow), timeout_seconds=0.01)],
    )

    with pytest.raises(WorkflowExecutionError) as captured:
        run(workflow.run(object()))

    assert isinstance(captured.value.cause, TimeoutError)


def test_try_block_handles_error_and_always_runs_finally():
    from playwright_auto.workflow import SetVariableBlock, TryBlock

    def fail(_ctx):
        raise RuntimeError("expected")

    workflow = Workflow(
        "try",
        [
            TryBlock(
                "protected",
                [ActionBlock("fail", fail)],
                except_blocks=[SetVariableBlock("handled", True)],
                finally_blocks=[SetVariableBlock("cleaned", True)],
            )
        ],
    )

    result = run(workflow.run(object()))

    assert result.context.variables["handled"] is True
    assert result.context.variables["cleaned"] is True
    assert result.context.results["protected"]["error"] == "RuntimeError: expected"


def test_repeat_block_has_hard_safety_limit():
    from playwright_auto.workflow import RepeatBlock

    workflow = Workflow(
        "repeat-limit",
        [
            RepeatBlock(
                "forever",
                [ActionBlock("noop", lambda ctx: None)],
                while_predicate=lambda ctx: True,
                max_iterations=2,
            )
        ],
    )

    with pytest.raises(WorkflowExecutionError, match="max_iterations=2"):
        run(workflow.run(object()))


def test_workflow_guard_serializes_same_physical_client():
    import asyncio
    from contextlib import asynccontextmanager

    events = []
    lock = asyncio.Lock()

    class GuardedClient:
        @asynccontextmanager
        async def workflow_guard(self):
            async with lock:
                yield

    async def action(name):
        events.append(f"{name}:start")
        await asyncio.sleep(0.01)
        events.append(f"{name}:end")

    first = Workflow("first", [ActionBlock("run_first", lambda ctx: action("first"))])
    second = Workflow("second", [ActionBlock("run_second", lambda ctx: action("second"))])
    client = GuardedClient()

    async def scenario():
        await asyncio.gather(first.run(client), second.run(client))

    asyncio.run(scenario())

    assert events in [
        ["first:start", "first:end", "second:start", "second:end"],
        ["second:start", "second:end", "first:start", "first:end"],
    ]


def test_generic_retry_rejects_ambiguous_send_side_effect():
    from playwright_auto.chatgpt_blocks import SendPromptBlock

    with pytest.raises(ValueError, match="ambiguous side effects"):
        RetryBlock(
            "unsafe_retry",
            SendPromptBlock("do not duplicate"),
            attempts=2,
        )


def test_retry_rejects_composite_with_unsafe_nested_action():
    from playwright_auto.workflow import SequenceBlock

    unsafe = SequenceBlock(
        "sequence",
        [ActionBlock("side_effect", lambda ctx: None)],
    )

    with pytest.raises(ValueError, match="ambiguous side effects"):
        RetryBlock("retry_sequence", unsafe, attempts=2)


def test_retry_accepts_composite_only_when_all_nested_blocks_are_safe():
    from playwright_auto.workflow import SequenceBlock

    safe = SequenceBlock(
        "sequence",
        [ActionBlock("read_only", lambda ctx: "ok", retry_safe=True)],
    )
    retry = RetryBlock("retry_sequence", safe, attempts=2)

    result = run(Workflow("safe-composite", [retry]).run(object()))
    assert result.context.results["retry_sequence"]["output"] == ["ok"]
