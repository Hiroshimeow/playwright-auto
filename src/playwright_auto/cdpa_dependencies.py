from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class DependencyReadiness:
    ready: bool
    waiting_on: tuple[str, ...]
    stopped: tuple[str, ...]
    missing: tuple[str, ...]


def _task_id(task: Mapping[str, Any]) -> str:
    value = task.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task_id must be a non-empty string")
    return value.strip()


def _parents(task: Mapping[str, Any]) -> tuple[str, ...]:
    value = task.get("depends_on_task_ids", ())
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("depends_on_task_ids must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("dependency IDs must be non-empty strings")
        result.append(item.strip())
    return tuple(result)


def task_index(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        task_id = _task_id(task)
        if task_id in result:
            raise ValueError(f"duplicate task_id in dependency graph: {task_id}")
        result[task_id] = task
    return result


def validate_new_dependencies(
    task_id: str,
    parent_ids: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
    *,
    allow_missing: bool = False,
) -> tuple[str, ...]:
    current = str(task_id).strip()
    if not current:
        raise ValueError("task_id must be a non-empty string")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in parent_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("dependency IDs must be non-empty strings")
        parent = raw.strip()
        if parent == current:
            raise ValueError("task cannot depend on itself")
        if parent in seen:
            raise ValueError(f"duplicate dependency ID: {parent}")
        seen.add(parent)
        normalized.append(parent)

    index = task_index(tasks)
    missing = [parent for parent in normalized if parent not in index]
    if missing and not allow_missing:
        raise ValueError(f"missing dependency task(s): {missing!r}")

    graph = {key: _parents(value) for key, value in index.items()}
    graph[current] = tuple(normalized)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("dependency graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, ()):
            if parent not in graph:
                continue
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    visit(current)
    return tuple(normalized)


def dependency_readiness(
    task: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> DependencyReadiness:
    index = task_index(tasks)
    waiting: list[str] = []
    stopped: list[str] = []
    missing: list[str] = []
    for parent_id in _parents(task):
        parent = index.get(parent_id)
        if parent is None:
            missing.append(parent_id)
            continue
        status = str(parent.get("status") or "").upper()
        if status == "DONE":
            continue
        waiting.append(parent_id)
        if status == "STOPPED":
            stopped.append(parent_id)
    return DependencyReadiness(
        ready=not waiting and not missing,
        waiting_on=tuple(waiting),
        stopped=tuple(stopped),
        missing=tuple(missing),
    )


def derived_children(
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    index = task_index(tasks)
    children: dict[str, list[str]] = {}
    for child in tasks:
        child_id = _task_id(child)
        for parent_id in _parents(child):
            if parent_id in index:
                children.setdefault(parent_id, []).append(child_id)
    return {key: tuple(value) for key, value in children.items()}
