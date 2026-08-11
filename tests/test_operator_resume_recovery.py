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

        async def backend_stream_status(self, *_args, **_kwargs):
            raise AssertionError("Resume completion must remain DOM-only")

        async def backend_conversation(self, *_args, **_kwargs):
            raise AssertionError("Resume completion must remain DOM-only")

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


def test_dashboard_distinguishes_continued_recovery_failed_queued_and_stale_worker():
    app = Path("src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")
    actions = Path(
        "src/playwright_auto/dashboard_assets/views/dashboard_actions.js"
    ).read_text(encoding="utf-8")
    combined = app + "\n" + actions

    for label in ("continued", "recovery required", "failed", "queued", "stale worker"):
        assert label in combined.casefold()
