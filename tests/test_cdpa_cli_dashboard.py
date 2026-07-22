from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

import playwright_auto.cdpa_cli as cdpa_cli_module
import playwright_auto.dashboard as dashboard_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_cli import main as cdpa_main
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.dashboard import (
    DASHBOARD_HTML_PATH,
    DashboardStore,
    _busy_role_suffixes,
    _handler,
    build_task_payload,
)

from test_cdpa_core import write_config


class FakeWorkerActions:
    async def acquire(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=True,
            new_chat=False,
        )


def start_dashboard_server(
    config_path: Path,
    repository: Path,
    *,
    html: bytes = b"dashboard",
):
    config = load_cdpa_config(config_path, repository_root=repository)
    tasks = TaskStore(config)
    live = DashboardStore(config.cdp_url)
    handler = _handler(live, html, task_store=tasks)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["dashboard"]["url"] = f"http://127.0.0.1:{server.server_port}"
    raw["dashboard"]["port"] = server.server_port
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return server, thread, tasks


def test_dashboard_and_cli_share_configured_endpoint(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
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
    served: dict[str, object] = {}
    submitted: dict[str, object] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        dashboard_module,
        "serve_dashboard",
        lambda **kwargs: served.update(kwargs),
    )
    monkeypatch.setattr(sys, "argv", ["dashboard", "--config", str(config_path)])
    assert dashboard_module.main() == 0

    def fake_submit(config, **_kwargs):
        submitted["dashboard_url"] = config.dashboard_url
        return {
            "task_id": "task-config",
            "team": "alpha",
            "manifest_path": str(tmp_path / ".plan" / "task.json"),
        }

    monkeypatch.setattr(cdpa_cli_module, "submit_task", fake_submit)
    assert cdpa_cli_module.main(
        [
            "configured endpoint",
            "--config",
            str(config_path),
            "--repository",
            str(tmp_path),
        ]
    ) == 0

    assert submitted["dashboard_url"] == f"http://127.0.0.1:{served['port']}"
    assert served["cdp_url"] == "http://127.0.0.1:9332"
    assert served["poll_seconds"] == 7
    assert "dashboard=http://127.0.0.1:9334" in capsys.readouterr().out


def _ui_task(
    task_id: str,
    status: str,
    *,
    surface: str = "active",
    retryable: bool = False,
    availability: str = "online",
    cleanup_state: str = "ACTIVE",
    warning: str | None = None,
):
    return {
        "task_id": task_id,
        "task_title": f"{task_id} title",
        "task_text": f"Full text for {task_id}",
        "repository": "/repo",
        "manifest_path": f"/repo/.plan/team-{task_id}/{task_id}/task.json",
        "team": f"team-{task_id}",
        "status": status,
        "column": "WORKING" if status in {"INBOX", "RUNNING"} else status,
        "surface": surface,
        "availability": availability,
        "surface_warning": warning,
        "active_role": "DEV" if status not in {"DONE", "STOPPED"} else None,
        "active_hop_id": f"hop-{task_id}" if status not in {"DONE", "STOPPED"} else None,
        "active_hop": {"hop_id": f"hop-{task_id}", "state": "wait_response"} if status not in {"DONE", "STOPPED"} else None,
        "active_action": "wait_response" if status not in {"DONE", "STOPPED"} else None,
        "block_retryable": retryable,
        "block_reason": "manual recovery required" if status == "BLOCKED" and not retryable else "retry transport" if status == "BLOCKED" else None,
        "block_code": "manual" if status == "BLOCKED" and not retryable else "transport" if status == "BLOCKED" else None,
        "pause_reason": "paused by user" if status == "PAUSED" else None,
        "stop_reason": "stopped by user" if status == "STOPPED" else None,
        "cleanup": {"state": cleanup_state},
        "roles": [
            {"logical_role": "PLAN", "physical_role": f"team-{task_id}-plan", "status": "waiting", "turn": 1, "page_id": None, "page_url": "", "conversation_generation": 0, "last_activity_at": "2026-07-22T14:00:00+00:00"},
            {"logical_role": "DEV", "physical_role": f"team-{task_id}-dev", "status": "running" if status not in {"DONE", "STOPPED"} else "done", "turn": 2, "page_id": f"page-{task_id}", "page_url": f"https://chatgpt.com/c/{task_id}", "conversation_generation": 1, "last_activity_at": "2026-07-22T14:10:00+00:00"},
        ],
        "reports": [{"report_id": "1", "physical_role": "PLAN", "turn": 1, "url": f"/api/reports/{task_id}/1", "path": "report.md"}],
        "route_timeline": [{"hop_id": f"route-{task_id}", "at": "2026-07-22T14:05:00+00:00", "source_role": "PLAN", "route": "DEV", "kind": "route"}],
        "errors": [{"at": "2026-07-22T14:06:00+00:00", "error": f"error-{task_id}"}] if task_id == "running" else [],
        "control_results": [{"control_id": f"control-{task_id}", "action": "pause", "status": "applied", "at": "2026-07-22T14:07:00+00:00"}] if task_id == "running" else [],
        "controls": ["pause", "resume", "retry", "stop", "restart_role", "open_tab", "new_chat", "route_plan", "clear_team"],
    }


def _ui_payloads():
    tasks = [
        _ui_task("running", "RUNNING"),
        _ui_task("inbox", "INBOX", availability="offline", surface="offline_recoverable"),
        _ui_task("blocked-retry", "BLOCKED", retryable=True),
        _ui_task("blocked-manual", "BLOCKED"),
        _ui_task("paused", "PAUSED"),
        _ui_task("done", "DONE", surface="offline_recoverable", availability="terminal_tab_present", warning="Terminal task still has an assigned role tab; clear the team to close it."),
        _ui_task("stopped", "STOPPED", surface="offline_recoverable", availability="terminal_tab_present", warning="Terminal task still has an assigned role tab; clear the team to close it."),
        _ui_task("cleared", "STOPPED", surface="offline_recoverable", availability="cleared_tab_present", cleanup_state="CLEARED", warning="Cleanup is marked CLEARED but an assigned role tab is still present; run Clear Team again."),
        _ui_task("history", "DONE", surface="history", availability="terminal"),
    ]
    pages = [
        {"page_id": "page-running", "role": "team-running-dev", "team": "team-running", "task_id": "running", "identity": "team-running-dev · page-running", "url": "https://chatgpt.com/c/running", "state": "waiting_prompt", "composer_preview": "", "message_count": 8},
        {"page_id": "page-free", "role": None, "team": None, "task_id": None, "identity": "UNASSIGNED · page-free", "url": "https://chatgpt.com/c/free", "state": "draft", "composer_preview": "manual draft", "message_count": 2},
    ]
    state = {
        "connected": True,
        "updated_at": "2026-07-22T14:20:00+00:00",
        "page_count": len(pages),
        "pages": pages,
        "events": [
            {"event_id": "matching", "task_id": "running", "role": "team-running-dev", "action": "send", "phase": "complete", "detail": "matching event", "at": "2026-07-22T14:08:00+00:00"},
            {"event_id": "other", "task_id": "paused", "role": "team-paused-dev", "action": "send", "phase": "complete", "detail": "must not leak", "at": "2026-07-22T14:09:00+00:00"},
        ],
        "error": None,
    }
    return {"tasks": tasks, "repository": "/repo", "errors": []}, state


def test_v3_dashboard_state_mapping_controls_logs_tabs_and_poll_stability(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(config_path, tmp_path, html=DASHBOARD_HTML_PATH.read_bytes())
    tasks_payload, state_payload = _ui_payloads()
    captured: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))
            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(tasks_payload))
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(state_payload))
                elif request.method == "POST" and "/controls" in request.url:
                    captured.append(json.loads(request.post_data or "{}"))
                    route.fulfill(status=202, content_type="application/json", body=json.dumps({"status": "requested"}))
                else:
                    route.continue_()
            page.route("**/api/**", api)
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="domcontentloaded")
            page.wait_for_selector('[data-task-id="running"]')
            assert page.locator('[data-lane-cards="RUNNING"] [data-task-id]').count() == 2
            assert page.locator('[data-lane-cards="BLOCKED"] [data-task-id]').count() == 2
            assert page.locator('[data-lane-cards="PAUSED"] [data-task-id]').count() == 1
            assert page.locator('[data-lane-cards="DONE"] [data-task-id]').count() == 1
            assert page.locator('[data-lane-cards="STOPPED"] [data-task-id]').count() == 2
            assert page.locator('[data-task-id="history"]').count() == 0
            assert page.locator('[data-history-task-id="history"]').count() == 1
            assert page.locator('[data-history-task-id="done"]').count() == 1
            board = page.locator('[data-testid="kanban-scroller"]')
            workspace = page.locator('[data-testid="workspace"]')
            board.evaluate("node => { node.scrollLeft = 320; }")
            workspace.evaluate("node => { node.scrollTop = 200; }")
            page.wait_for_timeout(1200)
            assert board.evaluate("node => node.scrollLeft") >= 300
            assert workspace.evaluate("node => node.scrollTop") >= 180
            page.locator('[data-task-id="running"]').click()
            assert page.locator('#task-control-target').text_content().endswith("running")
            assert page.locator('#task-primary [data-action="pause"]').count() == 1
            assert page.locator('#task-primary [data-action="resume"]').count() == 0
            assert page.locator('#task-primary [data-action="retry"]').count() == 0
            assert page.locator('#task-secondary [data-action="resume"]').count() == 1
            assert page.locator('#task-secondary [data-action="stop"]').count() == 1
            more = page.locator('[data-testid="more-actions"]')
            assert not more.get_attribute("open")
            assert "error-running" in page.locator("#logs-content").text_content()
            assert "matching event" in page.locator("#logs-content").text_content()
            assert "must not leak" not in page.locator("#logs-content").text_content()
            page.locator('[data-role="DEV"]').click()
            task_target = page.locator("#task-control-target").text_content()
            role_target = page.locator("#role-control-target").text_content()
            page.wait_for_timeout(2200)
            assert page.locator("#task-control-target").text_content() == task_target
            assert page.locator("#role-control-target").text_content() == role_target
            page.locator('#role-actions [data-action="restart_role"]').click()
            page.wait_for_timeout(100)
            assert captured[-1] == {"action": "restart_role", "role": "DEV"}
            page.locator('[data-task-id="paused"]').click()
            assert page.locator('#task-primary [data-action="resume"]').count() == 1
            page.locator('[data-task-id="blocked-retry"]').click()
            assert page.locator('#task-primary [data-action="retry"]').count() == 1
            assert page.locator('#task-secondary [data-action="resume"]').count() == 1
            page.locator('[data-task-id="blocked-manual"]').click()
            assert page.locator('#blocked-guidance').is_visible()
            assert page.locator('#task-primary [data-action="resume"]').count() == 1
            assert page.locator('#task-primary [data-action="retry"]').count() == 0
            page.locator('[data-task-id="done"]').click()
            assert page.locator('#controls-content [data-action]').count() == 1
            assert page.locator('#controls-content [data-action="clear_team"]').text_content() == "Re-verify cleanup"
            page.locator('[data-history-task-id="history"] button').click()
            assert page.locator('#controls-content [data-action]').count() == 0
            page.locator('[data-task-id="cleared"]').click()
            assert page.locator('#controls-content [data-action]').count() == 1
            assert page.locator('#controls-content [data-action="clear_team"]').text_content() == "Re-verify cleanup"
            page.locator('[data-task-id="running"]').click()
            more = page.locator('[data-testid="more-actions"]')
            more.locator("summary").click()
            page.wait_for_timeout(1200)
            assert more.get_attribute("open") is not None
            more.locator('[data-action="clear_team"]').click()
            page.wait_for_timeout(1200)
            assert page.locator("#clear-confirmation").is_visible()
            page.locator('#clear-confirmation [data-confirm-clear]').click()
            page.wait_for_timeout(100)
            assert captured[-1] == {"action": "clear_team", "confirmed": True}
            assert page.locator('[data-page-id="page-running"]').count() == 1
            assert "manual draft" in page.locator('[data-page-id="page-free"]').text_content()
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_v3_create_resume_modal_posts_real_modes_and_preserves_drafts_during_polls(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(config_path, tmp_path, html=DASHBOARD_HTML_PATH.read_bytes())
    tasks_payload, state_payload = _ui_payloads()
    captured: list[tuple[str, dict[str, object]]] = []
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on("console", lambda message: errors.append(f"console:{message.type}:{message.text}") if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))
            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(tasks_payload))
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(state_payload))
                elif request.method == "POST" and request.url.endswith("/api/tasks/resume"):
                    captured.append(("resume", json.loads(request.post_data or "{}")))
                    route.fulfill(status=202, content_type="application/json", body=json.dumps({"task_id": "running", "team": "team-running", "manifest_path": "/repo/task.json"}))
                elif request.method == "POST" and request.url.endswith("/api/tasks"):
                    captured.append(("create", json.loads(request.post_data or "{}")))
                    route.fulfill(status=201, content_type="application/json", body=json.dumps({"task_id": "created", "team": "new-team", "manifest_path": "/repo/new.json"}))
                else:
                    route.continue_()
            page.route("**/api/**", api)
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="domcontentloaded")
            page.wait_for_selector('[data-task-id="running"]')
            create_opener = page.locator("#open-create")
            create_opener.click()
            dialog = page.locator("#task-dialog")
            assert dialog.get_attribute("open") is not None
            assert page.locator("#create-mode-panel").is_visible()
            assert page.locator("#resume-mode-panel").is_hidden()
            page.locator("#create-task-input").fill("Create via V3")
            page.locator("#create-team-input").fill("new-team")
            page.locator("#create-repository-input").fill("/repo")
            page.locator('input[name="new_role"][value="PLAN"]').check()
            page.locator("#new-all-input").check()
            page.wait_for_timeout(2200)
            assert page.locator("#create-task-input").input_value() == "Create via V3"
            assert page.locator("#create-team-input").input_value() == "new-team"
            page.locator("#create-submit").click()
            page.wait_for_timeout(100)
            assert captured[-1] == ("create", {"task": "Create via V3", "repository": "/repo", "team": "new-team", "new_roles": ["PLAN"], "new_all": True})
            assert "Created created" in page.locator("#dialog-result").text_content()
            page.locator("#dialog-close").click()
            assert not dialog.get_attribute("open")
            assert page.evaluate("document.activeElement.id") == "open-create"
            resume_opener = page.locator("#open-resume")
            resume_opener.click()
            assert page.locator("#resume-mode-panel").is_visible()
            assert page.locator("#create-mode-panel").is_hidden()
            page.locator("#resume-team-input").fill(" exact-team2")
            page.wait_for_timeout(1200)
            assert page.locator("#resume-team-input").input_value() == " exact-team2"
            page.locator("#resume-submit").click()
            page.wait_for_timeout(100)
            assert captured[-1] == ("resume", {"repository": "/repo", "team": " exact-team2"})
            page.keyboard.press("Escape")
            assert not dialog.get_attribute("open")
            assert page.evaluate("document.activeElement.id") == "open-resume"
            resume_opener.click()
            bounds = page.locator("#task-dialog").bounding_box()
            assert bounds is not None
            assert bounds["width"] <= 390 and bounds["height"] <= 844
            page.keyboard.press("Escape")
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cdpa_cli_submits_through_dashboard_and_returns_identity_immediately(tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        code = cdpa_main([
            "build it",
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", "alpha",
            "--new", "plan,dev",
        ])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert code == 0
    out = capsys.readouterr().out
    assert "task_id=" in out
    assert "team=alpha" in out
    assert f"dashboard=http://127.0.0.1:{server.server_port}" in out
    manifests = tasks.discover()
    assert len(manifests) == 1
    assert manifests[0]["options"]["new_roles"] == ["PLAN", "DEV"]


def test_busy_terminal_role_tab_reserves_physical_suffix(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    tasks = TaskStore(config)
    terminal = tasks.create_task(
        "terminal task",
        requested_team="alpha",
        task_id="task-terminal",
    )
    tasks.update(
        terminal["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
        },
    )
    base_page = {
        "role": "alpha-plan",
        "team": "alpha",
        "task_id": "task-terminal",
    }

    assert _busy_role_suffixes(tasks, [{**base_page, "state": "draft"}], team_base="alpha") == (1,)
    assert _busy_role_suffixes(tasks, [{**base_page, "state": "responding"}], team_base="alpha") == (1,)
    assert _busy_role_suffixes(tasks, [{**base_page, "state": "waiting_prompt"}], team_base="alpha") == ()
    assert _busy_role_suffixes(tasks, [{"role": None, "state": "draft"}], team_base="alpha") == ()
    assert _busy_role_suffixes(tasks, [{**base_page, "state": "draft"}], team_base="beta") == ()

    allocated = tasks.create_task(
        "must not reuse unrelated manual draft",
        requested_team="beta",
        task_id="task-next",
        reserved_team_suffixes=_busy_role_suffixes(
            tasks, [{**base_page, "state": "draft"}], team_base="beta"
        ),
    )
    assert allocated["team"] == "beta"
    assert allocated["roles"]["PLAN"]["physical_role"] == "beta-plan"


def test_disconnected_dashboard_reserves_uncleared_terminal_role_suffix(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    tasks = TaskStore(config)
    terminal = tasks.create_task(
        "terminal task",
        requested_team="alpha",
        task_id="task-terminal",
    )
    terminal = tasks.update(
        terminal["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "roles": {
                **state["roles"],
                "PLAN": {
                    **state["roles"]["PLAN"],
                    "page_id": "recorded-page",
                },
            },
        },
    )

    assert _busy_role_suffixes(tasks, [], connected=False, team_base="alpha") == (1,)

    tasks.update(
        terminal["manifest_path"],
        lambda state: {
            **state,
            "cleanup": {**state["cleanup"], "cleared_at": "2026-07-21T00:00:00+00:00"},
        },
    )
    assert _busy_role_suffixes(tasks, [], connected=False, team_base="alpha") == ()


def test_task_payload_has_required_kanban_fields(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    full_title = "full task title " + "x" * 320
    task = TaskStore(config).create_task(full_title, requested_team="alpha", task_id="task-a")
    payload = build_task_payload(task)
    assert payload["column"] == "INBOX"
    assert payload["task_title"] == full_title
    assert payload["active_role"] == "PLAN"
    assert payload["roles"][0]["physical_role"] == "alpha-plan"
    assert payload["controls"] == [
        "pause", "resume", "retry", "stop", "restart_role",
        "open_tab", "new_chat", "route_plan", "clear_team",
    ]


def test_custom_plans_root_drives_manifest_prompt_validation_and_report_download(
    tmp_path: Path,
):
    config_path = write_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["paths"]["plans_root"] = "_plans"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        config = load_cdpa_config(config_path, repository_root=tmp_path)
        state = tasks.create_task(
            "custom plans root",
            requested_team="alpha",
            task_id="task-custom-root",
        )
        assert Path(state["manifest_path"]).is_relative_to(tmp_path / "_plans")

        worker = CDPAWorker(config, store=tasks)
        hop = _active_hop(state)
        asyncio.run(worker._pre_send(state, hop, FakeWorkerActions()))
        expected = "_plans/alpha/alpha-plan_turn1_task-custom-root.md"
        assert hop["expected_report_path"] == expected
        assert expected not in hop["prompt"]
        assert "_plans/<team>/<physical-role>_turn<N>_<task-id>.md" in hop["prompt"]

        report_path = tmp_path / expected
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("custom root evidence", encoding="utf-8")
        hop["response"] = json.dumps({"route": "DEV", "handoff": expected})
        hop["state"] = "responded"
        worker._responded(state, hop)
        assert state["reports"][0]["path"] == str(report_path)
        tasks.save(state["manifest_path"], state)

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/reports/task-custom-root/1")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"custom root evidence"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_report_api_rejects_content_changed_after_validation(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    task = tasks.create_task("report provenance", requested_team="alpha", task_id="task-report")
    report_path = tmp_path / ".plan" / "alpha" / "PLAN_turn1_task-report.md"
    report_path.write_text("validated report", encoding="utf-8")
    report_bytes = report_path.read_bytes()
    tasks.update(
        task["manifest_path"],
        lambda state: {
            **state,
            "reports": [
                {
                    "report_id": 1,
                    "physical_role": "PLAN",
                    "turn": 1,
                    "path": str(report_path),
                    "sha256": hashlib.sha256(report_bytes).hexdigest(),
                    "size": len(report_bytes),
                }
            ],
        },
    )
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/reports/task-report/1")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == report_bytes

        report_path.write_text("tampered report", encoding="utf-8")
        connection.request("GET", "/api/reports/task-report/1")
        changed = connection.getresponse()
        assert changed.status == 409
        assert "provenance" in json.loads(changed.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_task_api_uses_shared_store_and_persists_controls(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(config_path, tmp_path)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        body = json.dumps({
            "task": "from kanban",
            "repository": str(tmp_path),
            "team": "alpha",
            "new_roles": ["DEV"],
            "new_all": False,
        })
        connection.request("POST", "/api/tasks", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        created = json.loads(response.read())
        assert response.status == 201
        assert created["team"] == "alpha"

        connection.request("GET", "/api/tasks")
        listed_response = connection.getresponse()
        listed = json.loads(listed_response.read())
        assert listed_response.status == 200
        assert listed["tasks"][0]["task_id"] == created["task_id"]

        control = json.dumps({"action": "pause", "reason": "manual"})
        connection.request(
            "POST", f"/api/tasks/{created['task_id']}/controls",
            body=control, headers={"Content-Type": "application/json"},
        )
        control_response = connection.getresponse()
        assert control_response.status == 202
        control_state = json.loads(control_response.read())
        assert control_state["controls"][-1]["action"] == "pause"
        assert control_state["controls"][-1]["status"] == "requested"

        invalid = json.dumps({"task": "wrong repo", "repository": str(tmp_path / "other")})
        connection.request("POST", "/api/tasks", body=invalid, headers={"Content-Type": "application/json"})
        invalid_response = connection.getresponse()
        assert invalid_response.status == 400
        assert "must match" in json.loads(invalid_response.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_exact_team_resume_reuses_manifest_and_new_text_allocates_suffix(
    tmp_path: Path,
    capsys,
):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        assert cdpa_main([
            "first task",
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", "alpha",
        ]) == 0
        first_output = capsys.readouterr().out
        task_id = next(
            line.split("=", 1)[1]
            for line in first_output.splitlines()
            if line.startswith("task_id=")
        )
        before = tasks.discover()[0]
        manifest_count = len(tasks.discover_paths())
        catalog_count = len(
            json.loads(tasks.catalog_path.read_text(encoding="utf-8"))["entries"]
        )
        request_id = before["hops"][0]["request_id"]

        assert cdpa_main([
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", "alpha",
        ]) == 0
        resume_output = capsys.readouterr().out
        assert "mode=resumed" in resume_output
        assert f"task_id={task_id}" in resume_output
        assert "team=alpha" in resume_output
        resumed = tasks.discover()[0]
        assert resumed["manifest_path"] == before["manifest_path"]
        assert resumed["active_hop_id"] == before["active_hop_id"]
        assert resumed["hops"][0]["request_id"] == request_id
        assert len(tasks.discover_paths()) == manifest_count
        assert len(
            json.loads(tasks.catalog_path.read_text(encoding="utf-8"))["entries"]
        ) == catalog_count

        assert cdpa_main([
            "second task",
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", "alpha",
        ]) == 0
        create_output = capsys.readouterr().out
        assert "mode=created" in create_output
        assert "team=alpha2" in create_output
        assert sorted(task["team"] for task in tasks.discover()) == ["alpha", "alpha2"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_and_dashboard_resume_long_allocated_exact_team_without_truncation(
    tmp_path: Path,
    capsys,
):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    base = "a" * 48
    first = tasks.create_task("first", requested_team=base, task_id="task-a")
    second = tasks.create_task("second", requested_team=base, task_id="task-b")
    exact_team = second["team"]
    first_manifest = Path(first["manifest_path"])
    second_manifest = Path(second["manifest_path"])
    first_before = first_manifest.read_bytes()
    assert exact_team == f"{base}2"
    assert len(exact_team) == 49

    try:
        assert cdpa_main([
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", exact_team,
        ]) == 0
        output = capsys.readouterr().out
        assert "mode=resumed" in output
        assert "task_id=task-b" in output
        assert f"team={exact_team}" in output
        assert f"manifest={second_manifest.resolve()}" in output

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({"repository": str(tmp_path), "team": exact_team}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 202
        assert payload["task_id"] == "task-b"
        assert payload["team"] == exact_team
        assert payload["manifest_path"] == str(second_manifest.resolve())

        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({"repository": str(tmp_path), "team": f" {exact_team}"}),
            headers={"Content-Type": "application/json"},
        )
        invalid_response = connection.getresponse()
        invalid_payload = json.loads(invalid_response.read())
        assert invalid_response.status == 400
        assert "leading or trailing whitespace" in invalid_payload["error"]

        assert first_manifest.read_bytes() == first_before
        assert json.loads(first_manifest.read_text(encoding="utf-8"))["controls"] == []
        controls = json.loads(second_manifest.read_text(encoding="utf-8"))["controls"]
        assert len(controls) == 1
        assert controls[0]["action"] == "resume"
        assert controls[0]["status"] == "requested"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_taskless_resume_requires_exact_team_and_rejects_creation_flags(
    tmp_path: Path,
    capsys,
):
    config_path = write_config(tmp_path)
    assert cdpa_main([
        "--config", str(config_path),
        "--repository", str(tmp_path),
    ]) == 2
    assert "taskless resume requires --team" in capsys.readouterr().err

    assert cdpa_main([
        "--config", str(config_path),
        "--repository", str(tmp_path),
        "--team", "alpha",
        "--new", "plan",
    ]) == 2
    assert "invalid in taskless resume mode" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        {"new_roles": ["PLAN"]},
        {"new_all": True},
    ],
)
def test_dashboard_resume_rejects_creation_only_options(
    tmp_path: Path,
    extra: dict[str, object],
):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    task = tasks.create_task("first", requested_team="alpha", task_id="task-alpha")
    manifest = Path(task["manifest_path"])
    before = manifest.read_bytes()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps(
                {"repository": str(tmp_path), "team": "alpha", **extra}
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert "invalid when resuming" in payload["error"]
        assert manifest.read_bytes() == before
        assert json.loads(manifest.read_text(encoding="utf-8"))["controls"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_and_dashboard_resume_queue_same_durable_transition(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    first = tasks.create_task("first", requested_team="alpha", task_id="task-alpha")
    second = tasks.create_task("second", requested_team="beta", task_id="task-beta")
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({"repository": str(tmp_path), "team": "alpha"}),
            headers={"Content-Type": "application/json"},
        )
        cli_response = connection.getresponse()
        cli_state = json.loads(cli_response.read())
        assert cli_response.status == 202

        connection.request(
            "POST",
            "/api/tasks/task-beta/controls",
            body=json.dumps({"action": "resume", "role": "PLAN"}),
            headers={"Content-Type": "application/json"},
        )
        dashboard_response = connection.getresponse()
        dashboard_state = json.loads(dashboard_response.read())
        assert dashboard_response.status == 202

        cli_control = cli_state["control_results"][-1]
        dashboard_control = dashboard_state["controls"][-1]
        for control in (cli_control, dashboard_control):
            assert control["action"] == "resume"
            assert control["role"] == "PLAN"
            assert control["reason"] == "resume requested"
            assert control["status"] == "requested"
        assert cli_state["task_id"] == first["task_id"]
        assert dashboard_state["task_id"] == second["task_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
