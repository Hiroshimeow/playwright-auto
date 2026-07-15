from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .loop import LoopOptions
from .roles import RoleSlot, expand_role_team
from .chatgpt import validate_page_role
from .workflow import Workflow


@dataclass(frozen=True)
class WorkflowFile:
    path: Path
    workflow: Workflow[Any]
    loop_options: LoopOptions
    variables: dict[str, Any]
    workspace_roles: tuple[str, ...]
    workspace_slots: tuple[RoleSlot, ...]
    workspace_timeout_ms: int


def _load_module(path: Path) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(
        f"playwright_auto_user_workflow_{abs(hash(path.resolve()))}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load workflow file {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_workflow_file(path: str | Path) -> WorkflowFile:
    resolved = Path(path).expanduser().resolve()
    module = _load_module(resolved)

    workflow = getattr(module, "WORKFLOW", None)
    if not isinstance(workflow, Workflow):
        raise TypeError("workflow file must export WORKFLOW = Workflow(...)")

    raw_loop = getattr(module, "LOOP", LoopOptions())
    if isinstance(raw_loop, LoopOptions):
        loop_options = raw_loop
    elif isinstance(raw_loop, dict):
        loop_options = LoopOptions.from_mapping(raw_loop)
    else:
        raise TypeError("LOOP must be LoopOptions or dict")

    variables = getattr(module, "VARIABLES", {})
    if not isinstance(variables, dict):
        raise TypeError("VARIABLES must be a dict")

    raw_roles = getattr(module, "WORKSPACE_ROLES", ())
    raw_team = getattr(module, "WORKSPACE_TEAM", None)
    if raw_team is not None and raw_roles:
        raise ValueError("define either WORKSPACE_TEAM or WORKSPACE_ROLES, not both")
    if raw_team is not None:
        workspace_slots = expand_role_team(raw_team)
        workspace_roles = tuple(slot.display_name for slot in workspace_slots)
    else:
        if not isinstance(raw_roles, (list, tuple)):
            raise TypeError("WORKSPACE_ROLES must be a list or tuple")
        workspace_roles = tuple(validate_page_role(str(role)) for role in raw_roles)
        if len(set(workspace_roles)) != len(workspace_roles):
            raise ValueError("WORKSPACE_ROLES must not contain duplicates")
        workspace_slots = tuple(RoleSlot(role) for role in workspace_roles)

    raw_workspace_timeout = getattr(module, "WORKSPACE_TIMEOUT_MS", 15_000)
    if not isinstance(raw_workspace_timeout, int):
        raise TypeError("WORKSPACE_TIMEOUT_MS must be an int")
    if not 1_000 <= raw_workspace_timeout <= 300_000:
        raise ValueError("WORKSPACE_TIMEOUT_MS must be between 1000 and 300000")

    return WorkflowFile(
        path=resolved,
        workflow=workflow,
        loop_options=loop_options,
        variables=dict(variables),
        workspace_roles=workspace_roles,
        workspace_slots=workspace_slots,
        workspace_timeout_ms=raw_workspace_timeout,
    )
