from __future__ import annotations

from pathlib import Path

import pytest

from playwright_auto.cdpa_config import CDPA_ROLES, DEFAULT_CONFIG_PATH, load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder


ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROMPTS = ROOT / "prompts" / "cdpa"
PACKAGED_PROMPTS = DEFAULT_CONFIG_PATH.parent / "prompts" / "cdpa"


def _generation_zero_prompt(config, *, workspace: Path, role: str) -> str:
    allowed_routes = tuple(dict.fromkeys(("PLAN", role, "PAUSE", "DONE")))
    built = PromptBuilder(config).build(
        task_title="prompt contract",
        task_id="task-prompt-contract",
        team="prompt-contract",
        logical_role=role,
        physical_role=f"prompt-contract-{role.lower()}",
        turn=1,
        allowed_routes=allowed_routes,
        workspace=str(workspace),
        source_physical_role=None,
        handoff="prompt contract handoff",
        goal="prompt contract goal",
        constructor_sent_generation=None,
        conversation_generation=0,
    )
    assert built.constructor_included is True
    route_contract = "|".join(allowed_routes)
    assert f'"route":"{route_contract}"' in built.text
    assert PromptBuilder(config).naming_rule() in built.text
    return built.text


@pytest.mark.parametrize("mode", ("local", "packaged"))
def test_generation_zero_workflow_prompts_build_from_selected_asset_tree(
    tmp_path: Path, mode: str
):
    if mode == "local":
        config = load_cdpa_config(None, repository_root=ROOT)
        assert config.config_path == (ROOT / "cdpa.yaml").resolve()
        assert config.response_guide_path == (LOCAL_PROMPTS / "RESPONSE_GUIDE.md").resolve()
        expected_prompts = LOCAL_PROMPTS
        workspace = ROOT
    else:
        workspace = tmp_path / "no-local-config"
        workspace.mkdir()
        config = load_cdpa_config(None, repository_root=workspace)
        assert config.config_path == DEFAULT_CONFIG_PATH.resolve()
        assert config.response_guide_path == (PACKAGED_PROMPTS / "RESPONSE_GUIDE.md").resolve()
        expected_prompts = PACKAGED_PROMPTS

    base_context = config.response_guide_path.with_name("BASE_CONTEXT.md")
    assert base_context.is_file()

    for role in CDPA_ROLES:
        assert config.constructor_paths[role] == (expected_prompts / f"{role}.md").resolve()
        text = _generation_zero_prompt(config, workspace=workspace, role=role)
        assert base_context.read_text(encoding="utf-8").strip() in text
        assert config.constructor_paths[role].read_text(encoding="utf-8").strip() in text


def test_agents_documents_goal_closure_and_explicit_bootstrap_selection():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Explicit task-level completion authority governs final closure" in agents
    assert "fixable in-scope" in agents
    assert "PLAN must route the smallest authorized follow-up role instead of `DONE`" in agents
    assert "role-scope completion" in agents
    assert "`mcp-g8` -> `g8-bootstrap`" in agents
    assert "`mcp-thinkbook` -> `thinkbook-bootstrap`" in agents
    assert "`mcp-docker` / a5 -> `a5docker-bootstrap`" in agents
    assert "primary execution MCP" in agents
    assert "CDPA host/browser" in agents
    assert "--bootstrap <id>" in agents
    assert "--fresh" in agents


@pytest.mark.parametrize("prompt_root", (LOCAL_PROMPTS, PACKAGED_PROMPTS))
def test_runtime_role_prompts_preserve_authorized_goal_closure_semantics(prompt_root: Path):
    plan = (prompt_root / "PLAN.md").read_text(encoding="utf-8")
    dev = (prompt_root / "DEV.md").read_text(encoding="utf-8")
    review = (prompt_root / "REVIEW.md").read_text(encoding="utf-8")
    test = (prompt_root / "TEST.md").read_text(encoding="utf-8")

    assert "Explicit task-level completion authority controls final closure" in plan
    assert "fixable in-scope" in plan
    assert "do not route `DONE`" in plan
    assert "smallest authorized follow-up role" in plan
    assert "external/manual prerequisite" in plan

    assert "DEV role-scope completion is not global task completion" in dev
    assert "fixable in-scope" in dev
    assert "`DONE` remains PLAN-only" in dev

    assert "REVIEW role-scope completion is not global task completion" in review
    assert "confirmed fixable implementation blocker to DEV" in review
    assert "clean work directly to PLAN" in review
    assert "`PAUSE`" in review and "external/manual prerequisite" in review

    assert "TEST role-scope completion is not global task completion" in test
    assert "fixable in-scope acceptance failure to DEV" in test
    assert "verified clean result to PLAN" in test
    assert "`PAUSE`" in test and "external/manual prerequisite" in test
