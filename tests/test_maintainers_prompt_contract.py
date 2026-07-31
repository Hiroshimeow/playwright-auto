from pathlib import Path

from playwright_auto.cdpa_independent import BUILTIN_MAINTAINERS_PROMPT


ROOT = Path(__file__).resolve().parents[1]
PROMPT_ROOTS = (
    ROOT / "prompts" / "cdpa",
    ROOT / "src" / "playwright_auto" / "cdpa_defaults" / "prompts" / "cdpa",
)


def prompt(name: str) -> str:
    runtime = (PROMPT_ROOTS[0] / name).read_text(encoding="utf-8")
    packaged = (PROMPT_ROOTS[1] / name).read_text(encoding="utf-8")
    assert packaged == runtime
    return runtime.rstrip("\n")


def test_maintainers_prompt_uses_recovery_operating_contract():
    text = prompt("MAINTAINERS.md")
    assert BUILTIN_MAINTAINERS_PROMPT == text
    for required in (
        "built-in Recovery independent agent",
        "independent_task_control",
        "independent_create_repair",
        "independent_continue",
        "independent_complete",
        "PLAN/DEV/REVIEW repair task",
        "repository-root `PROBLEM.md`",
        ".learning/learning_recovery.md",
        "Reset is the independent-agent force-release control",
        "Stop is not an independent-agent lifecycle state",
        "never resend across an accepted-send boundary",
        "shared by trigger type, not by agent identity",
        "`trigger`, `disposition`, `old_text`, and `new_text`",
    ):
        assert required in text
    for retired in (
        "no more than five",
        "repository-root `LEARNING.md`",
        "deterministic successor",
        "maintenance decision JSON",
        "recovery arrays",
    ):
        assert retired not in text
