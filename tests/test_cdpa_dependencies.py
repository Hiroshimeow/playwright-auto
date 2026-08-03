from __future__ import annotations

import pytest

from playwright_auto.cdpa_dependencies import (
    dependency_readiness,
    derived_children,
    task_index,
    validate_new_dependencies,
)


def task(task_id: str, status: str = "INBOX", parents=()):
    return {
        "task_id": task_id,
        "status": status,
        "depends_on_task_ids": list(parents),
    }


def test_readiness_all_done_and_deterministic_order():
    tasks = [task("a", "DONE"), task("b", "DONE")]
    result = dependency_readiness(task("c", parents=("b", "a")), tasks)
    assert result.ready is True
    assert result.waiting_on == ()
    assert result.stopped == ()
    assert result.missing == ()


def test_readiness_tracks_running_stopped_and_missing_in_parent_order():
    tasks = [task("running", "RUNNING"), task("stopped", "STOPPED")]
    result = dependency_readiness(
        task("child", parents=("stopped", "missing", "running")), tasks
    )
    assert result.ready is False
    assert result.waiting_on == ("stopped", "running")
    assert result.stopped == ("stopped",)
    assert result.missing == ("missing",)


def test_dependency_validation_rejects_self_duplicate_missing_and_cycles():
    tasks = [
        task("a", parents=("b",)),
        task("b", parents=("c",)),
        task("c"),
    ]
    with pytest.raises(ValueError, match="self"):
        validate_new_dependencies("a", ("a",), tasks)
    with pytest.raises(ValueError, match="duplicate"):
        validate_new_dependencies("new", ("a", "a"), tasks)
    with pytest.raises(ValueError, match="missing"):
        validate_new_dependencies("new", ("absent",), tasks)
    with pytest.raises(ValueError, match="cycle"):
        validate_new_dependencies("c", ("a",), tasks)


def test_dependency_validation_rejects_empty_and_direct_cycle():
    tasks = [task("a", parents=("b",)), task("b")]
    with pytest.raises(ValueError, match="non-empty"):
        validate_new_dependencies("new", ("",), tasks)
    with pytest.raises(ValueError, match="cycle"):
        validate_new_dependencies("b", ("a",), tasks)


def test_dependency_validation_isolates_existing_missing_edges():
    tasks = [task("orphan", parents=("missing-parent",)), task("root")]

    assert validate_new_dependencies("new", (), tasks) == ()
    with pytest.raises(ValueError, match="missing"):
        validate_new_dependencies("new", ("missing-parent",), tasks)
    assert validate_new_dependencies(
        "orphan", ("missing-parent",), [task("root")], allow_missing=True
    ) == ("missing-parent",)
    with pytest.raises(ValueError, match="cycle"):
        validate_new_dependencies(
            "a", ("b",), [task("b", parents=("a",))], allow_missing=True
        )


def test_dependency_validation_scopes_cycles_to_current_dependency_closure():
    cycle = [task("a", parents=("b",)), task("b", parents=("a",))]

    assert validate_new_dependencies("unrelated", (), cycle) == ()
    with pytest.raises(ValueError, match="cycle"):
        validate_new_dependencies("dependent", ("a",), cycle)


def test_task_index_rejects_duplicate_task_ids():
    with pytest.raises(ValueError, match="duplicate task_id"):
        task_index([task("a"), task("a")])


def test_derived_children_is_scanned_not_persisted_and_sorted_by_task_order():
    tasks = [
        task("parent"),
        task("child-b", parents=("parent",)),
        task("child-a", parents=("parent",)),
        task("other", parents=("child-b",)),
    ]
    result = derived_children(tasks)
    assert result == {
        "parent": ("child-b", "child-a"),
        "child-b": ("other",),
    }
    assert all("child_task_ids" not in item for item in tasks)
