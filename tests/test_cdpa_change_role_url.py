"""Change URL is a durable role reassignment, not Send/Resume or endpoint migration."""
from __future__ import annotations

import json

import pytest

from playwright_auto.cdpa_commands import conversation_identity
from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.dashboard_api import APIError, DashboardAPI
from test_cdpa_worker import setup_task

CID = "6acb02f9-751c-83ee-8b7b-b46fd4ea6235"
OTHER = "11111111-1111-4111-8111-111111111111"
URL = f"https://chatgpt.com/g/g-p-project/c/{CID}"
PLAIN = f"https://chatgpt.com/c/{CID}"
OTHER_URL = f"https://chatgpt.com/c/{OTHER}"


def pinned_task(tmp_path, **kwargs):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.cdpa_worker import CDPAWorker
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=kwargs.get("task_id", "task-a"),
        browser_endpoint_id="legacy",
        governing_objective="Implement exact production behavior",
        governing_directives=[],
    )
    return config, store, state, CDPAWorker(config, store=store)


def test_role_url_change_persists_without_send_or_endpoint_change(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="role-url-one")
    path = state["manifest_path"]
    role = "PLAN"
    endpoint = state["roles"][role].get("browser_endpoint_id") or state["browser_endpoint_id"]
    original = _active_hop(state)
    request_id = original["request_id"]
    updated = store.change_role_url(
        path, role=role, url=URL, expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-change-role-url-one",
    )
    hop = _active_hop(updated)
    assert updated["roles"][role]["page_url"] == URL
    assert (updated["roles"][role].get("browser_endpoint_id") or updated["browser_endpoint_id"]) == endpoint
    assert updated["roles"][role]["page_id"] is None
    assert hop["request_id"] == request_id
    assert hop["conversation_url"] == URL
    assert hop["state"] == "pre_send"
    assert updated["status"] == state["status"]
    assert len(updated["role_url_revisions"]) == 1
    assert store.load(path)["roles"][role]["page_url"] == URL

    replay = store.change_role_url(
        path, role=role, url=URL, expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-change-role-url-one",
    )
    assert len(replay["role_url_revisions"]) == 1
    with pytest.raises(ValueError, match="replay provenance"):
        store.change_role_url(
            path, role=role, url=OTHER_URL, expected_browser_endpoint_id=endpoint,
            external_command_id="cmd-change-role-url-one",
        )


def test_paused_inflight_alias_change_preserves_request_and_receipt(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="role-url-paused")
    endpoint = state["roles"]["PLAN"].get("browser_endpoint_id") or state["browser_endpoint_id"]

    def paused(current):
        role = current["roles"]["PLAN"]
        role.update(page_id="old-owned-page", page_url="https://chatgpt.com/",
                    online=True)
        parent = _active_hop(current)
        parent.update(
            state="routed", route="PAUSE", conversation_url=PLAIN,
            receipt={"conversation_id": CID},
        )
        child = worker._append_hop(
            current, source_role="PLAN", target_role="PLAN",
            handoff="Continue review", kind="handoff",
        )
        child.update(state="sending", conversation_url=None,
                     prompt_sha256="immutable-prompt")
        current.update(status="PAUSED", kanban_column="PAUSED",
                       active_action="send")
        return current

    state = store.update(state["manifest_path"], paused)
    old = _active_hop(state)
    updated = store.change_role_url(
        state["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=endpoint,
        external_command_id="cmd-paused-alias",
    )
    new = _active_hop(updated)
    assert updated["status"] == "PAUSED"
    assert updated["roles"]["PLAN"]["page_url"] == URL
    assert new["request_id"] == old["request_id"]
    assert new["state"] == "sending"
    assert new["conversation_url"] is None
    assert new["prompt_sha256"] == "immutable-prompt"
    assert updated["hops"][0]["receipt"] == state["hops"][0]["receipt"]
    with pytest.raises(ValueError, match="in flight"):
        store.change_role_url(
            state["manifest_path"], role="PLAN", url=OTHER_URL,
            expected_browser_endpoint_id=endpoint,
            external_command_id="cmd-paused-foreign",
        )


def test_change_role_url_rejects_foreign_owner_and_wrong_endpoint(tmp_path):
    _config, store, first, _worker = pinned_task(tmp_path, task_id="url-first")
    endpoint = first["roles"]["PLAN"].get("browser_endpoint_id") or first["browser_endpoint_id"]
    store.change_role_url(
        first["manifest_path"], role="PLAN", url=URL,
        expected_browser_endpoint_id=endpoint, external_command_id="cmd-first",
    )
    _config2, _store2, second, _worker2 = pinned_task(tmp_path, task_id="url-second")
    with pytest.raises(ValueError, match="already reserved"):
        store.change_role_url(
            second["manifest_path"], role="PLAN", url=PLAIN,
            expected_browser_endpoint_id=endpoint, external_command_id="cmd-collision",
        )
    with pytest.raises(ValueError, match="endpoint mismatch"):
        store.change_role_url(
            second["manifest_path"], role="PLAN", url=OTHER_URL,
            expected_browser_endpoint_id="wrong-endpoint", external_command_id="cmd-mismatch",
        )
    with pytest.raises(ValueError, match="role"):
        store.change_role_url(
            second["manifest_path"], role="MISSING", url=OTHER_URL,
            expected_browser_endpoint_id=endpoint, external_command_id="cmd-missing-role",
        )


def test_change_url_rejects_other_task_hop_only_reservation(tmp_path):
    _config, store, state, _worker = pinned_task(tmp_path, task_id="url-target")
    solo = store.create_task(
        "Solo with existing conversation",
        requested_team="solo-existing-reservation",
        task_id="solo-existing-reservation",
        workflow_mode="solo", roles=("REVIEW",),
        browser_endpoint_id="legacy",
        conversation_url=URL,
    )
    assert solo["roles"]["REVIEW"]["page_url"] is None
    assert solo["hops"][0]["conversation_url"] == URL
    with pytest.raises(ValueError, match="already reserved"):
        store.change_role_url(
            state["manifest_path"], role="PLAN", url=PLAIN,
            expected_browser_endpoint_id=state["browser_endpoint_id"],
            external_command_id="cmd-url-reservation-collision",
        )


def test_role_url_api_requires_url_role_and_endpoint(tmp_path):
    config, _store, _state, _worker = pinned_task(tmp_path)
    payload, version = DashboardAPI.normalize_role_url_change({
        "role": "review", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
        "expected_task_version": 10,
    })
    assert payload == {
        "role": "REVIEW", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
    }
    assert version == 10
    for bad in (
        {"role": "REVIEW", "url": "https://chatgpt.com/"},
        {"role": "REVIEW", "url": "https://chatgpt.com/c/not-a-uuid"},
        {"role": "REVIEW", "url": URL},
        {"role": "REVIEW", "url": URL, "expected_browser_endpoint_id": "one", "extra": "x"},
        {"role": "REVIEW", "url": URL, "expected_browser_endpoint_id": "one", "expected_task_version": True},
    ):
        with pytest.raises(APIError):
            DashboardAPI.normalize_role_url_change(bad)


def test_role_url_api_endpoint_enqueues_idempotent_command(tmp_path):
    from test_dashboard_api import request, start_api

    _config, db, server, thread = start_api(tmp_path)
    body = {
        "role": "REVIEW", "url": URL,
        "expected_browser_endpoint_id": "endpoint-9212",
        "expected_task_version": 9,
    }
    try:
        headers = {"Idempotency-Key": "url-review-9212"}
        code, _, result = request(
            server, "POST", "/api/tasks/task-a/role-url", body=body, headers=headers
        )
        assert code == 202
        accepted = json.loads(result)
        command = db.get_command(accepted["command_id"])
        assert command["kind"] == "change_role_url"
        assert command["payload"] == {
            "role": "REVIEW", "url": URL,
            "expected_browser_endpoint_id": "endpoint-9212",
        }
        assert command["task_id"] == "task-a"
        assert command["expected_task_version"] == 9
        repeated, _, same = request(
            server, "POST", "/api/tasks/task-a/role-url",
            body=body, headers=headers,
        )
        assert repeated == 202
        assert json.loads(same) == accepted
        for index, bad in enumerate((
            {"role": "REVIEW", "url": "https://chatgpt.com/"},
            {"role": "REVIEW", "url": URL},
            {**body, "expected_task_version": "not-an-int"},
        )):
            bad_status, _, _ = request(
                server, "POST", "/api/tasks/task-a/role-url",
                body=bad, headers={"Idempotency-Key": f"url-bad-{index}"},
            )
            assert bad_status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_change_url_ui_is_role_scoped_and_endpoint_readonly():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "playwright_auto"
    html = (root / "dashboard.html").read_text(encoding="utf-8")
    app = (root / "dashboard_assets" / "app.js").read_text(encoding="utf-8")
    detail = (root / "dashboard_assets" / "views" / "task_detail.js").read_text(encoding="utf-8")
    assert 'id="change-url-dialog"' in html
    assert 'name="expected_browser_endpoint_id"' in html
    assert 'id="change-url-endpoint-name"' in html
    assert 'id="change-url-endpoint-id"' in html
    assert 'id="change-url-endpoint-address"' in html
    assert 'name="current_url" type="text" readonly' in html
    assert 'name="url" type="url" required' in html
    assert 'data.change_role_url' not in html
    assert 'node.dataset.changeRoleUrl = task.task_id' in detail
    assert 'const missing = !role || !endpointId || changeUrlDetail?.endpoint_pinned === false;' in app
    assert 'state.runtime?.browser?.endpoints' in app
    assert 'expected_browser_endpoint_id: endpointId' in app
    assert '/role-url' in app
    assert '/assets/app.js?v=20261011-p0012-challenge-confirm-v1' in html
    assert './views/task_detail.js?v=20261011-p0012-challenge-confirm-v1' in app


def test_change_role_url_does_not_change_existing_ten_minute_timeout():
    from playwright_auto.role_runtime.model import Policy
    assert Policy().timeout_seconds == 600.0


def test_change_role_url_runtime_command_idempotent_without_browser(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id="url-command")
    worker.hydrate_runtime()
    role = "PLAN"
    endpoint = state["roles"][role].get("browser_endpoint_id") or state["browser_endpoint_id"]
    worker.runtime_db.enqueue_command(
        command_id="cmd-url-change", idempotency_key="change-role-url",
        kind="change_role_url", task_id=state["task_id"],
        expected_task_version=None,
        payload={"role": role, "url": URL,
                 "expected_browser_endpoint_id": endpoint},
    )
    first = worker.dispatch_command_once()
    assert first["status"] == "applied"
    assert first["result"]["url"] == URL
    changed = store.load_task_id(state["task_id"])
    assert changed["roles"][role]["page_url"] == URL
    assert len(changed["role_url_revisions"]) == 1
    with worker.runtime_db.connection() as connection:
        connection.execute(
            "UPDATE command_queue SET status = 'queued', started_at = NULL, "
            "finished_at = NULL, result_json = NULL, error = NULL WHERE command_id = ?",
            ("cmd-url-change",),
        )
    second = worker.dispatch_command_once()
    assert second["status"] == "applied"
    assert second["result"]["reconciled"] is True
    assert len(store.load_task_id(state["task_id"])["role_url_revisions"]) == 1
