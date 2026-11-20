import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_actions as actions_module
from playwright_auto.cdpa_actions import AcquiredRole, CDPATabActions
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
from playwright_auto.chatgpt_errors import UnsafePageStateError
from playwright_auto.chatgpt_models import MessageSnapshot
from playwright_auto.durable import RequestLedger, RequestStatus

from test_cdpa_actions import FakeContext, FakePage
from test_cdpa_core import write_config
from test_cdpa_worker import (
    FakeActions, RecordingCDPASendClient, _prepare_sent_waiting_task, ready_client,
)
from test_operator_resume_recovery import _prepare_pristine_new_sending_record


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
    state["conversation_mode"] = "temporary"
    state["context_policy"] = "fresh"
    if parent_kind == "route_repair":
        state["route_repair_count"] = 1
        hop.update(kind=parent_kind, repair_attempt=1)
    else:
        hop["kind"] = parent_kind
    report = tmp_path / hop["expected_report_path"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Completed report\n\nRoute requested: DEV\n", encoding="utf-8")
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
        hop["wait"]["controller_reason"] = "response_terminal_missing"
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
    assert str(report) in repair["prompt"]
    assert "terminal result" in repair["prompt"]
    assert state["task_text"] not in repair["prompt"]
    assert ledger.peek(hop["request_id"]) == accepted


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
