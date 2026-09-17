from __future__ import annotations

import json
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker

from test_cdpa_core import write_config
from test_dashboard_api import request, start_api


def _command(db: RuntimeDB, body: bytes) -> dict:
    payload = json.loads(body)
    command = db.get_command(payload["command_id"])
    assert command is not None
    return command


def test_browser_endpoint_api_queues_pool_management_commands(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    try:
        status, _headers, body = request(
            server, "POST", "/api/browser-endpoints",
            body={"address": "9202", "label": "Peer", "max_active_tabs": 3},
            headers={"Idempotency-Key": "browser-add"},
        )
        assert status == 202
        add = _command(db, body)
        assert add["kind"] == "browser_endpoint_add"
        assert add["payload"] == {
            "address": "9202", "label": "Peer", "max_active_tabs": 3,
        }

        for operation, kind, payload in (
            ("settings", "browser_endpoint_update", {"draining": False}),
            ("start", "browser_endpoint_start", {}),
            ("stop", "browser_endpoint_stop", {}),
            ("remove", "browser_endpoint_remove", {}),
        ):
            status, _headers, body = request(
                server, "POST", f"/api/browser-endpoints/peer-id/{operation}",
                body=payload,
                headers={"Idempotency-Key": f"browser-{operation}"},
            )
            assert status == 202
            command = _command(db, body)
            assert command["kind"] == kind
            assert command["payload"]["endpoint_id"] == "peer-id"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_browser_endpoint_api_rejects_invalid_management_settings(tmp_path: Path):
    _config, _db, server, thread = start_api(tmp_path)
    try:
        invalid = (
            {"address": "9202", "management_mode": "managed-local"},
            {"address": "9202", "profile_path": str(tmp_path / "profile")},
            {"address": "9202", "max_active_tabs": True},
        )
        for index, body in enumerate(invalid):
            status, _headers, _response = request(
                server, "POST", "/api/browser-endpoints", body=body,
                headers={"Idempotency-Key": f"invalid-browser-{index}"},
            )
            assert status == 400
        status, _headers, _response = request(
            server,
            "POST",
            "/api/browser-endpoints/peer-id/settings",
            body={"draining": True},
            headers={"Idempotency-Key": "invalid-browser-redrain"},
        )
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_worker_applies_external_endpoint_registry_commands(tmp_path: Path, monkeypatch):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    worker.runtime_db.ensure_schema()
    from unittest.mock import AsyncMock
    monkeypatch.setattr(worker, "_probe_browser_endpoint", AsyncMock(return_value={
        "health": "online", "auth_state": "authenticated", "chatgpt_ready": True,
    }))
    worker.runtime_db.enqueue_command(
        command_id="cmd-add-browser", idempotency_key="add-browser",
        kind="browser_endpoint_add", task_id=None, expected_task_version=None,
        payload={"address": "9202", "label": "Peer", "rate_limit_scope": "account-b"},
    )
    result = worker._apply_next_command()
    assert result is not None and result["status"] == "applied"
    endpoint_id = result["result"]["endpoint_id"]
    added = store.endpoints.get(endpoint_id)
    assert added["cdp_url"] == "http://127.0.0.1:9202"
    assert added["rate_limit_scope"] == "account-b"

    worker.runtime_db.enqueue_command(
        command_id="cmd-update-browser", idempotency_key="update-browser",
        kind="browser_endpoint_update", task_id=None, expected_task_version=None,
        payload={"endpoint_id": endpoint_id, "is_default": True, "temporary_default": True},
    )
    assert worker._apply_next_command()["status"] == "applied"
    assert store.endpoints.resolve()["endpoint_id"] == endpoint_id
    assert store.endpoints.get(endpoint_id)["temporary_default"] is True

    store.endpoints.remove(endpoint_id, has_nonterminal_tasks=True)
    assert store.endpoints.get(endpoint_id)["draining"] is True
    worker.runtime_db.enqueue_command(
        command_id="cmd-restore-browser", idempotency_key="restore-browser",
        kind="browser_endpoint_update", task_id=None, expected_task_version=None,
        payload={"endpoint_id": endpoint_id, "draining": False},
    )
    assert worker._apply_next_command()["status"] == "applied"
    restored = store.endpoints.get(endpoint_id)
    assert restored["draining"] is False
    assert restored["removed"] is False

    worker.runtime_db.enqueue_command(
        command_id="cmd-remove-browser", idempotency_key="remove-browser",
        kind="browser_endpoint_remove", task_id=None, expected_task_version=None,
        payload={"endpoint_id": endpoint_id},
    )
    assert worker._apply_next_command()["status"] == "applied"
    assert store.endpoints.get(endpoint_id)["removed"] is True


def test_worker_manages_local_browser_lifecycle_and_profile_clone(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    worker.runtime_db.ensure_schema()
    source_profile = tmp_path / "source-profile"
    source_profile.mkdir()
    source = store.endpoints.add(
        "9203", management_mode="managed-local", profile_path=str(source_profile)
    )

    class FakeManagedBrowsers:
        def __init__(self):
            self.calls = []

        def read_pid(self, endpoint_id):
            self.calls.append(("read_pid", endpoint_id))
            return None

        def prepare_profile(self, **kwargs):
            self.calls.append(("prepare_profile", kwargs))

        def start(self, endpoint):
            self.calls.append(("start", endpoint["endpoint_id"]))
            return 4321

        def stop(self, endpoint):
            self.calls.append(("stop", endpoint["endpoint_id"]))
            return True

    fake = FakeManagedBrowsers()
    worker.managed_browsers = fake
    target_profile = tmp_path / "target-profile"
    worker.runtime_db.enqueue_command(
        command_id="cmd-managed-add", idempotency_key="managed-add",
        kind="browser_endpoint_add", task_id=None, expected_task_version=None,
        payload={
            "address": "9204", "management_mode": "managed-local",
            "profile_path": str(target_profile),
            "clone_from_endpoint_id": source["endpoint_id"], "start": True,
        },
    )
    added = worker._apply_next_command()
    assert added is not None and added["status"] == "applied"
    endpoint_id = added["result"]["endpoint_id"]
    prepare = next(call for call in fake.calls if call[0] == "prepare_profile")
    assert prepare[1]["clone_from"]["endpoint_id"] == source["endpoint_id"]
    assert ("start", endpoint_id) in fake.calls

    for operation in ("stop", "start"):
        worker.runtime_db.enqueue_command(
            command_id=f"cmd-managed-{operation}", idempotency_key=f"managed-{operation}",
            kind=f"browser_endpoint_{operation}", task_id=None, expected_task_version=None,
            payload={"endpoint_id": endpoint_id},
        )
        result = worker._apply_next_command()
        assert result is not None and result["status"] == "applied"
        assert "pid" not in result["result"]
    assert ("stop", endpoint_id) in fake.calls
    assert fake.calls.count(("start", endpoint_id)) == 2


def test_draining_endpoint_is_removed_after_running_execution_finishes(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    worker = CDPAWorker(config, store=store)
    endpoint = store.endpoints.add("9205")
    worker._running_executions = lambda: [{"endpoint_id": endpoint["endpoint_id"]}]
    draining = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint["endpoint_id"]})
    assert draining["draining"] is True
    assert draining["removed"] is False

    worker._running_executions = lambda: []
    assert worker._finalize_drained_endpoints() == [endpoint["endpoint_id"]]
    assert store.endpoints.get(endpoint["endpoint_id"])["removed"] is True


def test_projection_uses_requested_endpoint_until_role_is_actually_placed(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    requested = store.endpoints.add("9212")
    state = store.create_task(
        "queued placement",
        requested_team="queued-alpha",
        task_id="task-queued-placement",
        browser_endpoint_id=requested["endpoint_id"],
    )

    queued = build_task_projection(
        state,
        tasks=[state],
        browser_pages=(),
        browser_connected=True,
        repository_allowed_roots=config.repository_allowed_roots,
    )
    assert queued.summary["browser_endpoint_ids"] == [requested["endpoint_id"]]

    state["roles"]["PLAN"]["browser_endpoint_id"] = "actual-endpoint"
    placed = build_task_projection(
        state,
        tasks=[state],
        browser_pages=(),
        browser_connected=True,
        repository_allowed_roots=config.repository_allowed_roots,
    )
    assert placed.summary["browser_endpoint_ids"] == ["actual-endpoint"]


def test_projection_exposes_role_execution_endpoint_placement(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task("placement", requested_team="alpha", task_id="task-placement")
    state["roles"]["PLAN"]["browser_endpoint_id"] = "endpoint-a"
    state["roles"]["DEV"]["browser_endpoint_id"] = "endpoint-b"
    projection = build_task_projection(
        state,
        tasks=[state],
        browser_pages=(),
        browser_connected=True,
        repository_allowed_roots=config.repository_allowed_roots,
    )
    assert projection.summary["browser_endpoint_ids"] == ["endpoint-a", "endpoint-b"]
    placements = {
        role["logical_role"]: role["browser_endpoint_id"]
        for role in projection.detail["roles"]
    }
    assert placements["PLAN"] == "endpoint-a"
    assert placements["DEV"] == "endpoint-b"


def test_dashboard_browser_scope_and_create_controls_are_explicit():
    from playwright_auto.dashboard import ASSET_ROOT, DASHBOARD_HTML_PATH

    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views" / "board.js").read_text(encoding="utf-8")
    browsers = (ASSET_ROOT / "views" / "browsers.js").read_text(encoding="utf-8")
    store = (ASSET_ROOT / "store.js").read_text(encoding="utf-8")

    assert 'id="browser-scope"' in html
    assert html.count('name="browser_endpoint_id"') >= 3
    assert 'name="temporary"' in html
    assert 'id="browser-dialog"' in html
    assert "selectedEndpointId" in store
    assert "task.browser_endpoint_ids" in board
    assert "renderBrowserScope" in app
    assert "renderBrowserManager" in app
    assert "body.browser_endpoint_id" in app
    assert "agentBrowserEndpointSelect" in app
    assert "agentSettingsBrowserEndpointSelect" in app
    assert 'browser_endpoint_id: String(values.get("browser_endpoint_id") || "").trim() || null' in app
    assert "body.temporary = true" in app
    for operation in ("browser_endpoint_add", "browser_endpoint_update", "browser_endpoint_remove", "browser_endpoint_start", "browser_endpoint_stop"):
        assert operation in app or operation in browsers


def test_browser_manager_poll_does_not_replace_focused_form():
    from playwright_auto.dashboard import ASSET_ROOT
    import json
    import subprocess

    module = (ASSET_ROOT / "views" / "browsers.js").resolve().as_uri()
    script = f"""
      import {{ renderBrowserManager }} from {json.dumps(module)};
      const active = {{matches: selector => selector === 'input,select,textarea'}};
      globalThis.document = {{activeElement: active}};
      const root = {{
        dataset: {{browserSignature: 'old'}},
        childElementCount: 1,
        contains: node => node === active,
        replaceChildren: () => {{ throw new Error('focused form was replaced'); }},
      }};
      renderBrowserManager(root, {{runtime: {{browser: {{endpoints: [{{endpoint_id:'peer', health:'online'}}]}}}}}});
      console.log('ok');
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "ok"
