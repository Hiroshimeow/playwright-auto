from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

from .cdpa_independent import MAX_TRIGGER_LEARNING_BYTES, trigger_learning_path
from .cdpa_safety import sanitize_text
from .file_lock import exclusive_file_lock, fsync_parent_directory

_DISPOSITIONS = frozenset({"ADDED", "REVISED", "SUPERSEDED"})
_REQUEST_KEYS = frozenset({"disposition", "old_text", "new_text"})
_TRIGGER_REQUEST_KEYS = _REQUEST_KEYS | {"trigger"}
_TRIGGER_TITLES = {
    "learning_recovery.md": "# Recovery trigger learning",
    "learning_interval.md": "# Interval trigger learning",
    "learning_task_done.md": "# Task-done trigger learning",
    "learning_role_completed.md": "# Role-completed trigger learning",
    "learning_task_state.md": "# Task-state trigger learning",
    "learning_manual.md": "# Manual trigger learning",
}
_MAX_EDIT_BYTES = 8_000
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_TASK_ID = re.compile(r"\b(?:cdpa-(?:idem-)?|agent-)[a-z0-9][a-z0-9-]{7,}\b", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b")
_DATE_ONLY = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9:])/(?!/)[^\s`'\"<>]+")
_WINDOWS_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+)"
)
_TRANSIENT_ID = re.compile(
    r"\b(?:task|request|page|incident|team)(?:[_ -]?id)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_TRANSIENT_HYPHEN_ID = re.compile(
    r"\b(?:"
    r"task-(?:\d+|[a-f0-9]{8,})|"
    r"page-\d+|"
    r"request-[a-f0-9]{8,}|"
    r"incident-[a-f0-9]{6,}|"
    r"team-(?!(?:based|specific|level|wide|local|scoped)\b)[a-z0-9][a-z0-9-]{2,}"
    r")\b",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:cookie|session[_-]?cookie)\b\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_MARKDOWN_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+(?P<body>.+)$")


class LearningEditError(ValueError):
    pass


@dataclass(frozen=True)
class LearningEditEvidence:
    disposition: str
    path: str
    sha256: str
    size: int


def _repository_paths(repository: str | Path) -> tuple[Path, Path, Path]:
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise LearningEditError("repository root must be an existing directory")
    target = root / "LEARNING.md"
    if target.is_symlink() or not target.is_file() or target.parent.resolve() != root:
        raise LearningEditError("LEARNING.md must be a regular repository-root file")
    lock_root = root / ".plan"
    if lock_root.exists() and (lock_root.is_symlink() or not lock_root.is_dir()):
        raise LearningEditError("repository learning lock directory is invalid")
    lock_root.mkdir(parents=True, exist_ok=True)
    if lock_root.resolve().parent != root:
        raise LearningEditError("repository learning lock escapes the repository")
    lock = lock_root / "LEARNING.md.lock"
    if lock.is_symlink():
        raise LearningEditError("repository learning lock must not be a symlink")
    return root, target, lock


def _span_kind(label: str, value: str) -> str:
    lines = value.splitlines()
    if value.startswith("- "):
        if any(
            _MARKDOWN_LIST_ITEM.match(line.lstrip(" \t"))
            or line.lstrip(" \t").startswith("#")
            for line in lines[1:]
        ):
            raise LearningEditError(f"{label} must contain one top-level bullet")
        return "bullet"
    if value.startswith("## "):
        if any(
            re.match(r"^#{1,2}(?:[ \t]+|$)", line.lstrip(" \t"))
            for line in lines[1:]
        ):
            raise LearningEditError(f"{label} must contain one level-two section")
        return "section"
    raise LearningEditError(
        f"{label} must begin with one Markdown bullet or level-two section"
    )


def _validate_span(disposition: str, old_text: str, new_text: str) -> None:
    if disposition not in _DISPOSITIONS:
        raise LearningEditError(f"unsupported learning disposition {disposition!r}")
    kinds: list[str] = []
    for label, value in (("old_text", old_text), ("new_text", new_text)):
        if not isinstance(value, str) or not value:
            raise LearningEditError(f"{label} must be a non-empty string")
        if "\r" in value or "\0" in value or value != value.strip("\n"):
            raise LearningEditError(f"{label} must be bounded UTF-8 Markdown text")
        if len(value.encode("utf-8")) > _MAX_EDIT_BYTES:
            raise LearningEditError(f"{label} exceeds the bounded edit size")
        kinds.append(_span_kind(label, value))
    if kinds[0] != kinds[1]:
        raise LearningEditError("old_text and new_text must use the same Markdown span type")
    if old_text == new_text:
        raise LearningEditError("learning edit must change content")
    if disposition == "ADDED" and not new_text.startswith(old_text):
        raise LearningEditError("ADDED edits must preserve the exact anchor text")


def _validate_markdown(text: str) -> None:
    if "\r" in text or "\0" in text:
        raise LearningEditError("LEARNING.md must remain UTF-8 Markdown")
    lines = text.splitlines()
    if not lines or lines[0] != "# LEARNING.md":
        raise LearningEditError("LEARNING.md must retain its canonical title")
    if sum(line == "# LEARNING.md" for line in lines) != 1:
        raise LearningEditError("LEARNING.md must contain one canonical title")
    for line in lines:
        if line.startswith("#") and not re.fullmatch(r"#{1,6} .+", line):
            raise LearningEditError("LEARNING.md contains a malformed heading")


def _normalized_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    for line in text.splitlines():
        match = _MARKDOWN_LIST_ITEM.match(line.strip())
        if match:
            bullets.append(" ".join(match.group("body").split()).casefold())
    return bullets


def _validate_no_duplicate_lessons(text: str) -> None:
    bullets = [item for item in _normalized_bullets(text) if item]
    if len(bullets) != len(set(bullets)):
        raise LearningEditError("learning edit would create a duplicate lesson")


def _validate_sanitized_lesson(new_text: str) -> None:
    if sanitize_text(new_text) != new_text or any(
        pattern.search(new_text)
        for pattern in (
            _UUID,
            _TASK_ID,
            _TIMESTAMP,
            _DATE_ONLY,
            _POSIX_PATH,
            _WINDOWS_PATH,
            _TRANSIENT_ID,
            _TRANSIENT_HYPHEN_ID,
            _CREDENTIAL_ASSIGNMENT,
        )
    ):
        raise LearningEditError("learning edit contains sensitive or transient evidence")


def _exact_span_positions(text: str, target: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        index = text.find(target, start)
        if index < 0:
            return positions
        end = index + len(target)
        if (index == 0 or text[index - 1] == "\n") and (
            end == len(text) or text[end] == "\n"
        ):
            positions.append(index)
        start = index + 1


def update_learning(
    repository: str | Path,
    *,
    disposition: str,
    old_text: str,
    new_text: str,
) -> LearningEditEvidence:
    normalized_disposition = str(disposition).strip().upper()
    _validate_span(normalized_disposition, old_text, new_text)
    _validate_sanitized_lesson(new_text)
    root, target, lock = _repository_paths(repository)
    temporary: Path | None = None
    try:
        with exclusive_file_lock(lock):
            if target.is_symlink() or not target.is_file() or target.parent.resolve() != root:
                raise LearningEditError("LEARNING.md must be a regular repository-root file")
            try:
                current = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LearningEditError("LEARNING.md is not valid UTF-8") from exc
            positions = _exact_span_positions(current, old_text)
            if len(positions) != 1:
                raise LearningEditError(
                    "exact target must occur once as a complete Markdown span in current "
                    f"LEARNING.md; found {len(positions)}"
                )
            start = positions[0]
            prefix = current[:start]
            suffix = current[start + len(old_text) :]
            proposed = prefix + new_text + suffix
            _validate_markdown(proposed)
            _validate_no_duplicate_lessons(proposed)
            data = proposed.encode("utf-8")
            target_mode = target.stat().st_mode & 0o777
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=root,
                prefix=".LEARNING.md.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, target_mode)
            os.replace(temporary, target)
            temporary = None
            fsync_parent_directory(target)
            read_back = target.read_bytes()
            if read_back != data:
                raise LearningEditError("LEARNING.md read-back did not match the atomic write")
            verified = read_back.decode("utf-8")
            if not verified.startswith(prefix) or not verified.endswith(suffix):
                raise LearningEditError("unrelated LEARNING.md content changed during mutation")
            _validate_markdown(verified)
            _validate_no_duplicate_lessons(verified)
            return LearningEditEvidence(
                disposition=normalized_disposition,
                path="LEARNING.md",
                sha256=hashlib.sha256(read_back).hexdigest(),
                size=len(read_back),
            )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)



def _validate_trigger_markdown(text: str, *, title: str) -> None:
    if "\r" in text or "\0" in text:
        raise LearningEditError("trigger learning must remain UTF-8 Markdown")
    lines = text.splitlines()
    if not lines or lines[0] != title:
        raise LearningEditError(f"trigger learning must retain title {title!r}")
    if sum(line == title for line in lines) != 1:
        raise LearningEditError("trigger learning must contain one canonical title")
    for line in lines:
        if line.startswith("#") and not re.fullmatch(r"#{1,6} .+", line):
            raise LearningEditError("trigger learning contains a malformed heading")


def update_trigger_learning(
    repository: str | Path,
    *,
    trigger: str,
    disposition: str,
    old_text: str,
    new_text: str,
) -> LearningEditEvidence:
    normalized_disposition = str(disposition).strip().upper()
    if normalized_disposition not in _DISPOSITIONS:
        raise LearningEditError(
            f"unsupported learning disposition {normalized_disposition!r}"
        )
    _validate_sanitized_lesson(new_text)
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise LearningEditError("repository root must be an existing directory")
    try:
        target = trigger_learning_path(root, trigger)
    except ValueError as exc:
        raise LearningEditError(str(exc)) from exc
    learning_root = target.parent
    if learning_root.exists() and (learning_root.is_symlink() or not learning_root.is_dir()):
        raise LearningEditError("trigger learning directory is invalid")
    learning_root.mkdir(parents=True, exist_ok=True)
    if learning_root.resolve().parent != root:
        raise LearningEditError("trigger learning directory escapes repository")
    title = _TRIGGER_TITLES[target.name]
    lock_root = root / ".plan"
    lock_root.mkdir(parents=True, exist_ok=True)
    if lock_root.is_symlink() or lock_root.resolve().parent != root:
        raise LearningEditError("repository learning lock directory is invalid")
    lock = lock_root / f"{target.name}.lock"
    temporary: Path | None = None
    try:
        with exclusive_file_lock(lock):
            if target.exists():
                _validate_span(normalized_disposition, old_text, new_text)
                if target.is_symlink() or not target.is_file() or target.resolve().parent != learning_root.resolve():
                    raise LearningEditError("trigger learning file must be a contained regular file")
                try:
                    current = target.read_bytes().decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise LearningEditError("trigger learning is not valid UTF-8") from exc
                positions = _exact_span_positions(current, old_text)
                if len(positions) != 1:
                    raise LearningEditError(
                        "exact target must occur once as a complete Markdown span in current "
                        f"trigger learning; found {len(positions)}"
                    )
                start = positions[0]
                prefix = current[:start]
                suffix = current[start + len(old_text) :]
                proposed = prefix + new_text + suffix
                target_mode = target.stat().st_mode & 0o777
            else:
                if normalized_disposition != "ADDED":
                    raise LearningEditError("first trigger learning edit must be ADDED")
                if old_text != title or not new_text.startswith(title):
                    raise LearningEditError(
                        "first trigger learning edit must preserve the canonical title anchor"
                    )
                proposed = new_text + ("\n" if not new_text.endswith("\n") else "")
                prefix = ""
                suffix = ""
                target_mode = 0o644
            _validate_trigger_markdown(proposed, title=title)
            _validate_no_duplicate_lessons(proposed)
            data = proposed.encode("utf-8")
            if len(data) > MAX_TRIGGER_LEARNING_BYTES:
                raise LearningEditError("trigger learning exceeds bounded size")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=learning_root,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, target_mode)
            os.replace(temporary, target)
            temporary = None
            fsync_parent_directory(target)
            read_back = target.read_bytes()
            if read_back != data:
                raise LearningEditError("trigger learning read-back did not match atomic write")
            verified = read_back.decode("utf-8")
            _validate_trigger_markdown(verified, title=title)
            _validate_no_duplicate_lessons(verified)
            if prefix and (not verified.startswith(prefix) or not verified.endswith(suffix)):
                raise LearningEditError("unrelated trigger learning content changed")
            return LearningEditEvidence(
                disposition=normalized_disposition,
                path=target.relative_to(root).as_posix(),
                sha256=hashlib.sha256(read_back).hexdigest(),
                size=len(read_back),
            )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def _request(value: Any) -> tuple[str | None, str, str, str]:
    if not isinstance(value, dict) or set(value) not in {
        _REQUEST_KEYS,
        _TRIGGER_REQUEST_KEYS,
    }:
        raise LearningEditError(
            "request must contain disposition, old_text and new_text, with an "
            "optional trigger"
        )
    if not all(isinstance(value[key], str) for key in value):
        raise LearningEditError("all learning request fields must be strings")
    trigger = str(value.get("trigger") or "").strip().lower() or None
    return trigger, value["disposition"], value["old_text"], value["new_text"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply one conflict-safe repository LEARNING.md edit"
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    input_text: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    source = sys.stdin.read() if input_text is None else input_text
    try:
        request = json.loads(source)
        trigger, disposition, old_text, new_text = _request(request)
        if trigger is None:
            evidence = update_learning(
                args.repository,
                disposition=disposition,
                old_text=old_text,
                new_text=new_text,
            )
        else:
            evidence = update_trigger_learning(
                args.repository,
                trigger=trigger,
                disposition=disposition,
                old_text=old_text,
                new_text=new_text,
            )
    except (json.JSONDecodeError, LearningEditError, OSError) as exc:
        print(
            json.dumps(
                {"error": "learning_edit_rejected", "detail": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=errors,
        )
        return 2
    print(json.dumps(asdict(evidence), ensure_ascii=False, sort_keys=True), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
