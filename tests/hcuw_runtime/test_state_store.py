from __future__ import annotations

import json
from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.state import StateStoreError, RuntimeStateStore


def test_missing_runs_returns_empty_without_creating_file(tmp_path: Path) -> None:
    store = RuntimeStateStore(load_runtime_config(repository_root=tmp_path))

    assert store.list_runs() == []
    assert not store.config.runs_path.exists()


def test_malformed_runs_returns_typed_error_and_preserves_file(tmp_path: Path) -> None:
    store = RuntimeStateStore(load_runtime_config(repository_root=tmp_path))
    store.initialize()
    store.config.runs_path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(StateStoreError) as error:
        store.list_runs()

    assert error.value.reason == "state_json_invalid"
    assert store.config.runs_path.read_text(encoding="utf-8") == "{bad json"


def test_reads_and_writes_stay_under_runtime_v2(tmp_path: Path) -> None:
    store = RuntimeStateStore(load_runtime_config(repository_root=tmp_path))

    store.initialize()
    store.append_diagnostic("unit_test", "safe diagnostic")

    assert store.config.state_root.exists()
    assert store.config.diagnostics_path.exists()
    assert not (tmp_path / ".runtime").exists()

    first_line = store.config.diagnostics_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    assert payload["code"] == "unit_test"
    assert payload["message"] == "safe diagnostic"


def test_runs_file_must_contain_list(tmp_path: Path) -> None:
    store = RuntimeStateStore(load_runtime_config(repository_root=tmp_path))
    store.initialize()
    store.config.runs_path.write_text('{"items": []}', encoding="utf-8")

    with pytest.raises(StateStoreError) as error:
        store.list_runs()

    assert error.value.reason == "state_json_invalid"
