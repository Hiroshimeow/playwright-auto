from __future__ import annotations

from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import (
    MAX_REPOSITORY_SKILLS,
    PromptBuilder,
    discover_repository_skills,
)


def _write_skill(root: Path, name: str, description: str, *, body: str = "SECRET BODY") -> Path:
    path = root / ".skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _normal_prompt(builder: PromptBuilder, workspace: Path, goal: str) -> str:
    return builder.build(
        task_title="repo skills",
        task_id="task-repo-skills",
        team="repo-skills",
        logical_role="DEV",
        physical_role="repo-skills-dev",
        turn=1,
        allowed_routes=("PLAN", "DEV", "REVIEW", "PAUSE"),
        workspace=str(workspace),
        source_physical_role=None,
        handoff=".plan/input.md",
        goal=goal,
        expected_report_path=".plan/output.md",
        constructor_sent_generation=0,
        conversation_generation=0,
    ).text


def _event() -> dict:
    return {
        "event_key": "manual:repo-skills",
        "trigger_type": "manual",
        "occurred_at": "2026-09-22T00:00:00+00:00",
    }


def test_discovers_direct_repository_skills_without_loading_body(tmp_path: Path):
    _write_skill(tmp_path, "alpha-skill", "Use when alpha applies.", body="DO NOT INLINE THIS BODY")
    nested = tmp_path / ".skills" / "container" / "nested" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("---\nname: nested\ndescription: hidden\n---\n", encoding="utf-8")
    (tmp_path / ".skills" / "README.md").write_text("ignore", encoding="utf-8")

    skills = discover_repository_skills(str(tmp_path))

    assert [(item.name, item.description) for item in skills] == [
        ("alpha-skill", "Use when alpha applies.")
    ]


def test_missing_and_malformed_skill_catalog_is_fail_safe_and_bounded(tmp_path: Path):
    assert discover_repository_skills(str(tmp_path)) == ()

    _write_skill(tmp_path, "good-skill", "Good description.")
    bad = tmp_path / ".skills" / "bad-skill" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\nname: bad-skill\nno-description: nope\n---\n", encoding="utf-8")
    huge = tmp_path / ".skills" / "huge-skill" / "SKILL.md"
    huge.parent.mkdir(parents=True)
    huge.write_text("---\nname: huge-skill\ndescription: " + ("x" * 10000) + "\n---\n", encoding="utf-8")
    mismatch = tmp_path / ".skills" / "folder-name" / "SKILL.md"
    mismatch.parent.mkdir(parents=True)
    mismatch.write_text("---\nname: other-name\ndescription: mismatch\n---\n", encoding="utf-8")

    for index in range(MAX_REPOSITORY_SKILLS + 5):
        _write_skill(tmp_path, f"z-skill-{index:02d}", f"skill {index}")

    skills = discover_repository_skills(str(tmp_path))

    assert len(skills) <= MAX_REPOSITORY_SKILLS
    assert any(item.name == "good-skill" for item in skills)
    assert all(item.name not in {"bad-skill", "huge-skill", "other-name"} for item in skills)
    assert all(item.name != "z-skill-08" for item in skills)
    assert [item.name for item in skills] == sorted(item.name for item in skills)


def test_catalog_budget_keeps_selected_instructions_complete(tmp_path: Path):
    names = []
    for index in range(MAX_REPOSITORY_SKILLS):
        name = f"s{index:02d}-" + ("a" * 60)
        names.append(name[:64].rstrip("-"))
        _write_skill(
            tmp_path,
            names[-1],
            "d" * 220,
        )

    from playwright_auto.cdpa_prompts import _repository_skills_section

    section = _repository_skills_section(str(tmp_path), " ".join(names))

    assert len(section) <= 4096
    lines = section.splitlines()
    assert section == "\n".join(lines)
    for name in names:
        required = f"REQUIRED_REPOSITORY_SKILL: Load .skills/{name}/SKILL.md before substantive work."
        assert required in lines
    assert all(
        not line.startswith("REQUIRED_REPOSITORY_SKILL:") or line.endswith("before substantive work.")
        for line in lines
    )


def test_normal_prompt_does_not_serialize_repository_skill_catalog(tmp_path: Path):
    _write_skill(tmp_path, "cdpa-code-reducer", "Use when reducing CDPA implementation complexity.", body="SECRET REDUCTION BODY")
    builder = PromptBuilder(load_cdpa_config(None, repository_root=tmp_path))

    text = _normal_prompt(builder, tmp_path, "Apply cdpa-code-reducer to this implementation.")

    assert "REPOSITORY_SKILLS" not in text
    assert "- cdpa-code-reducer: Use when reducing CDPA implementation complexity." not in text
    assert "Load .skills/cdpa-code-reducer/SKILL.md before substantive work." not in text
    assert "SECRET REDUCTION BODY" not in text
    assert "Follow repository AGENTS/HCUW when present" in text


def test_independent_and_solo_prompts_share_repository_skill_catalog(tmp_path: Path):
    _write_skill(tmp_path, "cdpa-code-reducer", "Use when reducing CDPA implementation complexity.")
    builder = PromptBuilder(load_cdpa_config(None, repository_root=tmp_path))

    independent = builder.build_independent(
        agent_name="Reducer",
        system_prompt="Use cdpa-code-reducer for this job.",
        task_id="agent-reducer",
        team="agent-reducer",
        physical_role="agent-reducer-agent",
        workspace=str(tmp_path),
        event=_event(),
        cycle=1,
        max_cycles=1,
        constructor_sent_generation=0,
        conversation_generation=0,
    )
    solo = builder.build_solo(
        task_name="Solo reducer",
        logical_role="DEV",
        goal="Use cdpa-code-reducer and simplify.",
        workspace=str(tmp_path),
        continuation=False,
    )

    for text in (independent.text, solo.text):
        assert "REPOSITORY_SKILLS" in text
        assert "cdpa-code-reducer" in text
        assert "Load .skills/cdpa-code-reducer/SKILL.md before substantive work." in text
