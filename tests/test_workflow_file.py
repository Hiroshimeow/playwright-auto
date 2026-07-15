from pathlib import Path

import pytest

from playwright_auto.loop import LoopOptions
from playwright_auto.workflow_file import load_workflow_file


def test_loads_single_python_workflow_file(tmp_path):
    path = tmp_path / "flow.py"
    path.write_text(
        """
from playwright_auto.workflow_api import *
VARIABLES = {'prompt': 'hello'}
WORKFLOW = Workflow('loaded', [SetVariableBlock('value', 3)])
LOOP = {'max_iterations': 2, 'interval_seconds': 0}
""",
        encoding="utf-8",
    )

    loaded = load_workflow_file(path)

    assert loaded.workflow.name == "loaded"
    assert loaded.variables == {"prompt": "hello"}
    assert loaded.loop_options == LoopOptions(max_iterations=2)


def test_rejects_file_without_workflow(tmp_path):
    path = tmp_path / "bad.py"
    path.write_text("VARIABLES = {}", encoding="utf-8")

    with pytest.raises(TypeError, match="WORKFLOW"):
        load_workflow_file(path)


def test_loads_workspace_roles_and_rejects_duplicates(tmp_path):
    path = tmp_path / "roles.py"
    path.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_ROLES = ('PLAN', 'DEV')
WORKFLOW = Workflow('roles', [])
""",
        encoding="utf-8",
    )
    loaded = load_workflow_file(path)
    assert loaded.workspace_roles == ("PLAN", "DEV")

    duplicate = tmp_path / "duplicate.py"
    duplicate.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_ROLES = ('PLAN', 'PLAN')
WORKFLOW = Workflow('roles', [])
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates"):
        load_workflow_file(duplicate)


def test_loads_workspace_team_as_visible_role_instances(tmp_path):
    path = tmp_path / "team.py"
    path.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_TEAM = {'PLAN': 1, 'DEV': 3, 'REVIEW': 2}
WORKFLOW = Workflow('team', [])
""",
        encoding="utf-8",
    )

    loaded = load_workflow_file(path)

    assert loaded.workspace_roles == (
        "PLAN",
        "DEV",
        "DEV1",
        "DEV2",
        "REVIEW",
        "REVIEW1",
    )
    assert [slot.base_role for slot in loaded.workspace_slots] == [
        "PLAN",
        "DEV",
        "DEV",
        "DEV",
        "REVIEW",
        "REVIEW",
    ]


def test_rejects_mixed_workspace_team_and_explicit_roles(tmp_path):
    path = tmp_path / "mixed.py"
    path.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_TEAM = {'DEV': 2}
WORKSPACE_ROLES = ('PLAN',)
WORKFLOW = Workflow('mixed', [])
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="either WORKSPACE_TEAM or WORKSPACE_ROLES"):
        load_workflow_file(path)


def test_workspace_timeout_defaults_and_can_be_overridden(tmp_path):
    default_path = tmp_path / "default_timeout.py"
    default_path.write_text(
        """
from playwright_auto.workflow_api import *
WORKFLOW = Workflow('default-timeout', [])
""",
        encoding="utf-8",
    )
    assert load_workflow_file(default_path).workspace_timeout_ms == 15_000

    custom_path = tmp_path / "custom_timeout.py"
    custom_path.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_TIMEOUT_MS = 30000
WORKFLOW = Workflow('custom-timeout', [])
""",
        encoding="utf-8",
    )
    assert load_workflow_file(custom_path).workspace_timeout_ms == 30_000


def test_workspace_timeout_validation(tmp_path):
    wrong_type = tmp_path / "wrong_type.py"
    wrong_type.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_TIMEOUT_MS = '30000'
WORKFLOW = Workflow('wrong-type', [])
""",
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="WORKSPACE_TIMEOUT_MS"):
        load_workflow_file(wrong_type)

    out_of_range = tmp_path / "out_of_range.py"
    out_of_range.write_text(
        """
from playwright_auto.workflow_api import *
WORKSPACE_TIMEOUT_MS = 999999
WORKFLOW = Workflow('out-of-range', [])
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="between"):
        load_workflow_file(out_of_range)
