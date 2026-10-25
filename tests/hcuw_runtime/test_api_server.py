from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from playwright_auto.hcuw_runtime.api_server import ApiResponse, RuntimeApiServer, RuntimeApiServerError
from playwright_auto.hcuw_runtime.audit import AuditTimeline
from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.endpoint_inventory import EndpointInventory, EndpointInventoryResult
from playwright_auto.hcuw_runtime.state import RuntimeStateStore
from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner


class FakeEndpointInventory:
    def summarize_endpoints(self, *, probe: bool) -> EndpointInventoryResult:
        return EndpointInventoryResult(
            items=[
                {
                    "endpoint_id": "9222",
                    "label": "Plus",
                    "cdp_url": "http://127.0.0.1:9222",
                    "reachable": True,
                    "source": "unit",
                    "mode": "read_only_inventory",
                    "last_checked_at": "2026-09-30T00:00:00+09:00",
                    "diagnostic": "ok",
                }
            ],
            diagnostics=[],
        )


def make_server(tmp_path: Path) -> RuntimeApiServer:
    config = load_runtime_config(repository_root=tmp_path)
    store = RuntimeStateStore(config)
    inventory = FakeEndpointInventory()
    return RuntimeApiServer(config, store, inventory, AuditTimeline())


def response_json(response: ApiResponse) -> dict[str, object]:
    return json.loads(response.body.decode("utf-8"))


def test_json_envelope_success_shape(tmp_path: Path) -> None:
    response = make_server(tmp_path).handle_get_path("/api/health")
    payload = response_json(response)

    assert response.status == 200
    assert payload["ok"] is True
    assert "data" in payload
    assert payload["diagnostics"] == []
    assert "error" not in payload


def test_health_returns_runtime_v2_ports_and_state(tmp_path: Path) -> None:
    payload = response_json(make_server(tmp_path).handle_get_path("/api/health"))
    data = payload["data"]

    assert data["service"] == "hcuw-runtime-v2-api"
    assert data["port"] == 9227
    assert data["dashboard_port"] == 9226
    assert data["legacy_api_port"] == 9225
    assert data["state_root"].endswith(".runtime-v2")
    assert data["legacy_isolation"] is True


def test_config_route_returns_runtime_config_envelope(tmp_path: Path) -> None:
    payload = response_json(make_server(tmp_path).handle_get_path("/api/config"))
    data = payload["data"]

    assert data["api"]["port"] == 9227
    assert data["dashboard"]["port"] == 9226
    assert data["legacy"]["writable_state_shared"] is False


def test_runs_route_reads_only_runtime_state_store(tmp_path: Path) -> None:
    payload = response_json(make_server(tmp_path).handle_get_path("/api/runs"))

    assert payload["data"] == {"items": []}
    assert not (tmp_path / ".runtime").exists()


def test_endpoints_route_consumes_inventory_result(tmp_path: Path) -> None:
    payload = response_json(make_server(tmp_path).handle_get_path("/api/endpoints"))

    assert payload["data"]["items"][0]["endpoint_id"] == "9222"
    assert payload["data"]["items"][0]["reachable"] is True


def test_audit_snapshot_route_exposes_dom_mutation_listen_and_actions(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    server.handle_get_path("/api/health")
    response = server.handle_get_path("/api/audit/snapshot")
    payload = response_json(response)
    sources = {item["source"]: item for item in payload["data"]["sources"]}

    assert response.status == 200
    assert sources["dom"]["status"] == "inactive"
    assert sources["mutation"]["status"] == "inactive"
    assert sources["listen"]["status"] == "inactive"
    assert sources["actions"]["status"] == "active"
    assert payload["data"]["events"]


def test_audit_events_route_returns_incremental_events(tmp_path: Path) -> None:
    server = make_server(tmp_path)
    server.handle_get_path("/api/health")
    server.handle_get_path("/api/config")

    response = server.handle_get_path("/api/audit/events?since=1")
    payload = response_json(response)

    assert response.status == 200
    assert all(item["event_id"] > 1 for item in payload["data"]["items"])


def test_unknown_route_uses_error_envelope(tmp_path: Path) -> None:
    response = make_server(tmp_path).handle_get_path("/missing")
    payload = response_json(response)

    assert response.status == 404
    assert payload["ok"] is False
    assert payload["error"]["code"] == "route_not_found"


def _server_with_runner(tmp_path: Path, legacy_audit=None) -> RuntimeApiServer:
    config = load_runtime_config(repository_root=tmp_path)
    store = RuntimeStateStore(config)
    store.initialize()
    timeline = AuditTimeline()
    runner = RuntimeTaskRunner(config, store, timeline)
    return RuntimeApiServer(config, store, FakeEndpointInventory(), timeline, runner, legacy_audit)


def test_post_tasks_invalid_role_set_is_400_error_envelope(tmp_path: Path) -> None:
    server = _server_with_runner(tmp_path)
    body = json.dumps({"task": "x", "roles": ["DEV", "REVIEW"], "start_role": "PLAN"}).encode("utf-8")

    response = server.handle_post_path("/api/tasks", body, idempotency_key="invalid-role-set")
    payload = response_json(response)

    assert response.status == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_task_request"
    assert payload["error"]["message"] == "start_role must be included in roles"
    assert server.state_store.list_task_records() == []


def test_post_tasks_requires_idempotency_key(tmp_path: Path) -> None:
    server = _server_with_runner(tmp_path)
    body = json.dumps({"task": "x", "roles": ["REVIEW"]}).encode("utf-8")

    response = server.handle_post_path("/api/tasks", body)
    payload = response_json(response)

    assert response.status == 400
    assert payload["ok"] is False
    assert payload["error"]["code"] == "idempotency_key_required"
    assert server.state_store.list_task_records() == []


class FakeLegacyAudit:
    def __init__(self, summaries: dict) -> None:
        self.summaries = summaries
        self.refreshed_with: frozenset[str] | None = None

    def task_summaries(self) -> dict:
        return self.summaries

    def refresh(self, timeline: AuditTimeline, runtime_task_ids: frozenset[str] = frozenset()) -> None:
        self.refreshed_with = runtime_task_ids


def test_get_tasks_projects_latest_legacy_status_newest_first(tmp_path: Path) -> None:
    legacy = FakeLegacyAudit(
        {
            "cdpa-new": {"status": "RUNNING", "active_role": "DEV", "block_code": None, "updated_at": "t9"},
            "cdpa-old": {"status": "BLOCKED", "active_role": "PLAN", "block_code": "accepted_conversation_identity_unresolved", "updated_at": "t2"},
        }
    )
    server = _server_with_runner(tmp_path, legacy)
    for runtime_id, legacy_id, created in (("rtask-old", "cdpa-old", "t1"), ("rtask-new", "cdpa-new", "t8")):
        server.state_store.save_task_record(
            {
                "runtime_task_id": runtime_id,
                "legacy_task_id": legacy_id,
                "status": "submitted",
                "created_at": created,
                "roles": ["DEV", "REVIEW"],
                "start_role": "DEV",
            }
        )

    items = response_json(server.handle_get_path("/api/tasks"))["data"]["items"]

    assert [item["runtime_task_id"] for item in items] == ["rtask-new", "rtask-old"]
    assert [item["latest"] for item in items] == [True, False]
    assert items[0]["status"] == "RUNNING"
    assert items[0]["active_role"] == "DEV"
    assert items[0]["updated_at"] == "t9"
    assert items[0]["submission_status"] == "submitted"
    assert items[1]["block_code"] == "accepted_conversation_identity_unresolved"
    assert items[1]["roles"] == ["DEV", "REVIEW"]

    server.handle_get_path("/api/audit/snapshot")
    assert legacy.refreshed_with == frozenset({"cdpa-new", "cdpa-old"})


class DiagnosticsLegacyAudit(FakeLegacyAudit):
    def task_detail(self, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "dependency_diagnostics": {
                "ready": False,
                "waiting_on": ["parent-1"],
            },
            "async_continuations": [
                {
                    "continuation_id": "ac-1",
                    "source_kind": "provider_job",
                    "source_refs": ["pjob-1"],
                    "status": "waiting",
                }
            ],
            "provider_jobs": [{"provider_job_id": "pjob-1", "state": "RUNNING"}],
        }

    def runtime_state(self) -> dict:
        return {
            "worker_online": True,
            "worker_stale": False,
            "worker_heartbeat_at": "2026-10-15T00:00:00+00:00",
        }


def test_runtime_diagnostics_projects_scheduler_dependency_and_continuations_without_second_graph(
    tmp_path: Path,
) -> None:
    legacy = DiagnosticsLegacyAudit({})
    server = _server_with_runner(tmp_path, legacy)
    records = [
        {
            "runtime_task_id": "rtask-cron",
            "legacy_task_id": "task-cron",
            "status": "submitted",
            "created_at": "t1",
            "idempotency_key": "cron:job-1:2026-10-15T00:00:00+00:00",
            "trigger": {
                "kind": "cron",
                "cron_job_id": "job-1",
                "scheduled_for": "2026-10-15T00:00:00+00:00",
            },
        },
        {
            "runtime_task_id": "rtask-heartbeat",
            "legacy_task_id": "task-heartbeat",
            "status": "submitted",
            "created_at": "t2",
            "idempotency_key": "heartbeat:worker:legacy-worker:episode-1",
            "trigger": {
                "kind": "stale_heartbeat",
                "subject_kind": "worker",
                "subject_id": "legacy-worker",
                "stale_episode_started_at": "2026-10-15T00:00:00+00:00",
            },
        },
    ]
    for record in records:
        server.state_store.save_task_record(record)

    response = server.handle_get_path("/api/diagnostics")
    payload = response_json(response)["data"]

    assert response.status == 200
    assert {item["runtime_task_id"] for item in payload["scheduler"]} == {
        "rtask-cron", "rtask-heartbeat"
    }
    assert [item["runtime_task_id"] for item in payload["cron_jobs"]] == ["rtask-cron"]
    assert [item["runtime_task_id"] for item in payload["heartbeat_recoveries"]] == [
        "rtask-heartbeat"
    ]
    assert payload["heartbeats"] == [
        {
            "subject_type": "worker",
            "subject_id": "legacy-worker",
            "status": "healthy",
            "heartbeat_at": "2026-10-15T00:00:00+00:00",
            "source": "legacy_runtime_state",
        }
    ]
    assert len(payload["tasks"]) == 2
    assert payload["tasks"][0]["dependency"]["waiting_on"] == ["parent-1"]
    assert payload["tasks"][0]["continuations"][0]["continuation_id"] == "ac-1"
    assert payload["tasks"][0]["provider_jobs"][0]["provider_job_id"] == "pjob-1"
    assert payload["dependency_graph"] == {
        "stored_owner": "TeamTask.depends_on_task_ids",
        "continuation_edges": "derived_only",
    }
    assert payload["independent_agent"]["activation_enabled"] is False


def test_port_conflict_fails_without_fallback_to_legacy(tmp_path: Path) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocked_port = blocker.getsockname()[1]
    blocker.listen(1)

    config = load_runtime_config(repository_root=tmp_path, api_port=blocked_port)
    store = RuntimeStateStore(config)
    inventory = EndpointInventory(config)

    try:
        server = RuntimeApiServer(config, store, inventory)
        with pytest.raises(RuntimeApiServerError) as error:
            server.start()
        assert error.value.reason == "port_in_use"
        assert config.legacy_api_port == 9225
    finally:
        blocker.close()
