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
    PageOwnershipError,
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


def test_resume_pending_mcp_permission_hands_back_to_shared_controller(tmp_path: Path):
    store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-mcp-permission"
    )
    hop["conversation_url"] = "https://chatgpt.com/c/resume-mcp"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    snapshot = _accepted_snapshot(receipt)
    permission = {
        "type": "allow",
        "target_message_id": "resume-mcp-call",
        "remember_answer": True,
    }
    hop["wait"].update(
        mcp_allow_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat(),
        mcp_allow_seen_target="resume-mcp-call",
    )
    calls = []

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        def page_observation(self):
            return {"permission_action": permission}

        async def auto_allow_mcp_permission(self, *, passive_action=None):
            calls.append(("allow", passive_action["target_message_id"]))
            return {"method": "react_handler", "target_message_id": passive_action["target_message_id"]}

        def clear_permission_action(self):
            calls.append(("clear",))

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, hop["conversation_url"], False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired

        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("exact owned page should be reused")

        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("resume permission handoff must not require backend status")

    control = {
        "control_id": 1,
        "action": "resume",
        "role": "PLAN",
        "status": "recovering",
        "result": {"before": None},
    }
    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert calls == [("allow", "resume-mcp-call"), ("clear",)]
    assert state["status"] == "RUNNING"
    assert state["active_action"] == "wait_mcp_allow_continuation"
    assert state["block_code"] is None
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


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








def test_resume_preserves_manual_composer_and_never_uses_graph(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-manual-composer"
    )
    canonical = replace(receipt, conversation_id="resume-manual")
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=canonical.to_dict())
    hop["receipt"] = canonical.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/c/resume-manual"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()
    base_snapshot = _accepted_snapshot(canonical)
    snapshot = SimpleNamespace(
        **{
            **vars(base_snapshot),
            "manual_input_pending": True,
            "composer_empty": False,
            "composer_text": "operator draft",
        }
    )

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

        async def refresh(self):
            raise AssertionError("manual composer must never be destroyed by refresh")

    acquired = AcquiredRole(Client(), canonical.binding.page_id, hop["conversation_url"], False, False)
    calls = {"status": 0, "graph": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            return {"status": "COMPLETE"}
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("Resume must never fetch full conversation graph")
        async def locate_owned(self, *_args, **_kwargs):
            return acquired
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("exact owned page should be reused")

    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering", "result": {"before": None}}
    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert calls == {"status": 0, "graph": 0}
    assert hop["wait"]["manual_draft_present"] is True
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_resume_stream_status_rearms_exact_request_without_graph_or_replay(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-streaming-local"
    )
    canonical = replace(receipt, conversation_id="resume-streaming")
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=canonical.to_dict())
    hop["receipt"] = canonical.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/c/resume-streaming"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    snapshot = SimpleNamespace(**{**vars(_accepted_snapshot(canonical)), "stop_visible": True})

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(Client(), canonical.binding.page_id, hop["conversation_url"], False, False)
    calls = {"status": 0, "graph": 0, "tab": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            raise AssertionError("Resume must observe the exact page before any timeout diagnostic")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("streaming Resume must not fetch graph")
        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            return acquired
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("Resume must not reopen while the exact page is owned")

    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering", "result": {"before": None}}
    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert calls == {"status": 0, "graph": 0, "tab": 1}
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_resume_status_unavailable_falls_back_to_exact_local_progress_without_graph(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-status-unavailable"
    )
    canonical = replace(receipt, conversation_id="resume-unavailable")
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=canonical.to_dict())
    hop["receipt"] = canonical.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/c/resume-unavailable"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    base_snapshot = _accepted_snapshot(canonical)
    snapshot = SimpleNamespace(**{**vars(base_snapshot), "stop_visible": True})

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(Client(), canonical.binding.page_id, hop["conversation_url"], False, False)
    calls = {"graph": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("Resume must not depend on stream status before local observation")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("status recovery must not fetch graph")
        async def locate_owned(self, *_args, **_kwargs):
            return acquired
        async def reopen(self, *_args, **_kwargs):
            raise AssertionError("exact local page is already owned")

    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering", "result": {"before": None}}
    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert calls["graph"] == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


def test_resume_unknown_stream_status_fails_closed_before_dom_or_graph(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-status-unknown"
    )
    canonical = replace(receipt, conversation_id="resume-unknown")
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=canonical.to_dict())
    hop["receipt"] = canonical.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/c/resume-unknown"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    hop["wait"]["stream_status_next_poll_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    snapshot = _accepted_snapshot(canonical)

    class Client:
        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

    acquired = AcquiredRole(Client(), canonical.binding.page_id, hop["conversation_url"], False, False)
    calls = {"status": 0, "graph": 0, "tab": 0}

    class Actions:
        async def backend_stream_status(self, *_args, **_kwargs):
            calls["status"] += 1
            raise AssertionError("Resume must not consult stream status before local observation")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("Resume must never trigger graph lookup")
        async def locate_owned(self, *_args, **_kwargs):
            calls["tab"] += 1
            return acquired

    control = {"control_id": 1, "action": "resume", "role": "PLAN", "status": "recovering", "result": {"before": None}}
    asyncio.run(worker._recover_resume_waiting(state, hop, control, Actions()))

    assert calls == {"status": 0, "graph": 0, "tab": 1}
    assert control["status"] == "applied"
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "observation_rearmed"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


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


def test_resume_routes_valid_local_report_response_without_replay(tmp_path: Path, monkeypatch):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-path-only"
    )
    hop["conversation_url"] = "https://chatgpt.com/c/exact"
    state["roles"]["PLAN"]["page_url"] = hop["conversation_url"]
    state = store.save(path, state)
    hop = _active_hop(state)
    handoff = str(hop["expected_report_path"])
    report = tmp_path / handoff
    report.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = b"# PLAN report\n\nResume evidence.\n"
    report.write_bytes(report_bytes)
    response = MessageSnapshot(
        "assistant",
        "a-path-only",
        "ta-path-only",
        json.dumps({"route": "TEST", "handoff": handoff}),
        (),
    )
    snapshot = _accepted_snapshot(receipt, response=response)
    hop["wait"].update(
        result_seen_key=worker_module.hashlib.sha256(response.text.encode()).hexdigest(),
        result_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        result_samples=1,
    )
    state = store.save(path, state)
    hop = _active_hop(state)
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

        async def wait_snapshot(self, _receipt, **_kwargs):
            return snapshot

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
    assert control["result"]["action"] == "resume_role_controller"
    assert control["result"]["postcondition"] == "response_consumed"
    assert completed["response"] == response.text
    assert completed.get("validation_error") is None
    assert completed["state"] == "routed"
    assert completed["report_path"] == handoff
    assert completed["report_sha256"] == worker_module.hashlib.sha256(report_bytes).hexdigest()
    assert completed["report_size"] == len(report_bytes)
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
    actions = FakeActions()
    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert _active_hop(state)["state"] == "sending"
    assert actions.located_roles == ["PLAN"]
    assert control["status"] == "applied"
    assert control["result"]["outcome"] == "continued"
    assert control["result"]["action"] == "confirm_preboundary_role"
    assert control["result"]["postcondition"] == "ownership_confirmed_before_send"
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
    assert actions.backend_calls == []
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


def _prepare_operator_new_chat_pristine_sending(
    tmp_path: Path, *, task_id: str, empty_ledger: bool = False
):
    store, state, worker, path, hop, ledger, donor = _prepare_legacy_donorless_pristine_new(
        tmp_path, task_id=task_id
    )
    role = state["roles"]["PLAN"]
    old_page_id = "WEB:operator-fresh-lost"
    role.update(
        conversation_generation=1,
        page_id=old_page_id,
        page_url="https://chatgpt.com/",
        online=False,
        status="offline",
        last_error="page_missing",
    )
    before = {
        "status": "BLOCKED",
        "updated_at": "2026-09-05T12:46:03+00:00",
        "active_hop_id": hop["hop_id"],
        "active_role": "PLAN",
        "active_request_id": hop["request_id"],
        "hop_state": "pre_send",
        "hop_turn": hop["turn"],
        "handoff_sha256": worker_module._sha(str(hop["handoff"])),
        "role": "PLAN",
        "physical_role": hop["physical_role"],
        "page_id": None,
        "conversation_id": None,
        "conversation_generation": 0,
        "receipt_sha256": None,
        "block_code": "resume_progress_unverified",
    }
    state["controls"] = [
        {
            "control_id": 1,
            "action": "new_chat",
            "role": "PLAN",
            "reason": "Create the first exact PLAN conversation before Send.",
            "confirmed": False,
            "origin": "operator",
            "command": {
                "command_id": "cmd-operator-fresh",
                "origin": "operator",
                "action": "new_chat",
                "reason": "Create the first exact PLAN conversation before Send.",
                "task_id": state["task_id"],
                "team": state["team"],
                "repository": state["repository"],
                "role": "PLAN",
                "source_task_id": None,
                "source_event_key": None,
                "snapshot": before,
            },
            "command_state": "APPLIED",
            "status": "applied",
            "requested_at": "2026-09-05T12:46:03+00:00",
            "applied_at": "2026-09-05T12:46:19+00:00",
            "result": {"page_id": old_page_id, "new_chat": True},
            "external_command_id": None,
        }
    ]
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="preboundary_context_unrecoverable",
        reason="lost operator fresh page before durable Send",
    )
    if empty_ledger:
        ledger.path.write_text(
            json.dumps({"version": RequestLedger.VERSION, "records": {}}),
            encoding="utf-8",
        )
    return store, state, worker, path, _active_hop(state), ledger, donor


@pytest.mark.parametrize("empty_ledger", [False, True])
def test_resume_reacquires_proven_operator_new_chat_generation_one_before_send(
    tmp_path: Path, monkeypatch, empty_ledger: bool
):
    _store, state, worker, _path, hop, ledger, donor = (
        _prepare_operator_new_chat_pristine_sending(
            tmp_path,
            task_id=f"task-resume-operator-fresh-{int(empty_ledger)}",
            empty_ledger=empty_ledger,
        )
    )
    original = {
        "task_id": state["task_id"],
        "hop_id": hop["hop_id"],
        "request_id": hop["request_id"],
        "turn": hop["turn"],
        "prompt": hop["prompt"],
        "prompt_sha256": hop["prompt_sha256"],
        "generation": state["roles"]["PLAN"]["conversation_generation"],
    }
    client = SimpleNamespace(send_calls=0)

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.restart_calls = []

        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, conversation_id):
            self.backend_calls.append(conversation_id)
            raise AssertionError("proven fresh-context recovery must not depend on a bootstrap donor")

        async def restart(self, _state, role, *, known_automated_draft=None):
            self.restart_calls.append((role, known_automated_draft))
            return AcquiredRole(
                client=client,
                page_id="WEB:operator-fresh-recovered",
                url="https://chatgpt.com/",
                created=True,
                new_chat=True,
            )

        async def branch_from_anchor(self, *_args, **_kwargs):
            raise AssertionError("proven fresh-context recovery must not branch from a donor")

    ui_calls = []

    async def fresh_ui(_state, role, _actions, selected):
        ui_calls.append((role, dict(selected)))
        return AcquiredRole(
            client=client,
            page_id="WEB:operator-fresh-recovered",
            url="https://chatgpt.com/",
            created=True,
            new_chat=True,
        )

    monkeypatch.setattr(worker, "_branch_from_bootstrap_ui", fresh_ui)
    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    assert actions.backend_calls == []
    assert actions.restart_calls == [("PLAN", original["prompt"])]
    assert ui_calls == []
    assert client.send_calls == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "reacquire_preboundary_role"
    assert state["status"] == "RUNNING"
    assert state["block_code"] is None
    assert state["task_id"] == original["task_id"]
    assert hop["hop_id"] == original["hop_id"]
    assert hop["request_id"] == original["request_id"]
    assert hop["turn"] == original["turn"]
    assert hop["prompt"] == original["prompt"]
    assert hop["prompt_sha256"] == original["prompt_sha256"]
    assert state["roles"]["PLAN"]["conversation_generation"] == original["generation"]
    current = ledger.get(original["request_id"])
    if empty_ledger:
        assert current is None
    else:
        assert current is not None
        assert current.status is RequestStatus.NEW
        assert current.attempts == 0


def test_resume_operator_new_chat_provenance_mismatch_stays_fail_closed(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, _donor = (
        _prepare_operator_new_chat_pristine_sending(
            tmp_path, task_id="task-resume-operator-fresh-mismatch"
        )
    )
    state["controls"][0]["command"]["snapshot"]["active_request_id"] = "other-request"

    class Actions:
        async def locate_owned(self, _state, _role):
            return None

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("mismatched provenance must not inspect bootstrap context")

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert current is not None
    assert current.status is RequestStatus.NEW
    assert current.attempts == 0


def test_resume_operator_new_chat_with_crossed_durable_evidence_never_reacquires_fresh_context(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, _donor = (
        _prepare_operator_new_chat_pristine_sending(
            tmp_path, task_id="task-resume-operator-fresh-crossed"
        )
    )
    ledger.update(
        hop["request_id"],
        status=RequestStatus.SENDING,
        attempts=1,
        binding=PageBinding("WEB:crossed", hop["physical_role"]),
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        session_id_before="WEB:crossed",
    )

    class Actions:
        def __init__(self):
            self.bootstrap_calls = 0

        async def locate_owned(self, *_args, **_kwargs):
            return None

        async def backend_conversation(self, *_args, **_kwargs):
            self.bootstrap_calls += 1
            raise AssertionError("crossed durable evidence must not enter fresh-context bootstrap")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    assert actions.bootstrap_calls == 0
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"


def test_resume_operator_new_chat_with_attachments_never_transfers_preboundary_ownership(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, _donor = (
        _prepare_operator_new_chat_pristine_sending(
            tmp_path,
            task_id="task-resume-operator-fresh-attachment",
            empty_ledger=True,
        )
    )
    state["attachments"] = [
        {
            "path": "/tmp/operator-owned.txt",
            "name": "operator-owned.txt",
            "size": 1,
            "sha256": "0" * 64,
            "mime_type": "text/plain",
        }
    ]

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return None

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    assert ledger.get(hop["request_id"]) is None
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"


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
    assert actions.backend_calls == []
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
    hover_error: BaseException | None = None,
    rate_limit_testid: str | None = None,
    clean_ready_error: BaseException | None = None,
    validate_branch_error: BaseException | None = None,
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
            if hover_error is not None:
                raise hover_error
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

        async def evaluate(self, _script, payload=None):
            if rate_limit_testid is None:
                return False
            if not isinstance(payload, list) or len(payload) != 2:
                return False
            _markers, testids = payload
            return rate_limit_testid in testids

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
            if clean_ready_error is not None:
                raise clean_ready_error
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

        async def validate_branch_target(self, client, **kwargs):
            if validate_branch_error is not None:
                raise validate_branch_error
            return await super().validate_branch_target(client, **kwargs)

    return Actions(), context, page, clear_calls


def test_resume_ui_bootstrap_hover_timeout_without_rate_limit_stays_fail_closed(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-ui-hover-timeout"
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
    hover_error = TimeoutError(
        "Locator.hover: Timeout while <div data-testid=\"modal-conversation-history-rate-limit\"> "
        "intercepts pointer events"
    )
    actions, context, page, clear_calls = _real_ui_fallback_actions(
        worker,
        state,
        donor,
        monkeypatch,
        draft="",
        hover_error=hover_error,
    )
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(original["request_id"])
    role = state["roles"]["PLAN"]
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert context.new_page_calls == 1
    assert clear_calls == []
    assert page.closed is True
    assert page.snapshot_value is None
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert state["block_code"] == "preboundary_context_unrecoverable"
    assert state["block_retryable"] is False
    assert worker._rate_limit_cooldown is None
    assert role["page_id"] == "WEB:presend-lost"
    assert role["conversation_generation"] == original["generation"]
    assert role["bootstrap_source_donor"] == original["donor"]
    assert state["task_id"] == original["task_id"]
    assert state["team"] == original["team"]
    assert hop["hop_id"] == original["hop_id"]
    assert hop["request_id"] == original["request_id"]
    assert hop["turn"] == original["turn"]
    assert hop["prompt"] == original["prompt"]
    assert hop["prompt_sha256"] == original["prompt_sha256"]
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None


def test_resume_ui_bootstrap_ownership_failure_stays_fail_closed_with_rate_limit_modal(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-ui-ownership-failure-rate-limit-modal"
    )
    actions, context, page, clear_calls = _real_ui_fallback_actions(
        worker,
        state,
        donor,
        monkeypatch,
        draft="",
        rate_limit_testid="modal-conversation-history-rate-limit",
        clean_ready_error=PageOwnershipError("physical tab changed during clean-ready"),
    )
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert context.new_page_calls == 1
    assert clear_calls == []
    assert page.closed is True
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert state["block_code"] == "preboundary_context_unrecoverable"
    assert state["block_retryable"] is False
    assert worker._rate_limit_cooldown is None
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None


def test_resume_ui_bootstrap_semantic_failure_stays_fail_closed_with_rate_limit_modal(
    tmp_path: Path, monkeypatch
):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-ui-semantic-failure-rate-limit-modal"
    )
    actions, context, page, clear_calls = _real_ui_fallback_actions(
        worker,
        state,
        donor,
        monkeypatch,
        draft="",
        rate_limit_testid="modal-conversation-history-rate-limit",
        validate_branch_error=BranchBootstrapError(
            "branch target canonicalized back to source/donor conversation"
        ),
    )
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    control = state["controls"][-1]

    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    record = ledger.get(hop["request_id"])
    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert context.new_page_calls == 1
    assert clear_calls == []
    assert page.closed is True
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "preboundary_context_unrecoverable"
    assert state["block_code"] == "preboundary_context_unrecoverable"
    assert state["block_retryable"] is False
    assert worker._rate_limit_cooldown is None
    assert record is not None
    assert record.status is RequestStatus.NEW
    assert record.attempts == 0
    assert record.binding is None
    assert record.baseline is None
    assert record.receipt is None
    assert record.accepted_at is None


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



def test_resume_reacquires_empty_ledger_dead_page_before_send_and_sends_once(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-resume-empty-ledger-dead-page"
    )
    ledger.path.write_text(
        json.dumps({"version": RequestLedger.VERSION, "records": {}}, indent=2),
        encoding="utf-8",
    )
    assert ledger.get(hop["request_id"]) is None
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

    class Client:
        def __init__(self):
            self.binding = PageBinding("WEB:empty-ledger-replacement", hop["physical_role"])
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
                session_id="empty-ledger-session",
                conversation_url="https://chatgpt.com/c/WEB:empty-ledger-replacement",
                url="https://chatgpt.com/c/WEB:empty-ledger-replacement",
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
                MessageSnapshot("user", "u-empty-ledger", "t-empty-ledger", text, ()),
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
                session_id_before="empty-ledger-session",
                user_message_id="u-empty-ledger",
                user_turn_id="t-empty-ledger",
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

        async def branch_from_anchor(
            self, _state, role, *, source_conversation_id, assistant_message_id
        ):
            self.branch_calls.append((role, source_conversation_id, assistant_message_id))
            return acquired

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    assert actions.branch_calls == [
        ("PLAN", donor["conversation_id"], donor["assistant_message_id"])
    ]
    assert client.send_calls == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "reacquire_preboundary_role"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert ledger.get(original["request_id"]) is None
    assert state["task_id"] == original["task_id"]
    assert state["team"] == original["team"]
    assert hop["hop_id"] == original["hop_id"]
    assert hop["request_id"] == original["request_id"]
    assert hop["turn"] == original["turn"]
    assert hop["prompt"] == original["prompt"]
    assert hop["prompt_sha256"] == original["prompt_sha256"]
    assert state["roles"]["PLAN"]["conversation_generation"] == original["generation"]
    assert state["roles"]["PLAN"]["bootstrap_source_donor"] == original["donor"]

    asyncio.run(worker._sending(state, hop, actions))

    sent = ledger.get(original["request_id"])
    assert sent is not None
    assert sent.status is RequestStatus.SENT
    assert sent.attempts == 1
    assert client.send_calls == 1
    assert hop["state"] == "sent"



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


def _prepare_provisional_canonical_sending(tmp_path: Path, *, task_id: str):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id=task_id
    )
    provisional_url = "https://chatgpt.com/c/WEB:provisional-sending"
    ledger.update(
        hop["request_id"],
        baseline=baseline,
        session_id_before="WEB:provisional-sending",
        error="SendRecoveryError: acceptance observation was interrupted",
    )
    state["roles"]["PLAN"].update(
        page_url=provisional_url,
        online=True,
        status="active",
        last_error=None,
    )
    hop["conversation_url"] = provisional_url
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="durable_send_ambiguous",
        reason="same bound page canonicalized after markerless Send",
    )
    return store, state, worker, path, _active_hop(state), ledger, binding, baseline


def _backend_fresh_user_graph(user_message_id: str, prompt: str):
    def node(message_id, role, text, *, parent=None, children=(), recipient="all"):
        return {
            "id": message_id,
            "message": {
                "id": message_id,
                "author": {"role": role},
                "recipient": recipient,
                "content": {"content_type": "text", "parts": [text]},
            },
            "parent": parent,
            "children": list(children),
        }

    return {
        "current_node": "a-final",
        "mapping": {
            "client-created-root": {
                "id": "client-created-root",
                "message": None,
                "parent": None,
                "children": [user_message_id],
            },
            user_message_id: node(
                user_message_id,
                "user",
                prompt,
                parent="client-created-root",
                children=("a-call",),
            ),
            "a-call": node(
                "a-call",
                "assistant",
                "call",
                parent=user_message_id,
                children=("tool",),
                recipient="web.run",
            ),
            "tool": node(
                "tool",
                "tool",
                "result",
                parent="a-call",
                children=("u-internal",),
                recipient="assistant",
            ),
            "u-internal": node(
                "u-internal",
                "user",
                "continue",
                parent="tool",
                children=("a-final",),
            ),
            "a-final": node("a-final", "assistant", "done", parent="u-internal"),
        },
    }


def _backend_donor_user_graph(
    donor_assistant_id: str,
    accepted_user_id: str,
    accepted_prompt: str,
    *,
    donor_on_branch: bool = True,
    second_human_after_anchor: bool = False,
):
    def node(message_id, role, text, *, parent=None, children=(), recipient="all"):
        return {
            "id": message_id,
            "message": {
                "id": message_id,
                "author": {"role": role},
                "recipient": recipient,
                "content": {"content_type": "text", "parts": [text]},
            },
            "parent": parent,
            "children": list(children),
        }

    branch_donor = donor_assistant_id if donor_on_branch else "33333333-3333-4333-8333-333333333333"
    mapping = {
        "client-created-root": {
            "id": "client-created-root",
            "message": None,
            "parent": None,
            "children": ["u-inherited"],
        },
        "u-inherited": node(
            "u-inherited",
            "user",
            "inherited bootstrap request",
            parent="client-created-root",
            children=(branch_donor,),
        ),
        branch_donor: node(
            branch_donor,
            "assistant",
            "inherited bootstrap answer",
            parent="u-inherited",
            children=(accepted_user_id,),
        ),
        accepted_user_id: node(
            accepted_user_id,
            "user",
            accepted_prompt,
            parent=branch_donor,
            children=("a-final",),
        ),
        "a-final": node(
            "a-final",
            "assistant",
            "done",
            parent=accepted_user_id,
        ),
    }
    current_node = "a-final"
    if not donor_on_branch:
        mapping[donor_assistant_id] = node(
            donor_assistant_id,
            "assistant",
            "off-branch donor",
            parent="u-off-branch",
        )
    if second_human_after_anchor:
        mapping["a-final"]["children"] = ["u-followup"]
        mapping["u-followup"] = node(
            "u-followup",
            "user",
            "manual follow-up",
            parent="a-final",
        )
        current_node = "u-followup"
    return {"current_node": current_node, "mapping": mapping}


def test_provisional_sending_bootstrap_donor_anchor_excludes_only_proven_inherited_history(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, binding, baseline = (
        _prepare_provisional_canonical_sending(
            tmp_path, task_id="task-resume-provisional-donor-accepted"
        )
    )
    assert not baseline.message_ids
    assert not baseline.user_message_ids
    donor = {
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "assistant_message_id": "22222222-2222-4222-8222-222222222222",
    }
    state["roles"]["PLAN"]["bootstrap_source_donor"] = dict(donor)
    conversation_id = "77777777-7777-4777-8777-777777777777"
    canonical_url = f"https://chatgpt.com/c/{conversation_id}"
    accepted_user = "88888888-8888-4888-8888-888888888888"
    backend_prompt = hop["prompt"].replace(" ", "\u00a0", 1)
    snapshot = SimpleNamespace(
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("assistant", donor["assistant_message_id"], "turn-donor", "bootstrap prefix", ()),
            MessageSnapshot("user", accepted_user, "turn-user", backend_prompt, ()),
        ),
        send_enabled=False,
        stop_visible=False,
        session_id=conversation_id,
        url=canonical_url,
        conversation_url=canonical_url,
    )

    class Client:
        def __init__(self):
            self.binding = binding

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            raise AssertionError("donor-proven accepted SENDING must never replay Send")

    client = Client()

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.reopen_calls = 0
            self.branch_calls = 0

        async def locate_owned(self, _state, _role):
            return AcquiredRole(client, binding.page_id, canonical_url, False, False)

        async def backend_conversation(self, observed_conversation_id):
            self.backend_calls.append(observed_conversation_id)
            return _backend_donor_user_graph(
                donor["assistant_message_id"], accepted_user, backend_prompt
            )

        async def reopen(self, *_args, **_kwargs):
            self.reopen_calls += 1
            raise AssertionError("donor-proven accepted SENDING must not reopen")

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("donor-proven accepted SENDING must not branch")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "applied"
    assert current is not None and current.status is RequestStatus.SENT
    assert current.attempts == 1
    receipt = SendReceipt.from_dict(current.receipt)
    assert receipt.user_message_id == accepted_user
    assert receipt.conversation_id == conversation_id
    assert hop["state"] == "waiting"
    assert actions.backend_calls == []
    assert actions.reopen_calls == 0
    assert actions.branch_calls == 0


@pytest.mark.parametrize(
    "mode",
    [
        "donor_missing",
        "donor_wrong_branch",
        "malformed_donor",
        "changed_prompt",
        "second_human_after_anchor",
    ],
)
def test_provisional_sending_bootstrap_donor_provenance_remains_fail_closed(
    tmp_path: Path,
    mode: str,
):
    case_dir = tmp_path / mode
    case_dir.mkdir()
    _store, state, worker, _path, hop, ledger, binding, _baseline = (
        _prepare_provisional_canonical_sending(
            case_dir, task_id=f"task-resume-provisional-donor-{mode}"
        )
    )
    donor = {
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "assistant_message_id": "22222222-2222-4222-8222-222222222222",
    }
    if mode == "donor_missing":
        donor["assistant_message_id"] = "44444444-4444-4444-8444-444444444444"
    state["roles"]["PLAN"]["bootstrap_source_donor"] = (
        {"assistant_message_id": donor["assistant_message_id"]}
        if mode == "malformed_donor"
        else dict(donor)
    )
    conversation_id = "99999999-9999-4999-8999-999999999999"
    canonical_url = f"https://chatgpt.com/c/{conversation_id}"
    snapshot = SimpleNamespace(
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(),
        send_enabled=False,
        stop_visible=False,
        session_id=conversation_id,
        url=canonical_url,
        conversation_url=canonical_url,
    )

    class Client:
        def __init__(self):
            self.binding = binding

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            raise AssertionError("ambiguous donor SENDING must never replay Send")

    client = Client()

    class Actions:
        async def locate_owned(self, _state, _role):
            return AcquiredRole(client, binding.page_id, canonical_url, False, False)

        async def backend_conversation(self, _observed_conversation_id):
            prompt = hop["prompt"]
            if mode == "changed_prompt":
                prompt += " changed"
            graph_donor = donor["assistant_message_id"]
            if mode == "donor_missing":
                graph_donor = "55555555-5555-4555-8555-555555555555"
            return _backend_donor_user_graph(
                graph_donor,
                "88888888-8888-4888-8888-888888888888",
                prompt,
                donor_on_branch=(mode != "donor_wrong_branch"),
                second_human_after_anchor=(mode == "second_human_after_anchor"),
            )

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert current is not None and current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None


def test_provisional_sending_same_page_canonical_backend_acceptance_is_consumed_without_send(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, ledger, binding, baseline = (
        _prepare_provisional_canonical_sending(
            tmp_path, task_id="task-resume-provisional-canonical-accepted"
        )
    )
    conversation_id = "77777777-7777-4777-8777-777777777777"
    canonical_url = f"https://chatgpt.com/c/{conversation_id}"
    accepted_user = "88888888-8888-4888-8888-888888888888"
    backend_prompt = hop["prompt"].replace(" ", "\u00a0", 1)
    snapshot = SimpleNamespace(
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(
            MessageSnapshot("user", accepted_user, "turn-user", backend_prompt, ()),
        ),
        send_enabled=False,
        stop_visible=True,
        session_id=conversation_id,
        url=canonical_url,
        conversation_url=canonical_url,
    )

    class Client:
        def __init__(self):
            self.binding = binding
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("accepted provisional SENDING must never replay Send")

    client = Client()

    class Actions:
        def __init__(self):
            self.locate_calls = 0
            self.backend_calls = []
            self.reopen_calls = 0
            self.branch_calls = 0

        async def locate_owned(self, _state, _role):
            self.locate_calls += 1
            return AcquiredRole(client, binding.page_id, canonical_url, False, False)

        async def backend_conversation(self, observed_conversation_id):
            self.backend_calls.append(observed_conversation_id)
            return _backend_fresh_user_graph(accepted_user, backend_prompt)

        async def reopen(self, *_args, **_kwargs):
            self.reopen_calls += 1
            raise AssertionError("accepted provisional SENDING must not reopen")

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("accepted provisional SENDING must not branch/New Chat")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.locate_calls == 1
    assert actions.backend_calls == []
    assert actions.reopen_calls == 0
    assert actions.branch_calls == 0
    assert client.send_calls == 0
    assert control["status"] == "applied"
    assert control["result"]["action"] == "observe_progress"
    assert current is not None
    assert current.status is RequestStatus.SENT
    assert current.attempts == 1
    receipt = SendReceipt.from_dict(current.receipt)
    assert receipt.binding == binding
    assert receipt.baseline == baseline
    assert receipt.session_id_before == "WEB:provisional-sending"
    assert receipt.user_message_id == accepted_user
    assert receipt.conversation_id == conversation_id
    assert hop["state"] == "waiting"
    assert hop["conversation_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_url"] == canonical_url
    assert state["roles"]["PLAN"]["page_id"] == binding.page_id


@pytest.mark.parametrize(
    "mode",
    ["canonical_mismatch", "changed_prompt", "multiple_humans", "missing_provenance"],
)
def test_provisional_sending_without_unique_same_page_backend_provenance_fails_closed(
    tmp_path: Path,
    mode: str,
):
    case_dir = tmp_path / mode
    case_dir.mkdir()
    _store, state, worker, _path, hop, ledger, binding, _baseline = (
        _prepare_provisional_canonical_sending(
            case_dir, task_id=f"task-resume-provisional-{mode}"
        )
    )
    conversation_id = "99999999-9999-4999-8999-999999999999"
    canonical_url = f"https://chatgpt.com/c/{conversation_id}"
    snapshot_url = canonical_url
    snapshot_session = conversation_id
    if mode == "canonical_mismatch":
        snapshot_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        snapshot_url = f"https://chatgpt.com/c/{snapshot_session}"
    snapshot = SimpleNamespace(
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text=hop["prompt"],
        attachment_markers=(),
        blocking_dialogs=(),
        messages=(),
        send_enabled=True,
        stop_visible=False,
        session_id=snapshot_session,
        url=snapshot_url,
        conversation_url=snapshot_url,
    )

    class Client:
        def __init__(self):
            self.binding = binding
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("unproven provisional SENDING must never replay Send")

    client = Client()

    class Actions:
        def __init__(self):
            self.backend_calls = []
            self.reopen_calls = 0
            self.branch_calls = 0

        async def locate_owned(self, _state, _role):
            return AcquiredRole(client, binding.page_id, canonical_url, False, False)

        async def backend_conversation(self, observed_conversation_id):
            self.backend_calls.append(observed_conversation_id)
            if mode == "missing_provenance":
                raise worker_module.BackendNotReadyError(
                    "exact new user message is not materialized yet"
                )
            backend_prompt = hop["prompt"]
            if mode == "changed_prompt":
                backend_prompt += " changed"
            graph = _backend_fresh_user_graph("u-accepted", backend_prompt)
            if mode == "multiple_humans":
                graph["mapping"]["u-accepted"]["children"] = ["a-followup"]
                graph["mapping"]["a-followup"] = {
                    "id": "a-followup",
                    "message": {
                        "id": "a-followup",
                        "author": {"role": "assistant"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["answer"]},
                    },
                    "parent": "u-accepted",
                    "children": ["u-followup"],
                }
                graph["mapping"]["u-followup"] = {
                    "id": "u-followup",
                    "message": {
                        "id": "u-followup",
                        "author": {"role": "user"},
                        "recipient": "all",
                        "content": {"content_type": "text", "parts": ["manual follow-up"]},
                    },
                    "parent": "a-followup",
                    "children": [],
                }
                graph["current_node"] = "u-followup"
            return graph

        async def reopen(self, *_args, **_kwargs):
            self.reopen_calls += 1
            raise AssertionError("unproven provisional SENDING must not reopen")

        async def branch_from_anchor(self, *_args, **_kwargs):
            self.branch_calls += 1
            raise AssertionError("unproven provisional SENDING must not branch/New Chat")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert client.send_calls == 0
    assert actions.reopen_calls == 0
    assert actions.branch_calls == 0
    assert current is not None
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None
    assert hop["conversation_url"] == "https://chatgpt.com/c/WEB:provisional-sending"
    assert state["roles"]["PLAN"]["page_url"] == "https://chatgpt.com/c/WEB:provisional-sending"


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


def test_lost_sending_without_exact_page_fails_closed_without_graph_fetch(tmp_path: Path):
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
            raise AssertionError("lost SENDING recovery must not fetch conversation history")

        async def locate_owned(self, *_args, **_kwargs):
            self.locate_calls += 1
            return None

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.backend_calls == []
    assert actions.locate_calls == 1
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert current is not None
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None
    assert hop["state"] == "sending"


def test_lost_sending_empty_baseline_without_page_stays_ambiguous_without_graph_fetch(
    tmp_path: Path,
):
    store, state, worker, path, hop, ledger, binding, _baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-empty-baseline-accepted"
    )
    conversation_id = "55555555-5555-4555-8555-555555555555"
    accepted_user = "66666666-6666-4666-8666-666666666666"
    ledger.update(
        hop["request_id"],
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
        code="sending_provenance_ambiguous",
        reason="empty persisted baseline after accepted Send",
    )
    hop = _active_hop(state)

    class Actions:
        def __init__(self):
            self.locate_calls = 0

        async def backend_conversation(self, _observed_conversation_id):
            raise AssertionError("empty-baseline recovery must not fetch conversation history")

        async def locate_owned(self, *_args, **_kwargs):
            self.locate_calls += 1
            return None

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(hop["request_id"])
    assert actions.locate_calls == 1
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert current is not None and current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None
    assert hop["state"] == "sending"


def test_lost_sending_empty_baseline_duplicate_exact_prompt_stays_ambiguous(
    tmp_path: Path,
):
    store, state, worker, path, hop, ledger, _binding, _baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-empty-baseline-duplicate"
    )
    conversation_id = "77777777-7777-4777-8777-777777777777"
    accepted_user = "88888888-8888-4888-8888-888888888888"
    ledger.update(hop["request_id"], session_id_before=conversation_id)
    state["roles"]["PLAN"]["page_url"] = f"https://chatgpt.com/c/{conversation_id}"
    hop["conversation_url"] = f"https://chatgpt.com/c/{conversation_id}"
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="sending_provenance_ambiguous",
        reason="empty persisted baseline after accepted Send",
    )
    hop = _active_hop(state)

    class Actions:
        async def backend_conversation(self, observed_conversation_id):
            assert observed_conversation_id == conversation_id
            graph = _backend_long_user_graph(accepted_user, hop["prompt"])
            graph["mapping"]["u-base"]["message"]["content"]["parts"] = [hop["prompt"]]
            return graph

        async def locate_owned(self, *_args, **_kwargs):
            return None

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert current is not None and current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None


def test_lost_sending_empty_baseline_requires_same_durable_binding(tmp_path: Path):
    store, state, worker, path, hop, ledger, _binding, _baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-empty-baseline-binding-mismatch"
    )
    conversation_id = "99999999-9999-4999-8999-999999999999"
    ledger.update(hop["request_id"], session_id_before=conversation_id)
    state["roles"]["PLAN"].update(
        page_id="different-page",
        page_url=f"https://chatgpt.com/c/{conversation_id}",
    )
    hop["conversation_url"] = f"https://chatgpt.com/c/{conversation_id}"
    state = store.save(path, state)
    state = _queue_blocked_resume(
        store,
        state,
        code="sending_provenance_ambiguous",
        reason="empty persisted baseline after accepted Send",
    )
    hop = _active_hop(state)

    class Actions:
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("binding mismatch must fail before backend acceptance")

        async def locate_owned(self, *_args, **_kwargs):
            return None

    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, Actions()))

    current = ledger.get(hop["request_id"])
    assert control["status"] == "recovery_required"
    assert control["result"]["reason_code"] == "sending_provenance_ambiguous"
    assert current is not None and current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None


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

    actions = Actions()
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    first_control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, first_control, actions))
    first_record = ledger.get(hop["request_id"])
    assert first_control["result"]["reason_code"] == "send_acceptance_ambiguous"
    assert worker._rate_limit_cooldown["state"] == "active"
    assert client.send_calls == 1
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
        before_hops = len(state["hops"])
        with pytest.raises(ValueError):
            store.request_control(
                path,
                action,
                role="PLAN",
                reason=f"attempt {action}",
                external_command_id=f"guard-bypass-{action}",
            )
        unchanged = store.load(path)
        assert unchanged["status"] == "BLOCKED"
        assert unchanged["block_code"] == "consecutive_self_route_limit"
        assert unchanged["active_hop_id"] == source["hop_id"]
        assert len(unchanged["hops"]) == before_hops
        assert not unchanged.get("controls")

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


def _accepted_identity_graph(user_message_id: str, prompt: str):
    return {
        "current_node": user_message_id,
        "mapping": {
            user_message_id: {
                "id": user_message_id,
                "parent": None,
                "children": [],
                "message": {
                    "id": user_message_id,
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt]},
                },
            }
        },
    }


def test_resume_accepted_root_canonicalizes_from_exact_owned_local_page_without_backend_search(
    tmp_path: Path,
):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-root-local"
    )
    hop["conversation_url"] = "https://chatgpt.com/"
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/"
    snapshot = _accepted_snapshot(receipt)
    snapshot.session_id = "local-canonical"

    class Client:
        async def assert_ownership(self):
            return snapshot

    acquired = AcquiredRole(
        Client(), receipt.binding.page_id, "https://chatgpt.com/c/local-canonical", False, False
    )
    calls = {"search": 0, "graph": 0}

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired
        async def backend_search_conversations(self, *_args, **_kwargs):
            calls["search"] += 1
            raise AssertionError("accepted identity recovery must not search backend history")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("accepted identity recovery must not fetch conversation graph")

    enriched = asyncio.run(
        worker._discover_accepted_conversation_identity(state, hop, Actions(), receipt)
    )

    assert calls == {"search": 0, "graph": 0}
    assert enriched.conversation_id == "local-canonical"
    durable = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert durable is not None
    assert durable.receipt["conversation_id"] == "local-canonical"
    assert durable.attempts == 1


def test_resume_accepted_root_message_id_only_uses_same_local_identity_proof(tmp_path: Path):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-root-message-only"
    )
    message_only = replace(receipt, user_turn_id=None)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=message_only.to_dict())
    hop["receipt"] = message_only.to_dict()
    snapshot = _accepted_snapshot(message_only)
    snapshot.session_id = "message-only-canonical"

    class Client:
        async def assert_ownership(self):
            return snapshot

    acquired = AcquiredRole(
        Client(), message_only.binding.page_id, "https://chatgpt.com/c/message-only-canonical", False, False
    )

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired
        async def backend_search_conversations(self, *_args, **_kwargs):
            raise AssertionError("message-only identity must not search backend history")
        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("message-only identity must not fetch graph")

    enriched = asyncio.run(
        worker._discover_accepted_conversation_identity(state, hop, Actions(), message_only)
    )
    assert enriched.conversation_id == "message-only-canonical"
    assert RequestLedger(hop["ledger_path"]).get(hop["request_id"]).attempts == 1


@pytest.mark.parametrize("failure", ["missing_page", "wrong_page", "unsaved_page"])
def test_resume_accepted_root_unresolved_local_identity_never_rebinds_or_searches_backend(
    tmp_path: Path, failure: str
):
    _store, state, worker, _path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id=f"task-resume-root-{failure}"
    )
    calls = {"search": 0, "graph": 0}
    if failure == "missing_page":
        acquired = None
    else:
        snapshot = _accepted_snapshot(receipt)
        snapshot.session_id = None if failure == "unsaved_page" else "wrong-canonical"

        class Client:
            async def assert_ownership(self):
                return snapshot

        page_id = "different-page" if failure == "wrong_page" else receipt.binding.page_id
        acquired = AcquiredRole(Client(), page_id, "https://chatgpt.com/", False, False)

    class Actions:
        async def locate_owned(self, *_args, **_kwargs):
            return acquired
        async def backend_search_conversations(self, *_args, **_kwargs):
            calls["search"] += 1
            raise AssertionError("unresolved local identity must not search backend history")
        async def backend_conversation(self, *_args, **_kwargs):
            calls["graph"] += 1
            raise AssertionError("unresolved local identity must not fetch graph")

    with pytest.raises(worker_module.GraphIdentityError):
        asyncio.run(
            worker._discover_accepted_conversation_identity(state, hop, Actions(), receipt)
        )

    assert calls == {"search": 0, "graph": 0}
    durable = RequestLedger(hop["ledger_path"]).get(hop["request_id"])
    assert durable is not None
    assert durable.receipt.get("conversation_id") in (None, "")
    assert durable.attempts == 1


def test_route_plan_explicitly_reconciles_only_unresolved_accepted_send(tmp_path: Path):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-accepted-root-route-plan"
    )
    receipt = replace(receipt, user_turn_id=None)
    RequestLedger(hop["ledger_path"]).update(hop["request_id"], receipt=receipt.to_dict())
    hop["receipt"] = receipt.to_dict()
    hop["conversation_url"] = "https://chatgpt.com/"
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/"
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="accepted_conversation_identity_unresolved",
        block_retryable=False,
        block_reason="accepted conversation identity could not be verified",
        active_action="blocked",
    )
    state = store.save(path, state)
    queued = store.request_control(
        path,
        "route_plan",
        role="PLAN",
        reason="reconcile accepted orphan without replay",
        external_command_id="accepted-root-route-plan-once",
    )
    old_record = RequestLedger(hop["ledger_path"]).get(hop["request_id"])

    assert asyncio.run(worker._apply_control(queued, FakeActions(), path)) is True

    control = queued["controls"][-1]
    old_hop = next(item for item in queued["hops"] if item["hop_id"] == hop["hop_id"])
    child = _active_hop(queued)
    preserved = RequestLedger(old_hop["ledger_path"]).get(old_hop["request_id"])
    assert control["status"] == "applied"
    assert old_hop["state"] == "abandoned"
    assert old_hop["receipt"] == receipt.to_dict()
    assert child["kind"] == "accepted_send_reconciliation"
    assert child["target_role"] == "PLAN"
    assert child["state"] == "pre_send"
    assert child["request_id"] != old_hop["request_id"]
    assert "must not be replayed" in child["handoff"]
    assert "must not be repeated" in child["handoff"]
    assert old_record is not None and preserved is not None
    assert preserved.status is RequestStatus.SENT
    assert preserved.attempts == old_record.attempts == 1
    assert preserved.receipt == old_record.receipt
    assert queued["block_code"] is None


def test_btm_like_reconciliation_plan_can_terminalize_from_durable_evidence_without_external_mutation(
    tmp_path: Path
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-btm-like-accepted-root"
    )
    prior_report = ".plan/alpha/alpha-review_turn1_task-btm-like-accepted-root.md"
    prior_path = tmp_path / prior_report
    prior_path.parent.mkdir(parents=True, exist_ok=True)
    prior_path.write_text(
        "Durable business evidence: project 2331913 applied exactly once; requests 19->20.",
        encoding="utf-8",
    )
    state["reports"] = [{"path": prior_report}]
    hop["conversation_url"] = "https://chatgpt.com/"
    state["roles"]["PLAN"]["page_url"] = "https://chatgpt.com/"
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="accepted_conversation_identity_unresolved",
        block_retryable=False,
        block_reason="accepted conversation identity could not be verified",
        active_action="blocked",
    )
    state = store.save(path, state)
    queued = store.request_control(
        path,
        "route_plan",
        role="PLAN",
        reason="reconcile bookkeeping only",
        external_command_id="btm-like-route-plan-once",
    )
    external_mutations: list[str] = []

    assert asyncio.run(worker._apply_control(queued, FakeActions(), path)) is True
    child = _active_hop(queued)
    old_hop = next(item for item in queued["hops"] if item["hop_id"] == hop["hop_id"])
    assert prior_report in child["handoff"]
    assert "must not be repeated" in child["handoff"]

    asyncio.run(worker._pre_send(queued, child, FakeActions()))
    report_relative = (
        f".plan/{queued['team']}/{child['physical_role']}_turn{child['turn']}_"
        f"{queued['task_id']}.md"
    )
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "Reconciled from durable REVIEW/tracker evidence only; external_mutations=0.",
        encoding="utf-8",
    )
    child["response"] = json.dumps({"route": "DONE", "handoff": report_relative})
    child["state"] = "responded"
    worker._responded(queued, child)

    preserved = RequestLedger(old_hop["ledger_path"]).get(old_hop["request_id"])
    assert queued["status"] == "DONE"
    assert queued["terminal_state"] == "DONE"
    assert external_mutations == []
    assert preserved is not None and preserved.status is RequestStatus.SENT
    assert preserved.attempts == 1
    assert preserved.receipt == receipt.to_dict()


def test_route_plan_does_not_bypass_other_accepted_inflight_wait(tmp_path: Path):
    store, state, worker, path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-accepted-root-route-plan-guard"
    )
    state.update(
        status="BLOCKED",
        kanban_column="BLOCKED",
        block_code="role_offline",
        block_retryable=False,
        block_reason="ordinary accepted wait",
        active_action="blocked",
    )
    state = store.save(path, state)
    before_hops = len(state["hops"])
    with pytest.raises(ValueError, match="PLAN is already the active role"):
        store.request_control(
            path,
            "route_plan",
            role="PLAN",
            reason="must remain rejected",
            external_command_id="accepted-root-route-plan-guard",
        )
    unchanged = store.load(path)
    assert unchanged["status"] == "BLOCKED"
    assert unchanged["active_hop_id"] == hop["hop_id"]
    assert len(unchanged["hops"]) == before_hops
    assert not unchanged.get("controls")
