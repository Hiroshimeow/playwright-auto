from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty

import pytest

from playwright_auto import cdpa_learning
from playwright_auto.cdpa_learning import LearningEditError, update_learning


BASELINE = """# LEARNING.md

Reusable lessons.

## Browser and tab ownership

- Reopen the saved conversation before resuming.

## Dashboard and operations

- Preserve scroll context during polling.
"""


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "LEARNING.md").write_text(BASELINE, encoding="utf-8")
    return root


def _paused_update(
    repository: str,
    ready,
    release,
    result,
) -> None:
    original_replace = cdpa_learning.os.replace

    def paused_replace(source, target) -> None:
        ready.put("before_replace")
        release.get(timeout=10)
        original_replace(source, target)

    cdpa_learning.os.replace = paused_replace
    try:
        evidence = update_learning(
            repository,
            disposition="REVISED",
            old_text="- Reopen the saved conversation before resuming.",
            new_text="- Reopen the exact saved conversation before resuming.",
        )
        result.put(("ok", evidence.disposition))
    except BaseException as exc:  # pragma: no cover - child-process diagnostics
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _normal_update(repository: str, result) -> None:
    try:
        evidence = update_learning(
            repository,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text="- Preserve text selection and scroll context during polling.",
        )
        result.put(("ok", evidence.disposition))
    except BaseException as exc:  # pragma: no cover - child-process diagnostics
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def test_exact_target_change_is_rejected_without_mutation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    changed = BASELINE.replace(
        "- Reopen the saved conversation before resuming.",
        "- Reopen only after accepted-send ownership is verified.",
    )
    learning.write_text(changed, encoding="utf-8")

    with pytest.raises(LearningEditError, match="exact target"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Reopen the saved conversation before resuming.",
            new_text="- Reopen the exact saved conversation before resuming.",
        )

    assert learning.read_text(encoding="utf-8") == changed






def test_bullet_edit_cannot_inject_another_lesson_or_section(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="one top-level bullet"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text=(
                "- Preserve text selection and scroll context during polling.\n\n"
                "## Injected unrelated section\n\n"
                "- Add an unrelated lesson."
            ),
        )

    assert learning.read_text(encoding="utf-8") == before
















def test_duplicate_or_sensitive_lesson_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="duplicate"):
        update_learning(
            root,
            disposition="ADDED",
            old_text="## Dashboard and operations",
            new_text=(
                "## Dashboard and operations\n\n"
                "- Preserve scroll context during polling."
            ),
        )
    assert learning.read_text(encoding="utf-8") == before

    with pytest.raises(LearningEditError, match="sensitive or transient"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text=(
                "- Read token=secret-value from /home/user/repo for "
                "cdpa-idem-deadbeef."
            ),
        )
    assert learning.read_text(encoding="utf-8") == before












def test_cli_applies_one_exact_edit_and_returns_json(tmp_path: Path, capsys) -> None:
    root = _repository(tmp_path)
    request = {
        "disposition": "REVISED",
        "old_text": "- Preserve scroll context during polling.",
        "new_text": "- Preserve text selection and scroll context during polling.",
    }

    exit_code = cdpa_learning.main(
        ["--repository", str(root)],
        input_text=json.dumps(request),
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["disposition"] == "REVISED"
    assert output["path"] == "LEARNING.md"
    assert output["size"] == (root / "LEARNING.md").stat().st_size
    assert len(output["sha256"]) == 64


def test_concurrent_guarded_edits_preserve_both_changes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    release = context.Queue()
    result = context.Queue()
    first = context.Process(
        target=_paused_update,
        args=(str(root), ready, release, result),
    )
    second = context.Process(target=_normal_update, args=(str(root), result))

    first.start()
    assert ready.get(timeout=10) == "before_replace"
    second.start()
    release.put("continue")
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    outcomes = sorted(result.get(timeout=10) for _ in range(2))
    assert outcomes == [("ok", "REVISED"), ("ok", "REVISED")]
    learning = (root / "LEARNING.md").read_text(encoding="utf-8")
    assert "- Reopen the exact saved conversation before resuming." in learning
    assert "- Preserve text selection and scroll context during polling." in learning


def test_oversized_lesson_is_rejected_without_mutation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="bounded edit size"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text="- " + ("x" * 8_100),
        )

    assert learning.read_text(encoding="utf-8") == before


def test_final_plan_contract_requires_one_observable_guarded_learning_pass() -> None:
    prompt_paths = (
        Path("prompts/cdpa/PLAN.md"),
        Path("src/playwright_auto/cdpa_defaults/prompts/cdpa/PLAN.md"),
    )
    for prompt_path in prompt_paths:
        prompt = prompt_path.read_text(encoding="utf-8")
        assert "newest relevant completion evidence" in prompt
        assert "current `LEARNING.md`" in prompt
        assert "`ADDED`, `REVISED`, or `NONE`" in prompt
        assert "0-3" in prompt
        assert "semantic duplicate" in prompt
        assert "## Learning" in prompt
        assert "read back" in prompt
        assert "post-DONE model pass" in prompt

    agents = Path("AGENTS.md").read_text(encoding="utf-8")
    assert "newest relevant completion evidence" in agents
    assert "`ADDED`, `REVISED`, or `NONE`" in agents
    assert "## Learning" in agents
    assert "post-DONE model pass" in agents
