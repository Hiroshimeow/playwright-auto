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


def test_runtime_resume_command_remains_running_after_control_is_only_queued(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-running-command")
    blocked = _queue_blocked_resume(store, state)
    worker.hydrate_runtime()
    worker.runtime_db.enqueue_command(
        command_id="cmd-resume-running",
        idempotency_key="resume-running",
        kind="resume_team",
        task_id=None,
        expected_task_version=None,
        payload={"team": blocked["team"], "reason": "operator resume"},
    )

    command = worker.dispatch_command_once()

    assert command["status"] == "running"
    saved = store.load(blocked["manifest_path"])
    assert saved["controls"][-1]["status"] in {"requested", "recovering"}


def test_runtime_db_recovery_required_preserves_structured_result(tmp_path: Path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    db.enqueue_command(
        command_id="cmd-structured-recovery",
        idempotency_key="structured-recovery",
        kind="resume_team",
        task_id=None,
        expected_task_version=None,
        payload={"team": "alpha"},
    )
    db.require_command_recovery(
        "cmd-structured-recovery",
        error="worker is stale",
        result={
            "outcome": "recovery_required",
            "reason_code": "stale_worker",
            "next_safe_action": "pm2 restart playwright-cdpa-worker",
        },
    )

    command = db.get_command("cmd-structured-recovery")
    assert command["status"] == "recovery_required"
    assert command["result"]["reason_code"] == "stale_worker"


def test_stale_worker_resume_is_terminal_and_truthful_at_api_boundary(tmp_path: Path):
    _config, db, server, thread = start_api(tmp_path)
    try:
        status, _headers, body = request(
            server,
            "POST",
            "/api/tasks/resume",
            body={"team": "alpha", "reason": "operator resume"},
            headers={"Idempotency-Key": "stale-resume"},
        )
        accepted = json.loads(body)
        assert status == 202
        assert accepted["status"] == "recovery_required"

        status, _headers, body = request(
            server,
            "GET",
            f"/api/commands/{accepted['command_id']}",
        )
        command = json.loads(body)
        assert status == 200
        assert command["status"] == "recovery_required"
        assert command["result"]["reason_code"] == "stale_worker"
        assert command["result"]["next_safe_action"] == "pm2 restart playwright-cdpa-worker"
        assert db.claim_next_command() is None
    finally:
        server.shutdown()
        thread.join(timeout=5)


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


def test_expired_accepted_wait_uses_retry_generation_once_without_prompt_replay(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-retry-generation"
    )
    snapshot = _accepted_snapshot(receipt, retry=True)

    class Client:
        def __init__(self):
            self.retry_calls = 0
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("no stable response")

        async def retry_generation(self, _receipt, **kwargs):
            assert kwargs["expected_task_id"] == state["task_id"]
            assert kwargs["expected_team"] == state["team"]
            self.retry_calls += 1
            return {"progress": True, "transport_active": True}

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("Retry generation must not replay the accepted prompt")

    client = Client()
    acquired = AcquiredRole(client, receipt.binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    result = recovered["controls"][-1]["result"]
    assert result["outcome"] == "continued"
    assert result["action"] == "retry_generation"
    assert result["postcondition"] == "generation_progress"
    assert client.retry_calls == 1
    assert client.send_calls == 0
    assert _active_hop(recovered)["request_id"] == hop["request_id"]


def test_retry_without_generation_never_marks_resume_continued(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, _hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-retry-idle"
    )
    snapshot = _accepted_snapshot(receipt, retry=True)

    class Client:
        def __init__(self):
            self.retry_calls = 0
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("no stable response")

        async def retry_generation(self, _receipt, **_kwargs):
            self.retry_calls += 1
            raise TimeoutError("Retry generation produced no verified progress")

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
    assert control["result"]["outcome"] in {"recovery_required", "failed"}
    assert control["result"]["outcome"] != "continued"
    assert client.retry_calls == 1
    assert client.send_calls == 0


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


def test_missing_exact_tab_reopen_without_semantic_progress_is_not_success(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, _hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-reopen-only"
    )
    snapshot = _accepted_snapshot(receipt)

    class Client:
        async def assert_ownership(self):
            return snapshot

        async def wait_for_response(self, _receipt, **_kwargs):
            raise TimeoutError("no response after reopen")

    acquired = AcquiredRole(Client(), receipt.binding.page_id, snapshot.url, False, False)

    class Actions:
        def __init__(self):
            self.reopen_calls = 0

        async def locate_owned(self, _state, _role):
            return None

        async def reopen(self, _state, _role, **_kwargs):
            self.reopen_calls += 1
            return acquired

    actions = Actions()
    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    assert actions.reopen_calls == 1
    assert control["status"] == "recovery_required"
    assert control["result"]["action"] == "reopen_exact_tab"
    assert control["result"]["postcondition"] is None


def test_same_cause_reblock_is_recovery_required_not_applied(tmp_path: Path, monkeypatch):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-same-cause")
    path = Path(state["manifest_path"])
    _queue_blocked_resume(store, state, code="role_offline", reason="exact tab offline")

    class Actions:
        async def acquire(self, _state, _role):
            raise worker_module.RoleOwnershipError("exact tab offline")

        async def locate_owned(self, _state, _role):
            return None

        async def reopen(self, _state, _role, **_kwargs):
            raise worker_module.RoleOwnershipError("exact tab offline")

    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    control = recovered["controls"][-1]
    assert control["status"] == "recovery_required"
    assert control["result"]["outcome"] == "recovery_required"
    assert control["result"]["reason_code"] in {"role_offline", "same_cause_reblock"}


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


def test_sending_transcript_already_contains_accepted_user_turn_zero_resend(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id="task-resume-sending-accepted"
    )
    accepted = MessageSnapshot("user", "u-existing", "t-existing", hop["prompt"], ())
    snapshot = SimpleNamespace(
        state=ChatGPTState.SUBMITTING,
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text="",
        composer_empty=True,
        manual_input_pending=False,
        attachment_markers=(),
        send_enabled=False,
        stop_visible=True,
        retry_visible=False,
        blocking_dialogs=(),
        messages=(accepted,),
        conversation_url="https://chatgpt.com/c/exact",
        url="https://chatgpt.com/c/exact",
    )

    class Client:
        def __init__(self):
            self.binding = binding
            self.send_calls = 0

        async def assert_ownership(self):
            return snapshot

        async def send(self, *_args, **_kwargs):
            self.send_calls += 1
            raise AssertionError("existing accepted user turn forbids resend")

    client = Client()
    acquired = AcquiredRole(client, binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state, code="durable_send_ambiguous", reason="SENDING persisted")
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert client.send_calls == 0
    control = recovered["controls"][-1]
    assert control["status"] == "applied"
    assert control["result"]["action"] == "observe_progress"
    assert control["result"]["postcondition"] == "generation_progress"
    record = ledger.get(hop["request_id"])
    assert record.status is RequestStatus.SENT
    assert record.receipt["user_message_id"] == "u-existing"


def test_recovery_result_provenance_is_sanitized_and_prompt_free(tmp_path: Path):
    _config, store, state, worker = setup_task(tmp_path, task_id="task-resume-safe-result")
    path = Path(state["manifest_path"])
    secret_prompt = _active_hop(state)["handoff"]
    state = _queue_blocked_resume(store, state)

    asyncio.run(worker._apply_control(state, FakeActions(), path))

    result = state["controls"][-1]["result"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert "before" in result
    assert "prompt" not in encoded.casefold()
    assert secret_prompt not in encoded
    assert set(result["before"]) <= {
        "status",
        "updated_at",
        "active_hop_id",
        "active_role",
        "active_request_id",
        "hop_state",
        "hop_turn",
        "handoff_sha256",
        "role",
        "physical_role",
        "page_id",
        "conversation_id",
        "conversation_generation",
        "receipt_sha256",
        "block_code",
    }


def test_retry_error_dismissal_without_generation_is_not_progress(monkeypatch):
    binding = PageBinding("page-retry-idle", "alpha-plan")
    accepted = MessageSnapshot("user", "u-retry-idle", "t-retry-idle", "exact prompt", ())
    before = ChatGPTSnapshot(
        url="https://chatgpt.com/c/retry-idle",
        session_id="retry-idle",
        page_id=binding.page_id,
        page_role=binding.role,
        state=ChatGPTState.ERROR,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=("Something went wrong",),
        messages=(accepted,),
        retry_visible=True,
        page_task_id="task-retry-idle",
        page_team="alpha",
    )

    class IdleRetryPage:
        def __init__(self):
            self.current = before
            self.clicks = 0

        async def evaluate(self, _expression, arg=None):
            assert isinstance(arg, list) and len(arg) == 8
            self.clicks += 1
            self.current = replace(
                self.current,
                state=ChatGPTState.WAITING_PROMPT,
                retry_visible=False,
                error_texts=(),
            )
            return {"clicked": True, "reason": None}

    page = IdleRetryPage()
    client = ChatGPTPage(page, timeout_ms=20)
    client.binding = binding
    receipt = SendReceipt(
        prompt="exact prompt",
        prompt_sha256=worker_module._sha("exact prompt"),
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="retry-idle",
        user_message_id="u-retry-idle",
        user_turn_id="t-retry-idle",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr("playwright_auto.chatgpt.inspect_chatgpt_page", fake_inspect)

    with pytest.raises(TimeoutError, match="no verified progress"):
        asyncio.run(
            client.retry_generation(
                receipt,
                expected_task_id="task-retry-idle",
                expected_team="alpha",
            )
        )
    assert page.clicks == 1
    assert page.current.stop_visible is False
    assert not [item for item in page.current.messages if item.role == "assistant"]


def test_resume_requires_configured_stability_after_candidate_changes(
    tmp_path: Path, monkeypatch
):
    store, state, worker, path, hop, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-resume-stable-samples"
    )
    worker.config = replace(
        worker.config,
        response_stable_ms=20,
        response_poll_ms=5,
    )
    report_relative = ".plan/alpha/alpha-plan_turn1_task-resume-stable-samples.md"
    report = tmp_path / report_relative
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("stable response evidence", encoding="utf-8")
    accepted = MessageSnapshot(
        "user",
        str(receipt.user_message_id),
        str(receipt.user_turn_id),
        receipt.prompt,
        (),
    )
    first = MessageSnapshot(
        "assistant",
        "a-changing",
        "ta-changing",
        json.dumps({"route": "REVIEW", "handoff": report_relative}),
        (),
    )
    stable = MessageSnapshot(
        "assistant",
        "a-changing",
        "ta-changing",
        json.dumps({"route": "TEST", "handoff": report_relative}),
        (),
    )

    def snapshot(messages):
        return ChatGPTSnapshot(
            url="https://chatgpt.com/c/exact",
            session_id="exact",
            page_id=receipt.binding.page_id,
            page_role=receipt.binding.role,
            state=ChatGPTState.WAITING_PROMPT,
            requires_login=False,
            composer_present=True,
            composer_editable=True,
            composer_text="",
            send_visible=True,
            send_enabled=False,
            stop_visible=False,
            blocking_dialogs=(),
            attachment_markers=(),
            error_texts=(),
            messages=tuple(messages),
            page_task_id=state["task_id"],
            page_team=state["team"],
        )

    initial = snapshot((accepted,))
    changing = snapshot((accepted, first))
    settled = snapshot((accepted, stable))

    class SequencedClient(ChatGPTPage):
        def __init__(self):
            super().__init__(SimpleNamespace(), timeout_ms=100)
            self.binding = receipt.binding
            self.samples = [changing, settled]
            self.last = settled
            self.wait_samples = 0

        async def assert_ownership(self, snapshot=None, *, require_binding=True):
            return initial

        async def wait_snapshot(self, _receipt, **_kwargs):
            self.wait_samples += 1
            if self.samples:
                self.last = self.samples.pop(0)
            return self.last

    client = SequencedClient()
    acquired = AcquiredRole(client, receipt.binding.page_id, initial.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    _queue_blocked_resume(store, state)
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: Actions())

    recovered = asyncio.run(worker.advance(path, SimpleNamespace(pages=[])))

    assert client.wait_samples >= 3
    assert recovered["controls"][-1]["result"]["outcome"] == "continued"
    assert _active_hop(recovered)["target_role"] == "TEST"


def test_chatgpt_retry_generation_proves_progress_for_exact_accepted_turn(
    monkeypatch,
):
    binding = PageBinding("page-retry", "alpha-plan")
    accepted = MessageSnapshot("user", "u-retry", "t-retry", "exact prompt", ())
    before = ChatGPTSnapshot(
        url="https://chatgpt.com/c/retry",
        session_id="retry",
        page_id=binding.page_id,
        page_role=binding.role,
        state=ChatGPTState.ERROR,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="",
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=("Something went wrong",),
        messages=(accepted,),
        retry_visible=True,
        page_task_id="task-retry",
        page_team="alpha",
    )

    class RetryPage:
        def __init__(self):
            self.current = before
            self.clicks = 0

        async def evaluate(self, _expression, arg=None):
            assert isinstance(arg, list) and len(arg) == 8
            self.clicks += 1
            self.current = ChatGPTSnapshot(
                **{
                    **self.current.__dict__,
                    "state": ChatGPTState.SUBMITTING,
                    "stop_visible": True,
                    "retry_visible": False,
                    "error_texts": (),
                }
            )
            return {"clicked": True, "reason": None}

    page = RetryPage()
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = binding
    receipt = SendReceipt(
        prompt="exact prompt",
        prompt_sha256=worker_module._sha("exact prompt"),
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="retry",
        user_message_id="u-retry",
        user_turn_id="t-retry",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr("playwright_auto.chatgpt.inspect_chatgpt_page", fake_inspect)

    result = asyncio.run(
        client.retry_generation(
            receipt,
            expected_task_id="task-retry",
            expected_team="alpha",
        )
    )

    assert result == {"progress": True, "transport_active": True}
    assert page.clicks == 1


def test_chatgpt_retry_generation_preserves_manual_composer(monkeypatch):
    binding = PageBinding("page-retry-conflict", "alpha-plan")
    accepted = MessageSnapshot("user", "u-conflict", "t-conflict", "exact prompt", ())
    current = ChatGPTSnapshot(
        url="https://chatgpt.com/c/retry-conflict",
        session_id="retry-conflict",
        page_id=binding.page_id,
        page_role=binding.role,
        state=ChatGPTState.ERROR,
        requires_login=False,
        composer_present=True,
        composer_editable=True,
        composer_text="manual draft",
        send_visible=False,
        send_enabled=False,
        stop_visible=False,
        blocking_dialogs=(),
        attachment_markers=(),
        error_texts=("Something went wrong",),
        messages=(accepted,),
        retry_visible=True,
        page_task_id="task-retry-conflict",
        page_team="alpha",
    )

    class ConflictPage:
        def __init__(self):
            self.current = current
            self.clicks = 0

        async def evaluate(self, *_args, **_kwargs):
            self.clicks += 1
            raise AssertionError("manual composer must block before Retry click")

    page = ConflictPage()
    client = ChatGPTPage(page, timeout_ms=100)
    client.binding = binding
    receipt = SendReceipt(
        prompt="exact prompt",
        prompt_sha256=worker_module._sha("exact prompt"),
        binding=binding,
        baseline=MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        attempts=1,
        accepted_via="user_message_identity",
        session_id_before="retry-conflict",
        user_message_id="u-conflict",
        user_turn_id="t-conflict",
    )

    async def fake_inspect(_page):
        return page.current

    monkeypatch.setattr("playwright_auto.chatgpt.inspect_chatgpt_page", fake_inspect)

    with pytest.raises(ComposerConflictError, match="manual draft or attachments"):
        asyncio.run(
            client.retry_generation(
                receipt,
                expected_task_id="task-retry-conflict",
                expected_team="alpha",
            )
        )
    assert page.clicks == 0
    assert page.current.composer_text == "manual draft"


def test_dashboard_distinguishes_continued_recovery_failed_queued_and_stale_worker():
    app = Path("src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")
    actions = Path(
        "src/playwright_auto/dashboard_assets/views/dashboard_actions.js"
    ).read_text(encoding="utf-8")
    combined = app + "\n" + actions

    for label in ("continued", "recovery required", "failed", "queued", "stale worker"):
        assert label in combined.casefold()
