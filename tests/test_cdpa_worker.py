from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_store as store_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import (
    AcquiredRole,
    RoleOwnershipError,
    TeamCloseError,
)
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_maintenance import ensure_maintenance_incident
from playwright_auto.cdpa_routes import RouteContractError
from playwright_auto.cdpa_store import TaskStore, utc_now
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop, _report_mode
from playwright_auto.chatgpt import (
    ChatGPTSnapshot,
    ChatGPTState,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    StableMalformedResponseError,
    capture_message_baseline,
    capture_response_recovery_baseline,
    response_activity_signature,
    prompt_digest,
)
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_core import (
    install_cycle_isolation_graph,
    install_duplicate_task_graph,
    write_config,
)


class FakeActions:
    def __init__(self):
        self.restart_roles = []
        self.located_roles = []
        self.closed_teams = 0
        self.preflight_calls = 0
        self.stop_calls = 0
        self.pages = ["assigned-page"]

    async def acquire(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=record.get("page_id") is None,
            new_chat=False,
        )

    async def restart(self, state, role, *, known_automated_draft=None):
        self.restart_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"restarted-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=True,
            new_chat=True,
        )

    async def locate_owned(self, state, role):
        self.located_roles.append(role)
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"page-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=False,
        )

    async def stop_if_active(self, acquired):
        self.stop_calls += 1
        return True

    async def new_chat(self, state, role):
        record = state["roles"][role]
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id=f"new-chat-{record['physical_role']}",
            url="https://chatgpt.com/",
            created=False,
            new_chat=True,
        )

    async def preflight_team(self, _state):
        self.preflight_calls += 1
        return list(self.pages)

    async def close_team(self, _state, *, preflighted_pages=None):
        selected = list(preflighted_pages or [])
        assert all(page in self.pages for page in selected)
        self.closed_teams += 1
        for page in selected:
            self.pages.remove(page)
        return len(selected)


def setup_task(tmp_path: Path, *, task_id="task-a", report_mode="file"):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Implement exact production behavior",
        requested_team="alpha",
        task_id=task_id,
        report_mode=report_mode,
    )
    return config, store, state, CDPAWorker(config, store=store)


def send_snapshot(*, text="", messages=(), state=ChatGPTState.NEW_CHAT):
    return ChatGPTSnapshot(
        url="https://chatgpt.com/c/cdpa-send",
        session_id="cdpa-send",
        page_id="page-PLAN",
        page_role="PLAN",
        page_task_id="task-a",
        page_team="alpha",
        state=state,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text=text,
        send_visible=bool(text),
        send_enabled=bool(text),
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=(),
        messages=tuple(messages),
    )


class RecordingCDPASendClient:
    def __init__(self):
        self.binding = PageBinding("page-PLAN", "PLAN")
        self.current = send_snapshot()
        self.set_calls = []
        self.send_calls = []

    async def assert_ownership(self):
        return self.current

    async def set_text(self, text):
        self.set_calls.append(text)
        self.current = send_snapshot(text=text, messages=self.current.messages, state=ChatGPTState.DRAFT)

    async def send(
        self,
        text,
        *,
        wait_for_stop=True,
        max_attempts=2,
        recovery_reload=True,
        expected_attachment_count=0,
    ):
        self.send_calls.append(text)
        baseline = capture_message_baseline(self.current.messages)
        user = MessageSnapshot("user", "u1", "t1", text, ())
        self.current = send_snapshot(
            messages=(*self.current.messages, user),
            state=ChatGPTState.SUBMITTING,
        )
        return SendReceipt(
            prompt=text,
            prompt_sha256=prompt_digest(text),
            binding=self.binding,
            baseline=baseline,
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="cdpa-send",
            user_message_id="u1",
            user_turn_id="t1",
        )


class RecordingCDPASendActions:
    def __init__(self, client):
        self.client = client

    async def locate_owned(self, _state, _role):
        return AcquiredRole(
            client=self.client,
            page_id="page-PLAN",
            url="https://chatgpt.com/c/cdpa-send",
            created=False,
            new_chat=False,
        )


def test_pre_send_lazily_acquires_only_plan_and_persists_constructor(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert hop["state"] == "sending"
    assert hop["expected_report_path"] == ".plan/alpha/alpha-plan_turn1_task-a.md"
    assert "Constructor for PLAN" in hop["prompt"]
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in hop["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" in hop["prompt"]
    envelope = json.loads(
        hop["prompt"].split("\n\n# PLAN", 1)[0].removeprefix("CDPA_TASK_ENVELOPE\n")
    )
    assert envelope == {
        "title": "Implement exact production behavior",
        "task-id": "task-a",
        "team": "alpha",
        "role": "alpha-plan",
        "source-role": None,
        "turn": 1,
        "workspace": str(tmp_path),
        "allowed-routes": ["PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "DONE"],
        "goal": "Implement exact production behavior",
        "handoff": "Implement exact production behavior",
    }
    assert state["roles"]["PLAN"]["page_id"] == "page-alpha-plan"
    assert state["roles"]["DEV"]["status"] == "unallocated"
    assert state["roles"]["PLAN"]["constructor_sent_generation"] == 0


def test_normal_cdpa_send_keeps_transport_identity_out_of_actual_payload(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    client = RecordingCDPASendClient()

    asyncio.run(worker._sending(state, hop, RecordingCDPASendActions(client)))

    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert client.send_calls == [hop["prompt"]]
    assert hop["receipt"]["prompt"] == hop["prompt"]
    assert record is not None
    assert record.rendered_prompt == hop["prompt"]
    for forbidden in (
        "ROLE_REQUEST_ID",
        hop["request_id"],
        hop["ledger_path"],
        state["manifest_path"],
        hop["expected_report_path"],
        "controller_id",
        "run_id",
        "prompt_sha256",
        "created_at",
        "updated_at",
        "page_id",
    ):
        assert forbidden not in client.send_calls[0]
        assert forbidden not in hop["receipt"]["prompt"]


def test_route_repair_send_keeps_transport_identity_out_of_actual_payload(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-repair-payload")
    original = _active_hop(state)
    asyncio.run(worker._pre_send(state, original, FakeActions()))
    original["response"] = "not json"
    original["state"] = "responded"
    worker._responded(state, original)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    client = RecordingCDPASendClient()

    asyncio.run(worker._sending(state, repair, RecordingCDPASendActions(client)))

    record = RequestLedger(repair["ledger_path"]).get(repair["request_id"])
    assert client.send_calls == [repair["prompt"]]
    assert repair["receipt"]["prompt"] == repair["prompt"]
    assert record is not None
    assert record.rendered_prompt == repair["prompt"]
    for forbidden in (
        "ROLE_REQUEST_ID",
        repair["request_id"],
        repair["ledger_path"],
        state["manifest_path"],
        repair["expected_report_path"],
        "controller_id",
        "run_id",
        "prompt_sha256",
        "created_at",
        "updated_at",
        "page_id",
        "Implement exact production behavior",
    ):
        assert forbidden not in client.send_calls[0]
        assert forbidden not in repair["receipt"]["prompt"]


def test_valid_report_routes_to_lazy_dev_child_and_records_sha(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    hop["response"] = '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    child = _active_hop(state)
    assert hop["state"] == "routed"
    assert child["target_role"] == "DEV"
    assert child["state"] == "pre_send"
    assert state["active_role"] == "DEV"
    assert state["roles"]["DEV"]["status"] == "pending"
    assert len(state["reports"]) == 1
    assert len(state["reports"][0]["sha256"]) == 64


def test_successful_route_clears_stale_block_state(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "stale transport error"
    state["pause_reason"] = "stale pause"
    hop["response"] = '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "RUNNING"
    assert state["block_reason"] is None
    assert state["pause_reason"] is None


def test_malformed_response_creates_same_turn_guide_only_repair(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = "not json"
    hop["state"] = "responded"

    worker._responded(state, hop)
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == 1
    assert repair["repair_attempt"] == 1

    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert "Implement exact production behavior" not in repair["prompt"]
    assert "Validation error:" in repair["prompt"]
    assert ".plan/alpha/alpha-plan_turn1_task-a.md" not in repair["prompt"]
    assert ".plan/<team>/<physical-role>_turn<N>_<task-id>.md" in repair["prompt"]
    assert "Constructor for PLAN" not in repair["prompt"]
    assert repair["handoff"] == "route repair"


def test_only_plan_done_can_make_task_terminal(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path)
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-a.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("final evidence", encoding="utf-8")
    hop["response"] = '{"route":"DONE","handoff":".plan/alpha/alpha-plan_turn1_task-a.md"}'
    hop["state"] = "responded"

    worker._responded(state, hop)

    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["active_hop_id"] is None
    assert state["kanban_column"] == "DONE_STOPPED"


def test_resume_and_retry_controls_have_distinct_state_contracts(tmp_path: Path):
    _, _, paused, worker = setup_task(tmp_path, task_id="task-paused")
    paused["status"] = "PAUSED"
    paused["kanban_column"] = "PAUSED"
    paused["resume_column"] = "PLANNING"
    paused["pause_reason"] = "manual"
    paused_request_id = _active_hop(paused)["request_id"]
    paused["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(paused, FakeActions())) is True
    assert paused["status"] == "RUNNING"
    assert paused["kanban_column"] == "PLANNING"
    assert paused["controls"][0]["status"] == "applied"
    assert paused["controls"][0]["result"]["request_id"] == paused_request_id
    assert _active_hop(paused)["request_id"] == paused_request_id

    _, _, blocked, worker = setup_task(tmp_path, task_id="task-blocked")
    blocked["status"] = "BLOCKED"
    blocked["kanban_column"] = "BLOCKED"
    blocked["block_reason"] = "validation failed"
    blocked_hop = _active_hop(blocked)
    blocked_hop["repair_attempt"] = 2
    blocked_request_id = blocked_hop["request_id"]
    blocked["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(blocked, FakeActions())) is True
    assert blocked["status"] == "RUNNING"
    assert blocked["kanban_column"] == "PLANNING"
    assert blocked["block_reason"] is None
    assert blocked["controls"][0]["status"] == "applied"
    assert blocked["controls"][0]["result"]["request_id"] == blocked_request_id
    assert _active_hop(blocked)["repair_attempt"] == 2
    assert _active_hop(blocked)["request_id"] == blocked_request_id

    _, _, retried, worker = setup_task(tmp_path, task_id="task-retry")
    retried["status"] = "BLOCKED"
    retried["kanban_column"] = "BLOCKED"
    retried["block_code"] = "route_validation_exhausted"
    retried["block_retryable"] = True
    retried["block_reason"] = "route repair exhausted"
    retry_hop = _active_hop(retried)
    retry_hop["validation_error"] = "bad route"
    retry_hop["repair_attempt"] = worker.config.route_repair_attempts
    retried["controls"] = [
        {
            "control_id": 1,
            "action": "retry",
            "role": "PLAN",
            "status": "requested",
        }
    ]
    assert asyncio.run(worker._apply_control(retried, FakeActions())) is True
    assert retried["status"] == "RUNNING"
    assert retry_hop["repair_attempt"] == worker.config.route_repair_attempts - 1


def test_block_deduplicates_an_unchanged_active_error(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-block-dedup")
    message = "recorded role tab is offline"

    worker._block(
        state,
        message,
        code="role_offline",
        retryable=False,
    )
    task_error_count = len(state["errors"])
    hop_error_count = len(_active_hop(state)["errors"])

    worker._block(
        state,
        message,
        code="role_offline",
        retryable=False,
    )

    assert len(state["errors"]) == task_error_count
    assert len(_active_hop(state)["errors"]) == hop_error_count


def test_resume_recheck_ownership_failure_reblocks_once(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-resume-reblock")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_code"] = "role_offline"
    state["block_retryable"] = False
    state["block_reason"] = "recorded 'alpha-plan' tab is offline"
    state["errors"] = [
        {"at": "2026-07-22T00:00:00+00:00", "error": state["block_reason"]}
    ]
    hop["errors"] = [state["block_reason"]]
    state["controls"] = [
        {
            "control_id": 1,
            "action": "resume",
            "role": "PLAN",
            "status": "requested",
            "requested_at": "2026-07-22T00:00:01+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)

    class OfflineRecordedRoleActions(FakeActions):
        async def acquire(self, _state, _role):
            raise RoleOwnershipError(
                "recorded 'alpha-plan' tab is offline; use Open tab for controlled recovery"
            )

    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: OfflineRecordedRoleActions(),
    )

    first = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
    control = first["controls"][0]
    assert first["status"] == "BLOCKED"
    assert control["status"] == "reblocked"
    assert control["applied_at"] is not None
    assert control["result"] == {
        "block_code": "role_ownership_ambiguous",
        "block_retryable": False,
        "block_reason": (
            "RoleOwnershipError: recorded 'alpha-plan' tab is offline; "
            "use Open tab for controlled recovery"
        ),
    }
    assert _active_hop(first)["state"] == "pre_send"
    assert _active_hop(first).get("receipt") is None

    task_error_count = len(first["errors"])
    hop_error_count = len(_active_hop(first)["errors"])
    persisted_once = path.read_bytes()
    for _ in range(3):
        later = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))
        assert later["controls"][0]["status"] == "reblocked"
        assert len(later["errors"]) == task_error_count
        assert len(_active_hop(later)["errors"]) == hop_error_count
        assert path.read_bytes() == persisted_once


def test_terminal_idle_cleanup_preflight_failure_preserves_terminal_result(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-auto-cleanup-preflight")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE_STOPPED"
    state["completed_at"] = "2026-07-20T00:00:00+00:00"
    state["last_role_activity_at"] = "2026-07-20T00:00:00+00:00"
    state["active_role"] = None
    state["active_hop_id"] = None
    store.save(path, state)

    class DuplicateActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "DONE"
    assert result["terminal_state"] == "DONE"
    assert result["completed_at"] == "2026-07-20T00:00:00+00:00"
    assert result["cleanup"]["state"] == "ACTIVE"
    assert "duplicate exact role tabs" in result["cleanup"]["last_error"]
    assert result["block_code"] is None


def test_clear_team_preserves_done_terminal_state(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-done")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["kanban_column"] = "DONE_STOPPED"
    state["completed_at"] = "2026-07-22T00:00:00+00:00"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": False,
        "status": "requested",
    }]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    state = store.load(path)
    assert state["status"] == "DONE"
    assert state["terminal_state"] == "DONE"
    assert state["completed_at"] == "2026-07-22T00:00:00+00:00"
    assert state.get("stopped_at") is None
    assert state["cleanup"]["state"] == "CLEARED"
    assert actions.preflight_calls == 2
    assert actions.closed_teams == 1


def test_clear_team_preflight_failure_happens_before_stop_or_close(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-clear-preflight")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class DuplicateActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("duplicate exact role tabs")

    actions = DuplicateActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    assert state["controls"][0]["status"] == "rejected"
    assert actions.stop_calls == 0
    assert actions.closed_teams == 0
    assert state["cleanup"]["state"] == "ACTIVE"
    assert state["status"] == "INBOX"
    assert state["active_hop_id"] == 1


def test_clear_team_stop_failure_persists_stopped_recoverable_state_and_resumes(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-stop-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class StopFailureActions(FakeActions):
        async def stop_if_active(self, _acquired):
            self.stop_calls += 1
            raise RuntimeError("synthetic stop failure")

    failing = StopFailureActions()
    assert asyncio.run(worker._apply_control(state, failing, path)) is True
    persisted = store.load(path)
    assert persisted["status"] == "STOPPED"
    assert persisted["active_role"] is None
    assert persisted["active_hop_id"] is None
    assert persisted["hops"][0]["state"] == "abandoned"
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "stop_pending"
    assert "synthetic stop failure" in persisted["cleanup"]["last_error"]
    assert persisted["controls"][0]["status"] == "cleanup_pending"

    resumed = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: resumed)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "STOPPED"
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["retry_count"] == 1
    assert result["controls"][0]["status"] == "applied"
    assert resumed.stop_calls == 1
    assert resumed.closed_teams == 1
    assert result["block_code"] is None


def test_clear_team_partial_close_failure_resumes_remaining_tabs(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-clear-close-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class PartialCloseActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.pages = ["page-one", "page-two"]
            self.fail_once = True

        async def preflight_team(self, _state):
            self.preflight_calls += 1
            return list(self.pages)

        async def close_team(self, _state, *, preflighted_pages=None):
            self.closed_teams += 1
            selected = list(preflighted_pages or self.pages)
            if self.fail_once:
                self.fail_once = False
                self.pages.remove(selected[0])
                raise TeamCloseError(
                    "synthetic second-tab close failure",
                    closed_tabs=1,
                )
            closed = len(selected)
            self.pages.clear()
            return closed

    actions = PartialCloseActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    persisted = store.load(path)
    assert persisted["status"] == "STOPPED"
    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "close_pending"
    assert persisted["cleanup"]["target_tabs"] == 2
    assert "second-tab close failure" in persisted["cleanup"]["last_error"]

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["closed_tabs"] == 2
    assert result["cleanup"]["retry_count"] == 1
    assert actions.pages == []
    assert result["active_hop_id"] is None


def test_resumed_cleanup_preflight_inspection_failure_remains_clearing(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-inspection-unknown")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": "close_pending",
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 1,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    store.save(path, state)

    class UnreadableActions(FakeActions):
        async def preflight_team(self, _state):
            self.preflight_calls += 1
            raise RuntimeError("transient cleanup inspection failure")

    actions = UnreadableActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARING"
    assert result["cleanup"]["phase"] == "close_pending"
    assert result["cleanup"]["closed_tabs"] == 0
    assert result["cleanup"]["retry_count"] == 1
    assert "inspection failure" in result["cleanup"]["last_error"]
    assert result["active_action"] == "cleanup_recoverable"
    assert actions.closed_teams == 0


def test_zero_remaining_tabs_with_historical_target_does_not_infer_closures(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-no-inference")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": "close_pending",
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 2,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    store.save(path, state)

    actions = FakeActions()
    actions.pages.clear()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["target_tabs"] == 2
    assert result["cleanup"]["closed_tabs"] == 0
    assert actions.closed_teams == 1
    assert actions.preflight_calls == 2


def test_post_close_verification_failure_preserves_evidence_and_resumes(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleanup-verify-failure")
    path = Path(state["manifest_path"])
    state["controls"] = [{
        "control_id": 1,
        "action": "clear_team",
        "role": "PLAN",
        "confirmed": True,
        "status": "requested",
    }]

    class VerifyFailureActions(FakeActions):
        def __init__(self):
            super().__init__()
            self.fail_verify = True

        async def preflight_team(self, _state):
            self.preflight_calls += 1
            if self.fail_verify and self.closed_teams:
                self.fail_verify = False
                raise RuntimeError("post-close verification unavailable")
            return list(self.pages)

    actions = VerifyFailureActions()
    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    persisted = store.load(path)

    assert persisted["cleanup"]["state"] == "CLEARING"
    assert persisted["cleanup"]["phase"] == "verify_pending"
    assert persisted["cleanup"]["closed_tabs"] == 1
    assert persisted["cleanup"]["retry_count"] == 1
    assert "verification unavailable" in persisted["cleanup"]["last_error"]
    assert persisted["controls"][0]["status"] == "cleanup_pending"

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["closed_tabs"] == 1
    assert result["cleanup"]["verified_empty_at"]
    assert result["cleanup"]["retry_count"] == 1
    assert result["controls"][0]["status"] == "applied"
    assert actions.closed_teams == 1


def test_legacy_half_cleared_running_manifest_is_normalized_before_cleanup_resume(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(tmp_path, task_id="task-legacy-clearing")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    state["status"] = "RUNNING"
    state["active_role"] = "PLAN"
    state["active_hop_id"] = 1
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": None,
        "status_before": None,
        "active_role": None,
        "active_hop_id": None,
    })
    store.save(path, state)

    actions = FakeActions()
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "STOPPED"
    assert result["active_role"] is None
    assert result["active_hop_id"] is None
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["status_before"] == "RUNNING"
    assert result["cleanup"]["active_role"] == "PLAN"
    assert result["cleanup"]["active_hop_id"] == 1
    assert result["block_code"] is None


@pytest.mark.parametrize("phase", ["stop_pending", "close_pending", "closing", "verify_pending"])
def test_worker_restart_resumes_every_persisted_cleanup_phase(
    tmp_path: Path, monkeypatch, phase: str
):
    _, store, state, worker = setup_task(tmp_path, task_id=f"task-cleanup-{phase}")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "abandoned"
    hop["abandon_reason"] = "cleanup restart fixture"
    state["status"] = "STOPPED"
    state["terminal_state"] = "STOPPED"
    state["kanban_column"] = "DONE_STOPPED"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["cleanup"].update({
        "state": "CLEARING",
        "phase": phase,
        "status_before": "RUNNING",
        "active_role": "PLAN",
        "active_hop_id": 1,
        "target_tabs": 1,
        "closed_tabs": 0,
        "retry_count": 0,
    })
    store.save(path, state)

    actions = FakeActions()
    if phase == "verify_pending":
        actions.pages.clear()
        state["cleanup"]["closed_tabs"] = 1
        store.save(path, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["cleanup"]["state"] == "CLEARED"
    assert result["status"] == "STOPPED"
    assert result["active_hop_id"] is None
    assert result["block_code"] is None
    assert actions.stop_calls == (1 if phase == "stop_pending" else 0)
    assert actions.closed_teams == (0 if phase == "verify_pending" else 1)


def test_waiting_upgrades_legacy_collapsed_receipt_in_hop_and_ledger(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-legacy-upgrade")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="stop_button",
        session_id_before=None,
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    store.save(path, state)
    collapsed = MessageSnapshot(
        "user", "u-collapsed", "t-collapsed", prompt[:2000] + " Show more", ()
    )
    snapshot = SimpleNamespace(
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        messages=(collapsed,),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, receipt_value, **_kwargs):
            assert receipt_value.user_message_id == "u-collapsed"
            raise TimeoutError("continue")

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert hop["receipt"]["accepted_via"] == "user_message_identity"
    assert hop["receipt"]["user_message_id"] == "u-collapsed"
    assert hop["receipt"]["user_turn_id"] == "t-collapsed"
    persisted = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert persisted is not None
    assert persisted.receipt["user_message_id"] == "u-collapsed"
    assert persisted.receipt["user_turn_id"] == "t-collapsed"


def test_later_handoff_prompt_preserves_original_goal_and_source_role(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-envelope")
    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-envelope.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    plan_hop["response"] = json.dumps(
        {"route": "DEV", "handoff": ".plan/alpha/alpha-plan_turn1_task-envelope.md"}
    )
    plan_hop["state"] = "responded"
    worker._responded(state, plan_hop)
    dev_hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, dev_hop, FakeActions()))
    envelope = json.loads(
        dev_hop["prompt"].split("\n\n# DEV", 1)[0].removeprefix(
            "CDPA_TASK_ENVELOPE\n"
        )
    )
    assert envelope["role"] == "alpha-dev"
    assert envelope["source-role"] == "alpha-plan"
    assert envelope["handoff"] == ".plan/alpha/alpha-plan_turn1_task-envelope.md"
    assert envelope["goal"] == "Implement exact production behavior"
    assert dev_hop["expected_report_path"] == ".plan/alpha/alpha-dev_turn1_task-envelope.md"
    assert dev_hop["expected_report_path"] not in dev_hop["prompt"]
    for forbidden in (
        dev_hop["request_id"], dev_hop["ledger_path"], "prompt_sha256",
        "controller_id", "run_id", "page_id", "created_at",
    ):
        assert forbidden not in dev_hop["prompt"]


def test_open_tab_recovers_presend_role_offline_and_sends_original_once(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-open-tab-presend-recovery"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
        active_action="blocked",
    )
    state["roles"]["PLAN"].update(
        page_id="closed-page",
        page_url="https://chatgpt.com/c/owned-plan",
        online=False,
    )
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact owned PLAN tab",
            "confirmed": False,
            "status": "requested",
            "requested_at": "2026-07-23T00:00:00+00:00",
            "applied_at": None,
            "result": None,
        }
    ]
    store.save(path, state)
    sent_prompts: list[str] = []

    class RecoveryClient:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/owned-plan",
                url="https://chatgpt.com/c/owned-plan",
            )

    client = RecoveryClient()
    acquired = AcquiredRole(
        client=client,
        page_id="recovered-page",
        url="https://chatgpt.com/c/owned-plan",
        created=False,
        new_chat=False,
    )

    class RecoveryActions:
        def __init__(self):
            self.recovered = False

        async def locate_owned(self, _state, _role):
            return acquired if self.recovered else None

        async def reopen(self, _state, _role):
            self.recovered = True
            return acquired

        async def open_tab(self, _acquired):
            self.recovered = True

        async def acquire(self, _state, _role):
            assert self.recovered is True
            return acquired

    actions = RecoveryActions()
    monkeypatch.setattr(
        worker_module,
        "CDPATabActions",
        lambda *_args, **_kwargs: actions,
    )

    class OneSendBlock:
        def __init__(self, prompt, **_kwargs):
            self.prompt = prompt

        async def run(self, _context):
            sent_prompts.append(self.prompt)
            receipt = SendReceipt(
                prompt=self.prompt,
                prompt_sha256=prompt_digest(self.prompt),
                binding=PageBinding("recovered-page", "alpha-plan"),
                baseline=MessageBaseline(
                    frozenset(), frozenset(), frozenset(), frozenset()
                ),
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="owned-plan",
                user_message_id="user-1",
                user_turn_id="turn-1",
            )
            return {
                "receipt": receipt.to_dict(),
                "record": {
                    "accepted_at": datetime.now(timezone.utc).timestamp(),
                },
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", OneSendBlock)
    context = SimpleNamespace(pages=[])

    recovered = asyncio.run(worker.advance(path, context))
    recovered_hop = _active_hop(recovered)
    assert recovered["status"] == "RUNNING"
    assert recovered["kanban_column"] == "PLANNING"
    assert recovered["block_code"] is None
    assert recovered["block_reason"] is None
    assert recovered["roles"]["PLAN"]["online"] is True
    assert recovered["roles"]["PLAN"]["page_id"] == "recovered-page"
    assert recovered_hop["hop_id"] == original_hop_id
    assert recovered_hop["request_id"] == original_request_id
    assert recovered_hop["state"] == "pre_send"
    assert recovered["controls"][0]["status"] == "applied"
    assert recovered["controls"][0]["result"]["resumed"] is True
    assert sent_prompts == []

    prepared = asyncio.run(worker.advance(path, context))
    assert _active_hop(prepared)["state"] == "sending"
    assert _active_hop(prepared)["request_id"] == original_request_id
    assert sent_prompts == []

    sent = asyncio.run(worker.advance(path, context))
    assert _active_hop(sent)["state"] == "sent"
    assert _active_hop(sent)["request_id"] == original_request_id
    assert len(sent["hops"]) == 1
    assert len(sent_prompts) == 1

    waiting = asyncio.run(worker.advance(path, context))
    assert _active_hop(waiting)["state"] == "waiting"
    assert _active_hop(waiting)["request_id"] == original_request_id
    assert len(sent_prompts) == 1


@pytest.mark.parametrize("hop_state", ["sending", "sent", "waiting"])
def test_open_tab_rejects_inflight_role_offline_without_browser_mutation(
    tmp_path: Path,
    hop_state: str,
):
    _, _, state, worker = setup_task(
        tmp_path, task_id=f"task-open-tab-{hop_state}"
    )
    hop = _active_hop(state)
    hop["state"] = hop_state
    if hop_state in {"sent", "waiting"}:
        hop["receipt"] = {
            "request_id": hop["request_id"],
            "binding": {"page_id": "closed-page"},
        }
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="owned alpha-plan tab is offline",
    )
    state["roles"]["PLAN"].update(page_id="closed-page", online=False)
    state["controls"] = [
        {
            "control_id": 1,
            "action": "open_tab",
            "role": "PLAN",
            "reason": "recover exact owned PLAN tab",
            "status": "requested",
        }
    ]

    class NoBrowserMutation:
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("in-flight recovery must reject before browser mutation")

    assert asyncio.run(worker._apply_control(state, NoBrowserMutation())) is True
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "role_offline"
    assert _active_hop(state)["state"] == hop_state
    assert state["roles"]["PLAN"]["page_id"] == "closed-page"
    assert state["controls"][0]["status"] == "rejected"
    assert "pre_send" in state["controls"][0]["result"]


def test_blocked_unsent_restart_abandons_old_hop_and_creates_new_turn(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-restart")
    hop = _active_hop(state)
    old_request_id = hop["request_id"]
    original_handoff = hop["handoff"]
    state["roles"]["PLAN"]["turn"] = 1
    state["roles"]["PLAN"]["page_id"] = "closed-page"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "PLAN",
            "reason": "controlled recovery",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    old_hop = state["hops"][0]
    new_hop = _active_hop(state)
    assert old_hop["state"] == "abandoned"
    assert old_hop["request_id"] == old_request_id
    assert old_hop["receipt"] is None
    assert old_hop["abandon_reason"] == "controlled recovery"
    assert old_hop["timestamps"]["abandoned_at"]
    assert new_hop["hop_id"] == 2
    assert new_hop["parent_hop_id"] == 1
    assert new_hop["state"] == "pre_send"
    assert new_hop["kind"] == "role_restart"
    assert new_hop["turn"] == 2
    assert new_hop["request_id"] == "task-restart-hop2"
    assert new_hop["handoff"] == original_handoff
    assert state["status"] == "RUNNING"
    assert state["block_reason"] is None
    assert state["roles"]["PLAN"]["page_id"] == "restarted-alpha-plan"
    assert state["roles"]["PLAN"]["constructor_sent_generation"] is None
    assert state["controls"][0]["status"] == "applied"
    assert state["controls"][0]["result"] == {
        "old_hop_id": 1,
        "new_hop_id": 2,
        "old_page_id": "closed-page",
        "page_id": "restarted-alpha-plan",
        "new_chat": True,
    }
    assert state["route_timeline"][-1]["kind"] == "role_restart"
    assert state["route_timeline"][-1]["new_hop_id"] == 2


@pytest.mark.parametrize("hop_state", ["sending", "sent", "waiting"])
def test_blocked_inflight_restart_is_rejected_without_tab_mutation(
    tmp_path: Path,
    hop_state: str,
):
    _, _, state, worker = setup_task(tmp_path, task_id=f"task-{hop_state}")
    hop = _active_hop(state)
    hop["state"] = hop_state
    if hop_state in {"sent", "waiting"}:
        hop["receipt"] = {"request_id": hop["request_id"], "attempts": 1}
    state["roles"]["PLAN"]["page_id"] = "closed-page"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "PLAN",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert state["controls"][0]["status"] == "rejected"
    assert "in-flight" in state["controls"][0]["result"]
    assert actions.restart_roles == []
    assert state["status"] == "BLOCKED"
    assert state["active_hop_id"] == 1
    assert _active_hop(state)["state"] == hop_state


def test_blocked_restart_validates_selected_role_before_tab_mutation(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-wrong-role")
    hop = _active_hop(state)
    hop["state"] = "waiting"
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "owned PLAN tab is offline"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "restart_role",
            "role": "DEV",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert state["controls"][0]["status"] == "rejected"
    assert actions.restart_roles == []
    assert state["roles"]["DEV"]["page_id"] is None
    assert _active_hop(state)["state"] == "waiting"


def test_stop_targets_active_role_even_if_control_selects_another_role(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-stop-active")
    state["controls"] = [
        {
            "control_id": 1,
            "action": "stop",
            "role": "DEV",
            "status": "requested",
        }
    ]
    actions = FakeActions()

    assert asyncio.run(worker._apply_control(state, actions)) is True

    assert actions.located_roles == ["PLAN"]
    assert state["status"] == "STOPPED"
    assert state["controls"][0]["status"] == "applied"


def test_route_plan_abandons_superseded_unsent_hop(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-route-plan")
    plan_hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, plan_hop, FakeActions()))
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-route-plan.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("plan evidence", encoding="utf-8")
    plan_hop["response"] = (
        '{"route":"DEV","handoff":".plan/alpha/alpha-plan_turn1_task-route-plan.md"}'
    )
    plan_hop["state"] = "responded"
    worker._responded(state, plan_hop)
    dev_hop = _active_hop(state)
    state["controls"] = [
        {
            "control_id": 1,
            "action": "route_plan",
            "role": "DEV",
            "reason": "manual reassignment",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    new_hop = _active_hop(state)
    assert dev_hop["state"] == "abandoned"
    assert dev_hop["abandon_reason"] == "manual reassignment"
    assert new_hop["target_role"] == "PLAN"
    assert new_hop["parent_hop_id"] == dev_hop["hop_id"]
    assert state["route_timeline"][-1]["kind"] == "control"
    assert state["route_timeline"][-1]["new_hop_id"] == new_hop["hop_id"]


def test_new_chat_remains_rejected_at_inflight_boundary(tmp_path: Path):
    _, _, state, worker = setup_task(tmp_path, task_id="task-new-chat")
    hop = _active_hop(state)
    hop["state"] = "waiting"
    state["status"] = "RUNNING"
    state["controls"] = [
        {
            "control_id": 1,
            "action": "new_chat",
            "role": "PLAN",
            "status": "requested",
        }
    ]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True

    assert state["controls"][0]["status"] == "rejected"
    assert "in-flight" in state["controls"][0]["result"]
    assert _active_hop(state)["state"] == "waiting"


def test_terminal_task_without_due_cleanup_is_not_rewritten(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-terminal-idle")
    path = Path(state["manifest_path"])
    state["status"] = "DONE"
    state["terminal_state"] = "DONE"
    state["active_role"] = None
    state["active_hop_id"] = None
    state["completed_at"] = state["created_at"]
    state["last_role_activity_at"] = state["created_at"]
    store.save(path, state)
    before = path.read_bytes()

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "DONE"
    assert path.read_bytes() == before


def test_blocked_task_does_not_advance_without_explicit_control(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_reason"] = "manual inspection required"
    store.save(path, state)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert result["active_hop_id"] == 1
    assert _active_hop(result)["state"] == "pre_send"
    assert result["block_reason"] == "manual inspection required"


def test_accepted_send_initializes_wait_budget_at_sent_timestamp(
    tmp_path: Path,
    monkeypatch,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-accepted")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    accepted_at = datetime.now(timezone.utc) - timedelta(seconds=3)

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/exact",
                url="https://chatgpt.com/c/exact",
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-PLAN",
        url="https://chatgpt.com/",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    class FakeDurableSendBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            return {
                "receipt": {"prompt_sha256": hop["prompt_sha256"]},
                "record": {"status": "sent", "accepted_at": accepted_at.timestamp()},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)

    asyncio.run(worker._sending(state, hop, Actions()))

    assert datetime.fromisoformat(hop["timestamps"]["sent_at"]) == accepted_at
    assert datetime.fromisoformat(hop["wait"]["started_at"]) == accepted_at
    assert datetime.fromisoformat(hop["wait"]["deadline_at"]) == accepted_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )


def test_recovered_sending_uses_original_sending_anchor_when_acceptance_time_missing(
    tmp_path: Path,
    monkeypatch,
):
    _, _, state, worker = setup_task(tmp_path, task_id="task-recovered-sending")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    sending_at = datetime.now(timezone.utc) - timedelta(hours=3)
    hop["timestamps"]["sending_at"] = sending_at.isoformat()

    class Client:
        async def assert_ownership(self):
            return SimpleNamespace(
                conversation_url="https://chatgpt.com/c/recovered",
                url="https://chatgpt.com/c/recovered",
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-PLAN",
        url="https://chatgpt.com/",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    class FakeDurableSendBlock:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _context):
            return {
                "cached": True,
                "receipt": {"prompt_sha256": hop["prompt_sha256"]},
                "record": {"status": "sent", "accepted_at": None},
            }

    monkeypatch.setattr(worker_module, "DurableSendBlock", FakeDurableSendBlock)

    asyncio.run(worker._sending(state, hop, Actions()))

    assert datetime.fromisoformat(hop["timestamps"]["sent_at"]) == sending_at
    assert datetime.fromisoformat(hop["wait"]["started_at"]) == sending_at
    assert datetime.fromisoformat(hop["wait"]["deadline_at"]) == sending_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )


def test_restart_boundaries_resume_without_restarting_prior_hops(tmp_path: Path):
    _, store, sent, worker = setup_task(tmp_path, task_id="task-sent")
    sent_path = Path(sent["manifest_path"])
    sent_hop = _active_hop(sent)
    request_id = sent_hop["request_id"]
    accepted_at = datetime.now(timezone.utc) - timedelta(hours=3)
    expected_deadline = accepted_at + timedelta(
        seconds=worker.config.response_timeout_seconds
    )
    sent_hop["state"] = "sent"
    sent_hop["receipt"] = {"request_id": request_id}
    sent_hop["timestamps"]["sent_at"] = accepted_at.isoformat()
    store.save(sent_path, sent)

    resumed_sent = asyncio.run(worker.advance(sent_path, SimpleNamespace(pages=[])))

    resumed_hop = _active_hop(resumed_sent)
    assert resumed_hop["state"] == "waiting"
    assert resumed_hop["request_id"] == request_id
    assert datetime.fromisoformat(resumed_hop["wait"]["started_at"]) == accepted_at
    assert datetime.fromisoformat(resumed_hop["wait"]["deadline_at"]) == expected_deadline

    _, store, responded, worker = setup_task(tmp_path, task_id="task-responded")
    responded_path = Path(responded["manifest_path"])
    responded_hop = _active_hop(responded)
    report_relative = (
        f".plan/{responded['team']}/"
        f"{responded['roles']['PLAN']['physical_role']}_turn1_task-responded.md"
    )
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("restart boundary evidence", encoding="utf-8")
    responded_hop["state"] = "responded"
    responded_hop["response"] = json.dumps(
        {"route": "DONE", "handoff": report_relative}
    )
    store.save(responded_path, responded)

    resumed_response = asyncio.run(
        worker.advance(responded_path, SimpleNamespace(pages=[]))
    )

    assert resumed_response["status"] == "DONE"
    assert resumed_response["active_hop_id"] is None
    assert len(resumed_response["reports"]) == 1


def test_inflight_offline_role_blocks_without_opening_or_resending(tmp_path: Path):
    for suffix, hop_state in enumerate(("sending", "waiting"), start=1):
        _, store, state, worker = setup_task(
            tmp_path, task_id=f"task-offline-{suffix}"
        )
        path = Path(state["manifest_path"])
        hop = _active_hop(state)
        hop["state"] = hop_state
        if hop_state == "waiting":
            hop["receipt"] = {"request_id": hop["request_id"]}
        store.save(path, state)

        result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

        assert result["status"] == "BLOCKED"
        assert "offline" in result["block_reason"]
        assert _active_hop(result)["state"] == hop_state


def test_corrupt_manifest_is_excluded_from_worker_loop_and_remains_diagnostic(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path)
    Path(state["manifest_path"]).write_text("{broken", encoding="utf-8")

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
    tasks, errors = store.discover_with_errors()

    assert results == []
    assert tasks == []
    assert len(errors) == 1
    assert errors[0]["manifest_path"] == state["manifest_path"]
    assert errors[0]["error"].startswith("InvalidManifestError:")


def test_routed_parent_without_child_fails_closed_after_restart(tmp_path: Path):
    config, store, state, worker = setup_task(tmp_path)
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "routed"
    store.save(path, state)

    result = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "BLOCKED"
    assert "no durable child" in result["block_reason"]


def _prepare_sent_waiting_task(
    tmp_path: Path,
    *,
    task_id: str,
    report_mode: str = "file",
):
    _, store, state, worker = setup_task(
        tmp_path, task_id=task_id, report_mode=report_mode
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=prompt,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    sent_at = datetime.now(timezone.utc)
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = sent_at.isoformat()
    worker._start_wait_budget_from_sent(hop)
    state["roles"]["PLAN"]["page_id"] = "page-alpha-plan"
    store.save(path, state)
    return store, state, worker, path, hop, receipt, sent_at


def test_waiting_requires_valid_route_report_and_two_samples_before_hop_response(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-valid-gate"
    )
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-valid-gate.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("valid report", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-valid",
        "ta-valid",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-valid-gate.md"}',
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )

    class Client:
        def __init__(self):
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert client.kwargs["minimum_samples"] == 2
    assert client.kwargs["invalid_grace_ms"] >= 1_000
    assert hop["state"] == "responded"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    assert _active_hop(state)["target_role"] == "TEST"


def test_waiting_records_stable_malformed_final_then_repairs_on_next_boundary(tmp_path: Path):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-malformed-gate"
    )
    malformed = MessageSnapshot("assistant", "a-bad", "ta-bad", "final prose", ())
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), malformed),
    )

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise StableMalformedResponseError(
                malformed,
                worker_module.RouteContractError("response must contain only one route JSON object"),
            )

    acquired = AcquiredRole(
        client=Client(),
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))
    assert hop["state"] == "responded"
    assert hop["validation_error"] == "response must contain only one route JSON object"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    assert repair["turn"] == 1


def test_timeout_banner_without_stop_refreshes_after_no_progress_and_accepts_rehydrated_route(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-timeout-f5"
    )
    old = sent_at - timedelta(minutes=21)
    report = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-timeout-f5.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("rehydrated report", encoding="utf-8")
    progress = MessageSnapshot(
        "assistant",
        "a-progress",
        "ta-progress",
        "Inspecting durable block and searching for functions",
        (),
    )
    final = MessageSnapshot(
        "assistant",
        "a-final",
        "ta-final",
        '{"route":"TEST","handoff":".plan/alpha/alpha-plan_turn1_task-timeout-f5.md"}',
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.ERROR,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=("Message delivery timed out. Please try again.",),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), progress),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    hop["timestamps"]["sent_at"] = old.isoformat()
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("no acceptable candidate before refresh")
            kwargs["candidate_validator"](final)
            return final

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            persisted_wait = _active_hop(persisted)["wait"]
            assert persisted_wait["activity_signature"] == signature
            assert persisted_wait["activity_length"] == length
            assert persisted_wait["activity_changed_at"] == old.isoformat()
            assert persisted_wait["recovery_baseline"]["assistant_message_ids"] == ["a-progress"]

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 1
    assert client.calls == 2
    assert hop["state"] == "responded"
    assert hop["response"] == final.text
    assert hop["request_id"] == "task-timeout-f5-hop1"
    assert hop["turn"] == 1


def test_waiting_persists_pre_refresh_provenance_before_f5(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-refresh-baseline")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    prompt = hop["prompt"]
    binding = PageBinding("page-PLAN", "PLAN")
    receipt = SendReceipt(
        prompt=prompt,
        prompt_sha256=prompt_digest(prompt),
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id="u1",
        user_turn_id="t1",
    )
    sent_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    hop["receipt"] = receipt.to_dict()
    hop["state"] = "waiting"
    hop["timestamps"]["sent_at"] = sent_at.isoformat()
    hop["wait"].update(
        {
            "started_at": sent_at.isoformat(),
            "deadline_at": (sent_at + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": sent_at.isoformat(),
        }
    )
    state["roles"]["PLAN"]["page_id"] = "page-PLAN"
    store.save(path, state)

    snapshot = SimpleNamespace(
        state=ChatGPTState.RESPONDING,
        stop_visible=True,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", prompt, ()),
            MessageSnapshot("assistant", "a1", "t1", "pre-refresh result", ()),
        ),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": sent_at.isoformat(),
            "activity_observed_at": sent_at.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.wait_kwargs = None
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.wait_calls += 1
            self.wait_kwargs = kwargs
            raise TimeoutError("continue polling")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-PLAN",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            persisted_hop = _active_hop(persisted)
            assert persisted_hop["request_id"] == hop["request_id"]
            assert persisted_hop["wait"]["recovery_baseline"] == {
                "assistant_message_ids": ["a1"],
                "assistant_turn_ids": ["t1"],
                "assistant_fingerprints": persisted_hop["wait"]["recovery_baseline"][
                    "assistant_fingerprints"
                ],
            }
            assert len(
                persisted_hop["wait"]["recovery_baseline"]["assistant_fingerprints"]
            ) == 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 1
    assert client.wait_calls == 2
    assert hop["state"] == "waiting"
    assert hop["request_id"] == "task-refresh-baseline-hop1"
    assert client.wait_kwargs["active_reload_after_ms"] is None
    assert client.wait_kwargs["stale_response_baseline"] == hop["wait"][
        "recovery_baseline"
    ]
    assert hop["wait"]["refresh_count"] == 1
    assert hop["wait"]["recovery_baseline"]["assistant_message_ids"] == ["a1"]


def test_timeout_boundary_reconciles_valid_final_route_before_blocking(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-final-timeout-reconcile"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-final-timeout-reconcile.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("final timeout evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-final-timeout",
        "t-final-timeout",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    expired = datetime.now(timezone.utc) - timedelta(
        seconds=worker.config.response_timeout_seconds + 5
    )
    hop["timestamps"]["sent_at"] = expired.isoformat()
    hop["wait"]["started_at"] = expired.isoformat()
    hop["wait"]["deadline_at"] = (
        expired + timedelta(seconds=worker.config.response_timeout_seconds)
    ).isoformat()
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["wait"].update(
        {
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": expired.isoformat(),
            "activity_observed_at": expired.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/final-timeout",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert actions.refresh_calls == 0
    assert client.calls == 1
    assert client.kwargs["minimum_samples"] == 2
    assert client.kwargs["timeout_ms"] >= worker.config.response_stable_ms
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert state["status"] == "RUNNING"
    assert state.get("block_code") is None
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT



def test_refresh_due_reconciles_previously_observed_valid_final_before_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-refresh-reconcile-valid"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-refresh-reconcile-valid.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("valid pre-refresh evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-refresh-valid",
        "t-refresh-valid",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), response),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/refresh-valid",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 1
    assert actions.refresh_calls == 0
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert hop["wait"]["refresh_count"] == 0
    assert state.get("block_code") is None



def test_candidate_appearing_during_pre_refresh_read_prevents_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-late-pre-refresh-candidate"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-late-pre-refresh-candidate.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("late pre-refresh evidence", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-late",
        "t-late",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    initial_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    fresh_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            response,
        ),
    )
    signature, length = response_activity_signature(initial_snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.current = initial_snapshot
            self.wait_calls = 0
            self.normal_baseline = "unset"

        async def assert_ownership(self):
            return self.current

        async def wait_for_response(self, _receipt, **kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.current = fresh_snapshot
                raise TimeoutError("candidate appeared too late to stabilize")
            self.normal_baseline = kwargs["stale_response_baseline"]
            kwargs["candidate_validator"](response)
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/late-pre-refresh",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 2
    assert actions.refresh_calls == 0
    assert client.normal_baseline is None
    assert hop["state"] == "responded"
    assert hop["response"] == response.text
    assert hop["wait"]["refresh_count"] == 0
    assert state.get("block_code") is None


def test_pre_refresh_f5_baseline_uses_fresh_post_read_snapshot(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-fresh-pre-refresh-baseline"
    )
    old = sent_at - timedelta(minutes=21)
    current = MessageSnapshot("assistant", "a-current", "t-current", "same latest", ())
    hidden = MessageSnapshot("assistant", "a-hidden", "t-hidden", "earlier output", ())
    initial_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            current,
        ),
    )
    fresh_snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),
            hidden,
            current,
        ),
    )
    initial_signature, length = response_activity_signature(initial_snapshot, receipt.baseline)
    fresh_signature, _ = response_activity_signature(fresh_snapshot, receipt.baseline)
    assert fresh_signature == initial_signature
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": initial_signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.current = initial_snapshot
            self.wait_calls = 0

        async def assert_ownership(self):
            return self.current

        async def wait_for_response(self, _receipt, **_kwargs):
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.current = fresh_snapshot
            raise TimeoutError("no stable candidate")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/fresh-baseline",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0
            self.persisted_baseline = None

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1
            persisted = store.load(path)
            self.persisted_baseline = _active_hop(persisted)["wait"]["recovery_baseline"]

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 2
    assert actions.refresh_calls == 1
    assert actions.persisted_baseline["assistant_message_ids"] == ["a-current", "a-hidden"]
    assert actions.persisted_baseline["assistant_turn_ids"] == ["t-current", "t-hidden"]
    assert len(actions.persisted_baseline["assistant_fingerprints"]) == 2
    assert hop["state"] == "waiting"
    assert hop["wait"]["recovery_baseline"] == actions.persisted_baseline


def test_deadline_crossing_during_pre_refresh_read_blocks_without_f5(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-deadline-crosses-during-read"
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
        }
    )
    store.save(path, state)
    remaining_values = iter((1_000, 0))
    monkeypatch.setattr(worker_module, "remaining_timeout_ms", lambda _wait: next(remaining_values))

    class Client:
        def __init__(self):
            self.wait_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            self.wait_calls += 1
            raise TimeoutError("deadline crossed during bounded read")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/deadline-cross",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.wait_calls == 1
    assert actions.refresh_calls == 0
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"
    assert "final response reconciliation" in state["block_reason"]

def test_expired_wait_without_candidate_reconciles_once_then_blocks_without_f5(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-expired-no-candidate"
    )
    expired = datetime.now(timezone.utc) - timedelta(
        seconds=worker.config.response_timeout_seconds + 5
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = expired.isoformat()
    hop["wait"].update(
        {
            "started_at": expired.isoformat(),
            "deadline_at": (
                expired + timedelta(seconds=worker.config.response_timeout_seconds)
            ).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": expired.isoformat(),
            "activity_observed_at": expired.isoformat(),
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            self.calls += 1
            raise TimeoutError("no final response")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/expired-no-candidate",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 1
    assert actions.refresh_calls == 0
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "response_timeout"
    assert "final response reconciliation" in state["block_reason"]


def test_pre_refresh_reconciliation_preserves_prior_f5_stale_baseline(tmp_path: Path):
    store, state, worker, path, hop, receipt, sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-prior-f5-stale"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-prior-f5-stale.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("stale response evidence", encoding="utf-8")
    stale = MessageSnapshot(
        "assistant",
        "a-prior-f5",
        "t-prior-f5",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    old = sent_at - timedelta(minutes=21)
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()), stale),
    )
    stale_baseline = capture_response_recovery_baseline(snapshot.messages, receipt.baseline)
    signature, length = response_activity_signature(snapshot, receipt.baseline)
    hop["timestamps"]["sent_at"] = old.isoformat()
    hop["wait"].update(
        {
            "started_at": old.isoformat(),
            "deadline_at": (old + timedelta(hours=2)).isoformat(),
            "continuous_responding_since": None,
            "activity_signature": signature,
            "activity_length": length,
            "activity_changed_at": old.isoformat(),
            "activity_observed_at": old.isoformat(),
            "recovery_baseline": stale_baseline,
        }
    )
    store.save(path, state)

    class Client:
        def __init__(self):
            self.calls = 0
            self.baselines = []

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.calls += 1
            self.baselines.append(kwargs["stale_response_baseline"])
            assert stale.message_id in kwargs["stale_response_baseline"][
                "assistant_message_ids"
            ]
            raise TimeoutError("candidate remains stale")

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/prior-f5-stale",
        created=False,
        new_chat=False,
    )

    class Actions:
        def __init__(self):
            self.refresh_calls = 0

        async def locate_owned(self, _state, _role):
            return acquired

        async def refresh(self, _acquired):
            self.refresh_calls += 1

    actions = Actions()
    asyncio.run(worker._waiting(state, hop, actions, path))

    assert client.calls == 2
    assert actions.refresh_calls == 1
    assert hop["state"] == "waiting"
    assert hop.get("response") is None
    assert hop["wait"]["recovery_baseline"] == stale_baseline
    assert all(baseline == stale_baseline for baseline in client.baselines)

def test_cdp_disconnect_does_not_convert_inflight_task_to_blocked(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-disconnect-preserve"
    )
    before = path.read_bytes()
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})

    async def disconnected_wait(*_args, **_kwargs):
        raise TargetClosedError("Target page, context or browser has been closed")

    monkeypatch.setattr(worker, "_waiting", disconnected_wait)

    with pytest.raises(TargetClosedError):
        asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert path.read_bytes() == before
    current = store.load(path)
    current_hop = _active_hop(current)
    assert current["status"] == "RUNNING"
    assert current_hop["state"] == "waiting"
    assert current_hop["request_id"] == hop["request_id"]
    assert current.get("block_code") is None


def test_worker_reconnect_supervisor_preserves_inflight_identity(tmp_path: Path, monkeypatch):
    from contextlib import asynccontextmanager

    config, store, state, _worker = setup_task(
        tmp_path, task_id="task-supervisor-reconnect"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    hop["state"] = "waiting"
    hop["receipt"] = {"request_id": hop["request_id"]}
    hop["timestamps"]["sent_at"] = datetime.now(timezone.utc).isoformat()
    _worker._start_wait_budget_from_sent(hop)
    store.save(path, state)
    identity = (
        state["task_id"],
        state["active_hop_id"],
        hop["request_id"],
        hop["turn"],
        len(state["hops"]),
        len(state["reports"]),
        len(state["controls"]),
    )
    TargetClosedError = type("TargetClosedError", (RuntimeError,), {})
    connections: list[int] = []
    observed: list[tuple[object, ...]] = []

    @asynccontextmanager
    async def fake_connected_browser(_url):
        number = len(connections) + 1
        connections.append(number)
        yield SimpleNamespace(contexts=[SimpleNamespace(connection=number)])

    class FakeWorker:
        def __init__(self, _config):
            self.calls = 0

        async def run_forever(self, _context):
            self.calls += 1
            current = TaskStore(config).load(path)
            current_hop = _active_hop(current)
            observed.append(
                (
                    current["task_id"],
                    current["active_hop_id"],
                    current_hop["request_id"],
                    current_hop["turn"],
                    len(current["hops"]),
                    len(current["reports"]),
                    len(current["controls"]),
                )
            )
            if self.calls == 1:
                raise TargetClosedError("browser has been closed")
            raise asyncio.CancelledError

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(worker_module, "connected_browser", fake_connected_browser)
    monkeypatch.setattr(worker_module, "CDPAWorker", FakeWorker)
    monkeypatch.setattr(worker_module.asyncio, "sleep", no_delay)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_module._run(config))

    assert connections == [1, 2]
    assert observed == [identity, identity]
    current = store.load(path)
    assert _active_hop(current)["request_id"] == hop["request_id"]
    assert len(current["hops"]) == 1
    assert current["reports"] == []
    assert current["controls"] == []


def test_clear_team_reverifies_and_closes_tab_after_false_cleared_state(tmp_path: Path):
    _, store, state, worker = setup_task(tmp_path, task_id="task-cleared-tab-recheck")
    path = Path(state["manifest_path"])
    state.update(
        {
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
        }
    )
    state["cleanup"].update(
        {
            "state": "CLEARED",
            "phase": "cleared",
            "cleared_at": datetime.now(timezone.utc).isoformat(),
            "verified_empty_at": datetime.now(timezone.utc).isoformat(),
            "closed_tabs": 0,
        }
    )
    state["controls"].append(
        {
            "control_id": 1,
            "action": "clear_team",
            "role": "PLAN",
            "reason": None,
            "confirmed": True,
            "status": "requested",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "applied_at": None,
            "result": None,
        }
    )
    store.save(path, state)
    actions = FakeActions()
    actions.pages = ["surviving-tab"]

    assert asyncio.run(worker._apply_control(state, actions, path)) is True
    result = state

    assert actions.preflight_calls == 2
    assert actions.closed_teams == 1
    assert actions.pages == []
    assert result["cleanup"]["state"] == "CLEARED"
    assert result["cleanup"]["verified_empty_at"]
    assert result["controls"][-1]["status"] == "applied"
    assert result["controls"][-1]["result"]["reverified"] is True


def test_run_once_passes_locked_task_path_to_global_maintainers(
    tmp_path: Path, monkeypatch
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainer-locked-path"
    )
    path = Path(state["manifest_path"])
    calls = []

    async def locked_advance(_manifest_path, _browser_context):
        return None

    class FakeCoordinator:
        async def advance(self, tasks, browser_context):
            calls.append((tasks, browser_context))
            return False

    monkeypatch.setattr(worker, "advance", locked_advance)
    worker.maintainers = FakeCoordinator()
    browser_context = SimpleNamespace(pages=[])

    results = asyncio.run(worker.run_once(browser_context))

    assert results == [None]
    assert len(calls) == 1
    tasks, observed_context = calls[0]
    assert observed_context is browser_context
    assert tasks == [(path, {})]


def test_run_once_keeps_locked_active_a_serialized_before_b(
    tmp_path: Path, monkeypatch
):
    import hashlib

    import playwright_auto.cdpa_maintenance as maintenance_module
    from playwright_auto.cdpa_actions import AcquiredRole

    config, store, state_a, worker = setup_task(
        tmp_path, task_id="task-maintainer-active-a"
    )
    state_b = store.create_task(
        "Blocked task B",
        requested_team="beta",
        task_id="task-maintainer-waiting-b",
    )
    path_a = Path(state_a["manifest_path"])
    path_b = Path(state_b["manifest_path"])

    state_a.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_reason="failure A",
    )
    state_a = store.save(path_a, state_a)
    incident_a = ensure_maintenance_incident(state_a)
    assert incident_a is not None
    report = tmp_path / ".plan" / "maintainers" / "alpha_turn1_20260723T010203Z.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Active A report\n", encoding="utf-8")
    report_sha256 = hashlib.sha256(report.read_bytes()).hexdigest()
    incident_a.update(
        state="RUNNING",
        turn=1,
        request_id="request-a",
        decision={
            "action": "OPEN_ROLE_TAB",
            "reason": "recover A",
            "role": "PLAN",
            "lesson": None,
            "replacement": None,
        },
        report_at="2026-07-23T01:02:03+00:00",
        report_path=str(report),
        report_sha256=report_sha256,
        report_size=report.stat().st_size,
        applied_snapshot_key=incident_a["key"],
    )
    state_a = store.save_maintenance(path_a, state_a)
    state_a = store.request_control(
        path_a,
        "open_tab",
        role="PLAN",
        reason="recover A",
        maintenance_incident_id=incident_a["incident_id"],
        maintenance_request_id=incident_a["request_id"],
    )
    incident_a = next(
        item
        for item in state_a["maintenance"]["incidents"]
        if item["incident_id"] == incident_a["incident_id"]
    )
    incident_a["control_id"] = state_a["controls"][-1]["control_id"]
    state_a = store.save_maintenance(path_a, state_a)

    state_b.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="send_failed",
        block_reason="failure B",
    )
    state_b = store.save(path_b, state_b)

    global_state = worker.maintainers.state_store.load()
    global_state["active_incident"] = worker.maintainers._active_projection(
        state_a, incident_a
    )
    history_entry = worker.maintainers._history_entry(state_a, incident_a)
    assert history_entry is not None
    global_state["history"] = [history_entry]
    worker.maintainers.state_store.save(global_state)
    sends = []

    class FakeActions:
        def __init__(self, _context, _config):
            pass

        async def acquire_global_role(self, role):
            return AcquiredRole(
                client=SimpleNamespace(binding=SimpleNamespace(role=role)),
                page_id="maint-page",
                url="https://chatgpt.com/c/maint",
                created=False,
                new_chat=False,
            )

    class FakeSendBlock:
        def __init__(self, prompt, **_kwargs):
            sends.append(prompt)

        async def run(self, _context):
            return {
                "response": {
                    "text": (
                        "# Maintenance report\n\nWait.\n\n"
                        "```json\n"
                        '{"action":"WAIT","reason":"Wait for changed evidence.",'
                        '"role":null,"lesson":null,"replacement":null}\n'
                        "```"
                    )
                }
            }

    monkeypatch.setattr(maintenance_module, "CDPATabActions", FakeActions)
    monkeypatch.setattr(maintenance_module, "DurableSendBlock", FakeSendBlock)

    with store.task_run_lock(path_a, blocking=True):
        first = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))
        second = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert first[0] is None
    assert second[0] is None
    assert sends == []
    current_a = store.load(path_a)
    current_b = store.load(path_b)
    assert current_a["maintenance"]["active_incident_id"] == incident_a["incident_id"]
    assert current_a["maintenance"]["incidents"][0]["state"] == "RUNNING"
    assert current_b.get("maintenance") is None
    assert worker.maintainers.state_store.load()["active_incident"] == global_state["active_incident"]


def test_run_once_clears_genuinely_missing_global_task_without_browser(
    tmp_path: Path, monkeypatch
):
    import playwright_auto.cdpa_maintenance as maintenance_module

    _, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainer-existing-b"
    )
    path = Path(state["manifest_path"])
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="send_failed",
        block_reason="failure B",
    )
    store.save(path, state)
    global_state = worker.maintainers.state_store.load()
    global_state["active_incident"] = {
        "task_id": "task-deleted-a",
        "incident_id": "maint-deleted-a",
        "turn": 1,
        "request_id": "request-deleted-a",
    }
    worker.maintainers.state_store.save(global_state)

    class NoBrowserWork:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("missing-task reconciliation must not touch browser")

    monkeypatch.setattr(maintenance_module, "CDPATabActions", NoBrowserWork)

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert len(results) == 1
    assert worker.maintainers.state_store.load()["active_incident"] is None
    assert store.load(path).get("maintenance") is None


def test_run_once_advances_global_maintainers_after_task_iteration(
    tmp_path: Path, monkeypatch
):
    config, store, state, worker = setup_task(
        tmp_path, task_id="task-maintainer-run-once"
    )
    path = Path(state["manifest_path"])
    calls = []

    async def fake_advance(manifest_path, _browser_context):
        current = store.load(manifest_path)
        current["status"] = "BLOCKED"
        current["kanban_column"] = "BLOCKED"
        current["block_code"] = "role_offline"
        current["block_reason"] = "offline"
        return store.save(manifest_path, current)

    class FakeCoordinator:
        async def advance(self, tasks, browser_context):
            calls.append((tasks, browser_context))
            return True

    monkeypatch.setattr(worker, "advance", fake_advance)
    worker.maintainers = FakeCoordinator()
    browser_context = SimpleNamespace(pages=[])

    results = asyncio.run(worker.run_once(browser_context))

    assert len(results) == 1
    assert results[0]["status"] == "BLOCKED"
    assert len(calls) == 1
    tasks, observed_context = calls[0]
    assert observed_context is browser_context
    assert tasks[0][0] == path
    assert tasks[0][1]["block_code"] == "role_offline"



def _inline_response(route="DONE", body="# Inline report\n\nEvidence."):
    return f'{body}\n\n```json\n{{"route":"{route}","handoff":"INLINE"}}\n```'


def test_inline_pre_send_and_repair_guidance_never_expose_expected_path(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-prompt", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    assert "Do not create, edit, or write any role-report file" in hop["prompt"]
    assert "the worker owns report materialization" in hop["prompt"]
    assert '"handoff":"INLINE"' in hop["prompt"]
    assert hop["expected_report_path"] not in hop["prompt"]
    assert "Report naming rule:" not in hop["prompt"]

    hop["response"] = "bad"
    hop["state"] = "responded"
    worker._responded(state, hop)
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))

    assert "corrected inline Markdown" in repair["prompt"]
    assert '"handoff":"INLINE"' in repair["prompt"]
    assert repair["expected_report_path"] not in repair["prompt"]
    assert "Report naming rule:" not in repair["prompt"]


def test_inline_response_materializes_exact_report_and_routes(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-materialize", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"

    worker._responded(state, hop)

    expected = (tmp_path / hop["expected_report_path"]).resolve()
    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert hop["report_path"] == str(expected)
    assert hop["report_size"] == len(expected.read_bytes())
    assert hop["report_sha256"] == worker_module.hashlib.sha256(expected.read_bytes()).hexdigest()
    assert len(state["reports"]) == 1
    assert state["reports"][0]["path"] == str(expected)
    next_hop = _active_hop(state)
    assert next_hop["target_role"] == "DEV"
    assert next_hop["handoff"] == hop["expected_report_path"]


def test_inline_materialization_failure_blocks_operationally_without_path_or_repair(
    tmp_path: Path, monkeypatch
):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-write-fail", report_mode="inline"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    hop["response"] = _inline_response(route="DEV")
    hop["state"] = "responded"
    internal = str((tmp_path / hop["expected_report_path"]).resolve()) + ".lock"
    monkeypatch.setattr(
        worker_module,
        "materialize_inline_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            worker_module.InlineReportMaterializationError(
                f"Permission denied: {internal}"
            )
        ),
    )

    worker._responded(state, hop)

    assert hop["report_path"] is None
    assert state["reports"] == []
    assert len(state["hops"]) == 1
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert internal not in state["block_reason"]
    incident = ensure_maintenance_incident(state)
    assert incident is not None
    assert incident["trigger_code"] == "inline_report_materialization_failed"



@pytest.mark.parametrize("failure_point", ["parent_mkdir", "lock_is_symlink"])
def test_inline_raw_filesystem_failure_blocks_without_route_repair(
    tmp_path: Path,
    monkeypatch,
    failure_point: str,
):
    _, _, state, worker = setup_task(
        tmp_path,
        task_id=f"task-inline-{failure_point}",
        report_mode="inline",
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    response = _inline_response(route="DEV")
    hop["response"] = response
    hop["state"] = "responded"
    target = (tmp_path / hop["expected_report_path"]).resolve()

    if failure_point == "parent_mkdir":
        original = Path.mkdir

        def fail_parent_mkdir(self, *args, **kwargs):
            if self == target.parent:
                raise PermissionError(13, "Permission denied", str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_parent_mkdir)
    else:
        lock = target.with_suffix(target.suffix + ".lock")
        original = Path.is_symlink

        def fail_lock_is_symlink(self):
            if self == lock:
                raise PermissionError(13, "Permission denied", str(self))
            return original(self)

        monkeypatch.setattr(Path, "is_symlink", fail_lock_is_symlink)

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert len(state["hops"]) == 1
    assert hop["state"] == "responded"
    assert hop["response"] == response
    assert hop["report_path"] is None
    assert state["reports"] == []
    assert str(target) not in state["block_reason"]
    assert all(str(target) not in str(item) for item in hop.get("errors") or ())

    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert first is not None
    assert first["trigger_code"] == "inline_report_materialization_failed"
    assert len(state["maintenance"]["incidents"]) == 1


@pytest.mark.parametrize("local_failure", ["different_bytes", "target_symlink"])
def test_inline_local_report_state_failure_preserves_sent_request_and_blocks(
    tmp_path: Path,
    local_failure: str,
):
    _store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id=f"task-inline-{local_failure}",
        report_mode="inline",
    )
    response = _inline_response(route="DEV")
    hop["response"] = response
    hop["state"] = "responded"
    target = (tmp_path / hop["expected_report_path"]).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if local_failure == "different_bytes":
        target.write_text("different existing report", encoding="utf-8")
    else:
        outside = tmp_path / "outside-inline-report.md"
        outside.write_text("outside", encoding="utf-8")
        target.symlink_to(outside)

    ledger = RequestLedger(hop["ledger_path"])
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "inline_report_materialization_failed"
    assert state["block_reason"] == "inline report materialization failed"
    assert len(state["hops"]) == 1
    assert hop["state"] == "responded"
    assert hop["response"] == response
    assert hop["report_path"] is None
    assert state["reports"] == []
    assert ledger.get(hop["request_id"]).status is RequestStatus.SENT
    assert str(target) not in state["block_reason"]
    assert all(str(target) not in str(item) for item in hop.get("errors") or ())

    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert first is not None
    assert first["trigger_code"] == "inline_report_materialization_failed"
    assert len(state["maintenance"]["incidents"]) == 1

def test_inline_materialization_is_restart_idempotent_and_ledger_completes_once(
    tmp_path: Path
):
    _, store, state, worker = setup_task(
        tmp_path, task_id="task-inline-restart", report_mode="inline"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=hop["prompt"],
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    receipt = SendReceipt(
        prompt=hop["prompt"],
        prompt_sha256=prompt_digest(hop["prompt"]),
        binding=PageBinding("page-alpha-plan", "alpha-plan"),
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="inline-restart",
        user_message_id="u-inline",
        user_turn_id="t-inline",
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=receipt.binding,
        baseline=baseline,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=receipt.to_dict(),
    )
    hop["receipt"] = receipt.to_dict()
    hop["response"] = _inline_response()
    hop["response_record"] = {
        "role": "assistant",
        "message_id": "a-inline",
        "turn_id": "ta-inline",
        "text": hop["response"],
        "actions": [],
        "image_count": 0,
    }
    hop["state"] = "responded"
    store.save(path, state)

    expected = (tmp_path / hop["expected_report_path"]).resolve()
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text("# Inline report\n\nEvidence.", encoding="utf-8")
    before_stat = expected.stat()

    restarted = CDPAWorker(worker.config, store=store)
    result = asyncio.run(restarted.advance(path, SimpleNamespace(pages=[])))

    assert result["status"] == "DONE"
    assert len(result["reports"]) == 1
    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert expected.stat().st_mtime_ns == before_stat.st_mtime_ns
    persisted = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert persisted is not None
    assert persisted.status is RequestStatus.COMPLETED

    manifest_before = path.read_bytes()
    result_again = asyncio.run(restarted.advance(path, SimpleNamespace(pages=[])))
    assert result_again["status"] == "DONE"
    assert path.read_bytes() == manifest_before
    assert len(result_again["reports"]) == 1
    assert expected.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_exhausted_inline_validation_block_creates_one_maintenance_incident(tmp_path: Path):
    _, _, state, worker = setup_task(
        tmp_path, task_id="task-inline-exhausted", report_mode="inline"
    )
    hop = _active_hop(state)
    hop["repair_attempt"] = worker.config.route_repair_attempts
    worker._repair_route(state, hop, RouteContractError("inline Markdown report is empty"))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "route_validation_exhausted"
    first = ensure_maintenance_incident(state)
    second = ensure_maintenance_incident(state)
    assert first is second
    assert first["trigger_code"] == "route_validation_exhausted"
    assert len(state["maintenance"]["incidents"]) == 1



def test_waiting_inline_candidate_uses_same_stability_gate_before_materialization(
    tmp_path: Path,
):
    _store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path,
        task_id="task-inline-stable-gate",
        report_mode="inline",
    )
    response = MessageSnapshot(
        "assistant",
        "a-inline-stable",
        "ta-inline-stable",
        _inline_response(route="TEST"),
        (),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        composer_empty=True,
        manual_input_pending=False,
        error_texts=(),
        blocking_dialogs=(),
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    expected = (tmp_path / hop["expected_report_path"]).resolve()

    class Client:
        def __init__(self):
            self.kwargs = None

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            self.kwargs = kwargs
            kwargs["candidate_validator"](response)
            assert not expected.exists()
            return response

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id="page-alpha-plan",
        url="https://chatgpt.com/c/exact",
        created=False,
        new_chat=False,
    )

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    asyncio.run(worker._waiting(state, hop, Actions(), path))

    assert client.kwargs["minimum_samples"] == 2
    assert client.kwargs["invalid_grace_ms"] >= 1_000
    assert hop["state"] == "responded"
    assert not expected.exists()
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.SENT

    worker._responded(state, hop)

    assert expected.read_text(encoding="utf-8") == "# Inline report\n\nEvidence."
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED
    assert _active_hop(state)["target_role"] == "TEST"



@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, "other"])
def test_worker_rejects_explicit_invalid_report_mode(value: object):
    with pytest.raises(ValueError, match="report_mode"):
        _report_mode({"options": {"report_mode": value}})



def test_worker_defaults_missing_legacy_report_mode_to_file():
    assert _report_mode({"options": {}}) == "file"


class ExplodingBrowserContext:
    @property
    def pages(self):
        raise AssertionError("dependency WAITING/release must not inspect browser pages")


def test_dependency_waiting_does_not_construct_browser_actions_or_rewrite(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    path = Path(child["manifest_path"])
    before = path.read_bytes()
    before_events = list(child["dependency_events"])

    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))

    assert result["status"] == "WAITING"
    assert result["waiting"]["waiting_on"] == ["task-parent"]
    assert result["hops"][0]["state"] == "pre_send"
    assert path.read_bytes() == before
    assert result["dependency_events"] == before_events


def test_dependency_release_is_one_durable_transition_without_browser_action(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "DONE",
            "terminal_state": "DONE",
            "active_role": None,
            "active_hop_id": None,
            "completed_at": utc_now(),
        },
    )

    path = Path(child["manifest_path"])
    result = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert result["status"] == "INBOX"
    assert result["kanban_column"] == "INBOX"
    assert result["active_action"] == "queued"
    assert result["waiting"] == {
        "reason": None,
        "waiting_on": [],
        "stopped": [],
        "missing": [],
        "since": None,
    }
    assert result["hops"][0]["state"] == "pre_send"
    assert result["active_hop_id"] == 1
    assert result["dependency_events"][-1]["status"] == "RELEASED"

    persisted = path.read_bytes()
    # The next poll would be allowed to acquire PLAN, so only verify the release
    # transition itself is stable through a worker restart/load.
    restarted = CDPAWorker(config, store=store)
    loaded = store.load(path)
    assert loaded["status"] == "INBOX"
    assert path.read_bytes() == persisted
    assert restarted.store is store


def test_stopped_and_missing_dependencies_remain_waiting_across_restart(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    parent = store.create_task("Parent", requested_team="parent", task_id="task-parent")
    child = store.create_task(
        "Child",
        requested_team="child",
        task_id="task-child",
        depends_on_task_ids=("task-parent",),
    )
    store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "failed parent",
        },
    )
    path = Path(child["manifest_path"])

    stopped = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert stopped["status"] == "WAITING"
    assert stopped["waiting"]["stopped"] == ["task-parent"]
    assert stopped["waiting_code"] == "dependency_stopped"

    parent_path = Path(parent["manifest_path"])
    parent_path.unlink()
    missing = asyncio.run(CDPAWorker(config, store=store).advance(path, ExplodingBrowserContext()))
    assert missing["status"] == "WAITING"
    assert missing["waiting"]["missing"] == ["task-parent"]
    assert missing["waiting_code"] == "dependency_missing"
    assert missing["hops"][0]["state"] == "pre_send"
    assert store.load(path)["waiting"]["missing"] == ["task-parent"]


def test_run_once_never_reclassifies_missing_edge_cycle_as_dependency_missing(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    a = store.create_task(
        "A",
        requested_team="a",
        task_id="task-a",
        depends_on_task_ids=("missing-parent",),
    )
    store.create_task(
        "B",
        requested_team="b",
        task_id="task-b",
        depends_on_task_ids=("task-a",),
    )
    Path(missing_parent["manifest_path"]).unlink()
    a_path = Path(a["manifest_path"])
    raw_a = json.loads(a_path.read_text(encoding="utf-8"))
    raw_a["depends_on_task_ids"] = ["missing-parent", "task-b"]
    a_path.write_text(json.dumps(raw_a), encoding="utf-8")
    worker = CDPAWorker(config, store=store)

    async def forbidden_advance(*_args, **_kwargs):
        raise AssertionError("cyclic manifests must never enter worker scheduling")

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert tasks == []
            return False

    monkeypatch.setattr(worker, "advance", forbidden_advance)
    worker.maintainers = FakeCoordinator()

    assert asyncio.run(worker.run_once(SimpleNamespace(pages=[]))) == []
    tasks, errors = store.discover_with_errors()
    assert tasks == []
    assert sum("cycle" in item["error"] for item in errors) == 2


def test_run_once_isolates_diagnostic_cycle_from_unrelated_tasks(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    install_cycle_isolation_graph(store)
    worker = CDPAWorker(config, store=store)
    advanced: list[str] = []

    async def no_browser_advance(path, _browser_context):
        state = store.load(path)
        advanced.append(state["task_id"])
        return state

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert {state["task_id"] for _path, state in tasks} == {
                "missing-only",
                "unrelated-task",
            }
            return False

    monkeypatch.setattr(worker, "advance", no_browser_advance)
    worker.maintainers = FakeCoordinator()

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert {state["task_id"] for state in results if state} == {
        "missing-only",
        "unrelated-task",
    }
    assert set(advanced) == {"missing-only", "unrelated-task"}
    _tasks, errors = store.discover_with_errors()
    assert sum("cycle" in item["error"] for item in errors) == 2


def test_run_once_skips_duplicate_identity_manifests_and_advances_unrelated_task(
    tmp_path: Path,
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    states = install_duplicate_task_graph(store)
    store.update(
        states["unique"]["manifest_path"],
        lambda state: {
            **state,
            "status": "PAUSED",
            "kanban_column": "PAUSED",
            "active_action": "paused",
            "pause_reason": "test boundary",
        },
    )
    worker = CDPAWorker(config, store=store)

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert [path for path, _state in tasks] == [
                Path(states["unique"]["manifest_path"])
            ]
            return False

    worker.maintainers = FakeCoordinator()

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    assert [result["task_id"] for result in results] == ["unique-task"]
    assert results[0]["status"] == "PAUSED"


def test_run_once_recovers_phase4_journal_with_unrelated_missing_dependency(
    tmp_path: Path, monkeypatch
):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    missing_parent = store.create_task(
        "Missing parent", requested_team="missing-parent", task_id="missing-parent"
    )
    missing_child = store.create_task(
        "Missing child",
        requested_team="missing-child",
        task_id="missing-child",
        depends_on_task_ids=("missing-parent",),
    )
    target = store.create_task(
        "Recovery target", requested_team="recovery-target", task_id="recovery-target"
    )
    target_child = store.create_task(
        "Recovery child",
        requested_team="recovery-child",
        task_id="recovery-child",
        depends_on_task_ids=("recovery-target",),
    )
    store.update(
        target["manifest_path"],
        lambda state: {
            **state,
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "replacement probe",
        },
    )
    original_replace = store_module.os.replace
    installs = 0

    def interrupt_before_second_manifest(source, target_path):
        nonlocal installs
        if Path(source).name.endswith(".json.phase4.tmp"):
            installs += 1
            if installs == 2:
                raise SystemExit("simulated worker recovery interruption")
        return original_replace(source, target_path)

    monkeypatch.setattr(store_module.os, "replace", interrupt_before_second_manifest)
    with pytest.raises(SystemExit, match="worker recovery interruption"):
        store.replace_task_and_rewire(
            "recovery-target",
            "Continue recovery target safely",
            reuse_team=True,
            rewire_children=True,
            incident_id="maint-worker-missing-recovery",
        )
    monkeypatch.setattr(store_module.os, "replace", original_replace)
    Path(missing_parent["manifest_path"]).unlink()

    restarted_store = TaskStore(config)
    worker = CDPAWorker(config, store=restarted_store)
    advanced: list[str] = []

    async def no_browser_advance(path, _browser_context):
        state = restarted_store.load(path)
        advanced.append(state["task_id"])
        return state

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert "missing-child" in [state["task_id"] for _path, state in tasks]
            return False

    monkeypatch.setattr(worker, "advance", no_browser_advance)
    worker.maintainers = FakeCoordinator()

    results = asyncio.run(worker.run_once(SimpleNamespace(pages=[])))

    replacement = next(
        state
        for state in results
        if state and state.get("replacement_incident_id") == "maint-worker-missing-recovery"
    )
    assert "missing-child" in advanced
    assert restarted_store.load(missing_child["manifest_path"])["depends_on_task_ids"] == [
        "missing-parent"
    ]
    assert restarted_store.load(target_child["manifest_path"])["depends_on_task_ids"] == [
        replacement["task_id"]
    ]
    assert not restarted_store.phase4_journal_path.exists()
    assert list(restarted_store.root.rglob("*.phase4.tmp")) == []
    assert list(restarted_store.root.rglob("*.phase4.rollback.tmp")) == []


def test_run_once_recovers_phase4_journal_before_task_discovery(tmp_path: Path, monkeypatch):
    _, store, _state, worker = setup_task(
        tmp_path, task_id="task-phase4-recovery-order"
    )
    calls: list[str] = []

    def recover():
        calls.append("recover")
        return None

    def discover_paths():
        assert calls == ["recover"]
        calls.append("discover")
        return []

    class FakeCoordinator:
        async def advance(self, tasks, _browser_context):
            assert calls == ["recover", "discover"]
            assert tasks == []
            calls.append("maintainers")
            return False

    monkeypatch.setattr(store, "recover_phase4_replacement", recover)
    monkeypatch.setattr(store, "discover_paths", discover_paths)
    worker.maintainers = FakeCoordinator()

    assert asyncio.run(worker.run_once(SimpleNamespace(pages=[]))) == []
    assert calls == ["recover", "discover", "maintainers"]


def test_replacement_plan_first_prompt_contains_immutable_parent_context(tmp_path: Path):
    import hashlib

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    unique_goal = "ORIGINAL UNIQUE GOAL: migrate customer records and preserve checksum 7f4a"
    parent = store.create_task(
        unique_goal,
        requested_team="parent",
        task_id="task-parent-context",
    )
    report_path = (
        tmp_path
        / ".plan"
        / "parent"
        / "parent-plan_turn1_task-parent-context.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# PLAN checkpoint\n\nValidated 73 of 100 records.\n"
    report_path.write_bytes(report_bytes)
    parent = store.update(
        parent["manifest_path"],
        lambda state: {
            **state,
            "reports": [
                {
                    "report_id": 1,
                    "physical_role": "parent-plan",
                    "turn": 1,
                    "path": str(report_path),
                    "sha256": hashlib.sha256(report_bytes).hexdigest(),
                    "size": len(report_bytes),
                }
            ],
            "status": "STOPPED",
            "terminal_state": "STOPPED",
            "active_role": None,
            "active_hop_id": None,
            "stopped_at": utc_now(),
            "stop_reason": "parent cannot safely continue",
        },
    )
    result = store.replace_task_and_rewire(
        parent["task_id"],
        "Continue the original requested outcome safely",
        reuse_team=True,
        rewire_children=True,
        incident_id="maint-context-preservation",
    )
    replacement_path = Path(result["replacement"]["manifest_path"])
    restarted_store = TaskStore(config)
    replacement = restarted_store.load(replacement_path)
    worker = CDPAWorker(config, store=restarted_store)
    hop = _active_hop(replacement)

    asyncio.run(worker._pre_send(replacement, hop, FakeActions()))

    prompt = str(hop["prompt"])
    assert "task-parent-context" in prompt
    assert unique_goal in prompt
    assert str(report_path) in prompt
    assert "Continue the original requested outcome safely" in prompt
    assert hop["handoff"] == replacement["task_text"]
