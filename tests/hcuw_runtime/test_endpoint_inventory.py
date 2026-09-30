from __future__ import annotations

import json
from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.endpoint_inventory import EndpointInventory, EndpointInventoryError


def test_loads_endpoint_records_read_only_from_cdpa_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "cdpa.yaml"
    config_path.write_text(
        json.dumps({"browser": {"cdp_url": "http://127.0.0.1:9222"}}),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")
    inventory = EndpointInventory(load_runtime_config(repository_root=tmp_path))

    result = inventory.summarize_endpoints(probe=False)

    assert config_path.read_text(encoding="utf-8") == before
    assert len(result.items) == 1
    assert result.items[0]["endpoint_id"] == "legacy"
    assert result.items[0]["cdp_url"] == "http://127.0.0.1:9222"
    assert result.items[0]["mode"] == "read_only_inventory"


def test_offline_endpoint_reports_item_diagnostic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "cdpa.yaml"
    config_path.write_text(
        json.dumps({"browser": {"cdp_url": "http://127.0.0.1:65530"}}),
        encoding="utf-8",
    )
    inventory = EndpointInventory(load_runtime_config(repository_root=tmp_path))

    def fail_probe(host: str, port: int, timeout: float) -> bool:
        return False

    monkeypatch.setattr(inventory, "_probe_host_port", fail_probe)

    result = inventory.summarize_endpoints(probe=True)

    assert result.items[0]["reachable"] is False
    assert result.items[0]["diagnostic"] == "endpoint_probe_failed"
    assert result.diagnostics == []


def test_missing_config_returns_empty_with_diagnostic(tmp_path: Path) -> None:
    inventory = EndpointInventory(load_runtime_config(repository_root=tmp_path))

    result = inventory.summarize_endpoints(probe=False)

    assert result.items == []
    assert result.diagnostics[0]["code"] == "endpoint_config_missing"


def test_malformed_config_is_typed_error(tmp_path: Path) -> None:
    (tmp_path / "cdpa.yaml").write_text("{bad json", encoding="utf-8")
    inventory = EndpointInventory(load_runtime_config(repository_root=tmp_path))

    with pytest.raises(EndpointInventoryError) as error:
        inventory.summarize_endpoints(probe=False)

    assert error.value.reason == "endpoint_config_invalid"


def test_inventory_does_not_expose_mutation_methods(tmp_path: Path) -> None:
    inventory = EndpointInventory(load_runtime_config(repository_root=tmp_path))

    forbidden = ["send_prompt", "adopt_tab", "route_response", "repair_route"]
    for name in forbidden:
        assert not hasattr(inventory, name)
