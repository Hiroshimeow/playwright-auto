import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import playwright_auto.cdpa_actions as actions_module
import playwright_auto.cdpa_worker as worker_module
from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions
from playwright_auto.cdpa_commands import workflow_control_eligibility
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_prompts import CONVERSATION_ROUTE_REPAIR_PREFIX
from playwright_auto.cdpa_runtime_registry import MINIMUM_DEADLINE_SECONDS
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.chatgpt_errors import UnsafePageStateError
from playwright_auto.chatgpt_models import (
    ChatGPTState, MessageBaseline, MessageSnapshot, PageBinding, SendReceipt, prompt_digest,
)
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_actions import FakeContext, FakePage
from test_cdpa_core import write_config
from test_cdpa_worker import (
    FakeActions, RecordingCDPASendClient, _prepare_sent_waiting_task, ready_client,
)
from test_cdpa_worker_endpoints import _pool
from test_operator_resume_recovery import _prepare_pristine_new_sending_record, _queue_blocked_resume
from test_p019_mailbox_control_completion import _enqueue_control


TEMPORARY_CHAT_URL = "https://chatgpt.com/?temporary-chat=true"


class _TemporaryPage:
    async def evaluate(self, *_args):
        return "temporary-document-test-token"


def _temporary_ready_client(state, role):
    client = ready_client(state, role)
    client.page = _TemporaryPage()
    return client


def _temporary_task(tmp_path: Path, *, task_id: str):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "Continue workflow in temporary chat",
        requested_team="alpha",
        task_id=task_id,
        temporary=True,
    )
    worker = CDPAWorker(config, store=store)
    # Direct _pre_send tests must include scheduler endpoint admission.
    assert worker._prepare_execution_endpoint(state, str(state["browser_endpoint_id"]))
    return store, state, worker


class TemporaryWorkflowActions(FakeActions):
    def __init__(self, *, owned: bool = False):
        super().__init__()
        self.owned = owned
        self.fresh_urls = []

    async def acquire(self, state, role, *, clean_for_send=False):
        assert self.owned, "temporary workflow must reuse only the owned generation"
        assert clean_for_send is True
        record = state["roles"][role]
        return AcquiredRole(
            client=_temporary_ready_client(state, role),
            page_id=str(record["page_id"]),
            url=str(record["page_url"]),
            created=False,
            new_chat=False,
        )

    async def reopen(self, *_args, **_kwargs):
        raise AssertionError("temporary workflow must not reopen a saved conversation")

    async def locate_owned(self, state, role):
        if not self.owned:
            return None
        record = state["roles"][role]
        return AcquiredRole(
            client=_temporary_ready_client(state, role),
            page_id=str(record["page_id"]),
            url=str(record["page_url"]),
            created=False,
            new_chat=False,
        )

    async def fresh_chat(self, state, role, *, url, clean_for_send=False):
        assert clean_for_send is True
        self.fresh_urls.append(url)
        record = state["roles"][role]
        return AcquiredRole(
            client=_temporary_ready_client(state, role),
            page_id=f"temporary-{record['physical_role']}",
            url=url,
            created=True,
            new_chat=True,
        )


def test_workflow_temporary_first_send_opens_exact_temporary_url(tmp_path: Path):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-first")
    hop = _active_hop(state)
    actions = TemporaryWorkflowActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    role = state["roles"]["PLAN"]
    assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
    assert role["page_url"] == TEMPORARY_CHAT_URL
    assert hop["state"] == "sending"


@pytest.mark.parametrize(
    "scenario",
    [
        "valid", "valid_unavailable_composer", "streaming_valid", "invalid", "absent", "draft",
        "owned_draft", "owned_draft_uploaded_attachment", "owned_draft_attachment", "owned_draft_changed", "parent_draft", "draining_owned_draft",
        "attachment", "ambiguous_composer", "unavailable_composer", "readonly_composer",
        "wrong_document", "crossed_writer", "submitted", "accepted", "pause_supersedes",
        "draining_valid", "draining_invalid", "draining_absent", "draining_peer_binding",
        "draining_race_valid", "draining_race_absent", "draining_bad_ledger", "historical_pause",
    ],
)
def test_dashboard_resume_observes_exact_temporary_repair_before_send(tmp_path, monkeypatch, scenario):
    store, state, worker, path, parent, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="resume-temporary-plan-repair", conversation_mode="temporary",
    )
    parent.update(state="responded", response="Original reply without routing JSON.")
    worker._responded(state, parent)
    repair = _active_hop(state)
    assert repair["kind"] == "route_repair"
    actions = TemporaryWorkflowActions(owned=True)
    asyncio.run(worker._pre_send(state, repair, actions))
    assert repair["state"] == "sending"
    role = state["roles"]["PLAN"]
    if scenario == "owned_draft_uploaded_attachment":
        original_file = tmp_path / "original-input.txt"
        original_file.write_text("Already uploaded in this conversation.", encoding="utf-8")
        state["attachments"] = [item.to_dict() for item in worker_module.collect_file_identities((str(original_file),))]
        role["attachments_uploaded_generation"] = int(role.get("conversation_generation") or 0)
    role["page_url"] = repair["conversation_url"] = TEMPORARY_CHAT_URL
    owned = asyncio.run(actions.locate_owned(state, "PLAN"))
    owned = replace(owned, new_chat=True)  # Register the fixture's first writer binding.
    before = owned.client.assert_ownership.return_value
    before = replace(before, page_id=owned.page_id, url=owned.url, messages=(
        MessageSnapshot("user", "u1", "t1", parent["prompt"], ()),
        MessageSnapshot("assistant", "a1", "t1", parent["response"], ()),
    ))
    owned.client.assert_ownership.return_value = before
    draining = scenario.startswith("draining_")
    pause_case = scenario in {"pause_supersedes", "historical_pause"}
    drain_race = scenario.startswith("draining_race_")
    scheduled = draining or pause_case or scenario == "streaming_valid"
    endpoint_id = state["browser_endpoint_id"]
    if scheduled:
        peer = store.endpoints.add("9202")["endpoint_id"]
        pool = asyncio.run(_pool(store))
        worker._endpoint_pool = pool  # Register the writer with the real pool identity.
    writer, binding, intent = asyncio.run(worker._gate_c_prepare_browser_send(state, repair, "PLAN", owned))
    owned = replace(owned, new_chat=False)  # Subsequent locate_owned observes the existing page.
    ledger = RequestLedger(repair["ledger_path"])
    ledger.begin(
        role=repair["physical_role"], prompt=repair["prompt"],
        source_context={key: state[key] for key in ("task_id", "team")} | {
            "hop_id": repair["hop_id"], "manifest": state["manifest_path"],
        },
        role_prompt_hash=worker_module._sha(str(
            worker_module.task_workflow_definitions(state, worker.config)["PLAN"]["system_prompt"]
        )),
        request_id=repair["request_id"], render_request_marker=False,
    )
    assert worker._is_pristine_preboundary_sending_record(state, repair, ledger.peek(repair["request_id"]))
    parent_ledger = Path(parent["ledger_path"])
    assert RequestLedger(parent_ledger).peek(parent["request_id"]).status is RequestStatus.COMPLETED
    assert not parent.get("report_path")
    expected = parent["expected_report_path"]
    verified_report = scenario in {
        "valid", "valid_unavailable_composer", "streaming_valid", "pause_supersedes", "historical_pause",
        "draining_valid", "draining_race_valid",
    }

    def write_report(route="DEV"):
        footer = json.dumps({"route": route, "handoff": expected})
        (tmp_path / expected).write_text(
            f"# Recovered report\n\nCDPA-RESULT {parent['report_result']['key']}: {footer}\n", encoding="utf-8",
        )

    if (verified_report and not pause_case) or scenario in {
        "invalid", "wrong_document", "submitted", "accepted", "draining_invalid", "draining_peer_binding",
    }:
        write_report("WRONG" if scenario in {"invalid", "draining_invalid"} else "DEV")
    if scenario in {"submitted", "accepted"}:
        writer.transition_attempt(intent["attempt_id"], "submitted")
    if scenario == "accepted":
        accepted = replace(receipt, prompt=repair["prompt"], prompt_sha256=prompt_digest(repair["prompt"]),
                           user_message_id="repair-user", user_turn_id="repair-turn")
        ledger.update(repair["request_id"], status=RequestStatus.SENDING, attempts=1,
                      baseline=accepted.baseline, binding=accepted.binding)
        ledger.update(repair["request_id"], status=RequestStatus.SENT, receipt=accepted.to_dict(), accepted_at=1.0)
    if scenario == "wrong_document":
        owned.client.page.evaluate = AsyncMock(return_value="different-document-token")
    if scenario == "crossed_writer":
        role["conversation_binding_id"] = "other-writer-binding"
    # Keep a distinct operator turn; individual cases control the composer draft.
    idle = replace(before, state=ChatGPTState.WAITING_PROMPT, messages=(*before.messages,
        MessageSnapshot("user", "operator-user", "operator-turn", "Please finish the report.", ()),
        MessageSnapshot("assistant", "operator-reply", "operator-turn", "I handled the request.", ()),
    ))
    current = replace(
        idle,
        response_projection_active=scheduled,
        state=ChatGPTState.DRAFT if scenario in {
            "owned_draft", "owned_draft_uploaded_attachment", "owned_draft_attachment", "owned_draft_changed", "parent_draft", "draining_owned_draft",
        } else idle.state,
        composer_text=(
            repair["prompt"] if scenario in {"owned_draft", "owned_draft_uploaded_attachment", "owned_draft_attachment", "draining_owned_draft"}
            else repair["prompt"] + " operator edit" if scenario == "owned_draft_changed"
            else parent["prompt"] if scenario == "parent_draft"
            else "Unsaved operator draft" if scenario in {
                "draft", "valid", "streaming_valid", "draining_valid", "draining_race_valid",
            } else ""
        ),
        attachment_markers=("operator.pdf",) if scenario in {"attachment", "owned_draft_attachment"} else (),
        composer_present=scenario not in {"unavailable_composer", "valid_unavailable_composer"},
        composer_editable=scenario != "readonly_composer",
        composer_ambiguous=scenario == "ambiguous_composer",
    )
    owned.client.snapshot = AsyncMock(return_value=current)
    owned.client.assert_ownership = AsyncMock(side_effect=AssertionError("Resume observation must stay read-only"))
    actions.locate_owned = AsyncMock(return_value=owned)
    actions.preflight_role = AsyncMock(return_value=[])
    actions.acquire = actions.fresh_chat = actions.restart = AsyncMock(side_effect=AssertionError("must not replace tab"))
    monkeypatch.setattr(worker_module, "CDPATabActions", lambda *_args, **_kwargs: actions)
    if pause_case:
        store.save(path, state | {
            "status": "BLOCKED", "kanban_column": "BLOCKED", "block_code": "manual_composer_conflict",
            "block_retryable": False, "block_reason": "post-fill composer conflict",
        })
        worker.hydrate_runtime(startup=False)
        _enqueue_control(worker, state, command_id="older-resume", action="resume")
        assert worker.dispatch_command_once()["status"] == "running"
    else:
        _queue_blocked_resume(store, state, code="manual_composer_conflict", reason="post-fill composer conflict")
    before_ledgers = {file: file.read_bytes() for file in {ledger.path, parent_ledger}}
    before_binding = writer.get_binding(binding["binding_id"])

    def advance():
        return asyncio.run(worker.advance(path, SimpleNamespace(pages=[]), endpoint_id=role["browser_endpoint_id"]))

    def attempt_state():
        return worker.runtime_db.get_command(intent["attempt_id"])["payload"]["send_attempt"]["state"]

    def assert_recovering(saved, action):
        assert saved["controls"][-1]["status"] == "recovering"
        assert saved["active_hop_id"] == repair["hop_id"]
        assert saved["active_action"] == action
        assert attempt_state() == "intent_recorded"
        assert all(file.read_bytes() == data for file, data in before_ledgers.items())

    async def poll_streaming_resume():
        nonlocal worker
        assert state["endpoint_pinned"] is True
        assert {endpoint_id, peer} <= pool.contexts.keys()
        forbidden = AsyncMock(side_effect=AssertionError("observation must not admit a writer or Send"))
        for method in ("_acquire_endpoint_admission", "_gate_c_prepare_browser_send", "_sending"):
            monkeypatch.setattr(worker, method, forbidden)
        now = 1000.0
        monkeypatch.setattr(worker_module, "time", SimpleNamespace(time=lambda: now, monotonic=lambda: now))
        if pause_case:
            worker.hydrate_runtime(startup=False)
        real_advance = worker.advance

        async def check_lease(path, context, **kwargs):
            if kwargs.get("expected_endpointless_control") is not None:
                assert state["task_id"] not in worker._execution_leases
            else:
                lease = worker._execution_leases[state["task_id"]]
                assert lease["endpoint_id"] == kwargs["endpoint_id"] == endpoint_id
                assert lease["request_id"] == repair["request_id"]
                assert context is pool.contexts[endpoint_id]
            return await real_advance(path, context, **kwargs)

        dispatch = AsyncMock(side_effect=check_lease)
        monkeypatch.setattr(worker, "advance", dispatch)

        async def tick():
            results = await worker.run_once(None, endpoint_pool=pool, publish_browser=False)
            assert not worker._execution_leases
            return results

        try:
            if pause_case:
                now += MINIMUM_DEADLINE_SECONDS
            first, = await tick()
            assert_recovering(first, "wait_response")
            assert dispatch.await_count == owned.client.snapshot.await_count == 1
            if scenario == "historical_pause":
                # Pre-fix persistence: Pause applied, but the older Resume remained in flight.
                store.request_control(path, "pause")
                legacy = store.load(path)
                legacy.update(status="PAUSED", kanban_column="PAUSED", pause_reason="manual pause",
                              resume_status=first["status"], resume_column=first["kanban_column"],
                              resume_action=first["active_action"])
                legacy["controls"][-1].update(status="applied", command_state="APPLIED",
                                               applied_at=legacy["controls"][-1]["requested_at"])
                paused = store.save(path, legacy)
                worker = CDPAWorker(worker.config, store=store)
                worker._endpoint_pool = pool
                worker.hydrate_runtime()
                real_advance = worker.advance
                monkeypatch.setattr(worker, "advance", dispatch)
                for method in ("_acquire_endpoint_admission", "_gate_c_prepare_browser_send", "_sending"):
                    monkeypatch.setattr(worker, method, forbidden)
                # Rejection is a control-only operation, even with no attached browser.
                await pool.mark_unavailable(endpoint_id, "test restart without pinned endpoint")
                await pool.mark_unavailable(peer, "test restart without peer endpoint")
                now += MINIMUM_DEADLINE_SECONDS
                await tick()
                rejected = store.load(path)
                assert rejected["status"] == "PAUSED"
                assert rejected["active_hop_id"] == paused["active_hop_id"]
                prior, pause = rejected["controls"]
                assert prior["control_id"] < pause["control_id"]
                assert prior["status"] == "rejected" and prior["result"]["reason_code"] == "superseded_by_pause"
                older = worker.runtime_db.get_command("older-resume")
                assert older["status"] == "failed" and older["result"] == prior["result"]
                assert owned.client.snapshot.await_count == 1
                assert attempt_state() == "intent_recorded"
                await pool.refresh(wait=True)
                write_report()
                owned.client.snapshot.return_value = idle
                _enqueue_control(worker, rejected, command_id="fresh-resume", action="resume")
                assert worker.dispatch_command_once()["status"] == "running"
                now += MINIMUM_DEADLINE_SECONDS
                recovered, = await tick()
                assert [item["status"] for item in recovered["controls"]] == ["rejected", "applied", "applied"]
                assert worker.runtime_db.get_command("fresh-resume")["status"] == "applied"
                return recovered
            if scenario == "pause_supersedes":
                assert first["status"] == "RUNNING"
                _enqueue_control(worker, first, command_id="newer-pause", action="pause")
                command = worker.dispatch_command_once()
                assert command["command_id"] == "newer-pause" and command["status"] == "running"
                assert store.load(path)["controls"][-1]["action"] == "pause"
                now += MINIMUM_DEADLINE_SECONDS
                paused, = await tick()
                assert paused["status"] == "PAUSED", paused["controls"]
                assert worker.runtime_db.get_command("newer-pause")["status"] == "applied"
                owned.client.snapshot.return_value = idle  # Stale Resume could now admit Send.
                worker.hydrate_runtime(startup=False)
                now += MINIMUM_DEADLINE_SECONDS
                assert await tick() == []
                assert dispatch.await_count == 2 and owned.client.snapshot.await_count == 1
                prior, pause = store.load(path)["controls"]
                assert prior["control_id"] < pause["control_id"]
                assert prior["status"] == "rejected"
                assert prior["result"]["reason_code"] == "superseded_by_pause"
                older = worker.runtime_db.get_command("older-resume")
                assert older["status"] == "failed" and older["result"] == prior["result"]
                assert store.load(path)["status"] == "PAUSED" and attempt_state() == "intent_recorded"
                write_report()
                _enqueue_control(worker, paused, command_id="fresh-resume", action="resume")
                assert worker.dispatch_command_once()["status"] == "running"
                now += MINIMUM_DEADLINE_SECONDS
                recovered, = await tick()
                assert [item["status"] for item in recovered["controls"]] == ["rejected", "applied", "applied"]
                assert worker.runtime_db.get_command("fresh-resume")["status"] == "applied"
                assert owned.client.snapshot.await_count == 2
                return recovered
            # Exercise restart from a held manifest as well as the first requested tick.
            if scenario == "draining_invalid":
                held = first  # Even RUNNING protection must not admit Send during drain.
            else:
                held = store.save(path, first | {"status": "BLOCKED", "block_code": "manual_composer_conflict"})
            if scenario == "draining_peer_binding":
                peer_url, peer_physical_id = worker._gate_c_endpoint_evidence(peer)
                peer_binding = writer.bind(
                    binding_id="peer-writer-binding", provider_id=binding["provider_id"], endpoint_id=peer,
                    endpoint_cdp_url=peer_url, physical_browser_id=peer_physical_id, page_target_identity=owned.page_id,
                    conversation_url=owned.url, verified_new_chat=True, temporary_chat=True,
                    temporary_document_id=binding["temporary_document_id"], task_id=state["task_id"], role_key="PLAN",
                )
                held["roles"]["PLAN"]["conversation_binding_id"] = peer_binding["binding_id"]
                _active_hop(held)["conversation_binding_id"] = peer_binding["binding_id"]
                held = store.save(path, held)
            worker.hydrate_runtime(startup=False)
            if drain_race:
                now += MINIMUM_DEADLINE_SECONDS
                owned.client.snapshot.return_value = replace(current, response_projection_active=False)
                real_load = store.load
                endpoint_checked = False
                real_snapshot = pool.snapshot

                def snapshot_before_removal(**kwargs):
                    nonlocal endpoint_checked
                    snapshot = real_snapshot(**kwargs)
                    if "reservations" in kwargs:
                        endpoint_checked = True
                    return snapshot

                def remove_before_lease(candidate):
                    if Path(candidate) == path and endpoint_checked and not store.endpoints.get(endpoint_id)["draining"]:
                        assert state["task_id"] not in worker._execution_leases
                        removed = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint_id})
                        assert removed["draining"] and not removed["removed"]
                    return real_load(candidate)

                monkeypatch.setattr(pool, "snapshot", snapshot_before_removal)
                monkeypatch.setattr(store, "load", remove_before_lease)
                normal_resume = AsyncMock(wraps=worker._recover_resume_control)
                monkeypatch.setattr(worker, "_recover_resume_control", normal_resume)
                recovered, = await tick()
                assert store.endpoints.get(endpoint_id)["draining"]
                normal_resume.assert_not_awaited()
                assert dispatch.await_count == owned.client.snapshot.await_count == 2
                if not verified_report:
                    assert_recovering(recovered, "wait_report")
                return recovered
            if draining:
                removed = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint_id})
                assert removed["draining"] and not removed["removed"]
                assert worker._finalize_drained_endpoints() == []
                await pool.refresh(wait=True)
                assert {endpoint_id, peer} <= pool.contexts.keys()
            if scenario == "draining_bad_ledger":
                ledger.path.write_text(json.dumps({"version": -1, "records": {}}), encoding="utf-8")
                before_ledgers[ledger.path] = ledger.path.read_bytes()
                unrelated = store.create_task("Unrelated work", requested_team="beta", browser_endpoint_id=peer)
                worker.hydrate_runtime(startup=False)
                now += MINIMUM_DEADLINE_SECONDS

                async def check_unrelated(candidate, context, **kwargs):
                    assert Path(candidate) == Path(unrelated["manifest_path"])
                    assert context is pool.contexts[peer] and kwargs["endpoint_id"] == peer
                    return unrelated

                dispatch.side_effect = check_unrelated
                assert await tick() == [unrelated]
                assert dispatch.await_count == 2 and owned.client.snapshot.await_count == 1
                return store.load(path)
            assert await tick() == []  # Registry polling keeps the normal minimum delay.
            now += MINIMUM_DEADLINE_SECONDS
            await pool.mark_unavailable(endpoint_id, "test pinned endpoint outage")
            assert peer in pool.contexts
            assert await tick() == []  # Do not use the healthy peer.
            assert dispatch.await_count == 1
            await pool.refresh(wait=True)

            # A live lease for this task must also suppress dispatch.
            worker._execution_leases[state["task_id"]] = {"endpoint_id": endpoint_id}
            try:
                assert await worker.run_once(None, endpoint_pool=pool, publish_browser=False) == []
                assert dispatch.await_count == 1
            finally:
                worker._execution_leases.pop(state["task_id"])

            if scenario == "draining_peer_binding":
                owned.client.snapshot.return_value = idle
                assert await tick() == []  # A peer binding cannot authorize even observation.
                assert dispatch.await_count == owned.client.snapshot.await_count == 1
                return store.load(path)
            still_streaming, = await tick()
            assert_recovering(still_streaming, "wait_response")
            assert still_streaming["status"] == held["status"]
            assert still_streaming["updated_at"] == held["updated_at"]
            assert await tick() == []  # An unchanged observation is throttled too.
            assert dispatch.await_count == owned.client.snapshot.await_count == 2
            owned.client.snapshot.return_value = replace(current, response_projection_active=False) if draining else idle
            now += MINIMUM_DEADLINE_SECONDS
            recovered, = await tick()
            assert dispatch.await_count == owned.client.snapshot.await_count == 3
            if draining and not verified_report:
                assert_recovering(recovered, "wait_report")
                assert await tick() == []
                now += MINIMUM_DEADLINE_SECONDS
                still_held, = await tick()
                assert still_held == recovered
                assert dispatch.await_count == owned.client.snapshot.await_count == 4
            return recovered
        finally:
            forbidden.assert_not_awaited()
            await pool.close()

    if scheduled:
        saved = asyncio.run(poll_streaming_resume())
    else:
        saved = advance()
        if scenario in {"draft", "attachment", "ambiguous_composer", "unavailable_composer", "readonly_composer",
                        "owned_draft_attachment", "owned_draft_changed", "parent_draft"}:
            assert_recovering(saved, "wait_manual_input")
            owned.client.snapshot.return_value = idle
            saved = advance()

    control = saved["controls"][-1]
    active = _active_hop(saved)
    canonical_parent = next(hop for hop in saved["hops"] if hop["hop_id"] == parent["hop_id"])
    if verified_report:
        assert control["status"] == "applied"
        assert canonical_parent["route"] == "DEV" and canonical_parent["report_path"] == expected
        assert active["target_role"] == "DEV"
        assert next(hop for hop in saved["hops"] if hop["hop_id"] == repair["hop_id"])["state"] == "abandoned"
    elif draining:
        assert control["status"] == "recovering"
        assert active["hop_id"] == repair["hop_id"] and active["state"] == "sending"
        assert not canonical_parent.get("report_path")
        assert attempt_state() == "intent_recorded"
        assert writer.get_binding(binding["binding_id"]) == before_binding
    elif scenario in {"wrong_document", "crossed_writer", "submitted"}:
        assert control["status"] == "recovery_required"
        assert active["hop_id"] == repair["hop_id"] and not canonical_parent.get("report_path")
        assert attempt_state() == ("submitted" if scenario == "submitted" else "intent_recorded")
    elif scenario == "accepted":
        assert active["hop_id"] == repair["hop_id"] and active["state"] == "waiting"
        assert not canonical_parent.get("report_path")
        assert ledger.peek(repair["request_id"]).attempts == 1
        assert ledger.peek(repair["request_id"]).receipt["user_message_id"] == "repair-user"
    else:
        assert control["status"] == "applied"
        assert active["hop_id"] == repair["hop_id"] and active["state"] == "sending"
        assert active["request_id"] == repair["request_id"]
        assert active["send_attempt_id"] == intent["attempt_id"]
        saved_role = saved["roles"]["PLAN"]
        assert saved_role["page_id"] == owned.page_id
        assert saved_role["temporary_document_id"] == binding["temporary_document_id"]
        assert saved_role["conversation_binding_id"] == binding["binding_id"]
        assert not canonical_parent.get("report_path")
    if verified_report or (not draining and scenario not in {"wrong_document", "crossed_writer", "submitted", "accepted"}):
        assert attempt_state() == "reconciled_absent"
    assert all(file.read_bytes() == data for file, data in before_ledgers.items()
               if scenario != "accepted" or file != ledger.path)
    if scenario in {"owned_draft", "owned_draft_uploaded_attachment"}:
        assert owned.client.snapshot.return_value.composer_text == repair["prompt"]
        assert ledger.peek(repair["request_id"]).status is RequestStatus.NEW
        assert ledger.peek(repair["request_id"]).attempts == 0
    if scenario == "owned_draft_uploaded_attachment":
        assert saved["attachments"] == state["attachments"]
        assert not ledger.peek(repair["request_id"]).files
    owned.client.assert_ownership.assert_not_awaited()
    for change in (actions.acquire, actions.fresh_chat, actions.restart):
        change.assert_not_awaited()


@pytest.mark.parametrize("hop_state", ["sending", "waiting"])
def test_blocked_temporary_workflow_keeps_all_owned_tabs(tmp_path: Path, hop_state):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-keep-on-block")
    _active_hop(state)["state"] = hop_state
    worker._block(state, "operator recovery required")
    calls = []

    class Actions:
        async def preflight_team(self, state):
            calls.append("preflight")
            return [object()]

        async def close_team(self, state, *, preflighted_pages):
            calls.append("close")

    asyncio.run(worker._release_blocked_workflow_tabs(state, Actions()))

    assert calls == []
    assert state["status"] == "BLOCKED"
    assert _active_hop(state)["state"] == hop_state


def test_temporary_task_start_url_is_not_treated_as_adopted_conversation(tmp_path: Path):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-start-url")
    hop = _active_hop(state)
    # Temporary tasks may carry the generic ChatGPT start URL in their initial
    # hop. It is not an adopted durable conversation and has no canonical ID.
    hop["conversation_url"] = "https://chatgpt.com/"
    actions = TemporaryWorkflowActions()

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
    assert state["roles"]["PLAN"]["page_url"] == TEMPORARY_CHAT_URL
    assert hop["state"] == "sending"


@pytest.mark.parametrize(
    "page_url, composer_text, can_replace",
    [
        (TEMPORARY_CHAT_URL, "", True),
        (TEMPORARY_CHAT_URL, "operator draft", False),
        ("https://chatgpt.com/c/foreign?temporary-chat=true", "", False),
    ],
)
def test_temporary_first_send_with_owned_but_unproven_page(
    tmp_path: Path, page_url, composer_text, can_replace
):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-unproven")
    hop = _active_hop(state)
    role = state["roles"]["PLAN"]
    # Exact owner on the 9202 landing URL, but no recorded fresh acquisition.
    role.update(page_id="unproven-plan-page", page_url=page_url, online=True)
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    client.current = replace(client.current, url=page_url, composer_text=composer_text)

    class Actions(TemporaryWorkflowActions):
        async def locate_owned(self, state, logical_role):
            acquired = await super().locate_owned(state, logical_role)
            return replace(acquired, client=client) if acquired else None

        async def preflight_team(self, *_args, **_kwargs):
            pytest.fail("unsafe owned page must not be selected for blocked-tab cleanup")

    actions = Actions(owned=True)
    asyncio.run(worker._pre_send(state, hop, actions))

    if can_replace:
        assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
        assert hop["state"] == "sending"
        assert hop["generation_started_here"] is True
        assert hop["conversation_generation"] == 1
        assert role["page_id"] != "unproven-plan-page"
        assert role["page_url"] == TEMPORARY_CHAT_URL
    else:
        assert state["status"] == "BLOCKED"
        assert hop["state"] == "pre_send"
        assert not actions.fresh_urls
        assert role["page_id"] == "unproven-plan-page"
        asyncio.run(worker._release_blocked_workflow_tabs(state, actions))
    assert not client.set_calls
    assert not client.send_calls


def test_workflow_temporary_same_generation_reuses_owned_tab(tmp_path: Path):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-owned")
    role = state["roles"]["PLAN"]
    role.update(
        page_id="temporary-plan-page",
        page_url="https://chatgpt.com/c/current-temporary-generation",
        online=True,
        conversation_generation=1,
    )
    actions = TemporaryWorkflowActions(owned=True)
    hop = _active_hop(state)

    asyncio.run(worker._pre_send(state, hop, actions))

    assert actions.fresh_urls == []
    assert role["page_id"] == "temporary-plan-page"
    assert role["page_url"] == "https://chatgpt.com/c/current-temporary-generation"
    assert hop["state"] == "sending"


@pytest.mark.parametrize("report_present,tab_lost,draft,attachments,parent_kind", [
    (True, False, "", (), "task"), (False, False, "", (), "task"),
    (True, True, "", (), "task"), (False, True, "", (), "task"),
    (True, False, "operator draft", (), "task"), (False, False, "", ("operator.pdf",), "task"),
    (True, False, "", (), "route_repair"), (False, False, "", (), "route_repair"),
])
def test_completed_blank_temporary_turn_queues_one_same_tab_repair(
    tmp_path, report_present, tab_lost, draft, attachments, parent_kind,
):
    _store, state, worker, _path, hop, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="task-temp-completed-blank",
    )
    hop.pop("report_result")
    state["conversation_mode"] = "temporary"
    if parent_kind == "route_repair":
        # Reproduce an accepted format repair whose prompt still allows tools.
        hop.update(kind=parent_kind, repair_attempt=1)
        state["route_repair_count"] = 1
    state["roles"]["PLAN"].update(
        conversation_generation=1,
        temporary_document_id="temporary-document-test-token",
        page_url="https://chatgpt.com/c/exact?temporary-chat=true",
    )
    report = tmp_path / hop["expected_report_path"]
    if report_present:
        report.write_text("# Completed report\n\n**Route verdict:** DEV.\n", encoding="utf-8")
    assistant = MessageSnapshot("assistant", "blank-id", "blank-turn", "", ("copy", "retry-menu"))
    hop["wait"]["empty_terminal_seen"] = {
        "identity": [assistant.message_id, assistant.turn_id],
        "since": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
    }
    client = ready_client(state, "PLAN")
    snapshot = replace(
        client.assert_ownership.return_value,
        url="https://chatgpt.com/c/exact?temporary-chat=true",
        messages=(MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()), assistant),
    )
    acquired = SimpleNamespace(client=client, page_id=snapshot.page_id)
    ledger = Path(hop["ledger_path"])
    accepted = ledger.read_bytes()

    decision = asyncio.run(worker.role_controller.run(
        worker, state, hop, acquired, FakeActions(), snapshot=snapshot,
    ))

    assert decision.reason == "empty_terminal_route_repair"
    assert ledger.read_bytes() == accepted
    repair = _active_hop(state)
    assert repair["parent_hop_id"] == hop["hop_id"] and repair["target_role"] == hop["target_role"]
    assert repair["kind"] == "route_repair" and repair["state"] == "pre_send"
    assert repair["request_id"] != hop["request_id"] and hop["response"] is None

    class OriginalDocument(_TemporaryPage):
        async def evaluate(self, expression, create):
            assert not create, "repair must never replace the original document token"
            return await super().evaluate(expression, create)

    class Actions(TemporaryWorkflowActions):
        async def locate_owned(self, state, role):
            owned = await super().locate_owned(state, role)
            if owned is not None:
                owned.client.page = OriginalDocument()
                current = owned.client.assert_ownership.return_value
                owned.client.assert_ownership.return_value = replace(
                    current, composer_text=draft, attachment_markers=attachments,
                )
            return owned

        async def acquire(self, *_args, **_kwargs):
            pytest.fail("blank-turn repair must use the exact tab without cleaning the composer")

    actions = Actions(owned=not tab_lost)
    asyncio.run(worker._pre_send(state, repair, actions))
    assert not actions.fresh_urls
    if tab_lost:
        assert repair["state"] == "pre_send" and state["block_code"] == "temporary_repair_document_unavailable"
        return
    if draft or attachments:
        assert repair["state"] == "pre_send"
        assert state["status"] == "RUNNING"
        assert repair["wait"]["manual_draft_present"] is True
        assert ledger.read_bytes() == accepted
        return
    assert repair["state"] == "sending" and state["roles"]["PLAN"]["page_id"] == receipt.binding.page_id
    assert "Do not call tools, repeat role work, change files" in repair["prompt"]
    assert ("Read-only excerpt" in repair["prompt"]) == report_present
    assert ("REPORT_UNAVAILABLE" in repair["prompt"]) == (not report_present)


@pytest.mark.parametrize("kind", ["route_repair", "report_repair"])
@pytest.mark.parametrize("draft,attachments", [
    ("operator draft", ()), ("", ("operator.pdf",)), ("", ()),
])
def test_temporary_repair_preserves_operator_input(tmp_path, kind, draft, attachments):
    _, state, worker, _, hop, _, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="preserve-repair-composer", conversation_mode="temporary",
    )
    hop.update(state="responded", response="malformed")
    if kind == "route_repair":
        worker._responded(state, hop)
    else:
        hop["response"] = json.dumps({"route": "DEV", "handoff": hop["expected_report_path"]})
        worker._responded(state, hop)
    repair = _active_hop(state)
    assert repair["kind"] == kind

    class Actions(TemporaryWorkflowActions):
        async def locate_owned(self, state, role):
            owned = await super().locate_owned(state, role)
            current = owned.client.assert_ownership.return_value
            owned.client.assert_ownership.return_value = replace(
                current, composer_text=draft, attachment_markers=attachments,
            )
            return owned

        async def acquire(self, *_args, **_kwargs):
            pytest.fail("repair must reuse the owned tab without cleaning the composer")

    actions = Actions(owned=True)
    asyncio.run(worker._pre_send(state, repair, actions))

    assert not actions.fresh_urls
    assert repair["state"] == ("pre_send" if draft or attachments else "sending")
    assert repair["wait"]["manual_draft_present"] is bool(draft or attachments)


def test_temporary_continuation_stays_pre_send_while_previous_turn_is_responding(tmp_path):
    from playwright_auto.chatgpt_errors import UnsafePageStateError

    _, state, worker, _, parent, _, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="temporary-presend-previous-response", conversation_mode="temporary",
    )
    expected = parent["expected_report_path"]
    key = parent["report_result"]["key"]
    footer = json.dumps({"route": "PAUSE", "handoff": expected})
    (tmp_path / expected).write_text(
        f"# External input needed\n\nCDPA-RESULT {key}: {footer}\n", encoding="utf-8",
    )
    assert worker._poll_running_report_result(state, parent)
    hop = worker._append_hop(state, source_role="PLAN", target_role="PLAN", handoff=expected)
    ledger = Path(parent["ledger_path"])
    accepted = ledger.read_bytes()

    class Actions(TemporaryWorkflowActions):
        responding = True

        async def acquire(self, *args, **kwargs):
            if self.responding:
                raise UnsafePageStateError("page is already responding")
            return await super().acquire(*args, **kwargs)

    actions = Actions(owned=True)
    asyncio.run(worker._pre_send(state, hop, actions))

    assert hop["state"] == "pre_send"
    assert hop["wait"]["pre_send_retry_at"]
    assert hop.get("observed_existing_response") is None
    assert hop["receipt"] is None
    assert ledger.read_bytes() == accepted
    assert not actions.fresh_urls

    actions.responding = False
    hop["wait"]["pre_send_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(worker._pre_send(state, hop, actions))

    assert hop["state"] == "sending"
    assert ledger.read_bytes() == accepted
    assert not actions.fresh_urls


@pytest.mark.parametrize("assistant_text", ["", "I finished the work."])
@pytest.mark.parametrize("report_state", ["missing", "empty", "unchanged_empty", "invalid", "valid"])
@pytest.mark.parametrize("solo", [False, True])
@pytest.mark.parametrize("conversation_mode", ["temporary", "persistent"])
def test_report_contract_checks_the_file_before_any_response_repair(
    tmp_path, assistant_text, report_state, solo, conversation_mode,
):
    _store, state, worker, _path, hop, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="task-report-before-repair", conversation_mode=conversation_mode, solo=solo,
    )
    worker.role_controller.policy = replace(worker.role_controller.policy, response_stable_seconds=0)
    assistant = MessageSnapshot("assistant", "answer-id", "answer-turn", assistant_text, ("copy",))
    now = datetime.now(timezone.utc)
    hop["wait"]["empty_terminal_seen"] = {
        "identity": [assistant.message_id, assistant.turn_id],
        "since": (now - timedelta(seconds=10)).isoformat(),
    }
    hop["report_result"]["checked_at"] = now.isoformat()
    expected = hop["expected_report_path"]
    if report_state == "valid":
        key = hop["report_result"]["key"]
        result = json.dumps({"route": "DONE" if solo else "DEV", "handoff": expected})
        (tmp_path / expected).write_text(f"# Finished work\n\nCDPA-RESULT {key}: {result}\n", encoding="utf-8")
    elif report_state == "invalid":
        (tmp_path / expected).write_text("# Finished work, missing route footer\n", encoding="utf-8")
    elif report_state in {"empty", "unchanged_empty"}:
        (tmp_path / expected).write_bytes(b"")
        if report_state == "unchanged_empty":
            worker._prepare_report_result(state, hop, expected)
    client = ready_client(state, hop["target_role"])
    snapshot = replace(
        client.assert_ownership.return_value,
        url="https://chatgpt.com/c/exact" + ("?temporary-chat=true" if conversation_mode == "temporary" else ""),
        messages=(MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()), assistant),
    )
    acquired = SimpleNamespace(client=client, page_id=snapshot.page_id)
    for _ in range(2):
        decision = asyncio.run(worker.role_controller.run(
            worker, state, hop, acquired, FakeActions(), snapshot=snapshot,
        ))
        if decision.action.value != "wait":
            break
    if report_state == "valid":
        assert decision.reason == "verified_report_result"
        if solo:
            assert state["status"] == "DONE"
            assert len(state["hops"]) == 1
        else:
            assert _active_hop(state)["target_role"] == "DEV"
    elif assistant_text:
        assert decision.reason == "malformed_result"
        assert _active_hop(state)["kind"] == "route_repair"
    elif report_state in {"invalid", "empty"}:
        assert decision.reason == "invalid_report_result"
        repair = _active_hop(state)
        assert repair["kind"] == "route_repair"
        assert "report" in repair["validation_error"]
        if report_state == "empty":
            assert "empty" in repair["validation_error"]
    else:
        assert decision.reason == "empty_terminal_response"
        assert state["status"] == "BLOCKED" and _active_hop(state) is hop


@pytest.mark.parametrize("report_restored", [False, True])
def test_resume_after_unavailable_read_only_repair_checks_exact_report(tmp_path, report_restored):
    store, state, worker = _temporary_task(tmp_path, task_id="restore-read-only-repair-report")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, TemporaryWorkflowActions()))
    prompt = CONVERSATION_ROUTE_REPAIR_PREFIX + "Return the route from existing evidence only."
    receipt = SendReceipt(
        prompt, prompt_digest(prompt), PageBinding("temporary-alpha-plan", hop["physical_role"]),
        MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        1, "user_message_identity", None, "accepted-user", "accepted-turn",
    )
    ledger = RequestLedger(hop["ledger_path"])
    ledger.begin(role=hop["physical_role"], prompt=prompt, request_id=hop["request_id"],
                 render_request_marker=False)
    ledger.update(hop["request_id"], status=RequestStatus.SENDING, attempts=1,
                  binding=receipt.binding, baseline=receipt.baseline)
    ledger.update(hop["request_id"], status=RequestStatus.SENT, accepted_at=1.0,
                  receipt=receipt.to_dict())
    hop.update(kind="route_repair", state="waiting", prompt=prompt,
               prompt_sha256=receipt.prompt_sha256, receipt=receipt.to_dict())
    hop.pop("report_result")  # The read-only prompt did not authorize a report footer.
    hop["wait"].update(controller_reason="route_repair_report_unavailable", result_seen_key="unavailable-result")
    state.update(status="BLOCKED", kanban_column="BLOCKED", active_action="blocked",
                 block_code="route_repair_report_unavailable", block_retryable=False)
    state = store.save(path, state)
    if report_restored:
        report = tmp_path / hop["expected_report_path"]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# Restored report\n\n**Route verdict:** DEV.\n", encoding="utf-8")
    accepted = Path(hop["ledger_path"]).read_bytes()
    queued = store.resume_team(state["team"])
    control = queued["controls"][-1]
    saved = asyncio.run(worker.advance(
        path, None, scheduling_tasks=[queued],
        expected_endpointless_control=(control["control_id"], "resume"),
    ))
    assert Path(hop["ledger_path"]).read_bytes() == accepted
    if report_restored:
        assert saved["controls"][-1]["result"]["postcondition"] == "report_route_recovered"
        assert _active_hop(saved)["state"] == "pre_send"
        assert _active_hop(saved)["target_role"] == "DEV"
    else:
        assert saved["status"] == "BLOCKED"
        assert saved["block_code"] == "route_repair_report_unavailable"
        assert saved["active_hop_id"] == hop["hop_id"]


@pytest.mark.parametrize("tab_lost,parent_kind,guard", [
    (False, "handoff", None), (True, "handoff", None),
    (False, "route_repair", None), (True, "route_repair", None),
    (False, "handoff", "report_missing"), (False, "handoff", "draft"),
    (False, "handoff", "receipt_mismatch"), (False, "route_repair", "budget_exhausted"),
])
def test_temporary_blank_terminal_recovers_only_route_on_same_turn(
    tmp_path: Path, tab_lost, parent_kind, guard,
):
    _store, state, worker, path, hop, receipt, _ = _prepare_sent_waiting_task(
        tmp_path, task_id="task-temp-terminal-missing",
    )
    hop.pop("report_result")
    state["conversation_mode"] = "temporary"
    state["context_policy"] = "fresh"
    if parent_kind == "route_repair":
        state["route_repair_count"] = 1
        hop.update(kind=parent_kind, repair_attempt=1)
    else:
        hop["kind"] = parent_kind
    report = tmp_path / hop["expected_report_path"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "# Completed report\n\n" + "Intermediate evidence. " * 650 + "\nRoute requested: DEV\n",
        encoding="utf-8",
    )
    hop["timestamps"]["sent_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=worker.role_controller.policy.timeout_seconds + 1)
    ).isoformat()
    worker._start_wait_budget_from_sent(hop)
    hop["wait"]["controller_progress_at"] = hop["timestamps"]["sent_at"]
    ledger = RequestLedger(hop["ledger_path"])
    if guard == "report_missing":
        report.unlink()
    if guard == "receipt_mismatch":
        ledger.update(hop["request_id"], receipt=None)
    if guard == "budget_exhausted":
        state["route_repair_count"] = state["max_route_repairs"]
    accepted = ledger.peek(hop["request_id"])
    role = state["roles"]["PLAN"]
    role.update(
        conversation_generation=1,
        temporary_document_id="temporary-document-test-token",
        page_url="https://chatgpt.com/c/exact?temporary-chat=true",
    )
    snapshot = ready_client(state, "PLAN").assert_ownership.return_value
    snapshot = replace(snapshot, url=role["page_url"], composer_text="my draft" if guard == "draft" else "", messages=(
        MessageSnapshot("user", receipt.user_message_id, receipt.user_turn_id, receipt.prompt, ()),
        MessageSnapshot("assistant", "empty", "exact-assistant", "", ()),
    ))

    class Actions(TemporaryWorkflowActions):
        async def locate_owned(self, state, logical_role):
            acquired = await super().locate_owned(state, logical_role)
            if acquired is not None:
                async def wait_snapshot(_receipt, **_kwargs):
                    assert _receipt == receipt
                    return snapshot
                acquired.client.wait_snapshot = wait_snapshot
            return acquired

    actions = Actions(owned=not tab_lost)
    if tab_lost:
        # Resume admission clears block_code; the waiting hop retains its diagnosis.
        hop["wait"]["controller_reason"] = (
            "missing_terminal_route_repair" if parent_kind == "route_repair"
            else "response_terminal_missing"
        )
        control = {"role": "PLAN", "status": "recovering", "result": {"before": {
            "block_code": "response_terminal_missing",
        }}}
        asyncio.run(worker._recover_resume_waiting(state, hop, control, actions))
        assert control["status"] == "applied"
    else:
        asyncio.run(worker._waiting(state, hop, actions, path))

    repair = _active_hop(state)
    assert ledger.peek(hop["request_id"]) == accepted
    if guard:
        assert repair is hop
        assert state["status"] == "BLOCKED"
        assert state["block_code"] == (
            "ROUTE_VALIDATION_EXHAUSTED" if guard == "budget_exhausted" else "response_terminal_missing"
        )
        assert not actions.fresh_urls
        return
    assert state["status"] == "RUNNING" and state["block_code"] is None
    assert repair["kind"] == "route_repair"
    assert repair["parent_hop_id"] == hop["hop_id"]
    assert repair["turn"] == hop["turn"]
    assert repair["repair_attempt"] == (2 if parent_kind == "route_repair" else 1)
    assert repair["request_id"] != hop["request_id"]
    assert hop.get("response") is None
    assert not actions.fresh_urls  # Queuing a repair does not itself touch Send or a tab.

    asyncio.run(worker._pre_send(state, repair, actions))
    assert actions.fresh_urls == ([TEMPORARY_CHAT_URL] if tab_lost else [])
    assert repair["expected_report_path"] == hop["expected_report_path"]
    assert hop["expected_report_path"] in repair["prompt"]
    assert "terminal result" in repair["prompt"]
    assert "Route requested: DEV" in repair["prompt"]
    assert "Read-only excerpt" in repair["prompt"]
    assert "Do not call tools" in repair["prompt"]
    assert "Ensure the report exists" not in repair["prompt"]
    assert len(repair["prompt"]) < 6000
    assert state["task_text"] not in repair["prompt"]
    assert ledger.peek(hop["request_id"]) == accepted


@pytest.mark.parametrize("route_label", ["Route", "Route verdict"])
@pytest.mark.parametrize("guard", [None, "conflicting_route", "nonfinal_route", "unselected_route",
                                     "terminal_route", "missing_report", "receipt_mismatch",
                                     "route_budget", "malformed_reply", "pause_final",
                                     "pause_header", "pause_conflict", "pause_in_body", "pause_at_cap",
                                     "requires_nonce"])
def test_taskless_resume_recovers_exhausted_blank_route_from_exact_report(tmp_path, guard, route_label):
    store, state, worker = _temporary_task(tmp_path, task_id="temp-exhausted-report-route")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, TemporaryWorkflowActions()))
    ledger = RequestLedger(hop["ledger_path"])
    receipt = SendReceipt(
        hop["prompt"], prompt_digest(hop["prompt"]),
        PageBinding("temporary-alpha-plan", hop["physical_role"]),
        MessageBaseline(frozenset(), frozenset(), frozenset(), frozenset()),
        1, "user_message_identity", None, "accepted-user", "accepted-turn",
    )
    ledger.begin(role=hop["physical_role"], prompt=hop["prompt"], request_id=hop["request_id"],
                 render_request_marker=False)
    ledger.update(hop["request_id"], status=RequestStatus.SENDING, attempts=1,
                  binding=receipt.binding, baseline=receipt.baseline)
    ledger.update(hop["request_id"], status=RequestStatus.SENT, accepted_at=1.0,
                  receipt=receipt.to_dict())
    hop.update(state="waiting", receipt=receipt.to_dict())
    state.update(status="BLOCKED", kanban_column="BLOCKED", active_action="blocked",
                 block_code="ROUTE_VALIDATION_EXHAUSTED", block_retryable=False,
                 route_repair_count=state["max_route_repairs"])
    hop.update(kind="route_repair", repair_attempt=state["max_route_repairs"])
    if guard != "requires_nonce":
        hop.pop("report_result")  # These legacy turns predate the report footer contract.
    hop["wait"]["controller_reason"] = (
        "malformed_result" if guard == "malformed_reply" else "missing_terminal_route_repair"
    )
    report = tmp_path / hop["expected_report_path"]
    report.parent.mkdir(parents=True, exist_ok=True)
    route = {
        "unselected_route": "UNKNOWN", "terminal_route": "DONE", "pause_final": "PAUSE",
        "pause_at_cap": "PAUSE",
    }.get(guard, "DEV")
    title = "# Completed report\n\n"
    pause_line = f"**{route_label}:** `PAUSE`; waiting for external evidence.\n"
    prerequisite = "\n**block_code:** `SOURCE_UNAVAILABLE`\n\n**resume_condition:** Authorized access restored.\n"
    target = route if route_label == "Route verdict" else f"**{route}**"
    final_route = f"**{route_label}:** {target}. Ready for independent checks.\n"
    body = title + {
        "pause_header": "**Role:** PLAN\n" + pause_line + prerequisite,
        "pause_conflict": pause_line + prerequisite + "\nRoute: DEV\n",
        "pause_in_body": "## Examples\n\n```md\n" + pause_line + "```\n" + prerequisite,
        "conflicting_route": "Route: REVIEW\n\n" + final_route,
        "nonfinal_route": final_route + "\nMore notes follow.\n",
    }.get(guard, f"**Role:** PLAN | **Route:** {route} (independent checks).\n\n" + final_route)
    if guard != "missing_report":
        report.write_text(body, encoding="utf-8")
    if guard == "receipt_mismatch":
        ledger.update(hop["request_id"], receipt=None)
    if guard in {"route_budget", "pause_at_cap"}:
        state.update(route_seq=8, max_routes=8)
    ledger_before = Path(hop["ledger_path"]).read_bytes()
    state = store.save(path, state)
    if guard == "malformed_reply":
        assert not workflow_control_eligibility(state, "resume")["eligible"]
        return
    queued = store.resume_team(state["team"])
    control = queued["controls"][-1]
    saved = asyncio.run(worker.advance(
        path, None, scheduling_tasks=[queued],
        expected_endpointless_control=(control["control_id"], "resume"),
    ))
    assert Path(hop["ledger_path"]).read_bytes() == ledger_before
    assert saved["max_route_repairs"] == state["max_route_repairs"]
    assert saved["task_id"] == state["task_id"]
    if guard not in {None, "pause_header", "pause_final", "route_budget", "pause_at_cap"}:
        assert saved["active_hop_id"] == hop["hop_id"]
        assert saved["status"] == "BLOCKED"
        assert saved["block_code"] == "ROUTE_VALIDATION_EXHAUSTED"
        assert saved["controls"][-1]["status"] == "ineffective"
        assert saved["route_repair_count"] == state["max_route_repairs"]
        return
    completed = saved["hops"][0]
    assert saved["block_code"] is None
    assert saved["controls"][-1]["result"]["postcondition"] == "report_route_recovered"
    assert completed["state"] == "routed"
    assert completed["response"] is None and completed.get("response_record") is None
    assert completed["report_sha256"] == saved["reports"][-1]["sha256"]
    assert saved["route_timeline"][-1]["kind"] == "report_route_recovered"
    if guard in {"pause_header", "pause_final", "pause_at_cap"}:
        assert completed["route"] == "PAUSE"
        assert saved["status"] == "PAUSED"
        assert saved["active_hop_id"] == hop["hop_id"]
        assert len(saved["hops"]) == 1  # PAUSE must not schedule another Send.
        assert saved["route_seq"] == state["route_seq"]
        assert saved["route_repair_count"] == state["route_repair_count"]
        if guard == "pause_at_cap":
            queued = store.request_resume(path, reason="Operator resolved the prerequisite.")
            actions = TemporaryWorkflowActions(owned=True)
            assert asyncio.run(worker._apply_control(queued, actions, path))
            resumed = store.load(path)
            child = _active_hop(resumed)
            assert child["parent_hop_id"] == hop["hop_id"]
            assert child["target_role"] == "PLAN" and child["state"] == "pre_send"
            assert resumed["hops"][0]["receipt"] == receipt.to_dict()
            assert resumed["hops"][0]["report_sha256"] == completed["report_sha256"]
            assert Path(hop["ledger_path"]).read_bytes() == ledger_before
            assert ledger.peek(child["request_id"]) is None
            assert resumed["route_seq"] == resumed["max_routes"] == 8
            assert "DEV" in worker._gate_a_route_contract(resumed, "PLAN").allowed_routes
            assert actions.fresh_urls == []
        return
    assert saved["status"] == "RUNNING"
    assert completed["route"] == "DEV"
    assert saved["route_repair_count"] == 0 and saved["route_seq"] == state["route_seq"] + 1
    child = _active_hop(saved)
    assert child["parent_hop_id"] == hop["hop_id"]
    assert child["target_role"] == "DEV" and child["state"] == "pre_send"
    assert child["handoff"] == hop["expected_report_path"]
    assert ledger.peek(child["request_id"]) is None


def test_cross_endpoint_accepted_temporary_recovery_waits_for_same_team_endpoint(tmp_path: Path):
    store, state, worker, _path, hop, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id="task-temp-cross-endpoint"
    )
    state["conversation_mode"] = "temporary"
    old_endpoint = store.endpoints.resolve()["endpoint_id"]
    new_endpoint = store.endpoints.add("9202")["endpoint_id"]
    role = state["roles"]["PLAN"]
    role.update(
        browser_endpoint_id=old_endpoint,
        page_id="lost-temporary-page",
        page_url="https://chatgpt.com/c/lost-temporary-generation",
        online=False,
    )
    original_request_id = hop["request_id"]

    assert worker._prepare_cross_endpoint_recovery(
        state, new_endpoint, reason="temporary endpoint offline"
    ) is False

    assert _active_hop(state)["request_id"] == original_request_id
    assert role["browser_endpoint_id"] == old_endpoint
    assert state["status"] == "WAITING"
    assert state["waiting_code"] == "team_endpoint_unavailable"


def test_same_endpoint_pristine_temporary_resume_starts_new_temporary_generation(tmp_path: Path):
    _store, state, worker, _path, hop, ledger, _donor = _prepare_pristine_new_sending_record(
        tmp_path, task_id="task-temp-pristine-resume"
    )
    state["conversation_mode"] = "temporary"
    state["context_policy"] = "fresh"
    state.pop("bootstrap", None)
    role = state["roles"]["PLAN"]
    role["context_source"] = "fresh"
    role.pop("bootstrap_source_donor", None)
    original_request_id = hop["request_id"]

    class Actions(TemporaryWorkflowActions):
        async def branch_from_anchor(self, *_args, **_kwargs):
            raise AssertionError("temporary resume must not branch from a persistent donor")

        async def restart(self, *_args, **_kwargs):
            raise AssertionError("temporary resume must not use persistent restart")

    actions = Actions()
    control = state["controls"][-1]
    asyncio.run(worker._recover_resume_sending(state, hop, control, actions))

    current = ledger.get(original_request_id)
    assert actions.fresh_urls == [TEMPORARY_CHAT_URL]
    assert hop["request_id"] == original_request_id
    assert hop["state"] == "sending"
    assert control["status"] == "applied"
    assert state["roles"]["PLAN"]["page_url"] == TEMPORARY_CHAT_URL
    assert current is not None
    assert current.attempts == 0
    assert current.receipt is None


@pytest.mark.parametrize("page_still_open", [False, True])
def test_temporary_resume_9202_pristine_block_restores_fresh_proof(tmp_path: Path, page_still_open):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-9202-resume")
    actions = TemporaryWorkflowActions()
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, actions))
    assert hop["state"] == "sending"
    role = state["roles"]["PLAN"]
    # Reproduce the persisted 9202 values and the Gate C failure, not a URL variant.
    hop.update(conversation_generation=0, generation_started_here=False)
    role.update(conversation_generation=0, constructor_sent_generation=0)
    original_identity = (hop["hop_id"], hop["turn"], hop["request_id"], hop["prompt_sha256"])
    live = TemporaryWorkflowActions(owned=True)
    asyncio.run(worker._sending(state, hop, live))
    assert state["status"] == "BLOCKED"
    assert state["block_reason"] == (
        "CONVERSATION_IDENTITY_MISMATCH: temporary chat requires an exact verified Temporary Chat URL"
    )
    assert role["page_url"] == TEMPORARY_CHAT_URL
    assert not Path(hop["ledger_path"]).exists()
    assert worker.runtime_db.get_command_by_idempotency_key(f"browser-send:{hop['request_id']}") is None

    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    client.current = replace(client.current, url=TEMPORARY_CHAT_URL, session_id=None)

    class Actions(TemporaryWorkflowActions):
        async def locate_owned(self, state, logical_role):
            acquired = await super().locate_owned(state, logical_role)
            return replace(acquired, client=client) if acquired else None

    replacement = Actions(owned=page_still_open)
    control = {"action": "resume", "status": "recovering"}

    async def resume():
        try:
            await worker._recover_resume_sending(state, hop, control, replacement)
        finally:
            worker._release_endpoint_admission()

    asyncio.run(resume())
    assert control["status"] == "applied"
    assert replacement.fresh_urls == [TEMPORARY_CHAT_URL]
    assert hop["generation_started_here"] is True
    assert (hop["hop_id"], hop["turn"], hop["request_id"], hop["prompt_sha256"]) == original_identity
    assert not Path(hop["ledger_path"]).exists()

    # The next worker tick finds the replacement by ownership; lookup does not
    # label the page new again. The persisted proof must reach the writer gate.
    acquired = asyncio.run(live.locate_owned(state, "PLAN"))
    assert acquired.new_chat is False
    _writer, binding, attempt = asyncio.run(
        worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired)
    )
    assert binding["conversation_url"] == TEMPORARY_CHAT_URL
    assert binding["verified_new_chat"] is True
    assert attempt["state"] == "intent_recorded"


@pytest.mark.parametrize(
    "page_url, can_send",
    [
        (TEMPORARY_CHAT_URL, True),
        ("https://chatgpt.com/", False),
        ("https://chatgpt.com/?temporary-chat=false", False),
        (TEMPORARY_CHAT_URL + "&temporary-chat=false", False),
        ("https://chatgpt.com/c/foreign?temporary-chat=true", False),
    ],
)
def test_temporary_send_verifies_url_after_owned_tab_lookup(
    tmp_path: Path, monkeypatch, page_url, can_send
):
    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-send")
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, TemporaryWorkflowActions()))
    assert hop["state"] == "sending"
    role = state["roles"]["PLAN"]
    assert role["page_url"] == TEMPORARY_CHAT_URL

    class Page(FakePage):
        async def evaluate(self, script, *args):
            if args:
                return role["temporary_document_id"]
            return await super().evaluate(script)

    page = Page(
        page_id=role["page_id"], role=role["physical_role"],
        team=state["team"], task_id=state["task_id"],
    )
    page.url = page_url
    client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
    client.page = page
    monkeypatch.setattr(actions_module, "ChatGPTPage", lambda *_a, **_kw: client)
    actions = CDPATabActions(FakeContext([page]), worker.config)

    asyncio.run(worker._sending(state, hop, actions))

    if can_send:
        assert state.get("block_code") is None
        assert hop["state"] == "sent"
        assert len(client.send_calls) == 1
        assert client.send_max_attempts == [1]
        assert client.send_recovery_reload == [False]
    else:
        assert state["status"] == "BLOCKED"
        assert state["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
        assert not client.set_calls
        assert not client.send_calls
        assert worker.runtime_db.get_command_by_idempotency_key(
            f"browser-send:{hop['request_id']}"
        ) is None



@pytest.mark.parametrize(
    "loss", ["tab_closed", "document_reloaded", "already_blocked", "child_intent_exists"],
)
def test_temporary_dev_generation_loss_uses_new_writer_without_replaying(tmp_path, loss):
    store, state, worker = _temporary_task(tmp_path, task_id="task-temp-dev-recovery")

    class Actions(TemporaryWorkflowActions):
        current = None
        document = None

        async def locate_owned(self, state, role):
            if self.current is not None:
                return replace(self.current, new_chat=False, created=False)
            return None

        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            assert url == TEMPORARY_CHAT_URL and clean_for_send
            self.fresh_urls.append(url)
            page_id = f"dev-page-{len(self.fresh_urls)}"
            self.document = f"document-{len(self.fresh_urls)}"
            client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
            client.current = replace(client.current, url=url, session_id=None)

            async def evaluate(*_args):
                return self.document

            client.page = SimpleNamespace(evaluate=evaluate)
            client.binding = replace(client.binding, page_id=page_id, role=state["roles"][role]["physical_role"])
            self.current = AcquiredRole(client=client, page_id=page_id, url=url, created=True, new_chat=True)
            return self.current

    async def run():
        actions = Actions()
        # Reproduce the live shape: earlier workflow progress, an accepted DEV
        # repair on generation 1, and its own DEV turn 2 continuation request.
        plan = _active_hop(state)
        plan.update(state="routed", route="DEV")
        dev = worker._append_hop(state, source_role="PLAN", target_role="DEV", handoff="do work")
        dev.update(state="routed", route="PLAN")
        parent = worker._append_hop(
            state, source_role="DEV", target_role="DEV", handoff="route repair",
            kind="route_repair", turn=1,
        )
        role = state["roles"]["DEV"]
        await worker._pre_send(state, parent, actions)
        await worker._sending(state, parent, actions)
        assert parent["state"] == "sent"
        original = actions.current
        parent["state"] = "waiting"
        ledger = RequestLedger(parent["ledger_path"])
        accepted = ledger.peek(parent["request_id"])
        assert accepted.status is RequestStatus.SENT and accepted.attempts == 1
        assert accepted.receipt["conversation_id"] is None
        old_binding = parent["conversation_binding_id"]
        original_document = role["temporary_document_id"]
        if loss == "document_reloaded":
            actions.document = "reloaded-document"
        else:
            actions.current = None
        await worker._waiting(state, parent, actions, Path(state["manifest_path"]))
        child = _active_hop(state)
        assert parent["state"] == "abandoned"
        assert child["recovery_of_request_id"] == parent["request_id"]
        assert (child["hop_id"], child["turn"], child["state"]) == (4, 2, "pre_send")
        # Keep the test on the actual Send implementation, but use the same
        # fresh-worker boundary as guarded reload (no preceding local Send).
        sender = CDPAWorker(worker.config, store=store)
        await sender._pre_send(state, child, actions)
        assert child["generation_started_here"] and child["conversation_generation"] == 2
        assert role["temporary_document_id"] != original_document
        if loss in {"already_blocked", "child_intent_exists"}:
            # Emulate the snapshot left by the old code, including the stale
            # binding, but with no irreversible evidence for the child itself.
            role["conversation_binding_id"] = old_binding
            prepared_page = await actions.locate_owned(state, "DEV")
            await sender._sending(state, child, actions)
            assert state["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
            assert ledger.peek(child["request_id"]) is None
            actions.current = None  # existing blocked-tab cleanup closed it
            if loss == "child_intent_exists":
                # Simulate a writer-ledger save gap. The child must stay blocked
                # even with no requests.json entry and no attempt in its manifest.
                role.pop("conversation_binding_id")
                replacement = await sender._gate_c_prepare_browser_send(
                    state, child, "DEV", prepared_page,
                )
                assert replacement[2]["state"] == "intent_recorded"
                for key in ("send_attempt_id", "conversation_binding_id", "writer_lease_epoch"):
                    child.pop(key)
                role["conversation_binding_id"] = old_binding
            control = {"action": "resume", "status": "recovering"}
            try:
                await sender._recover_resume_sending(state, child, control, actions)
            finally:
                sender._release_endpoint_admission()
            assert ledger.peek(child["request_id"]) is None
            if loss == "child_intent_exists":
                assert control["status"] == "recovery_required"
                assert state["block_code"] == "durable_send_provenance_missing"
                assert actions.fresh_urls == [TEMPORARY_CHAT_URL] * 2
                assert not prepared_page.client.set_calls and not prepared_page.client.send_calls
                assert ledger.peek(parent["request_id"]) == accepted
                return
            assert control["status"] == "applied"
        await sender._sending(state, child, actions)
        assert state["block_code"] is None, state["block_reason"]
        assert child["state"] == "sent"
        assert actions.fresh_urls == [TEMPORARY_CHAT_URL] * (3 if loss == "already_blocked" else 2)
        new_binding = ConversationWriterLease(sender.runtime_db).get_binding(child["conversation_binding_id"])
        assert new_binding["binding_id"] != old_binding
        assert new_binding["verified_new_chat"] is True
        assert new_binding["page_target_identity"] == actions.current.page_id
        assert new_binding["temporary_document_id"] == role["temporary_document_id"]
        assert ledger.peek(parent["request_id"]) == accepted
        sent = ledger.peek(child["request_id"])
        assert sent.status is RequestStatus.SENT and sent.attempts == 1
        assert len(original.client.send_calls) == len(actions.current.client.send_calls) == 1
        assert actions.current.client.send_max_attempts == [1]
        assert actions.current.client.send_recovery_reload == [False]

    asyncio.run(run())


@pytest.mark.parametrize("intent_state", ["intent_recorded", "submitted", "accepted"])
def test_temporary_model_gate_resume_preserves_exact_request(tmp_path, intent_state):
    store, state, worker = _temporary_task(tmp_path, task_id="task-temp-model-gate")
    hop = _active_hop(state)
    request_id = hop["request_id"]
    key = f"browser-send:{request_id}"

    class Actions(TemporaryWorkflowActions):
        current = None

        async def locate_owned(self, _state, _role):
            if self.current:
                return replace(self.current, created=False, new_chat=False)
            return None

        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            assert url == TEMPORARY_CHAT_URL and clean_for_send
            self.fresh_urls.append(url)
            index = len(self.fresh_urls)
            client = RecordingCDPASendClient(task_id=state["task_id"], team=state["team"])
            client.current = replace(client.current, url=url, session_id=None)
            client.binding = replace(client.binding, page_id=f"model-tab-{index}", role=hop["physical_role"])

            async def evaluate(*_args):
                return f"model-doc-{index}"

            client.page = SimpleNamespace(evaluate=evaluate)
            if index == 1:
                async def block_set_text(_text):
                    raise UnsafePageStateError("model_verification_blocked: exact task tab does not show High/Extra High/Pro")
                client.set_text = block_set_text
            self.current = AcquiredRole(client, client.binding.page_id, url, True, True)
            return self.current

    async def run():
        actions = Actions()
        await worker._pre_send(state, hop, actions)
        # Save gap: the old durable writer intent exists, but the manifest's
        # attempt and binding fields were rolled back when the gate raised.
        before = dict(hop)
        with pytest.raises(UnsafePageStateError, match="model_verification_blocked") as error:
            await worker._sending(state, hop, actions)
        original = actions.current.client
        hop.clear()
        hop.update(before)
        state["roles"]["PLAN"].pop("conversation_binding_id", None)
        worker._block(state, error.value, code="unexpected_error", retryable=False)
        actions.current = None  # the worker closed the blocked pristine tab
        row = worker.runtime_db.get_command_by_idempotency_key(key)
        writers = ConversationWriterLease(worker.runtime_db)
        if intent_state != "intent_recorded":
            # Contradictory writer evidence must not be erased by a NEW ledger.
            writers.transition_attempt(row["command_id"], intent_state)
        ledger = RequestLedger(hop["ledger_path"])
        assert ledger.peek(request_id).status is RequestStatus.NEW
        resumed_worker = CDPAWorker(worker.config, store=store)
        control = {"action": "resume", "status": "recovering"}
        try:
            await resumed_worker._recover_resume_sending(state, hop, control, actions)
        finally:
            resumed_worker._release_endpoint_admission()
        old = worker.runtime_db.get_command(row["command_id"])["payload"]["send_attempt"]
        assert not original.send_calls
        if intent_state != "intent_recorded":
            assert control["status"] == "recovery_required"
            assert len(actions.fresh_urls) == 1
            assert old["state"] == intent_state
            assert ledger.peek(request_id).attempts == 0
            return
        assert control["status"] == "applied"
        assert len(actions.fresh_urls) == 2
        assert old["state"] == "reconciled_absent"
        assert writers.get_binding(old["owner_binding_id"])["binding_status"] == "sealed"
        await resumed_worker._sending(state, hop, actions)
        assert state["block_code"] is None, state["block_reason"]
        assert (hop["hop_id"], hop["request_id"], hop["state"]) == (1, request_id, "sent")
        record = ledger.peek(request_id)
        assert record.status is RequestStatus.SENT and record.attempts == 1 and record.receipt
        assert len(actions.current.client.send_calls) == 1
        new = worker.runtime_db.get_command(hop["send_attempt_id"])["payload"]["send_attempt"]
        assert new["state"] == "accepted" and new["owner_binding_id"] != old["owner_binding_id"]

    asyncio.run(run())


def test_temporary_fresh_chat_goto_timeout_is_pre_send_not_blocked(tmp_path: Path):
    """Fix D (temporary branch): fresh_chat TimeoutError for temporary workflow stays pre-Send."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright_auto.durable import RequestLedger

    _store, state, worker = _temporary_task(tmp_path, task_id="task-temp-fresh-timeout")
    hop = _active_hop(state)
    request_id = hop["request_id"]

    class TimeoutOnFresh(TemporaryWorkflowActions):
        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            raise PlaywrightTimeoutError("Page.goto timed out after 30000ms")

    asyncio.run(worker._pre_send(state, hop, TimeoutOnFresh()))

    assert hop.get("state") != "sending"
    ledger = RequestLedger(hop["ledger_path"])
    record = ledger.get(request_id)
    assert record is None or int(record.attempts or 0) == 0
    if state.get("status") == "BLOCKED":
        assert state.get("block_code") not in {None, "unexpected_error"}, (
            f"Temporary fresh_chat timeout must not classify as unexpected_error"
        )


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("operator_resume", [False, True])
def test_closed_unbound_fresh_page_retries_the_same_presend_request(tmp_path, recover, operator_resume):
    from playwright_auto.workspace import FreshPageReadinessTimeout

    _store, state, worker = _temporary_task(tmp_path, task_id="task-closed-fresh-page")
    hop = _active_hop(state)
    request_id = hop["request_id"]
    calls = []

    class FreshReadinessActions(TemporaryWorkflowActions):
        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            calls.append(url)
            if recover and len(calls) == 2:
                return await super().fresh_chat(state, role, url=url, clean_for_send=clean_for_send)
            raise FreshPageReadinessTimeout("composer absent before binding")

    actions = FreshReadinessActions()
    control = {"action": "resume", "status": "recovering", "role": "PLAN"}

    async def resume():
        try:
            await worker._recover_resume_control(state, control, actions)
        finally:
            worker._release_endpoint_admission()

    def advance():
        if operator_resume:
            asyncio.run(resume())
        else:
            asyncio.run(worker._pre_send(state, hop, actions))

    advance()
    assert state["status"] == "RUNNING" and hop["state"] == "pre_send"
    assert control["status"] == "recovering"
    assert hop["wait"]["pre_send_retry_at"]
    assert hop.get("preboundary_recovery") is None
    advance()
    assert len(calls) == 1
    for _ in range(1 if recover else 2):
        hop["wait"]["pre_send_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        advance()
    assert hop["request_id"] == request_id
    assert RequestLedger(hop["ledger_path"]).peek(request_id) is None
    if recover:
        assert hop["state"] == "sending"
        assert state["status"] == "RUNNING"
        if operator_resume:
            assert control["status"] == "applied"
            assert control["result"]["postcondition"] == "hop_advanced"
    else:
        assert hop["state"] == "pre_send"
        assert state["block_code"] == "fresh_page_readiness_timeout"
        assert len(calls) == 3
        if operator_resume:
            assert control["status"] == "recovery_required"


def test_explicit_resume_renews_only_closed_fresh_page_retry_budget(tmp_path):
    from playwright_auto.workspace import FreshPageReadinessTimeout

    store, state, worker = _temporary_task(tmp_path, task_id="temp-fresh-page-resume")
    path = Path(state["manifest_path"])
    hop = _active_hop(state)
    request_id = hop["request_id"]
    hop["wait"]["fresh_readiness_failures"] = 2
    worker._handle_pre_send_acquisition_timeout(state, hop, FreshPageReadinessTimeout("navigation"))
    assert state["block_code"] == "fresh_page_readiness_timeout"
    store.save(path, state)
    state = store.resume_team(state["team"])

    class Actions(TemporaryWorkflowActions):
        calls = 0

        async def fresh_chat(self, state, role, *, url, clean_for_send=False):
            self.calls += 1
            if self.calls == 1:
                raise FreshPageReadinessTimeout("navigation")
            return await super().fresh_chat(state, role, url=url, clean_for_send=clean_for_send)

    async def run():
        actions = Actions()
        await worker._apply_control(state, actions)
        control = state["controls"][-1]
        hop = _active_hop(state)
        try:
            await worker._recover_resume_control(state, control, actions)
            assert state["status"] == "RUNNING"
            assert control["status"] == "recovering"
            assert hop["wait"]["fresh_readiness_failures"] == 1
            hop["wait"]["pre_send_retry_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
            await worker._recover_resume_control(state, control, actions)
        finally:
            worker._release_endpoint_admission()
        assert (hop["hop_id"], hop["request_id"], hop["state"]) == (1, request_id, "sending")
        assert control["status"] == "applied"
        assert not Path(hop["ledger_path"]).exists()

    asyncio.run(run())
