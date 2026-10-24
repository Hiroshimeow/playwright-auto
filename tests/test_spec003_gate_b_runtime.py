"""Spec003 Gate B entry and runtime contract regressions."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_commands import ProviderRetryIntent
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.role_runtime import Action, RoleController
from test_operational_contract import snapshot
from test_spec003_phase15_runtime import (
    _ObservedClient,
    _accepted_turn,
    _assistant_turn,
    _fake_environment,
    _receipt,
    _run,
    _store,
    _write_report,
)


def _old_retry_seen() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


def _receipt_sha256() -> str:
    payload = json.dumps(
        _receipt().to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _CrashAfterRetryClient(_ObservedClient):
    def __init__(self, db: RuntimeDB):
        super().__init__()
        self.db = db

    async def retry_generation(self, receipt, *, expected_task_id, expected_team):
        # T050: the irreversible click boundary may be crossed only after the
        # deterministic intent is durable in the existing command ledger.
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT status, kind, task_id, payload_json FROM command_queue "
                "WHERE kind='provider_retry'"
            ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "recovery_required"
        assert row["task_id"] == expected_task_id == "task-1"
        payload = json.loads(row["payload_json"])
        assert payload == {
            "accepted_user_turn": "user-original",
            "request_id": "task-1-hop1",
            "receipt_sha256": _receipt_sha256(),
            "team": expected_team,
        }
        self.retry_calls.append((receipt.user_message_id, expected_task_id, expected_team))
        raise RuntimeError("simulated process crash after Retry click")


def test_t050_retry_intent_is_durable_before_click_and_crash_recovery_never_replays(tmp_path):
    db_path = tmp_path / "runtime.sqlite3"
    db = RuntimeDB(db_path)
    db.ensure_schema()
    crashing_client = _CrashAfterRetryClient(db)
    state, hop, worker, acquired, _recorded = _fake_environment(
        crashing_client, runtime_db=db
    )
    hop["wait"]["retry_seen_at"] = _old_retry_seen()

    with pytest.raises(RuntimeError, match="simulated process crash"):
        _run(RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True))
    assert len(crashing_client.retry_calls) == 1

    # Simulate a worker/process restart from the same accepted turn before the
    # task manifest had a chance to persist the in-memory provider_retry marker.
    db.close()
    recovered_db = RuntimeDB(db_path)
    recovered_db.ensure_schema()
    recovered_client = _ObservedClient()
    recovered_state, recovered_hop, recovered_worker, recovered_acquired, _ = _fake_environment(
        recovered_client, runtime_db=recovered_db
    )
    recovered_hop["wait"]["retry_seen_at"] = _old_retry_seen()

    decision = _run(
        RoleController(),
        recovered_worker,
        recovered_state,
        recovered_hop,
        recovered_acquired,
        snapshot(retry_visible=True),
    )

    assert decision.action is Action.WAIT
    assert decision.reason == "provider_retry_reconciliation_required"
    assert recovered_client.retry_calls == []
    assert recovered_hop["wait"]["provider_retry"]["accepted_user_turn"] == "user-original"
    assert recovered_hop["wait"]["provider_retry"]["result"] == "recovery_required"
    with recovered_db.connection() as connection:
        rows = connection.execute(
            "SELECT status FROM command_queue WHERE kind='provider_retry'"
        ).fetchall()
    assert [row["status"] for row in rows] == ["recovery_required"]


def test_t059_retry_intent_reuses_command_ledger_without_becoming_replayable_queue_work(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    state = {"task_id": "task-1", "team": "team-1"}
    hop = {"request_id": "task-1-hop1"}
    intent = ProviderRetryIntent.create(
        state=state,
        hop=hop,
        receipt=_receipt(),
        accepted_user_turn="user-original",
    )

    first, created = db.reserve_action_intent(
        command_id=intent.command_id,
        idempotency_key=intent.idempotency_key,
        kind="provider_retry",
        task_id=intent.task_id,
        payload=intent.payload,
    )
    duplicate, duplicate_created = db.reserve_action_intent(
        command_id=intent.command_id,
        idempotency_key=intent.idempotency_key,
        kind="provider_retry",
        task_id=intent.task_id,
        payload=intent.payload,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate["command_id"] == first["command_id"] == intent.command_id
    assert first["status"] == "recovery_required"
    assert db.claim_next_command() is None
    assert db.requeue_running_commands() == 0
    applied = db.finish_action_intent(intent.command_id, result={"result": "progress"})
    assert applied["status"] == "applied"
    replay, replay_created = db.reserve_action_intent(
        command_id=intent.command_id,
        idempotency_key=intent.idempotency_key,
        kind="provider_retry",
        task_id=intent.task_id,
        payload=intent.payload,
    )
    assert replay_created is False
    assert replay["status"] == "applied"


def _retry_receipt_for_hop(hop):
    return _receipt(physical_role=str(hop.get("physical_role") or "DEV"))


def _reserve_retry_intent(db: RuntimeDB, state, hop):
    intent = ProviderRetryIntent.create(
        state=state,
        hop=hop,
        receipt=_retry_receipt_for_hop(hop),
        accepted_user_turn="user-original",
    )
    command, created = db.reserve_action_intent(
        command_id=intent.command_id,
        idempotency_key=intent.idempotency_key,
        kind="provider_retry",
        task_id=intent.task_id,
        payload=intent.payload,
    )
    assert command["status"] == "recovery_required"
    return intent, created


def test_t050_post_click_timeout_remains_ambiguous_in_command_ledger(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    client = _ObservedClient()
    client.retry_error = TimeoutError("simulated post-click verification timeout")
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    hop["wait"]["retry_seen_at"] = _old_retry_seen()

    decision = _run(
        RoleController(), worker, state, hop, acquired, snapshot(retry_visible=True)
    )

    assert decision.action is Action.WAIT
    assert decision.reason == "provider_retry_reconciliation_required"
    assert client.retry_calls == [("user-original", "task-1", "team-1")]
    intent = ProviderRetryIntent.create(
        state=state,
        hop=hop,
        receipt=_receipt(),
        accepted_user_turn="user-original",
    )
    command = db.get_command(intent.command_id)
    assert command is not None
    assert command["status"] == "recovery_required"
    assert command["result"]["result"] == "ambiguous"
    assert "TimeoutError" in str(command["error"])


def test_t050_recovered_ambiguous_retry_stays_gated_across_cycles_without_state_repair(tmp_path):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    db.ensure_schema()
    client = _ObservedClient()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    _reserve_retry_intent(db, state, hop)
    hop["wait"].update(
        controller_progress_at=(datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(),
        timeout_refreshed=True,
        timeout_status_checked=True,
        timeout_status="COMPLETE",
    )

    first = _run(RoleController(), worker, state, hop, acquired, snapshot())
    second = _run(RoleController(), worker, state, hop, acquired, snapshot())

    assert first.action is Action.WAIT
    assert second.action is Action.WAIT
    assert first.reason == second.reason == "provider_retry_reconciliation_required"
    assert client.retry_calls == []
    assert "repair" not in state
    command = db.get_command_by_idempotency_key(
        ProviderRetryIntent.create(
            state=state,
            hop=hop,
            receipt=_receipt(),
            accepted_user_turn="user-original",
        ).idempotency_key
    )
    assert command is not None and command["status"] == "recovery_required"


def test_t050_terminal_reconciliation_still_precedes_retry_intent(tmp_path):
    store = _store(tmp_path)
    state, hop, worker, report = _accepted_turn(
        store,
        roles=("REVIEW",),
        closing_role="REVIEW",
        task_id="task-gateb-terminal",
        team="gateb-terminal",
    )
    _write_report(report)
    text = '{"route":"DONE","handoff":"%s"}' % hop["expected_report_path"]
    current = snapshot(retry_visible=True, messages=_assistant_turn(text))
    client = _ObservedClient()
    acquired = SimpleNamespace(client=client, page_id="page-1")
    controller = RoleController()
    worker.runtime_db.ensure_schema()
    _reserve_retry_intent(worker.runtime_db, state, hop)
    hop["wait"]["retry_seen_at"] = _old_retry_seen()

    first = _run(controller, worker, state, hop, acquired, current)
    assert first.action is Action.WAIT
    hop["wait"]["result_seen_at"] = _old_retry_seen()
    accepted = _run(controller, worker, state, hop, acquired, current)

    assert accepted.action is Action.ACCEPT
    assert accepted.reason == "terminal_route_reconciled"
    assert client.retry_calls == []
    command = worker.runtime_db.get_command_by_idempotency_key(
        ProviderRetryIntent.create(
            state=state,
            hop=hop,
            receipt=_retry_receipt_for_hop(hop),
            accepted_user_turn="user-original",
        ).idempotency_key
    )
    assert command is not None and command["status"] == "recovery_required"


# T051 ----------------------------------------------------------------------------


class _PageTarget:
    def __init__(self, url="https://chatgpt.com/"):
        self.url = url


class _WakeDrivenClient(_ObservedClient):
    def __init__(self, observer, fresh_snapshot):
        super().__init__()
        self.observer = observer
        self.fresh_snapshot = fresh_snapshot
        self.wake_waits = 0
        self.snapshot_reads = 0

    async def wait_for_page_observation(self, *, timeout_ms):
        self.wake_waits += 1
        return await self.observer.wait(timeout_ms / 1000)

    async def wait_snapshot(self, _receipt, *, probe_wait_ms):
        self.snapshot_reads += 1
        return self.fresh_snapshot


def test_t051_one_page_target_reuses_one_authoritative_dom_projection_producer():
    from playwright_auto.role_runtime.page_observer import observer_for

    page = _PageTarget()
    first = observer_for(page)
    second = observer_for(page)

    assert first is second
    before = first.observation()
    after = second.observation()
    assert before == after
    assert first.page is page


def test_t051_audit_consumes_supplied_projection_without_live_page_crawler(
    tmp_path, monkeypatch
):
    from playwright_auto.hcuw_runtime import legacy_audit as legacy_audit_module
    from playwright_auto.hcuw_runtime.audit import AuditTimeline
    from playwright_auto.hcuw_runtime.config import load_runtime_config
    from playwright_auto.hcuw_runtime.legacy_audit import LegacyObservationAudit
    from playwright_auto.role_runtime import page_observer

    audit = LegacyObservationAudit(load_runtime_config(repository_root=tmp_path))
    board_projection = {
        "generation": 7,
        "counts": {"RUNNING": 1},
        "items": [
            {
                "task_id": "task-projected",
                "team": "team-projected",
                "status": "RUNNING",
                "roles": [],
            }
        ],
    }
    monkeypatch.setattr(audit, "_read_legacy_board", lambda: board_projection)
    monkeypatch.setattr(audit, "_publish_listen_source", lambda _timeline: None)

    def forbidden_live_page_crawler(*_args, **_kwargs):
        pytest.fail("audit/dashboard consumers must not create a second live-page crawler")

    monkeypatch.setattr(page_observer, "observer_for", forbidden_live_page_crawler)
    monkeypatch.setattr(
        legacy_audit_module, "observer_for", forbidden_live_page_crawler, raising=False
    )

    timeline = AuditTimeline()
    audit.refresh(timeline, frozenset({"task-projected"}))

    dom_source = next(
        item for item in timeline.snapshot()["sources"] if item["source"] == "dom"
    )
    assert dom_source["status"] == "active"
    assert dom_source["payload"]["generation"] == 7
    assert dom_source["payload"]["active_tasks"][0]["task_id"] == "task-projected"
    assert dom_source["payload"]["active_tasks"][0]["runtime_v2"] is True


def test_t051_mutation_wake_forces_fresh_dom_read_before_role_decision():
    from playwright_auto.role_runtime.page_observer import observer_for

    page = _PageTarget()
    observer = observer_for(page)
    observer._on_dom_wake()
    client = _WakeDrivenClient(observer, snapshot(stop_visible=True))
    state, hop, worker, acquired, _ = _fake_environment(client)

    decision = asyncio.run(
        RoleController().run(worker, state, hop, acquired, None, snapshot=None, wait_ms=1)
    )

    assert client.wake_waits == 1
    assert client.snapshot_reads == 1
    assert decision.action is Action.WAIT
    assert decision.reason != "valid_bound_result"


def test_t051_listen_terminal_candidate_never_decides_without_dom_terminal_evidence():
    controller = RoleController()
    wait = {
        "bound_assistant_message_id": "assistant-1",
        "bound_assistant_turn_id": "turn-assistant",
    }
    rendered = snapshot(messages=_assistant_turn(""))
    listen_candidate = _assistant_turn(
        '{"route":"DONE","handoff":".plan/foreign.md"}', actions=("Copy",)
    )[1]
    now = datetime.now(timezone.utc)

    first = controller.decide(
        rendered,
        wait,
        receipt=_receipt(),
        network_response=listen_candidate,
        validate=lambda _candidate: None,
        now=now,
    )
    second = controller.decide(
        rendered,
        wait,
        receipt=_receipt(),
        network_response=listen_candidate,
        validate=lambda _candidate: None,
        now=now + timedelta(seconds=6),
    )

    assert first.action is not Action.ACCEPT
    assert second.action is not Action.ACCEPT
    assert second.reason != "valid_bound_result"


# T052 ----------------------------------------------------------------------------


class _ProviderJobsSpy:
    def __init__(self):
        self.calls = []

    def execute(self, invocation, entry):
        from playwright_auto.cdpa_provider_runtime import ProviderResult

        self.calls.append((invocation, entry))
        return ProviderResult(
            provider_job_id=f"pjob-{invocation.provider_id}",
            provider_id=invocation.provider_id,
            status="succeeded",
        )


class _RoleControllerSpy:
    def __init__(self):
        self.calls = []

    async def run(self, worker, state, hop, acquired, actions, **kwargs):
        self.calls.append((worker, state, hop, acquired, actions, kwargs))
        return {"owner": "role_controller", "endpoint_id": acquired.endpoint_id}


def _endpoint_registry(tmp_path):
    from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry

    config = SimpleNamespace(
        repository_root=tmp_path,
        cdp_url="http://127.0.0.1:9222",
        browser_endpoints=(
            {
                "endpoint_id": "ep-9222",
                "cdp_url": "http://127.0.0.1:9222",
                "label": "FJP 9222",
                "management_mode": "external",
                "is_default": True,
            },
        ),
    )
    return BrowserEndpointRegistry(config)


def _browser_context(endpoint_id="ep-9222"):
    return {
        "worker": object(),
        "state": {"task_id": "task-browser"},
        "hop": {"target_role": "DEV"},
        "acquired": SimpleNamespace(endpoint_id=endpoint_id),
        "actions": object(),
    }


def test_t052_browser_dom_dispatch_resolves_registry_and_only_calls_role_controller(tmp_path):
    from playwright_auto.cdpa_provider_runtime import ProviderDispatcher, ProviderInvocation

    jobs = _ProviderJobsSpy()
    roles = _RoleControllerSpy()
    dispatcher = ProviderDispatcher(
        endpoints=_endpoint_registry(tmp_path), jobs=jobs, role_controller=roles
    )
    invocation = ProviderInvocation(
        provider_id="chatgpt_web",
        task_id="task-browser",
        role_key="DEV",
        task_text="browser turn",
        execution_mode="browser_dom",
        endpoint_id="ep-9222",
    )

    result = asyncio.run(dispatcher.dispatch(invocation, browser_context=_browser_context()))

    assert result == {"owner": "role_controller", "endpoint_id": "ep-9222"}
    assert len(roles.calls) == 1
    assert jobs.calls == []


@pytest.mark.parametrize("raw_key", ["cdp_url", "address", "browser"])
def test_t052_browser_dom_rejects_raw_transport_bypass_before_role_execution(tmp_path, raw_key):
    from playwright_auto.cdpa_provider_runtime import (
        ProviderDispatcher,
        ProviderInvocation,
        ProviderRuntimeError,
    )

    jobs = _ProviderJobsSpy()
    roles = _RoleControllerSpy()
    dispatcher = ProviderDispatcher(
        endpoints=_endpoint_registry(tmp_path), jobs=jobs, role_controller=roles
    )
    invocation = ProviderInvocation(
        provider_id="chatgpt_web",
        task_id="task-browser",
        role_key="DEV",
        task_text="browser turn",
        execution_mode="browser_dom",
        endpoint_id="ep-9222",
        provider_options={raw_key: "http://127.0.0.1:9333"},
    )

    with pytest.raises(ProviderRuntimeError) as caught:
        asyncio.run(dispatcher.dispatch(invocation, browser_context=_browser_context()))

    assert caught.value.code == "BROWSER_ENDPOINT_BYPASS_REJECTED"
    assert roles.calls == []
    assert jobs.calls == []


@pytest.mark.parametrize(
    ("provider_id", "execution_mode"),
    [("codex", "acp"), ("pi", "exec"), ("custom", "custom")],
)
def test_t052_nonbrowser_dispatch_uses_provider_job_path_only(tmp_path, provider_id, execution_mode):
    from playwright_auto.cdpa_provider_runtime import ProviderDispatcher, ProviderInvocation

    jobs = _ProviderJobsSpy()
    roles = _RoleControllerSpy()
    dispatcher = ProviderDispatcher(
        endpoints=_endpoint_registry(tmp_path), jobs=jobs, role_controller=roles
    )
    invocation = ProviderInvocation(
        provider_id=provider_id,
        task_id=f"task-{provider_id}",
        role_key="DEV",
        task_text="external provider work",
        execution_mode=execution_mode,
    )

    result = asyncio.run(dispatcher.dispatch(invocation))

    assert result.provider_id == provider_id
    assert result.status == "succeeded"
    assert len(jobs.calls) == 1
    assert roles.calls == []


def test_t052_unknown_or_mismatched_endpoint_fails_before_browser_execution(tmp_path):
    from playwright_auto.cdpa_provider_runtime import (
        ProviderDispatcher,
        ProviderInvocation,
        ProviderRuntimeError,
    )

    jobs = _ProviderJobsSpy()
    roles = _RoleControllerSpy()
    dispatcher = ProviderDispatcher(
        endpoints=_endpoint_registry(tmp_path), jobs=jobs, role_controller=roles
    )
    missing = ProviderInvocation(
        provider_id="chatgpt_web",
        task_id="task-browser",
        role_key="DEV",
        task_text="browser turn",
        execution_mode="browser_dom",
        endpoint_id="ep-missing",
    )
    with pytest.raises(ValueError, match="unknown browser endpoint"):
        asyncio.run(dispatcher.dispatch(missing, browser_context=_browser_context("ep-missing")))

    exact = ProviderInvocation(
        provider_id="chatgpt_web",
        task_id="task-browser",
        role_key="DEV",
        task_text="browser turn",
        execution_mode="browser_dom",
        endpoint_id="ep-9222",
    )
    with pytest.raises(ProviderRuntimeError) as caught:
        asyncio.run(dispatcher.dispatch(exact, browser_context=_browser_context("ep-other")))
    assert caught.value.code == "BROWSER_ENDPOINT_MISMATCH"
    assert roles.calls == []
    assert jobs.calls == []


@pytest.mark.parametrize("flag", ["draining", "removed"])
def test_t052_draining_or_removed_endpoint_fails_before_browser_execution(flag):
    from playwright_auto.cdpa_provider_runtime import (
        ProviderDispatcher,
        ProviderInvocation,
        ProviderRuntimeError,
    )

    endpoint = {"endpoint_id": "ep-9222", "draining": False, "removed": False}
    endpoint[flag] = True
    endpoints = SimpleNamespace(get=lambda _endpoint_id: endpoint)
    jobs = _ProviderJobsSpy()
    roles = _RoleControllerSpy()
    dispatcher = ProviderDispatcher(endpoints=endpoints, jobs=jobs, role_controller=roles)
    invocation = ProviderInvocation(
        provider_id="chatgpt_web",
        task_id="task-browser",
        role_key="DEV",
        task_text="browser turn",
        execution_mode="browser_dom",
        endpoint_id="ep-9222",
    )

    with pytest.raises(ProviderRuntimeError) as caught:
        asyncio.run(dispatcher.dispatch(invocation, browser_context=_browser_context()))

    assert caught.value.code == "BROWSER_ENDPOINT_UNAVAILABLE"
    assert roles.calls == []
    assert jobs.calls == []


# T053 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_id", "execution_mode"),
    [
        ("chatgpt_web", "browser_dom"),
        ("codex", "acp"),
        ("codex", "exec"),
        ("custom", "http"),
        ("custom", "custom"),
    ],
)
def test_t053_provider_invocation_preserves_unified_required_fields(provider_id, execution_mode):
    from playwright_auto.cdpa_provider_runtime import ProviderInvocation

    invocation = ProviderInvocation.from_mapping(
        {
            "provider_id": provider_id,
            "task_id": "task-invocation",
            "role_key": "dev",
            "task_text": "do work",
            "execution_mode": execution_mode,
            "material_refs": ["mat-a", "mat-b"],
            "workspace": "C:/work",
            "timeout_seconds": 123,
            "output_limit_bytes": 456,
            "environment_policy": "inherit-safe",
        }
    )

    assert invocation.provider_id == provider_id
    assert invocation.task_id == "task-invocation"
    assert invocation.role_key == "DEV"
    assert invocation.task_text == "do work"
    assert invocation.execution_mode == execution_mode
    assert invocation.material_refs == ("mat-a", "mat-b")
    assert invocation.workspace == "C:/work"
    assert invocation.timeout_seconds == 123
    assert invocation.output_limit_bytes == 456
    assert invocation.environment_policy == "inherit-safe"


def test_t053_long_timeout_is_capability_not_execution_mode():
    from playwright_auto.cdpa_provider_runtime import EXECUTION_MODES, select_provider

    selected = select_provider(required_capabilities=("long_timeout",))
    assert selected.provider_id == "pi"
    assert "long_timeout" in selected.capabilities
    assert "long_timeout" not in EXECUTION_MODES


def test_t053_explicit_provider_missing_required_capability_fails_closed():
    from playwright_auto.cdpa_provider_runtime import ProviderRuntimeError, select_provider

    with pytest.raises(ProviderRuntimeError) as caught:
        select_provider(
            explicit_provider_id="chatgpt_web",
            required_capabilities=("long_timeout",),
        )

    assert caught.value.code == "PROVIDER_CAPABILITY_MISSING"


def test_t053_unsupported_mode_and_nonbrowser_endpoint_binding_fail_closed(tmp_path):
    from playwright_auto.cdpa_provider_runtime import (
        ProviderDispatcher,
        ProviderInvocation,
        ProviderRuntimeError,
    )

    dispatcher = ProviderDispatcher(
        endpoints=_endpoint_registry(tmp_path), jobs=_ProviderJobsSpy(), role_controller=_RoleControllerSpy()
    )
    unsupported = ProviderInvocation(
        provider_id="codex",
        task_id="task-codex",
        role_key="DEV",
        task_text="work",
        execution_mode="http",
    )
    with pytest.raises(ProviderRuntimeError) as caught:
        asyncio.run(dispatcher.dispatch(unsupported))
    assert caught.value.code == "PROVIDER_MODE_UNSUPPORTED"

    endpoint_bound = ProviderInvocation(
        provider_id="pi",
        task_id="task-pi",
        role_key="DEV",
        task_text="work",
        execution_mode="exec",
        endpoint_id="ep-9222",
    )
    with pytest.raises(ProviderRuntimeError) as caught:
        asyncio.run(dispatcher.dispatch(endpoint_bound))
    assert caught.value.code == "PROVIDER_INVOCATION_INVALID"


def test_t053_work_owner_distinguishes_role_slot_child_teamtask_and_provider_job():
    from playwright_auto.cdpa_provider_runtime import ProviderRuntimeError, select_work_owner

    assert select_work_owner(delegated_role=True).owner == "role_slot"
    assert select_work_owner(independent_route_contract=True).owner == "team_task"
    assert select_work_owner().owner == "provider_job"
    with pytest.raises(ProviderRuntimeError) as caught:
        select_work_owner(delegated_role=True, independent_route_contract=True)
    assert caught.value.code == "PROVIDER_WORK_INVALID"


# T054 ----------------------------------------------------------------------------


def _provider_job_fixture(tmp_path, *, task_id="task-provider-job", provider_id="pi", execution_mode="exec"):
    from playwright_auto.cdpa_provider_runtime import (
        DEFAULT_PROVIDER_REGISTRY,
        ProviderInvocation,
        ProviderJobManager,
    )

    store = _store(tmp_path)
    state = store.create_task(
        "provider job owner",
        requested_team=task_id.replace("_", "-"),
        task_id=task_id,
        roles=("DEV",),
        closing_role="DEV",
    )
    manager = ProviderJobManager(store)
    invocation = ProviderInvocation(
        provider_id=provider_id,
        task_id=task_id,
        role_key="DEV",
        task_text="provider work",
        execution_mode=execution_mode,
    )
    entry = DEFAULT_PROVIDER_REGISTRY[provider_id]
    job = manager.create(invocation, entry)
    return store, state, manager, invocation, entry, job


@pytest.mark.parametrize(
    ("terminal", "projected"),
    [
        ("SUCCEEDED", "succeeded"),
        ("TIMEOUT", "timeout"),
        ("FAILED", "failed"),
        ("CANCELLED", "cancelled"),
        ("PARTIAL", "partial"),
    ],
)
def test_t054_provider_job_owns_lifecycle_refs_and_result_is_terminal_projection(
    tmp_path, terminal, projected
):
    store, state, manager, _invocation, _entry, job = _provider_job_fixture(
        tmp_path, task_id=f"task-provider-{terminal.lower()}"
    )
    invariant = {
        key: state.get(key)
        for key in ("status", "active_role", "route_seq", "route_contract_id")
    }

    running = manager.transition(state["task_id"], job["provider_job_id"], "RUNNING")
    assert running["state"] == "RUNNING"
    finished = manager.transition(
        state["task_id"],
        job["provider_job_id"],
        terminal,
        stdout="stdout evidence",
        stderr="stderr evidence",
        result="result evidence",
    )
    result = manager.result(state["task_id"], job["provider_job_id"])
    after = store.load_task_id(state["task_id"])

    assert finished["state"] == terminal
    assert result.status == projected
    assert result.provider_job_id == job["provider_job_id"]
    assert result.stdout_ref and result.stderr_ref and result.result_ref
    assert {result.stdout_ref, result.stderr_ref, result.result_ref}.issubset(after["materials"])
    assert {
        key: after.get(key)
        for key in ("status", "active_role", "route_seq", "route_contract_id")
    } == invariant
    assert "route" not in result.to_dict()


def test_t054_terminal_result_refs_survive_duplicate_callback(tmp_path):
    store, state, manager, _invocation, _entry, job = _provider_job_fixture(tmp_path)
    task_id, job_id = state["task_id"], job["provider_job_id"]
    manager.transition(task_id, job_id, "RUNNING")
    first = manager.transition(
        task_id, job_id, "SUCCEEDED", stdout="original stdout", result="original result"
    )

    replayed = manager.transition(task_id, job_id, "SUCCEEDED")
    current = manager.result(task_id, job_id)
    assert replayed["stdout_ref"] == current.stdout_ref == first["stdout_ref"]
    assert replayed["result_ref"] == current.result_ref == first["result_ref"]
    assert store.load_task_id(task_id)["materials"][current.stdout_ref]["content"] == "original stdout"


def test_t065_invalid_provider_command_cannot_orphan_running_job(tmp_path):
    from playwright_auto.cdpa_provider_runtime import ProviderRuntimeError

    store, state, manager, invocation, entry, job = _provider_job_fixture(
        tmp_path, task_id="task-provider-invalid-command", provider_id="custom", execution_mode="exec"
    )
    with pytest.raises(ProviderRuntimeError) as caught:
        manager.execute(invocation, entry)
    assert caught.value.code == "PROVIDER_COMMAND_REQUIRED"
    assert store.load_task_id(state["task_id"])["provider_jobs"][job["provider_job_id"]]["state"] == "QUEUED"


def test_t054_provider_result_is_unavailable_before_provider_job_terminal(tmp_path):
    from playwright_auto.cdpa_provider_runtime import ProviderRuntimeError

    store, state, manager, _invocation, _entry, job = _provider_job_fixture(tmp_path)
    with pytest.raises(ProviderRuntimeError) as caught:
        manager.result(state["task_id"], job["provider_job_id"])
    assert caught.value.code == "PROVIDER_JOB_NOT_TERMINAL"
    assert store.load_task_id(state["task_id"])["provider_jobs"][job["provider_job_id"]]["state"] == "QUEUED"


# T055/T056 shared fixtures -------------------------------------------------------


def _gateb_continuation_state(task_id="task-target"):
    return {
        "task_id": task_id,
        "team": "gateb-continuation",
        "status": "RUNNING",
        "workflow_mode": "team",
        "route_contract_id": "route-contract-gateb",
        "active_role": "DEV",
        "active_hop_id": 1,
        "route_repair_count": 2,
        "max_route_repairs": 4,
        "depends_on_task_ids": [],
        "hops": [
            {
                "hop_id": 1,
                "target_role": "DEV",
                "physical_role": "DEV",
                "state": "waiting",
                "request_id": f"{task_id}-hop1",
                "receipt": _receipt().to_dict(),
                "timestamps": {},
            }
        ],
        "roles": {
            "DEV": {
                "status": "active",
                "page_url": "https://chatgpt.com/c/11111111-2222-4333-8444-555555555555",
                "conversation_generation": 0,
            }
        },
    }


class _WakeSchedulerSpy:
    def __init__(self):
        self.calls = []

    def _schedule_async_wake(self, state, continuation):
        assert state["status"] == "WAITING"
        assert state["waiting_code"] == "async_callback"
        self.calls.append(continuation["continuation_id"])
        continuation["state"] = "wake_scheduled"
        continuation["wake"] = {"status": "scheduled"}


class _CommitWakeSpy:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _complete_request_response(hop):
        hop["request_response_completed"] = True

    def _schedule_async_wake(self, state, continuation):
        self.calls.append(
            {
                "continuation_id": continuation["continuation_id"],
                "hop_state": state["hops"][0]["state"],
                "role_status": state["roles"]["DEV"]["status"],
            }
        )
        continuation["state"] = "wake_scheduled"
        continuation["wake"] = {"status": "scheduled"}

    @staticmethod
    def _block_async_wake(_state, _continuation, reason, *, code):
        raise AssertionError(f"unexpected async wake block {code}: {reason}")


# T055 ----------------------------------------------------------------------------


def test_t055_phase15_script_source_adopts_provider_job_without_second_continuation_store():
    from playwright_auto.cdpa_provider_runtime import (
        adopt_external_script_callback,
        adopt_external_script_spawn,
    )

    owner = {"task_id": "task-compat", "provider_jobs": {}}
    continuation = {
        "continuation_id": "ac-compat",
        "registered_at": "2026-10-02T00:00:00+00:00",
        "spawn_confirmed_at": "2026-10-02T00:00:01+00:00",
        "job_id": "legacy-script-1",
        "callback": None,
    }

    job = adopt_external_script_spawn(owner, continuation)
    assert len(owner["provider_jobs"]) == 1
    assert continuation["provider_job_id"] == job["provider_job_id"]
    assert job["state"] == "RUNNING"
    assert job["metadata"]["compatibility_source"] == "phase1.5_external_script"

    continuation["callback"] = {
        "status": "succeeded",
        "material_ids": ["ac-compat-stdout", "ac-compat-result-refs"],
        "received_at": "2026-10-02T00:00:02+00:00",
    }
    terminal = adopt_external_script_callback(owner, continuation)
    assert terminal["state"] == "SUCCEEDED"
    assert terminal["stdout_ref"] == "ac-compat-stdout"
    assert terminal["result_ref"] == "ac-compat-result-refs"
    assert len(owner["provider_jobs"]) == 1


def test_t055_provider_job_terminalizes_before_continuation_observes_refs():
    from playwright_auto import cdpa_async_continuation as async_continuation

    state = _gateb_continuation_state()
    state["provider_jobs"] = {
        "pjob-1": {
            "provider_job_id": "pjob-1",
            "state": "RUNNING",
            "stdout_ref": None,
            "stderr_ref": None,
            "result_ref": None,
        }
    }
    record = async_continuation.register_source(
        state,
        continuation_id="ac-provider-1",
        role="DEV",
        source_kind="provider_job",
        source_refs=("pjob-1",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    assert record["state"] == "source_acknowledged"
    assert record["source_results"] == {}

    job = state["provider_jobs"]["pjob-1"]
    job.update(
        state="SUCCEEDED",
        stdout_ref="mat-stdout",
        stderr_ref="mat-stderr",
        result_ref="mat-result",
    )
    assert job["state"] == "SUCCEEDED"
    observed, recorded = async_continuation.record_source_terminal(
        state,
        "ac-provider-1",
        "pjob-1",
        status="succeeded",
        material_ids=(job["stdout_ref"], job["stderr_ref"], job["result_ref"]),
        now="2026-10-02T00:00:01+00:00",
    )

    assert recorded is True
    assert observed["state"] == "source_terminal"
    assert observed["source_refs"] == ["pjob-1"]
    assert observed["source_results"]["pjob-1"]["material_ids"] == [
        "mat-stdout",
        "mat-stderr",
        "mat-result",
    ]
    assert not any(key in observed for key in ("stdout", "stderr", "result", "provider_state"))


# T056 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dependency_status", "projection"),
    [
        ("DONE", "ready"),
        ("RUNNING", "waiting"),
        ("WAITING", "waiting"),
        ("PAUSED", "waiting"),
        ("BLOCKED", "waiting"),
        ("STOPPED", "failed"),
    ],
)
def test_t056_dependency_readiness_uses_one_ready_waiting_failed_evaluator(
    dependency_status, projection
):
    from playwright_auto.cdpa_dependencies import dependency_readiness, teamtask_source_readiness

    source = {"task_id": "task-source", "status": dependency_status, "depends_on_task_ids": []}
    target = {
        "task_id": "task-target",
        "status": "INBOX",
        "depends_on_task_ids": ["task-source"],
    }
    tasks = [source, target]

    dependency = dependency_readiness(target, tasks)
    continuation = teamtask_source_readiness(("task-source",), tasks)
    assert dependency.projection_state == projection
    assert continuation.projection_state == projection
    assert dependency == continuation


def test_t056_missing_dependency_fails_and_combined_dependency_continuation_cycle_rejects():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_dependencies import dependency_readiness

    target = _gateb_continuation_state("task-b")
    missing = dependency_readiness(
        {"task_id": "task-x", "depends_on_task_ids": ["task-missing"]},
        [{"task_id": "task-x", "depends_on_task_ids": ["task-missing"]}],
    )
    assert missing.projection_state == "failed"
    assert missing.missing == ("task-missing",)

    child = {
        "task_id": "task-c",
        "status": "RUNNING",
        "depends_on_task_ids": ["task-b"],
    }
    with pytest.raises(async_continuation.AsyncContinuationError) as caught:
        async_continuation.register_source(
            target,
            continuation_id="ac-cycle",
            role="DEV",
            source_kind="teamtask",
            source_refs=("task-c",),
            failure_policy="block",
            now="2026-10-02T00:00:00+00:00",
            all_tasks=(target, child),
        )
    assert caught.value.code == "ASYNC_WAIT_CYCLE"


def test_t056_one_teamtask_terminal_transition_feeds_dag_and_continuation():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_dependencies import dependency_readiness, terminal_transition

    target = _gateb_continuation_state("task-parent")
    target["depends_on_task_ids"] = ["task-child"]
    before = {"task_id": "task-child", "status": "RUNNING", "depends_on_task_ids": []}
    after = {"task_id": "task-child", "status": "DONE", "depends_on_task_ids": []}
    record = async_continuation.register_source(
        target,
        continuation_id="ac-child",
        role="DEV",
        source_kind="teamtask",
        source_refs=("task-child",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
        all_tasks=(target, before),
    )

    event = terminal_transition(before, after)
    readiness = dependency_readiness(target, (target, after))
    observed, recorded = async_continuation.record_source_terminal(
        target,
        record["continuation_id"],
        event.task_id,
        status="succeeded" if event.status == "DONE" else "failed",
        now="2026-10-02T00:00:01+00:00",
    )

    assert event.task_id == "task-child" and event.status == "DONE"
    assert readiness.projection_state == "ready"
    assert recorded is True
    assert observed["state"] == "source_terminal"


def test_t056_source_terminal_before_wait_commit_schedules_only_after_durable_wait():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_worker import CDPAWorker

    state = _gateb_continuation_state()
    record = async_continuation.register_source(
        state,
        continuation_id="ac-race-before-wait",
        role="DEV",
        source_kind="provider_job",
        source_refs=("pjob-race",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    async_continuation.record_source_terminal(
        state,
        record["continuation_id"],
        "pjob-race",
        status="succeeded",
        now="2026-10-02T00:00:01+00:00",
    )
    spy = _WakeSchedulerSpy()
    repair_count = state["route_repair_count"]

    CDPAWorker._route_async_callback(spy, state, record)
    assert spy.calls == []
    assert record["wake"]["status"] == "pending_wait"
    assert state["route_repair_count"] == repair_count

    state.update(
        status="WAITING",
        waiting_code="async_callback",
        waiting_reason="waiting",
        waiting={"reason": "async_callback", "continuation_id": record["continuation_id"]},
    )
    CDPAWorker._route_async_callback(spy, state, record)
    assert spy.calls == [record["continuation_id"]]
    assert record["state"] == "wake_scheduled"
    assert state["route_repair_count"] == repair_count


def test_t056_wait_commit_consumes_source_that_finished_before_wait_without_second_callback():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_worker import CDPAWorker

    state = _gateb_continuation_state()
    record = async_continuation.register_source(
        state,
        continuation_id="ac-finished-before-wait",
        role="DEV",
        source_kind="provider_job",
        source_refs=("pjob-finished-early",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    async_continuation.record_source_terminal(
        state,
        record["continuation_id"],
        "pjob-finished-early",
        status="succeeded",
        material_ids=("mat-early",),
        now="2026-10-02T00:00:01+00:00",
    )
    hop = state["hops"][0]
    spy = _CommitWakeSpy()
    repair_count = state["route_repair_count"]

    CDPAWorker._commit_async_wait(spy, state, hop, record)

    assert spy.calls == [
        {
            "continuation_id": record["continuation_id"],
            "hop_state": "routed",
            "role_status": "idle",
        }
    ]
    assert state["route_repair_count"] == repair_count
    assert record["source_results"]["pjob-finished-early"]["material_ids"] == ["mat-early"]


def test_t056_non_wait_route_supersedes_continuation_but_not_its_source_result():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_worker import CDPAWorker

    state = _gateb_continuation_state()
    record = async_continuation.register_source(
        state,
        continuation_id="ac-superseded",
        role="DEV",
        source_kind="provider_job",
        source_refs=("pjob-independent",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    async_continuation.record_source_terminal(
        state,
        record["continuation_id"],
        "pjob-independent",
        status="succeeded",
        material_ids=("mat-result",),
        now="2026-10-02T00:00:01+00:00",
    )
    state["active_hop_id"] = 2  # a valid non-WAIT route already committed away from this turn
    spy = _WakeSchedulerSpy()

    CDPAWorker._route_async_callback(spy, state, record)

    assert spy.calls == []
    assert record["state"] == "superseded"
    assert record["wake"]["status"] == "not_scheduled"
    assert record["source_results"]["pjob-independent"]["status"] == "succeeded"
    assert record["source_results"]["pjob-independent"]["material_ids"] == ["mat-result"]


def test_t056_route_validation_block_holds_terminal_source_until_wait_can_commit():
    from playwright_auto import cdpa_async_continuation as async_continuation
    from playwright_auto.cdpa_worker import CDPAWorker

    state = _gateb_continuation_state()
    record = async_continuation.register_source(
        state,
        continuation_id="ac-blocked-before-wait",
        role="DEV",
        source_kind="provider_job",
        source_refs=("pjob-blocked",),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    async_continuation.record_source_terminal(
        state,
        record["continuation_id"],
        "pjob-blocked",
        status="succeeded",
        now="2026-10-02T00:00:01+00:00",
    )
    spy = _WakeSchedulerSpy()
    repair_count = state["route_repair_count"]
    state.update(status="BLOCKED", block_code="ROUTE_VALIDATION_EXHAUSTED")

    CDPAWorker._route_async_callback(spy, state, record)
    assert spy.calls == []
    assert record["state"] == "source_terminal"
    assert state["route_repair_count"] == repair_count

    state.update(status="RUNNING", block_code=None)
    CDPAWorker._route_async_callback(spy, state, record)
    assert spy.calls == []
    assert record["wake"]["status"] == "pending_wait"
    state.update(
        status="WAITING",
        waiting_code="async_callback",
        waiting={"reason": "async_callback", "continuation_id": record["continuation_id"]},
    )
    CDPAWorker._route_async_callback(spy, state, record)
    assert spy.calls == [record["continuation_id"]]
    assert state["route_repair_count"] == repair_count


# T057 ----------------------------------------------------------------------------


class _TeamTaskSubmitSpy:
    def __init__(self):
        self.calls = []
        self.by_key = {}

    def submit_task(self, payload, *, idempotency_key=None, trigger=None):
        self.calls.append((payload, idempotency_key, trigger))
        if idempotency_key not in self.by_key:
            self.by_key[idempotency_key] = {
                "runtime_task_id": f"rtask-{len(self.by_key) + 1}",
                "idempotency_key": idempotency_key,
                "mode": "team",
                "trigger": trigger,
            }
        return self.by_key[idempotency_key]


def test_t057_cron_occurrence_reuses_ordinary_teamtask_creation_with_deterministic_key():
    from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner

    spy = _TeamTaskSubmitSpy()
    payload = {"task": "cron work", "team": "cron-team", "roles": ["DEV"]}
    first = RuntimeTaskRunner.submit_cron_job(
        spy, "cron-daily", "2026-10-02T00:00:00Z", payload
    )
    second = RuntimeTaskRunner.submit_cron_job(
        spy, "cron-daily", "2026-10-02T00:00:00+00:00", payload
    )

    assert first == second
    assert first["mode"] == "team"
    assert first["idempotency_key"] == "cron:cron-daily:2026-10-02T00:00:00+00:00"
    assert all(call[2]["kind"] == "cron" for call in spy.calls)


def test_t057_heartbeat_stale_episode_reuses_one_recovery_teamtask_key():
    from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner

    spy = _TeamTaskSubmitSpy()
    payload = {"task": "recover stale endpoint", "team": "recovery", "roles": ["DEV"]}
    first = RuntimeTaskRunner.submit_heartbeat_recovery(
        spy, "endpoint", "ep-9222", "2026-10-02T01:02:03Z", payload
    )
    second = RuntimeTaskRunner.submit_heartbeat_recovery(
        spy, "endpoint", "ep-9222", "2026-10-02T01:02:03+00:00", payload
    )

    assert first == second
    assert first["mode"] == "team"
    assert first["idempotency_key"] == "heartbeat:endpoint:ep-9222:2026-10-02T01:02:03+00:00"
    assert all(call[2]["kind"] == "stale_heartbeat" for call in spy.calls)
    assert all("INDEPENDENT_AGENT_TRIGGER_CONTEXT" not in json.dumps(call[2]) for call in spy.calls)


def test_t057_independent_routed_work_uses_ordinary_teamtask_owner_and_delegation_does_not(
    tmp_path, monkeypatch
):
    from playwright_auto.cdpa_provider_runtime import select_work_owner
    from playwright_auto.hcuw_runtime.audit import AuditTimeline
    from playwright_auto.hcuw_runtime.config import load_runtime_config
    from playwright_auto.hcuw_runtime.state import RuntimeStateStore
    from playwright_auto.hcuw_runtime.task_runner import RuntimeTaskRunner

    config = load_runtime_config(repository_root=tmp_path)
    state_store = RuntimeStateStore(config)
    state_store.initialize()
    runner = RuntimeTaskRunner(config, state_store, AuditTimeline())
    legacy_submits = []

    monkeypatch.setattr(
        RuntimeTaskRunner,
        "ensure_legacy_runtime",
        lambda self: {"status": "already_running"},
    )

    def submit_once(self, request, *, idempotency_key=None):
        legacy_submits.append((request, idempotency_key))
        return {
            "parsed": {
                "task_id": "child-task-1",
                "command_id": "child-command-1",
                "status": "queued",
            },
            "stdout": "",
            "stderr": "",
            "command": [],
        }

    monkeypatch.setattr(RuntimeTaskRunner, "_submit_team_with_legacy_api", submit_once)

    independent = select_work_owner(independent_route_contract=True)
    assert independent.owner == "team_task"
    assert "RouteContract" in independent.reason

    payload = {
        "task": "independent routed child work",
        "team": "child-team",
        "roles": ["DEV", "REVIEW"],
        "start_role": "DEV",
        "closing_role": "REVIEW",
    }
    child_key = "child:parent-task:subwork-1"
    first = runner.submit_task(
        payload,
        idempotency_key=child_key,
        trigger={"kind": "child_teamtask", "parent_task_id": "parent-task"},
    )
    second = runner.submit_task(
        payload,
        idempotency_key=child_key,
        trigger={"kind": "child_teamtask", "parent_task_id": "parent-task"},
    )

    assert first == second
    assert len(legacy_submits) == 1
    assert first["mode"] == "team"
    assert first["idempotency_key"] == child_key
    assert first["route_contract_id"]
    assert first["start_role"] == "DEV"
    assert first["closing_role"] == "REVIEW"
    assert first["role_slots"]["DEV"]["state"] == "materialized"
    assert first["role_slots"]["REVIEW"]["state"] == "declared"

    delegated = select_work_owner(delegated_role=True)
    assert delegated.owner == "role_slot"
    assert len(legacy_submits) == 1


# T058 ----------------------------------------------------------------------------


class _PrivateTaskDB:
    def __init__(self, tasks):
        self.tasks = tasks

    def get_task_private(self, task_id):
        return self.tasks.get(task_id)


def _material_api_for(tasks):
    from playwright_auto.dashboard_api import DashboardAPI

    api = DashboardAPI.__new__(DashboardAPI)
    api.db = _PrivateTaskDB(tasks)
    return api


def test_t058_material_api_serves_only_registered_bounded_material_identity():
    from playwright_auto.cdpa_materials import DEFAULT_INLINE_LIMIT, register_material

    state = {"task_id": "task-materials"}
    content = "x" * (DEFAULT_INLINE_LIMIT + 37)
    registered = register_material(
        state,
        source_kind="provider_result",
        material_type="artifact",
        version="pjob-1",
        content=content,
        material_id="mat-large-result",
    )
    api = _material_api_for({"task-materials": state})

    payload = api.report_material("task-materials", registered["material_id"])
    missing = api.report_material("task-materials", "mat-not-registered")
    path_probe = api.report_material("task-materials", "../../outside-secret.txt")
    cross_task = _material_api_for(
        {"task-materials": state, "task-other": {"task_id": "task-other", "materials": {}}}
    ).report_material("task-other", registered["material_id"])

    assert payload["material_id"] == "mat-large-result"
    assert payload["source_kind"] == "provider_result"
    assert payload["material_type"] == "artifact"
    assert payload["version"] == "pjob-1"
    assert payload["content_length"] == len(content)
    assert payload["truncated"] is True
    assert len(payload["content"].encode("utf-8")) <= DEFAULT_INLINE_LIMIT
    assert missing["error"]["code"] == "MATERIAL_NOT_FOUND"
    assert path_probe["error"]["code"] == "MATERIAL_NOT_FOUND"
    assert cross_task["error"]["code"] == "MATERIAL_NOT_FOUND"


@pytest.mark.parametrize(
    "material_type",
    ["report", "handoff", "prompt", "skill", "rule", "task_context", "log", "artifact"],
)
def test_t058_registered_material_types_roundtrip_by_task_material_identity(material_type):
    from playwright_auto.cdpa_materials import register_material

    state = {"task_id": "task-material-types"}
    material_id = f"mat-{material_type}"
    register_material(
        state,
        source_kind="inline",
        material_type=material_type,
        version="v-test",
        content=f"content:{material_type}",
        material_id=material_id,
    )
    payload = _material_api_for({"task-material-types": state}).report_material(
        "task-material-types", material_id
    )

    assert payload["material_id"] == material_id
    assert payload["material_type"] == material_type
    assert payload["version"] == "v-test"
    assert payload["content"] == f"content:{material_type}"
    assert payload["error"] is None


def test_t058_material_api_preserves_inline_report_compatibility_without_path_dereference():
    state = {
        "task_id": "task-report",
        "reports": {
            "report-inline": {
                "content": "# report\naccepted\n",
                "size": len("# report\naccepted\n".encode("utf-8")),
                "sha256": hashlib.sha256("# report\naccepted\n".encode("utf-8")).hexdigest(),
            }
        },
    }
    api = _material_api_for({"task-report": state})

    payload = api.report_material("task-report", "report-inline")

    assert payload["material_id"] == "report-inline"
    assert payload["material_type"] == "report"
    assert payload["source_kind"] == "inline"
    assert payload["content"] == "# report\naccepted\n"
    assert payload["error"] is None


def test_t067_task_context_material_exposes_adopted_url_handoff_without_new_path_owner():
    from playwright_auto.cdpa_materials import task_context_materials

    state = {
        "task_id": "task-adopted-context",
        "active_role": "REVIEW",
        "task_text": "review current work",
        "depends_on_task_ids": ["parent-1"],
        "adopted_url_handoff_context": {
            "conversation_url": "https://chatgpt.com/c/existing",
            "conversation_id": "existing",
            "binding_kind": "adopted-url",
        },
        "materials": {
            "report-prior": {"material_type": "report"},
        },
    }
    parent = {"task_id": "parent-1", "status": "DONE"}

    materials = task_context_materials(state, (state, parent))
    locator = materials["runtime-task-context"]
    context = json.loads(locator["content"])

    assert locator["material_type"] == "task_context"
    assert context["dependencies"] == [{"task_id": "parent-1", "status": "DONE"}]
    assert context["prior_report_material_ids"] == ["report-prior"]
    assert context["adopted_url_handoff_context"] == {
        "conversation_url": "https://chatgpt.com/c/existing",
        "conversation_id": "existing",
        "binding_kind": "adopted-url",
    }


def test_t058_provider_job_owns_result_materials_continuation_only_references_them(tmp_path):
    from playwright_auto import cdpa_async_continuation as async_continuation

    store, state, manager, _invocation, _entry, job = _provider_job_fixture(
        tmp_path, task_id="task-material-owner"
    )
    manager.transition(state["task_id"], job["provider_job_id"], "RUNNING")
    terminal = manager.transition(
        state["task_id"],
        job["provider_job_id"],
        "SUCCEEDED",
        stdout="provider stdout",
        stderr="provider stderr",
        result="provider result",
    )
    owner = store.load_task_id(state["task_id"])
    continuation_state = _gateb_continuation_state("task-continuation-target")
    record = async_continuation.register_source(
        continuation_state,
        continuation_id="ac-material-owner",
        role="DEV",
        source_kind="provider_job",
        source_refs=(terminal["provider_job_id"],),
        failure_policy="block",
        now="2026-10-02T00:00:00+00:00",
    )
    material_ids = (terminal["stdout_ref"], terminal["stderr_ref"], terminal["result_ref"])
    async_continuation.record_source_terminal(
        continuation_state,
        record["continuation_id"],
        terminal["provider_job_id"],
        status="succeeded",
        material_ids=material_ids,
        now="2026-10-02T00:00:01+00:00",
    )

    assert set(material_ids).issubset(owner["materials"])
    assert record["source_refs"] == [terminal["provider_job_id"]]
    assert record["source_results"][terminal["provider_job_id"]]["material_ids"] == list(material_ids)
    assert not any(key in record for key in ("stdout", "stderr", "result", "provider_job_state"))
