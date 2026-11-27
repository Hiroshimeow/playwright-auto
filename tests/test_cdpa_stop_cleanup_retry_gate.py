"""Stop cleanup must retain proof holds without polling the same failure forever."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from playwright_auto import cdpa_worker as worker_module
from playwright_auto.cdpa_conversations import ConversationWriterLease
from playwright_auto.cdpa_runtime_registry import CDPARuntimeRegistry
from playwright_auto.cdpa_worker import CDPAWorker
from test_cdpa_stop_writer_cleanup import StopActions, owned_binding, snapshot, stop
from test_cdpa_worker import setup_task
from test_stopped_writer_proof import evidence
from test_stopped_writer_seal_owner import owned
from test_stopped_writer_worker_maintenance import configured, enqueue, execute


@pytest.fixture
def clock(monkeypatch):
    value = [datetime.now(timezone.utc)]
    monkeypatch.setattr(worker_module, "utc_now", lambda: value[0].isoformat())
    return value


def retry_due(store, worker, actions, path, clock):
    state = store.load(path)
    when = state["cleanup"].get("next_retry_at")
    if when:
        clock[0] = max(clock[0], datetime.fromisoformat(when)) + timedelta(seconds=1)
    else:
        clock[0] += timedelta(days=1)
    asyncio.run(worker._continue_cleanup(state, actions, path))
    return store.load(path)


def legacy_stopped(tmp_path, clock):
    config, store, state, worker = setup_task(tmp_path)
    state["roles"]["PLAN"].update(
        page_id="lost-legacy-page", page_url="https://chatgpt.com/c/legacy",
        turn=1, online=False,
    )
    state = store.save(state["manifest_path"], state)
    actions = StopActions(missing=True)
    stopped = stop(store, state, worker, actions)
    assert stopped["cleanup"]["same_failure_count"] == 1
    return config, store, stopped, worker, actions


def assert_pending(state):
    assert state["status"] == state["terminal_state"] == "STOPPED"
    assert state["cleanup"]["state"] == "CLEARING"
    assert state["cleanup"]["phase"] == "writer_stop_pending"
    original = next(c for c in state["controls"] if c["action"] == "stop")
    assert original["status"] == "applied"
    assert original["result"]["writer_cleanup_pending"] is True


@pytest.mark.parametrize("failure", ["legacy_missing", "bound_missing", "identity_changed"])
def test_three_identical_stop_failures_hold_without_fourth_browser_or_save(
    tmp_path, monkeypatch, clock, failure,
):
    _, store, state, worker = setup_task(tmp_path)
    owner = ConversationWriterLease(worker.runtime_db)
    attempts = {}
    if failure == "legacy_missing":
        state["roles"]["PLAN"].update(page_id="lost-legacy-page", turn=1)
        state = store.save(state["manifest_path"], state)
    else:
        owner, _ = owned_binding(worker, state, "PLAN", provisional=True)
        lease = owner.acquire("PLAN")
        attempt, _ = owner.record_send_intent(
            lease, idempotency_key="original-accepted", text="Keep accepted work.",
            baseline_last_user_message_id=None, baseline_user_turn_count=0,
        )
        owner.transition_attempt(attempt["attempt_id"], "accepted")
        attempts[attempt["attempt_id"]] = owner.db.get_command(attempt["attempt_id"])
        if failure == "identity_changed":
            original = worker._gate_c_endpoint_evidence
            monkeypatch.setattr(
                worker, "_gate_c_endpoint_evidence",
                lambda endpoint: (original(endpoint)[0], "devtools:another-browser"),
            )
    original_owner = copy.deepcopy(snapshot(owner))
    original_hops = copy.deepcopy(store.load(state["manifest_path"])["hops"])
    actions = StopActions(missing=failure != "identity_changed")
    stopped = stop(store, state, worker, actions)
    path = Path(stopped["manifest_path"])
    assert datetime.fromisoformat(stopped["cleanup"]["next_retry_at"]) - clock[0] == timedelta(seconds=30)
    stopped = retry_due(store, worker, actions, path, clock)
    assert datetime.fromisoformat(stopped["cleanup"]["next_retry_at"]) - clock[0] == timedelta(seconds=120)
    stopped = retry_due(store, worker, actions, path, clock)

    assert_pending(stopped)
    assert stopped["cleanup"]["same_failure_count"] == 3
    assert stopped["cleanup"]["retry_count"] == 3
    assert stopped["cleanup"]["retry_suppressed"] is True
    assert stopped["cleanup"]["next_retry_at"] is None
    assert stopped["hops"] == original_hops
    assert snapshot(owner) == original_owner
    assert all(owner.db.get_command(key) == value for key, value in attempts.items())

    before = path.read_bytes()
    catalog_before = store.catalog_path.read_bytes()
    calls = list(actions.located_roles)
    # The ordinary automatic path must respect suppression, even much later.
    retry_due(store, worker, actions, path, clock)
    assert path.read_bytes() == before
    assert store.catalog_path.read_bytes() == catalog_before
    assert actions.located_roles == calls
    assert actions.stop_calls == actions.closed_teams == 0
    assert not actions.fresh_calls

    with pytest.raises(ValueError, match="cleanup|writer"):
        store.replace_task_and_rewire(
            stopped["task_id"], "No replacement while proof is held",
            reuse_team=False, rewire_children=True, restart_team=True,
            incident_id="suppressed-writer-hold",
        )
    assert path.read_bytes() == before
    assert not [s for s in store.discover() if s.get("replaces_task_id") == stopped["task_id"]]


def test_existing_high_count_loop_stops_on_next_failure_and_stays_quiet_after_reload(tmp_path, clock):
    config, store, state, worker, actions = legacy_stopped(tmp_path, clock)
    path = Path(state["manifest_path"])
    state["cleanup"].update(retry_count=5000, same_failure_count=5000, retry_suppressed=False)
    store.save(path, state)
    stopped = retry_due(store, worker, actions, path, clock)
    assert stopped["cleanup"]["retry_count"] == 5001
    assert stopped["cleanup"]["same_failure_count"] == 5001
    assert stopped["cleanup"]["retry_suppressed"] is True
    assert stopped["cleanup"]["next_retry_at"] is None
    assert_pending(stopped)
    assert not ConversationWriterLease(worker.runtime_db).list_bindings(include_sealed=True)

    before = path.read_bytes()
    calls = list(actions.located_roles)
    restarted = CDPAWorker(config, store=store)
    registry = CDPARuntimeRegistry.hydrate([store.load(path)], now=clock[0].timestamp())
    assert registry.due_task_ids(clock[0].timestamp() + 86400) == ()
    assert registry.next_due_at() is None
    retry_due(store, restarted, actions, path, clock)
    assert path.read_bytes() == before
    assert actions.located_roles == calls


def test_newly_released_binding_resets_only_the_identical_failure_streak(tmp_path, clock):
    _, store, state, worker = setup_task(tmp_path, roles=("PLAN", "DEV"))
    owner, _ = owned_binding(worker, state, "PLAN")

    class PartialActions(StopActions):
        async def locate_owned(self, state, role):
            if role == "PLAN":
                self.located_roles.append(role)
                return None
            return await super().locate_owned(state, role)

    actions = PartialActions()
    stopped = stop(store, state, worker, actions)
    path = Path(stopped["manifest_path"])
    stopped = retry_due(store, worker, actions, path, clock)
    assert stopped["cleanup"]["same_failure_count"] == 2
    previous_error = stopped["cleanup"]["failure_fingerprint"]
    # A newly discovered task-owned binding can now be positively released,
    # while the original missing-page error remains byte-for-byte identical.
    owned_binding(worker, stopped, "DEV")
    progressed = retry_due(store, worker, actions, path, clock)
    assert progressed["cleanup"]["failure_fingerprint"] == previous_error
    assert progressed["cleanup"]["released_bindings"] == ["DEV"]
    assert owner.get_binding("DEV")["binding_status"] == "sealed"
    assert owner.get_binding("PLAN")["binding_status"] != "sealed"
    assert progressed["cleanup"]["retry_count"] == 3
    assert progressed["cleanup"]["same_failure_count"] == 1
    assert progressed["cleanup"]["retry_suppressed"] is False
    assert_pending(progressed)

    # Rediscovering that already sealed binding is not new progress.
    retry_due(store, worker, actions, path, clock)
    capped = retry_due(store, worker, actions, path, clock)
    assert capped["cleanup"]["retry_count"] == 5
    assert capped["cleanup"]["same_failure_count"] == 3
    assert capped["cleanup"]["retry_suppressed"] is True
    assert capped["cleanup"]["released_bindings"] == ["DEV"]


def test_exact_owned_proof_can_finish_before_the_failure_cap(tmp_path, clock):
    _, store, state, worker = setup_task(tmp_path)
    owner, _ = owned_binding(worker, state, "PLAN", provisional=True)
    lease = owner.acquire("PLAN")
    attempt, _ = owner.record_send_intent(
        lease, idempotency_key="accepted-before-stop", text="Preserve the original.",
        baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    owner.transition_attempt(attempt["attempt_id"], "accepted")
    attempt_before = owner.db.get_command(attempt["attempt_id"])
    actions = StopActions(missing=True)
    stopped = stop(store, state, worker, actions)
    path = Path(stopped["manifest_path"])
    hops = copy.deepcopy(stopped["hops"])
    actions.missing = False
    released = retry_due(store, worker, actions, path, clock)
    assert released["cleanup"]["state"] == "STOP_RELEASED"
    assert released["status"] == "STOPPED"
    assert released["hops"] == hops
    assert owner.get_binding("PLAN")["binding_status"] == "sealed"
    assert owner.db.get_command(attempt["attempt_id"]) == attempt_before
    assert actions.closed_teams == 0 and not actions.fresh_calls


def test_explicit_clear_control_rearms_suppressed_writer_verification_without_releasing_it(tmp_path, clock):
    _, store, state, worker, actions = legacy_stopped(tmp_path, clock)
    path = Path(state["manifest_path"])
    retry_due(store, worker, actions, path, clock)
    capped = retry_due(store, worker, actions, path, clock)
    assert capped["cleanup"]["retry_suppressed"] is True
    hops = copy.deepcopy(capped["hops"])
    requested = store.request_control(path, "clear_team", reason="Reverify exact saved ownership")
    asyncio.run(worker._apply_control(requested, actions, path))
    pending = store.load(path)
    assert_pending(pending)
    assert pending["cleanup"]["retry_count"] == 4
    assert pending["cleanup"]["same_failure_count"] == 1
    assert pending["cleanup"]["retry_suppressed"] is False
    assert pending["controls"][-1]["status"] == "cleanup_pending"
    assert pending["hops"] == hops
    assert actions.closed_teams == 0 and not actions.fresh_calls


def test_explicit_r7_maintenance_finishes_suppressed_stop_after_full_coverage(configured):
    c = configured
    state = c.store.load(c.path)
    state["cleanup"].update(
        retry_count=5000, same_failure_count=5000, failure_fingerprint="old-proof-hold",
        retry_suppressed=True, next_retry_at=None,
    )
    c.store.save(c.path, state)
    c.worker.hydrate_runtime(startup=False)
    ledger_before = c.ledger.read_bytes()
    hops_before = copy.deepcopy(c.store.load(c.path)["hops"])
    pages_before = tuple(c.context.pages)
    attempts = {hop["send_attempt_id"]: c.owner.db.get_command(hop["send_attempt_id"])
                for hop in hops_before}
    enqueue(c, "release-suppressed-writer")
    result = execute(c)
    assert result["status"] == "applied", result
    saved = c.store.load(c.path)
    assert saved["status"] == "STOPPED"
    assert saved["cleanup"]["state"] == "STOP_RELEASED"
    assert saved["cleanup"]["phase"] == "writer_released"
    assert saved["cleanup"]["retry_count"] == 5000
    assert saved["hops"] == hops_before
    assert c.ledger.read_bytes() == ledger_before
    assert all(c.owner.db.get_command(key) == value for key, value in attempts.items())
    assert c.owner.get_binding("binding-old")["binding_status"] == "sealed"
    assert tuple(c.context.pages) == pages_before
    assert all(not page.closed for page in pages_before)
    assert c.seen == [c.target.target_id, c.target.target_id]
    assert c.owner.db.get_task_detail("task-stopped")["writer_maintenance"]["released"] is True


def test_forced_release_only_still_holds_uncovered_binding_without_browser_or_save(configured):
    c = configured
    state = c.store.load(c.path)
    state["cleanup"].update(retry_suppressed=True, same_failure_count=5000, next_retry_at=None)
    c.store.save(c.path, state)
    before = c.path.read_bytes()
    owner_before = copy.deepcopy(snapshot(c.owner))

    class NoBrowser:
        def __getattr__(self, name):
            pytest.fail(f"release-only attempted browser action {name}")

    assert asyncio.run(c.worker._continue_stop_writer_cleanup(
        c.store.load(c.path), NoBrowser(), c.path, force_retry=True, release_only=True,
    )) is False
    assert c.path.read_bytes() == before
    assert snapshot(c.owner) == owner_before
    assert c.owner.get_binding("binding-old")["binding_status"] == "active"
