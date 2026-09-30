from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RuntimeConfig


class StateStoreError(RuntimeError):
    """Raised when runtime-v2 state cannot be read or written safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class RuntimeStateStore:
    """Single owner of `.runtime-v2` persistence."""

    config: RuntimeConfig

    def initialize(self) -> None:
        self._ensure_path_stays_under_state_root(self.config.state_root)
        try:
            self.config.state_root.mkdir(parents=True, exist_ok=True)
            self.task_records_dir().mkdir(parents=True, exist_ok=True)
            self.task_runner_dir().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateStoreError("state_write_failed", str(exc)) from exc

    def list_runs(self) -> list[dict[str, Any]]:
        path = self.config.runs_path
        self._ensure_path_stays_under_state_root(path)
        if not path.exists():
            return []
        raw = self._read_text_file(path)
        value = self._decode_json(raw, path)
        if not isinstance(value, list):
            raise StateStoreError(
                "state_json_invalid",
                "runs.json must contain a JSON array",
            )
        return [self._validate_run_record(item) for item in value]

    def save_task_record(self, record: dict[str, Any]) -> None:
        runtime_task_id = str(record.get("runtime_task_id") or "").strip()
        if not runtime_task_id:
            raise StateStoreError("state_write_failed", "runtime_task_id is required")
        path = self.task_records_dir() / f"{runtime_task_id}.json"
        self._write_json_file(path, record)

    def list_task_records(self) -> list[dict[str, Any]]:
        directory = self.task_records_dir()
        self._ensure_path_stays_under_state_root(directory)
        if not directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            value = self._decode_json(self._read_text_file(path), path)
            if isinstance(value, dict):
                records.append(dict(value))
        return records

    def save_task_runner_process(self, record: dict[str, Any]) -> None:
        self._write_json_file(self.task_runner_dir() / "legacy-runtime.json", record)

    def task_runner_log_path(self) -> Path:
        path = self.task_runner_dir() / "legacy-runtime.log"
        self._ensure_path_stays_under_state_root(path)
        return path

    def task_runner_dir(self) -> Path:
        path = self.config.state_root / "task-runner"
        self._ensure_path_stays_under_state_root(path)
        return path

    def task_records_dir(self) -> Path:
        path = self.config.state_root / "tasks"
        self._ensure_path_stays_under_state_root(path)
        return path

    def append_diagnostic(self, code: str, message: str) -> None:
        path = self.config.diagnostics_path
        self._ensure_path_stays_under_state_root(path)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "code": str(code),
            "message": str(message),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            raise StateStoreError("state_write_failed", str(exc)) from exc

    def recent_diagnostics(self, *, limit: int = 20) -> list[dict[str, Any]]:
        path = self.config.diagnostics_path
        self._ensure_path_stays_under_state_root(path)
        if not path.exists():
            return []
        raw = self._read_text_file(path)
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            value = self._decode_json(line, path)
            if isinstance(value, dict):
                records.append(value)
        return records[-limit:]

    def _write_json_file(self, path: Path, value: dict[str, Any]) -> None:
        self._ensure_path_stays_under_state_root(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            raise StateStoreError("state_write_failed", str(exc)) from exc

    def _ensure_path_stays_under_state_root(self, path: Path) -> None:
        root = self.config.state_root.resolve()
        candidate = path.resolve()
        if candidate == root:
            return
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise StateStoreError(
                "state_path_escape",
                "runtime-v2 state path escaped .runtime-v2",
            ) from exc

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateStoreError("state_read_failed", str(exc)) from exc

    def _decode_json(self, raw: str, path: Path) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateStoreError(
                "state_json_invalid",
                f"invalid JSON in {path.name}",
            ) from exc

    def _validate_run_record(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise StateStoreError(
                "state_json_invalid",
                "each run record must be a JSON object",
            )
        return dict(value)
