from __future__ import annotations

from pathlib import Path

import pytest

from playwright_auto.cdpa_config import CDPA_ROLES, DEFAULT_CONFIG_PATH, load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder


ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROMPTS = ROOT / "prompts" / "cdpa"
PACKAGED_PROMPTS = DEFAULT_CONFIG_PATH.parent / "prompts" / "cdpa"


def _generation_zero_prompt(config, *, workspace: Path, role: str) -> str:
    # This is an already-filtered RouteContract; an ordinary role cannot
    # self-route, and only the configured closing role can use DONE.
    allowed_routes = (
        ("DEV", "PAUSE", "DONE") if role == "PLAN" else ("PLAN", "PAUSE")
    )
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
        closing_role="PLAN",
        workspace=str(workspace),
        source_physical_role=None,
        handoff="prompt contract handoff",
        goal="prompt contract goal",
        expected_report_path=expected_report_path,
        constructor_sent_generation=None,
        conversation_generation=0,
    )
    assert built.constructor_included is False
    route_contract = "|".join(allowed_routes)
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

    assert "team leader and decision owner" in prompts["PLAN"]
    assert "implementation owner" in prompts["DEV"]
    assert "independent correctness reviewer" in prompts["REVIEW"]
    assert "independent verification owner" in prompts["TEST"]
    assert "independent operational/provenance auditor" in prompts["AUDIT"]
    assert "required capability" in base
    assert "historical tool" in base.lower()
    assert "exact target" in base
    for text in prompts.values():
        assert "skills" in text.lower()
        assert "RouteContract" in text
    # Role-specific instructions are allowed to be descriptive, not truncated.
    assert max(len(text.splitlines()) for text in prompts.values()) <= 16
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

    assert "Use DONE only as the authorized closing_role" in prompts["PLAN"]
    assert "Route completed work to REVIEW" in prompts["DEV"]
    assert "blocking defects to DEV" in prompts["REVIEW"]
    assert "implementation failures to DEV" in prompts["TEST"]
    assert "implementation defects to DEV" in prompts["AUDIT"]
    assert all("skills" in text.lower() for text in prompts.values())
    assert all("LEARNING.md" not in text for text in prompts.values())
    assert all(len(text.split()) <= 280 for text in prompts.values())

@pytest.mark.parametrize("mode", ("local", "packaged"))
def test_effective_workflow_role_contract_and_frozen_snapshot(tmp_path: Path, mode: str):
    from dataclasses import replace
    from playwright_auto.cdpa_workflow_agents import (
        WorkflowAgentCatalog, semantic_routes_from_workflow_definitions,
        task_workflow_definitions, workflow_snapshot,
    )

    workspace = ROOT if mode == "local" else tmp_path
    config = load_cdpa_config(None, repository_root=workspace)
    config = replace(config, plans_root=tmp_path / "isolated-plans")
    catalog = WorkflowAgentCatalog(config)
    definitions = catalog.resolve_for_task(config.roles)
    snapshot = workflow_snapshot(list(definitions.values()))
    state = {"roles": dict.fromkeys(snapshot), "workflow_agents": snapshot}
    resolved = task_workflow_definitions(state, config)
    assert resolved == snapshot
    expected_routes = {
        "PLAN": {"DEV", "TEST", "REVIEW", "AUDIT"},
        "DEV": {"REVIEW", "TEST", "PLAN"},
        "TEST": {"DEV", "PLAN"},
        "REVIEW": {"DEV", "TEST", "PLAN"},
        "AUDIT": {"DEV", "TEST", "PLAN"},
    }
    routes = semantic_routes_from_workflow_definitions(resolved)
    assert {role: set(edges.values()) for role, edges in routes.items()} == expected_routes
    for role, definition in resolved.items():
        prompt = definition["system_prompt"]
        assert prompt == config.constructor_paths[role].read_text(encoding="utf-8").strip()
        assert "specific external or manual prerequisite" in prompt
        assert "checked alternatives" in prompt
        assert "resume condition" in prompt
        assert "task/workspace identity, all required acceptance, and artifacts are verified" in prompt
        assert "no known in-scope failure remaining" in prompt
        assert "new evidence, a changed approach, or a concrete corrective action" in prompt
        assert "COMPLETE/FIX" not in prompt
        assert "PRESERVE" not in prompt
        if role != "PLAN":
            assert "to PLAN when allowed" in prompt
            assert "otherwise route to an authorized role" in prompt
            assert "no meaningful authorized action or handoff remains" in prompt
        built = PromptBuilder(config).build(
            task_title="contract", task_id="task-contract", team="contract",
            logical_role=role, physical_role=role.lower(), turn=1,
            allowed_routes=tuple(expected_routes[role]) + ("PAUSE",) + (("DONE",) if role == "PLAN" else ()),
            closing_role="PLAN", workspace=str(workspace),
            source_physical_role=None, handoff="Inspect the assigned boundary.",
            goal="Verify the role contract.", constructor_text=prompt,
            expected_report_path=".plan/contract/report.md",
        )
        assert prompt in built.text
        assert built.constructor_included

    catalog.update("PLAN", display_name="PLAN", system_prompt="New catalog prompt")
    assert catalog.get("PLAN")["system_prompt"] == "New catalog prompt"
    assert task_workflow_definitions(state, config)["PLAN"]["system_prompt"] == snapshot["PLAN"]["system_prompt"]


def test_workflow_without_plan_keeps_review_as_closer(tmp_path: Path):
    from dataclasses import replace
    from playwright_auto.cdpa_workflow_agents import (
        WorkflowAgentCatalog, semantic_routes_from_workflow_definitions,
        workflow_closing_role,
    )

    config = replace(load_cdpa_config(None, repository_root=ROOT), plans_root=tmp_path)
    definitions = WorkflowAgentCatalog(config).resolve_for_task(("DEV", "REVIEW"))
    assert workflow_closing_role(definitions) == "REVIEW"
    routes = semantic_routes_from_workflow_definitions(definitions)
    assert set(routes["DEV"].values()) == {"REVIEW"}
    assert set(routes["REVIEW"].values()) == {"DEV"}
    assert "otherwise route to an authorized role" in definitions["REVIEW"]["system_prompt"]
