from __future__ import annotations

import json
import re
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
    dashboard_payload,
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
    assert html.count('class="w-[300px] shrink-0') == 5
    positions = [html.index(f'data-lane="{lane}"') for lane in ("RUNNING", "BLOCKED", "PAUSED", "DONE", "STOPPED")]
    assert positions == sorted(positions)
    assert "grid grid-cols-1 lg:grid-cols-12" in html
    assert html.count("lg:col-span-3") >= 2
    assert "lg:col-span-6" in html
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
    monkeypatch.setattr(dashboard_module, "ThreadingHTTPServer", FakeServer)

    dashboard_module.serve_dashboard(
        host="127.0.0.1",
        port=9224,
        cdp_url="http://127.0.0.1:9222",
        event_log=tmp_path / "events.jsonl",
        poll_seconds=0.5,
        reconnect_seconds=2.0,
    )

    assert events == ["thread-start", "serve:0.25", "server-close", "thread-join:3"]
