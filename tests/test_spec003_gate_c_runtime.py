"""Gate C owner contracts. All browser evidence is offline, with no runtime started."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_runtime_db import RuntimeDB


@pytest.fixture
def writers(tmp_path):
    from playwright_auto.cdpa_conversations import ConversationWriterLease
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    yield ConversationWriterLease(db)
    db.close()


def bind(writers, identity="one", *, conversation="abc", physical="browser-1", target="target-1", task=None):
    return writers.bind(
        binding_id=identity, provider_id="chatgpt_web", endpoint_id="ep-1",
        endpoint_cdp_url="http://127.0.0.1:9999", physical_browser_id=physical,
        page_target_identity=target,
        conversation_url=f"https://chatgpt.com/c/{conversation}" if conversation else "https://chatgpt.com/",
        verified_new_chat=conversation is None, task_id=task, role_key="DEV" if task else None,
    )


def intent(writers, binding, key="message-1"):
    return writers.record_send_intent(binding, idempotency_key=key, text="identical text",
                                      baseline_last_user_message_id="old", baseline_user_turn_count=3)


def test_t075_provisional_physical_target_is_unique_before_send(writers):
    first = bind(writers, conversation=None, task="task-1")
    assert first["page_binding_id"]
    with pytest.raises(ValueError, match="WRITER_LEASE_BUSY"):
        bind(writers, "direct", conversation=None)


def test_t075_canonical_global_writer_and_epoch_fence(writers):
    first = bind(writers)
    second = bind(writers, "two", physical="other-browser", target="other-target")
    lease = writers.acquire(first["binding_id"])
    with pytest.raises(ValueError, match="WRITER_LEASE_BUSY"):
        writers.acquire(second["binding_id"])
    writers.finish_turn(first["binding_id"], assistant_terminal=True, turn_committed=True)
    new = writers.acquire(second["binding_id"])
    assert new["lease_epoch"] > lease["lease_epoch"]
    with pytest.raises(ValueError, match="WRITER_LEASE_LOST"):
        intent(writers, lease)
    assert writers.db.get_command_by_idempotency_key("message-1") is None


def test_t076_send_attempt_is_fenced_in_existing_command_ledger_and_not_queued(writers):
    current = writers.acquire(bind(writers)["binding_id"])
    attempt, created = intent(writers, current)
    assert created and attempt["state"] == "intent_recorded"
    assert attempt["lease_epoch"] == current["lease_epoch"]
    assert writers.db.claim_next_command() is None
    duplicate, created = intent(writers, current)
    assert not created and duplicate == attempt
    assert writers.db.get_command(attempt["attempt_id"])["kind"] == "browser_send"
    for state in ("submitted", "unconfirmed", "reconciled_accepted"):
        attempt = writers.transition_attempt(attempt["attempt_id"], state)
        assert attempt["state"] == state
    with pytest.raises(ValueError, match="SEND_ATTEMPT_TRANSITION"):
        writers.transition_attempt(attempt["attempt_id"], "submitted")


def test_t077_same_historical_text_does_not_prove_acceptance(writers):
    current = writers.acquire(bind(writers)["binding_id"])
    attempt, _ = intent(writers, current)
    historical = {"last_user_message_id": "old", "user_turn_count": 3, "last_user_text": "identical text"}
    assert writers.reconcile_attempt(attempt["attempt_id"], historical)["state"] == "unconfirmed"
    fresh = {**historical, "last_user_message_id": "new", "user_turn_count": 4}
    assert writers.reconcile_attempt(attempt["attempt_id"], fresh)["state"] == "reconciled_accepted"


def test_t075_accepted_send_holds_lease_through_terminal_and_route_commit(writers):
    current = writers.acquire(bind(writers)["binding_id"])
    attempt, _ = intent(writers, current)
    writers.transition_attempt(attempt["attempt_id"], "accepted")
    with pytest.raises(ValueError, match="TURN_NOT_TERMINAL"):
        writers.finish_turn("one", assistant_terminal=False, turn_committed=True)
    assert writers.acquire("one")["lease_epoch"] == current["lease_epoch"]
    writers.finish_turn("one", assistant_terminal=True, turn_committed=True)
    assert writers.get_binding("one")["binding_status"] == "idle"
    assert writers.endpoint_in_use("ep-1")
    writers.close_binding("one")
    assert not writers.endpoint_in_use("ep-1")


def test_t075_unresolved_stop_seals_and_permanently_quarantines(writers):
    current = writers.acquire(bind(writers)["binding_id"])
    intent(writers, current)
    writers.close_binding("one", abandon=True)
    assert writers.get_binding("one")["binding_status"] == "sealed"
    assert not writers.endpoint_in_use("ep-1")
    bind(writers, "two", physical="other", target="other")
    with pytest.raises(ValueError, match="WRITER_LEASE_QUARANTINED") as error:
        writers.acquire("two")
    assert not error.value.retryable


@pytest.mark.parametrize("field,value", [("endpoint_id", "ep-2"), ("endpoint_cdp_url", "http://127.0.0.1:9998"),
                                            ("physical_browser_id", "changed"), ("page_target_identity", "changed")])
def test_t074_binding_drift_rejects_before_send(writers, field, value):
    original = bind(writers)
    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.validate_binding("one", {**original, field: value})


def test_t078_adopted_identity_is_exact_and_unresolved_never_provisional(writers):
    from playwright_auto.cdpa_conversations import canonical_conversation
    assert canonical_conversation("https://chatgpt.com/c/abc?model=x") == "abc"
    for url in ("https://evil.example/c/abc", "https://chatgpt.com/c/abc/extra", "https://chatgpt.com/"):
        with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
            canonical_conversation(url)
    original = bind(writers)
    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.validate_binding("one", {**original, "conversation_url": "https://chatgpt.com/c/abcd"})


def test_t075_rekey_collision_blocks_winner_and_loser_then_seals_loser(writers):
    winner = writers.acquire(bind(writers)["binding_id"])
    loser = writers.acquire(bind(writers, "new", conversation=None, physical="other", target="new")["binding_id"])
    attempt, _ = intent(writers, loser)
    with pytest.raises(ValueError, match="WRITER_LEASE_QUARANTINED"):
        writers.rekey("new", "https://chatgpt.com/c/abc")
    with pytest.raises(ValueError, match="WRITER_LEASE_QUARANTINED"):
        intent(writers, winner, "winner-message")
    writers.transition_attempt(attempt["attempt_id"], "reconciled_absent")
    assert writers.get_binding("new")["binding_status"] == "sealed"
    assert writers.acquire("one")["baseline_required"]


def test_t075_recovery_target_mismatch_is_reconciliation_only(writers):
    original = bind(writers, conversation=None)
    current = writers.acquire("one")
    intent(writers, current)
    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.validate_binding("one", {**original, "page_target_identity": "replacement"})
    assert writers.get_binding("one")["binding_status"] == "blocked"
