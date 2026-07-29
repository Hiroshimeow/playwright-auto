from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    capture_message_baseline,
)
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import utc_now
from playwright_auto.durable import RequestLedger, RequestStatus

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop

from test_dashboard_api import request, start_api
from test_cdpa_worker import FakeActions, _prepare_sent_waiting_task, setup_task


def _queue_blocked_resume(store, state, *, code="response_timeout", reason="expired wait"):
    path = Path(state["manifest_path"])
    state["status"] = "BLOCKED"
    state["kanban_column"] = "BLOCKED"
    state["block_code"] = code
    state["block_retryable"] = False
    state["block_reason"] = reason
    store.save(path, state)
    return store.request_resume(path, reason="operator resume")


def _accepted_snapshot(receipt: SendReceipt, *, response: MessageSnapshot | None = None, retry=False):
    messages = [
        MessageSnapshot(
            "user",
            str(receipt.user_message_id),
            str(receipt.user_turn_id),
            receipt.prompt,
            (),
        )
    ]
    if response is not None:
        messages.append(response)
    return SimpleNamespace(
        state=ChatGPTState.ERROR if retry else ChatGPTState.WAITING_PROMPT,
        stop_visible=False,
        retry_visible=retry,
        composer_empty=True,
        composer_text="",
        manual_input_pending=False,
        attachment_markers=(),
        send_enabled=False,
        error_texts=("Something went wrong",) if retry else (),
        blocking_dialogs=(),
        messages=tuple(messages),
        response_activity_length=0,
        response_activity_turn_id=None,
        conversation_url="https://chatgpt.com/c/exact",
        url="https://chatgpt.com/c/exact",
        page_id=receipt.binding.page_id,
        page_role=receipt.binding.role,
        page_task_id=None,
        page_team=None,
    )


def test_resume_control_stays_recovering_until_verified_postcondition(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-recovering")
    path = Path(state["manifest_path"])
    state = _queue_blocked_resume(store, state)

    applied = asyncio.run(worker._apply_control(state, FakeActions(), path))

    assert applied is True
    control = state["controls"][-1]
    assert control["status"] == "recovering"
    assert control["command_state"] == "RUNNING"
    assert control["result"]["outcome"] == "recovering"
    assert control["result"]["before"]["block_code"] == "response_timeout"
    assert control["applied_at"] is None








def test_resume_consumes_existing_stable_response_without_another_send(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-consume-response"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-resume-consume-response.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("verified response", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-resume",
        "ta-resume",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    snapshot = _accepted_snapshot(receipt, response=response)

    class Client:
        def __init__(self):
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("accepted request must not be sent again")

    client = Client()
    acquired = AcquiredRole(client, receipt.binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "consume_response"
    assert control["result"]["postcondition"] in {"response_consumed", "hop_advanced"}
    assert client.send_calls == 0
    assert _active_hop(recovered)["target_role"] == "TEST"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).status is RequestStatus.COMPLETED






def test_manual_composer_conflict_returns_recovery_required_and_is_untouched(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, _hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-manual-conflict"
    )
    snapshot = _accepted_snapshot(receipt, retry=True)
    snapshot.composer_empty = False
    snapshot.composer_text = "operator manual draft"
    snapshot.manual_input_pending = True

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, *_args, **_kwargs):
            raise AssertionError("manual composer must fail closed before response mutation")

        async def retry_generation(self, *_args, **_kwargs):
            raise AssertionError("manual composer must not be mutated")

    acquired = AcquiredRole(Client(), receipt.binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    assert control["status"] == "recovery_required"
    assert control["result"]["outcome"] == "recovery_required"
    assert control["result"]["reason_code"] == "manual_composer_conflict"
    assert snapshot.composer_text == "operator manual draft"






def _prepare_sending_record(tmp_path: Path, *, task_id: str):
    _config, store, state, worker = setup_task(tmp_path, task_id=task_id)
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    baseline = MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset())
    binding = PageBinding("page-alpha-plan", "alpha-plan")
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role="alpha-plan",
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash="",
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=binding,
        baseline=baseline,
        session_id_before="exact",
    )
    state["roles"]["PLAN"].update(
        page_id=binding.page_id,
        page_url="https://chatgpt.com/c/exact",
        online=True,
    )
    hop["conversation_url"] = "https://chatgpt.com/c/exact"
    state = store.save(path, state)
    hop = _active_hop(state)
    return store, state, worker, path, hop, ledger, binding, baseline


def test_exact_owned_sending_draft_is_accepted_once(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-owned-draft"
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.DRAFT,
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text=hop["prompt"],
        composer_empty=False,
        manual_input_pending=True,
        attachment_markers=(),
        send_enabled=True,
        stop_visible=False,
        retry_visible=False,
        blocking_dialogs=(),
        messages=(),
        conversation_url="https://chatgpt.com/c/exact",
        url="https://chatgpt.com/c/exact",
    )

    class Client:
        def __init__(self):
            self.binding = binding
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def send(self, prompt, **kwargs):
            self.send_calls += 1
            assert prompt == hop["prompt"]
            assert kwargs["max_attempts"] == 1
            return SendReceipt(
                prompt=prompt,
                prompt_sha256=hop["prompt_sha256"],
                binding=binding,
                baseline=baseline,
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="exact",
                user_message_id="u-owned",
                user_turn_id="t-owned",
            )

    client = Client()
    acquired = AcquiredRole(client, binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state, code="durable_send_ambiguous", reason="SENDING persisted")
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["action"] == "accept_owned_draft"
    assert control["result"]["postcondition"] == "draft_accepted_once"
    assert client.send_calls == 1
    record = ledger.get(hop["request_id"])
    assert record.status is RequestStatus.SENT
    assert record.receipt["user_message_id"] == "u-owned"














def test_dashboard_distinguishes_continued_recovery_failed_queued_and_stale_worker():
    app = Path("src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")
    actions = Path(
        "src/playwright_auto/dashboard_assets/views/dashboard_actions.js"
    ).read_text(encoding="utf-8")
    combined = app + "\n" + actions

    for label in ("continued", "recovery required", "failed", "queued", "stale worker"):
        assert label in combined.casefold()
