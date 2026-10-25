from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto import connection
from test_cdpa_core import write_config


def registry(tmp_path):
    from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return BrowserEndpointRegistry(config)


@pytest.mark.parametrize(("value", "expected"), [
    ("9202", "http://127.0.0.1:9202"),
    ("localhost:9202", "http://127.0.0.1:9202"),
    ("100.80.10.4:9222", "http://100.80.10.4:9222"),
    ("http://100.80.10.8:9222/", "http://100.80.10.8:9222"),
    ("https://browser.example/cdp/", "https://browser.example/cdp"),
    ("[::1]:9222", "http://[::1]:9222"),
    ("ws://host:9222/devtools/browser/abc", "ws://host:9222/devtools/browser/abc"),
])
def test_endpoint_address_normalization(value, expected):
    assert connection.normalize_cdp_endpoint(value) == expected


@pytest.mark.parametrize("value", [
    "", "0", "65536", "host", "http://host:0", "http://host:65536",
    "ftp://host:9222", "http://user:pass@host:9222", "http://host:9222/#fragment",
    "host:9222\nother", "http://host:notaport", "file:///tmp/chrome", True,
])
def test_invalid_endpoint_rejected(value):
    with pytest.raises(ValueError):
        connection.normalize_cdp_endpoint(value)


def test_legacy_identity_survives_default_and_address_changes(tmp_path):
    catalog = registry(tmp_path)
    original = catalog.resolve()
    added = catalog.add("9202", label="Second browser")
    catalog.update(added["endpoint_id"], is_default=True)
    catalog.update(original["endpoint_id"], cdp_url="9302")
    reopened = registry(tmp_path)
    assert reopened.resolve()["endpoint_id"] == added["endpoint_id"]
    assert reopened.legacy_endpoint_id == original["endpoint_id"]
    assert reopened.get(original["endpoint_id"])["cdp_url"] == "http://127.0.0.1:9302"


def test_same_port_different_hosts_are_independent(tmp_path):
    catalog = registry(tmp_path)
    one = catalog.add("100.80.10.4:9222")
    two = catalog.add("100.80.10.8:9222")
    assert one["endpoint_id"] != two["endpoint_id"]
    assert catalog.resolve("100.80.10.4:9222") == one
    assert catalog.resolve(two["endpoint_id"]) == two
    assert catalog.add("http://100.80.10.4:9222/") == one


def test_default_and_temporary_updates_are_atomic(tmp_path):
    catalog = registry(tmp_path)
    one = catalog.resolve()
    two = catalog.add("9202")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: catalog.update(two["endpoint_id"], temporary_default=True), range(8)))
    catalog.update(two["endpoint_id"], is_default=True)
    assert sum(item["is_default"] for item in catalog.list()) == 1
    assert catalog.get(two["endpoint_id"])["temporary_default"] is True
    assert catalog.get(one["endpoint_id"])["temporary_default"] is False
    with pytest.raises(ValueError):
        catalog.update(two["endpoint_id"], temporary_default="false")


def test_removal_drains_assigned_endpoint_without_changing_identity(tmp_path):
    catalog = registry(tmp_path)
    original = catalog.resolve()
    second = catalog.add("9202")
    catalog.update(second["endpoint_id"], is_default=True)
    result = catalog.remove(original["endpoint_id"], has_nonterminal_tasks=True)
    assert result["draining"] is True
    assert result["removed"] is False
    with pytest.raises(ValueError, match="draining"):
        catalog.resolve(original["endpoint_id"])
    catalog.remove(original["endpoint_id"], has_nonterminal_tasks=False)
    assert catalog.get(original["endpoint_id"])["removed"] is True
    assert catalog.legacy_endpoint_id == original["endpoint_id"]
    assert all(item["endpoint_id"] != original["endpoint_id"] for item in catalog.list())


def test_managed_remote_and_unsafe_profile_are_rejected(tmp_path):
    catalog = registry(tmp_path)
    with pytest.raises(ValueError, match="local"):
        catalog.add("100.80.10.4:9222", management_mode="managed-local", profile_path=str(tmp_path))
    with pytest.raises(ValueError, match="HTTP"):
        catalog.add(
            "ws://127.0.0.1:9202/devtools/browser/test",
            management_mode="managed-local", profile_path=str(tmp_path),
        )
    with pytest.raises(ValueError, match="absolute"):
        catalog.add("9202", management_mode="managed-local", profile_path="relative/profile")


def test_explicit_config_registry_seeds_once(tmp_path):
    import json
    from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
    path = write_config(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["browser"]["endpoints"] = [
        {"endpoint_id": "primary", "cdp_url": "9222", "is_default": True},
        {"endpoint_id": "secondary", "cdp_url": "9202", "temporary_default": True},
    ]
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_cdpa_config(path, repository_root=tmp_path)
    catalog = BrowserEndpointRegistry(config)
    assert catalog.resolve()["endpoint_id"] == "primary"
    assert catalog.get("secondary")["temporary_default"] is True
    catalog.update("secondary", is_default=True)
    assert BrowserEndpointRegistry(config).resolve()["endpoint_id"] == "secondary"
    assert catalog.legacy_endpoint_id == "primary"


def test_unknown_endpoint_never_falls_back_to_default(tmp_path):
    catalog = registry(tmp_path)
    with pytest.raises(ValueError, match="not registered"):
        catalog.resolve("9999")
    with pytest.raises(ValueError, match="unknown"):
        catalog.get("missing-id")


def test_registry_read_is_nonmutating_after_initialization(tmp_path):
    catalog = registry(tmp_path)
    catalog.list()
    before = catalog.path.stat().st_mtime_ns
    catalog.list()
    catalog.resolve()
    assert catalog.path.stat().st_mtime_ns == before


def test_endpoint_capacity_defaults_to_two_and_is_configurable(tmp_path):
    catalog = registry(tmp_path)
    original = catalog.resolve()
    assert original["max_active_tabs"] == 2
    second = catalog.add("9202", max_active_tabs=3, rate_limit_scope="account-b")
    assert second["max_active_tabs"] == 3
    assert second["rate_limit_scope"] == "account-b"
    updated = catalog.update(second["endpoint_id"], max_active_tabs=4)
    assert updated["max_active_tabs"] == 4


def test_endpoint_loader_ignores_legacy_tool_capability_metadata(tmp_path):
    catalog = registry(tmp_path)
    endpoint = catalog.resolve()
    raw = catalog.snapshot()
    raw["entries"][endpoint["endpoint_id"]].update(
        tool_capabilities=["md:g6"],
        tool_capabilities_configured=True,
        tool_capability_bindings={"md:g6": {"target_device_id": "legacy-device"}},
    )

    normalized = catalog._validate(raw)

    loaded = normalized["entries"][endpoint["endpoint_id"]]
    assert "tool_capabilities" not in loaded
    assert "tool_capabilities_configured" not in loaded
    assert "tool_capability_bindings" not in loaded


@pytest.mark.parametrize("value", [0, -1, True, "2"])
def test_endpoint_capacity_requires_a_positive_integer(tmp_path, value):
    catalog = registry(tmp_path)
    with pytest.raises(ValueError, match="max_active_tabs"):
        catalog.add("9202", max_active_tabs=value)
