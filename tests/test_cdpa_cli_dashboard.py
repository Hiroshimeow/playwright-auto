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
    build_task_timeline,
)

from test_cdpa_core import (
    install_duplicate_task_graph,
    poison_catalog_identity_entry,
    write_config,
)


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
    effective_at = {
        "inbox": "2026-07-22T14:30:00+00:00",
        "running": "2026-07-22T14:20:00+00:00",
        "waiting": "2026-07-22T14:19:00+00:00",
        "blocked-manual": "2026-07-22T14:18:00+00:00",
        "blocked-retry": "2026-07-22T14:17:00+00:00",
    }.get(task_id, "2026-07-22T14:10:00+00:00")
    block_reason = "manual recovery required" if status == "BLOCKED" and not retryable else "retry transport" if status == "BLOCKED" else None
    block_code = "manual" if status == "BLOCKED" and not retryable else "transport" if status == "BLOCKED" else None
    maintenance_report = {
        "incident_id": "maint-blocked-manual",
        "state": "RUNNING",
        "turn": 1,
        "path": "/repo/.plan/maintainers/team-blocked-manual_turn1.md",
        "url": "/api/maintenance-reports/blocked-manual/maint-blocked-manual",
        "at": "2026-07-22T14:18:00+00:00",
    } if task_id == "blocked-manual" else None
    active_maintenance = {
        "incident_id": "maint-blocked-manual",
        "state": "RUNNING",
        "trigger_code": block_code,
        "trigger_reason": block_reason,
        "decision": {"action": "OPEN_ROLE_TAB", "reason": "Restore the exact role tab."},
    } if task_id == "blocked-manual" else None
    timeline = [
        {"key": f"route:{task_id}", "at": "2026-07-22T14:05:00+00:00", "level": "ROUTE", "source": "PLAN", "message": "PLAN → DEV · route"},
    ]
    if task_id == "running":
        timeline.extend([
            {"key": "error:running", "at": "2026-07-22T14:06:00+00:00", "level": "ERROR", "source": "DEV", "message": "error-running"},
            {"key": "control:running", "at": "2026-07-22T14:07:00+00:00", "level": "CONTROL", "source": "pause", "message": "pause · applied"},
        ])
    if status == "BLOCKED":
        timeline.append({"key": f"error:{task_id}", "at": effective_at, "level": "ERROR", "source": "DEV", "message": block_reason})
    if status == "WAITING":
        timeline.append({"key": f"dependency:{task_id}", "at": effective_at, "level": "DEPENDENCY", "source": "dependency", "message": "Waiting for task-parent"})
    timeline.sort(key=lambda item: item["at"], reverse=True)
    primary_problem = None
    if status == "BLOCKED":
        primary_problem = {
            "kind": "BLOCKED", "code": block_code, "message": block_reason,
            "role": "DEV", "hop_id": f"hop-{task_id}", "at": effective_at,
            "recommended": "Inspect the active Maintainers incident and resume the task",
            "maintenance_incident_id": active_maintenance["incident_id"] if active_maintenance else None,
            "maintenance_report": maintenance_report,
        }
    elif status == "WAITING":
        primary_problem = {
            "kind": "WAITING", "code": "dependency_wait", "message": "Waiting for task-parent",
            "role": "DEV", "hop_id": f"hop-{task_id}", "at": effective_at,
            "recommended": "Wait for dependencies or queue ownership to become ready",
            "maintenance_incident_id": None, "maintenance_report": None,
        }
    return {
        "task_id": task_id,
        "task_title": f"{task_id} title",
        "task_text": f"Full text for {task_id}",
        "repository": "/repo",
        "manifest_path": f"/repo/.plan/team-{task_id}/{task_id}/task.json",
        "team": f"team-{task_id}",
        "status": status,
        "column": "WORKING" if status in {"INBOX", "RUNNING"} else status,
        "effective_activity_at": effective_at,
        "surface": surface,
        "availability": availability,
        "surface_warning": warning,
        "active_role": "DEV" if status not in {"DONE", "STOPPED"} else None,
        "active_hop_id": f"hop-{task_id}" if status not in {"DONE", "STOPPED"} else None,
        "active_hop": {"hop_id": f"hop-{task_id}", "state": "wait_response"} if status not in {"DONE", "STOPPED"} else None,
        "active_action": "wait_response" if status not in {"DONE", "STOPPED"} else None,
        "block_retryable": retryable,
        "block_reason": block_reason,
        "block_code": block_code,
        "primary_problem": primary_problem,
        "active_maintenance_incident": active_maintenance,
        "active_maintenance_report": maintenance_report if active_maintenance else None,
        "latest_maintenance_report": maintenance_report,
        "projection_errors": [],
        "depends_on_task_ids": ["task-parent"] if status == "WAITING" else [],
        "child_task_ids": ["task-child"] if task_id == "running" else [],
        "waiting_on_task_ids": ["task-parent"] if status == "WAITING" else [],
        "waiting_reason": "Waiting for task-parent" if status == "WAITING" else None,
        "queue_position": 2 if status == "WAITING" else None,
        "queue_length": 3 if status == "WAITING" else None,
        "pause_reason": "paused by user" if status == "PAUSED" else None,
        "stop_reason": "stopped by user" if status == "STOPPED" else None,
        "cleanup": {"state": cleanup_state},
        "roles": [
            {"logical_role": "PLAN", "physical_role": f"team-{task_id}-plan", "status": "waiting", "turn": 1, "page_id": None, "page_url": "", "conversation_generation": 0, "last_activity_at": "2026-07-22T14:00:00+00:00"},
            {"logical_role": "DEV", "physical_role": f"team-{task_id}-dev", "status": "running" if status not in {"DONE", "STOPPED"} else "done", "turn": 2, "page_id": f"page-{task_id}", "page_url": f"https://chatgpt.com/c/{task_id}", "conversation_generation": 1, "last_activity_at": "2026-07-22T14:10:00+00:00"},
        ],
        "reports": [{"report_id": "1", "physical_role": "PLAN", "turn": 1, "url": f"/api/reports/{task_id}/1", "path": "report.md"}],
        "timeline": timeline,
        "route_timeline": [{"hop_id": f"route-{task_id}", "at": "2026-07-22T14:05:00+00:00", "source_role": "PLAN", "route": "DEV", "kind": "route"}],
        "errors": [{"at": "2026-07-22T14:06:00+00:00", "error": f"error-{task_id}"}] if task_id == "running" else [],
        "control_results": [{"control_id": f"control-{task_id}", "action": "pause", "status": "applied", "at": "2026-07-22T14:07:00+00:00"}] if task_id == "running" else [],
        "controls": ["pause", "resume", "retry", "stop", "restart_role", "open_tab", "new_chat", "route_plan", "clear_team"],
    }


def _ui_payloads():
    tasks = [
        _ui_task("running", "RUNNING"),
        _ui_task("inbox", "INBOX", availability="offline", surface="offline_recoverable"),
        _ui_task("waiting", "WAITING"),
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
            assert page.locator('[data-lane-cards="RUNNING"] [data-task-id]').first.get_attribute("data-task-id") == "inbox"
            assert page.locator('[data-lane-cards="WAITING"] [data-task-id]').count() == 1
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
            detail_order = page.eval_on_selector_all(
                "#primary-problem-section, #maintenance-section, #controls-content, #dependency-summary, #role-table-body, #logs-content, #selected-reports",
                "nodes => nodes.map(node => node.getBoundingClientRect().top)",
            )
            assert detail_order == sorted(detail_order)
            selected_text = page.evaluate(
                """() => {
                    const row = [...document.querySelectorAll('#logs-content [data-key]')].find(node => node.textContent.includes('error-running'));
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(row.querySelector('[data-message]'));
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return selection.toString();
                }"""
            )
            assert selected_text == "error-running"
            page.wait_for_timeout(1200)
            assert page.evaluate("window.getSelection().toString()") == "error-running"
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
            assert page.locator('#primary-problem-section').is_visible()
            assert "manual recovery required" in page.locator('#primary-problem-section').text_content()
            assert page.locator('#maintenance-section').is_visible()
            assert page.locator('#maintenance-report').get_attribute("href") == "/api/maintenance-reports/blocked-manual/maint-blocked-manual"
            assert page.locator('#blocked-guidance').is_visible()
            assert page.locator('#task-primary [data-action="resume"]').count() == 1
            assert page.locator('#task-primary [data-action="retry"]').count() == 0
            page.locator('[data-task-id="waiting"]').click()
            assert page.locator('#primary-problem-section').is_visible()
            assert "Waiting for task-parent" in page.locator('#primary-problem-section').text_content()
            assert "Position 2 of 3" in page.locator('#dependency-content').text_content()
            page.locator('[data-task-id="done"]').click()
            assert not page.locator('#primary-problem-section').is_visible()
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
            page.locator('[data-task-id="blocked-manual"]').click()
            assert page.locator('#primary-problem-section').is_visible()
            assert page.locator('#maintenance-section').is_visible()
            problem_bounds = page.locator('#primary-problem-section').bounding_box()
            maintenance_bounds = page.locator('#maintenance-section').bounding_box()
            assert problem_bounds is not None and problem_bounds["width"] <= 390
            assert maintenance_bounds is not None and maintenance_bounds["width"] <= 390
            page.locator('[data-task-id="waiting"]').click()
            assert "Position 2 of 3" in page.locator('#dependency-content').text_content()
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
            page.locator("#create-task-input").fill("Queue via V3")
            page.locator("#create-team-input").fill("exact-team")
            page.locator('input[name="new_role"][value="PLAN"]').uncheck()
            page.locator("#new-all-input").uncheck()
            page.locator("#create-reuse-team-input").check()
            page.locator("#create-submit").click()
            page.wait_for_timeout(100)
            assert captured[-1] == (
                "create",
                {
                    "task": "Queue via V3",
                    "repository": "/repo",
                    "reuse_team": "exact-team",
                    "new_roles": [],
                    "new_all": False,
                },
            )
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


def test_cdpa_without_arguments_starts_runtime_for_current_repository(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cdpa_cli_module,
        "start_runtime",
        lambda *, repository, config_path, open_ui: captured.update(
            repository=repository, config_path=config_path, open_ui=open_ui
        ) or 0,
    )

    assert cdpa_main([]) == 0
    assert captured == {
        "repository": tmp_path.resolve(),
        "config_path": None,
        "open_ui": True,
    }


def test_cdpa_start_and_ui_are_reserved_global_commands(tmp_path: Path, monkeypatch):
    started = {}
    opened = {}
    monkeypatch.setattr(
        cdpa_cli_module,
        "start_runtime",
        lambda *, repository, config_path, open_ui: started.update(
            repository=repository, config_path=config_path, open_ui=open_ui
        ) or 0,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "open_runtime_ui",
        lambda *, repository, config_path: opened.update(
            repository=repository, config_path=config_path
        ) or 0,
    )

    assert cdpa_main(["start", "--repository", str(tmp_path), "--no-open"]) == 0
    assert started == {
        "repository": tmp_path.resolve(),
        "config_path": None,
        "open_ui": False,
    }
    assert cdpa_main(["ui", "--repository", str(tmp_path)]) == 0
    assert opened == {"repository": tmp_path.resolve(), "config_path": None}


def test_cdpa_runtime_supervisor_starts_and_stops_packaged_children(tmp_path: Path, monkeypatch):
    processes = []
    commands = []

    class FakeProcess:
        def __init__(self, command, cwd, **_kwargs):
            commands.append((command, Path(cwd)))
            self.terminated = False
            self.signals = []
            processes.append(self)

        def poll(self):
            return None

        def send_signal(self, value):
            self.signals.append(value)
            self.terminated = True

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    def fake_read_json(url, *, timeout):
        if url.endswith("/api/tasks"):
            raise RuntimeError("not running")
        return {"task_store_ready": True}

    monkeypatch.setattr(cdpa_cli_module, "_read_json", fake_read_json)
    monkeypatch.setattr(cdpa_cli_module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        cdpa_cli_module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cdpa_cli_module.start_runtime(
        repository=tmp_path.resolve(), config_path=None, open_ui=False
    ) == 130
    assert [command[2] for command, _cwd in commands] == [
        "playwright_auto.dashboard",
        "playwright_auto.cdpa_worker",
    ]
    assert all(cwd == tmp_path.resolve() for _command, cwd in commands)
    assert all(process.terminated for process in processes)
    assert all(process.signals == [cdpa_cli_module._interrupt_signal()] for process in processes)


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


def test_dashboard_resume_payload_excludes_catalog_invalid_derived_child(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    target = tasks.create_task(
        "Target", requested_team="alpha", task_id="task-target"
    )
    child = tasks.create_task(
        "Child",
        requested_team="beta",
        task_id="task-child",
        depends_on_task_ids=(target["task_id"],),
    )
    poison_catalog_identity_entry(tasks, child)
    filtered, _errors = tasks.discover_with_errors()
    assert child["task_id"] not in {task["task_id"] for task in filtered}

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({"repository": str(tmp_path), "team": "alpha"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 202
        assert payload["task_id"] == target["task_id"]
        assert payload["child_task_ids"] == []
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
        {"report_mode": "inline"},
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



def test_phase2_selected_task_layout_is_error_first_and_scroll_bounded():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    ordered_ids = [
        "team-heading",
        "primary-problem-section",
        "maintenance-section",
        "controls-content",
        "dependency-summary",
        "role-table-body",
        "logs-content",
        "selected-reports",
    ]
    positions = [html.index(f'id="{element_id}"') for element_id in ordered_ids]
    assert positions == sorted(positions)
    assert 'data-lane="WAITING"' in html
    assert 'max-h-96 overflow-y-auto' in html
    assert 'function renderPrimaryProblem' in html
    assert 'function renderMaintenance' in html
    assert 'function renderDependencySummary' in html
    assert 'task.timeline || []' in html
    assert "effective_activity_at" in html


def test_dashboard_serves_validated_maintenance_report_and_rejects_tampering(
    tmp_path: Path,
):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    task = tasks.create_task(
        "maintenance report task",
        requested_team="alpha",
        task_id="task-maintenance-report",
    )
    report = tmp_path / ".plan" / "maintainers" / "alpha_turn1_20260723T010203Z.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    body = b"# Maintenance report\n\nRecovered exact role ownership.\n"
    report.write_bytes(body)

    def add_report(state):
        state["maintenance"] = {
            "active_incident_id": None,
            "incidents": [
                {
                    "incident_id": "maint-report-1",
                    "key": "role_offline|snapshot",
                    "state": "RESOLVED",
                    "turn": 1,
                    "report_path": str(report.resolve()),
                    "report_sha256": hashlib.sha256(body).hexdigest(),
                    "report_size": len(body),
                    "created_at": "2026-07-23T01:02:03+00:00",
                    "updated_at": "2026-07-23T01:03:03+00:00",
                    "resolved_at": "2026-07-23T01:03:03+00:00",
                }
            ],
            "last_resolved_at": "2026-07-23T01:03:03+00:00",
        }
        return state

    tasks.update(task["manifest_path"], add_report)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "GET",
            "/api/maintenance-reports/task-maintenance-report/maint-report-1",
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/markdown; charset=utf-8"
        assert response.read() == body

        report.write_text("tampered", encoding="utf-8")
        connection.request(
            "GET",
            "/api/maintenance-reports/task-maintenance-report/maint-report-1",
        )
        tampered = connection.getresponse()
        payload = json.loads(tampered.read())
        assert tampered.status == 409
        assert "content changed after validation" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_selected_timeline_renders_all_rows_and_same_hop_routes_independently(
    tmp_path: Path,
):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(
        config_path,
        tmp_path,
        html=DASHBOARD_HTML_PATH.read_bytes(),
    )
    task = _ui_task("long-timeline", "RUNNING")
    raw_timeline = {
        "errors": [
            {
                "at": f"2026-07-23T{2 + index // 60:02d}:{index % 60:02d}:00+00:00",
                "error": f"error-{index:03d}",
            }
            for index in range(101)
        ],
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
        ],
    }
    task["timeline"] = build_task_timeline(raw_timeline)
    tasks_payload = {"tasks": [task], "repository": "/repo", "errors": []}
    state_payload = {
        "connected": True,
        "updated_at": "2026-07-23T04:00:00+00:00",
        "page_count": 0,
        "pages": [],
        "events": [],
        "error": None,
    }
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on(
                "console",
                lambda message: errors.append(f"console:{message.type}:{message.text}")
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))

            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(tasks_payload),
                    )
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(state_payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/**", api)
            page.goto(
                f"http://127.0.0.1:{server.server_port}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector('[data-task-id="long-timeline"]')
            page.locator('[data-task-id="long-timeline"]').click()

            timeline_rows = page.locator("#logs-content [data-key]")
            assert timeline_rows.count() == len(task["timeline"]) == 103
            text = page.locator("#logs-content").text_content()
            assert "DEV → DEV · route_repair" in text
            assert "DEV → TEST · route" in text
            assert page.locator('#logs-content [data-key^="route:"]').count() == 2
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_preserves_active_report_provenance_and_equal_timestamp_rows(
    tmp_path: Path,
):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(
        config_path,
        tmp_path,
        html=DASHBOARD_HTML_PATH.read_bytes(),
    )
    stale = _ui_task("stale-report", "BLOCKED")
    stale["active_maintenance_incident"] = {
        "incident_id": "maint-new",
        "state": "OPEN",
        "trigger_code": "role_offline",
        "trigger_reason": "Current incident has no report yet.",
    }
    stale["active_maintenance_report"] = None
    stale["latest_maintenance_report"] = {
        "incident_id": "maint-old",
        "state": "RESOLVED",
        "turn": 1,
        "url": "/api/maintenance-reports/stale-report/maint-old",
    }
    stale["primary_problem"] = {
        "kind": "BLOCKED",
        "code": "role_offline",
        "message": "Current incident has no report yet.",
        "role": "DEV",
        "hop_id": 7,
        "at": "2026-07-23T01:00:00+00:00",
        "recommended": "Open the exact owned role tab",
        "maintenance_incident_id": "maint-new",
        "maintenance_report": None,
    }

    current = _ui_task("current-report", "WAITING")
    current_report = {
        "incident_id": "maint-current",
        "state": "RUNNING",
        "turn": 2,
        "url": "/api/maintenance-reports/current-report/maint-current",
    }
    current["active_maintenance_incident"] = {
        "incident_id": "maint-current",
        "state": "RUNNING",
        "trigger_code": "dependency_wait",
        "trigger_reason": "Waiting for repaired parent.",
    }
    current["active_maintenance_report"] = current_report
    current["latest_maintenance_report"] = current_report
    current["primary_problem"] = {
        "kind": "WAITING",
        "code": "dependency_wait",
        "message": "Waiting for repaired parent.",
        "role": "DEV",
        "hop_id": 8,
        "at": "2026-07-23T01:01:00+00:00",
        "recommended": "Wait for dependencies or queue ownership to become ready",
        "maintenance_incident_id": "maint-current",
        "maintenance_report": current_report,
    }

    equal = _ui_task("equal-events", "RUNNING")
    at = "2026-07-23T02:00:00+00:00"
    equal["timeline"] = build_task_timeline(
        {
            "errors": [
                {"at": at, "error": "first equal error"},
                {"at": at, "error": "second equal error"},
            ],
            "dependency_events": [
                {"at": at, "message": "first equal dependency"},
                {"at": at, "message": "second equal dependency"},
            ],
        }
    ) + [
        {
            "key": f"filler:{index}",
            "at": f"2026-07-23T01:{index:02d}:00+00:00",
            "level": "STATE",
            "source": "task",
            "message": f"filler {index}",
        }
        for index in range(40)
    ]

    tasks_payload = {
        "tasks": [stale, current, equal],
        "repository": "/repo",
        "errors": [],
    }
    state_payload = {
        "connected": True,
        "updated_at": "2026-07-23T03:00:00+00:00",
        "page_count": 0,
        "pages": [],
        "events": [
            {
                "task_id": "equal-events",
                "at": at,
                "role": "DEV",
                "action": "send",
                "phase": "first",
                "detail": "first equal browser event",
            },
            {
                "task_id": "equal-events",
                "at": at,
                "role": "DEV",
                "action": "send",
                "phase": "second",
                "detail": "second equal browser event",
            },
        ],
        "error": None,
    }
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on(
                "console",
                lambda message: errors.append(f"console:{message.type}:{message.text}")
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))

            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(tasks_payload),
                    )
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(state_payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/**", api)
            page.goto(
                f"http://127.0.0.1:{server.server_port}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector('[data-task-id="stale-report"]')

            page.locator('[data-task-id="stale-report"]').click()
            assert "maint-new" in page.locator("#primary-problem-meta").text_content()
            assert not page.locator("#primary-problem-report").is_visible()
            assert not page.locator("#maintenance-report").is_visible()

            page.locator('[data-task-id="current-report"]').click()
            assert "maint-current" in page.locator("#primary-problem-meta").text_content()
            assert page.locator("#primary-problem-report").get_attribute("href") == current_report["url"]
            assert page.locator("#maintenance-report").get_attribute("href") == current_report["url"]

            page.locator('[data-task-id="equal-events"]').click()
            rows = page.locator("#logs-content [data-key]")
            assert rows.count() == 46
            log_text = page.locator("#logs-content").text_content()
            for message in (
                "first equal error",
                "second equal error",
                "first equal dependency",
                "second equal dependency",
                "first equal browser event",
                "second equal browser event",
            ):
                assert message in log_text
            assert page.locator('#logs-content [data-key^="event:"]').count() == 2

            selected_text = page.evaluate(
                """() => {
                    const row = [...document.querySelectorAll('#logs-content [data-key]')]
                        .find(node => node.textContent.includes('first equal error'));
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(row.querySelector('[data-message]'));
                    selection.removeAllRanges();
                    selection.addRange(range);
                    const host = document.querySelector('#logs-content');
                    host.scrollTop = Math.max(1, host.scrollHeight - host.clientHeight - 20);
                    return {selection: selection.toString(), scrollTop: host.scrollTop};
                }"""
            )
            assert selected_text["selection"] == "first equal error"
            assert selected_text["scrollTop"] > 0
            page.wait_for_timeout(2200)
            assert page.evaluate("window.getSelection().toString()") == "first equal error"
            assert page.locator("#logs-content").evaluate("node => node.scrollTop") > 0
            assert page.locator("#logs-content [data-key]").count() == 46
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_browser_event_keys_survive_real_rolling_window_shift(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(
        config_path,
        tmp_path,
        html=DASHBOARD_HTML_PATH.read_bytes(),
    )
    task = _ui_task("rolling-events", "RUNNING")
    tasks_payload = {"tasks": [task], "repository": "/repo", "errors": []}
    at = "2026-07-23T01:00:00+00:00"

    def browser_event(index: int) -> dict[str, object]:
        return {
            "event_id": f"browser-{index:03d}",
            "task_id": "rolling-events",
            "at": at,
            "role": "DEV",
            "action": "send",
            "phase": f"phase-{index:03d}",
            "detail": f"browser event {index:03d}",
        }

    state_payload = {
        "connected": True,
        "updated_at": "2026-07-23T03:00:00+00:00",
        "page_count": 0,
        "pages": [],
        "events": [browser_event(index) for index in range(160)],
        "error": None,
    }
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on(
                "console",
                lambda message: errors.append(f"console:{message.type}:{message.text}")
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))

            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(tasks_payload),
                    )
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(state_payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/**", api)
            page.goto(
                f"http://127.0.0.1:{server.server_port}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector('[data-task-id="rolling-events"]')
            page.locator('[data-task-id="rolling-events"]').click()
            page.wait_for_function(
                "document.querySelectorAll('#logs-content [data-key^=\"event:\"]').length === 24"
            )

            before = page.evaluate(
                """() => {
                    const row = [...document.querySelectorAll('#logs-content [data-key]')]
                        .find(node => node.textContent.includes('browser event 150'));
                    row.dataset.marker = 'kept';
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(row.querySelector('[data-message]'));
                    selection.removeAllRanges();
                    selection.addRange(range);
                    const host = document.querySelector('#logs-content');
                    host.scrollTop = Math.max(1, host.scrollHeight - host.clientHeight - 20);
                    return {
                        key: row.dataset.key,
                        selection: selection.toString(),
                        scrollTop: host.scrollTop,
                    };
                }"""
            )
            assert before["selection"] == "send · phase-150 · browser event 150"
            assert before["scrollTop"] > 0

            state_payload["events"] = [browser_event(index) for index in range(1, 161)]
            page.wait_for_function(
                "[...document.querySelectorAll('#logs-content [data-key]')].some(node => node.textContent.includes('browser event 160'))",
                timeout=5000,
            )

            after = page.evaluate(
                """() => {
                    const row = [...document.querySelectorAll('#logs-content [data-key]')]
                        .find(node => node.textContent.includes('browser event 150'));
                    const host = document.querySelector('#logs-content');
                    return {
                        key: row.dataset.key,
                        marker: row.dataset.marker || null,
                        selection: window.getSelection().toString(),
                        scrollTop: host.scrollTop,
                        eventRows: document.querySelectorAll('#logs-content [data-key^="event:"]').length,
                    };
                }"""
            )

            assert after["key"] == before["key"]
            assert after["marker"] == "kept"
            assert after["selection"] == before["selection"]
            assert after["scrollTop"] == before["scrollTop"]
            assert after["eventRows"] == 24
            assert page.locator('#logs-content [data-key^="event:"]').count() == 24
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_history_reports_polling_preserves_selection_scroll_and_keyed_nodes(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(
        config_path,
        tmp_path,
        html=DASHBOARD_HTML_PATH.read_bytes(),
    )
    tasks = []
    for index in range(20):
        task = _ui_task(f"history-{index:02d}", "DONE", surface="history", availability="terminal")
        task["effective_activity_at"] = f"2026-07-23T{index:02d}:00:00+00:00"
        task["task_title"] = f"History title {index:02d}"
        task["reports"] = [
            {
                "report_id": f"report-{index:02d}",
                "physical_role": "PLAN",
                "turn": index + 1,
                "url": f"/api/reports/history-{index:02d}/report-{index:02d}",
                "path": f"history-{index:02d}.md",
            }
        ]
        tasks.append(task)
    tasks_payload = {"tasks": tasks, "repository": "/repo", "errors": []}
    state_payload = {
        "connected": True,
        "updated_at": "2026-07-23T23:00:00+00:00",
        "page_count": 0,
        "pages": [],
        "events": [],
        "error": None,
    }
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            bundled = Path(playwright.chromium.executable_path)
            executable = bundled if bundled.is_file() else Path(shutil.which("chromium") or "")
            assert executable.is_file(), "Chromium executable is required for browser regression"
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on(
                "console",
                lambda message: errors.append(f"console:{message.type}:{message.text}")
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: errors.append(f"page:{error}"))

            def api(route):
                request = route.request
                if request.method == "GET" and request.url.endswith("/api/tasks"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(tasks_payload),
                    )
                elif request.method == "GET" and request.url.endswith("/api/state"):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(state_payload),
                    )
                else:
                    route.continue_()

            page.route("**/api/**", api)
            page.goto(
                f"http://127.0.0.1:{server.server_port}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector('[data-history-task-id="history-10"]')

            order = page.locator("#history-list > article[data-key]").evaluate_all(
                "nodes => nodes.map(node => node.dataset.historyTaskId)"
            )
            assert order == [f"history-{index:02d}" for index in range(19, -1, -1)]

            before = page.evaluate(
                """() => {
                    const row = document.querySelector('[data-history-task-id="history-10"]');
                    const link = row.querySelector('[data-field="reports"] a');
                    row.dataset.marker = 'row-kept';
                    link.dataset.marker = 'link-kept';
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(link);
                    selection.removeAllRanges();
                    selection.addRange(range);
                    const host = document.querySelector('#history-list');
                    host.scrollTop = Math.max(1, host.scrollHeight - host.clientHeight - 40);
                    return {
                        selection: selection.toString(),
                        scrollTop: host.scrollTop,
                        rowKey: row.dataset.key,
                        linkKey: link.dataset.key,
                        href: link.getAttribute('href'),
                    };
                }"""
            )
            assert before["selection"] == "PLAN turn 11"
            assert before["scrollTop"] > 0
            assert before["linkKey"] == "role:report-10"
            assert before["href"] == "/api/reports/history-10/report-10"

            page.wait_for_timeout(2300)

            after = page.evaluate(
                """() => {
                    const row = document.querySelector('[data-history-task-id="history-10"]');
                    const link = row.querySelector('[data-field="reports"] a[data-key]');
                    const host = document.querySelector('#history-list');
                    return {
                        selection: window.getSelection().toString(),
                        scrollTop: host.scrollTop,
                        rowKey: row.dataset.key,
                        linkKey: link.dataset.key,
                        rowMarker: row.dataset.marker || null,
                        linkMarker: link.dataset.marker || null,
                        href: link.getAttribute('href'),
                        reportLinks: document.querySelectorAll('#history-list [data-field="reports"] a[data-key]').length,
                    };
                }"""
            )
            assert after["selection"] == before["selection"]
            assert after["scrollTop"] == before["scrollTop"]
            assert after["rowKey"] == before["rowKey"]
            assert after["linkKey"] == before["linkKey"]
            assert after["rowMarker"] == "row-kept"
            assert after["linkMarker"] == "link-kept"
            assert after["href"] == before["href"]
            assert after["reportLinks"] == 20
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)



def test_cdpa_cli_inline_report_persists_task_option(tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        code = cdpa_main([
            "inline report task",
            "--inline-report",
            "--config", str(config_path),
            "--repository", str(tmp_path),
            "--team", "alpha",
        ])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert code == 0
    assert "team=alpha" in capsys.readouterr().out
    task = tasks.discover()[0]
    assert task["options"]["report_mode"] == "inline"


def test_cdpa_cli_rejects_inline_report_for_taskless_resume(tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    assert cdpa_main([
        "--team", "alpha",
        "--inline-report",
        "--config", str(config_path),
        "--repository", str(tmp_path),
    ]) == 2
    assert "invalid in taskless resume" in capsys.readouterr().err



@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, "other"])
def test_dashboard_create_rejects_explicit_invalid_report_mode(
    tmp_path: Path,
    value: object,
):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks",
            body=json.dumps(
                {
                    "task": "invalid report mode",
                    "repository": str(tmp_path),
                    "team": "alpha",
                    "new_roles": [],
                    "new_all": False,
                    "report_mode": value,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 400
        assert "report_mode" in payload["error"]
        assert tasks.discover() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_depends_on_flattens_commas_and_preserves_first_seen(monkeypatch, tmp_path: Path):
    config_path = write_config(tmp_path)
    captured = {}

    def fake_submit(config, **kwargs):
        captured.update(kwargs)
        return {
            "task_id": "task-child",
            "team": "child",
            "manifest_path": str(tmp_path / ".plan" / "child.json"),
        }

    monkeypatch.setattr(cdpa_cli_module, "submit_task", fake_submit)
    assert cdpa_cli_module.main([
        "Child",
        "--team", "child",
        "--repository", str(tmp_path),
        "--config", str(config_path),
        "--depends-on", "task-a,task-b",
        "--depends-on", "task-b",
        "--depends-on", "task-c",
    ]) == 0
    assert captured["depends_on_task_ids"] == ("task-a", "task-b", "task-c")


def test_cli_reuse_team_sends_exact_creation_mode(monkeypatch, tmp_path: Path):
    config_path = write_config(tmp_path)
    captured = {}

    def fake_submit(config, **kwargs):
        captured.update(kwargs)
        return {
            "task_id": "task-queued",
            "team": "alpha",
            "manifest_path": str(tmp_path / ".plan" / "alpha.json"),
        }

    monkeypatch.setattr(cdpa_cli_module, "submit_task", fake_submit)
    assert cdpa_cli_module.main([
        "Queued",
        "--reuse-team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
    ]) == 0
    assert captured["team"] is None
    assert captured["reuse_team"] == "alpha"


def test_cli_rejects_reuse_team_with_team_or_taskless(tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    assert cdpa_cli_module.main([
        "Invalid",
        "--team", "alpha",
        "--reuse-team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
    ]) == 2
    assert "mutually exclusive" in capsys.readouterr().err

    assert cdpa_cli_module.main([
        "--reuse-team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
    ]) == 2
    assert "taskless resume" in capsys.readouterr().err


def test_cli_rejects_depends_on_in_taskless_resume(monkeypatch, tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    assert cdpa_cli_module.main([
        "--team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
        "--depends-on", "task-a",
    ]) == 2
    assert "--depends-on" in capsys.readouterr().err


def test_dashboard_creates_same_team_queue_and_rejects_mixed_team_modes(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        tasks.create_task("Owner", requested_team="alpha", task_id="task-owner")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/tasks",
            body=json.dumps({
                "task": "Queued",
                "repository": str(tmp_path),
                "reuse_team": "alpha",
            }),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 201
        assert payload["team"] == "alpha"
        assert payload["status"] == "WAITING"
        assert payload["queue_position"] == 1
        assert payload["queue_length"] == 1
        assert payload["active_team_owner_task_id"] == "task-owner"
        assert payload["queue_blocked_by_task_id"] == "task-owner"

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/tasks",
            body=json.dumps({
                "task": "Invalid",
                "repository": str(tmp_path),
                "team": "alpha",
                "reuse_team": "alpha",
            }),
            headers={"Content-Type": "application/json"},
        )
        invalid_response = connection.getresponse()
        invalid_payload = json.loads(invalid_response.read())
        assert invalid_response.status == 400
        assert "mutually exclusive" in invalid_payload["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_derives_queue_order_and_owner_without_persisting_projection(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    tasks = TaskStore(config)
    owner = tasks.create_task("Owner", requested_team="alpha", task_id="task-owner")
    queued_b = tasks.create_task("Queued B", reuse_team="alpha", task_id="task-b")
    queued_a = tasks.create_task("Queued A", reuse_team="alpha", task_id="task-a")
    same_time = "2026-07-23T00:00:00+00:00"
    for queued in (queued_a, queued_b):
        tasks.update(
            queued["manifest_path"],
            lambda state: {
                **state,
                "created_at": same_time,
                "queue": {**state["queue"], "enqueued_at": same_time},
            },
        )
    all_tasks = tasks.discover()

    payload_a = build_task_payload(tasks.load(queued_a["manifest_path"]), tasks=all_tasks)
    payload_b = build_task_payload(tasks.load(queued_b["manifest_path"]), tasks=all_tasks)

    assert payload_a["queue_position"] == 1
    assert payload_b["queue_position"] == 2
    assert payload_a["queue_length"] == payload_b["queue_length"] == 2
    assert payload_a["active_team_owner_task_id"] == owner["task_id"]
    assert payload_b["queue_blocked_by_task_id"] == owner["task_id"]
    for queued in (queued_a, queued_b):
        raw = json.loads(Path(queued["manifest_path"]).read_text(encoding="utf-8"))
        assert "queue_position" not in raw
        assert "queue_length" not in raw
        assert "owner_task_ids" not in raw


def test_dashboard_mixed_waiters_match_worker_deterministic_blocker(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    tasks = TaskStore(config)
    parent = tasks.create_task("Parent", requested_team="parent", task_id="task-parent")
    first = tasks.create_task(
        "First",
        requested_team="alpha",
        task_id="task-first",
        depends_on_task_ids=(parent["task_id"],),
    )
    second = tasks.create_task(
        "Second",
        reuse_team="alpha",
        task_id="task-second",
        depends_on_task_ids=(parent["task_id"],),
    )
    tasks.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": "2026-07-23T00:00:00+00:00",
        },
    )
    all_tasks = tasks.discover_with_errors()[0]

    payload = build_task_payload(
        tasks.load(second["manifest_path"]),
        tasks=all_tasks,
    )
    scheduled, changed = tasks.refresh_scheduling(
        second["manifest_path"],
        tasks=all_tasks,
    )

    assert payload["dependency_ready_task_ids"] == [
        first["task_id"],
        second["task_id"],
    ]
    assert payload["queue_blocked_by_task_id"] == first["task_id"]
    assert changed is True
    assert scheduled["waiting"]["blocked_by_task_id"] == first["task_id"]


def test_dashboard_projects_clearing_owner_as_queue_availability_barrier(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    tasks = TaskStore(config)
    owner = tasks.create_task("Owner", requested_team="alpha", task_id="task-owner")
    queued = tasks.create_task("Queued", reuse_team="alpha", task_id="task-queued")
    owner = tasks.update(
        owner["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": "2026-07-23T00:00:00+00:00",
            "stop_reason": "team cleared",
            "cleanup": {
                **state["cleanup"],
                "state": "CLEARING",
                "phase": "verify_pending",
                "verified_empty_at": None,
            },
        },
    )

    payload = build_task_payload(
        tasks.load(queued["manifest_path"]),
        tasks=tasks.discover_with_errors()[0],
    )

    assert payload["active_team_owner_task_id"] == owner["task_id"]
    assert payload["queue_blocked_by_task_id"] == owner["task_id"]


def test_dashboard_create_dependencies_wait_and_reject_missing(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        parent = tasks.create_task("Parent", requested_team="parent", task_id="task-parent")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({
            "task": "Child",
            "repository": str(tmp_path),
            "team": "child",
            "depends_on_task_ids": ["task-parent"],
        })
        connection.request("POST", "/api/tasks", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 201
        assert payload["status"] == "WAITING"
        assert payload["depends_on_task_ids"] == ["task-parent"]
        assert payload["waiting_on_task_ids"] == ["task-parent"]

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        bad = json.dumps({
            "task": "Missing",
            "repository": str(tmp_path),
            "team": "missing",
            "depends_on_task_ids": ["not-found"],
        })
        connection.request("POST", "/api/tasks", body=bad, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        assert "missing dependency" in json.loads(response.read())["error"]
        assert all(task["task_id"] != "not-found-child" for task in tasks.discover())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_html_exposes_dependency_creation_and_status_projection():
    html = dashboard_module.DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    assert 'id="create-dependencies-input"' in html
    assert "depends_on_task_ids" in html
    assert "Stopped dependencies" in html
    assert "Missing dependencies" in html
    assert "queue_position" in html  # Phase 5 renders the derived queue projection.


def test_dashboard_control_recovers_phase4_before_task_lookup(tmp_path: Path, monkeypatch):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    task = tasks.create_task(
        "dashboard recovery order",
        requested_team="alpha",
        task_id="task-dashboard-recovery-order",
    )
    calls: list[str] = []
    original_recover = tasks.recover_phase4_replacement
    original_discover = tasks.discover_with_errors

    def recover():
        calls.append("recover")
        return original_recover()

    def discover():
        assert calls and calls[0] == "recover"
        calls.append("discover")
        return original_discover()

    monkeypatch.setattr(tasks, "recover_phase4_replacement", recover)
    monkeypatch.setattr(tasks, "discover_with_errors", discover)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            f"/api/tasks/{task['task_id']}/controls",
            body=json.dumps({"action": "pause", "reason": "operator"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 202, payload
        assert payload["controls"][-1]["action"] == "pause"
        assert calls[0:2] == ["recover", "discover"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_resume_rejects_replaced_blocked_parent_without_mutation(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    parent = tasks.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = tasks.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    parent = tasks.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "BLOCKED",
            "kanban_column": "BLOCKED",
            "block_code": "send_failed",
            "block_reason": "unsafe blocked parent",
            "block_retryable": False,
        },
    )
    result = tasks.replace_task_and_rewire(
        "task-parent",
        "Replacement parent",
        reuse_team=False,
        rewire_children=True,
        incident_id="maint-dashboard-resume-immutable",
    )
    parent_path = Path(parent["manifest_path"])
    parent_before = parent_path.read_bytes()
    catalog_before = tasks.catalog_path.read_bytes()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({"repository": str(tmp_path), "team": "parent"}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 400
        assert "immutable history" in payload["error"]
        assert parent_path.read_bytes() == parent_before
        assert tasks.catalog_path.read_bytes() == catalog_before
        assert tasks.load(child["manifest_path"])["depends_on_task_ids"] == [
            result["replacement"]["task_id"]
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_missing_task_get_returns_404_without_python_exception_name(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, _tasks = start_dashboard_server(config_path, tmp_path)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/tasks/does-not-exist")
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 404
        assert payload == {"error": "task not found"}
        assert "StopIteration" not in json.dumps(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_create_unrelated_task_survives_duplicate_diagnostics(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    install_duplicate_task_graph(tasks)

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request(
            "POST",
            "/api/tasks",
            body=json.dumps(
                {
                    "task": "Dashboard unrelated after duplicate corruption",
                    "repository": str(tmp_path),
                    "team": "gamma",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 201, payload
        assert payload["team"] == "gamma"
        assert payload["task_id"]
        discovered, errors = tasks.discover_with_errors()
        assert {task["task_id"] for task in discovered} == {
            "unique-task",
            payload["task_id"],
        }
        assert len(errors) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_duplicate_task_get_returns_409_without_selecting_one_manifest(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    states = install_duplicate_task_graph(tasks)
    discovered, errors = tasks.discover_with_errors()
    assert [task["task_id"] for task in discovered] == ["unique-task"]
    assert {item["manifest_path"] for item in errors} == {
        states["alpha"]["manifest_path"],
        states["beta"]["manifest_path"],
        states["child"]["manifest_path"],
    }

    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/tasks/dup-task")
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 409
        assert set(payload) == {"error"}
        assert "duplicate task ID 'dup-task'" in payload["error"]

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/api/tasks")
        response = connection.getresponse()
        listing = json.loads(response.read())
        assert response.status == 200
        assert [task["task_id"] for task in listing["tasks"]] == ["unique-task"]
        assert len(listing["errors"]) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_upload_preserves_order_and_rejects_taskless_resume(monkeypatch, tmp_path: Path, capsys):
    config_path = write_config(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.md"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    captured = {}

    def fake_submit(config, **kwargs):
        captured.update(kwargs)
        return {
            "task_id": "task-upload-cli",
            "team": "alpha",
            "manifest_path": str(tmp_path / ".plan" / "alpha.json"),
        }

    monkeypatch.setattr(cdpa_cli_module, "submit_task", fake_submit)
    assert cdpa_cli_module.main([
        "Upload context",
        "--team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
        "--upload", str(first),
        "--upload", str(second),
    ]) == 0
    assert captured["upload_paths"] == (str(first.resolve()), str(second.resolve()))

    assert cdpa_cli_module.main([
        "--team", "alpha",
        "--repository", str(tmp_path),
        "--config", str(config_path),
        "--upload", str(first),
    ]) == 2
    assert "--upload" in capsys.readouterr().err


def test_dashboard_upload_creation_and_payload_are_path_free(tmp_path: Path):
    config_path = write_config(tmp_path)
    attachment = tmp_path / "dashboard-context.txt"
    attachment.write_text("dashboard secret content", encoding="utf-8")
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/tasks",
            body=json.dumps({
                "task": "Dashboard upload",
                "repository": str(tmp_path),
                "team": "upload-ui",
                "upload_paths": [str(attachment)],
            }),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())

        assert response.status == 201, payload
        assert payload["attachments"] == [
            {
                "name": "dashboard-context.txt",
                "size": len("dashboard secret content"),
                "mime_type": "text/plain",
                "sha256_prefix": __import__("hashlib").sha256(
                    b"dashboard secret content"
                ).hexdigest()[:12],
            }
        ]
        serialized = json.dumps(payload)
        assert str(attachment.resolve()) not in serialized
        assert "dashboard secret content" not in serialized
        assert all("attachments_uploaded_generation" in role for role in payload["roles"])
        persisted = tasks.load(payload["manifest_path"])
        assert persisted["attachments"][0]["path"] == str(attachment.resolve())

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            f"/api/tasks/{payload['task_id']}/controls",
            body=json.dumps({"action": "pause", "reason": "privacy check"}),
            headers={"Content-Type": "application/json"},
        )
        control_response = connection.getresponse()
        control_payload = json.loads(control_response.read())
        assert control_response.status == 202
        assert str(attachment.resolve()) not in json.dumps(control_payload)
        assert control_payload["attachments"] == payload["attachments"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_resume_rejects_upload_paths(tmp_path: Path):
    config_path = write_config(tmp_path)
    server, thread, tasks = start_dashboard_server(config_path, tmp_path)
    tasks.create_task("Owner", requested_team="alpha", task_id="task-owner-upload-resume")
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/tasks/resume",
            body=json.dumps({
                "repository": str(tmp_path),
                "team": "alpha",
                "upload_paths": [str(tmp_path / "context.txt")],
            }),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert "upload" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_dashboard_html_renders_sanitized_attachment_metadata_only():
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")

    assert 'id="attachments-summary"' in html
    assert 'id="attachment-content"' in html
    assert 'id="create-uploads-input"' in html
    assert "sha256_prefix" in html
    assert "renderAttachments(task)" in html
    assert "item.path" not in html
