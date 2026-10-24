from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class DependencyReadiness:
    ready: bool
    waiting_on: tuple[str, ...]
    stopped: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return bool(self.stopped or self.missing)

    @property
    def projection_state(self) -> str:
        if self.failed:
            return "failed"
        return "ready" if self.ready else "waiting"


@dataclass(frozen=True)
class TeamTaskTerminalEvent:
    task_id: str
    status: str


@dataclass(frozen=True)
class ContinuationWaitEdge:
    """Derived wait edge: target TeamTask waits for source TeamTask terminal state."""

    target_task_id: str
    source_task_id: str
    continuation_id: str | None = None


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


def dependency_parent_ids(task: Mapping[str, Any]) -> tuple[str, ...]:
    return _parents(task)


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
    normalized = _normalize_ids(parent_ids, current=current)
    index = task_index(tasks)
    missing = [parent for parent in normalized if parent not in index]
    if missing and not allow_missing:
        raise ValueError(f"missing dependency task(s): {missing!r}")
    graph = _dependency_graph(index, current=current, parents=normalized)
    _validate_acyclic_from(graph, current, label="dependency graph")
    return normalized


def dependency_readiness(
    task: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> DependencyReadiness:
    """Shared TeamTask prerequisite evaluator used by scheduling and continuations."""
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


def terminal_transition(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> TeamTaskTerminalEvent | None:
    """One terminal-transition detector for DAG and TeamTask continuation consumers."""
    before_status = str(before.get("status") or "").upper()
    after_status = str(after.get("status") or "").upper()
    if before_status == after_status or after_status not in {"DONE", "STOPPED"}:
        return None
    return TeamTaskTerminalEvent(task_id=_task_id(after), status=after_status)


def terminal_event_from_state(task: Mapping[str, Any]) -> TeamTaskTerminalEvent | None:
    """Read the TaskStore-emitted terminal event instead of re-deciding terminality."""
    task_id = _task_id(task)
    events = task.get("terminal_events")
    if not isinstance(events, list):
        return None
    for raw in reversed(events):
        if not isinstance(raw, Mapping) or raw.get("kind") != "teamtask_terminal":
            continue
        if str(raw.get("task_id") or "") != task_id:
            continue
        status = str(raw.get("status") or "").upper()
        if status in {"DONE", "STOPPED"}:
            return TeamTaskTerminalEvent(task_id=task_id, status=status)
    return None


def continuation_wait_edges(tasks: Sequence[Mapping[str, Any]]) -> tuple[ContinuationWaitEdge, ...]:
    """Derive TeamTask-source continuation edges without persisting another graph."""
    edges: list[ContinuationWaitEdge] = []
    for task in tasks:
        target_id = _task_id(task)
        registry = task.get("async_continuations")
        if not isinstance(registry, Mapping):
            continue
        for continuation_id, value in registry.items():
            if not isinstance(value, Mapping) or value.get("source_kind") != "teamtask":
                continue
            if str(value.get("state") or "") in {"delivered", "cancelled", "superseded"}:
                continue
            for source_id in value.get("source_refs") or ():
                source = str(source_id or "").strip()
                if source:
                    edges.append(
                        ContinuationWaitEdge(
                            target_task_id=target_id,
                            source_task_id=source,
                            continuation_id=str(continuation_id),
                        )
                    )
    return tuple(edges)


def validate_combined_wait_for_graph(
    tasks: Sequence[Mapping[str, Any]],
    *,
    additional_edges: Iterable[ContinuationWaitEdge] = (),
) -> None:
    """Fail closed when dependency and TeamTask-continuation waits form a cycle."""
    index = task_index(tasks)
    graph: dict[str, set[str]] = {
        task_id: set(_parents(task)) for task_id, task in index.items()
    }
    for edge in (*continuation_wait_edges(tasks), *tuple(additional_edges)):
        if edge.target_task_id not in index:
            raise ValueError(f"continuation target task is missing: {edge.target_task_id}")
        if edge.source_task_id not in index:
            raise ValueError(f"continuation source task is missing: {edge.source_task_id}")
        if edge.target_task_id == edge.source_task_id:
            raise ValueError("combined wait-for graph contains a self-cycle")
        graph.setdefault(edge.target_task_id, set()).add(edge.source_task_id)
    _validate_acyclic({key: tuple(value) for key, value in graph.items()}, label="combined wait-for graph")


def teamtask_source_readiness(
    source_ids: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
) -> DependencyReadiness:
    """Evaluate TeamTask continuation sources with the same prerequisite semantics."""
    synthetic = {"task_id": "__continuation_target__", "depends_on_task_ids": list(source_ids)}
    return dependency_readiness(synthetic, tasks)


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


def _normalize_ids(parent_ids: Sequence[str], *, current: str) -> tuple[str, ...]:
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
    return tuple(normalized)


def _dependency_graph(
    index: Mapping[str, Mapping[str, Any]],
    *,
    current: str,
    parents: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    graph = {key: _parents(value) for key, value in index.items()}
    graph[current] = tuple(parents)
    return graph


def _validate_acyclic_from(
    graph: Mapping[str, Sequence[str]], root: str, *, label: str
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"{label} contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, ()):
            if parent in graph:
                visit(parent)
        visiting.remove(node)
        visited.add(node)

    visit(root)


def _validate_acyclic(
    graph: Mapping[str, Sequence[str]], *, label: str = "dependency graph"
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"{label} contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, ()):
            if parent in graph:
                visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
