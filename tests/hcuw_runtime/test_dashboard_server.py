from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

from playwright_auto.hcuw_runtime.config import load_runtime_config
from playwright_auto.hcuw_runtime.dashboard_server import DashboardResponse, DashboardServer


def response_text(response: DashboardResponse) -> str:
    return response.body.decode("utf-8")


def test_dashboard_index_has_read_only_audit_panels(tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))

    response = dashboard.handle_get_path("/")
    text = response_text(response)

    assert response.status == 200
    assert "HCUW Runtime V2 Audit" in text
    assert "API status" in text
    assert "Runtime config" in text
    assert "Runs" in text
    assert "Endpoints" in text
    assert "Diagnostics" in text
    assert "Realtime Observation Audit" in text
    assert "DOM source" in text
    assert "Mutation source" in text
    assert "LISTEN source" in text
    assert "Actions" in text
    assert "/api/audit/snapshot" in text
    assert "Refresh" in text
    assert "create" not in text.lower()


def test_dashboard_proxy_returns_api_response(monkeypatch, tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))

    def fake_fetch(path: str) -> bytes:
        assert path == "/api/health"
        return json.dumps({"ok": True, "data": {"status": "ok"}, "diagnostics": []}).encode("utf-8")

    monkeypatch.setattr(dashboard, "_fetch_api_bytes", fake_fetch)

    response = dashboard.handle_get_path("/api/health")

    assert response.status == 200
    assert json.loads(response.body.decode("utf-8"))["ok"] is True


def test_dashboard_proxy_unavailable_is_structured(monkeypatch, tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))

    def fail_fetch(path: str) -> bytes:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(dashboard, "_fetch_api_bytes", fail_fetch)

    response = dashboard.handle_get_path("/api/health")
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status == 503
    assert payload["ok"] is False
    assert payload["error"]["code"] == "api_unavailable"


def test_dashboard_routes_do_not_create_runtime_state(tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))

    dashboard.handle_get_path("/")

    assert not (tmp_path / ".runtime-v2").exists()
    assert not (tmp_path / ".runtime").exists()


def test_dashboard_unknown_route_is_404(tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))

    response = dashboard.handle_get_path("/missing")
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status == 404
    assert payload["error"]["code"] == "dashboard_route_not_found"


def test_dashboard_team_task_form_and_task_table_show_roles_and_block_state(tmp_path: Path) -> None:
    text = response_text(DashboardServer(load_runtime_config(repository_root=tmp_path)).handle_get_path("/"))

    assert 'id="team-roles" value="DEV,REVIEW"' in text
    assert 'id="route-targets"' in text
    assert 'id="start-role"' in text
    assert 'id="closing-role"' in text
    assert "must include PLAN or REVIEW" not in text
    assert "payload.start_role = startRole" in text
    assert "payload.closing_role = closingRole" in text
    assert "payload.route_targets = routeTargets" in text
    for header in ("Roles", "Start role", "Status", "Block code", "Updated"):
        assert f"<th>{header}</th>" in text
    assert "item.latest ? 'current' : 'older'" in text
    assert "document.visibilityState === 'visible' && document.hasFocus()" in text
    assert "window.setInterval(lazyRefreshTick, 500)" in text


def test_dashboard_post_proxy_preserves_api_validation_error(monkeypatch, tmp_path: Path) -> None:
    dashboard = DashboardServer(load_runtime_config(repository_root=tmp_path))
    body = json.dumps({"ok": False, "error": {"code": "invalid_task_request"}}).encode("utf-8")

    def reject(path: str, payload: bytes) -> bytes:
        raise urllib.error.HTTPError(path, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(dashboard, "_post_api_bytes", reject)
    response = dashboard.proxy_post_api_request("/api/tasks", b"{}")

    assert response.status == 400
    assert json.loads(response.body.decode("utf-8"))["error"]["code"] == "invalid_task_request"
