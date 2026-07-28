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


def test_whitespace_only_target_mismatch_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    indented = BASELINE.replace(
        "- Reopen the saved conversation before resuming.",
        "  - Reopen the saved conversation before resuming.",
    )
    learning.write_text(indented, encoding="utf-8")

    with pytest.raises(LearningEditError, match="exact target"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Reopen the saved conversation before resuming.",
            new_text="- Reopen the exact saved conversation before resuming.",
        )

    assert learning.read_text(encoding="utf-8") == indented


def test_overlapping_compliant_writers_preserve_both_edits(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Queue()
    first_result = context.Queue()
    second_result = context.Queue()
    first = context.Process(
        target=_paused_update,
        args=(str(root), ready, release, first_result),
    )
    second = context.Process(
        target=_normal_update,
        args=(str(root), second_result),
    )

    first.start()
    try:
        assert ready.get(timeout=10) == "before_replace"
        second.start()
        with pytest.raises(Empty):
            second_result.get(timeout=0.2)
        release.put("continue")
        first.join(timeout=10)
        second.join(timeout=10)
    finally:
        if first.is_alive():
            release.put("continue")
            first.terminate()
            first.join(timeout=5)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert first_result.get(timeout=2) == ("ok", "REVISED")
    assert second_result.get(timeout=2) == ("ok", "REVISED")
    final = (root / "LEARNING.md").read_text(encoding="utf-8")
    assert "- Reopen the exact saved conversation before resuming." in final
    assert "- Preserve text selection and scroll context during polling." in final


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


@pytest.mark.parametrize("marker", ("*", "+", "1.", "1)"))
def test_bullet_edit_rejects_alternate_top_level_bullet_markers(
    tmp_path: Path,
    marker: str,
) -> None:
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
                f"{marker} Add an unrelated lesson."
            ),
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "prefix,marker",
    (
        (" ", "-"),
        ("   ", "*"),
        ("\t", "+"),
        ("  ", "1."),
        ("\t", "1)"),
    ),
)
def test_bullet_edit_rejects_indented_list_items(
    tmp_path: Path,
    prefix: str,
    marker: str,
) -> None:
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
                f"{prefix}{marker} Add an unrelated lesson."
            ),
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("marker", ("-", "*", "+", "1.", "1)"))
def test_bullet_edit_rejects_tab_separated_list_items(
    tmp_path: Path,
    marker: str,
) -> None:
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
                f"{marker}\tAdd an unrelated lesson."
            ),
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("marker", ("*", "+", "1.", "1)"))
def test_alternate_bullet_markers_cannot_bypass_duplicate_detection(
    tmp_path: Path,
    marker: str,
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
                f"{marker} Preserve scroll context during polling."
            ),
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "old_text,new_text",
    (
        (
            "## Browser and tab ownership",
            "## Browser and tab ownership\n\n## Injected unrelated section",
        ),
        (
            "## Browser and tab ownership",
            "## Browser and tab ownership\n\n# Injected document title",
        ),
    ),
)
def test_section_edit_cannot_contain_another_top_level_section(
    tmp_path: Path,
    old_text: str,
    new_text: str,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="one level-two section"):
        update_learning(
            root,
            disposition="ADDED",
            old_text=old_text,
            new_text=new_text,
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("prefix", (" ", "  ", "   "))
@pytest.mark.parametrize("heading", ("# Injected title", "## Injected section"))
@pytest.mark.parametrize("mode", ("bullet", "section"))
def test_indented_top_level_heading_is_rejected_in_bounded_span(
    tmp_path: Path,
    prefix: str,
    heading: str,
    mode: str,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")
    if mode == "bullet":
        old_text = "- Preserve scroll context during polling."
        new_text = (
            "- Preserve text selection and scroll context during polling.\n\n"
            f"{prefix}{heading}"
        )
        error = "one top-level bullet"
    else:
        old_text = "## Dashboard and operations\n\n- Preserve scroll context during polling."
        new_text = (
            "## Dashboard and operations\n\n"
            "- Preserve text selection and scroll context during polling.\n\n"
            f"{prefix}{heading}"
        )
        error = "one level-two section"

    with pytest.raises(LearningEditError, match=error):
        update_learning(
            root,
            disposition="REVISED",
            old_text=old_text,
            new_text=new_text,
        )

    assert learning.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("prefix", (" ", "   ", "\t"))
@pytest.mark.parametrize("marker", ("#", "##"))
@pytest.mark.parametrize("suffix", ("\tInjected section", ""))
def test_section_edit_rejects_indented_heading_horizontal_variants(
    tmp_path: Path,
    prefix: str,
    marker: str,
    suffix: str,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="one level-two section"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="## Dashboard and operations\n\n- Preserve scroll context during polling.",
            new_text=(
                "## Dashboard and operations\n\n"
                "- Preserve text selection and scroll context during polling.\n\n"
                f"{prefix}{marker}{suffix}"
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


@pytest.mark.parametrize(
    "lesson",
    (
        "- Use `/home/user/repo` for recovery.",
        "- Run /usr/local/bin/tool before recovery.",
        "- Open `C:\\Users\\agent\\repo` before recovery.",
        r"- Open \\server\share\repo before recovery.",
        "- Reopen page-123 before resuming.",
        "- Recheck request-abcdef1234 before resuming.",
        "- Compare incident-deadbeef before resuming.",
        "- Preserve team-alpha ownership before resuming.",
        "- Retry only after 2026-07-27.",
    ),
)
def test_raw_path_transient_id_and_date_forms_are_rejected(
    tmp_path: Path,
    lesson: str,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="sensitive or transient"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text=lesson,
        )

    assert learning.read_text(encoding="utf-8") == before


def test_ordinary_page_and_team_words_remain_allowed(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    evidence = update_learning(
        root,
        disposition="REVISED",
        old_text="- Preserve scroll context during polling.",
        new_text="- Preserve page context while the team reviews polling updates.",
    )

    assert evidence.disposition == "REVISED"


@pytest.mark.parametrize(
    "lesson",
    (
        "- Reject task-specific chronology in reusable lessons.",
        "- Prefer team-based ownership validation.",
        "- Repeat the page-level readiness check before mutation.",
    ),
)
def test_descriptive_hyphenated_compounds_remain_allowed(
    tmp_path: Path,
    lesson: str,
) -> None:
    root = _repository(tmp_path)

    evidence = update_learning(
        root,
        disposition="REVISED",
        old_text="- Preserve scroll context during polling.",
        new_text=lesson,
    )

    assert evidence.disposition == "REVISED"


@pytest.mark.parametrize(
    "lesson",
    (
        "- Reuse cookie=session-secret during recovery.",
        "- Reuse session_cookie=abcdef123456 during recovery.",
    ),
)
def test_cookie_and_session_assignments_are_rejected_without_mutation(
    tmp_path: Path,
    lesson: str,
) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    before = learning.read_text(encoding="utf-8")

    with pytest.raises(LearningEditError, match="sensitive or transient"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text=lesson,
        )

    assert learning.read_text(encoding="utf-8") == before


def test_atomic_edit_preserves_learning_file_mode(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    learning = root / "LEARNING.md"
    learning.chmod(0o644)

    update_learning(
        root,
        disposition="REVISED",
        old_text="- Preserve scroll context during polling.",
        new_text="- Preserve text selection and scroll context during polling.",
    )

    assert learning.stat().st_mode & 0o777 == 0o644


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


def test_learning_target_must_be_repository_root_file(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "LEARNING.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_text(BASELINE, encoding="utf-8")
    os.symlink(outside, root / "LEARNING.md")

    with pytest.raises(LearningEditError, match="regular repository-root file"):
        update_learning(
            root,
            disposition="REVISED",
            old_text="- Preserve scroll context during polling.",
            new_text="- Preserve text selection and scroll context during polling.",
        )
