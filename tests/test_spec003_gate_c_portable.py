from __future__ import annotations

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
from playwright_auto.cdpa_provider_runtime import ProviderRuntimeError, select_provider
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.dashboard_api import APIError, DashboardAPI

from test_cdpa_core import write_config


def _config(tmp_path):
    return load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)


def test_gate_c_chatgpt_provider_advertises_adopted_and_direct_chat():
    provider = select_provider(
        explicit_provider_id="chatgpt_web",
        required_capabilities=("browser", "adopted_url", "direct_chat"),
        execution_mode="browser_dom",
    )
    assert provider.provider_id == "chatgpt_web"
    with pytest.raises(ProviderRuntimeError):
        select_provider(
            explicit_provider_id="codex",
            required_capabilities=("adopted_url",),
        )


def test_gate_c_teamtask_adopted_url_requires_explicit_endpoint(tmp_path):
    config = _config(tmp_path)
    api = DashboardAPI(config)
    endpoint = BrowserEndpointRegistry(config).resolve()
    payload = api.normalize_create(
        {
            "task": "continue exact conversation",
            "roles": ["DEV", "REVIEW"],
            "start_role": "DEV",
            "browser_endpoint_id": endpoint["endpoint_id"],
            "conversation_url": "https://chatgpt.com/c/adopted-123",
        }
    )
    assert payload["conversation_url"] == "https://chatgpt.com/c/adopted-123"
    assert payload["browser_endpoint_id"] == endpoint["endpoint_id"]

    with pytest.raises(APIError, match="browser_endpoint_id"):
        api.normalize_create(
            {
                "task": "continue exact conversation",
                "roles": ["DEV"],
                "conversation_url": "https://chatgpt.com/c/adopted-123",
            }
        )


def test_gate_c_teamtask_start_role_owns_adopted_url(tmp_path):
    config = _config(tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    state = store.create_task(
        "continue adopted",
        roles=["DEV", "REVIEW"],
        route_targets=["REVIEW"],
        closing_role="REVIEW",
        start_role="DEV",
        browser_endpoint_id=endpoint["endpoint_id"],
        conversation_url="https://chatgpt.com/c/adopted-123",
    )
    assert state["start_role"] == "DEV"
    assert state["hops"][0]["target_role"] == "DEV"
    assert state["hops"][0]["conversation_url"] == "https://chatgpt.com/c/adopted-123"
    assert state["role_slots"]["REVIEW"]["state"] == "declared"
    assert "conversation_url" not in state["roles"]["REVIEW"]


def test_gate_c_noncapable_provider_cannot_adopt_url(tmp_path):
    config = _config(tmp_path)
    store = TaskStore(config)
    endpoint = store.endpoints.resolve()
    with pytest.raises(ValueError, match="cannot adopt conversation_url"):
        store.create_task(
            "invalid adopted provider",
            roles=["DEV"],
            provider_id="codex",
            browser_endpoint_id=endpoint["endpoint_id"],
            conversation_url="https://chatgpt.com/c/adopted-123",
        )
