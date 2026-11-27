from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playwright_auto import chatgpt_transcript_view as view_module
from test_cdpa_preintent_transcript_hydration import _recovery


def _collapsed_recovery(tmp_path, monkeypatch):
    values = _recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    collapsed = replace(exact, messages=(replace(
        exact.messages[0], text=exact.messages[0].text[:max(1, len(exact.messages[0].text)//2)] + " … Show more",
    ),))
    client.wait_until_clean_ready = AsyncMock(return_value=collapsed)
    client.snapshot = AsyncMock(return_value=collapsed)
    target = {"targetInfo": {"targetId": "saved-target", "type": "page", "url": exact.url}}
    session = SimpleNamespace(send=AsyncMock(return_value=target), detach=AsyncMock())
    client.page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))

    async def expand(_client, **kwargs):
        assert _client is client
        assert kwargs["expected_user_id"] == previous["receipt"]["user_message_id"]
        assert kwargs["expected_prompt"] == previous["receipt"]["prompt"]
        assert kwargs["expected_owner"] == {
            "page_id": exact.page_id, "page_role": exact.page_role,
            "page_task_id": state["task_id"], "page_team": state["team"],
        }
        assert kwargs["canonical"] == exact.session_id
        assert 0 < kwargs["timeout_seconds"] <= worker.config.workspace_timeout_seconds
        await kwargs["identity_guard"]()
        return exact

    helper = AsyncMock(side_effect=expand)
    monkeypatch.setattr(view_module, "expand_saved_user_message", helper)
    return values, collapsed, session, helper


def _assert_preserved(values, ledger_bytes, prior, accepted_command):
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    assert Path(hop["ledger_path"]).read_bytes() == ledger_bytes
    assert previous == prior
    assert worker.runtime_db.get_command(accepted["attempt_id"]) == accepted_command
    assert not hop.get("send_attempt_id") and not hop.get("receipt")
    client.send.assert_not_awaited()
    client.set_composer_text.assert_not_awaited()
    client.page.goto.assert_not_awaited()
    client.page.reload.assert_not_awaited()
    client.page.close.assert_not_awaited()
    actions.fresh_chat.assert_not_awaited()
    actions.restart.assert_not_awaited()


def _before(values):
    state, worker, previous, hop, writer, old_id, accepted, *_ = values
    return Path(hop["ledger_path"]).read_bytes(), copy.deepcopy(previous), worker.runtime_db.get_command(accepted["attempt_id"])


@pytest.mark.parametrize("arrival", ["initial_collapsed", "hydration_then_collapsed"])
def test_recovery_expands_exact_saved_message_then_restores_writer(tmp_path, monkeypatch, arrival):
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    if arrival == "hydration_then_collapsed":
        client.wait_until_clean_ready.return_value = replace(exact, messages=())
    saved = _before(values)

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "RUNNING"
    assert control["result"]["postcondition"] == "ownership_reacquired_before_send"
    assert helper.await_count == 1
    assert session.detach.await_count == 1
    restored = writer.get_binding(state["roles"]["PLAN"]["conversation_binding_id"])
    assert restored["restored_from_binding_id"] == old_id
    assert restored["physical_browser_id"] == "current-browser"
    _assert_preserved(values, *saved)


@pytest.mark.parametrize("change", [
    "target_id", "target_url", "target_type", "physical_browser", "helper_error",
    "late_full_text", "changed_snapshot_owner", "changed_snapshot_text",
])
def test_expansion_failure_never_releases_writer_or_replays_send(tmp_path, monkeypatch, change):
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    saved = _before(values)
    original = helper.side_effect

    async def changed(_client, **kwargs):
        if change.startswith("target_"):
            new = dict(session.send.return_value["targetInfo"])
            key = {"target_id": "targetId", "target_url": "url", "target_type": "type"}[change]
            new[key] = {"target_id": "other-target", "target_url": "https://chatgpt.com/c/other", "target_type": "iframe"}[change]
            session.send.return_value = {"targetInfo": new}
        elif change == "physical_browser":
            monkeypatch.setattr(worker, "_gate_c_endpoint_evidence", lambda _endpoint: ("http://127.0.0.1:9202", "new-browser"))
        elif change == "helper_error":
            raise view_module.SavedTranscriptViewError("unverified view")
        elif change == "late_full_text":
            clock["now"] = worker.config.workspace_timeout_seconds + 0.1
            return exact
        elif change == "changed_snapshot_owner":
            return replace(exact, page_task_id="another-task")
        elif change == "changed_snapshot_text":
            return replace(exact, messages=(replace(exact.messages[0], text="different full prompt"),))
        return await original(_client, **kwargs)

    helper.side_effect = changed
    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert control["result"]["outcome"] == "recovery_required"
    assert state["roles"]["PLAN"]["conversation_binding_id"] == old_id
    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert helper.await_count == 1
    assert session.detach.await_count == 1
    _assert_preserved(values, *saved)


def test_collapsed_view_is_attempted_at_most_once_per_resume(tmp_path, monkeypatch):
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    saved = _before(values)
    helper.side_effect = None
    helper.return_value = None

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert helper.await_count == 1
    assert client.snapshot.await_count > 0
    _assert_preserved(values, *saved)


@pytest.mark.parametrize("kind", ["already_full", "no_user_id", "no_collapse_label", "wrong_user", "unsafe_draft"])
def test_unqualified_message_does_not_open_view(tmp_path, monkeypatch, kind):
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    initial = collapsed
    if kind == "already_full":
        initial = exact
    elif kind == "no_user_id":
        initial = replace(collapsed, messages=(replace(collapsed.messages[0], message_id=None),))
    elif kind == "no_collapse_label":
        initial = replace(collapsed, messages=(replace(collapsed.messages[0], text="incomplete loading text"),))
    elif kind == "wrong_user":
        initial = replace(collapsed, messages=(replace(collapsed.messages[0], message_id="foreign"),))
    elif kind == "unsafe_draft":
        initial = replace(collapsed, composer_text="operator draft")
    client.wait_until_clean_ready.return_value = initial
    client.snapshot.return_value = initial
    saved = _before(values)

    asyncio.run(worker._recover_resume_control(state, control, actions))

    helper.assert_not_awaited()
    client.page.context.new_cdp_session.assert_not_awaited()
    assert state["status"] == ("RUNNING" if kind == "already_full" else "BLOCKED")
    _assert_preserved(values, *saved)



def test_no_candidate_view_recomputes_remaining_snapshot_budget(tmp_path, monkeypatch):
    from playwright_auto import cdpa_worker as worker_module
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    saved = _before(values)
    timeouts = []
    real_wait_for = asyncio.wait_for

    async def no_candidate(_client, **kwargs):
        clock["now"] = 0.25
        return None

    async def timed(awaitable, *, timeout):
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    helper.side_effect = no_candidate
    client.snapshot.side_effect = TimeoutError()
    monkeypatch.setattr(worker_module.asyncio, "wait_for", timed)

    asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["status"] == "BLOCKED"
    assert helper.await_count == 1
    assert timeouts[-1] == pytest.approx(0.05)
    _assert_preserved(values, *saved)


def test_expansion_cancellation_keeps_original_send_and_writer(tmp_path, monkeypatch):
    values, collapsed, session, helper = _collapsed_recovery(tmp_path, monkeypatch)
    state, worker, previous, hop, writer, old_id, accepted, client, exact, actions, control, clock = values
    saved = _before(values)
    helper.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._recover_resume_control(state, control, actions))

    assert state["roles"]["PLAN"]["conversation_binding_id"] == old_id
    assert writer.get_binding(old_id)["binding_status"] == "blocked"
    assert session.detach.await_count == 1
    _assert_preserved(values, *saved)
