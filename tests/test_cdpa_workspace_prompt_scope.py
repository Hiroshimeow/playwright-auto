from pathlib import PureWindowsPath

from playwright_auto.cdpa_prompts import PromptBuilder
from test_cdpa_core import _prompt_kwargs, write_config
from playwright_auto.cdpa_config import load_cdpa_config


def test_nested_workspace_report_does_not_resolve_from_parent_git_root(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    nested = tmp_path / "nested" / "workspace"
    kwargs = _prompt_kwargs(config, tmp_path)
    kwargs.update(workspace=str(nested), expected_report_path=".plan/alpha/PLAN_turn1_task-1.md")
    prompt = PromptBuilder(config).build(**kwargs).text
    assert str(nested / ".plan/alpha/PLAN_turn1_task-1.md") in prompt
    assert "not its parent or Git's top-level directory" in prompt


def test_remote_windows_workspace_report_is_unambiguous_on_any_worker_host(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    kwargs = _prompt_kwargs(config, tmp_path)
    kwargs.update(workspace=r"E:\work\nested", expected_report_path=".plan/alpha/PLAN_turn1_task-1.md")
    prompt = PromptBuilder(config).build(**kwargs).text
    assert str(PureWindowsPath(r"E:\work\nested\.plan\alpha\PLAN_turn1_task-1.md")) in prompt


def test_report_repair_repeats_exact_workspace_destination(tmp_path):
    import asyncio
    from playwright_auto.cdpa_routes import expected_report_relative
    from playwright_auto.cdpa_worker import _active_hop
    from test_cdpa_worker import setup_task, FakeActions

    config, _store, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    path = expected_report_relative(repository_root=tmp_path, plans_root=config.plans_root,
        team=state["team"], physical_role=hop["physical_role"], turn=hop["turn"], task_id=state["task_id"])
    hop.update(kind="report_repair", locked_route="DONE", locked_handoff=path, validation_error="report missing")
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    absolute = str(tmp_path / path)
    assert hop["prompt"].count(absolute) == 1
    assert f"{path}/{path}" not in hop["prompt"]
    assert "CDPA_REPORT_REPAIR" in hop["prompt"]
    assert hop["prompt"].count(f"Write the complete role report to exactly: {path}") == 1
    assert "Authorized endpoint tools:" not in hop["prompt"]
    assert "@mcp-thinkbook" not in hop["prompt"]
