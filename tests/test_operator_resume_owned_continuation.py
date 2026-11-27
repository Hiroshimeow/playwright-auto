"""Exactly-once regression for continuing an owned, proven-unaccepted draft."""

import asyncio
from types import SimpleNamespace

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.chatgpt import (
    ChatGPTState,
    ComposerConflictError,
    MessageSnapshot,
    PageBinding,
    RateLimitBlockedError,
    SendReceipt,
)
from playwright_auto.durable import RequestLedger, RequestStatus

from test_operator_resume_recovery import _prepare_sending_record


def _owned_draft(tmp_path, monkeypatch, *, outcome="accepted"):
    store, state, worker, path, hop, ledger, binding, baseline = _prepare_sending_record(
        tmp_path, task_id="owned-continuation-" + outcome.replace("_", "-")
    )
    ledger.update(
        hop["request_id"],
        error=(
            "ComposerConflictError: composer changed or became unavailable "
            "inside the atomic send boundary"
        ),
    )
    snapshot = SimpleNamespace(
        state=ChatGPTState.DRAFT,
        page_id=binding.page_id,
        page_role=binding.role,
        page_task_id=state["task_id"],
        page_team=state["team"],
        composer_text=hop["prompt"],
        attachment_markers=(),
        send_enabled=True,
        stop_visible=False,
        blocking_dialogs=(),
        messages=(),
        logical_turn_ids=(),
        conversation_url="https://chatgpt.com/c/exact",
        url="https://chatgpt.com/c/exact",
    )

    class Client:
        def __init__(self):
            self.binding = binding
            self.send_calls = 0
            self.before_send = []

        async def assert_ownership(self):
            return snapshot

        async def send(self, prompt, **kwargs):
            self.send_calls += 1
            self.before_send.append(ledger.get(hop["request_id"]))
            assert prompt == hop["prompt"]
            assert kwargs["max_attempts"] == 1
            assert kwargs["recovery_reload"] is False
            assert kwargs["expected_task_id"] == state["task_id"]
            assert kwargs["expected_team"] == state["team"]
            if outcome == "rate_limit":
                raise RateLimitBlockedError("continuation acknowledgement unavailable")
            if outcome == "composer_conflict":
                raise ComposerConflictError("composer changed during bounded continuation")
            if outcome == "unexpected_error":
                raise RuntimeError("transport lost after possible acceptance")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return SendReceipt(
                prompt=prompt,
                prompt_sha256=hop["prompt_sha256"],
                binding=PageBinding("different-page", binding.role) if outcome == "receipt_mismatch" else binding,
                baseline=baseline,
                attempts=1,
                accepted_via="user_message_identity",
                session_id_before="exact",
                user_message_id="owned-continuation-user",
                user_turn_id="owned-continuation-turn",
            )

    client = Client()
    acquired = AcquiredRole(client, binding.page_id, snapshot.url, False, False)

    class Actions:
        async def locate_owned(self, _state, _role):
            return acquired

    monkeypatch.setattr(worker, "_publish_heartbeat", lambda *args, **kwargs: None)
    control = {"role": "PLAN", "result": {"before": {}}}
    return state, worker, hop, ledger, binding, baseline, snapshot, client, Actions(), control


def test_owned_continuation_consumes_durable_authorization_before_send(tmp_path, monkeypatch):
    state, worker, hop, ledger, binding, baseline, _snapshot, client, actions, control = _owned_draft(
        tmp_path, monkeypatch
    )
    original = (state["task_id"], hop["hop_id"], hop["request_id"], hop["turn"])
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    assert client.send_calls == 1
    before = client.before_send[0]
    assert before.error == worker_module._SENDING_CONTINUATION_STARTED
    assert before.status is RequestStatus.SENDING
    assert before.attempts == 1
    assert before.binding == binding
    assert before.baseline == baseline
    current = ledger.get(hop["request_id"])
    assert current.status is RequestStatus.SENT
    assert current.receipt["user_message_id"] == "owned-continuation-user"
    assert current.attempts == 1
    assert current.error is None
    assert (state["task_id"], hop["hop_id"], hop["request_id"], hop["turn"]) == original
    assert control["result"]["postcondition"] == "draft_accepted_once"


@pytest.mark.parametrize(
    "outcome",
    ["rate_limit", "composer_conflict", "unexpected_error", "receipt_mismatch", "cancelled"],
)
def test_owned_continuation_ambiguous_failure_is_not_replayed(tmp_path, monkeypatch, outcome):
    state, worker, hop, ledger, _binding, _baseline, _snapshot, client, actions, control = _owned_draft(
        tmp_path, monkeypatch, outcome=outcome
    )

    async def attempt(current_control):
        try:
            await worker._recover_resume_sending(state, hop, current_control, actions)
        except (RuntimeError, asyncio.CancelledError):
            # A crash/cancellation cannot erase the consumed durable authorization.
            pass

    async def exercise():
        await attempt(control)
        worker._rate_limit_cooldown = None
        second = {"role": "PLAN", "result": {"before": {}}}
        await attempt(second)
        return second

    second = asyncio.run(exercise())
    assert client.send_calls == 1
    current = ledger.get(hop["request_id"])
    assert current.status is RequestStatus.SENDING
    assert current.attempts == 1
    assert current.receipt is None
    assert str(current.error).startswith(worker_module._SENDING_CONTINUATION_STARTED)
    assert second["result"]["reason_code"] == "sending_provenance_ambiguous"


def test_owned_continuation_never_sends_if_consumption_cannot_persist(tmp_path, monkeypatch):
    state, worker, hop, _ledger, _binding, _baseline, _snapshot, client, actions, control = _owned_draft(
        tmp_path, monkeypatch
    )
    original_update = RequestLedger.update

    def fail_consumption(self, request_id, **kwargs):
        if kwargs.get("error") == worker_module._SENDING_CONTINUATION_STARTED:
            raise OSError("durable authorization write failed")
        return original_update(self, request_id, **kwargs)

    monkeypatch.setattr(RequestLedger, "update", fail_consumption)
    with pytest.raises(OSError, match="authorization write failed"):
        asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    assert client.send_calls == 0


def test_owned_continuation_preserves_nonmatching_manual_draft(tmp_path, monkeypatch):
    state, worker, hop, ledger, _binding, _baseline, snapshot, client, actions, control = _owned_draft(
        tmp_path, monkeypatch
    )
    original = ledger.get(hop["request_id"])
    snapshot.composer_text = "Operator draft which must remain untouched"
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    assert client.send_calls == 0
    assert ledger.get(hop["request_id"]).error == original.error
    assert snapshot.composer_text == "Operator draft which must remain untouched"
    assert control["result"]["reason_code"] == "manual_composer_conflict"


def test_owned_continuation_later_acceptance_is_reconciled_without_send(tmp_path, monkeypatch):
    state, worker, hop, ledger, _binding, _baseline, snapshot, client, actions, control = _owned_draft(
        tmp_path, monkeypatch, outcome="rate_limit"
    )
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))
    snapshot.messages = (
        MessageSnapshot("user", "accepted-after-timeout", "accepted-turn", hop["prompt"], ()),
    )
    worker._rate_limit_cooldown = None
    recovered = {"role": "PLAN", "result": {"before": {}}}
    asyncio.run(worker._recover_resume_sending(state, hop, recovered, actions))
    current = ledger.get(hop["request_id"])
    assert client.send_calls == 1
    assert current.status is RequestStatus.SENT
    assert current.receipt["user_message_id"] == "accepted-after-timeout"
    assert current.error is None
    assert recovered["result"]["postcondition"] == "generation_progress"
