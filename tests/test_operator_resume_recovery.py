from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, BranchBootstrapError, CDPATabActions
from playwright_auto.cdpa_bootstraps import BootstrapCatalog
from playwright_auto.chatgpt import (
    ChatGPTPage,
    ChatGPTSnapshot,
    ChatGPTState,
    ComposerConflictError,
    ManualInputPendingError,
    RateLimitBlockedError,
    MessageBaseline,
    MessageSnapshot,
    PageBinding,
    SendReceipt,
    StableMalformedResponseError,
    capture_message_baseline,
)
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import utc_now
from playwright_auto.durable import RequestLedger, RequestStatus
from playwright_auto.upload import UploadReceipt, collect_file_identities

import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_worker import _active_hop

from test_dashboard_api import request, start_api
from test_cdpa_worker import (
    FakeActions,
    RecordingCDPASendActions,
    RecordingCDPASendClient,
    _backend_graph,
    _bootstrap_graph,
    _prepare_sent_waiting_task,
    setup_task,
)


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








def test_resume_backend_complete_ignores_manual_composer_and_routes_without_source_tab(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-backend-complete"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-resume-backend-complete.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("verified backend response", encoding="utf-8")
    response_text = json.dumps({"route": "TEST", "handoff": report_relative})
    canonical_id = "resume-backend-complete"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["wait"]["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    hop["wait"]["stream_status_next_poll_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)
    calls = {"status": 0, "graph": 0, "tab": 0}
    dirty_snapshot = _accepted_snapshot(enriched)
    dirty_snapshot.composer_empty = False
    dirty_snapshot.composer_text = "operator manual draft"
    dirty_snapshot.manual_input_pending = True
    dirty_snapshot.attachment_markers = ("manual.txt",)

    class DirtyClient:
        async def assert_ownership(self):
            return dirty_snapshot

    dirty_acquired = AcquiredRole(
        DirtyClient(), enriched.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == canonical_id
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def backend_conversation(self, conversation_id):
            assert conversation_id == canonical_id
            calls["graph"] += 1
            return _backend_graph(enriched.user_message_id, "a-resume-backend", response_text)

        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("backend-complete Resume must not inspect a source tab")

        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            return dirty_acquired

        async def reopen(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("backend-complete Resume must not reopen the accepted tab")

        async def wake(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("backend-complete Resume must not wake a source tab")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    completed = recovered["hops"][0]
    record = RequestLedger(completed["ledger_path"]).get(completed["request_id"])
    assert calls == {"status": 1, "graph": 1, "tab": 0}
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "consume_response"
    assert control["result"]["postcondition"] == "hop_advanced"
    assert completed["response"] == response_text
    assert _active_hop(recovered)["target_role"] == "TEST"
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1
    assert dirty_snapshot.composer_text == "operator manual draft"
    assert dirty_snapshot.attachment_markers == ("manual.txt",)


def test_resume_backend_complete_force_follows_manual_steering_without_replay(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-manual-steer"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-resume-manual-steer.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("manual steering accepted", encoding="utf-8")
    response_text = json.dumps({"route": "TEST", "handoff": report_relative})
    canonical_id = "resume-manual-steer"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    calls = {"status": 0, "graph": 0, "tab": 0}

    graph = {
        "current_node": "a-final",
        "mapping": {
            enriched.user_message_id: {
                "id": enriched.user_message_id,
                "parent": None,
                "children": ["a-before"],
                "message": {
                    "id": enriched.user_message_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [enriched.prompt]},
                },
            },
            "a-before": {
                "id": "a-before",
                "parent": enriched.user_message_id,
                "children": ["u-steer"],
                "message": {
                    "id": "a-before",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": ["holding"]},
                },
            },
            "u-steer": {
                "id": "u-steer",
                "parent": "a-before",
                "children": ["a-final"],
                "message": {
                    "id": "u-steer",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["route test now"]},
                },
            },
            "a-final": {
                "id": "a-final",
                "parent": "u-steer",
                "children": [],
                "message": {
                    "id": "a-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "content": {"content_type": "text", "parts": [response_text]},
                },
            },
        },
    }

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == canonical_id
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def backend_conversation(self, conversation_id):
            assert conversation_id == canonical_id
            calls["graph"] += 1
            return graph

        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("force Resume must not inspect source DOM")

        async def reopen(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("force Resume must not reopen source tab")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    completed = recovered["hops"][0]
    record = RequestLedger(completed["ledger_path"]).get(completed["request_id"] )
    control = recovered["controls"][-1]
    assert calls == {"status": 1, "graph": 1, "tab": 0}
    assert control["status"] == "applied"
    assert control["result"]["action"] == "consume_response"
    assert completed["response"] == response_text
    assert _active_hop(recovered)["target_role"] == "TEST"
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_resume_backend_complete_malformed_response_uses_existing_route_repair(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-backend-malformed"
    )
    canonical_id = "resume-backend-malformed"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    calls = {"status": 0, "graph": 0, "tab": 0}

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == canonical_id
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def backend_conversation(self, conversation_id):
            assert conversation_id == canonical_id
            calls["graph"] += 1
            return _backend_graph(enriched.user_message_id, "a-resume-malformed", "not json")

        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("backend-complete malformed Resume must not inspect a source tab")

        async def reopen(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("backend-complete malformed Resume must not reopen a source tab")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    completed = recovered["hops"][0]
    repair = _active_hop(recovered)
    control = recovered["controls"][-1]
    record = RequestLedger(completed["ledger_path"]).get(completed["request_id"])
    assert calls == {"status": 1, "graph": 1, "tab": 0}
    assert control["status"] == "applied"
    assert control["result"]["action"] == "consume_response"
    assert completed["response"] == "not json"
    assert completed.get("validation_error")
    assert repair["kind"] == "route_repair"
    assert repair["target_role"] == "PLAN"
    assert repair["turn"] == 1
    assert repair["repair_attempt"] == 1
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1


def test_resume_backend_streaming_ignores_manual_composer_and_rearms_without_source_tab(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-backend-streaming"
    )
    canonical_id = "resume-backend-streaming"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    hop["wait"]["deadline_at"] = expired_at.isoformat()
    hop["wait"]["stream_status_next_poll_at"] = expired_at.isoformat()
    state = store.save(path, state)
    hop = _active_hop(state)
    calls = {"status": 0, "tab": 0}
    dirty_snapshot = _accepted_snapshot(enriched)
    dirty_snapshot.composer_empty = False
    dirty_snapshot.composer_text = "operator manual draft"
    dirty_snapshot.manual_input_pending = True
    dirty_snapshot.attachment_markers = ("manual.txt",)

    class DirtyClient:
        async def assert_ownership(self):
            return dirty_snapshot

    dirty_acquired = AcquiredRole(
        DirtyClient(), enriched.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == canonical_id
            calls["status"] += 1
            return {"status": "IS_STREAMING"}

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("IS_STREAMING Resume must not fetch the full graph")

        async def locate_owned_metadata(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("healthy backend Resume must not inspect source metadata")

        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            return dirty_acquired

        async def reopen(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("healthy backend Resume must not reopen a source tab")

        async def wake(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("healthy backend Resume must not wake a source tab")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    active = _active_hop(recovered)
    control = recovered["controls"][-1]
    record = RequestLedger(active["ledger_path"]).get(active["request_id"])
    assert calls == {"status": 1, "tab": 0}
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "rearm_backend_wait"
    assert control["result"]["postcondition"] == "backend_wait_rearmed"
    assert recovered["status"] == "RUNNING"
    assert recovered["active_action"] == "wait_response"
    assert active["state"] == "waiting"
    assert active["wait"]["completion_mode"] == "stream_status"
    assert worker_module.parse_time(active["wait"]["deadline_at"]) > datetime.now(timezone.utc)
    assert worker_module.parse_time(active["wait"]["stream_status_next_poll_at"]) > datetime.now(
        timezone.utc
    )
    assert record is not None
    assert record.status is RequestStatus.SENT
    assert record.attempts == 1
    assert dirty_snapshot.composer_text == "operator manual draft"
    assert dirty_snapshot.attachment_markers == ("manual.txt",)






def test_resume_backend_unavailable_dom_fallback_ignores_manual_composer_without_replay(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-backend-unavailable"
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-resume-backend-unavailable.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("verified DOM fallback response", encoding="utf-8")
    response = MessageSnapshot(
        "assistant",
        "a-resume-dom-fallback",
        "ta-resume-dom-fallback",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )
    canonical_id = "resume-backend-unavailable"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)
    snapshot = _accepted_snapshot(enriched, response=response)
    snapshot.conversation_url = hop["conversation_url"]
    snapshot.url = hop["conversation_url"]
    snapshot.composer_empty = False
    snapshot.composer_text = "operator manual draft"
    snapshot.manual_input_pending = True
    snapshot.attachment_markers = ("manual.txt",)
    order = []

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            order.append("dom_wait")
            kwargs["candidate_validator"](response)
            return response

        async def retry_generation(self, *_args, **_kwargs):
            raise AssertionError("accepted Resume fallback must not Retry or Regenerate")

        async def send(self, *_args, **_kwargs):
            raise AssertionError("accepted Resume fallback must not resend")

    acquired = AcquiredRole(Client(), enriched.binding.page_id, hop["conversation_url"], False, False)

    class Actions:
        async def backend_stream_status(self, conversation_id):
            assert conversation_id == canonical_id
            order.append("backend_status")
            raise worker_module.BackendUnavailableError(0, "stream_status")

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("status-unavailable Resume should enter exact DOM fallback")

        async def locate_owned(self, _state, _role):
            order.append("locate_exact")
            return acquired

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("existing exact fallback tab should be reused")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    completed = recovered["hops"][0]
    control = recovered["controls"][-1]
    record = RequestLedger(completed["ledger_path"]).get(completed["request_id"])
    assert order == ["backend_status", "locate_exact", "dom_wait"]
    assert control["status"] == "applied"
    assert control["result"]["action"] == "consume_response"
    assert _active_hop(recovered)["target_role"] == "TEST"
    assert record is not None
    assert record.attempts == 1
    assert record.status is RequestStatus.COMPLETED
    assert snapshot.composer_text == "operator manual draft"
    assert snapshot.attachment_markers == ("manual.txt",)


def test_resume_ambiguous_backend_evidence_fails_closed_before_dom(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-backend-ambiguous"
    )
    canonical_id = "resume-backend-ambiguous"
    enriched = replace(receipt, conversation_id=canonical_id)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=enriched.to_dict())
    hop["receipt"] = enriched.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)
    calls = {"status": 0, "graph": 0, "tab": 0}

    class Actions:
        async def backend_stream_status(self, _conversation_id):
            calls["status"] += 1
            return {"status": "COMPLETE"}

        async def backend_conversation(self, _conversation_id):
            calls["graph"] += 1
            raise worker_module.GraphIdentityError("ambiguous terminal branch")

        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("ambiguous backend evidence must fail closed before DOM")

        async def reopen(self, *_args, **_kwargs):
            calls["tab"] += 1
            raise AssertionError("ambiguous backend evidence must not reopen")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert calls == {"status": 1, "graph": 1, "tab": 0}
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "backend_evidence_ambiguous"
    assert recovered["status"] == "BLOCKED"
    assert record is not None
    assert record.attempts == 1


def test_resume_immutable_receipt_divergence_fails_before_backend_or_dom(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-receipt-divergence"
    )
    ledger_receipt = replace(receipt, conversation_id="ledger-canonical")
    hop_receipt = replace(receipt, conversation_id="hop-canonical")
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=ledger_receipt.to_dict())
    hop["receipt"] = hop_receipt.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/c/hop-canonical"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("receipt divergence must fail before backend reads")

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("receipt divergence must fail before backend reads")

        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("receipt divergence must fail before DOM")

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("receipt divergence must fail before reopen")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert control["status"] == "failed"
    assert control["result"]["reason_code"] == "resume_recovery_failed"
    assert "conversation identity" in control["result"]["reason"]
    assert recovered["status"] == "BLOCKED"
    assert record is not None
    assert record.attempts == 1


def test_resume_different_accepted_binding_fails_before_backend_or_rebind(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-binding-divergence"
    )
    canonical_id = "resume-binding-divergence"
    hop_receipt = replace(receipt, conversation_id=canonical_id)
    ledger_receipt = replace(
        hop_receipt,
        binding=PageBinding("different-accepted-page", hop_receipt.binding.role),
    )
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=ledger_receipt.to_dict())
    hop["receipt"] = hop_receipt.to_dict()
    hop["conversation_url"] = f"https://chatgpt.com/c/{canonical_id}"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("binding divergence must fail before backend reads")

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("binding divergence must fail before backend reads")

        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("binding divergence must not inspect or rebind DOM ownership")

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("binding divergence must not reopen another page")

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert control["status"] == "failed"
    assert control["result"]["reason_code"] == "resume_recovery_failed"
    assert "immutable receipt diverged" in control["result"]["reason"]
    assert recovered["status"] == "BLOCKED"
    assert record is not None
    assert record.attempts == 1


def test_resume_routes_path_only_response_without_replay(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-path-only"
    )
    hop["conversation_url"] = "https://chatgpt.com/c/exact"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)
    handoff = ".plan/windows-team/windows-plan_turn1_task-resume-path-only.md"
    response = MessageSnapshot(
        "assistant",
        "a-path-only",
        "ta-path-only",
        json.dumps({"route": "TEST", "handoff": handoff}),
        (),
    )
    snapshot = _accepted_snapshot(receipt, response=response)
    original = {
        "task_id": state["task_id"],
        "team": state["team"],
        "request_id": hop["request_id"],
        "receipt": json.loads(json.dumps(hop["receipt"])),
        "conversation_url": hop["conversation_url"],
        "page_id": receipt.binding.page_id,
        "physical_role": hop["physical_role"],
        "user_message_id": receipt.user_message_id,
        "user_turn_id": receipt.user_turn_id,
    }

    class Client:
        def __init__(self):
            self.send_calls = 0
            self.retry_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **kwargs):
            kwargs["candidate_validator"](response)
            return response

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("accepted request must not be sent again")

        async def retry_generation(self, *_args, **_kwargs):
            self.retry_calls += 1
            raise AssertionError("accepted response must not trigger Retry or Regenerate")

    client = Client()
    acquired = AcquiredRole(client, receipt.binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    completed = recovered["hops"][0]
    child = _active_hop(recovered)
    record = RequestLedger(completed["ledger_path"]).get(original["request_id"])

    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "consume_response"
    assert control["result"]["postcondition"] == "hop_advanced"
    assert completed["response"] == response.text
    assert completed.get("validation_error") is None
    assert completed["state"] == "routed"
    assert completed["report_path"] == handoff
    assert completed["report_sha256"] is None
    assert completed["report_size"] is None
    assert child["kind"] == "handoff"
    assert child["target_role"] == "TEST"
    assert child["parent_hop_id"] == completed["hop_id"]
    assert child["handoff"] == handoff
    assert client.send_calls == 0
    assert client.retry_calls == 0
    assert record is not None
    assert record.status is RequestStatus.COMPLETED
    assert record.attempts == 1
    assert completed["request_id"] == original["request_id"]
    assert completed["receipt"] == original["receipt"]
    assert completed["conversation_url"] == original["conversation_url"]
    assert completed["receipt"]["binding"]["page_id"] == original["page_id"]
    assert completed["physical_role"] == original["physical_role"]
    assert completed["receipt"]["user_message_id"] == original["user_message_id"]
    assert completed["receipt"]["user_turn_id"] == original["user_turn_id"]
    assert recovered["task_id"] == original["task_id"]
    assert recovered["team"] == original["team"]


def test_resume_does_not_block_send_preparation_before_durable_boundary(tmp_path: Path):
    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-resume-pre-durable-send"
    )
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert hop["state"] == "sending"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]) is None

    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="operator_recovery",
        reason="resume arrived between pre_send and durable send",
    )

    applied = asyncio.run(worker._apply_control(state, FakeActions(), path))
    assert applied is True
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_control(state, control, FakeActions()))

    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert _active_hop(state)["state"] == "sending"
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "await_durable_send"
    assert control["result"]["postcondition"] == "send_not_started"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]) is None


def test_resume_failed_attachment_upload_reuses_same_request_and_owned_page(tmp_path: Path):
    config, store, state, worker = setup_task(
        tmp_path, task_id="task-resume-failed-upload"
    )
    attachment = tmp_path / "resume-context.txt"
    attachment.write_text("stable attachment", encoding="utf-8")
    identities = collect_file_identities([attachment])
    state["attachments"] = [item.to_dict() for item in identities]
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))

    class FailingOnceUploadClient(RecordingCDPASendClient):
        def __init__(self):
            super().__init__(task_id=state["task_id"], team=state["team"])
            self.binding = PageBinding(
                self.binding.page_id, state["roles"]["PLAN"]["physical_role"]
            )
            self.current = replace(self.current, page_role=self.binding.role)
            self.upload_calls = 0

        async def upload_files(self, paths, *, request_marker, **_options):
            self.upload_calls += 1
            if self.upload_calls == 1:
                raise RuntimeError("synthetic upload failure before readiness")
            uploaded = collect_file_identities(paths)
            self.current = replace(
                self.current,
                attachment_markers=tuple(item.name for item in uploaded),
                state=ChatGPTState.DRAFT,
            )
            return UploadReceipt(
                request_marker=request_marker,
                method="input",
                files=uploaded,
                attachment_count=len(uploaded),
                ownership_token="resume-owned-attachment",
            )

    client = FailingOnceUploadClient()
    actions = RecordingCDPASendActions(client)
    worker._record_acquired(
        state,
        "PLAN",
        AcquiredRole(
            client=client,
            page_id=client.binding.page_id,
            url=client.current.url,
            created=False,
            new_chat=False,
        ),
    )
    original_hop_id = hop["hop_id"]
    original_request_id = hop["request_id"]

    asyncio.run(worker._sending(state, hop, actions))

    ledger = RequestLedger(hop["ledger_path"])
    failed = ledger.get(original_request_id)
    assert failed is not None
    assert failed.status is RequestStatus.UPLOADING
    assert failed.attempts == 0
    assert failed.binding is None
    assert failed.baseline is None
    assert failed.receipt is None
    assert failed.accepted_at is None
    assert failed.upload_receipt is None
    assert client.upload_calls == 1
    assert client.send_calls == []
    assert state["block_code"] == "attachment_upload_failed"

    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="attachment_upload_failed",
        reason="retry same pre-acceptance upload",
    )
    hop = _active_hop(state)
    control = state["controls"][-1]
    fresh_worker = worker_module.CDPAWorker(config, store=store)

    asyncio.run(fresh_worker._recover_resume_sending(state, hop, control, actions))

    authorized = ledger.get(original_request_id)
    assert authorized is not None
    assert authorized.error == (
        f"{worker_module.DurableSendBlock.UPLOAD_RETRY_AUTHORIZATION}:"
        f"{control['control_id']}"
    )
    assert control["status"] == "applied", control["result"]["reason"]
    assert control["result"]["action"] == "continue_failed_upload"
    assert control["result"]["postcondition"] == "same_request_upload_recovery_ready"
    assert state["active_action"] == "send"
    assert state["block_code"] is None
    assert hop["hop_id"] == original_hop_id
    assert hop["request_id"] == original_request_id
    assert client.upload_calls == 1
    assert client.send_calls == []

    repeated = {"role": "PLAN", "result": {"before": {}}}
    asyncio.run(fresh_worker._recover_resume_sending(state, hop, repeated, actions))
    repeated_record = ledger.get(original_request_id)
    assert repeated["status"] == "applied"
    assert repeated_record is not None
    assert repeated_record.error == authorized.error
    assert client.upload_calls == 1
    assert client.send_calls == []

    asyncio.run(fresh_worker._sending(state, hop, actions))

    sent = ledger.get(original_request_id)
    assert sent is not None
    assert sent.request_id == original_request_id
    assert sent.status is RequestStatus.SENT
    assert sent.attempts == 1
    assert hop["hop_id"] == original_hop_id
    assert hop["request_id"] == original_request_id
    assert client.upload_calls == 2
    assert len(client.send_calls) == 1


def test_resume_interrupted_upload_without_failure_evidence_stays_fail_closed(tmp_path: Path):
    config, store, state, worker = setup_task(
        tmp_path, task_id="task-resume-interrupted-upload"
    )
    attachment = tmp_path / "resume-context.txt"
    attachment.write_text("stable attachment", encoding="utf-8")
    identities = collect_file_identities([attachment])
    state["attachments"] = [item.to_dict() for item in identities]
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    constructor = worker_module.task_workflow_definitions(state, config)["PLAN"][
        "system_prompt"
    ]
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(str(constructor)),
        files=identities,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    record = ledger.update(record.request_id, status=RequestStatus.PROMPT_SET)
    ledger.update(record.request_id, status=RequestStatus.UPLOADING, error=None)

    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    client.binding = PageBinding(client.binding.page_id, hop["physical_role"])
    client.current = replace(
        client.current,
        page_role=client.binding.role,
        composer_text=record.rendered_prompt,
        state=ChatGPTState.DRAFT,
    )
    actions = RecordingCDPASendActions(client)
    worker._record_acquired(
        state,
        "PLAN",
        AcquiredRole(client, client.binding.page_id, client.current.url, False, False),
    )
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="attachment_upload_failed",
        reason="worker died during upload before outcome was durable",
    )
    hop = _active_hop(state)
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(record.request_id)
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "attachment_upload_outcome_ambiguous"
    assert state["status"] == "BLOCKED"
    assert current is not None and current.error is None
    assert client.send_calls == []


def test_sending_boundary_classifier_uses_durable_evidence_not_hop_label(tmp_path: Path):
    _config, _store, state, worker = setup_task(
        tmp_path, task_id="task-sending-boundary-evidence"
    )
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert hop["state"] == "sending"
    assert worker._sending_hop_durable_boundary_state(hop) == "preboundary"

    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    assert worker._sending_hop_durable_boundary_state(hop) == "preboundary"

    ledger.update(
        record.request_id,
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("page-boundary", hop["physical_role"]),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        session_id_before="boundary-session",
    )
    assert worker._sending_hop_durable_boundary_state(hop) == "crossed"


def _prepare_pristine_new_sending_record(tmp_path: Path, *, task_id: str):
    _config, store, state, worker = setup_task(tmp_path, task_id=task_id)
    path = Path(state["manifest_path"])
    donor = {
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "assistant_message_id": "22222222-2222-4222-8222-222222222222",
    }
    role = state["roles"]["PLAN"]
    role.update(
        context_source="bootstrap_donor",
        bootstrap_source_donor=dict(donor),
        conversation_generation=1,
        page_id="WEB:presend-live",
        page_url="https://chatgpt.com/c/WEB:presend-live",
        online=True,
        status="active",
    )
    state["bootstrap"] = {
        "bootstrap_id": "general-team-bootstrap",
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "source_conversation_id": donor["conversation_id"],
        "prewarm_prompt": None,
        "max_backups": 7,
        "donors": [dict(donor)],
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
    }
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, FakeActions()))
    assert hop["state"] == "sending"
    constructor = worker_module.task_workflow_definitions(state, worker.config)["PLAN"][
        "system_prompt"
    ]
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": state["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(str(constructor)),
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    role.update(
        page_id="WEB:presend-lost",
        page_url="https://chatgpt.com/c/WEB:presend-lost",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="role_offline",
        reason="temporary WEB role disappeared before durable SENDING",
    )
    return store, state, worker, path, _active_hop(state), ledger, donor


def _prepare_legacy_donorless_pristine_new(tmp_path: Path, *, task_id: str):
    store, state, worker, path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id=task_id
    )
    BootstrapCatalog(worker.config.repository_root).upsert(dict(state["bootstrap"]))
    role = state["roles"]["PLAN"]
    role["context_source"] = None
    role["bootstrap_source_donor"] = None
    role["conversation_generation"] = 0
    state["bootstrap"] = None
    return store, state, worker, path, hop, ledger, donor


def test_resume_legacy_donorless_pristine_new_uses_one_current_default_bootstrap_donor(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id="task-resume-legacy-donorless"
    )
    original = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": hop["hop_id"],
        "request_id": hop["request_id"],
        "turn": hop["turn"],
        "prompt": hop["prompt"],
        "prompt_sha256": hop["prompt_sha256"],
        "generation": state["roles"]["PLAN"]["conversation_generation"],
    }

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            self.backend_calls.append(conversation_id)
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls.append(True)
            raise AssertionError("donorless reacquisition must not open a native branch first")

    ui_calls = []

    async def one_ui_branch(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id="WEB:legacy-replacement",
            url="https://chatgpt.com/c/WEB:legacy-replacement",
            created=True,
            new_chat=True,
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", one_ui_branch)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(original["request_id"])
    role = state["roles"]["PLAN"]
    assert actions.backend_calls == [donor["conversation_id"]]
    assert actions.branch_calls == []
    assert ui_calls == [("PLAN", donor)]
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert state["active_action"] == "send"
    assert role["page_id"] == "WEB:legacy-replacement"
    assert role["page_id"] != "WEB:presend-lost"
    assert role["conversation_generation"] == original["generation"] == 0
    assert role["context_source"] is None
    assert role["bootstrap_source_donor"] is None
    assert state["bootstrap"] is None
    assert state["task_id"] == original["task_id"]
    assert state["team"] == original["team"]
    assert hop["hop_id"] == original["hop_id"]
    assert hop["request_id"] == original["request_id"]
    assert hop["turn"] == original["turn"]
    assert hop["prompt"] == original["prompt"]
    assert hop["prompt_sha256"] == original["prompt_sha256"]
    assert current is not None
    assert current.status is RequestStatus.NEW
    assert current.attempts == 0
    assert current.binding is None
    assert current.baseline is None
    assert current.receipt is None
    assert current.accepted_at is None


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (ManualInputPendingError("manual bootstrap composer"), "manual_composer_conflict"),
        (RateLimitBlockedError("bootstrap rate limit"), "rate_limit_cooldown"),
    ],
)
def test_resume_legacy_donorless_stops_on_bootstrap_safety_signal(
    tmp_path: Path, monkeypatch, error, reason_code
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id=f"task-resume-{reason_code}"
    )

    class Actions:
        def __init__(self):
            self.branch_calls = 0
            self.locate_calls = 0

        async def locate_owned(self, _state, _role):
            self.locate_calls += 1
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == donor["conversation_id"]
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("donorless safety path must not open a native branch first")

    ui_calls = []

    async def safety_ui(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        raise error

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", safety_ui)
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.branch_calls == 0
    assert ui_calls == [("PLAN", donor)]
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == reason_code
    assert state["block_code"] == reason_code
    assert current is not None
    assert current.status is RequestStatus.NEW
    assert current.attempts == 0
    if reason_code == "rate_limit_cooldown":
        assert state["block_retryable"] is True
        assert worker._rate_limit_cooldown["state"] == "active"


@pytest.mark.parametrize(
    ("context_source", "generation"),
    [("task_reuse", 0), (None, 1)],
)
def test_resume_donorless_fallback_rejects_nonlegacy_role_context(
    tmp_path: Path, context_source, generation
):
    _store, state, worker, _path, hop, ledger, _donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id=f"task-resume-nonlegacy-{generation}"
    )
    role = state["roles"]["PLAN"]
    role["context_source"] = context_source
    role["conversation_generation"] = generation

    class Actions:
        def __init__(self):
            self.backend_calls = 0
            self.branch_calls = 0

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, _conversation_id):
            self.backend_calls += 1
            raise AssertionError("non-legacy role context must not use current bootstrap")

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("non-legacy role context must not branch")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.backend_calls == 0
    assert actions.branch_calls == 0
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0


def test_resume_legacy_donorless_failure_does_not_try_second_donor_or_bootstrap_retry(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id="task-resume-donorless-bounded"
    )
    second = {
        "conversation_id": "33333333-3333-4333-8333-333333333333",
        "assistant_message_id": "44444444-4444-4444-8444-444444444444",
    }
    current = BootstrapCatalog(worker.config.repository_root).get("general-team-bootstrap")
    assert current is not None
    BootstrapCatalog(worker.config.repository_root).upsert({**current, "donors": [donor, second]})

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            self.backend_calls.append(conversation_id)
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls.append(True)
            raise AssertionError("donorless bounded path must not open a native branch first")

    ui_calls = []

    async def failed_ui(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        raise worker_module.BootstrapUIBranchError("UI branch unavailable")

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", failed_ui)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.backend_calls == [donor["conversation_id"]]
    assert actions.branch_calls == []
    assert ui_calls == [("PLAN", donor)]
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert state["active_action"] != "bootstrap_retry"
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0


def _real_ui_fallback_actions(
    worker,
    state,
    donor,
    monkeypatch,
    *,
    draft: str,
    rehydrate: bool = False,
):
    clear_calls = []

    class BranchResponse:
        url = "https://chatgpt.com/backend-api/conversation/new_branch"
        status = 200

        async def json(self):
            return {
                "conversation": {
                    "conversation_id": "77777777-7777-4777-8777-777777777777"
                }
            }

    class Button:
        def __init__(self, page, label):
            self.page = page
            self.label = label

        async def click(self):
            if self.label == "Branch in new chat":
                self.page.url = "https://chatgpt.com/c/WEB:resume-ui-fallback"
                for listener in tuple(self.page.listeners.get("response", ())):
                    listener(BranchResponse())

    class Turn:
        def __init__(self, page):
            self.page = page

        async def hover(self):
            return None

        def get_by_role(self, _role, *, name, exact):
            assert exact is True
            return Button(self.page, name)

    class Assistant:
        def __init__(self, page):
            self.page = page
            self.first = self

        async def wait_for(self, **_kwargs):
            return None

        def locator(self, _selector):
            return Turn(self.page)

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.closed = False
            self.snapshot_value = None
            self.listeners = {}

        async def goto(self, url, **_kwargs):
            self.url = url

        def locator(self, _selector):
            return Assistant(self)

        def get_by_role(self, _role, *, name, exact):
            assert exact is True
            return Button(self, name)

        def on(self, event, listener):
            self.listeners.setdefault(event, []).append(listener)

        def remove_listener(self, event, listener):
            listeners = self.listeners.get(event, [])
            if listener in listeners:
                listeners.remove(listener)

        async def wait_for_url(self, predicate, **_kwargs):
            if not predicate(self.url):
                raise TimeoutError("UI branch URL predicate not satisfied")

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    class Context:
        def __init__(self):
            self.pages = []
            self.new_page_calls = 0

        async def new_page(self):
            self.new_page_calls += 1
            self.pages.append(page)
            return page

    context = Context()

    class Client:
        def __init__(self):
            self.page = page
            self.binding = PageBinding("ui-page", state["roles"]["PLAN"]["physical_role"])
            self.snapshot_value = SimpleNamespace(
                page_id="ui-page",
                page_role=state["roles"]["PLAN"]["physical_role"],
                page_task_id=None,
                page_team=None,
                composer_text=draft,
                composer_present=True,
                composer_editable=True,
                stop_visible=False,
                blocking_dialogs=(),
                attachment_markers=(),
                state=ChatGPTState.NEW_CHAT,
                requires_login=False,
                url=page.url,
            )
            page.snapshot_value = self.snapshot_value

        async def assert_ownership(self):
            self.snapshot_value.url = page.url
            return self.snapshot_value

        async def wait_until_clean_ready(self, *, timeout_ms):
            del timeout_ms
            if self.snapshot_value.composer_text.strip() or self.snapshot_value.attachment_markers:
                raise ManualInputPendingError(
                    "composer still contains manual text or attachments; automated mutation blocked"
                )
            return self.snapshot_value

        async def bind_task_identity(self, task_id, team):
            self.snapshot_value.page_task_id = task_id
            self.snapshot_value.page_team = team

    class Workspace:
        async def bind(self, _role, bound_page, **_kwargs):
            assert bound_page is page
            return Client()

    monkeypatch.setattr(worker_module, "ChatGPTWorkspace", Workspace)

    async def clear_once(target, **_kwargs):
        clear_calls.append(target)
        target.snapshot_value.composer_text = ""
        if rehydrate:
            asyncio.get_running_loop().call_later(
                0.05,
                setattr,
                target.snapshot_value,
                "composer_text",
                draft,
            )

    monkeypatch.setattr(actions_module, "clear_composer", clear_once)

    class Actions(CDPATabActions):
        def __init__(self):
            super().__init__(context, worker.config)
            self.branch_calls = []
            self.backend_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            self.backend_calls.append(conversation_id)
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            raise BranchBootstrapError("native branch unavailable")

    return Actions(), context, page, clear_calls


def test_resume_generic_native_failure_ui_fallback_clears_fresh_stale_composer(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-ui-stale-composer"
    )
    actions, context, page, clear_calls = _real_ui_fallback_actions(
        worker,
        state,
        donor,
        monkeypatch,
        draft="stale UI bootstrap draft",
    )
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert context.new_page_calls == 1
    assert clear_calls == [page]
    assert page.closed is False
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert state["active_action"] == "send"
    assert state["block_code"] is None
    assert state["roles"]["PLAN"]["page_id"] == "ui-page"
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0


def test_resume_ui_fallback_rehydration_closes_once_and_blocks_without_fanout(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-ui-rehydrate"
    )
    actions, context, page, clear_calls = _real_ui_fallback_actions(
        worker,
        state,
        donor,
        monkeypatch,
        draft="stale UI bootstrap draft",
        rehydrate=True,
    )
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert actions.backend_calls == []
    assert context.new_page_calls == 1
    assert clear_calls == [page]
    assert page.closed is True
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "manual_composer_conflict"
    assert state["block_code"] == "manual_composer_conflict"
    assert state["active_action"] == "blocked"
    assert state["roles"]["PLAN"]["page_id"] == "WEB:presend-lost"
    assert state["active_action"] != "bootstrap_retry"
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None


def test_resume_unresolved_native_branch_stops_without_ui_fallback(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-unresolved-native"
    )
    unresolved_type = getattr(
        worker_module, "BranchTargetUnresolvedError", BranchBootstrapError
    )

    class Actions:
        def __init__(self):
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == donor["conversation_id"]
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            raise unresolved_type("branch target remained provisional WEB identity")

    ui_calls = []

    async def forbidden_ui(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        return AcquiredRole(
            client=SimpleNamespace(),
            page_id="unexpected-ui-page",
            url="https://chatgpt.com/c/unexpected-ui",
            created=True,
            new_chat=True,
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", forbidden_ui)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert ui_calls == []
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "branch_target_unresolved"
    assert state["block_code"] == "branch_target_unresolved"
    assert state["active_action"] == "blocked"
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None


def test_resume_reacquires_lost_bootstrap_role_for_pristine_new_request(tmp_path: Path):
    store, state, worker, path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-pristine-new"
    )
    original = {
        "task_id": state["task_id"],
        "team": state["team"],
        "hop_id": hop["hop_id"],
        "request_id": hop["request_id"],
        "turn": hop["turn"],
        "prompt": hop["prompt"],
        "prompt_sha256": hop["prompt_sha256"],
        "generation": state["roles"]["PLAN"]["conversation_generation"],
        "donor": dict(state["roles"]["PLAN"]["bootstrap_source_donor"]),
    }

    class Actions:
        def __init__(self):
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            return AcquiredRole(
                client=SimpleNamespace(),
                page_id="WEB:presend-replacement",
                url="https://chatgpt.com/c/WEB:presend-replacement",
                created=True,
                new_chat=True,
            )

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(original["request_id"])
    role = state["roles"]["PLAN"]
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "reacquire_preboundary_role"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert role["page_id"] == "WEB:presend-replacement"
    assert role["page_url"] == "https://chatgpt.com/c/WEB:presend-replacement"
    assert role["online"] is True
    assert role["conversation_generation"] == original["generation"]
    assert role["bootstrap_source_donor"] == original["donor"]
    assert state["task_id"] == original["task_id"]
    assert state["team"] == original["team"]
    assert hop["hop_id"] == original["hop_id"]
    assert hop["request_id"] == original["request_id"]
    assert hop["turn"] == original["turn"]
    assert hop["prompt"] == original["prompt"]
    assert hop["prompt_sha256"] == original["prompt_sha256"]
    assert current is not None
    assert current.status is RequestStatus.NEW
    assert current.attempts == 0
    assert current.binding is None
    assert current.baseline is None
    assert current.receipt is None
    assert current.accepted_at is None



def test_reacquired_pristine_request_crosses_send_boundary_exactly_once(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id="task-resume-pristine-send-once"
    )

    class Client:
        def __init__(self):
            self.binding = PageBinding("WEB:presend-replacement", "alpha-plan")
            self.send_calls = 0
            self.current = SimpleNamespace(
                state=ChatGPTState.NEW_CHAT,
                page_id=self.binding.page_id,
                page_role=self.binding.role,
                page_task_id=state["task_id"],
                page_team=state["team"],
                composer_text="",
                composer_empty=True,
                manual_input_pending=False,
                attachment_markers=(),
                send_enabled=False,
                stop_visible=False,
                retry_visible=False,
                blocking_dialogs=(),
                messages=(),
                session_id="presend-session",
                conversation_url="https://chatgpt.com/c/WEB:presend-replacement",
                url="https://chatgpt.com/c/WEB:presend-replacement",
            )

        async def assert_ownership(self):
            return self.current

        async def set_text(self, text):
            self.current.composer_text = text
            self.current.composer_empty = False
            self.current.send_enabled = True
            self.current.state = ChatGPTState.DRAFT

        async def send(self, text, **kwargs):
            self.send_calls += 1
            assert kwargs["expected_task_id"] == state["task_id"]
            assert kwargs["expected_team"] == state["team"]
            baseline = capture_message_baseline(self.current.messages)
            self.current.messages = (
                MessageSnapshot("user", "u-pristine", "t-pristine", text, ()),
            )
            self.current.composer_text = ""
            self.current.composer_empty = True
            self.current.send_enabled = False
            self.current.state = ChatGPTState.SUBMITTING
            return SendReceipt(
                prompt=text,
                prompt_sha256=worker_module._sha(text),
                binding=self.binding,
                baseline=baseline,
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="presend-session",
                user_message_id="u-pristine",
                user_turn_id="t-pristine",
            )

    client = Client()
    acquired = AcquiredRole(
        client=client,
        page_id=client.binding.page_id,
        url=client.current.url,
        created=True,
        new_chat=True,
    )

    class Actions:
        def __init__(self):
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            role = state["roles"]["PLAN"]
            if role.get("page_id") == client.binding.page_id and role.get("online") is True:
                return AcquiredRole(client, client.binding.page_id, client.current.url, False, False)
            return None

        async def backend_conversation(self, conversation_id):
            assert conversation_id == donor["conversation_id"]
            return _bootstrap_graph(donor["assistant_message_id"])

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls.append(True)
            raise AssertionError("donorless send-once path must not open a native branch first")

    ui_calls = []

    async def one_ui_branch(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        return acquired

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", one_ui_branch)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    before_send = ledger.get(hop["request_id"])
    assert before_send is not None
    assert before_send.status is RequestStatus.NEW
    assert before_send.attempts == 0

    asyncio.run(worker._sending(state, hop, actions))

    after_send = ledger.get(hop["request_id"])
    assert actions.branch_calls == []
    assert ui_calls == [("PLAN", donor)]
    assert client.send_calls == 1
    assert after_send is not None
    assert after_send.status is RequestStatus.SENT
    assert after_send.attempts == 1
    assert hop["state"] == "sent"
    assert hop["request_id"] == before_send.request_id
    assert state["roles"]["PLAN"]["conversation_generation"] == 0



def test_pristine_preboundary_recovery_survives_worker_restart(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-pristine-restart"
    )
    original_request_id = hop["request_id"]
    original_generation = state["roles"]["PLAN"]["conversation_generation"]

    class Actions:
        def __init__(self):
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            return AcquiredRole(
                SimpleNamespace(),
                "WEB:restart-replacement",
                "https://chatgpt.com/c/WEB:restart-replacement",
                True,
                True,
            )

    actions = Actions()
    fresh_worker = worker_module.CDPAWorker(worker.config, store=store)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    recovered = asyncio.run(fresh_worker.advance(path, SimpleNamespace(pages=[])))

    recovered_hop = _active_hop(recovered)
    record = ledger.get(original_request_id)
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert recovered["controls"][-1]["status"] == "applied"
    assert recovered["controls"][-1]["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert recovered_hop["request_id"] == original_request_id
    assert recovered_hop["state"] == "sending"
    assert recovered["roles"]["PLAN"]["conversation_generation"] == original_generation
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0


def test_preboundary_classifier_rejects_nonpristine_and_crossed_records(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, _donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-preboundary-negative"
    )
    pristine = ledger.get(hop["request_id"])
    assert pristine is not None
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is True
    assert worker._is_pristine_preboundary_sending_record(
        state, hop, replace(pristine, attempts=1)
    ) is False
    assert worker._is_pristine_preboundary_sending_record(
        state,
        hop,
        replace(pristine, binding=PageBinding("WEB:unexpected", hop["physical_role"])),
    ) is False
    assert worker._is_pristine_preboundary_sending_record(
        state, hop, replace(pristine, idempotency_key="0" * 64)
    ) is False
    original_mode = state.get("task_mode")
    state["task_mode"] = "independent"
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is False
    if original_mode is None:
        state.pop("task_mode", None)
    else:
        state["task_mode"] = original_mode
    original_prompt_hash = hop["prompt_sha256"]
    hop["prompt_sha256"] = "0" * 64
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is False
    hop["prompt_sha256"] = original_prompt_hash
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is True

    malformed_path = tmp_path / "malformed-ledger.json"
    malformed_ledger = RequestLedger(malformed_path)
    malformed = malformed_ledger.begin(
        role=hop["physical_role"],
        prompt=hop["prompt"],
        source_context={
            "task_id": state["task_id"],
            "team": state["team"],
            "hop_id": hop["hop_id"],
            "manifest": "wrong-manifest",
        },
        role_prompt_hash=pristine.role_prompt_hash,
        request_id=hop["request_id"],
        render_request_marker=False,
    )
    assert worker._is_pristine_preboundary_sending_record(state, hop, malformed) is False

    state["attachments"] = [
        {
            "path": "/tmp/unexpected.txt",
            "name": "unexpected.txt",
            "size": 1,
            "sha256": "a" * 64,
            "mime_type": "text/plain",
        }
    ]
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is False
    state["attachments"] = []
    assert worker._is_pristine_preboundary_sending_record(state, hop, pristine) is True

    ledger.update(
        hop["request_id"],
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("WEB:bound", hop["physical_role"]),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        session_id_before="session",
    )
    sending = ledger.get(hop["request_id"])
    assert sending is not None
    assert worker._is_pristine_preboundary_sending_record(state, hop, sending) is False

    ledger.update(
        hop["request_id"],
        status=RequestStatus.SENT,
        accepted_at=1.0,
        receipt=SendReceipt(
            prompt=sending.rendered_prompt,
            prompt_sha256=worker_module._sha(sending.rendered_prompt),
            binding=sending.binding,
            baseline=sending.baseline,
            attempts=1,
            accepted_via="user_message_identity",
            session_id_before="session",
            user_message_id="u-negative",
            user_turn_id="t-negative",
        ).to_dict(),
    )
    sent = ledger.get(hop["request_id"])
    assert sent is not None
    assert worker._is_pristine_preboundary_sending_record(state, hop, sent) is False

    ledger.update(
        hop["request_id"],
        status=RequestStatus.COMPLETED,
        response=MessageSnapshot(
            "assistant", "a-negative", "ta-negative", "done", ()
        ).to_dict(),
    )
    completed = ledger.get(hop["request_id"])
    assert completed is not None
    assert worker._is_pristine_preboundary_sending_record(state, hop, completed) is False


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


def _prepare_lost_sending_record(tmp_path: Path, *, task_id: str, error: str | None):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id=task_id
    )
    donor = {
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "assistant_message_id": "22222222-2222-4222-8222-222222222222",
    }
    role = state["roles"]["PLAN"]
    state["bootstrap"] = {
        "bootstrap_id": "general-team-bootstrap",
        "name": "General Team Bootstrap",
        "description": "Reusable task-neutral context",
        "source_conversation_id": donor["conversation_id"],
        "prewarm_prompt": None,
        "max_backups": 7,
        "donors": [dict(donor)],
        "enabled": True,
        "tags": ["general"],
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:00+00:00",
    }
    role.update(
        context_source="bootstrap_donor",
        bootstrap_source_donor=dict(donor),
        conversation_generation=1,
        page_url="https://chatgpt.com/c/WEB:lost-sending",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    hop["conversation_url"] = "https://chatgpt.com/c/WEB:lost-sending"
    ledger.update(
        hop["request_id"],
        session_id_before="WEB:lost-sending",
        error=error,
    )
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="durable_send_ambiguous",
        reason="SENDING persisted after provisional page disappeared",
    )
    return store, state, worker, path, _active_hop(state), ledger, donor, binding, baseline


def _backend_long_user_graph(user_message_id: str, prompt: str):
    def node(message_id, role, text, *, parent=None, children=()):
        return {
            "id": message_id,
            "message": {
                "id": message_id,
                "author": {"role": role},
                "recipient": "all",
                "content": {"content_type": "text", "parts": [text]},
            },
            "parent": parent,
            "children": list(children),
        }

    return {
        "current_node": user_message_id,
        "mapping": {
            "u-old": node("u-old", "user", "historical", children=("a-old",)),
            "a-old": node("a-old", "assistant", "old answer", parent="u-old", children=("u-base",)),
            "u-base": node("u-base", "user", "recent", parent="a-old", children=("a-base",)),
            "a-base": node("a-base", "assistant", "recent answer", parent="u-base", children=(user_message_id,)),
            user_message_id: node(user_message_id, "user", prompt, parent="a-base"),
        },
    }


def test_lost_sending_page_without_positive_proof_stays_ambiguous_without_tab_churn(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, _donor, binding, _baseline = (
        _prepare_lost_sending_record(
            tmp_path,
            task_id="task-resume-lost-sending-ambiguous",
            error="SendRecoveryError: transport outcome unknown",
        )
    )

    class Actions:
        def __init__(self):
            self.locate_calls = 0
            self.reopen_calls = 0
            self.branch_calls = 0
            self.backend_calls = 0

        async def locate_owned(self, _state, _role):
            self.locate_calls += 1
            return None

        async def reopen(self, *_args, **_kwargs):
            self.reopen_calls += 1
            raise AssertionError("ambiguous SENDING must not reopen or recreate a tab")

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("ambiguous SENDING must not branch a replacement tab")

        async def backend_conversation(self, *_args, **_kwargs):
            self.backend_calls += 1
            raise AssertionError("provisional WEB identity has no canonical backend target")

    actions = Actions()
    first_control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, first_control, actions))
    second_control = {"role": "PLAN", "result": {"before": {}}}
    asyncio.run(worker._recover_resume_sending(state, hop, second_control, actions))

    current = ledger.get(hop["request_id"])
    assert first_control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert second_control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert actions.locate_calls == 2
    assert actions.reopen_calls == 0
    assert actions.branch_calls == 0
    assert actions.backend_calls == 0
    assert state["roles"]["PLAN"]["page_id"] == binding.page_id
    assert current is not None
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None


def test_lost_sending_backend_proves_acceptance_before_page_acquisition(tmp_path: Path):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-backend-accepted"
    )
    conversation_id = "33333333-3333-4333-8333-333333333333"
    accepted_user = "44444444-4444-4444-8444-444444444444"
    baseline = MessageBaseline(
        frozenset({"u-base", "a-base"}),
        frozenset(),
        frozenset({"a-base"}),
        frozenset({"u-base"}),
    )
    ledger.update(
        hop["request_id"],
        baseline=baseline,
        session_id_before=conversation_id,
        error="SendRecoveryError: acceptance observation was interrupted",
    )
    state["roles"]["PLAN"].update(
        page_url=f"https://chatgpt.com/c/{conversation_id}",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    hop["conversation_url"] = f"https://chatgpt.com/c/{conversation_id}"
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="durable_send_ambiguous",
        reason="lost page after SENDING",
    )
    hop = _active_hop(state)

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.locate_calls = 0

        async def backend_conversation(self, observed_conversation_id):
            self.backend_calls.append(observed_conversation_id)
            return _backend_long_user_graph(accepted_user, hop["prompt"])

        async def locate_owned(self, *_args, **_kwargs):
            self.locate_calls += 1
            raise AssertionError("backend acceptance must be consumed before page acquisition")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.backend_calls == [conversation_id]
    assert actions.locate_calls == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "observe_progress"
    assert current is not None
    assert current.status is RequestStatus.SENT
    assert current.attempts == 1
    receipt = SendReceipt.from_dict(current.receipt)
    assert receipt.binding == binding
    assert receipt.baseline == baseline
    assert receipt.user_message_id == accepted_user
    assert receipt.conversation_id == conversation_id
    assert hop["state"] == "waiting"


def test_lost_sending_atomic_nonacceptance_rebinds_same_request_and_sends_once(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, donor, old_binding, _old_baseline = (
        _prepare_lost_sending_record(
            tmp_path,
            task_id="task-resume-lost-sending-proven-nonacceptance",
            error=(
                "ComposerConflictError: composer changed or became unavailable "
                "inside the atomic send boundary"
            ),
        )
    )
    original = (state["task_id"], hop["hop_id"], hop["request_id"], hop["turn"])
    replacement_binding = PageBinding(
        "replacement-page-id", state["roles"]["PLAN"]["physical_role"]
    )
    replacement_snapshot = SimpleNamespace(
        page_id=replacement_binding.page_id,
        page_role=replacement_binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(),
        session_id="55555555-5555-4555-8555-555555555555",
        url="https://chatgpt.com/c/55555555-5555-4555-8555-555555555555",
        conversation_url="https://chatgpt.com/c/55555555-5555-4555-8555-555555555555",
    )
    replacement_baseline = capture_message_baseline(replacement_snapshot.messages)

    class Client:
        def __init__(self):
            self.binding = replacement_binding
            self.send_calls = 0

        async def assert_ownership(self):
            return replacement_snapshot

        async def send(self, prompt, **kwargs):
            self.send_calls += 1
            during = ledger.get(hop["request_id"])
            assert during is not None
            assert during.status is RequestStatus.SENDING
            assert during.attempts == 1
            assert during.binding == replacement_binding
            assert during.baseline == replacement_baseline
            assert "bounded continuation" in str(during.error).lower()
            assert prompt == hop["prompt"]
            assert kwargs["max_attempts"] == 1
            assert kwargs["recovery_reload"] is False
            return SendReceipt(
                prompt=prompt,
                prompt_sha256=worker_module._sha(prompt),
                binding=replacement_binding,
                baseline=replacement_baseline,
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before=replacement_snapshot.session_id,
                user_message_id="66666666-6666-4666-8666-666666666666",
                user_turn_id="turn-accepted-once",
                conversation_id=replacement_snapshot.session_id,
            )

    client = Client()

    class Actions:
        def __init__(self):
            self.locate_calls = 0
            self.branch_calls = []

        async def locate_owned(self, _state, _role):
            self.locate_calls += 1
            return None

        async def branch_from_anchor(self, _state, role, **kwargs):
            self.branch_calls.append((role, kwargs))
            return AcquiredRole(
                client=client,
                page_id=replacement_binding.page_id,
                url=replacement_snapshot.url,
                created=True,
                new_chat=True,
            )

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.locate_calls == 1
    assert actions.branch_calls == [
        (
            "PLAN",
            {
                "source_conversation_id": donor["conversation_id"],
                "assistant_message_id": donor["assistant_message_id"],
            },
        )
    ]
    assert client.send_calls == 1
    assert (state["task_id"], hop["hop_id"], hop["request_id"], hop["turn"]) == original
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert state["roles"]["PLAN"]["page_id"] == replacement_binding.page_id
    assert old_binding != replacement_binding
    assert current is not None
    assert current.status is RequestStatus.SENT
    assert current.attempts == 1
    assert current.error is None
    assert current.receipt["user_message_id"] == "66666666-6666-4666-8666-666666666666"
    assert control["result"]["postcondition"] == "draft_accepted_once"


def test_lost_sending_bounded_continuation_failure_is_not_replayed(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor, _old_binding, _old_baseline = (
        _prepare_lost_sending_record(
            tmp_path,
            task_id="task-resume-lost-sending-one-shot",
            error=(
                "ComposerConflictError: composer changed or became unavailable "
                "inside the atomic send boundary"
            ),
        )
    )
    replacement_binding = PageBinding(
        "replacement-one-shot", state["roles"]["PLAN"]["physical_role"]
    )
    snapshot = SimpleNamespace(
        page_id=replacement_binding.page_id,
        page_role=replacement_binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(),
        session_id="99999999-9999-4999-8999-999999999999",
        url="https://chatgpt.com/c/99999999-9999-4999-8999-999999999999",
        conversation_url="https://chatgpt.com/c/99999999-9999-4999-8999-999999999999",
    )

    class Client:
        binding = replacement_binding

        def __init__(self):
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise RateLimitBlockedError("bounded continuation hit rate limit")

    client = Client()

    class Actions:
        def __init__(self):
            self.branch_calls = 0
            self.locate_calls = 0

        async def locate_owned(self, *_args, **_kwargs):
            self.locate_calls += 1
            return None

        async def branch_from_anchor(self, _state, _role, **kwargs):
            self.branch_calls += 1
            assert kwargs == {
                "source_conversation_id": donor["conversation_id"],
                "assistant_message_id": donor["assistant_message_id"],
            }
            return AcquiredRole(client, replacement_binding.page_id, snapshot.url, True, True)

        async def cleanup_rate_limited_chatgpt_pages(self):
            return {"targeted": 1, "closed": 1, "cleared": 0, "errors": []}

    actions = Actions()
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    first_control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, first_control, actions))
    first_record = ledger.get(hop["request_id"])
    assert first_control["result"]["reason_code"] == "rate_limit_cooldown"
    assert worker._rate_limit_cooldown["state"] == "active"
    assert first_record is not None
    assert first_record.status is RequestStatus.SENDING
    assert first_record.attempts == 1
    assert str(first_record.error).startswith("bounded continuation started")

    worker._rate_limit_cooldown = None
    second_control = {"role": "PLAN", "result": {"before": {}}}
    asyncio.run(worker._recover_resume_sending(state, hop, second_control, actions))
    second_record = ledger.get(hop["request_id"])
    assert second_control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert actions.branch_calls == 1
    assert client.send_calls == 1
    assert actions.locate_calls == 2
    assert second_record is not None
    assert second_record.attempts == 1
    assert second_record.receipt is None


def test_crossed_durable_receipt_blocks_nonacceptance_replacement(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, _donor, _binding, baseline = (
        _prepare_lost_sending_record(
            tmp_path,
            task_id="task-resume-crossed-durable-receipt",
            error=(
                "ComposerConflictError: composer changed or became unavailable "
                "inside the atomic send boundary"
            ),
        )
    )
    crossed_binding = PageBinding("foreign-page", state["roles"]["PLAN"]["physical_role"])
    crossed = SendReceipt(
        prompt=hop["prompt"],
        prompt_sha256=hop["prompt_sha256"],
        binding=crossed_binding,
        baseline=baseline,
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="foreign",
        user_message_id="foreign-user",
        user_turn_id="foreign-turn",
    )
    ledger.update(hop["request_id"], receipt=crossed.to_dict())

    class Actions:
        def __init__(self):
            self.branch_calls = 0

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("crossed accepted receipt must never authorize replacement")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    current = ledger.get(hop["request_id"])
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert actions.branch_calls == 0
    assert current is not None
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt == crossed.to_dict()


def test_lost_sending_atomic_nonacceptance_obeys_global_rate_limit_before_acquisition(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, _donor, _binding, _baseline = (
        _prepare_lost_sending_record(
            tmp_path,
            task_id="task-resume-lost-sending-rate-limit",
            error=(
                "ComposerConflictError: composer changed or became unavailable "
                "inside the atomic send boundary"
            ),
        )
    )
    worker._rate_limit_cooldown = {
        "state": "active",
        "release_not_before": "2999-01-01T00:00:00+00:00",
        "post_release_acquisition": "pending",
    }

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            raise AssertionError("active rate limit must block before tab inspection/acquisition")

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise AssertionError("active rate limit must block replacement acquisition")

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    current = ledger.get(hop["request_id"])
    assert control["result"]["reason_code"] == "rate_limit_cooldown"
    assert state["block_code"] == "rate_limit_cooldown"
    assert current is not None
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1


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


def _guard_blocked_state(tmp_path: Path, *, task_id: str):
    _config, store, state, worker = setup_task(tmp_path, task_id=task_id)
    from test_cdpa_worker import _accept_self_route_guard_decision

    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    source = _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["block_code"] == "consecutive_self_route_limit"
    store.save(state["manifest_path"], state)
    return store, state, worker, Path(state["manifest_path"]), source


def test_operator_resume_releases_self_route_guard_once_without_replaying_accepted_evidence(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, source = _guard_blocked_state(
        tmp_path, task_id="task-self-route-operator-resume"
    )
    report_count = len(state["reports"])
    timeline_count = len(state["route_timeline"])
    source_response = source["response"]
    source_report = source["report_path"]
    source_hop_id = source["hop_id"]
    state = store.request_resume(
        path,
        reason="explicit operator release",
        external_command_id="resume-self-route-guard-once",
    )

    def forbidden_responded(*_args, **_kwargs):
        raise AssertionError("guard Resume must not re-run _responded")

    monkeypatch.setattr(worker, "_responded", forbidden_responded)
    released = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = released["controls"][-1]
    child = _active_hop(released)
    assert released["status"] == "RUNNING"
    assert released["block_code"] is None
    assert child["parent_hop_id"] == source_hop_id
    assert child["source_role"] == "PLAN"
    assert child["target_role"] == "PLAN"
    assert child["handoff"] == source_report
    assert child["state"] == "pre_send"
    assert len(released["reports"]) == report_count
    assert len(released["route_timeline"]) == timeline_count
    persisted_source = next(h for h in released["hops"] if h["hop_id"] == source_hop_id)
    assert persisted_source["state"] == "routed"
    assert persisted_source["response"] == source_response
    assert persisted_source["report_path"] == source_report
    assert control["status"] == "applied"
    assert control["result"]["action"] == "release_self_route_guard"
    assert control["result"]["postcondition"] == "child_hop_appended"
    assert RequestLedger(child["ledger_path"]).peek(child["request_id"]) is None

    replay = store.request_resume(
        path,
        reason="duplicate command",
        external_command_id="resume-self-route-guard-once",
    )
    assert len(replay["hops"]) == len(released["hops"])
    assert len(replay["controls"]) == len(released["controls"])
    assert asyncio.run(worker._apply_control(replay, FakeActions(), path)) is False


def test_self_route_guard_rejects_bypass_controls_but_new_chat_does_not_release(tmp_path: Path):
    for action in ("retry", "restart_role", "route_plan"):
        store, state, worker, path, source = _guard_blocked_state(
            tmp_path, task_id=f"task-self-route-bypass-{action}"
        )
        queued = store.request_control(
            path,
            action,
            role="PLAN",
            reason=f"attempt {action}",
            external_command_id=f"guard-bypass-{action}",
        )
        before_hops = len(queued["hops"])
        actions = FakeActions()
        applied = asyncio.run(worker._apply_control(queued, actions, path))
        assert applied is True
        assert queued["controls"][-1]["status"] == "rejected"
        assert queued["status"] == "BLOCKED"
        assert queued["block_code"] == "consecutive_self_route_limit"
        assert queued["active_hop_id"] == source["hop_id"]
        assert len(queued["hops"]) == before_hops
        if action == "restart_role":
            assert actions.restart_roles == []

    store, state, worker, path, source = _guard_blocked_state(
        tmp_path, task_id="task-self-route-new-chat"
    )
    queued = store.request_control(
        path,
        "new_chat",
        role="PLAN",
        reason="operational new chat only",
        external_command_id="guard-new-chat",
    )
    before_hops = len(queued["hops"])
    assert asyncio.run(worker._apply_control(queued, FakeActions(), path)) is True
    assert queued["controls"][-1]["status"] == "applied"
    assert queued["status"] == "BLOCKED"
    assert queued["block_code"] == "consecutive_self_route_limit"
    assert queued["active_hop_id"] == source["hop_id"]
    assert len(queued["hops"]) == before_hops


def test_independent_agent_resume_cannot_release_self_route_guard(tmp_path: Path):
    store, state, worker, path, source = _guard_blocked_state(
        tmp_path, task_id="task-self-route-independent-resume"
    )
    state = store.load(path)
    source = _active_hop(state)
    store._queue_control(
        state,
        "resume",
        role="PLAN",
        reason="automatic recovery must not release semantic guard",
        origin="independent_agent",
        source_task_id="agent-maintainers-g1",
        source_event_key="recovery:self-route-guard",
    )
    state = store.save(path, state)

    applied = asyncio.run(worker._apply_control(state, FakeActions(), path))

    assert applied is True
    assert state["controls"][-1]["status"] == "rejected"
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "consecutive_self_route_limit"
    assert state["active_hop_id"] == source["hop_id"]
    assert len(state["hops"]) == 3


def test_successful_operator_resume_from_paused_rearms_self_route_streak(tmp_path: Path):
    from test_cdpa_worker import _accept_self_route_guard_decision

    _config, store, state, worker = setup_task(tmp_path, task_id="task-self-route-paused-rearm")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    active = _active_hop(state)
    handoff = (
        f".plan/{state['team']}/{active['physical_role']}_turn{active['turn']}_"
        f"{state['task_id']}.md"
    )
    active["response"] = json.dumps({"route": "PLAN", "handoff": handoff})
    active["state"] = "responded"
    state["status"] = "PAUSED"
    state["kanban_column"] = "PAUSED"
    state["resume_column"] = "PLANNING"
    state["pause_reason"] = "operator paused before consuming response"
    store.save(state["manifest_path"], state)
    state = store.request_resume(
        state["manifest_path"],
        reason="operator resumes semantic workflow",
        external_command_id="resume-paused-rearm",
    )
    control = state["controls"][-1]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert control["status"] == "recovering"
    asyncio.run(worker._recover_resume_control(state, control, FakeActions()))

    assert control["status"] == "applied"
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert _active_hop(state)["hop_id"] == active["hop_id"] + 1


def test_operator_resume_from_self_route_guard_rearms_at_new_child_boundary(tmp_path: Path):
    from test_cdpa_worker import _accept_self_route_guard_decision

    store, state, worker, path, _source = _guard_blocked_state(
        tmp_path, task_id="task-self-route-guard-rearm"
    )
    state = store.request_resume(
        path,
        reason="explicit operator rearm",
        external_command_id="resume-self-route-rearm",
    )

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert state["controls"][-1]["status"] == "applied"
    first_child = _active_hop(state)
    assert first_child["hop_id"] == 4

    _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "RUNNING"
    _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "RUNNING"
    third = _accept_self_route_guard_decision(worker, state, "PLAN")

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "consecutive_self_route_limit"
    assert state["active_hop_id"] == third["hop_id"]


def test_unrelated_blocked_resume_does_not_reset_self_route_streak(tmp_path: Path):
    from test_cdpa_worker import _accept_self_route_guard_decision

    _config, store, state, worker = setup_task(
        tmp_path, task_id="task-self-route-unrelated-resume"
    )
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    active = _active_hop(state)
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="operational role recovery",
        active_action="blocked",
    )
    store.save(state["manifest_path"], state)
    state = store.request_resume(
        state["manifest_path"],
        reason="recover unrelated operational block",
        external_command_id="resume-unrelated-block",
    )
    control = state["controls"][-1]

    assert asyncio.run(worker._apply_control(state, FakeActions())) is True
    assert control["status"] == "recovering"
    asyncio.run(worker._recover_resume_control(state, control, FakeActions()))
    assert control["status"] == "applied"
    assert state["block_code"] is None
    assert _active_hop(state)["hop_id"] == active["hop_id"]

    third = _accept_self_route_guard_decision(worker, state, "PLAN")
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "consecutive_self_route_limit"
    assert state["active_hop_id"] == third["hop_id"]


def test_interrupted_paused_resume_does_not_rearm_self_route_streak_after_reload(tmp_path: Path):
    from test_cdpa_worker import _accept_self_route_guard_decision

    config, store, state, worker = setup_task(
        tmp_path, task_id="task-self-route-paused-interrupted"
    )
    _accept_self_route_guard_decision(worker, state, "PLAN")
    _accept_self_route_guard_decision(worker, state, "PLAN")
    active = _active_hop(state)
    handoff = (
        f".plan/{state['team']}/{active['physical_role']}_turn{active['turn']}_"
        f"{state['task_id']}.md"
    )
    active["response"] = json.dumps({"route": "PLAN", "handoff": handoff})
    active["state"] = "responded"
    state["status"] = "PAUSED"
    state["kanban_column"] = "PAUSED"
    state["resume_column"] = "PLANNING"
    state["pause_reason"] = "operator paused before consuming response"
    path = Path(state["manifest_path"])
    store.save(path, state)
    state = store.request_resume(
        path,
        reason="operator resume interrupted before verified completion",
        external_command_id="resume-paused-interrupted",
    )

    assert asyncio.run(worker._apply_control(state, FakeActions(), path)) is True
    persisted = store.load(path)
    control = persisted["controls"][-1]
    assert control["status"] == "recovering"
    assert control["result"]["outcome"] == "recovering"
    assert control["result"]["before"]["status"] == "PAUSED"

    restarted = worker_module.CDPAWorker(config, store=store)
    assert restarted._consecutive_self_route_streak(persisted) == ("PLAN", 2)

    source = _active_hop(persisted)
    _accept_self_route_guard_decision(restarted, persisted, "PLAN")

    assert source["state"] == "routed"
    assert persisted["status"] == "BLOCKED"
    assert persisted["block_code"] == "consecutive_self_route_limit"
    assert persisted["active_hop_id"] == source["hop_id"]
    assert len(persisted["hops"]) == 3
    assert RequestLedger(source["ledger_path"]).peek(
        "task-self-route-paused-interrupted-hop4"
    ) is None
