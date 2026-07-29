from pathlib import Path

from playwright_auto.cdpa_independent import (
    BUILTIN_MAINTAINERS_PROMPT,
    BUILTIN_MONITOR_PROMPT,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT_ROOTS = (
    ROOT / "prompts" / "cdpa",
    ROOT / "src" / "playwright_auto" / "cdpa_defaults" / "prompts" / "cdpa",
)
LEARNING_COMMAND = "uv run python -m playwright_auto.cdpa_learning"


def prompt(name: str) -> str:
    runtime = (PROMPT_ROOTS[0] / name).read_text(encoding="utf-8")
    packaged = (PROMPT_ROOTS[1] / name).read_text(encoding="utf-8")
    assert packaged == runtime
    return runtime


def test_maintainers_prompt_uses_direct_shared_independent_controls():
    text = prompt("MAINTAINERS.md")
    assert BUILTIN_MAINTAINERS_PROMPT == text.rstrip("\n")

    for required in (
        "normal one-agent CDPA task",
        "independent_task_control",
        "independent_create_repair",
        "independent_continue",
        "independent_complete",
        "no more than five",
        "HOLD_FOR_REPAIR",
        "CONTINUE_IN_PARALLEL",
        "accepted-send receipt",
        "operator Pause, Stop, Clear Team, Restart role, or New Chat",
        "SUCCESS",
        "NO_ACTION",
        "REPAIR_REQUIRED",
        "OPERATOR_REQUIRED",
    ):
        assert required in text

    for retired in (
        '"version":2',
        "CREATE_REPAIR_TASK",
        "REPLACE_TASK",
        "worker alone validates and applies the proposal",
        "maintenance decision JSON v1",
    ):
        assert retired not in text

    assert "Never emit route JSON, maintenance decision JSON, recovery arrays" in text
    assert "single-operator local runtime" in text
    assert "trusted-local metadata visibility" in text
    assert "single-operator local runtime" in BUILTIN_MAINTAINERS_PROMPT
    assert "generic privacy/security" in BUILTIN_MAINTAINERS_PROMPT
    assert "independent_task_control" in BUILTIN_MAINTAINERS_PROMPT
    assert "independent_create_repair" in BUILTIN_MAINTAINERS_PROMPT
    assert "route/action JSON" in BUILTIN_MAINTAINERS_PROMPT





def test_maintainers_prompt_requires_bounded_evidence_backed_learning():
    text = prompt("MAINTAINERS.md")

    for required in (
        "one bounded post-incident learning pass",
        "only after the operational outcome is verified",
        "Facts, Inference, and Proposed reusable rule",
        "same root cause in retained evidence",
        "deterministic invariant or regression",
        "SKIPPED — insufficient reusable evidence",
        "prefer revising the matching lesson over adding a duplicate",
        "REVISED",
        "SUPERSEDED",
        "not an incident log",
        "secrets, credentials, raw paths, transient IDs, or timestamps",
        "read repository-root `LEARNING.md` immediately before mutation",
        "bounded exact-content section or bullet edit",
        "reject stale or conflicting target content",
        "preserve unrelated concurrent edits",
        "read back and validate UTF-8, Markdown structure, repository containment",
        "Repair creation and learning are separate decisions",
    ):
        assert required in text

    assert text.count("one bounded post-incident learning pass") == 1
    assert "Improver" not in text
    assert "second memory store" not in text
    assert "approval engine" not in text
    assert "special scheduler" not in text
