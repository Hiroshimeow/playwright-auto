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
    expected_report_path = (
        f".plan/prompt-contract/prompt-contract-{role.lower()}_turn1_task-prompt-contract.md"
    )
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
        expected_report_path=expected_report_path,
        constructor_sent_generation=None,
        conversation_generation=0,
    )
    assert built.constructor_included is False
    prompt_routes = tuple(
        item for item in allowed_routes if item != "PAUSE" and (item != "DONE" or role == "PLAN")
    )
    route_contract = "|".join(prompt_routes)
    assert f'"route":"{route_contract}"' in built.text
    assert f'"handoff":"{expected_report_path}"' in built.text
    assert f"Before replying, write a non-empty report to exactly: {expected_report_path}" in built.text
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
        assert base_context.read_text(encoding="utf-8").strip() not in text
        assert config.constructor_paths[role].read_text(encoding="utf-8").strip() not in text
        assert "Goal: prompt contract goal" in text
        assert "REPOSITORY_SKILLS" not in text


def test_agents_is_bootstrap_orientation_not_duplicate_rulebook():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "cdpa \"<complete task: repository/worktree, goal, constraints, acceptance evidence>\"" in agents
    assert ".agents/skills/hcu-work/SKILL.md" in agents
    assert "hcuw fingerprint ." in agents
    assert ".hcu/rules.md" in agents
    assert "one browser endpoint/account for its lifetime" in agents
    assert "Fresh" in agents
    assert "Bootstrap branching is temporarily disabled" in agents
    assert "must never scan or snapshot every ChatGPT tab" in agents
    assert len(agents) < 5000


@pytest.mark.parametrize("prompt_root", (LOCAL_PROMPTS, PACKAGED_PROMPTS))
def test_runtime_prompt_assets_use_concise_role_specific_contracts(prompt_root: Path):
    prompts = {
        role: (prompt_root / f"{role}.md").read_text(encoding="utf-8")
        for role in CDPA_ROLES
    }
    base = (prompt_root / "BASE_CONTEXT.md").read_text(encoding="utf-8")

    assert "scope and workflow decision owner" in prompts["PLAN"]
    assert "implementation owner" in prompts["DEV"]
    assert "independent correctness reviewer" in prompts["REVIEW"]
    assert "independent verification owner" in prompts["TEST"]
    assert "independent operational/provenance auditor" in prompts["AUDIT"]
    assert "required capability" in base
    assert "historical tool" in base.lower()
    assert "exact target" in base
    for text in prompts.values():
        assert "relevant skills" in text
    assert max(len(text.splitlines()) for text in prompts.values()) <= 7
    assert len(base.splitlines()) <= 14


@pytest.mark.parametrize("name", ("BASE_CONTEXT.md", "PLAN.md", "DEV.md", "REVIEW.md", "TEST.md", "AUDIT.md", "INDEPENDENT_RULE.md", "MONITOR.md", "RESPONSE_GUIDE.md"))
def test_local_and_packaged_workflow_prompt_mirror_match(name: str):
    assert (LOCAL_PROMPTS / name).read_bytes() == (PACKAGED_PROMPTS / name).read_bytes()


@pytest.mark.parametrize("prompt_root", (LOCAL_PROMPTS, PACKAGED_PROMPTS))
def test_runtime_role_prompts_keep_only_identity_skill_and_route_contract(prompt_root: Path):
    prompts = {
        role: (prompt_root / f"{role}.md").read_text(encoding="utf-8")
        for role in CDPA_ROLES
    }

    assert "only PLAN may route `DONE`" in prompts["PLAN"]
    assert "Route complete work to REVIEW" in prompts["DEV"]
    assert "Route actionable defects to DEV" in prompts["REVIEW"]
    assert "Route fixable failures to DEV" in prompts["TEST"]
    assert "Route confirmed source defects to DEV" in prompts["AUDIT"]
    assert all("relevant skills" in text for text in prompts.values())
    assert all("LEARNING.md" not in text for text in prompts.values())
    assert all(len(text.split()) <= 65 for text in prompts.values())
