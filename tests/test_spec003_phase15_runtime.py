"""Spec003 Phase 1.5 (T040-T044): browser-control recovery and minimal script WAIT callback."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

import playwright_auto.chatgpt as chatgpt_module
from playwright_auto import cdpa_async_continuation as async_continuation
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_routes import expected_report_relative
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_team import is_active_team_owner
from playwright_auto.cdpa_worker import CDPAWorker, _workflow_report_roots
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    TaskBindingError,
    UnsafePageStateError,
    prompt_digest,
)
from playwright_auto.dashboard_api import DashboardAPI
from playwright_auto.hcuw_runtime.api_server import RuntimeApiServer
from playwright_auto.hcuw_runtime.legacy_audit import LegacyObservationAudit
from playwright_auto.role_runtime import Action, RoleController
from test_cdpa_core import write_config
from test_operational_contract import snapshot

NOW = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
CONVERSATION = "11111111-2222-4333-8444-555555555555"


def _receipt(*, user_message_id: str | None = "user-original", physical_role: str = "DEV") -> SendReceipt:
    return SendReceipt(
        prompt="original task",
        prompt_sha256=prompt_digest("original task"),
        binding=PageBinding("page-1", physical_role),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before=None,
        user_message_id=user_message_id,
        conversation_id=CONVERSATION,
    )


def _step(current, wait, seconds=0, *, receipt=None, **kwargs):
    return RoleController().decide(
        current,
        wait,
        receipt=receipt if receipt is not None else _receipt(),
        now=NOW + timedelta(seconds=seconds),
        **kwargs,
    )


def _assistant_turn(text: str, *, actions=()):
    return (
        MessageSnapshot("user", "user-original", "turn-user", "original task", ()),
        MessageSnapshot("assistant", "assistant-1", "turn-assistant", text, tuple(actions)),
    )


class _ObservedClient:
    """Fake page client exposing only the existing owner methods Phase 1.5 may call."""

    def __init__(self):
        self.allow_calls = []
        self.retry_calls = []
        self.allow_result = {"method": "react_handler", "target_message_id": "call-1", "remember_answer": "true"}
        self.allow_error = None
        self.retry_error = None

    async def install_page_observer(self):
        return None

    def page_observation(self):
        return {}

    async def auto_allow_mcp_permission(self, *, passive_action=None, expected_task_id=None, expected_team=None, expected_surface_key=None):
        self.allow_calls.append((expected_task_id, expected_team))
        if self.allow_error is not None:
            raise self.allow_error
        return self.allow_result

    def clear_permission_action(self):
        return None

    async def retry_generation(self, receipt, *, expected_task_id, expected_team):
        self.retry_calls.append((receipt.user_message_id, expected_task_id, expected_team))
        if self.retry_error is not None:
            raise self.retry_error
        return {"progress": True, "transport_active": True}

    async def send_prompt(self, *_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("Phase 1.5 recovery must never resend the accepted prompt")


def _fake_environment(client, *, runtime_db=None):
    state = {"task_id": "task-1", "team": "team-1", "status": "RUNNING", "active_action": "wait_response", "active_hop_id": 1}
    hop = {"hop_id": 1, "receipt": _receipt().to_dict(), "request_id": "task-1-hop1", "physical_role": "DEV", "target_role": "DEV", "wait": {}}
    recorded = []
    tempdir = None
    if runtime_db is None:
        tempdir = TemporaryDirectory()
        runtime_db = RuntimeDB(Path(tempdir.name) / "runtime.sqlite3")
        runtime_db.ensure_schema()
    worker = SimpleNamespace(
        runtime_db=runtime_db,
        _runtime_db_tempdir=tempdir,
        _validate_response_candidate=lambda *_: None,
        _terminal_route_evidence=lambda *_: (_ for _ in ()).throw(ValueError("no exact report evidence")),
        _queue_format_repair=lambda _state, _hop, _response, error: _state.update(repair=str(error)),
        _capture_passive_remote_report_if_available=lambda *_: None,
        _record_response=lambda _state, _hop, response: recorded.append(response),
        _release_stream_status_slot=lambda *_: None,
        _block=lambda _state, message, **kwargs: _state.update(block_code=kwargs.get("code")),
    )
    acquired = SimpleNamespace(client=client, page_id="page-1")
    return state, hop, worker, acquired, recorded


def _run(controller, worker, state, hop, acquired, current):
    try:
        return asyncio.run(controller.run(worker, state, hop, acquired, None, snapshot=current, wait_ms=0))
    finally:
        if getattr(worker, "_runtime_db_tempdir", None) is not None:
            worker.runtime_db.close()


# T040 ----------------------------------------------------------------------------


class _FakePage:
    url = f"https://chatgpt.com/c/{CONVERSATION}"


def _owned_page(monkeypatch, **snapshot_changes):
    client = ChatGPTPage(_FakePage(), timeout_ms=1_000)
    clicks = []

    async def owned(*_args, **_kwargs):
        return snapshot(page_task_id="task-1", page_team="team-1", **snapshot_changes)

    async def click(_page, *, passive_action=None, expected_surface_key=None):
        clicks.append(passive_action)
        return {"method": "dom_click", "target_message_id": "", "remember_answer": "false"}

    monkeypatch.setattr(client, "assert_ownership", owned)
    monkeypatch.setattr(chatgpt_module, "click_preferred_mcp_allow", click)
    return client, clicks


def test_t040_permission_owner_clicks_only_with_exact_ownership_and_clean_composer(monkeypatch):
    client, clicks = _owned_page(monkeypatch)
    result = asyncio.run(client.auto_allow_mcp_permission(expected_task_id="task-1", expected_team="team-1"))
    assert result["method"] == "dom_click"
    assert len(clicks) == 1

    with pytest.raises(TaskBindingError):
        asyncio.run(client.auto_allow_mcp_permission(expected_task_id="task-other", expected_team="team-1"))
    with pytest.raises(TaskBindingError):
        asyncio.run(client.auto_allow_mcp_permission(expected_task_id="task-1", expected_team="team-other"))
    assert len(clicks) == 1

    drafted, draft_clicks = _owned_page(monkeypatch, composer_text="operator draft")
    with pytest.raises(ComposerConflictError):
        asyncio.run(drafted.auto_allow_mcp_permission(expected_task_id="task-1", expected_team="team-1"))
    attached, attach_clicks = _owned_page(monkeypatch, attachment_markers=("notes.txt",))
    with pytest.raises(ComposerConflictError):
        asyncio.run(attached.auto_allow_mcp_permission(expected_task_id="task-1", expected_team="team-1"))
    assert draft_clicks == [] and attach_clicks == []


def test_t040_allow_continues_same_wait_and_is_never_a_route_terminal_or_provider_result():
    client = _ObservedClient()
    state, hop, worker, acquired, recorded = _fake_environment(client)
    receipt_before = copy.deepcopy(hop["receipt"])
    result = _run(RoleController(), worker, state, hop, acquired, snapshot(mcp_permission_allow_count=1, mcp_permission_surface_key="test-call"))

    assert result.action is Action.ALLOW
    assert client.allow_calls == [("task-1", "team-1")]
    assert state["active_action"] == "wait_mcp_allow_continuation"
    assert hop["wait"]["mcp_allow_click_count"] == 1
    assert hop["wait"]["mcp_allow_last"]["result"] == "clicked"
    assert state["control_events"][-1]["kind"] == "mcp_allow"
    assert state["control_events"][-1]["result"] == "clicked"
    # Permission is not a response, route, terminal result, or ProviderResult.
    assert recorded == []
    assert "response" not in hop and "route" not in hop and "repair" not in state
    assert hop["receipt"] == receipt_before
    assert client.retry_calls == []

    # Once the Allow node disappears the same accepted turn keeps waiting.
    follow = _run(RoleController(), worker, state, hop, acquired, snapshot())
    assert follow.action is Action.WAIT
    assert follow.reason == "wait_response"
    assert hop["receipt"] == receipt_before


def test_t040_allow_with_operator_draft_is_deferred_without_click_and_retried_later():
    client = _ObservedClient()
    client.allow_error = ComposerConflictError("MCP Allow deferred by manual draft or attachments")
    state, hop, worker, acquired, recorded = _fake_environment(client)
    current = snapshot(mcp_permission_allow_count=1, mcp_permission_surface_key="test-call", composer_text="operator draft")

    result = _run(RoleController(), worker, state, hop, acquired, current)
    assert result.reason == "mcp_allow_composer_conflict"
    assert state["active_action"] == "wait_mcp_allow_composer_conflict"
    assert hop["wait"]["mcp_allow_last"]["result"] == "composer_conflict"
    assert "mcp_allow_clicked_target" not in hop["wait"]
    events = len(state["control_events"])

    _run(RoleController(), worker, state, hop, acquired, current)
    assert len(state["control_events"]) == events  # unchanged deferral is not re-logged

    client.allow_error = None
    clicked = _run(RoleController(), worker, state, hop, acquired, snapshot(mcp_permission_allow_count=1, mcp_permission_surface_key="test-call"))
    assert clicked.action is Action.ALLOW
    # Manual composer evidence is handled before the irreversible Allow intent;
    # deferred checks must not touch the browser or consume a click.
    assert len(client.allow_calls) == 1
    assert recorded == []


# T041 ----------------------------------------------------------------------------


def test_t041_stop_visible_without_retry_keeps_waiting_and_never_repairs_early():
    wait = {}
    for seconds in (0, 5, 30, 300, 599):
        decision = _step(snapshot(stop_visible=True), wait, seconds)
        assert decision.action is Action.WAIT
    assert "retry_seen_at" not in wait


def test_t041_stop_visible_suppresses_retry_even_when_retry_is_rendered():
    wait = {}
    for seconds in (0, 5, 10, 60):
        decision = _step(snapshot(stop_visible=True, retry_visible=True), wait, seconds)
        assert decision.action is Action.WAIT
    assert "retry_seen_at" not in wait


def test_t041_stable_retry_is_invoked_exactly_once_per_accepted_turn():
    wait = {}
    assert _step(snapshot(retry_visible=True), wait).action is Action.WAIT
    assert _step(snapshot(retry_visible=True), wait, 4.9).action is Action.WAIT
    decision = _step(snapshot(retry_visible=True), wait, 5)
    assert decision.action is Action.RETRY
    wait["provider_retry"] = {"accepted_user_turn": "user-original", "result": "progress"}
    wait.pop("retry_seen_at", None)
    for seconds in (6, 12, 30):
        assert _step(snapshot(retry_visible=True), wait, seconds).action is Action.WAIT


def test_t041_retry_requires_exact_accepted_user_turn_identity():
    wait = {}
    receipt = _receipt(user_message_id=None)
    _step(snapshot(retry_visible=True), wait, receipt=receipt)
    assert _step(snapshot(retry_visible=True), wait, 10, receipt=receipt).action is Action.WAIT


def test_t041_retry_absence_alone_does_nothing_until_existing_timeout_policy_exhausts():
    wait = {}
    for seconds in (0, 60, 300, 599):
        assert _step(snapshot(), wait, seconds).action is Action.WAIT
    assert _step(snapshot(), wait, 600).action is Action.BLOCK
    assert _step(snapshot(), wait, 601).reason == "response_terminal_missing"
    assert "timeout_refreshed" not in wait


def test_t041_provider_retry_runs_once_with_exact_identity_and_never_resends():
    client = _ObservedClient()
    state, hop, worker, acquired, recorded = _fake_environment(client)
    receipt_before = copy.deepcopy(hop["receipt"])
    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()

    result = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert result.reason == "provider_retry_clicked"
    assert client.retry_calls == [("user-original", "task-1", "team-1")]
    assert hop["wait"]["provider_retry"]["result"] == "progress"
    assert hop["wait"]["provider_retry"]["original_prompt_resent"] is False
    assert state["control_events"][-1]["kind"] == "provider_retry"
    assert "block_code" not in state and "repair" not in state

    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    again = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert again.action is Action.WAIT
    assert len(client.retry_calls) == 1
    assert hop["receipt"] == receipt_before
    assert recorded == []


def test_t041_retry_with_operator_draft_defers_without_consuming_the_single_retry():
    client = _ObservedClient()
    state, hop, worker, acquired, _recorded = _fake_environment(client)
    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    result = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True, composer_text="draft"))
    assert result.reason == "provider_retry_composer_conflict"
    assert client.retry_calls == []
    assert "provider_retry" not in hop["wait"]

    clean = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert clean.reason == "provider_retry_clicked"
    assert len(client.retry_calls) == 1


def test_t041_retry_error_after_durable_intent_is_reconciliation_gated():
    client = _ObservedClient()
    client.retry_error = UnsafePageStateError("Retry generation was not available: retry_unavailable")
    state, hop, worker, acquired, _recorded = _fake_environment(client)
    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    result = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert result.reason == "provider_retry_reconciliation_required"
    assert hop["wait"]["provider_retry"]["result"] == "ambiguous"
    assert hop["wait"]["provider_retry"]["intent_status"] == "recovery_required"
    hop["wait"].update(
        controller_progress_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(),
        timeout_refreshed=True,
        timeout_status_checked=True,
        timeout_status="COMPLETE",
    )
    again = _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert again.reason == "provider_retry_reconciliation_required"
    assert len(client.retry_calls) == 1
    assert "block_code" not in state and "repair" not in state


# T042 ----------------------------------------------------------------------------


def _store(tmp_path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    return TaskStore(config)


def _accepted_turn(store, *, roles, closing_role, task_id, team, route_targets=()):
    state = store.create_task(
        "phase 1.5 accepted turn",
        requested_team=team,
        task_id=task_id,
        roles=roles,
        route_targets=route_targets,
        closing_role=closing_role,
    )
    worker = CDPAWorker(store.config, store=store)
    worker.runtime_db.ensure_schema()
    hop = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    role = hop["target_role"]
    repository, plans_root = _workflow_report_roots(store.config, state)
    expected = expected_report_relative(
        plans_root=plans_root,
        repository_root=repository,
        team=str(state["team"]),
        physical_role=str(hop["physical_role"]),
        turn=int(hop["turn"]),
        task_id=str(state["task_id"]),
    )
    hop.update(
        state="waiting",
        receipt=_receipt(physical_role=str(hop["physical_role"])).to_dict(),
        expected_report_path=expected,
        wait={},
    )
    state["status"] = "RUNNING"
    state["active_role"] = role
    state["roles"][role].update(page_url=f"https://chatgpt.com/c/{CONVERSATION}", page_id="page-1")
    return state, hop, worker, Path(repository) / expected


def _write_report(path: Path, text: str = "# Review\n\nAccepted.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_t042_terminal_route_reconciliation_wins_over_stable_retry_and_closes_done(tmp_path):
    store = _store(tmp_path)
    state, hop, worker, report = _accepted_turn(
        store, roles=("REVIEW",), closing_role="REVIEW", task_id="task-reconcile", team="reconcile"
    )
    _write_report(report)
    text = '{"route":"DONE","handoff":"%s"}' % hop["expected_report_path"]
    # Retry is rendered and the terminal JSON has no completion action, which previously stalled REVIEW.
    current = snapshot(retry_visible=True, messages=_assistant_turn(text))
    client = _ObservedClient()
    acquired = SimpleNamespace(client=client, page_id="page-1")
    controller = RoleController()
    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    first = _run(controller, worker, state, hop, acquired, current)
    assert first.action is Action.WAIT
    assert client.retry_calls == []  # unstable valid result is never Retried
    hop["wait"]["result_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    accepted = _run(controller, worker, state, hop, acquired, current)

    assert accepted.action is Action.ACCEPT
    assert accepted.reason == "terminal_route_reconciled"
    assert client.retry_calls == []
    reconciliation = hop["wait"]["terminal_reconciliation"]
    assert reconciliation["result"] == "reconciled"
    assert reconciliation["source"] == "dom_assistant_text"
    assert reconciliation["route"] == "DONE"
    assert reconciliation["accepted_user_turn"] == "user-original"
    assert reconciliation["report_size"] == report.stat().st_size
    assert hop["state"] == "responded"

    worker._responded(state, hop)
    assert state["status"] == "DONE"
    assert hop["route"] == "DONE"
    assert hop["report_size"] == report.stat().st_size
    assert len([item for item in state["hops"]]) == 1  # no repair, new Send, or new chat hop


@pytest.mark.parametrize(
    "case",
    ["missing_report", "empty_report", "mismatched_report", "historical_turn", "route_outside_contract"],
)
def test_t042_stale_foreign_or_invalid_terminal_evidence_fails_closed_to_retry(tmp_path, case):
    store = _store(tmp_path)
    state, hop, worker, report = _accepted_turn(
        store, roles=("REVIEW",), closing_role="REVIEW", task_id=f"task-{case.replace('_', '-')}", team=case.replace("_", "-")
    )
    text = '{"route":"DONE","handoff":"%s"}' % hop["expected_report_path"]
    messages = _assistant_turn(text)
    if case == "empty_report":
        _write_report(report, "")
    elif case != "missing_report":
        _write_report(report)
    if case == "mismatched_report":
        messages = _assistant_turn('{"route":"DONE","handoff":".plan/other/REVIEW_turn9_x.md"}')
    if case == "historical_turn":
        messages = (
            MessageSnapshot("assistant", "assistant-old", "turn-old", text, ()),
            MessageSnapshot("user", "user-original", "turn-user", "original task", ()),
        )
    if case == "route_outside_contract":
        messages = _assistant_turn('{"route":"DEV","handoff":"%s"}' % hop["expected_report_path"])
    wait = hop["wait"]
    validate = lambda candidate: worker._validate_response_candidate(state, hop, candidate)
    reconcile = lambda candidate: worker._terminal_route_evidence(state, hop, candidate)
    current = snapshot(retry_visible=True, messages=messages)
    decisions = [
        RoleController().decide(
            current, wait, receipt=_receipt(physical_role=str(hop["physical_role"])),
            validate=validate, reconcile=reconcile, now=NOW + timedelta(seconds=seconds),
        )
        for seconds in (0, 6, 12)
    ]
    assert all(decision.action is not Action.ACCEPT for decision in decisions)
    if case in {"missing_report", "empty_report", "mismatched_report"}:
        assert wait["terminal_reconciliation"]["result"] == "rejected"
    if case in {"missing_report", "empty_report", "mismatched_report", "historical_turn"}:
        assert decisions[-1].action is Action.RETRY


# T043 ----------------------------------------------------------------------------


def _enqueue(worker, *, key, kind, task_id, payload):
    worker.runtime_db.ensure_schema()
    worker.runtime_db.enqueue_command(
        command_id=f"cmd-{key}", idempotency_key=key, kind=kind, task_id=task_id,
        expected_task_version=None, payload=payload,
    )
    return worker._apply_next_command(allowed_kinds=(kind,))


def _register(worker, state, *, key="spawn-key-1", job_id="job-42", role="DEV", delivery="1", **extra):
    payload = DashboardAPI.normalize_async_spawn(
        state["task_id"], {"role": role, "job_id": job_id, **extra}, spawn_idempotency_key=key
    )
    return _enqueue(worker, key=f"register-{key}-{job_id}-{delivery}", kind="async_continuation_register", task_id=state["task_id"], payload=payload), payload


def _callback(worker, state, continuation_id, *, key, **body):
    payload = DashboardAPI.normalize_async_callback(continuation_id, {"status": "succeeded", "exit_code": 0, "stdout": "done\n", **body})
    return _enqueue(worker, key=key, kind="async_continuation_complete", task_id=state["task_id"], payload=payload)


def _waiting_dev_turn(tmp_path, task_id="task-async"):
    store = _store(tmp_path)
    state, hop, worker, _report = _accepted_turn(
        store, roles=("DEV", "REVIEW"), closing_role="DEV", task_id=task_id, team=task_id.replace("task-", "")
    )
    state = store.update(state["manifest_path"], lambda _current: state)
    return store, worker, state


def _commit_wait(store, worker, state, continuation_id):
    current = store.load(state["manifest_path"])
    hop = next(item for item in current["hops"] if item["hop_id"] == current["active_hop_id"])
    hop["response"] = '{"route":"WAIT","handoff":"%s"}' % continuation_id
    hop["state"] = "responded"
    worker._responded(current, hop)
    return store.update(current["manifest_path"], lambda _latest: current)


def test_t043_wait_is_admitted_only_after_confirmed_spawn_and_sets_async_waiting(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    hop = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    assert "WAIT" not in worker._frozen_route_contract(state, "DEV").allowed_routes
    with pytest.raises(ValueError):
        worker._validate_response_candidate(
            state, hop, MessageSnapshot("assistant", "a", "t", '{"route":"WAIT","handoff":"ac-000000000000000000000000"}', ())
        )
    assert "WAIT" not in worker._gate_a_route_contract(state, "DEV").allowed_routes  # never in the prompt enum

    command, payload = _register(worker, state)
    assert command["status"] == "applied", command
    continuation_id = payload["continuation_id"]
    registered = store.load(state["manifest_path"])
    record = registered["async_continuations"][continuation_id]
    assert record["state"] == "spawn_confirmed" and record["job_id"] == "job-42"
    assert "WAIT" in worker._frozen_route_contract(registered, "DEV").allowed_routes

    duplicate, _ = _register(worker, state, delivery="redelivered")
    assert duplicate["status"] == "applied"
    assert len(store.load(state["manifest_path"])["async_continuations"]) == 1
    conflict, _ = _register(worker, state, job_id="job-other")
    assert conflict["status"] == "failed"
    assert "different job metadata" in str(conflict["error"])

    route_seq = registered["route_seq"]
    waiting = _commit_wait(store, worker, registered, continuation_id)
    assert waiting["status"] == "WAITING"
    assert waiting["waiting_code"] == "async_callback"
    assert waiting["waiting"]["continuation_id"] == continuation_id
    assert waiting["route_seq"] == route_seq
    wait_hop = next(item for item in waiting["hops"] if item["hop_id"] == waiting["active_hop_id"])
    assert wait_hop["route"] == "WAIT" and wait_hop["state"] == "routed"
    record = waiting["async_continuations"][continuation_id]
    assert record["state"] == "waiting"
    assert record["parent"]["conversation_id"] == CONVERSATION
    assert workflow_control_eligibility(waiting, "resume")["eligible"] is False
    assert is_active_team_owner(waiting) is True


def test_t043_callback_wakes_same_task_role_and_parent_conversation_exactly_once(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    _command, payload = _register(worker, state, wake_prompt="Summarize the benchmark result.")
    continuation_id = payload["continuation_id"]
    waiting = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)
    hop_count = len(waiting["hops"])
    task_count = len(store.discover())

    done = _callback(worker, waiting, continuation_id, key="callback-1", stdout="x" * 9000 + "tail-marker", stderr="warn")
    assert done["status"] == "applied", done
    woken = store.load(state["manifest_path"])
    assert woken["status"] == "RUNNING"
    assert woken.get("waiting_code") is None
    assert len(woken["hops"]) == hop_count + 1
    wake = woken["hops"][-1]
    assert wake["kind"] == "async_wake"
    assert wake["state"] == "pre_send"
    assert wake["target_role"] == "DEV" and wake["source_role"] == "DEV"
    assert wake["physical_role"] == waiting["roles"]["DEV"]["physical_role"]
    assert wake["async_continuation_id"] == continuation_id
    assert "tail-marker" in wake["handoff"] and "Summarize the benchmark result." in wake["handoff"]
    record = woken["async_continuations"][continuation_id]
    assert record["state"] == "wake_scheduled"
    assert record["wake"]["hop_id"] == wake["hop_id"]
    assert len(record["callback"]["stdout_tail"]) == async_continuation.OUTPUT_TAIL_CHARS
    assert woken["materials"][f"{continuation_id}-stdout"]["source_kind"] == "provider_result"
    assert woken["roles"]["DEV"]["page_url"] == f"https://chatgpt.com/c/{CONVERSATION}"
    assert len(store.discover()) == task_count  # never a fallback task

    duplicate = _callback(worker, woken, continuation_id, key="callback-redelivery", stdout="x" * 9000 + "tail-marker", stderr="warn")
    assert duplicate["status"] == "applied" and duplicate["result"]["duplicate"] is True
    assert len(store.load(state["manifest_path"])["hops"]) == hop_count + 1

    conflict = _callback(worker, woken, continuation_id, key="callback-conflict", stdout="different")
    assert conflict["status"] == "failed"
    assert "ASYNC_CALLBACK_CONFLICT" in str(conflict["error"]) or "different callback payload" in str(conflict["error"])
    assert len(store.load(state["manifest_path"])["hops"]) == hop_count + 1


def test_t043_callback_before_wait_commit_wakes_once_when_wait_arrives(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    _command, payload = _register(worker, state)
    continuation_id = payload["continuation_id"]
    early = _callback(worker, state, continuation_id, key="callback-early")
    assert early["result"]["wake"]["status"] == "pending_wait"
    assert store.load(state["manifest_path"])["status"] == "RUNNING"

    woken = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)
    assert woken["status"] == "RUNNING"
    assert woken["hops"][-1]["kind"] == "async_wake"
    assert woken["async_continuations"][continuation_id]["state"] == "wake_scheduled"


def test_t043_parent_identity_mismatch_blocks_without_new_chat_task_or_role_slot(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    _command, payload = _register(worker, state)
    continuation_id = payload["continuation_id"]
    waiting = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)

    def drift(current):
        current["roles"]["DEV"]["page_url"] = "https://chatgpt.com/c/99999999-2222-4333-8444-555555555555"
        return current

    drifted = store.update(waiting["manifest_path"], drift)
    hop_count = len(drifted["hops"])
    roles = sorted(drifted["roles"])
    _callback(worker, drifted, continuation_id, key="callback-mismatch")
    blocked = store.load(state["manifest_path"])
    assert blocked["status"] == "BLOCKED"
    assert blocked["block_code"] == "ASYNC_WAKE_IDENTITY_MISMATCH"
    assert len(blocked["hops"]) == hop_count
    assert sorted(blocked["roles"]) == roles
    assert blocked["async_continuations"][continuation_id]["wake"]["status"] == "blocked"


def test_t043_failed_callback_without_failure_handoff_blocks_visibly(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    _command, payload = _register(worker, state)
    continuation_id = payload["continuation_id"]
    waiting = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)
    _callback(worker, waiting, continuation_id, key="callback-failed", status="failed", exit_code=2)
    blocked = store.load(state["manifest_path"])
    assert blocked["block_code"] == "ASYNC_CALLBACK_FAILED"
    assert blocked["async_continuations"][continuation_id]["state"] == "failed"
    assert blocked["hops"][-1]["kind"] != "async_wake"


def test_t043_wake_send_reuses_exact_parent_page_or_blocks(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    _command, payload = _register(worker, state)
    continuation_id = payload["continuation_id"]
    waiting = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)
    woken_state = store.load(waiting["manifest_path"])
    _callback(worker, woken_state, continuation_id, key="callback-ok")
    woken = store.load(state["manifest_path"])
    wake = woken["hops"][-1]

    class Actions:
        def __init__(self, *, located=True, url=f"https://chatgpt.com/c/{CONVERSATION}", new_chat=False):
            self.located, self.url, self.new_chat = located, url, new_chat

        async def locate_owned(self, _state, _role):
            return object() if self.located else None

        async def acquire(self, _state, _role, *, clean_for_send=False):
            assert clean_for_send is True
            return SimpleNamespace(url=self.url, new_chat=self.new_chat, created=False, page_id="page-1")

    ok_state = copy.deepcopy(woken)
    acquired = asyncio.run(worker._acquire_async_wake_page(ok_state, wake, "DEV", Actions()))
    assert acquired is not None
    assert ok_state["async_continuations"][continuation_id]["wake"]["status"] == "verified"

    for actions in (
        Actions(located=False),
        Actions(url="https://chatgpt.com/c/99999999-2222-4333-8444-555555555555"),
        Actions(new_chat=True),
    ):
        blocked_state = copy.deepcopy(woken)
        assert asyncio.run(worker._acquire_async_wake_page(blocked_state, wake, "DEV", actions)) is None
        assert blocked_state["block_code"] == "ASYNC_WAKE_IDENTITY_MISMATCH"


def test_t043_registration_requires_active_accepted_turn_of_that_role(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    wrong_role, _ = _register(worker, state, role="REVIEW")
    assert wrong_role["status"] == "failed"
    assert "active accepted turn" in str(wrong_role["error"])


# T044 ----------------------------------------------------------------------------


def test_t044_projection_exposes_phase15_control_and_wait_evidence(tmp_path):
    store, worker, state = _waiting_dev_turn(tmp_path)
    hop = next(item for item in state["hops"] if item["hop_id"] == state["active_hop_id"])
    client = _ObservedClient()
    acquired = SimpleNamespace(client=client, page_id="page-1")
    _run(RoleController(), worker, state, hop, acquired, snapshot(
        mcp_permission_allow_count=1, mcp_permission_surface_key="one-tool-call"
    ))
    hop["wait"]["retry_seen_at"] = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
    _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    persisted = store.update(state["manifest_path"], lambda _current: state)

    projection = build_task_projection(persisted, tasks=[persisted])
    control = projection.summary["browser_control"]
    assert control["retry_visible"] is True
    assert control["stop_visible"] is False
    assert control["permission_click_count"] == 1
    assert control["last_permission_click"]["result"] == "clicked"
    assert control["retry_decision"]["result"] == "progress"
    assert control["controller_reason"] == "provider_retry_clicked"
    kinds = {item["kind"] for item in projection.detail["timeline"] if item["level"] == "CONTROL"}
    assert {"mcp_allow", "provider_retry"} <= kinds

    _command, payload = _register(worker, persisted)
    continuation_id = payload["continuation_id"]
    waiting = _commit_wait(store, worker, store.load(state["manifest_path"]), continuation_id)
    waiting_projection = build_task_projection(waiting, tasks=[waiting])
    assert waiting_projection.summary["async_continuation"]["continuation_id"] == continuation_id
    assert waiting_projection.summary["async_continuation"]["state"] == "waiting"
    assert waiting_projection.summary["lifecycle_kind"] == "async_wait"

    _callback(worker, waiting, continuation_id, key="callback-projection")
    woken = store.load(state["manifest_path"])
    detail = build_task_projection(woken, tasks=[woken]).detail
    projected = detail["async_continuations"][0]
    assert projected["callback"]["status"] == "succeeded"
    assert projected["wake"]["status"] == "scheduled"

    legacy_summary = build_task_projection(woken, tasks=[woken]).summary
    audit = LegacyObservationAudit.__new__(LegacyObservationAudit)
    bridged = audit._task_summary(legacy_summary)
    assert bridged["browser_control"] == legacy_summary["browser_control"]
    assert bridged["async_continuation"]["wake"]["status"] == "scheduled"

    api = RuntimeApiServer(
        config=SimpleNamespace(),
        state_store=SimpleNamespace(list_task_records=lambda: [{"runtime_task_id": "rt-1", "legacy_task_id": woken["task_id"], "created_at": "t"}]),
        endpoint_inventory=SimpleNamespace(),
        legacy_audit=SimpleNamespace(task_summaries=lambda: {woken["task_id"]: bridged}),
    )
    item = api._projected_task_records()[0]
    assert item["browser_control"]["retry_decision"]["result"] == "progress"
    assert item["async_continuation"]["callback"]["status"] == "succeeded"


def test_t044_api_normalization_is_deterministic_and_bounded():
    first = DashboardAPI.normalize_async_spawn("task-1", {"role": "dev", "job_id": "job"}, spawn_idempotency_key="k")
    again = DashboardAPI.normalize_async_spawn("task-1", {"role": "DEV", "job_id": "job"}, spawn_idempotency_key="k")
    assert first["continuation_id"] == again["continuation_id"] == async_continuation.continuation_id_for("task-1", "k")
    assert first["role"] == "DEV"
    from playwright_auto.dashboard_api import APIError

    with pytest.raises(APIError):
        DashboardAPI.normalize_async_spawn("task-1", {"role": "WAIT", "job_id": "job"}, spawn_idempotency_key="k")
    with pytest.raises(APIError):
        DashboardAPI.normalize_async_callback(first["continuation_id"], {"status": "done"})
    with pytest.raises(APIError):
        DashboardAPI.normalize_async_callback("not-a-continuation", {"status": "succeeded"})
