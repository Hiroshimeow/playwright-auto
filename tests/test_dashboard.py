from __future__ import annotations

import json
import re
import socket
import struct
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import playwright_auto.dashboard as dashboard_module
from playwright_auto.dashboard import (
    DASHBOARD_HTML_PATH,
    build_dashboard_page,
    build_task_payload,
    build_task_timeline,
    dashboard_payload,
    effective_activity_at,
    primary_task_problem,
)
from playwright_auto.observability import (
    append_action_event,
    configure_action_event_log,
    read_recent_action_events,
)


def test_build_dashboard_page_exposes_live_button_matrix():
    page = build_dashboard_page(
        {
            "url": "https://chatgpt.com/c/session-1",
            "title": "DEV · task-1",
            "page_id": "page-1",
            "role": "DEV",
            "task_id": "task-1",
            "composer_present": True,
            "composer_editable": True,
            "composer_text": "draft prompt",
            "requires_login": False,
            "error_present": False,
            "last_message_role": "assistant",
            "message_count": 4,
            "assistant_count": 2,
            "user_count": 2,
            "buttons": {
                "new_chat": {"visible": True, "enabled": True},
                "send": {"visible": True, "enabled": True},
                "stop": {"visible": False, "enabled": False},
                "delete_confirm": {"visible": False, "enabled": False},
            },
            "dialogs": [],
        }
    )

    assert page["state"] == "draft"
    assert page["identity"] == "DEV · page-1"
    assert page["buttons"]["send"] == {"visible": True, "enabled": True}
    assert page["buttons"]["stop"] == {"visible": False, "enabled": False}
    assert page["message_count"] == 4


def test_dashboard_payload_is_sorted_by_role_then_page_id():
    pages = [
        {"role": "REVIEW", "page_id": "page-b", "state": "waiting_prompt"},
        {"role": "DEV", "page_id": "page-a", "state": "responding"},
    ]
    payload = dashboard_payload(
        connected=True,
        cdp_url="http://127.0.0.1:9222",
        pages=pages,
        events=[{"action": "send", "phase": "complete"}],
        error=None,
    )

    assert [page["role"] for page in payload["pages"]] == ["DEV", "REVIEW"]
    assert payload["connected"] is True
    assert payload["page_count"] == 2
    assert payload["events"][0]["action"] == "send"


def test_action_event_log_is_jsonl_and_tail_limited(tmp_path: Path):
    path = tmp_path / "actions.jsonl"
    configure_action_event_log(path)
    for index in range(4):
        append_action_event(
            "send",
            "delay" if index == 0 else "complete",
            page_url=f"https://chatgpt.com/c/{index}",
            delay_seconds=3.5 if index == 0 else None,
            detail=str(index),
        )

    events = read_recent_action_events(path, limit=2)
    assert [event["detail"] for event in events] == ["2", "3"]
    assert all("at" in event and "at_epoch" in event for event in events)
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["delay_seconds"] == 3.5


def test_dashboard_html_is_cdpa_control_center_tailwind_monitor_surface():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    assert "<title>CDPA Control Center</title>" in html
    assert ">CDPA Control Center<" in html
    assert "CDPA Kanban" not in html
    assert 'src="https://cdn.tailwindcss.com"' in html
    for token in (
        "dark: '#0f141e'", "panel: '#151b28'", "borderDark: '#262f3f'",
        "accent: '#059669'", "danger: '#e11d48'", "warning: '#d97706'",
        "textDim: '#94a3b8'",
    ):
        assert token in html
    assert 'data-testid="sidebar"' in html and "w-20" in html
    assert 'data-testid="header"' in html and "h-14" in html
    assert 'data-testid="workspace"' in html
    assert "flex-1 min-h-0 overflow-y-auto overflow-x-hidden" in html
    assert 'data-testid="kanban-scroller"' in html and "overflow-x-auto" in html
    assert html.count('class="w-[300px] shrink-0') == 6
    positions = [html.index(f'data-lane="{lane}"') for lane in ("RUNNING", "WAITING", "BLOCKED", "PAUSED", "DONE", "STOPPED")]
    assert positions == sorted(positions)
    assert 'id="primary-problem-section"' in html
    assert 'id="maintenance-section"' in html
    assert 'id="dependency-summary"' in html
    assert 'id="selected-reports"' in html
    assert 'id="task-card-template"' in html
    assert "progress" not in html.casefold()
    for region in ("logs-content", "role-table-body", "controls-content", "tab-grid", "history-list"):
        assert f'id="{region}"' in html
    assert "setInterval(refreshDashboard, 1000);" in html


def test_blank_accessibility_alert_does_not_mark_page_as_error():
    page = build_dashboard_page(
        {
            "composer_present": True,
            "composer_text": "",
            "last_message_role": "assistant",
            "retry_visible": False,
            "error_texts": [""],
            "buttons": {"stop": {"visible": False, "enabled": False}},
        }
    )
    assert page["state"] == "waiting_prompt"


def test_retry_button_or_meaningful_error_text_marks_page_as_error():
    retry_page = build_dashboard_page(
        {
            "composer_present": True,
            "retry_visible": True,
            "error_texts": [],
            "buttons": {"stop": {"visible": False, "enabled": False}},
        }
    )
    alert_page = build_dashboard_page(
        {
            "composer_present": True,
            "retry_visible": False,
            "error_texts": ["Something went wrong. Try again."],
            "buttons": {"stop": {"visible": False, "enabled": False}},
        }
    )
    assert retry_page["state"] == "error"
    assert alert_page["state"] == "error"


def test_dashboard_tailwind_has_no_inline_or_javascript_style_mutation():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    assert len(re.findall(r"<style(?:\s|>)", html)) == 1
    assert not re.search(r"\sstyle\s*=", html, re.IGNORECASE)
    assert ".style." not in html
    assert "setAttribute('style'" not in html
    assert 'setAttribute("style"' not in html
    assert "location.reload" not in html
    assert ".innerHTML =" not in html
    assert "replaceChildren(" not in html
    assert "opacity-50" not in html


def test_live_polling_uses_keyed_incremental_dom_updates_and_stable_targets():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    for token in (
        "function upsertTaskCard", "function renderSelectedTask", "function renderRoles",
        "function renderLogs", "function renderTabs", "function renderHistory",
        "selectedTaskId", "selectedRoleByTask", "removeStale",
    ):
        assert token in html
    assert "page.role === role.physical_role" in html
    assert "page.team === task.team" in html
    assert "page.task_id === task.task_id" in html
    assert "event.task_id === task.task_id" in html
    assert "surface === 'history'" in html
    assert "surface === 'offline_recoverable'" in html
    assert "INBOX" in html and "RUNNING" in html


def test_dashboard_modal_explicitly_supports_real_create_and_resume_endpoints():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    assert 'id="create-task-form"' not in html
    assert 'id="task-dialog"' in html
    dialog_tag = re.search(r'<dialog\b[^>]*id="task-dialog"[^>]*>', html)
    assert dialog_tag and not re.search(r"\bopen(?:\s|=|>)", dialog_tag.group(0))
    for element_id in ("open-create", "open-resume", "mode-create", "mode-resume"):
        assert f'id="{element_id}"' in html
    assert "'/api/tasks'" in html
    assert "'/api/tasks/resume'" in html
    for element_id in ("create-task-input", "create-repository-input", "create-team-input", "resume-team-input"):
        assert f'id="{element_id}"' in html
    assert "Fresh conversation on first use" in html
    assert "Fresh conversation for every role on first use" in html
    assert "new_roles" in html and "new_all" in html
    assert "dialog.showModal()" in html
    assert "dialog.close()" in html


def test_superseded_agent_flow_product_and_dashboard_bridge_are_removed():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "src/playwright_auto/agent_cli.py",
        "src/playwright_auto/agent_flow.py",
        "tests/test_agent_cli.py",
        "tests/test_agent_flow.py",
    ):
        assert not (root / relative).exists()
    product_text = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "pyproject.toml",
            "README.md",
            "scripts/dashboard-start.sh",
            "src/playwright_auto/dashboard.py",
            "src/playwright_auto/dashboard.html",
            "src/playwright_auto/__init__.py",
        )
    )
    for forbidden in (
        "playwright-agent-flow",
        "AGENT_FLOW_STATE",
        "agent-flow-state.json",
        "--run-state",
        "build_flow_payload",
        "read_agent_run_state",
        "controller-id",
        "run-id",
        "TRANSPORT_METADATA",
    ):
        assert forbidden not in product_text


def _surface_task(*, cleanup_state: str = "CLEARED"):
    return {
        "task_id": "task-cleared",
        "task_text": "Cleared task",
        "task_slug": "cleared-task",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-cleared/cleared-task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "STOPPED",
        "kanban_column": "DONE_STOPPED",
        "active_role": None,
        "active_hop_id": None,
        "cleanup": {"state": cleanup_state},
        "roles": {
            "PLAN": {
                "physical_role": "alpha-plan",
                "status": "cleared",
                "turn": 1,
                "online": False,
            }
        },
        "hops": [],
        "reports": [],
        "route_timeline": [],
        "errors": [],
        "controls": [],
        "options": {},
    }


def test_cleared_task_with_assigned_tab_stays_recoverable_with_warning():
    payload = build_task_payload(
        _surface_task(),
        pages=[{"role": "alpha-plan", "team": "alpha", "task_id": "task-cleared"}],
        connected=True,
    )
    assert payload["surface"] == "offline_recoverable"
    assert payload["availability"] == "cleared_tab_present"
    assert "run Clear Team again" in payload["surface_warning"]


def test_cleared_task_without_assigned_tab_is_history_only():
    payload = build_task_payload(_surface_task(), pages=[], connected=True)
    assert payload["surface"] == "history"
    assert payload["availability"] == "terminal"
    assert payload["surface_warning"] is None


def test_ordinary_terminal_task_remains_recoverable_when_cdp_is_disconnected():
    payload = build_task_payload(
        _surface_task(cleanup_state="ACTIVE"),
        pages=[],
        connected=False,
    )
    assert payload["surface"] == "offline_recoverable"
    assert payload["availability"] == "unknown"
    assert payload["surface_warning"] is None


@pytest.mark.parametrize("disconnect_error", [BrokenPipeError, ConnectionResetError])
def test_dashboard_response_write_ignores_client_disconnect(disconnect_error):
    handler_type = dashboard_module._handler(
        dashboard_module.DashboardStore("http://127.0.0.1:9222"),
        b"dashboard",
    )
    handler = handler_type.__new__(handler_type)
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    class DisconnectedWriter:
        def write(self, _body):
            raise disconnect_error("client disconnected")

    handler.wfile = DisconnectedWriter()
    handler._send(200, "text/plain", b"ok")


@pytest.mark.parametrize("disconnect_error", [BrokenPipeError, ConnectionResetError])
def test_dashboard_request_processing_ignores_client_disconnect(disconnect_error, capsys):
    server = dashboard_module.DashboardHTTPServer.__new__(
        dashboard_module.DashboardHTTPServer
    )
    try:
        raise disconnect_error("client disconnected while reading headers")
    except disconnect_error:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_dashboard_read_side_reset_does_not_log_traceback(capsys):
    handler = dashboard_module._handler(
        dashboard_module.DashboardStore("http://127.0.0.1:9222"),
        b"dashboard",
    )
    server = dashboard_module.DashboardHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=3)
    client.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_LINGER,
        struct.pack("ii", 1, 0),
    )
    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
    client.close()
    thread.join(timeout=3)
    server.server_close()

    assert not thread.is_alive()
    assert capsys.readouterr().err == ""


def test_dashboard_request_processing_reports_unexpected_errors(capsys):
    server = dashboard_module.DashboardHTTPServer.__new__(
        dashboard_module.DashboardHTTPServer
    )
    try:
        raise RuntimeError("unexpected request failure")
    except RuntimeError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "RuntimeError: unexpected request failure" in capsys.readouterr().err


def test_dashboard_main_uses_config_runtime_defaults_and_explicit_overrides(
    tmp_path: Path,
    monkeypatch,
):
    from test_cdpa_core import write_config

    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["dashboard"].update(
        {
            "url": "http://127.0.0.1:9334",
            "port": 9334,
            "poll_seconds": 7,
        }
    )
    raw["browser"]["cdp_url"] = "http://127.0.0.1:9332"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    captured: list[dict[str, object]] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        dashboard_module,
        "serve_dashboard",
        lambda **kwargs: captured.append(kwargs),
    )

    monkeypatch.setattr(sys, "argv", ["dashboard", "--config", str(config_path)])
    assert dashboard_module.main() == 0
    assert captured[-1]["port"] == 9334
    assert captured[-1]["cdp_url"] == "http://127.0.0.1:9332"
    assert captured[-1]["poll_seconds"] == 7

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dashboard",
            "--config",
            str(config_path),
            "--port",
            "9444",
            "--cdp",
            "http://127.0.0.1:9442",
            "--poll",
            "2.5",
        ],
    )
    assert dashboard_module.main() == 0
    assert captured[-1]["port"] == 9444
    assert captured[-1]["cdp_url"] == "http://127.0.0.1:9442"
    assert captured[-1]["poll_seconds"] == 2.5


def test_dashboard_html_poll_interval_uses_runtime_value():
    html = dashboard_module._dashboard_html(7).decode("utf-8")
    assert "setInterval(refreshDashboard, 7000);" in html
    assert "setInterval(refreshDashboard, 1000);" not in html


def test_dashboard_main_fails_closed_on_missing_config(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dashboard", "--config", "missing.json"])
    monkeypatch.setattr(
        dashboard_module,
        "serve_dashboard",
        lambda **_kwargs: pytest.fail("dashboard must not serve with invalid config"),
    )

    assert dashboard_module.main() == 2
    assert "missing CDPA config" in capsys.readouterr().err


def test_health_fails_when_task_store_is_unavailable():
    store = dashboard_module.DashboardStore("http://127.0.0.1:9222")
    handler = dashboard_module._handler(store, b"dashboard", task_store=None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload == {
            "ok": False,
            "task_store_ready": False,
            "cdp_connected": False,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_shutdown_handles_keyboard_interrupt_cleanly(monkeypatch, tmp_path: Path):
    events: list[str] = []

    class FakeThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            events.append("thread-start")

        def join(self, timeout=None):
            events.append(f"thread-join:{timeout}")

    class FakeServer:
        def __init__(self, _address, _handler):
            self.daemon_threads = False

        def serve_forever(self, poll_interval=0.25):
            events.append(f"serve:{poll_interval}")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("server-close")

    monkeypatch.setattr(dashboard_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(dashboard_module, "DashboardHTTPServer", FakeServer)

    dashboard_module.serve_dashboard(
        host="127.0.0.1",
        port=9224,
        cdp_url="http://127.0.0.1:9222",
        event_log=tmp_path / "events.jsonl",
        poll_seconds=0.5,
        reconnect_seconds=2.0,
    )

    assert events == ["thread-start", "serve:0.25", "server-close", "thread-join:3"]



def _mixed_timeline_task() -> dict[str, object]:
    return {
        "task_id": "task-timeline",
        "team": "alpha",
        "status": "BLOCKED",
        "block_code": "route_validation_exhausted",
        "block_reason": "Report file does not exist",
        "active_role": "PLAN",
        "active_hop_id": 4,
        "created_at": "2026-07-23T01:00:00+00:00",
        "updated_at": "2026-07-23T02:10:00+00:00",
        "last_role_activity_at": "2026-07-23T02:08:00+00:00",
        "errors": [
            {"at": "2026-07-23T01:20:00+00:00", "error": "old transport error"},
            {"at": "2026-07-23T02:09:04+00:00", "error": "Report file does not exist"},
        ],
        "hops": [
            {
                "hop_id": 4,
                "target_role": "PLAN",
                "state": "waiting",
                "timestamps": {
                    "created_at": "2026-07-23T02:00:00+00:00",
                    "sent_at": "2026-07-23T02:04:00+00:00",
                },
                "errors": ["Report file does not exist"],
            }
        ],
        "route_timeline": [
            {
                "hop_id": 3,
                "at": "2026-07-23T01:50:00+00:00",
                "source_role": "DEV",
                "route": "PLAN",
                "kind": "route",
            }
        ],
        "controls": [
            {
                "control_id": "control-1",
                "action": "resume",
                "status": "applied",
                "at": "2026-07-23T01:55:00+00:00",
            }
        ],
        "maintenance": {
            "active_incident_id": "maint-1",
            "incidents": [
                {
                    "incident_id": "maint-1",
                    "state": "RUNNING",
                    "trigger_code": "route_validation_exhausted",
                    "trigger_reason": "Report file does not exist",
                    "source_role": "PLAN",
                    "source_hop_id": 4,
                    "created_at": "2026-07-23T02:09:10+00:00",
                    "updated_at": "2026-07-23T02:09:30+00:00",
                    "report_path": "/repo/.plan/maintainers/alpha_turn1_20260723T020920Z.md",
                    "report_sha256": "a" * 64,
                    "report_size": 120,
                    "turn": 1,
                }
            ],
        },
        "dependency_events": [
            {
                "event_id": "dep-1",
                "at": "2026-07-23T01:40:00+00:00",
                "message": "Waiting for task-parent",
            }
        ],
    }


def test_timeline_orders_all_sources_by_real_timestamp_descending():
    timeline = build_task_timeline(_mixed_timeline_task())
    assert [item["at"] for item in timeline] == sorted(
        [item["at"] for item in timeline], reverse=True
    )
    assert timeline[0]["level"] == "MAINTENANCE"
    assert {item["level"] for item in timeline} >= {
        "ERROR", "STATE", "ROUTE", "CONTROL", "MAINTENANCE", "DEPENDENCY"
    }
    assert all(set(item) == {"key", "at", "level", "source", "message"} for item in timeline)


def test_primary_problem_prefers_active_block_over_old_errors():
    problem = primary_task_problem(_mixed_timeline_task())
    assert problem is not None
    assert problem["code"] == "route_validation_exhausted"
    assert problem["role"] == "PLAN"
    assert problem["hop_id"] == 4
    assert problem["message"] == "Report file does not exist"
    assert problem["maintenance_incident_id"] == "maint-1"


def test_effective_activity_uses_state_specific_real_evidence():
    blocked = _mixed_timeline_task()
    waiting = {
        "status": "WAITING",
        "updated_at": "2026-07-23T04:00:00+00:00",
        "dependency_events": [
            {"at": "2026-07-23T03:00:00+00:00", "message": "parent incomplete"}
        ],
    }
    done = {"status": "DONE", "completed_at": "2026-07-23T05:00:00+00:00", "updated_at": "2026-07-23T06:00:00+00:00"}
    stopped = {"status": "STOPPED", "stopped_at": "2026-07-23T07:00:00+00:00", "updated_at": "2026-07-23T08:00:00+00:00"}
    running = {"status": "RUNNING", "last_role_activity_at": "2026-07-23T09:00:00+00:00", "updated_at": "2026-07-23T10:00:00+00:00"}

    assert effective_activity_at(blocked) == "2026-07-23T02:09:30+00:00"
    assert effective_activity_at(waiting) == "2026-07-23T03:00:00+00:00"
    assert effective_activity_at(done) == done["completed_at"]
    assert effective_activity_at(stopped) == stopped["stopped_at"]
    assert effective_activity_at(running) == running["last_role_activity_at"]


def test_task_payload_exposes_error_first_projections_and_maintenance_report():
    raw = _mixed_timeline_task()
    raw.update({
        "task_text": "Timeline task",
        "task_slug": "timeline-task",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-timeline/timeline-task.json",
        "team_suffix": 1,
        "roles": {},
        "reports": [],
        "cleanup": {},
        "options": {},
    })
    payload = build_task_payload(raw)

    assert payload["timeline"] == build_task_timeline(raw)
    assert payload["primary_problem"]["code"] == "route_validation_exhausted"
    assert payload["effective_activity_at"] == "2026-07-23T02:09:30+00:00"
    assert payload["latest_maintenance_report"] == {
        "incident_id": "maint-1",
        "state": "RUNNING",
        "turn": 1,
        "path": "/repo/.plan/maintainers/alpha_turn1_20260723T020920Z.md",
        "url": "/api/maintenance-reports/task-timeline/maint-1",
        "at": "2026-07-23T02:09:30+00:00",
    }


def test_malformed_maintenance_projection_surfaces_without_mutating_or_recursing():
    raw = {
        "task_id": "task-bad-maintenance",
        "task_text": "Bad maintenance projection",
        "task_slug": "bad-maintenance-projection",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-bad-maintenance/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "RUNNING",
        "updated_at": "2026-07-23T10:00:00+00:00",
        "roles": {},
        "hops": [],
        "reports": [],
        "route_timeline": [],
        "errors": [],
        "controls": [],
        "cleanup": {},
        "options": {},
        "maintenance": {"active_incident_id": "missing", "incidents": "broken"},
    }
    before = json.loads(json.dumps(raw))

    payload = build_task_payload(raw)

    assert payload["projection_errors"] == ["maintenance.incidents must be a list"]
    assert payload["primary_problem"]["code"] == "dashboard_projection_error"
    assert raw == before


def test_terminal_task_never_promotes_historical_error_to_primary_problem():
    raw = _mixed_timeline_task()
    raw["status"] = "DONE"
    raw["completed_at"] = "2026-07-23T03:00:00+00:00"
    assert primary_task_problem(raw) is None


def test_timeline_uses_requested_and_applied_control_timestamps():
    raw = {
        "created_at": "2026-07-23T01:00:00+00:00",
        "controls": [
            {
                "control_id": "control-2",
                "action": "resume",
                "status": "applied",
                "requested_at": "2026-07-23T01:01:00+00:00",
                "applied_at": "2026-07-23T01:02:00+00:00",
            }
        ],
    }
    controls = [item for item in build_task_timeline(raw) if item["level"] == "CONTROL"]
    assert [(item["key"], item["at"]) for item in controls] == [
        ("control:control-2:applied", "2026-07-23T01:02:00+00:00"),
        ("control:control-2:requested", "2026-07-23T01:01:00+00:00"),
    ]


def test_active_block_timestamp_outranks_newer_unrelated_historical_error():
    raw = _mixed_timeline_task()
    raw["errors"].append(
        {"at": "2026-07-23T03:00:00+00:00", "error": "unrelated later history"}
    )
    problem = primary_task_problem(raw)
    assert problem is not None
    assert problem["at"] == "2026-07-23T02:09:30+00:00"


def test_timeline_sorts_timezone_offsets_by_instant_not_text():
    raw = {
        "errors": [
            {"at": "2026-07-23T02:00:00+09:00", "error": "earlier instant"},
            {"at": "2026-07-23T00:30:00+00:00", "error": "later instant"},
        ]
    }
    errors = [item for item in build_task_timeline(raw) if item["level"] == "ERROR"]
    assert [item["message"] for item in errors] == ["later instant", "earlier instant"]


def test_malformed_maintenance_report_evidence_is_explicit_projection_error():
    raw = {
        "status": "RUNNING",
        "updated_at": "2026-07-23T04:00:00+00:00",
        "maintenance": {
            "active_incident_id": None,
            "incidents": [
                {
                    "incident_id": "maint-bad-report",
                    "state": "RESOLVED",
                    "report_path": "/repo/.plan/maintainers/bad.md",
                    "report_sha256": "short",
                    "report_size": "invalid",
                }
            ],
        },
    }
    payload = build_task_payload(raw)
    assert payload["primary_problem"]["code"] == "dashboard_projection_error"
    assert any("report_sha256" in item for item in payload["projection_errors"])
    assert any("report_size" in item for item in payload["projection_errors"])


def test_route_timeline_keys_are_unique_for_distinct_events_on_same_hop():
    raw = {
        "route_timeline": [
            {
                "hop_id": 7,
                "at": "2026-07-23T01:00:00+00:00",
                "source_role": "DEV",
                "route": "DEV",
                "kind": "route_repair",
            },
            {
                "hop_id": 7,
                "at": "2026-07-23T01:01:00+00:00",
                "source_role": "DEV",
                "route": "TEST",
                "kind": "route",
            },
        ]
    }

    routes = [item for item in build_task_timeline(raw) if item["level"] == "ROUTE"]

    assert len(routes) == 2
    assert len({item["key"] for item in routes}) == 2
    assert {item["message"] for item in routes} == {
        "DEV → DEV · route_repair",
        "DEV → TEST · route",
    }


def _active_report_provenance_task(status: str, *, active_has_report: bool) -> dict[str, object]:
    raw = _mixed_timeline_task()
    raw["status"] = status
    raw["task_id"] = f"task-{status.lower()}-provenance"
    raw["maintenance"] = {
        "active_incident_id": "maint-new",
        "incidents": [
            {
                "incident_id": "maint-old",
                "state": "RESOLVED",
                "turn": 1,
                "created_at": "2026-07-23T00:00:00+00:00",
                "updated_at": "2026-07-23T00:05:00+00:00",
                "resolved_at": "2026-07-23T00:05:00+00:00",
                "report_path": "/repo/.plan/maintainers/old.md",
                "report_sha256": "a" * 64,
                "report_size": 10,
            },
            {
                "incident_id": "maint-new",
                "state": "OPEN",
                "turn": 2,
                "trigger_code": "dependency_wait" if status == "WAITING" else "role_offline",
                "trigger_reason": "Waiting for parent" if status == "WAITING" else "Exact role is offline",
                "created_at": "2026-07-23T01:00:00+00:00",
                "updated_at": "2026-07-23T01:01:00+00:00",
                **(
                    {
                        "report_path": "/repo/.plan/maintainers/new.md",
                        "report_sha256": "b" * 64,
                        "report_size": 20,
                    }
                    if active_has_report
                    else {}
                ),
            },
        ],
    }
    if status == "WAITING":
        raw["waiting_reason"] = "Waiting for parent"
        raw["dependency_events"] = [
            {"at": "2026-07-23T01:02:00+00:00", "message": "Waiting for parent"}
        ]
    return raw


@pytest.mark.parametrize("status", ["BLOCKED", "WAITING"])
def test_active_problem_never_links_historical_report_from_another_incident(status: str):
    raw = _active_report_provenance_task(status, active_has_report=False)

    problem = primary_task_problem(raw)
    payload = build_task_payload(raw)

    assert problem is not None
    assert problem["maintenance_incident_id"] == "maint-new"
    assert problem["maintenance_report"] is None
    assert payload["active_maintenance_report"] is None
    assert payload["latest_maintenance_report"]["incident_id"] == "maint-old"


@pytest.mark.parametrize("status", ["BLOCKED", "WAITING"])
def test_active_problem_links_only_its_own_report(status: str):
    raw = _active_report_provenance_task(status, active_has_report=True)

    problem = primary_task_problem(raw)
    payload = build_task_payload(raw)

    assert problem is not None
    assert problem["maintenance_incident_id"] == "maint-new"
    assert problem["maintenance_report"]["incident_id"] == "maint-new"
    assert problem["maintenance_report"]["url"].endswith("/maint-new")
    assert payload["active_maintenance_report"] == problem["maintenance_report"]


def test_equal_timestamp_errors_and_dependency_events_keep_unique_timeline_keys():
    at = "2026-07-23T01:00:00+00:00"
    raw = {
        "errors": [
            {"at": at, "error": "first error"},
            {"at": at, "error": "second error"},
        ],
        "dependency_events": [
            {"at": at, "message": "first dependency"},
            {"at": at, "message": "second dependency"},
        ],
    }

    timeline = build_task_timeline(raw)

    assert len(timeline) == 4
    assert len({item["key"] for item in timeline}) == 4
    assert {item["message"] for item in timeline} == {
        "first error",
        "second error",
        "first dependency",
        "second dependency",
    }


def test_action_event_identity_is_persisted_and_legacy_tail_ids_stay_stable(tmp_path: Path):
    path = tmp_path / "actions.jsonl"
    configure_action_event_log(path)

    first = append_action_event("send", "complete", detail="first")
    second = append_action_event("send", "complete", detail="second")

    assert first["event_id"]
    assert second["event_id"]
    assert first["event_id"] != second["event_id"]
    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [item["event_id"] for item in persisted] == [first["event_id"], second["event_id"]]

    legacy = [
        {
            "at": "2026-07-23T01:00:00+00:00",
            "task_id": "task-rolling",
            "action": "send",
            "phase": "complete",
            "detail": f"legacy-{index:03d}",
        }
        for index in range(161)
    ]
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in legacy[:160]),
        encoding="utf-8",
    )
    before = read_recent_action_events(path, limit=160)
    before_ids = {item["detail"]: item["event_id"] for item in before}

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy[160], sort_keys=True) + "\n")
    after = read_recent_action_events(path, limit=160)
    after_ids = {item["detail"]: item["event_id"] for item in after}

    assert before_ids["legacy-150"] == after_ids["legacy-150"]
    assert len({item["event_id"] for item in before}) == 160
    assert len({item["event_id"] for item in after}) == 160


def test_maintenance_creation_row_never_back_projects_final_state():
    raw = {
        "maintenance": {
            "active_incident_id": None,
            "incidents": [
                {
                    "incident_id": "maint-history",
                    "state": "RESOLVED",
                    "trigger_code": "role_offline",
                    "trigger_reason": "offline",
                    "created_at": "2026-07-23T01:00:00+00:00",
                    "updated_at": "2026-07-23T01:10:00+00:00",
                    "resolved_at": "2026-07-23T01:10:00+00:00",
                    "decision": {"action": "OPEN_ROLE_TAB"},
                }
            ],
        }
    }

    maintenance = [
        item for item in build_task_timeline(raw) if item["level"] == "MAINTENANCE"
    ]

    assert [(item["key"], item["at"], item["message"]) for item in maintenance] == [
        (
            "maintenance:maint-history:updated",
            "2026-07-23T01:10:00+00:00",
            "OPEN_ROLE_TAB · role_offline · offline",
        ),
        (
            "maintenance:maint-history:resolved",
            "2026-07-23T01:10:00+00:00",
            "Resolved · role_offline",
        ),
        (
            "maintenance:maint-history:created",
            "2026-07-23T01:00:00+00:00",
            "Opened · role_offline · offline",
        ),
    ]
    assert len({item["key"] for item in maintenance}) == 3
    assert "RESOLVED" not in maintenance[-1]["message"]


def test_dependency_projection_derives_parent_child_status_without_mirrored_state():
    from playwright_auto.dashboard import build_task_payload

    parent = {
        "task_id": "task-parent",
        "task_title": "Parent",
        "task_text": "Parent",
        "team": "parent",
        "team_suffix": 1,
        "status": "STOPPED",
        "created_at": "2026-07-23T01:00:00+00:00",
        "updated_at": "2026-07-23T01:01:00+00:00",
        "roles": {},
        "hops": [],
        "reports": [],
        "controls": [],
        "route_timeline": [],
        "errors": [],
        "depends_on_task_ids": [],
        "dependency_events": [],
    }
    child = {
        "task_id": "task-child",
        "task_title": "Child",
        "task_text": "Child",
        "team": "child",
        "team_suffix": 1,
        "status": "WAITING",
        "kanban_column": "WAITING",
        "active_action": "waiting_dependency",
        "waiting_reason": "Waiting for dependencies: task-parent, task-missing",
        "waiting_code": "dependency_missing",
        "waiting": {
            "reason": "dependency",
            "waiting_on": ["task-parent"],
            "stopped": ["task-parent"],
            "missing": ["task-missing"],
            "since": "2026-07-23T01:02:00+00:00",
        },
        "created_at": "2026-07-23T01:02:00+00:00",
        "updated_at": "2026-07-23T01:02:00+00:00",
        "roles": {},
        "hops": [],
        "reports": [],
        "controls": [],
        "route_timeline": [],
        "errors": [],
        "depends_on_task_ids": ["task-parent", "task-missing"],
        "dependency_events": [{
            "at": "2026-07-23T01:02:00+00:00",
            "status": "WAITING",
            "message": "Waiting for dependencies",
        }],
    }
    tasks = [parent, child]

    child_payload = build_task_payload(child, tasks=tasks)
    parent_payload = build_task_payload(parent, tasks=tasks)

    assert child_payload["parents"] == [
        {"task_id": "task-parent", "status": "STOPPED", "team": "parent"},
        {"task_id": "task-missing", "status": "MISSING", "team": None},
    ]
    assert child_payload["children"] == []
    assert child_payload["stopped_dependency_task_ids"] == ["task-parent"]
    assert child_payload["missing_dependency_task_ids"] == ["task-missing"]
    assert child_payload["primary_problem"]["code"] == "dependency_missing"
    assert parent_payload["child_task_ids"] == ["task-child"]
    assert parent_payload["children"] == [
        {"task_id": "task-child", "status": "WAITING", "team": "child"}
    ]
    assert "child_task_ids" not in parent


def test_replacement_provenance_suppresses_frozen_parent_active_maintenance_projection():
    from playwright_auto.dashboard import build_task_payload

    incident = {
        "incident_id": "maint-1",
        "state": "RUNNING",
        "turn": 2,
        "request_id": "maint-1-turn2",
        "decision": {
            "action": "REPLACE_TASK",
            "reason": "replace",
            "role": None,
            "lesson": None,
            "replacement": {
                "target_task_id": "task-parent",
                "task": "continue",
                "reuse_team": True,
                "rewire_children": True,
            },
        },
        "report_path": "/repo/.plan/maintainers/parent_turn2_report.md",
        "report_sha256": "a" * 64,
        "report_size": 12,
    }
    parent = {
        "task_id": "task-parent",
        "task_title": "Parent",
        "task_text": "Parent",
        "team": "parent",
        "team_suffix": 1,
        "status": "STOPPED",
        "created_at": "2026-07-23T01:00:00+00:00",
        "updated_at": "2026-07-23T01:01:00+00:00",
        "roles": {
            "AUDIT": {
                "physical_role": "parent-audit",
                "status": "offline",
                "turn": 1,
                "online": False,
            }
        },
        "hops": [],
        "reports": [],
        "controls": [],
        "route_timeline": [],
        "errors": [],
        "depends_on_task_ids": [],
        "dependency_events": [],
        "maintenance": {
            "active_incident_id": "maint-1",
            "incidents": [incident],
        },
    }
    replacement = {
        **parent,
        "task_id": "task-replacement",
        "task_title": "Replacement",
        "task_text": "Replacement",
        "status": "INBOX",
        "replaces_task_id": "task-parent",
        "replacement_incident_id": "maint-1",
        "maintenance": None,
    }

    observed_old_page = {
        "role": "parent-audit",
        "team": "parent",
        "task_id": "task-parent",
    }
    connected_payload = build_task_payload(
        parent,
        tasks=[parent, replacement],
        pages=[observed_old_page],
        connected=True,
    )
    disconnected_payload = build_task_payload(
        parent,
        tasks=[parent, replacement],
        pages=[],
        connected=False,
    )

    for payload in (connected_payload, disconnected_payload):
        assert payload["active_maintenance_incident"] is None
        assert payload["active_maintenance_report"] is None
        assert payload["latest_maintenance_report"]["incident_id"] == "maint-1"
        assert payload["maintenance_replacement_task_id"] == "task-replacement"
        assert payload["surface"] == "history"
        assert payload["availability"] == "terminal"
        assert payload["surface_warning"] is None
        assert payload["immutable_history"] is True
        assert payload["controls"] == []


def test_task_payload_sanitizes_attachments_and_exposes_role_generation():
    from playwright_auto.dashboard import build_task_payload

    raw = {
        "task_id": "task-attachment-payload",
        "task_title": "Attachment payload",
        "task_text": "Attachment payload",
        "task_slug": "attachment-payload",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task/attachment-payload.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "INBOX",
        "created_at": "2026-07-24T00:00:00+00:00",
        "updated_at": "2026-07-24T00:00:00+00:00",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "roles": {
            "PLAN": {
                "physical_role": "alpha-plan",
                "status": "pending",
                "turn": 0,
                "online": False,
                "conversation_generation": 2,
                "attachments_uploaded_generation": 1,
            }
        },
        "hops": [{"hop_id": 1}],
        "reports": [],
        "controls": [],
        "route_timeline": [],
        "errors": [],
        "depends_on_task_ids": [],
        "attachments": [{
            "path": "/private/context.txt",
            "name": "context.txt",
            "size": 7,
            "sha256": "a" * 64,
            "mime_type": "text/plain",
        }],
    }

    payload = build_task_payload(raw, tasks=[raw])

    assert payload["attachments"] == [{
        "name": "context.txt",
        "size": 7,
        "sha256_prefix": "a" * 12,
        "mime_type": "text/plain",
    }]
    assert payload["roles"][0]["attachments_uploaded_generation"] == 1
    assert "/private/context.txt" not in json.dumps(payload)


def test_dashboard_projects_repair_relationship_gate_priority_and_release():
    repair = _surface_task(cleanup_state="ACTIVE")
    repair.update(
        task_id="repair-1",
        team="cdpa-repair",
        status="RUNNING",
        kanban_column="WORKING",
        cleanup={"state": "ACTIVE"},
    )
    repair["priority"] = "urgent_repair"
    repair["repair"] = {
        "kind": "cdpa_repair",
        "priority": "urgent",
        "root_cause_key": "root-role-offline",
        "affected_task_id": "task-affected",
        "affected_team": "alpha",
        "disposition": "HOLD_FOR_REPAIR",
        "reason": "exact recovery postcondition failed",
    }
    affected = _surface_task(cleanup_state="ACTIVE")
    affected.update(
        task_id="task-affected",
        team="alpha",
        status="WAITING",
        kanban_column="WAITING",
        cleanup={"state": "ACTIVE"},
    )
    affected["depends_on_task_ids"] = ["repair-1"]
    affected["repair_wait"] = {
        "repair_task_id": "repair-1",
        "repair_team": "cdpa-repair",
        "root_cause_key": "root-role-offline",
        "disposition": "HOLD_FOR_REPAIR",
        "blocker": "exact recovery postcondition failed",
        "state": "WAITING",
        "release_event": None,
    }

    tasks = [affected, repair]
    affected_payload = build_task_payload(affected, tasks=tasks)
    repair_payload = build_task_payload(repair, tasks=tasks)

    assert affected_payload["repair_relationship"] == {
        "repair_task_id": "repair-1",
        "repair_team": "cdpa-repair",
        "repair_status": "RUNNING",
        "repair_priority": "urgent",
        "affected_task_id": "task-affected",
        "affected_team": "alpha",
        "affected_status": "WAITING",
        "disposition": "HOLD_FOR_REPAIR",
        "blocker": "exact recovery postcondition failed",
        "dependency_gate": True,
        "release_event": None,
        "root_cause_key": "root-role-offline",
    }
    assert repair_payload["repair_relationship"]["affected_task_id"] == "task-affected"
    assert repair_payload["repair_relationship"]["repair_priority"] == "urgent"

    affected["depends_on_task_ids"] = []
    affected["status"] = "RUNNING"
    affected["repair_wait"].update(
        state="RELEASED",
        release_event="repair DONE released preserved waiting hop",
    )
    released = build_task_payload(affected, tasks=[affected, repair])
    assert released["repair_relationship"]["dependency_gate"] is False
    assert released["repair_relationship"]["release_event"] == (
        "repair DONE released preserved waiting hop"
    )


def test_dashboard_projects_all_affected_operations_for_shared_repair():
    repair = _surface_task(cleanup_state="ACTIVE")
    repair.update(
        task_id="repair-shared",
        team="cdpa-repair",
        status="RUNNING",
        kanban_column="WORKING",
        cleanup={"state": "ACTIVE"},
        priority="urgent_repair",
        repair={
            "kind": "cdpa_repair",
            "priority": "urgent",
            "root_cause_key": "root-shared",
            "affected_task_id": "task-a",
            "affected_team": "alpha",
            "disposition": "HOLD_FOR_REPAIR",
            "reason": "shared defect",
            "affected_operations": [
                {
                    "affected_task_id": "task-a",
                    "affected_team": "alpha",
                    "incident_id": "maint-a",
                    "disposition": "HOLD_FOR_REPAIR",
                    "reason": "shared defect",
                },
                {
                    "affected_task_id": "task-b",
                    "affected_team": "beta",
                    "incident_id": "maint-b",
                    "disposition": "CONTINUE_IN_PARALLEL",
                    "reason": "shared defect",
                },
            ],
        },
    )
    task_a = _surface_task(cleanup_state="ACTIVE")
    task_a.update(task_id="task-a", team="alpha", status="WAITING")
    task_b = _surface_task(cleanup_state="ACTIVE")
    task_b.update(task_id="task-b", team="beta", status="RUNNING")

    payload = build_task_payload(repair, tasks=[repair, task_a, task_b])

    assert [
        (item["affected_task_id"], item["affected_status"], item["disposition"])
        for item in payload["repair_relationships"]
    ] == [
        ("task-a", "WAITING", "HOLD_FOR_REPAIR"),
        ("task-b", "RUNNING", "CONTINUE_IN_PARALLEL"),
    ]


def test_dashboard_html_renders_complete_repair_relationships():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")

    assert 'id="repair-summary"' in html
    assert 'id="repair-content"' in html
    assert "function renderRepairRelationships(task)" in html
    assert "task.repair_relationships" in html
    assert "renderRepairRelationships(task);" in html
    for label in (
        "Repair task",
        "Affected task",
        "Disposition",
        "Blocker",
        "Dependency gate",
        "Release event",
    ):
        assert label in html


def test_dashboard_projects_allowlisted_maintenance_summary_without_credentials_or_prompt():
    secret = "review-secret-dashboard-token"
    raw = {
        "task_id": "task-dashboard-secret",
        "task_text": "safe task",
        "task_slug": "task-dashboard-secret",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-dashboard-secret/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": "safe block",
        "updated_at": "2026-07-25T05:00:00+00:00",
        "roles": {},
        "hops": [],
        "reports": [],
        "route_timeline": [],
        "errors": [],
        "controls": [],
        "cleanup": {},
        "options": {},
        "maintenance": {
            "active_incident_id": "maint-secret",
            "incidents": [
                {
                    "incident_id": "maint-secret",
                    "state": "RUNNING",
                    "turn": 1,
                    "trigger_status": "BLOCKED",
                    "trigger_code": "unexpected_error",
                    "trigger_reason": f"token={secret}",
                    "created_at": "2026-07-25T05:00:00+00:00",
                    "updated_at": "2026-07-25T05:01:00+00:00",
                    "prompt": f"full maintainer prompt with {secret}",
                    "environment_last_error": f"access_token={secret}",
                    "environment_evidence": [{"raw": secret}],
                    "environment_signature": {
                        "version": 3,
                        "prerequisite": "network",
                        "available": False,
                        "network_available": False,
                        "network_evidence": {
                            "endpoint": f"https://api.invalid/run?access_token={secret}",
                            "method": "HEAD",
                            "status": None,
                            "error": f"Bearer {secret}",
                        },
                    },
                    "last_error": f"Bearer {secret}",
                }
            ],
        },
    }

    payload = build_task_payload(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert secret not in serialized
    incident = payload["active_maintenance_incident"]
    assert incident is not None
    assert "prompt" not in incident
    assert "environment_evidence" not in incident
    assert set(incident).issuperset(
        {
            "incident_id",
            "state",
            "turn",
            "trigger_status",
            "trigger_code",
            "trigger_reason",
            "environment_last_error",
            "environment_signature",
        }
    )
    assert all(secret not in item["message"] for item in payload["timeline"])


def test_dashboard_defense_in_depth_sanitizes_all_operational_error_surfaces():
    secret = "dashboard-operational-secret-token"
    sensitive = (
        f"RuntimeError: GET https://api.invalid/webhooks/{secret}/status failed; "
        f"Authorization: Bearer {secret}; access_token={secret}"
    )
    raw = {
        "task_id": "task-dashboard-operational-secret",
        "task_text": "safe task text",
        "task_slug": "task-dashboard-operational-secret",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-dashboard-operational-secret/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": sensitive,
        "waiting_reason": sensitive,
        "pause_reason": sensitive,
        "stop_reason": sensitive,
        "active_role": "DEV",
        "active_hop_id": 1,
        "active_action": "blocked",
        "updated_at": "2026-07-25T06:35:00+00:00",
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "status": "offline",
                "turn": 1,
                "page_url": f"https://chatgpt.com/webhooks/{secret}/status",
                "last_error": sensitive,
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "timestamps": {"created_at": "2026-07-25T06:34:00+00:00"},
                "errors": [sensitive],
                "validation_error": sensitive,
                "wait": {"refresh_error": sensitive},
            }
        ],
        "reports": [],
        "route_timeline": [
            {
                "at": "2026-07-25T06:34:30+00:00",
                "source_role": "DEV",
                "route": "DEV",
                "kind": "route_repair",
                "error": sensitive,
            }
        ],
        "errors": [{"at": "2026-07-25T06:35:00+00:00", "error": sensitive}],
        "controls": [
            {
                "control_id": 1,
                "action": "resume",
                "status": "rejected",
                "requested_at": "2026-07-25T06:34:20+00:00",
                "applied_at": "2026-07-25T06:34:40+00:00",
                "result": {"error": sensitive},
            }
        ],
        "cleanup": {"state": "CLEARING", "last_error": sensitive},
        "options": {},
    }

    payload = build_task_payload(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert all(secret not in item["message"] for item in payload["timeline"])
    assert secret not in payload["primary_problem"]["message"]


@pytest.mark.parametrize(
    "path_template",
    (
        "/webhooks/incoming/{secret}/status",
        "/oauth/callback/{secret}/complete",
        "/password-reset/confirm/{secret}",
        "/capability/v1/{secret}/run",
        "/magic-link/callback/{secret}",
        "/password-reset-link/{secret}",
        "/signed-url/{secret}/result",
    ),
)
def test_dashboard_defense_in_depth_redacts_multi_segment_high_risk_paths(
    path_template: str,
):
    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.invalid" + path_template.format(secret=secret)
    sensitive = f"RuntimeError: GET {url} failed"
    raw = {
        "task_id": "task-dashboard-multi-segment-secret",
        "task_text": "safe task text",
        "task_slug": "task-dashboard-multi-segment-secret",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-dashboard-multi-segment-secret/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": sensitive,
        "active_role": "DEV",
        "active_hop_id": 1,
        "active_action": "blocked",
        "updated_at": "2026-07-25T07:15:00+00:00",
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "status": "offline",
                "turn": 1,
                "page_url": url,
                "last_error": sensitive,
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "timestamps": {"created_at": "2026-07-25T07:14:00+00:00"},
                "errors": [sensitive],
                "validation_error": sensitive,
                "wait": {"last_refresh_result": {"error": sensitive}},
            }
        ],
        "reports": [],
        "route_timeline": [
            {
                "at": "2026-07-25T07:14:30+00:00",
                "source_role": "DEV",
                "route": "DEV",
                "kind": "route_repair",
                "error": sensitive,
            }
        ],
        "errors": [{"at": "2026-07-25T07:15:00+00:00", "error": sensitive}],
        "controls": [
            {
                "control_id": 1,
                "action": "resume",
                "status": "rejected",
                "requested_at": "2026-07-25T07:14:20+00:00",
                "applied_at": "2026-07-25T07:14:40+00:00",
                "result": {"error": sensitive},
            }
        ],
        "cleanup": {"state": "CLEARING", "last_error": sensitive},
        "options": {},
    }

    payload = build_task_payload(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert all(secret not in item["message"] for item in payload["timeline"])
    assert secret not in payload["primary_problem"]["message"]


@pytest.mark.parametrize(
    "path_template",
    (
        "/run/token-{secret}/status",
        "/run/secret_{secret}/status",
        "/run/CREDENTIAL-{secret}/status",
        "/run/signature-{secret}/status",
        "/run/session-{secret}/status",
        "/run/jwt-{secret}/status",
        "/run/api-key-{secret}/status",
        "/run/auth-{secret}/status",
        "/run/authorization-{secret}/status",
        "/run/code-{secret}/status",
        "/run/key-{secret}/status",
        "/run/signed-{secret}/status",
        "/run/token%2D{secret}/status",
    ),
)
def test_dashboard_defense_in_depth_redacts_direct_marker_value_segment(
    path_template: str,
):
    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.invalid" + path_template.format(secret=secret)
    sensitive = f"RuntimeError: GET {url} failed"
    raw = {
        "task_id": "task-dashboard-direct-marker-value",
        "task_text": "safe task text",
        "task_slug": "task-dashboard-direct-marker-value",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-dashboard-direct-marker-value/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": sensitive,
        "active_role": "DEV",
        "active_hop_id": 1,
        "active_action": "blocked",
        "updated_at": "2026-07-25T07:40:00+00:00",
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "status": "offline",
                "turn": 1,
                "page_url": url,
                "last_error": sensitive,
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "timestamps": {"created_at": "2026-07-25T07:39:00+00:00"},
                "errors": [sensitive],
                "validation_error": sensitive,
                "wait": {"last_refresh_result": {"error": sensitive}},
            }
        ],
        "reports": [],
        "route_timeline": [
            {
                "at": "2026-07-25T07:39:30+00:00",
                "source_role": "DEV",
                "route": "DEV",
                "kind": "route_repair",
                "error": sensitive,
            }
        ],
        "errors": [{"at": "2026-07-25T07:40:00+00:00", "error": sensitive}],
        "controls": [
            {
                "control_id": 1,
                "action": "resume",
                "status": "rejected",
                "requested_at": "2026-07-25T07:39:20+00:00",
                "applied_at": "2026-07-25T07:39:40+00:00",
                "result": {"error": sensitive},
            }
        ],
        "cleanup": {"state": "CLEARING", "last_error": sensitive},
        "options": {},
    }

    payload = build_task_payload(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert all(secret not in item["message"] for item in payload["timeline"])
    assert secret not in payload["primary_problem"]["message"]


@pytest.mark.parametrize(
    "path_template",
    (
        "/oauth2/callback/{secret}/complete",
        "/oauthCallback/{secret}/complete",
        "/oauthcallback/{secret}/complete",
        "/oauth2Callback/{secret}/complete",
        "/oauth2callback/{secret}/complete",
        "/signedUrl/{secret}/result",
        "/signedurl/{secret}/result",
        "/magicLink/{secret}/complete",
        "/magiclink/{secret}/complete",
        "/webhookIncoming/{secret}/status",
        "/webhookincoming/{secret}/status",
        "/authorizationCallback/{secret}/complete",
        "/authorizationcallback/{secret}/complete",
        "/apiKey/{secret}/status",
        "/apikey/{secret}/status",
        "/accessToken/{secret}/status",
        "/accesstoken/{secret}/status",
        "/sessionId/{secret}/status",
        "/sessionid/{secret}/status",
        "/passwordResetLink/{secret}",
        "/passwordresetlink/{secret}",
        "/resetPassword/{secret}/complete",
        "/resetpassword/{secret}/complete",
        "/refreshtoken/{secret}/status",
        "/idtoken/{secret}/status",
        "/apitoken/{secret}/status",
        "/clientsecret/{secret}/status",
        "/clientcredential/{secret}/status",
        "/bearertoken/{secret}/status",
        "/authtoken/{secret}/status",
        "/sessiontoken/{secret}/status",
        "/csrftoken/{secret}/status",
        "/verificationcode/{secret}/status",
        "/activationcode/{secret}/status",
        "/invitecode/{secret}/status",
        "/resetcode/{secret}/status",
        "/passwordreset/{secret}/complete",
        "/magiclinkcallback/{secret}/complete",
        "/signedurlcallback/{secret}/complete",
        "/webhooksincoming/{secret}/status",
        "/webhookcallback/{secret}/status",
        "/oauthredirect/{secret}/complete",
        "/oauth2redirect/{secret}/complete",
        "/refreshToken/{secret}/status",
        "/idToken/{secret}/status",
        "/apiToken/{secret}/status",
        "/clientSecret/{secret}/status",
        "/clientCredential/{secret}/status",
        "/bearerToken/{secret}/status",
        "/authToken/{secret}/status",
        "/sessionToken/{secret}/status",
        "/csrfToken/{secret}/status",
        "/verificationCode/{secret}/status",
        "/activationCode/{secret}/status",
        "/inviteCode/{secret}/status",
        "/resetCode/{secret}/status",
        "/passwordReset/{secret}/complete",
        "/magicLinkCallback/{secret}/complete",
        "/signedUrlCallback/{secret}/complete",
        "/webhooksIncoming/{secret}/status",
        "/webhookCallback/{secret}/status",
        "/oauthRedirect/{secret}/complete",
        "/oauth2Redirect/{secret}/complete",
        "/RefreshToken/{secret}/status",
        "/IDToken/{secret}/status",
        "/APIToken/{secret}/status",
        "/ClientSecret/{secret}/status",
        "/CSRFToken/{secret}/status",
        "/VerificationCode/{secret}/status",
        "/MagicLinkCallback/{secret}/complete",
        "/SignedURLCallback/{secret}/complete",
        "/OAuth2Redirect/{secret}/complete",
    ),
)
def test_dashboard_redacts_canonicalized_high_risk_route_from_legacy_state(
    path_template: str,
):
    secret = "K7p4Q9Lm3Vx8"
    url = "https://api.invalid" + path_template.format(secret=secret)
    sensitive = f"RuntimeError: GET {url} failed"
    raw = {
        "task_id": "task-dashboard-canonicalized-route",
        "task_text": "safe task text",
        "task_slug": "task-dashboard-canonicalized-route",
        "repository": "/repo",
        "manifest_path": "/repo/.plan/alpha/task-dashboard-canonicalized-route/task.json",
        "team": "alpha",
        "team_suffix": 1,
        "status": "BLOCKED",
        "block_code": "unexpected_error",
        "block_reason": sensitive,
        "active_role": "DEV",
        "active_hop_id": 1,
        "active_action": "blocked",
        "updated_at": "2026-07-25T08:10:00+00:00",
        "roles": {
            "DEV": {
                "physical_role": "alpha-dev",
                "status": "offline",
                "turn": 1,
                "page_url": url,
                "last_error": sensitive,
            }
        },
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "timestamps": {"created_at": "2026-07-25T08:09:00+00:00"},
                "errors": [sensitive],
                "validation_error": sensitive,
                "wait": {"last_refresh_result": {"error": sensitive}},
            }
        ],
        "reports": [],
        "route_timeline": [
            {
                "at": "2026-07-25T08:09:30+00:00",
                "source_role": "DEV",
                "route": "DEV",
                "kind": "route_repair",
                "error": sensitive,
            }
        ],
        "errors": [{"at": "2026-07-25T08:10:00+00:00", "error": sensitive}],
        "controls": [
            {
                "control_id": 1,
                "action": "resume",
                "status": "rejected",
                "requested_at": "2026-07-25T08:09:20+00:00",
                "applied_at": "2026-07-25T08:09:40+00:00",
                "result": {"error": sensitive},
            }
        ],
        "cleanup": {"state": "CLEARING", "last_error": sensitive},
        "options": {},
    }

    payload = build_task_payload(raw)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert all(secret not in item["message"] for item in payload["timeline"])
    assert secret not in payload["primary_problem"]["message"]
