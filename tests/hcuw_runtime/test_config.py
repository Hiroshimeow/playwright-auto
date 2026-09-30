from __future__ import annotations

from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.config import ConfigError, load_runtime_config


def test_default_config_uses_runtime_v2_ports_and_state_root(tmp_path: Path) -> None:
    config = load_runtime_config(repository_root=tmp_path)

    assert config.api_port == 9227
    assert config.dashboard_port == 9226
    assert config.legacy_api_port == 9225
    assert config.state_root == tmp_path / ".runtime-v2"
    assert config.runs_path == tmp_path / ".runtime-v2" / "runs.json"
    assert config.diagnostics_path == tmp_path / ".runtime-v2" / "diagnostics.jsonl"


def test_config_rejects_legacy_api_port_reuse(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_runtime_config(repository_root=tmp_path, api_port=9225)

    assert error.value.reason == "legacy_port_conflict"


def test_config_payload_is_json_safe_and_explicit(tmp_path: Path) -> None:
    config = load_runtime_config(repository_root=tmp_path)
    payload = config.to_config_payload()

    assert payload["api"] == {"host": "127.0.0.1", "port": 9227}
    assert payload["dashboard"]["port"] == 9226
    assert payload["dashboard"]["api_proxy_prefix"] == "/api"
    assert payload["legacy"] == {"api_port": 9225, "writable_state_shared": False}
    assert payload["state"]["root"].endswith(".runtime-v2")
    assert payload["limits"]["dashboard_min_refresh_seconds"] == 10


def test_health_payload_contains_legacy_isolation(tmp_path: Path) -> None:
    config = load_runtime_config(repository_root=tmp_path)
    payload = config.to_health_payload(
        started_at="2026-09-30T00:00:00+09:00",
        branch="refactor/hcuw-runtime-v2",
        head="abc123",
    )

    assert payload["service"] == "hcuw-runtime-v2-api"
    assert payload["status"] == "ok"
    assert payload["port"] == 9227
    assert payload["dashboard_port"] == 9226
    assert payload["legacy_api_port"] == 9225
    assert payload["legacy_isolation"] is True
    assert payload["branch"] == "refactor/hcuw-runtime-v2"
    assert payload["head"] == "abc123"
