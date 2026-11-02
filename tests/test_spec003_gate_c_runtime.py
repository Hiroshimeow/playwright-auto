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


def test_t076_reserved_async_wait_cannot_record_new_send_intent(writers):
    current = writers.acquire(bind(writers)["binding_id"])
    writers.reserve_async_wait("one")
    with pytest.raises(ValueError, match="WRITER_LEASE_LOST"):
        intent(writers, current, key="reserved-send")
    assert writers.db.get_command_by_idempotency_key("reserved-send") is None


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


def test_t078_temporary_chat_url_is_a_valid_provisional_new_chat(writers):
    url = "https://chatgpt.com/?temporary-chat=true"
    binding = writers.bind(
        binding_id="temporary-task-plan",
        provider_id="chatgpt_web",
        endpoint_id="ep-1",
        endpoint_cdp_url="http://127.0.0.1:9212",
        physical_browser_id="browser-9212",
        page_target_identity="temporary-page-1",
        conversation_url=url,
        verified_new_chat=True,
        temporary_chat=True,
        temporary_document_id="temporary-document-1",
        task_id="temporary-task",
        role_key="PLAN",
    )
    assert binding["conversation_url"] == url
    assert binding["canonical_conversation_id"] is None
    assert binding["verified_new_chat"] is True

    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.bind(
            binding_id="not-verified-temporary-task",
            provider_id="chatgpt_web",
            endpoint_id="ep-1",
            endpoint_cdp_url="http://127.0.0.1:9212",
            physical_browser_id="browser-9212",
            page_target_identity="temporary-page-2",
            conversation_url=url,
            verified_new_chat=False,
            task_id="temporary-task-2",
            role_key="PLAN",
        )


@pytest.mark.parametrize("divert", [None, "wrong_url", "wrong_page"])
def test_t078_team_adopted_start_role_keeps_exact_page_before_send(tmp_path, divert):
    import asyncio

    from playwright_auto.cdpa_actions import AcquiredRole
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.cdpa_worker import CDPAWorker, _active_hop
    from test_cdpa_core import write_config
    from test_cdpa_worker import FakeActions, ready_client

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    endpoint_id = store.endpoints.resolve()["endpoint_id"]
    url = "https://chatgpt.com/c/adopted-team"
    state = store.create_task(
        "Follow an adopted conversation",
        requested_team="adopted-team",
        task_id=f"task-adopted-{divert or 'exact'}",
        roles=("PLAN", "DEV"),
        browser_endpoint_id=endpoint_id,
        conversation_url=url,
    )
    worker = CDPAWorker(config, store=store)
    assert worker._prepare_execution_endpoint(state, endpoint_id)

    class Actions(FakeActions):
        def __init__(self):
            super().__init__()
            self.adopted = []
            self.acquire_calls = []

        async def adopt_conversation(self, current, role, *, url):
            self.adopted.append((role, url))
            client = ready_client(current, role)
            if divert == "wrong_page":
                client.binding = SimpleNamespace(page_id="other-page")
            return AcquiredRole(
                client, "adopted-page",
                "https://chatgpt.com/c/other" if divert == "wrong_url" else url,
                True, False,
            )

        async def acquire(self, current, role, **_kwargs):
            self.acquire_calls.append(role)
            return AcquiredRole(
                ready_client(current, role),
                "other-page" if divert == "wrong_page" else "adopted-page",
                "https://chatgpt.com/c/other" if divert == "wrong_url" else url,
                False,
                False,
            )

    actions = Actions()
    hop = _active_hop(state)
    asyncio.run(worker._pre_send(state, hop, actions))
    assert actions.adopted == [("PLAN", url)]
    assert not actions.fresh_calls
    assert state["roles"]["DEV"]["page_id"] is None
    if divert is None:
        assert hop["state"] == "sending"
        assert state["roles"]["PLAN"]["page_id"] == "adopted-page"
        assert state["roles"]["PLAN"]["page_url"] == url
    else:
        assert state["status"] == "BLOCKED", (state["status"], state.get("block_code"), hop["state"], actions.acquire_calls)
        assert state["block_code"] == "CONVERSATION_IDENTITY_MISMATCH"
        assert hop["state"] == "pre_send"
        assert state["roles"]["PLAN"]["page_id"] is None
        from playwright_auto.durable import RequestLedger
        assert RequestLedger(hop["ledger_path"]).peek(hop["request_id"]) is None


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


def test_t081_dom_stream_projects_authoritative_snapshot_without_page_access():
    from playwright_auto.cdpa_dom_stream import ACTIVITY_SELECTORS, project_dom_snapshot
    from playwright_auto.chatgpt import MessageSnapshot
    from test_operational_contract import snapshot

    current = snapshot(
        messages=(
            MessageSnapshot("user", "u1", "ut1", "question", ()),
            MessageSnapshot("assistant", "a1", "at1", "answer", ("Copy",)),
        ),
        response_activity_text="Reading tool output",
        response_activity_structure="agent-activity tool",
        response_activity_turn_id="at1",
        error_texts=("Transient warning",),
    )
    projected = project_dom_snapshot(current)
    kinds = [node["kind"] for node in projected["nodes"]]
    assert kinds == ["user_message", "assistant_text", "tool_activity", "error_banner"]
    assert projected["active"] is True
    assert ".cadencedShimmerSweep-ICUAVH" in ACTIVITY_SELECTORS
    assert ".group/activity-header" in ACTIVITY_SELECTORS

    terminal = project_dom_snapshot(
        snapshot(
            messages=(
                MessageSnapshot("assistant", "a2", "at2", "done", ("Copy",)),
            )
        )
    )
    assert terminal["nodes"][-1]["kind"] == "terminal"
    assert terminal["active"] is False


def test_t074_duplicate_endpoint_add_reuses_identical_and_rejects_conflict(tmp_path):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
    from test_cdpa_core import write_config

    registry = BrowserEndpointRegistry(
        load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    )
    first = registry.add("9202", label="Peer")
    again = registry.add("http://127.0.0.1:9202/", label="Peer")
    assert again["endpoint_id"] == first["endpoint_id"]
    with pytest.raises(ValueError, match="different settings|conflicting"):
        registry.add("127.0.0.1:9202", label="Different")


def test_t074_endpoint_update_and_remove_respect_unsealed_binding(tmp_path):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_conversations import ConversationWriterLease
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.cdpa_worker import CDPAWorker
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    worker = CDPAWorker(config, store=TaskStore(config))
    worker.runtime_db.ensure_schema()
    endpoint = worker.store.endpoints.resolve()
    lease = ConversationWriterLease(worker.runtime_db)
    lease.bind(
        binding_id="bound-session",
        provider_id="chatgpt_web",
        endpoint_id=endpoint["endpoint_id"],
        endpoint_cdp_url=endpoint["cdp_url"],
        physical_browser_id="physical-1",
        page_target_identity="target-1",
        conversation_url="https://chatgpt.com/c/existing",
        verified_new_chat=False,
    )

    with pytest.raises(ValueError, match="binding|drain|retarget"):
        worker._apply_browser_endpoint_update({
            "endpoint_id": endpoint["endpoint_id"],
            "cdp_url": "http://127.0.0.1:9333",
        })
    draining = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint["endpoint_id"]})
    assert draining["draining"] is True and draining["removed"] is False
    lease.close_binding("bound-session")
    removed = worker._apply_browser_endpoint_remove({"endpoint_id": endpoint["endpoint_id"]})
    assert removed["removed"] is True


@pytest.mark.parametrize(
    ("hop_state", "receipt"),
    [
        ("pre_send", None),
        ("sent", None),
        ("pre_send", {"request_id": "accepted-elsewhere"}),
    ],
)
def test_t093_provider_source_registration_requires_accepted_parent_turn(hop_state, receipt):
    from playwright_auto import cdpa_async_continuation as continuation

    state = {
        "task_id": "task-await-source",
        "status": "RUNNING",
        "workflow_mode": "team",
        "route_contract_id": "contract-1",
        "active_role": "DEV",
        "active_hop_id": 1,
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "state": hop_state,
                "receipt": receipt,
            }
        ],
        "roles": {"DEV": {}},
    }
    with pytest.raises(continuation.AsyncContinuationError) as caught:
        continuation.register_source(
            state,
            continuation_id="ac-parent-gate",
            role="DEV",
            source_kind="provider_job",
            source_refs=("pjob-1",),
            failure_policy="block",
            now="2026-10-27T00:00:00+00:00",
        )
    assert caught.value.code == "ASYNC_SPAWN_REJECTED"
    assert continuation.wait_admissible(state, "DEV") is False
    assert not state.get("async_continuations")


def test_t094_reserved_async_wait_fences_every_other_writer_and_preserves_epoch(writers):
    binding = bind(writers, task="task-wait")
    active = writers.acquire(binding["binding_id"])
    reserved = writers.reserve_async_wait(binding["binding_id"])
    assert reserved["lease_epoch"] == active["lease_epoch"]
    assert writers.get_binding(binding["binding_id"])["binding_status"] == "reserved_async_wait"

    with pytest.raises(ValueError, match="WRITER_LEASE_RESERVED"):
        writers.acquire(binding["binding_id"])
    with pytest.raises(ValueError, match="WRITER_LEASE_BUSY"):
        writers.acquire(binding["binding_id"], require_idle=True)

    resumed = writers.resume_async_wait(binding["binding_id"])
    assert resumed["lease_epoch"] == active["lease_epoch"]
    assert writers.get_binding(binding["binding_id"])["binding_status"] == "active"
    writers.release_async_wait(binding["binding_id"])
    assert writers.get_binding(binding["binding_id"])["binding_status"] == "idle"


def test_t084_send_attempt_metadata_persists_receipt_but_cannot_replace_identity(writers):
    lease = writers.acquire(bind(writers)["binding_id"])
    attempt, _ = intent(writers, lease)
    attempt = writers.transition_attempt(
        attempt["attempt_id"],
        "accepted",
        metadata={"receipt": {"accepted": True}, "accepted_at": "2026-10-26T00:00:00+00:00"},
    )
    assert attempt["receipt"] == {"accepted": True}
    with pytest.raises(ValueError, match="SEND_ATTEMPT_INVALID"):
        writers.transition_attempt(
            attempt["attempt_id"],
            "accepted",
            metadata={"lease_epoch": 999},
        )



def test_t081_dom_stream_projects_all_activity_kinds_and_selector_metadata():
    from playwright_auto.cdpa_dom_stream import ACTIVITY_SELECTORS, project_dom_snapshot
    from test_operational_contract import snapshot

    cases = (
        ("agent-activity tool", "tool_activity"),
        ("cadenced shimmer thinking", "visible_thinking"),
        ("plain progress text", "progress"),
    )
    for structure, expected in cases:
        projected = project_dom_snapshot(
            snapshot(
                response_activity_text="working",
                response_activity_structure=structure,
                response_activity_turn_id="turn-active",
            )
        )
        assert projected["nodes"][-1]["kind"] == expected
        assert projected["active"] is True

    assert set(ACTIVITY_SELECTORS) == {
        ".group/activity-header",
        '[class*="agent-activity"]',
        '[class*="activity-header"]',
        ".summary-lK7Lpm",
        ".cadencedShimmerSweep-ICUAVH",
        ".cadencedShimmerHighlight-VcX29h",
    }


def test_t088_dom_stream_restart_resets_epoch_and_replays_full_projection(tmp_path):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_conversations import ConversationWriterLease
    from playwright_auto.dashboard_api import DashboardAPI
    from test_cdpa_core import write_config

    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    api = DashboardAPI(config)
    writer = ConversationWriterLease(api.db)
    writer.bind(
        binding_id="stream-session",
        provider_id="chatgpt_web",
        endpoint_id="ep-stream",
        endpoint_cdp_url="http://127.0.0.1:9222",
        physical_browser_id="browser-stream",
        page_target_identity="page-stream",
        conversation_url="https://chatgpt.com/c/stream",
        verified_new_chat=False,
    )
    projection = {
        "page_id": "page-stream",
        "url": "https://chatgpt.com/c/stream",
        "active": True,
        "nodes": [{"kind": "progress", "key": "progress:1", "text": "working"}],
    }
    writer.update_dom_projection("stream-session", projection)
    first = api.session_dom_state("stream-session")
    again = api.session_dom_state("stream-session")
    assert first["seq"] == 1
    assert again["seq"] == first["seq"]
    assert again["stream_epoch"] == first["stream_epoch"]
    assert again["projection"] == projection

    projection2 = {**projection, "nodes": [{"kind": "terminal", "key": "terminal:1", "text": "done"}], "active": False}
    writer.update_dom_projection("stream-session", projection2)
    changed = api.session_dom_state("stream-session")
    assert changed["seq"] == 2
    assert changed["stream_epoch"] == first["stream_epoch"]
    assert changed["projection"] == projection2

    restarted = DashboardAPI(config)
    try:
        after_restart = restarted.session_dom_state("stream-session")
        assert after_restart["stream_epoch"] != first["stream_epoch"]
        assert after_restart["seq"] == 1
        assert after_restart["projection"] == projection2
    finally:
        restarted.db.close()
        api.db.close()


def test_t089_temporary_chat_gate_c_uses_document_scoped_writer_identity(tmp_path):
    import asyncio

    from playwright_auto.cdpa_conversations import ConversationWriterLease
    from playwright_auto.cdpa_runtime_db import RuntimeDB
    from playwright_auto.cdpa_worker import CDPAWorker

    db = RuntimeDB(tmp_path / "temporary.sqlite3")
    db.ensure_schema()
    writer = ConversationWriterLease(db)

    class Endpoints:
        @staticmethod
        def get(_endpoint_id):
            return {"endpoint_id": "ep-temp", "cdp_url": "http://127.0.0.1:9212"}

    class Client:
        @staticmethod
        async def assert_ownership():
            return SimpleNamespace(messages=[])

    worker = object.__new__(CDPAWorker)
    worker.runtime_db = db
    worker.store = SimpleNamespace(endpoints=Endpoints())
    worker._endpoint_pool = None

    state = {
        "task_id": "temporary-task",
        "conversation_mode": "temporary",
        "roles": {
            "PLAN": {
                "conversation_generation": 2,
                "temporary_document_id": "temporary-document-2",
            }
        },
    }
    hop = {
        "browser_endpoint_id": "ep-temp",
        "request_id": "temporary-request-1",
        "prompt": "research prompt",
    }
    acquired = SimpleNamespace(
        page_id="page-temp",
        url="https://chatgpt.com/?temporary-chat=true",
        new_chat=True,
        client=Client(),
    )

    try:
        _writer, lease, attempt = asyncio.run(
            worker._gate_c_prepare_browser_send(state, hop, "PLAN", acquired)
        )
        binding_id = state["roles"]["PLAN"]["conversation_binding_id"]
        binding = writer.get_binding(binding_id)

        assert lease["writer_identity_key"].startswith("temporary:")
        assert binding["canonical_conversation_id"] is None
        assert binding["temporary_chat"] is True
        assert binding["temporary_document_id"] == "temporary-document-2"
        assert attempt["state"] == "intent_recorded"
        assert hop["send_attempt_id"] == attempt["attempt_id"]

        worker._gate_c_mark_send_accepted(
            state, hop, "PLAN", "https://chatgpt.com/?temporary-chat=true"
        )
        accepted = writer.get_binding(binding_id)
        assert accepted["canonical_conversation_id"] is None
        assert accepted["temporary_chat"] is True
        assert writer.db.get_command(attempt["attempt_id"])["payload"]["send_attempt"]["state"] == "accepted"
    finally:
        db.close()


def test_t089_temporary_binding_rejects_changed_document_identity(writers):
    binding = writers.bind(
        binding_id="temporary-one",
        provider_id="chatgpt_web",
        endpoint_id="ep-temp",
        endpoint_cdp_url="http://127.0.0.1:9212",
        physical_browser_id="browser-temp",
        page_target_identity="page-temp",
        conversation_url="https://chatgpt.com/?temporary-chat=true",
        verified_new_chat=True,
        temporary_chat=True,
        temporary_document_id="document-one",
        task_id="task-temp",
        role_key="PLAN",
    )

    assert binding["canonical_conversation_id"] is None
    assert binding["temporary_chat"] is True
    assert binding["temporary_document_id"] == "document-one"

    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.validate_binding(
            "temporary-one",
            {**binding, "temporary_document_id": "document-two"},
        )


def test_t089_temporary_binding_requires_exact_temporary_url_and_document_token(writers):
    arguments = {
        "binding_id": "temporary-invalid",
        "provider_id": "chatgpt_web",
        "endpoint_id": "ep-temp",
        "endpoint_cdp_url": "http://127.0.0.1:9212",
        "physical_browser_id": "browser-temp",
        "page_target_identity": "page-temp",
        "verified_new_chat": True,
        "temporary_chat": True,
        "task_id": "task-temp",
        "role_key": "PLAN",
    }

    with pytest.raises(ValueError, match="CONVERSATION_BINDING_INVALID"):
        writers.bind(
            **arguments,
            conversation_url="https://chatgpt.com/?temporary-chat=true",
        )

    with pytest.raises(ValueError, match="CONVERSATION_IDENTITY_MISMATCH"):
        writers.bind(
            **{**arguments, "binding_id": "temporary-wrong-url", "temporary_document_id": "document-one"},
            conversation_url="https://chatgpt.com/",
        )
