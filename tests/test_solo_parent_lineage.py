from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.dashboard_api import APIError, DashboardAPI, create_server

from test_cdpa_core import write_config
from test_dashboard_api import projection, request

PARENT_URL = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
CHILD_URL = "https://chatgpt.com/c/22222222-3333-4444-5555-666666666666"


def make_store(tmp_path: Path) -> TaskStore:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config)


def create_solo(store: TaskStore, task_id: str, conversation_url: str, **kwargs):
    return store.create_task(
        "Solo lineage work",
        requested_team=f"lineage-{task_id}",
        task_id=task_id,
        workflow_mode="solo",
        roles=("DEV",),
        goal="Lineage goal",
        browser_endpoint_id="9222",
        conversation_url=conversation_url,
        **kwargs,
    )


def test_solo_without_parent_has_no_parent_key(tmp_path: Path):
    store = make_store(tmp_path)

    state = create_solo(store, "lineage-plain", PARENT_URL)

    assert "parent_task_id" not in state


def test_solo_child_records_existing_parent(tmp_path: Path):
    store = make_store(tmp_path)
    create_solo(store, "lineage-parent", PARENT_URL)

    child = create_solo(
        store, "lineage-child", CHILD_URL, parent_task_id="lineage-parent"
    )

    assert child["parent_task_id"] == "lineage-parent"


def test_solo_with_unknown_parent_is_rejected(tmp_path: Path):
    store = make_store(tmp_path)

    with pytest.raises(
        ValueError, match="parent_task_id must identify an existing task"
    ):
        create_solo(
            store, "lineage-orphan", CHILD_URL, parent_task_id="lineage-missing"
        )


def test_team_task_with_parent_is_rejected_by_store(tmp_path: Path):
    store = make_store(tmp_path)

    with pytest.raises(
        ValueError, match="parent_task_id is supported only for Solo"
    ):
        store.create_task(
            "Team lineage work",
            requested_team="lineage-team",
            task_id="lineage-team-task",
            parent_task_id="lineage-parent",
        )


def test_normalize_create_rejects_parent_for_team(tmp_path: Path):
    config = load_cdpa_config(None, repository_root=tmp_path)
    api = DashboardAPI(config)

    with pytest.raises(APIError) as excinfo:
        api.normalize_create({"task": "team work", "parent_task_id": "lineage-parent"})
    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_request"

    with pytest.raises(APIError):
        api.normalize_create({"task": "team work", "parent_task_id": None})


def test_normalize_create_accepts_parent_for_solo(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config)

    payload = api.normalize_create(
        {
            "task": "solo child",
            "workflow_mode": "solo",
            "role": "DEV",
            "goal": "child goal",
            "parent_task_id": "lineage-parent",
            "browser_endpoint_id": "9222",
            "repository": str(tmp_path),
        }
    )

    assert payload["parent_task_id"] == "lineage-parent"
    assert payload["roles"] == ["DEV"]


def test_command_by_idempotency_endpoint_returns_provenance_only(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    db = RuntimeDB(config.runtime_database)
    db.ensure_schema()
    db.replace_task_projections(
        [projection(tmp_path)],
        catalog={
            "complete": True,
            "discovered_at": "2026-07-25T00:00:00+00:00",
            "errors": [],
            "control_repository": str(tmp_path),
        },
    )
    server = create_server(config, host="127.0.0.1", port=0, db=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, data = request(
            server,
            "POST",
            "/api/tasks",
            body={
                "task": "solo lookup",
                "workflow_mode": "solo",
                "role": "DEV",
                "goal": "reconcile me",
                "browser_endpoint_id": "9222",
                "repository": str(tmp_path),
            },
            headers={"Idempotency-Key": "lineage-lookup-1"},
        )
        assert status == 202
        created = json.loads(data)

        status, headers, data = request(
            server, "GET", "/api/commands/by-idempotency/lineage-lookup-1"
        )
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        body = json.loads(data)
        assert set(body) == {
            "command_id",
            "idempotency_key",
            "kind",
            "task_id",
            "status",
            "parent_task_id",
            "team",
            "role",
            "goal_sha256",
        }
        assert body["command_id"] == created["command_id"]
        assert body["task_id"] == created["task_id"]
        assert body["idempotency_key"] == "lineage-lookup-1"
        assert body["role"] == "DEV"
        assert body["parent_task_id"] is None
        assert body["goal_sha256"] == hashlib.sha256(b"reconcile me").hexdigest()
        assert b"reconcile me" not in data

        status, _headers, _data = request(
            server, "GET", "/api/commands/by-idempotency/no-such-key"
        )
        assert status == 404

        status, _headers, _data = request(
            server, "GET", "/api/commands/by-idempotency/" + "k" * 201
        )
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)
