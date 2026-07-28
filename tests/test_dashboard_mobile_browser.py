from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.dashboard import create_server


COLUMNS = (
    "RUNNING",
    "WAITING",
    "BLOCKED",
    "PAUSED",
    "DONE",
    "STOPPED",
    "INDEPENDENT_AGENTS",
)
COLUMN_TITLES = tuple(
    "INDEPENDENT AGENTS" if column == "INDEPENDENT_AGENTS" else column
    for column in COLUMNS
)
CHROMIUM = next(
    (
        path for path in (
            Path("/snap/chromium/current/usr/lib/chromium-browser/chrome"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        )
        if path.is_file()
    ),
    None,
)


def _summary(column: str, index: int) -> dict:
    independent = column == "INDEPENDENT_AGENTS"
    task_id = (
        "agent-monitor-g2"
        if independent
        else f"task-{column.casefold()}-{index:02d}"
    )
    status = "WAITING" if independent else column
    return {
        "task_id": task_id,
        "team": "agent-monitor" if independent else f"team-{column.casefold()}-{index:02d}",
        "task_title": (
            "Independent agent: Monitor"
            if independent
            else f"{column} task {index} with enough card information"
        ),
        "task_mode": "independent" if independent else "workflow",
        "status": status,
        "column": column,
        "surface": "history" if column in {"DONE", "STOPPED"} else "active",
        "active_role": "AGENT" if independent else ("DEV" if column == "RUNNING" else "PLAN"),
        "active_hop_id": index + 1 if column == "RUNNING" else None,
        "active_action": (
            "waiting_trigger"
            if independent
            else ("wait_response" if column in {"RUNNING", "WAITING"} else "idle")
        ),
        "effective_activity_at": "2026-07-25T01:30:00+00:00" if column == "RUNNING" else None,
        "created_at": "2026-07-25T00:00:00+00:00",
        "started_at": "2026-07-25T01:00:00+00:00" if column == "RUNNING" else None,
        "updated_at": f"2026-07-25T02:{index:02d}:00+00:00",
        "primary_problem": (
            {
                "kind": column,
                "code": "dependency",
                "message": (
                    "Waiting for dependencies and exact-team ownership: task-running-00"
                    if column == "WAITING"
                    else "Waiting for parent task"
                ),
            }
            if column in {"WAITING", "BLOCKED"}
            else None
        ),
        "waiting_reason": (
            "Waiting for dependencies and exact-team ownership: task-running-00"
            if column == "WAITING"
            else None
        ),
        "waiting_order": (
            {"rank": 1, "fifo": index, "intervention": None}
            if column == "WAITING"
            else None
        ),
        "roles": (
            [
                {
                    "logical_role": "AGENT",
                    "physical_role": "agent-monitor-agent",
                    "status": "idle",
                    "turn": 1,
                    "online": False,
                }
            ]
            if independent
            else [
                {"logical_role": "PLAN", "physical_role": "alpha-plan", "status": "idle", "turn": 1, "online": True},
                {"logical_role": "DEV", "physical_role": "alpha-dev", "status": "active", "turn": 2, "online": True},
            ]
        ),
        "agent": (
            {
                "name": "Monitor",
                "enabled": True,
                "generation": 1,
                "trigger_type": None,
                "target_team": None,
                "target_task_id": None,
                "occurrence_count": 0,
                "check_count": 0,
                "cycle": 0,
                "max_cycles": 1,
            }
            if independent
            else None
        ),
        "version": 1,
        "projection_sha256": f"summary-{task_id}",
    }


ITEMS = [
    *[_summary("RUNNING", index) for index in range(12)],
    *[_summary(column, 0) for column in COLUMNS[1:]],
]
DETAILS = {
    item["task_id"]: {
        **item,
        "task_text": f"Full task text for {item['task_id']}\nSecond line stays lazy-loaded.",
        "active_input": {
            "hop_id": 2,
            "kind": "normal",
            "state": "waiting",
            "logical_role": "DEV",
            "source_role": "PLAN",
            "route": None,
            "turn": 2,
            "input": "Current DEV input without private repository paths.",
            "handoff": "alpha-dev_turn2_task.md",
        },
        "role_inputs": {
            "PLAN": {
                "hop_id": 1,
                "kind": "normal",
                "state": "routed",
                "logical_role": "PLAN",
                "source_role": None,
                "route": "DEV",
                "turn": 1,
                "input": "PLAN input",
                "handoff": "alpha-plan_turn1_task.md",
            },
            "DEV": {
                "hop_id": 2,
                "kind": "normal",
                "state": "waiting",
                "logical_role": "DEV",
                "source_role": "PLAN",
                "route": None,
                "turn": 2,
                "input": "Current DEV input without private repository paths.",
                "handoff": "alpha-dev_turn2_task.md",
            },
        },
        "timeline": [
            {
                "key": "route:1",
                "at": "2026-07-25T02:00:00+00:00",
                "level": "ROUTE",
                "kind": "route",
                "status": "",
                "message": "PLAN → REVIEW",
                "source_role": "PLAN",
                "route": "REVIEW",
                "hop_id": 2,
            }
        ],
        "timeline_total": 1,
        "reports": [],
        "maintenance_reports": [],
        "controls": [],
        "cleanup": {},
        "attachments": [],
        "projection_sha256": f"detail-{item['task_id']}",
    }
    for item in ITEMS
}
DETAILS["agent-monitor-g2"].update(
    active_input={
        "hop_id": 1,
        "kind": "independent_job",
        "state": "waiting_trigger",
        "logical_role": "AGENT",
        "source_role": None,
        "route": None,
        "turn": 1,
        "input": "Waiting for trigger.",
        "handoff": None,
    },
    role_inputs={
        "AGENT": {
            "hop_id": 1,
            "kind": "independent_job",
            "state": "waiting_trigger",
            "logical_role": "AGENT",
            "source_role": None,
            "route": None,
            "turn": 2,
            "input": "Waiting for trigger.",
            "handoff": None,
        }
    },
    independent_history=[
        {
            "task_id": "agent-monitor-g2",
            "agent_key": "monitor",
            "generation": 2,
            "status": "WAITING",
            "updated_at": "2026-07-27T02:00:00+00:00",
            "last_outcome": None,
        },
        {
            "task_id": "agent-monitor-g1",
            "agent_key": "monitor",
            "generation": 1,
            "status": "DONE",
            "completed_at": "2026-07-26T02:00:00+00:00",
            "last_outcome": {
                "outcome": "SUCCESS",
                "summary": "Completed prior Monitor generation",
            },
        },
    ],
    reports=[
        {
            "report_id": "g2-hop1-response",
            "physical_role": "agent-monitor-agent",
            "role": "AGENT",
            "turn": 2,
            "summary": None,
            "outcome": None,
            "source_task_id": "agent-monitor-g2",
            "generation": 2,
            "url": "/api/reports/agent-monitor-g2/g2-hop1-response",
        },
        {
            "report_id": "g1-agent-monitor-g1-independent",
            "role": "AGENT",
            "summary": "Completed prior Monitor generation",
            "outcome": "SUCCESS",
            "source_task_id": "agent-monitor-g1",
            "generation": 1,
            "url": "/api/reports/agent-monitor-g2/g1-agent-monitor-g1-independent",
        },
    ],
)


class MockAPIHandler(BaseHTTPRequestHandler):
    counts: dict[str, int] = {}
    board_etag = '"board-1"'
    detail_delays: dict[str, float] = {}
    detail_failures: dict[str, int] = {}

    def log_message(self, _format, *_args):
        return

    def _send_text(self, status: int, value: str):
        body = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, value: dict, *, etag: str | None = None):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        if etag:
            self.send_header("ETag", etag)
        if status != 304:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 304:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.counts[path] = self.counts.get(path, 0) + 1
        if path == "/api/reports/agent-monitor-g2/g2-hop1-response":
            self._send_text(
                200,
                "# Monitor responded report\n\nDurable body before independent completion.",
            )
            return
        if path == "/api/reports/agent-monitor-g2/g1-agent-monitor-g1-independent":
            self._send_text(
                200,
                "# Monitor report\n\nDurable report body from completed generation one.",
            )
            return
        if path == "/api/tasks":
            etag = self.board_etag
            if self.headers.get("If-None-Match") == etag:
                self._send(304, {}, etag=etag)
                return
            counts = {column: sum(item["column"] == column for item in ITEMS) for column in COLUMNS}
            self._send(
                200,
                {
                    "generation": 1,
                    "items": ITEMS,
                    "counts": counts,
                    "catalog": {"complete": True, "discovered_at": "now", "errors": []},
                },
                etag=etag,
            )
            return
        if path.startswith("/api/tasks/") and not path.endswith("/timeline"):
            task_id = path.rsplit("/", 1)[-1]
            delay = self.detail_delays.get(task_id, 0)
            if delay:
                time.sleep(delay)
            failures = self.detail_failures.get(task_id, 0)
            if failures > 0:
                self.detail_failures[task_id] = failures - 1
                self._send(503, {"error": {"message": "detail unavailable"}})
                return
            detail = DETAILS.get(task_id)
            if detail is None:
                self._send(404, {"error": {"message": "missing"}})
                return
            etag = f'"task-{task_id}-1"'
            if self.headers.get("If-None-Match") == etag:
                self._send(304, {}, etag=etag)
                return
            self._send(200, detail, etag=etag)
            return
        if path == "/api/history":
            terminal = [item for item in ITEMS if item["status"] in {"DONE", "STOPPED"}]
            self._send(200, {"items": terminal, "next_cursor": None})
            return
        if path == "/api/state":
            self._send(200, {"worker": {"status": "online"}, "browser": {}, "maintainers": {}})
            return
        if path == "/api/system":
            self._send(200, {"cpu_percent": 0.1, "rss_bytes": 1024})
            return
        self._send(404, {"error": {"message": "missing"}})


def _start_api():
    MockAPIHandler.counts = {}
    MockAPIHandler.board_etag = '"board-1"'
    MockAPIHandler.detail_delays = {}
    MockAPIHandler.detail_failures = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _start_frontend(tmp_path: Path, api_port: int):
    config = load_cdpa_config(None, repository_root=tmp_path)
    config = replace(config, dashboard_api_host="127.0.0.1", dashboard_api_port=api_port)
    server = create_server(config, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _swipe(page: Page, *, x1: float, y1: float, x2: float, y2: float, steps: int = 8):
    session = page.context.new_cdp_session(page)
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x1, "y": y1}]})
    for index in range(1, steps + 1):
        ratio = index / steps
        session.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [{"x": x1 + (x2 - x1) * ratio, "y": y1 + (y2 - y1) * ratio}],
            },
        )
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(120)


@pytest.mark.parametrize(
    ("viewport", "is_mobile"),
    [
        ({"width": 1280, "height": 900}, False),
        ({"width": 390, "height": 844}, True),
    ],
)
def test_selected_agent_history_and_reports_buttons_show_own_lifecycle_and_body(
    tmp_path: Path, viewport: dict, is_mobile: bool
):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(
                viewport=viewport,
                is_mobile=is_mobile,
                has_touch=is_mobile,
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")

            page.locator(
                '[data-column="INDEPENDENT_AGENTS"] [data-select-task="agent-monitor-g2"]'
            ).first.click()
            history_button = page.locator(
                '#task-detail [data-independent-action="history"][data-task-id="agent-monitor-g2"]'
            )
            history_button.wait_for()
            global_history_before = MockAPIHandler.counts.get("/api/history", 0)
            history_button.click()
            page.wait_for_selector('#secondary-dialog[open]')
            assert page.locator("#secondary-title").inner_text() == "Agent history"
            history_text = page.locator("#secondary-content").inner_text()
            assert "Generation 2 · WAITING" in history_text
            assert "Generation 1 · DONE" in history_text
            assert "Completed prior Monitor generation" in history_text
            assert "task-done-00" not in history_text
            assert MockAPIHandler.counts.get("/api/history", 0) == global_history_before
            page.locator("[data-close-secondary]").click()
            page.wait_for_function("!document.querySelector('#secondary-dialog').open")

            reports_button = page.locator(
                '#task-detail [data-independent-action="reports"][data-task-id="agent-monitor-g2"]'
            )
            reports_button.click()
            page.wait_for_selector('#secondary-dialog[open]')
            page.wait_for_function(
                "document.querySelector('#secondary-content').innerText.includes("
                "'Durable body before independent completion.') && "
                "document.querySelector('#secondary-content').innerText.includes("
                "'Durable report body from completed generation one.')"
            )
            report_text = page.locator("#secondary-content").inner_text()
            assert "# Monitor responded report" in report_text
            assert "Durable body before independent completion." in report_text
            assert "# Monitor report" in report_text
            assert "Durable report body from completed generation one." in report_text
            report_paths = [
                "/api/reports/agent-monitor-g2/g2-hop1-response",
                "/api/reports/agent-monitor-g2/g1-agent-monitor-g1-independent",
            ]
            for report_path in report_paths:
                report_link = page.locator(f'#secondary-content a[href="{report_path}"]')
                assert report_link.count() == 1
                assert report_link.inner_text() == report_path
                assert MockAPIHandler.counts.get(report_path) == 1
            assert not errors
            context.close()
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)


def test_waiting_board_uses_projected_order_badges_and_preserves_keyed_mobile_state(
    tmp_path: Path,
):
    source = Path("src/playwright_auto/dashboard_assets/views/board.js").read_text(
        encoding="utf-8"
    )
    assert "task.waiting_order" in source
    assert "depends_on_task_ids" not in source

    original_items = list(ITEMS)
    original_details = dict(DETAILS)
    template = dict(DETAILS["task-waiting-00"])

    def waiting_item(
        task_id: str,
        *,
        rank: int | None,
        fifo: int,
        updated_at: str,
        intervention: str | None = None,
    ) -> dict:
        item = _summary("WAITING", fifo)
        item.update(
            task_id=task_id,
            team=f"team-{task_id}",
            task_title=f"Projected order for {task_id}",
            updated_at=updated_at,
            waiting_order={
                "rank": rank,
                "fifo": fifo,
                "intervention": intervention,
            },
            projection_sha256=f"summary-{task_id}-v1",
        )
        return item

    waiting = [
        waiting_item(
            "waiting-rank-2",
            rank=2,
            fifo=3,
            updated_at="2026-07-27T04:00:00+00:00",
        ),
        waiting_item(
            "waiting-rank-1b",
            rank=1,
            fifo=2,
            updated_at="2026-07-27T03:00:00+00:00",
        ),
        waiting_item(
            "waiting-invalid",
            rank=None,
            fifo=0,
            updated_at="2026-07-27T05:00:00+00:00",
            intervention="Intervention required: missing dependency parent-a",
        ),
        waiting_item(
            "waiting-rank-1a",
            rank=1,
            fifo=1,
            updated_at="2026-07-27T02:00:00+00:00",
        ),
    ]
    ITEMS[:] = [item for item in original_items if item["column"] != "WAITING"] + waiting
    for item in waiting:
        DETAILS[item["task_id"]] = {
            **template,
            **item,
            "task_text": f"Full task text for {item['task_id']}",
            "projection_sha256": f"detail-{item['task_id']}-v1",
        }

    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )
            page = context.new_page()
            page.goto(base, wait_until="networkidle")

            waiting_lane = page.locator('[data-column="WAITING"] .lane-list')
            cards = page.locator('[data-column="WAITING"] [data-task-id]')
            assert cards.evaluate_all(
                "nodes => nodes.map(node => node.dataset.taskId)"
            ) == [
                "waiting-rank-1a",
                "waiting-rank-1b",
                "waiting-rank-2",
                "waiting-invalid",
            ]
            assert cards.evaluate_all(
                "nodes => nodes.map(node => Math.round(node.getBoundingClientRect().height))"
            ) == [150, 150, 150, 150]

            first_meta = page.locator(
                '[data-task-id="waiting-rank-1a"] .task-meta'
            )
            assert "ORDER" in first_meta.inner_text()
            assert "STATUS" not in first_meta.inner_text()
            assert first_meta.locator(".task-order span").inner_text() == "1"
            invalid_order = page.locator(
                '[data-task-id="waiting-invalid"] .task-order'
            )
            assert invalid_order.locator("span").inner_text() == "—"
            assert "missing dependency parent-a" in invalid_order.get_attribute("title")
            assert "Execution order unavailable" in invalid_order.get_attribute(
                "aria-label"
            )

            selected_id = "waiting-rank-1b"
            page.locator(
                f'[data-task-id="{selected_id}"] [data-select-task="{selected_id}"]'
            ).first.click()
            page.wait_for_selector("#task-detail .task-text")
            scroll_top = waiting_lane.evaluate(
                "node => { node.scrollTop = 155; return node.scrollTop; }"
            )
            assert scroll_top > 0
            page.evaluate(
                "taskId => { window.__stableWaitingCard = document.querySelector(`[data-task-id=\"${CSS.escape(taskId)}\"]`); }",
                selected_id,
            )

            rank_2 = next(item for item in waiting if item["task_id"] == "waiting-rank-2")
            rank_1a = next(item for item in waiting if item["task_id"] == "waiting-rank-1a")
            rank_2["waiting_order"].update(rank=1, fifo=0)
            rank_2["projection_sha256"] = "summary-waiting-rank-2-v2"
            rank_1a["waiting_order"].update(rank=2, fifo=1)
            rank_1a["projection_sha256"] = "summary-waiting-rank-1a-v2"
            MockAPIHandler.board_etag = '"board-waiting-2"'

            page.wait_for_function(
                """() => [...document.querySelectorAll('[data-column="WAITING"] [data-task-id]')]
                  .map(node => node.dataset.taskId).join(',') ===
                  'waiting-rank-2,waiting-rank-1b,waiting-rank-1a,waiting-invalid'"""
            )
            state = page.evaluate(
                """taskId => ({
                  sameNode: window.__stableWaitingCard === document.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`),
                  selected: document.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`).classList.contains('selected'),
                  scrollTop: document.querySelector('[data-column="WAITING"] .lane-list').scrollTop,
                })""",
                selected_id,
            )
            assert state["sameNode"] is True
            assert state["selected"] is True
            assert abs(state["scrollTop"] - scroll_top) <= 1
            context.close()
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)
        ITEMS[:] = original_items
        DETAILS.clear()
        DETAILS.update(original_details)


@pytest.mark.parametrize("viewport", [{"width": 390, "height": 844}])
def test_real_mobile_touch_board_independent_agent_controls_drawers_geometry_and_detail_cache(tmp_path: Path, viewport: dict):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(viewport=viewport, is_mobile=True, has_touch=True)
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base, wait_until="networkidle")

            assert page.locator(".lane").count() == 7
            assert page.locator(".lane > header h2").all_inner_texts() == list(COLUMN_TITLES)
            assert page.evaluate("document.documentElement.scrollWidth === document.documentElement.clientWidth")
            geometry = page.evaluate(
                """() => {
                  const section = document.querySelector('.board-section').getBoundingClientRect();
                  const workspace = document.querySelector('.task-workspace').getBoundingClientRect();
                  const board = document.querySelector('#board');
                  const lanes = [...document.querySelectorAll('.lane')].map(node => node.getBoundingClientRect());
                  return {
                  gap: workspace.top - section.bottom,
                  boardHeight: board.getBoundingClientRect().height,
                  boardClientWidth: board.clientWidth,
                  laneHeight: lanes[0].height,
                  rowCount: new Set(lanes.map(item => Math.round(item.top))).size,
                    horizontalOverflow: board.scrollWidth - board.clientWidth,
                  };
                }"""
            )
            assert 0 <= geometry["gap"] <= 16
            assert geometry["rowCount"] == 1
            assert 0 <= geometry["boardHeight"] - geometry["laneHeight"] <= 6
            assert geometry["horizontalOverflow"] > geometry["boardClientWidth"] * 3
            visible_cards = page.evaluate(
                """() => {
                  const list = document.querySelector('[data-column="RUNNING"] .lane-list').getBoundingClientRect();
                  const cards = [...document.querySelectorAll('[data-column="RUNNING"] .task-card')].slice(0, 2).map(node => node.getBoundingClientRect());
                  return {
                    count: cards.length,
                    firstFullyVisible: cards[0] && cards[0].top >= list.top && cards[0].bottom <= list.bottom,
                    secondFullyVisible: cards[1] && cards[1].top >= list.top && cards[1].bottom <= list.bottom,
                  };
                }"""
            )
            assert visible_cards == {"count": 2, "firstFullyVisible": True, "secondFullyVisible": True}
            agent_card = page.locator('[data-column="INDEPENDENT_AGENTS"] [data-task-id="agent-monitor-g2"]')
            assert agent_card.count() == 1
            assert "Monitor" in agent_card.inner_text()
            assert "Waiting for trigger" in agent_card.inner_text()
            agent_card.locator('[data-select-task="agent-monitor-g2"]').first.tap()
            page.wait_for_selector('#task-detail [data-independent-action="run"][data-task-id="agent-monitor-g2"]')
            agent_controls = set(
                page.locator("#task-detail .control-grid button").all_inner_texts()
            )
            assert {
                "Run now",
                "Pause",
                "Stop current job",
                "Retry",
                "Open tab",
                "Close tab",
                "New Chat next job",
                "Settings",
                "History",
                "Reports",
            }.issubset(agent_controls)

            waiting_summary = page.locator('[data-column="WAITING"] .task-summary').first
            assert "WAITING task" in waiting_summary.inner_text()
            assert "Waiting for team-running-00" in waiting_summary.inner_text()
            assert waiting_summary.evaluate("node => node.scrollHeight <= node.clientHeight")

            first_card = page.locator('[data-column="RUNNING"] [data-select-task]').first
            card_meta = page.locator('[data-column="RUNNING"] .task-card').first.locator(".task-meta").inner_text()
            assert "STATUS" in card_meta
            assert "RUNNING" in card_meta
            assert "ACTION" in card_meta
            assert "wait_response" in card_meta
            first_card.tap()
            task_id = first_card.get_attribute("data-select-task")
            assert task_id
            page.wait_for_selector("#task-detail .task-text")
            assert "Full task text" in page.locator("#task-detail").inner_text()
            assert "PLAN → REVIEW" in page.locator("#task-detail").inner_text()
            timeline_time = page.locator("#task-detail .timeline time").first
            assert timeline_time.get_attribute("title") == "2026-07-25T02:00:00+00:00"
            assert "T" not in timeline_time.inner_text()
            assert "ago" in timeline_time.inner_text()
            page.locator(f'[data-role-select="PLAN"][data-task-id="{task_id}"]').tap()
            assert "PLAN input" in page.locator("#task-detail .role-input-section").inner_text()
            second_copy = page.locator('[data-column="RUNNING"] [data-copy-task-id]').nth(1)
            second_copy.tap()
            assert task_id in page.locator("#task-detail").inner_text()
            detail_path = f"/api/tasks/{task_id}"
            assert MockAPIHandler.counts.get(detail_path) == 1
            page.wait_for_timeout(2300)
            assert MockAPIHandler.counts.get(detail_path) == 1

            selected_text = page.evaluate(
                """taskId => {
                  const card = document.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`);
                  const textNode = card.querySelector('.task-team').firstChild;
                  const range = document.createRange();
                  range.selectNodeContents(textNode);
                  const selection = window.getSelection();
                  selection.removeAllRanges();
                  selection.addRange(range);
                  window.__stableCard = card;
                  return selection.toString();
                }""",
                task_id,
            )
            assert selected_text
            page.wait_for_timeout(2300)
            stable_selection = page.evaluate(
                """taskId => ({
                  sameNode: window.__stableCard === document.querySelector(`[data-task-id="${CSS.escape(taskId)}"]`),
                  selectedText: window.getSelection().toString(),
                })""",
                task_id,
            )
            assert stable_selection == {"sameNode": True, "selectedText": selected_text}
            page.evaluate("window.getSelection().removeAllRanges()")

            summary = next(item for item in ITEMS if item["task_id"] == task_id)
            original_projection = summary["projection_sha256"]
            summary["projection_sha256"] = f"{original_projection}-changed"
            MockAPIHandler.board_etag = '"board-2"'
            for _ in range(20):
                page.wait_for_timeout(250)
                if MockAPIHandler.counts.get(detail_path) == 2:
                    break
            assert MockAPIHandler.counts.get(detail_path) == 2
            page.wait_for_timeout(1300)
            assert MockAPIHandler.counts.get(detail_path) == 2
            summary["projection_sha256"] = original_projection
            MockAPIHandler.board_etag = '"board-3"'

            lane = page.locator('[data-column="RUNNING"] .lane-list')
            waiting_lane = page.locator('[data-column="WAITING"] .lane-list')
            assert waiting_lane.evaluate("node => node.scrollTop") == 0
            lane_box = lane.bounding_box()
            assert lane_box
            _swipe(
                page,
                x1=lane_box["x"] + lane_box["width"] * 0.55,
                y1=lane_box["y"] + lane_box["height"] * 0.80,
                x2=lane_box["x"] + lane_box["width"] * 0.55,
                y2=lane_box["y"] + lane_box["height"] * 0.25,
            )
            assert lane.evaluate("node => node.scrollTop") > 0
            assert waiting_lane.evaluate("node => node.scrollTop") == 0
            board = page.locator("#board")
            board_box = board.bounding_box()
            assert board_box
            for _ in range(6):
                _swipe(
                    page,
                    x1=board_box["x"] + board_box["width"] * 0.85,
                    y1=board_box["y"] + board_box["height"] * 0.50,
                    x2=board_box["x"] + board_box["width"] * 0.15,
                    y2=board_box["y"] + board_box["height"] * 0.50,
                )
            horizontal = page.evaluate(
                """() => {
                  const board = document.querySelector('#board');
                  const running = document.querySelector('[data-column="RUNNING"]').getBoundingClientRect();
                  const independent = document.querySelector('[data-column="INDEPENDENT_AGENTS"]').getBoundingClientRect();
                  return {
                    horizontalOverflow: board.scrollWidth - board.clientWidth,
                    sameRow: Math.abs(independent.top - running.top) <= 2,
                    boardScrollLeft: board.scrollLeft,
                    maxScrollLeft: board.scrollWidth - board.clientWidth,
                    finalLaneVisible: independent.left >= 0 && independent.right <= innerWidth,
                  };
                }"""
            )
            assert horizontal["horizontalOverflow"] > 0
            assert horizontal["sameRow"] is True
            assert horizontal["boardScrollLeft"] > 0
            assert horizontal["maxScrollLeft"] - horizontal["boardScrollLeft"] <= 2
            assert horizontal["finalLaneVisible"] is True

            page.evaluate("window.scrollTo(0, 0)")
            page.locator('[data-view="history"]').click()
            page.wait_for_selector("#secondary-dialog[open]")
            page.locator("#secondary-dialog .secondary-header").tap()
            assert page.locator("#secondary-dialog").evaluate("dialog => dialog.open")
            page.locator("[data-close-secondary]").tap()
            page.wait_for_function("!document.querySelector('#secondary-dialog').open")

            page.evaluate("window.scrollTo(0, 0)")
            page.locator('[data-view="history"]').click()
            page.wait_for_selector("#secondary-dialog[open]")
            page.mouse.click(2, viewport["height"] / 2)
            page.wait_for_function("!document.querySelector('#secondary-dialog').open")

            page.evaluate("window.scrollTo(0, 0)")
            page.locator('[data-view="runtime"]').click()
            page.wait_for_selector("#secondary-dialog[open]")
            dialog_evidence = page.evaluate(
                """() => {
                  const dialog = document.querySelector('#secondary-dialog');
                  window.__stableSecondaryDialog = dialog;
                  window.__dialogOpenMutations = 0;
                  window.__dialogObserver = new MutationObserver(records => {
                    window.__dialogOpenMutations += records.filter(record => record.attributeName === 'open').length;
                  });
                  window.__dialogObserver.observe(dialog, {attributes: true});
                  return {
                    backdrop: getComputedStyle(dialog, '::backdrop').backgroundColor,
                    open: dialog.open,
                  };
                }"""
            )
            assert dialog_evidence["open"] is True
            assert dialog_evidence["backdrop"].startswith("rgba(")
            page.wait_for_timeout(2300)
            stable_dialog = page.evaluate(
                """() => ({
                  sameNode: window.__stableSecondaryDialog === document.querySelector('#secondary-dialog'),
                  open: document.querySelector('#secondary-dialog').open,
                  openMutations: window.__dialogOpenMutations,
                })"""
            )
            assert stable_dialog == {"sameNode": True, "open": True, "openMutations": 0}
            page.keyboard.press("Escape")
            page.wait_for_function("!document.querySelector('#secondary-dialog').open")

            page.evaluate("window.scrollTo(0, 0)")
            page.locator('[data-view="history"]').click()
            page.wait_for_selector("#secondary-dialog[open]")
            page.go_back()
            page.wait_for_function("!document.querySelector('#secondary-dialog').open")
            assert task_id in page.locator("#task-detail").inner_text()
            assert page.locator(f'[data-role-select="PLAN"][data-task-id="{task_id}"]').get_attribute("class").endswith("selected")

            page.evaluate("window.scrollTo(0, 0)")
            page.locator("[data-open-create]").click()
            page.wait_for_timeout(1300)
            assert page.locator("#create-dialog").evaluate("dialog => dialog.open")
            page.keyboard.press("Escape")
            page.wait_for_function("!document.querySelector('#create-dialog').open")

            visible_before = dict(MockAPIHandler.counts)
            time.sleep(2.2)
            visible_after = dict(MockAPIHandler.counts)
            page.evaluate(
                """() => {
                  Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                  Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                  document.dispatchEvent(new Event('visibilitychange'));
                }"""
            )
            hidden_before = dict(MockAPIHandler.counts)
            time.sleep(2.2)
            hidden_after = dict(MockAPIHandler.counts)
            for path in ("/api/tasks", "/api/state", "/api/system"):
                assert visible_after.get(path, 0) - visible_before.get(path, 0) >= 2
                assert hidden_after.get(path, 0) - hidden_before.get(path, 0) == 0
            assert not errors
            context.close()
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)



def test_team_first_create_and_batch_resume_surface_preserve_worker_options(tmp_path: Path):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    actions = {
        "dependency_teams": [
            {
                "team": "single-team",
                "ambiguous": False,
                "tasks": [
                    {
                        "task_id": "task-running-00",
                        "title": "Single dependency",
                        "status": "RUNNING",
                    }
                ],
            },
            {
                "team": "ambiguous-team",
                "ambiguous": True,
                "tasks": [
                    {"task_id": "task-running-01", "title": "First candidate", "status": "RUNNING"},
                    {"task_id": "task-running-02", "title": "Second candidate", "status": "PAUSED"},
                ],
            },
        ],
        "resume_teams": [
            {"team": "blocked-team", "task_id": "task-blocked-00", "title": "Blocked", "status": "BLOCKED", "reason": "blocked"},
            {"team": "paused-team", "task_id": "task-paused-00", "title": "Paused", "status": "PAUSED", "reason": "paused"},
            {"team": "offline-team", "task_id": "task-running-03", "title": "Offline", "status": "RUNNING", "reason": "offline"},
        ],
        "reuse_teams": [{"team": "reuse-team", "status": "available"}],
    }
    action_requests: list[str | None] = []
    create_requests: list[dict] = []
    resume_requests: list[dict] = []
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            def actions_route(route):
                conditional = route.request.headers.get("if-none-match")
                action_requests.append(conditional)
                if conditional == '"dashboard-actions-1"':
                    route.fulfill(status=304, headers={"ETag": '"dashboard-actions-1"'}, body="")
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    headers={"ETag": '"dashboard-actions-1"'},
                    body=json.dumps(actions),
                )

            def tasks_route(route):
                if route.request.method != "POST":
                    route.continue_()
                    return
                create_requests.append(
                    {
                        "body": route.request.post_data_json,
                        "idempotency": route.request.headers.get("idempotency-key"),
                    }
                )
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "command_id": "cmd-create-team-first",
                            "task_id": "task-created-team-first",
                            "status": "queued",
                        }
                    ),
                )

            def resume_route(route):
                body = route.request.post_data_json
                resume_requests.append(
                    {
                        "body": body,
                        "idempotency": route.request.headers.get("idempotency-key"),
                    }
                )
                if body["team"] == "paused-team":
                    route.fulfill(
                        status=409,
                        content_type="application/json",
                        body=json.dumps({"error": {"message": "paused team changed"}}),
                    )
                    return
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "command_id": f"cmd-resume-{body['team']}",
                            "task_id": body["team"],
                            "status": "queued",
                        }
                    ),
                )

            def command_route(route):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "command_id": route.request.url.rsplit("/", 1)[-1],
                            "status": "applied",
                            "task_id": None,
                            "result": {
                                "outcome": "continued",
                                "action": "observe_progress",
                                "postcondition": "generation_progress",
                            },
                            "error": None,
                        }
                    ),
                )

            page.route("**/api/dashboard-actions", actions_route)
            page.route("**/api/tasks/resume", resume_route)
            page.route("**/api/tasks", tasks_route)
            page.route("**/api/commands/*", command_route)
            page.goto(base, wait_until="networkidle")

            page.locator("[data-open-create]").click()
            page.wait_for_selector('#create-dialog [data-dependency-team="single-team"]')
            single = page.locator(
                '#create-dialog [data-dependency-team="single-team"] input[data-dependency-task-id="task-running-00"]'
            )
            ambiguous = page.locator(
                '#create-dialog [data-dependency-team="ambiguous-team"] input[data-dependency-team-select]'
            )
            assert single.is_enabled()
            assert ambiguous.is_disabled()
            ambiguous_text = page.locator(
                '#create-dialog [data-dependency-team="ambiguous-team"]'
            ).inner_text()
            assert "task-running-01" in ambiguous_text
            assert "task-running-02" in ambiguous_text
            single.check()
            page.locator('#create-dialog select[name="reuse_team"]').select_option("reuse-team")
            assert page.locator('#create-dialog input[name="requested_team"]').is_disabled()
            page.locator('#create-dialog details[data-advanced-dependencies]').click()
            page.locator('#create-dialog input[name="depends_on_task_ids"]').fill(
                "manual-parent, task-running-00"
            )
            page.locator('#create-dialog textarea[name="task"]').fill("Create with team-first dependency")
            page.locator('#create-dialog button[type="submit"]').click()
            page.wait_for_function("() => !document.querySelector('#create-dialog').open")

            assert len(create_requests) == 1
            assert create_requests[0]["idempotency"]
            assert create_requests[0]["body"] == {
                "task": "Create with team-first dependency",
                "reuse_team": "reuse-team",
                "repository": None,
                "depends_on_task_ids": ["task-running-00", "manual-parent"],
            }

            page.locator('[data-view="resume"]').click()
            page.wait_for_selector('#secondary-dialog [data-resume-team="blocked-team"]')
            assert page.locator("#secondary-dialog").inner_text().find("STOPPED") >= 0
            assert "replacement/reuse" in page.locator("#secondary-dialog").inner_text()
            for team in ("blocked-team", "paused-team", "offline-team"):
                page.locator(
                    f'#secondary-dialog input[data-resume-team="{team}"]'
                ).check()
            resume_button = page.locator("#secondary-dialog [data-resume-selected]")
            resume_button.click()
            resume_button.evaluate("button => button.click()")
            page.wait_for_selector("#secondary-dialog .resume-batch-summary")
            page.wait_for_function(
                "() => document.querySelector('#secondary-dialog .resume-batch-summary')?.textContent.includes('2 continued')"
            )
            summary = page.locator("#secondary-dialog .resume-batch-summary").inner_text()
            assert "2 continued" in summary
            assert "1 failed" in summary
            assert {item["body"]["team"] for item in resume_requests} == {
                "blocked-team",
                "paused-team",
                "offline-team",
            }
            assert len({item["idempotency"] for item in resume_requests}) == 3
            assert "paused team changed" in page.locator("#commands").inner_text()
            assert action_requests[0] is None
            assert '"dashboard-actions-1"' in action_requests[1:]
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)



def test_detail_transition_clears_stale_controls_on_failure_then_installs_new_task(
    tmp_path: Path,
):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    first_task = "task-running-00"
    second_task = "task-running-01"
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )
            page = context.new_page()
            page.goto(base, wait_until="networkidle")

            page.locator(f'[data-select-task="{first_task}"]').first.tap()
            page.wait_for_selector(f'#task-detail [data-control][data-task-id="{first_task}"]')
            assert first_task in page.locator("#task-detail").inner_text()

            MockAPIHandler.detail_delays[second_task] = 0.35
            MockAPIHandler.detail_failures[second_task] = 1
            page.locator(f'[data-select-task="{second_task}"]').first.tap()
            page.wait_for_selector('#task-detail [data-detail-state="loading"]')
            loading = page.locator("#task-detail").inner_text()
            assert first_task not in loading
            assert page.locator(f'#task-detail [data-task-id="{first_task}"]').count() == 0
            assert page.locator("#task-detail [data-control]").count() == 0

            page.wait_for_selector('#task-detail [data-detail-state="error"]')
            failed = page.locator("#task-detail").inner_text()
            assert "detail unavailable" in failed
            assert first_task not in failed
            assert page.locator("#task-detail [data-control]").count() == 0

            MockAPIHandler.detail_delays.pop(second_task, None)
            page.locator(f'[data-select-task="{second_task}"]').first.tap()
            page.wait_for_selector(f'#task-detail [data-control][data-task-id="{second_task}"]')
            assert second_task in page.locator("#task-detail").inner_text()
            assert page.locator(f'#task-detail [data-control][data-task-id="{first_task}"]').count() == 0
            assert set(page.locator("#task-detail [data-control]").evaluate_all(
                "nodes => nodes.map(node => node.dataset.taskId)"
            )) == {second_task}
            context.close()
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)



def test_cached_task_selection_replaces_selected_text_and_cannot_submit_previous_task_control(
    tmp_path: Path,
):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    first_task = "task-running-00"
    second_task = "task-running-01"
    captured_controls: list[dict] = []
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )
            page = context.new_page()

            def capture_control(route):
                captured_controls.append(
                    {
                        "url": route.request.url,
                        "method": route.request.method,
                        "body": route.request.post_data_json,
                    }
                )
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({"command_id": "cmd-cached-selection", "status": "queued"}),
                )

            page.route("**/api/tasks/*/controls", capture_control)
            page.goto(base, wait_until="networkidle")

            # Warm task B into the detail cache, then render task A.
            page.locator(f'[data-select-task="{second_task}"]').first.tap()
            page.wait_for_selector(
                f'#task-detail [data-control][data-task-id="{second_task}"]'
            )
            assert MockAPIHandler.counts.get(f"/api/tasks/{second_task}") == 1

            page.locator(f'[data-select-task="{first_task}"]').first.tap()
            page.wait_for_selector(
                f'#task-detail [data-control][data-task-id="{first_task}"]'
            )

            selected_text = page.evaluate(
                """() => {
                  const text = document.querySelector('#task-detail .task-text');
                  const range = document.createRange();
                  range.selectNodeContents(text);
                  const selection = window.getSelection();
                  selection.removeAllRanges();
                  selection.addRange(range);
                  return selection.toString();
                }"""
            )
            assert first_task in selected_text

            # Selecting cached B is an identity transition and must never defer behind A selection.
            page.locator(f'[data-select-task="{second_task}"]').first.tap()
            page.wait_for_timeout(200)
            switched = page.evaluate(
                """() => ({
                  selectedCards: [...document.querySelectorAll('.task-card.selected')]
                    .map(node => node.dataset.taskId),
                  detailTaskId: document.querySelector('#task-detail').dataset.taskId,
                  pendingRender: Boolean(document.querySelector('#task-detail')._pendingRender),
                  selectedText: window.getSelection().toString(),
                  controlTaskIds: [...document.querySelectorAll('#task-detail [data-control]')]
                    .map(node => node.dataset.taskId),
                })"""
            )
            assert switched["selectedCards"] == [second_task]
            assert switched["detailTaskId"] == second_task
            assert switched["pendingRender"] is False
            assert first_task not in switched["selectedText"]
            assert set(switched["controlTaskIds"]) == {second_task}
            assert MockAPIHandler.counts.get(f"/api/tasks/{second_task}") == 1

            page.locator(
                f'#task-detail [data-control="pause"][data-task-id="{second_task}"]'
            ).tap()
            page.wait_for_function("() => window.__unused === undefined")
            for _ in range(20):
                if captured_controls:
                    break
                page.wait_for_timeout(50)
            assert captured_controls == [
                {
                    "url": f"{base}/api/tasks/{second_task}/controls",
                    "method": "POST",
                    "body": {
                        "action": "pause",
                        "role": "DEV",
                        "expected_task_version": 1,
                        "confirmed": False,
                    },
                }
            ]
            assert all(first_task not in request["url"] for request in captured_controls)
            context.close()
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)

def test_active_role_clock_uses_projected_timestamp_for_role_hop_and_visibility_transitions(
    tmp_path: Path,
):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    task = next(item for item in ITEMS if item["task_id"] == "task-running-00")
    original = {
        key: task.get(key)
        for key in ("active_role", "active_hop_id", "effective_activity_at", "projection_sha256", "version")
    }
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )
            page = context.new_page()
            page.goto(base, wait_until="networkidle")
            clock = page.locator('[data-task-id="task-running-00"] .task-role-clock')
            page.wait_for_selector('[data-task-id="task-running-00"] .task-role-clock')

            role_at = "2026-07-26T08:00:00+00:00"
            task.update({
                "active_role": "REVIEW",
                "active_hop_id": 99,
                "effective_activity_at": role_at,
                "projection_sha256": "summary-task-running-00-role-99",
                "version": 2,
            })
            MockAPIHandler.board_etag = '"board-role-99"'
            page.wait_for_function(
                """expected => document.querySelector('[data-task-id="task-running-00"] .task-role-clock')?.dataset.elapsedAt === expected""",
                arg=role_at,
            )
            assert clock.get_attribute("data-role-timer") == "REVIEW:99"
            assert clock.get_attribute("data-elapsed-at") == role_at

            page.evaluate(
                """() => {
                  Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                  Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                  document.dispatchEvent(new Event('visibilitychange'));
                }"""
            )
            same_role_new_hop_at = "2026-07-26T07:00:00+00:00"
            task.update({
                "active_hop_id": 100,
                "effective_activity_at": same_role_new_hop_at,
                "projection_sha256": "summary-task-running-00-role-100",
                "version": 3,
            })
            MockAPIHandler.board_etag = '"board-role-100"'
            page.wait_for_timeout(1300)
            assert clock.get_attribute("data-role-timer") == "REVIEW:99"

            page.evaluate(
                """() => {
                  Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                  Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
                  document.dispatchEvent(new Event('visibilitychange'));
                }"""
            )
            page.wait_for_function(
                """expected => document.querySelector('[data-task-id="task-running-00"] .task-role-clock')?.dataset.elapsedAt === expected""",
                arg=same_role_new_hop_at,
            )
            assert clock.get_attribute("data-role-timer") == "REVIEW:100"
            assert clock.get_attribute("data-elapsed-at") == same_role_new_hop_at
            context.close()
            browser.close()
    finally:
        task.update(original)
        MockAPIHandler.board_etag = '"board-1"'
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)

def test_mock_board_payload_remains_below_gate():
    payload = json.dumps(
        {
            "generation": 1,
            "items": ITEMS,
            "counts": {column: sum(item["column"] == column for item in ITEMS) for column in COLUMNS},
            "catalog": {"complete": True, "discovered_at": "now", "errors": []},
        },
        separators=(",", ":"),
    ).encode()
    assert len(payload) < 100 * 1024
    assert all("task_text" not in item and "role_inputs" not in item for item in ITEMS)


def test_product_breakpoints_show_multiple_lanes_without_horizontal_scroll(
    tmp_path: Path,
):
    api, api_thread = _start_api()
    frontend, frontend_thread = _start_frontend(tmp_path, api.server_address[1])
    base = f"http://127.0.0.1:{frontend.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            if CHROMIUM is None:
                pytest.skip("system Chromium is unavailable")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(CHROMIUM),
                args=["--no-sandbox"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(base, wait_until="networkidle")

            def geometry():
                return page.evaluate(
                    """() => {
                      const board = document.querySelector('#board');
                      const lanes = [...document.querySelectorAll('.lane')].map(node => node.getBoundingClientRect());
                      const section = document.querySelector('.board-section').getBoundingClientRect();
                      const workspace = document.querySelector('.task-workspace').getBoundingClientRect();
                      const firstTop = Math.round(lanes[0].top);
                      return {
                        rowCount: new Set(lanes.map(item => Math.round(item.top))).size,
                        columnsFirstRow: lanes.filter(item => Math.round(item.top) === firstTop).length,
                        laneCount: lanes.length,
                        horizontalOverflow: board.scrollWidth - board.clientWidth,
                        detailBelow: workspace.top >= section.bottom,
                        boardWidth: board.clientWidth,
                        viewportWidth: innerWidth,
                      };
                    }"""
                )

            desktop = geometry()
            assert desktop["laneCount"] == 7
            assert desktop["columnsFirstRow"] == 4
            assert desktop["rowCount"] == 2
            assert desktop["horizontalOverflow"] == 0
            assert desktop["detailBelow"] is True
            assert desktop["boardWidth"] >= desktop["viewportWidth"] - 50

            page.set_viewport_size({"width": 900, "height": 900})
            page.wait_for_timeout(100)
            tablet = geometry()
            assert tablet["columnsFirstRow"] == 2
            assert tablet["rowCount"] == 4
            assert tablet["horizontalOverflow"] == 0
            assert tablet["detailBelow"] is True

            page.set_viewport_size({"width": 1900, "height": 900})
            page.wait_for_timeout(100)
            wide = geometry()
            assert wide["columnsFirstRow"] == 5
            assert wide["rowCount"] == 2
            assert wide["horizontalOverflow"] == 0
            browser.close()
    finally:
        frontend.shutdown()
        api.shutdown()
        frontend_thread.join(timeout=5)
        api_thread.join(timeout=5)
