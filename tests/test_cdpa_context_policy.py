from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.dashboard_api import DashboardAPI

from test_cdpa_core import write_config


def test_store_persists_explicit_fresh_context_policy_independently_of_bootstrap_default(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)

    fresh = store.create_task(
        "explicit fresh task",
        requested_team="fresh-policy",
        task_id="task-explicit-fresh",
        context_policy="fresh",
    )

    assert fresh["context_policy"] == "fresh"
    assert "bootstrap" not in fresh
    with pytest.raises(ValueError, match="immutable"):
        store.update(
            fresh["manifest_path"],
            lambda state: state.update(context_policy="bootstrap"),
        )


def test_store_rejects_fresh_policy_with_bootstrap_snapshot(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    bootstrap = {
        "bootstrap_id": "test-bootstrap",
        "name": "Test bootstrap",
        "description": "",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_assistant_message_id": "22222222-2222-4222-8222-222222222222",
        "enabled": True,
        "tags": [],
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "expires_at": None,
        "last_verified_at": None,
        "source_fingerprints": {},
    }

    with pytest.raises(ValueError, match="fresh.*bootstrap"):
        store.create_task(
            "invalid mixed context",
            task_id="task-invalid-context",
            bootstrap=bootstrap,
            context_policy="fresh",
        )


def test_dashboard_explicit_null_bootstrap_materializes_fresh_policy(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    api = DashboardAPI(config)

    payload = api.normalize_create({"task": "fresh API task", "bootstrap_id": None})

    assert payload["bootstrap_id"] is None
    assert payload["context_policy"] == "fresh"
