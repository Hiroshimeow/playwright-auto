from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROMPT = ROOT / "prompts" / "cdpa" / "MAINTAINERS.md"
PACKAGED_PROMPT = (
    ROOT
    / "src"
    / "playwright_auto"
    / "cdpa_defaults"
    / "prompts"
    / "cdpa"
    / "MAINTAINERS.md"
)


def test_maintainers_prompt_states_recovery_boundary_and_lesson_rule():
    prompt = RUNTIME_PROMPT.read_text(encoding="utf-8")

    assert PACKAGED_PROMPT.read_text(encoding="utf-8") == prompt
    assert "recover, diagnose, prevent recurrence, and delegate repairs" in prompt
    assert "smallest safe action" in prompt
    assert "worker alone validates and applies" in prompt
    assert "without waiting for user approval" in prompt
    assert "verified role-offline true list" in prompt
    assert "call the affected team through ROUTE_PLAN" in prompt
    assert "PLAN selects DEV, TEST, REVIEW, or AUDIT" in prompt
    assert "worker automatically appends" in prompt
    assert "not already present in CURRENT LEARNING.md" in prompt
    assert "Use `null` only when no new reusable lesson exists" in prompt
