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
