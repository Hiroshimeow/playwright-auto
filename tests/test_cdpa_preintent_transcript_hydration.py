from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_conversations import ConversationLeaseError, ConversationWriterLease
from playwright_auto.chatgpt import MessageSnapshot
from playwright_auto.chatgpt_errors import ProviderChallengeError
from playwright_auto.durable import RequestLedger
from test_cdpa_worker import _prepare_sent_waiting_task, ready_client


def _recovery(tmp_path, monkeypatch):
    _store, state, worker, _path, previous, receipt, _sent = _prepare_sent_waiting_task(
        tmp_path, task_id="task-saved-writer-hydration", conversation_mode="persistent",
    )
    worker.config = replace(worker.config, workspace_timeout_seconds=0.3)
    role = state["roles"]["PLAN"]
    page_id = receipt.binding.page_id
    saved_url = "https://chatgpt.com/c/50b179f2-9671-44e3-8497-10905bf3903c"
    binding_id = "saved-plan-writer"
    endpoint = state["browser_endpoint_id"]
    cdp_url, _physical = worker._gate_c_endpoint_evidence(endpoint)
    observed = dict(
        endpoint_id=endpoint, endpoint_cdp_url=cdp_url,
        physical_browser_id="old-browser", page_target_identity=page_id,
        conversation_url="https://chatgpt.com/", temporary_chat=False,
        temporary_document_id=None,
    )
    writer = ConversationWriterLease(worker.runtime_db)
    writer.bind(
        binding_id=binding_id, provider_id="chatgpt_web", task_id=state["task_id"],
        role_key="PLAN", verified_new_chat=True, **observed,
    )
    accepted, _ = writer.record_send_intent(
        writer.acquire(binding_id), idempotency_key=f"browser-send:{previous['request_id']}",
        text=receipt.prompt, baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    writer.transition_attempt(accepted["attempt_id"], "accepted")
    writer.rekey(binding_id, saved_url)
    role.update(page_url=saved_url, conversation_generation=1, conversation_binding_id=binding_id)
    previous.update(
        state="routed", kind="report_repair", conversation_url=saved_url,
        conversation_generation=1, conversation_binding_id=binding_id,
    )
    hop = worker._append_hop(state, source_role="DEV", target_role="PLAN", handoff="next")
    hop.update(
        state="sending", prompt="next substantive prompt",
        prompt_sha256=worker_module._sha("next substantive prompt"),
        conversation_generation=1, browser_endpoint_id=endpoint,
        errors=["CONVERSATION_IDENTITY_MISMATCH: binding evidence changed before browser send"],
    )
    with pytest.raises(ConversationLeaseError, match="binding evidence changed"):
        writer.validate_binding(binding_id, {
            **observed, "physical_browser_id": "current-browser", "conversation_url": saved_url,
        })
    monkeypatch.setattr(worker, "_gate_c_endpoint_evidence", lambda _endpoint: (cdp_url, "current-browser"))
    worker._acquire_endpoint_admission = AsyncMock()
    client = ready_client(state, "PLAN")
    exact = replace(
        asyncio.run(client.assert_ownership()), url=saved_url,
        session_id="50b179f2-9671-44e3-8497-10905bf3903c",
        messages=(MessageSnapshot("user", "u1", "t1", receipt.prompt, ()),),
    )
    client.send = AsyncMock()
    client.set_composer_text = AsyncMock()
    client.page = SimpleNamespace(goto=AsyncMock(), reload=AsyncMock(), close=AsyncMock())
    actions = SimpleNamespace(
        reopen=AsyncMock(return_value=AcquiredRole(
            client=client, page_id=page_id, url=saved_url, created=True, new_chat=False,
        )),
        fresh_chat=AsyncMock(), restart=AsyncMock(),
    )
    control = {"action": "resume", "role": "PLAN", "status": "recovering", "origin": "operator"}
    clock = {"now": 0.0}
    real_sleep = asyncio.sleep

    async def bounded_sleep(seconds):
        assert seconds >= 0
        clock["now"] += seconds
        await real_sleep(0)

    worker_asyncio = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    worker_asyncio.sleep = bounded_sleep
    monkeypatch.setattr(worker_module, "asyncio", worker_asyncio)
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    return state, worker, previous, hop, writer, binding_id, accepted, client, exact, actions, control, clock


@pytest.mark.parametrize("arrival", ["already_exact", "empty_then_exact", "partial_history_then_exact", "partial_text_then_exact"])
def test_preintent_resume_waits_for_exact_accepted_transcript(tmp_path, monkeypatch, arrival):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    if arrival == "already_exact":
        initial = exact
    elif arrival == "partial_history_then_exact":
        initial = replace(exact, messages=(MessageSnapshot("user", "older-user", "older-turn", "older prompt", ()),))
    elif arrival == "partial_text_then_exact":
        initial = replace(exact, messages=(replace(exact.messages[0], text=exact.messages[0].text[:80]),))
    else:
        initial = replace(exact, messages=())
    client.wait_until_clean_ready = AsyncMock(return_value=initial)
    client.snapshot = AsyncMock(return_value=exact)
    parent_before = copy.deepcopy(previous)
    ledger = Path(hop["ledger_path"])
    before = ledger.read_bytes()
    command_before = worker.runtime_db.get_command(accepted["attempt_id"])

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "RUNNING"
    assert control["status"] == "applied"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    restored = writer.get_binding(state["roles"]["PLAN"]["conversation_binding_id"])
    assert restored["physical_browser_id"] == "current-browser"
    assert restored["binding_status"] == "idle"
    assert restored["restored_from_binding_id"] == old_id
    assert writer.get_binding(old_id)["binding_status"] == "sealed"
    assert ledger.read_bytes() == before
    assert worker.runtime_db.get_command(accepted["attempt_id"]) == command_before
    assert previous == parent_before
    assert hop["state"] == "sending" and hop.get("send_attempt_id") is None
    assert RequestLedger(ledger).peek(hop["request_id"]) is None
    assert state["roles"]["PLAN"]["conversation_generation"] == 1
    assert client.snapshot.await_count == (0 if arrival == "already_exact" else 1)
    assert clock["now"] <= worker.config.workspace_timeout_seconds
    client.send.assert_not_awaited()
    client.set_composer_text.assert_not_awaited()
    client.page.goto.assert_not_awaited()
    client.page.reload.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()
    actions.restart.assert_not_awaited()


@pytest.mark.parametrize("change", [
    "absent", "different_message", "different_text", "page", "role", "task", "team",
    "conversation", "manual_text", "attachment", "dialog", "responding", "composer",
    "login", "choice", "provider_challenge", "physical_browser",
])
def test_preintent_transcript_wait_preserves_fail_closed_guards(tmp_path, monkeypatch, change):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    empty = replace(exact, messages=())
    observed = exact
    if change == "absent":
        observed = empty
    elif change == "different_message":
        observed = replace(exact, messages=(replace(exact.messages[0], message_id="other-user"),))
    elif change == "different_text":
        observed = replace(exact, messages=(replace(exact.messages[0], text="not the accepted prompt"),))
    elif change in {"page", "role", "task", "team"}:
        observed = replace(exact, **{"page_" + {"page": "id", "role": "role", "task": "task_id", "team": "team"}[change]: "foreign"})
    elif change == "conversation":
        observed = replace(exact, url="https://chatgpt.com/c/91adfa5e-2e2f-4120-835d-fc98240f6a93", session_id="91adfa5e-2e2f-4120-835d-fc98240f6a93")
    elif change == "manual_text":
        observed = replace(exact, composer_text="operator draft")
    elif change == "attachment":
        observed = replace(exact, attachment_markers=("operator-file.txt",))
    elif change == "dialog":
        observed = replace(exact, blocking_dialogs=("Verify you are human",))
    elif change == "responding":
        observed = replace(exact, stop_visible=True)
    elif change == "composer":
        observed = replace(exact, composer_present=False, composer_editable=False)
    elif change == "login":
        observed = replace(exact, requires_login=True)
    elif change == "choice":
        observed = replace(exact, composer_present=False, choice_prompt_labels=("Choose answer",))
    client.wait_until_clean_ready = AsyncMock(return_value=empty)
    client.snapshot = AsyncMock(return_value=observed)
    if change == "provider_challenge":
        client.snapshot.side_effect = ProviderChallengeError("Provider verification is required")
    if change == "physical_browser":
        calls = []
        def evidence(_endpoint):
            calls.append(_endpoint)
            return ("http://127.0.0.1:9202", "current-browser" if len(calls) == 1 else "third-browser")
        # Use the same URL in the old record so only the physical identity changes.
        original = worker._gate_c_endpoint_evidence(state["browser_endpoint_id"])[0]
        def consistent_evidence(_endpoint):
            cdp, physical = evidence(_endpoint)
            return original, physical
        worker._gate_c_endpoint_evidence = consistent_evidence
    parent_before = copy.deepcopy(previous)
    ledger = Path(hop["ledger_path"])
    before = ledger.read_bytes()
    command_before = worker.runtime_db.get_command(accepted["attempt_id"])

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert control["status"] == "recovery_required"
    assert state["block_code"] == ("provider_challenge" if change == "provider_challenge" else "CONVERSATION_IDENTITY_MISMATCH")
    assert state["roles"]["PLAN"]["conversation_binding_id"] == old_id
    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert ledger.read_bytes() == before
    assert worker.runtime_db.get_command(accepted["attempt_id"]) == command_before
    assert previous == parent_before
    assert hop["state"] == "sending" and not hop.get("receipt") and not hop.get("send_attempt_id")
    assert clock["now"] <= worker.config.workspace_timeout_seconds
    assert client.snapshot.await_count <= 4
    client.send.assert_not_awaited()
    client.set_composer_text.assert_not_awaited()
    client.page.goto.assert_not_awaited()
    client.page.reload.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()
    actions.restart.assert_not_awaited()


def test_preintent_transcript_read_is_bounded_and_cancelled_on_timeout(tmp_path, monkeypatch):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    worker.config = replace(worker.config, workspace_timeout_seconds=0.05)
    client.wait_until_clean_ready = AsyncMock(return_value=replace(exact, messages=()))
    cancelled = []
    async def blocked_read():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    client.snapshot = AsyncMock(side_effect=blocked_read)
    before = Path(hop["ledger_path"]).read_bytes()
    started = time.perf_counter()

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert time.perf_counter() - started < 1
    assert cancelled == [True]
    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert not hop.get("send_attempt_id")


def test_preintent_transcript_observation_honors_task_cancellation(tmp_path, monkeypatch):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    client.wait_until_clean_ready = AsyncMock(return_value=replace(exact, messages=()))
    client.snapshot = AsyncMock(side_effect=asyncio.CancelledError)
    before = Path(hop["ledger_path"]).read_bytes()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._recover_resume_control(state, control, actions))

    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert not hop.get("send_attempt_id")


def test_preintent_transcript_observation_has_fixed_upper_bound(tmp_path, monkeypatch):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    worker.config = replace(worker.config, workspace_timeout_seconds=60)
    empty = replace(exact, messages=())
    client.wait_until_clean_ready = AsyncMock(return_value=empty)
    client.snapshot = AsyncMock(return_value=empty)
    before = Path(hop["ledger_path"]).read_bytes()

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert 4.9 <= clock["now"] <= 5
    assert client.snapshot.await_count <= 51
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert writer.get_binding(old_id)["binding_status"] == "blocked"


def test_preintent_transcript_returned_after_deadline_is_not_admitted(tmp_path, monkeypatch):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = _recovery(tmp_path, monkeypatch)
    client.wait_until_clean_ready = AsyncMock(return_value=replace(exact, messages=()))
    async def late_read():
        clock["now"] += worker.config.workspace_timeout_seconds + 0.01
        return exact
    client.snapshot = AsyncMock(side_effect=late_read)
    before = Path(hop["ledger_path"]).read_bytes()

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert state["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
    assert state["roles"]["PLAN"]["conversation_binding_id"] == old_id
    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert Path(hop["ledger_path"]).read_bytes() == before
    assert not hop.get("send_attempt_id")
