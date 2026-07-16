from __future__ import annotations

from playwright_auto.studio.models import (
    RunStatus,
    StudioSettings,
    StudioState,
    WorkerModel,
    WorkerStatus,
    duplicate_roles,
    render_worker_prompt,
    reorder_pending_workers,
    slug_task_id,
)


def worker(page_id: str, role: str | None, status: WorkerStatus = WorkerStatus.IDLE) -> WorkerModel:
    return WorkerModel(
        page_id=page_id,
        title=f"Tab {page_id}",
        url=f"https://chatgpt.com/c/{page_id}",
        role=role,
        status=status,
    )


def test_state_roundtrip_preserves_worker_runtime_fields():
    first = worker("a", "DEV", WorkerStatus.COMPLETED)
    first.prompt_template = "Implement the assigned part"
    first.responses = ["one", "two"]
    first.last_prompt = "sent prompt"
    first.error = ""
    state = StudioState(
        settings=StudioSettings(
            global_goal="Build it",
            task_id="TASK-1",
            rounds=2,
            response_timeout_ms=90_000,
            context_char_limit=4_000,
        ),
        workers=[first, worker("b", "REVIEW", WorkerStatus.QUEUED)],
        run_status=RunStatus.PAUSED,
        active_page_id="b",
        current_round=1,
    )

    restored = StudioState.from_dict(state.to_dict())

    assert restored.to_dict() == state.to_dict()
    assert restored.workers[0].responses == ["one", "two"]


def test_duplicate_roles_ignores_unassigned_and_returns_sorted_duplicates():
    workers = [
        worker("a", "REVIEW"),
        worker("b", None),
        worker("c", "DEV"),
        worker("d", "REVIEW"),
        worker("e", "DEV"),
    ]

    assert duplicate_roles(workers) == ("DEV", "REVIEW")


def test_render_worker_prompt_contains_goal_role_template_and_prior_outputs():
    current = worker("b", "REVIEW")
    current.prompt_template = "Check the prior implementation and return concrete findings."

    prompt = render_worker_prompt(
        global_goal="Ship a safe release",
        worker=current,
        ordinal=2,
        total_workers=3,
        round_index=1,
        total_rounds=2,
        prior_outputs=[("DEV", "implemented alpha"), ("TEST", "tests passed")],
        context_char_limit=2_000,
    )

    assert "Ship a safe release" in prompt
    assert "Worker: REVIEW (2/3)" in prompt
    assert "Round: 2/2" in prompt
    assert "Check the prior implementation" in prompt
    assert "[DEV]\nimplemented alpha" in prompt
    assert "[TEST]\ntests passed" in prompt


def test_render_worker_prompt_truncates_old_context_first():
    current = worker("c", "REVIEW")
    current.prompt_template = "Review"

    prompt = render_worker_prompt(
        global_goal="Goal",
        worker=current,
        ordinal=3,
        total_workers=3,
        round_index=0,
        total_rounds=1,
        prior_outputs=[("A", "a" * 200), ("B", "b" * 200)],
        context_char_limit=180,
    )

    assert "[earlier context truncated]" in prompt
    assert "b" * 40 in prompt
    assert len(prompt) < 1_000


def test_reorder_pending_workers_keeps_started_prefix_fixed():
    workers = [
        worker("a", "PLAN", WorkerStatus.COMPLETED),
        worker("b", "DEV", WorkerStatus.RUNNING),
        worker("c", "REVIEW", WorkerStatus.QUEUED),
        worker("d", "TEST", WorkerStatus.IDLE),
        worker("e", "SECURITY", WorkerStatus.QUEUED),
    ]

    reordered = reorder_pending_workers(workers, "e", 2)

    assert [item.page_id for item in reordered] == ["a", "b", "e", "c", "d"]
    assert [item.order for item in reordered] == list(range(5))


def test_reorder_rejects_active_or_completed_source():
    workers = [
        worker("a", "PLAN", WorkerStatus.COMPLETED),
        worker("b", "DEV", WorkerStatus.QUEUED),
    ]

    try:
        reorder_pending_workers(workers, "a", 1)
    except ValueError as exc:
        assert "not pending" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_slug_task_id_is_stable_and_safe():
    first = slug_task_id("Review QMH: correctness / recovery")
    second = slug_task_id("Review QMH: correctness / recovery")

    assert first == second
    assert first.startswith("review-qmh-correctness-recovery-")
    assert all(character.isalnum() or character == "-" for character in first)
