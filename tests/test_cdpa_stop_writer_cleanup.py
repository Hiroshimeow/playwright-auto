"""Offline Stop lifecycle regressions using the real disposable Gate C ledger."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from playwright_auto.cdpa_actions import RoleOwnershipError
from playwright_auto.cdpa_conversations import ConversationWriterLease
from test_cdpa_worker import FakeActions, ready_client, setup_task


def snapshot(writers):
    with writers.db.connection() as connection:
        state, _ = writers._load_state(connection)
        return state


def owned_binding(worker, state, role, *, identity=None, task=None, provisional=False):
    writers = ConversationWriterLease(worker.runtime_db)
    endpoint = state['browser_endpoint_id']
    cdp, physical = worker._gate_c_endpoint_evidence(endpoint)
    identity = identity or role
    page = f"page-{state['roles'][role]['physical_role']}"
    binding = writers.bind(
        binding_id=identity, provider_id='chatgpt_web', endpoint_id=endpoint,
        endpoint_cdp_url=cdp, physical_browser_id=physical,
        page_target_identity=page if task is None else 'foreign-page',
        conversation_url='https://chatgpt.com/', verified_new_chat=True,
        task_id=task or state['task_id'], role_key=role,
    )
    lease = writers.acquire(identity)
    if provisional:
        writers.rekey(identity, f'https://chatgpt.com/c/{identity}')
    return writers, lease


class StopActions(FakeActions):
    def __init__(self, *, streaming=False, lookup_error=False, missing=False, click_error=False):
        super().__init__()
        self.streaming = streaming
        self.lookup_error = lookup_error
        self.missing = missing
        self.click_error = click_error

    async def locate_owned(self, state, role):
        if self.lookup_error:
            raise RoleOwnershipError('lookup failed')
        if self.missing:
            self.located_roles.append(role)
            return None
        acquired = await super().locate_owned(state, role)
        current = await acquired.client.assert_ownership()
        acquired.client.assert_ownership.return_value = replace(
            current, stop_visible=self.streaming, url=f'https://chatgpt.com/c/{role}'
        )
        return acquired

    async def stop_if_active(self, acquired):
        self.stop_calls += 1
        if self.click_error:
            raise RuntimeError('click failed')
        return True


def stop(store, state, worker, actions):
    path = Path(state['manifest_path'])
    requested = store.request_control(path, 'stop', reason='test stop')
    asyncio.run(worker._apply_control(requested, actions, path))
    return store.load(path)


def test_stop_seals_all_task_roles_reserved_and_both_identities_without_closing_tabs(tmp_path):
    _, store, state, worker = setup_task(tmp_path, roles=('PLAN', 'DEV'))
    writers, _ = owned_binding(worker, state, 'PLAN', provisional=True)
    owned_binding(worker, state, 'DEV')
    writers.reserve_async_wait('DEV')
    owned_binding(worker, state, 'DEV', identity='foreign', task='other-task')
    foreign_before = writers.get_binding('foreign')
    actions = StopActions()
    stopped = stop(store, state, worker, actions)
    assert stopped['status'] == 'STOPPED'
    assert stopped['controls'][-1]['status'] == 'applied'
    assert stopped['cleanup']['state'] == 'STOP_RELEASED'
    assert set(actions.located_roles) == {'PLAN', 'DEV'}
    durable = snapshot(writers)
    for role in ('PLAN', 'DEV'):
        assert writers.get_binding(role)['binding_status'] == 'sealed'
        assert all(lease['owner_binding_id'] != role for lease in durable['leases'].values())
        assert role not in durable['page_claims'].values()
    assert writers.get_binding('foreign') == foreign_before
    assert actions.closed_teams == 0


@pytest.mark.parametrize('attempt_state', ['intent_recorded', 'submitted', 'accepted'])
def test_stop_retains_unresolved_evidence_and_later_terminal_retry_releases(tmp_path, attempt_state):
    _, store, state, worker = setup_task(tmp_path)
    writers, lease = owned_binding(worker, state, 'PLAN', provisional=True)
    # Rekey changed the writer identity: acquire the current fenced projection.
    lease = writers.acquire('PLAN')
    attempt, _ = writers.record_send_intent(lease, idempotency_key='stop-attempt', text='once',
                                          baseline_last_user_message_id=None, baseline_user_turn_count=0)
    if attempt_state != 'intent_recorded':
        writers.transition_attempt(attempt['attempt_id'], attempt_state)
    command_before = writers.db.get_command(attempt['attempt_id'])
    actions = StopActions(streaming=attempt_state == 'accepted')
    stopped = stop(store, state, worker, actions)
    assert stopped['controls'][-1]['status'] == 'applied'
    assert stopped['status'] == 'STOPPED'
    assert stopped['cleanup']['state'] == 'CLEARING'
    assert stopped['cleanup']['phase'] == 'writer_stop_pending'
    assert stopped['cleanup']['last_error']
    assert writers.get_binding('PLAN')['binding_status'] != 'sealed'
    assert writers.db.get_command(attempt['attempt_id']) == command_before
    if attempt_state != 'accepted':
        writers.transition_attempt(attempt['attempt_id'], 'reconciled_absent')
    actions.streaming = False
    asyncio.run(worker._continue_cleanup(stopped, actions, Path(state['manifest_path']), force_retry=True))
    assert store.load(state['manifest_path'])['cleanup']['state'] == 'STOP_RELEASED'
    assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    assert len([c for c in store.load(state['manifest_path'])['controls'] if c['action'] == 'stop']) == 1
    assert actions.closed_teams == 0


@pytest.mark.parametrize('attempt_state', ['intent_recorded', 'submitted', 'accepted'])
@pytest.mark.parametrize('reserved', [False, True])
def test_late_callback_preserves_pending_stop_writer_and_attempt(tmp_path, attempt_state, reserved):
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, 'PLAN', provisional=True)
    lease = writers.acquire('PLAN')
    attempt, _ = writers.record_send_intent(
        lease, idempotency_key='late-stop-callback', text='once',
        baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    if attempt_state != 'intent_recorded':
        writers.transition_attempt(attempt['attempt_id'], attempt_state)
    reservation = writers.reserve_async_wait('PLAN') if reserved else lease
    actions = StopActions(streaming=attempt_state == 'accepted')
    stopped = stop(store, state, worker, actions)
    assert stopped['status'] == 'STOPPED'
    assert stopped['cleanup']['phase'] == 'writer_stop_pending'
    before = snapshot(writers)
    assert before['leases'] and before['page_claims']
    command_before = writers.db.get_command(attempt['attempt_id'])
    cleanup_before = copy.deepcopy(stopped['cleanup'])
    hops_before = copy.deepcopy(stopped['hops'])
    continuation = {
        'continuation_id': 'late-stop-callback',
        'target_ref': {'binding_id': 'PLAN'},
        'writer_reservation': copy.deepcopy(reservation),
        'state': 'source_terminal',
    }
    for _ in range(2):
        worker._route_async_callback(stopped, continuation)
        assert continuation['state'] == 'completed'
        assert continuation['wake']['status'] == 'not_woken'
        assert snapshot(writers) == before  # Complete bindings, leases and page claims.
        assert writers.db.get_command(attempt['attempt_id']) == command_before
        assert continuation['writer_reservation'] == reservation
        assert stopped['cleanup'] == cleanup_before
        assert stopped['hops'] == hops_before
    # Only the existing Stop owner may release after both proofs become available.
    if attempt_state != 'accepted':
        writers.transition_attempt(attempt['attempt_id'], 'reconciled_absent')
    actions.streaming = False
    assert asyncio.run(worker._continue_cleanup(
        stopped, actions, Path(state['manifest_path']), force_retry=True,
    ))
    assert stopped['cleanup']['state'] == 'STOP_RELEASED'
    assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    assert not snapshot(writers)['leases'] and not snapshot(writers)['page_claims']
    assert actions.closed_teams == 0


@pytest.mark.parametrize('failure', ['streaming', 'lookup_error', 'missing', 'click_error'])
def test_stop_requires_positive_nonstreaming_owned_page_evidence(tmp_path, failure):
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, 'PLAN')
    before = snapshot(writers)
    stopped = stop(store, state, worker, StopActions(**{failure: True}))
    assert stopped['status'] == 'STOPPED'
    assert stopped['cleanup']['state'] == 'CLEARING'
    assert stopped['cleanup']['last_error']
    assert snapshot(writers) == before


def test_stop_cleanup_waits_for_normal_endpoint_admission(tmp_path):
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, 'PLAN')
    path = Path(state['manifest_path'])
    requested = store.request_control(path, 'stop')
    actions = StopActions()

    async def exercise():
        await worker._acquire_endpoint_admission(state, 'PLAN')
        operation = asyncio.create_task(worker._apply_control(requested, actions, path))
        try:
            await asyncio.sleep(0.05)
            assert not operation.done()
            assert actions.stop_calls == 0
            assert writers.get_binding('PLAN')['binding_status'] != 'sealed'
        finally:
            worker._release_endpoint_admission()
        await asyncio.wait_for(operation, 3)
        assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    asyncio.run(exercise())


def test_terminal_advance_retries_stop_before_terminal_clear_and_survives_save_gap(tmp_path, monkeypatch):
    import playwright_auto.cdpa_worker as module
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, 'PLAN')
    stopped = stop(store, state, worker, StopActions(missing=True))
    path = Path(state['manifest_path'])
    assert stopped['cleanup']['state'] == 'CLEARING'
    # Crash after Gate C commit but before task save: durable task ownership remains discoverable.
    writers.close_binding('PLAN')
    sealed = writers.get_binding('PLAN')
    stopped['cleanup']['next_retry_at'] = None
    store.save(path, stopped)
    actions = StopActions()
    monkeypatch.setattr(module, 'CDPATabActions', lambda *_a, **_k: actions)
    result = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert result['cleanup']['state'] == 'STOP_RELEASED'
    assert writers.get_binding('PLAN') == sealed
    assert result['controls'][-1]['status'] == 'applied'
    assert actions.closed_teams == 0


@pytest.mark.parametrize(('activity', 'value'), [
    ('response_activity_turn_id', 'active-turn'),
    ('response_projection_active', True),
    ('response_projection_active', None),
])
@pytest.mark.parametrize('accepted', [False, True])
def test_real_snapshot_activity_without_stop_preserves_writer_evidence(tmp_path, activity, value, accepted):
    from playwright_auto.chatgpt_models import ChatGPTSnapshot
    _, store, state, worker = setup_task(tmp_path)
    writers, lease = owned_binding(worker, state, 'PLAN')
    if accepted:
        attempt, _ = writers.record_send_intent(
            lease, idempotency_key='terminal-accepted', text='once',
            baseline_last_user_message_id=None, baseline_user_turn_count=0,
        )
        writers.transition_attempt(attempt['attempt_id'], 'accepted')
    before = snapshot(writers)

    class ActiveSnapshotActions(StopActions):
        async def locate_owned(self, state, role):
            acquired = await super().locate_owned(state, role)
            current = await acquired.client.assert_ownership()
            assert isinstance(current, ChatGPTSnapshot)
            assert not current.stop_visible
            acquired.client.assert_ownership.return_value = replace(
                current, **{activity: value}
            )
            return acquired

    stopped = stop(store, state, worker, ActiveSnapshotActions())
    assert stopped['cleanup']['state'] == 'CLEARING'
    assert 'still responding' in stopped['cleanup']['last_error']
    assert snapshot(writers) == before


@pytest.mark.parametrize('prepared_request', [False, True])
def test_pristine_stop_needs_no_browser_and_survives_reload_and_replacement_retry(tmp_path, prepared_request):
    from playwright_auto.cdpa_store import TaskStore
    from playwright_auto.cdpa_worker import CDPAWorker
    _, store, state, worker = setup_task(tmp_path)
    original_hop = store.load(state['manifest_path'])['hops'][0]
    assert original_hop['state'] == 'pre_send' and original_hop['request_id']
    if prepared_request:
        from playwright_auto.durable import RequestLedger
        RequestLedger(original_hop['ledger_path']).begin(
            role=original_hop['physical_role'], prompt='prepared but never dispatched',
            request_id=original_hop['request_id'],
        )
    actions = StopActions(missing=True)
    stopped = stop(store, state, worker, actions)
    assert stopped['cleanup']['state'] == 'STOP_RELEASED'
    assert stopped['hops'][0] == original_hop
    assert actions.located_roles == []
    assert actions.stop_calls == 0
    reloaded_store = TaskStore(worker.config)
    restarted = CDPAWorker(worker.config, store=reloaded_store)
    restarted.hydrate_runtime()
    assert reloaded_store.load(state['manifest_path'])['cleanup']['state'] == 'STOP_RELEASED'
    first = reloaded_store.replace_task_and_rewire(
        state['task_id'], 'continue original goal', reuse_team=True,
        rewire_children=True, incident_id='pristine-stop-replacement',
    )
    second = reloaded_store.replace_task_and_rewire(
        state['task_id'], 'continue original goal', reuse_team=True,
        rewire_children=True, incident_id='pristine-stop-replacement',
    )
    assert first['replacement']['task_id'] == second['replacement']['task_id']


@pytest.fixture
def queued_dev_stop(tmp_path):
    _, store, state, worker = setup_task(tmp_path, roles=('PLAN', 'DEV'))
    writers, _ = owned_binding(worker, state, 'PLAN')
    state['roles']['PLAN']['turn'] = 1
    state['hops'][0]['state'] = 'routed'
    state['hops'][0]['receipt'] = {'accepted': True, 'user_message_id': 'plan-user'}
    worker._append_hop(state, source_role='PLAN', target_role='DEV', handoff='implement')
    state = store.save(state['manifest_path'], state)
    return store, state, worker, writers


class QueuedDevStopActions(StopActions):
    def __init__(self, config, *, secondary='missing'):
        super().__init__()
        self.config = config
        self.secondary = secondary
        self.stopped_roles = []

    async def locate_owned(self, state, role):
        if role != 'DEV':
            return await super().locate_owned(state, role)
        if self.secondary == 'missing':
            from types import SimpleNamespace
            from playwright_auto.cdpa_actions import CDPATabActions
            self.located_roles.append(role)
            # Real no-create locator against an empty disposable context.
            return await CDPATabActions(SimpleNamespace(pages=[]), self.config).locate_owned(state, role)
        if self.secondary == 'unavailable':
            self.located_roles.append(role)
            raise RoleOwnershipError('queued DEV browser unavailable')
        acquired = await super().locate_owned(state, role)
        current = await acquired.client.assert_ownership()
        acquired.client.assert_ownership.return_value = replace(
            current, stop_visible=self.secondary == 'streaming',
            page_task_id='foreign-task' if self.secondary == 'foreign' else current.page_task_id,
        )
        return acquired

    async def stop_if_active(self, acquired):
        current = await acquired.client.assert_ownership()
        self.stopped_roles.append(current.page_role)
        return await super().stop_if_active(acquired)

    async def acquire(self, *_args, **_kwargs):
        pytest.fail('Stop must not create or acquire pages')

    restart = acquire
    new_chat = acquire
    fresh_chat = acquire


@pytest.mark.parametrize('prepared_request', [False, True])
def test_stop_releases_plan_with_pristine_queued_dev(queued_dev_stop, prepared_request):
    from playwright_auto.durable import RequestLedger
    store, state, worker, writers = queued_dev_stop
    hop = state['hops'][-1]
    ledger_path = Path(hop['ledger_path'])
    if prepared_request:
        RequestLedger(ledger_path).begin(
            role=hop['physical_role'], prompt='prepared only', request_id=hop['request_id'],
        )
    ledger_before = ledger_path.read_bytes() if ledger_path.exists() else None
    hops_before = copy.deepcopy(state['hops'])
    # Unrelated durable ownership must remain untouched as well.
    owned_binding(worker, state, 'DEV', identity='foreign', task='foreign-task')
    foreign_before = writers.get_binding('foreign')
    actions = QueuedDevStopActions(worker.config)
    stopped = stop(store, state, worker, actions)
    assert stopped['cleanup']['state'] == 'STOP_RELEASED', stopped['cleanup']['last_error']
    assert stopped['cleanup']['phase'] == 'writer_released'
    assert stopped['controls'][-1]['status'] == 'applied'
    assert stopped['controls'][-1]['result']['writer_cleanup_pending'] is False
    assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    assert writers.get_binding('foreign') == foreign_before
    assert actions.located_roles == ['PLAN'] and actions.stop_calls == 1
    assert stopped['hops'] == hops_before
    assert (ledger_path.read_bytes() if ledger_path.exists() else None) == ledger_before
    assert actions.closed_teams == 0 and actions.preflight_calls == 0
    assert asyncio.run(worker._continue_stop_writer_cleanup(
        stopped, actions, Path(state['manifest_path']), force_retry=True,
    ))
    assert actions.located_roles == ['PLAN']  # Retry cannot invent DEV ownership.


@pytest.mark.parametrize('status', ['NEW', 'PROMPT_SET'])
@pytest.mark.parametrize('receipt_kind', ['empty', 'none', 'absent', 'populated'])
def test_queued_dev_durable_receipt_roundtrip_controls_stop(
    queued_dev_stop, status, receipt_kind,
):
    import json
    from playwright_auto.durable import RequestLedger, RequestStatus
    store, state, worker, writers = queued_dev_stop
    hop = state['hops'][-1]
    ledger_path = Path(hop['ledger_path'])
    ledger = RequestLedger(ledger_path)
    ledger.begin(
        role=hop['physical_role'], prompt='prepared only', request_id=hop['request_id'],
    )
    receipt = {'accepted': True} if receipt_kind == 'populated' else (
        {} if receipt_kind == 'empty' else None
    )
    record = ledger.update(hop['request_id'], status=RequestStatus[status], receipt=receipt)
    needs_dev = receipt_kind in {'empty', 'populated'}
    assert record.receipt == receipt
    assert record.attempts == 0
    assert record.pristine_before_dispatch() is not needs_dev
    raw = json.loads(ledger_path.read_text(encoding='utf-8'))
    assert raw['records'][hop['request_id']]['receipt'] == receipt
    if receipt_kind == 'absent':
        raw['records'][hop['request_id']].pop('receipt')
        ledger_path.write_text(json.dumps(raw), encoding='utf-8')
    ledger_before = ledger_path.read_bytes()
    ledger_mtime_before = ledger_path.stat().st_mtime_ns
    hops_before = copy.deepcopy(state['hops'])
    actions = QueuedDevStopActions(worker.config)  # No owned DEV page exists.

    stopped = stop(store, state, worker, actions)

    assert stopped['hops'] == hops_before
    assert ledger_path.read_bytes() == ledger_before
    assert ledger_path.stat().st_mtime_ns == ledger_mtime_before
    assert stopped['status'] == 'STOPPED'
    assert stopped['controls'][-1]['status'] == 'applied'
    assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    assert actions.closed_teams == 0 and actions.preflight_calls == 0
    assert actions.stop_calls == 1
    assert stopped['cleanup']['state'] == ('CLEARING' if needs_dev else 'STOP_RELEASED')
    assert actions.located_roles == (['PLAN', 'DEV'] if needs_dev else ['PLAN'])
    assert stopped['cleanup']['phase'] == ('writer_stop_pending' if needs_dev else 'writer_released')
    assert stopped['controls'][-1]['result']['writer_cleanup_pending'] is needs_dev
    if needs_dev:
        assert stopped['cleanup']['last_error']


@pytest.mark.parametrize('evidence', [
    'receipt', 'empty_receipt', 'send_attempt', 'sending', 'durable_attempt',
    'durable_receipt',
    'constructor_sent_generation', 'attachments_uploaded_generation', 'reset_applied_generation',
    'page', 'activity', 'earlier_executed_hop', 'binding',
])
@pytest.mark.parametrize('secondary', ['missing', 'streaming', 'unavailable', 'foreign'])
def test_queued_dev_with_execution_or_ambiguity_still_needs_owned_page(
    queued_dev_stop, evidence, secondary,
):
    from playwright_auto.durable import RequestLedger
    store, state, worker, writers = queued_dev_stop
    hop = state['hops'][-1]
    record = state['roles']['DEV']
    if evidence in {'receipt', 'empty_receipt'}:
        hop['receipt'] = {'accepted': True} if evidence == 'receipt' else {}
    elif evidence == 'send_attempt':
        hop['send_attempt_id'] = 'ambiguous-attempt'
    elif evidence == 'sending':
        hop['state'] = 'sending'
    elif evidence.startswith('durable_'):
        ledger = RequestLedger(hop['ledger_path'])
        ledger.begin(role=hop['physical_role'], prompt='once', request_id=hop['request_id'])
        changes = {'attempts': 1} if evidence == 'durable_attempt' else {
            'receipt': {'accepted': True},
        }
        ledger.update(hop['request_id'], **changes)
    elif evidence.endswith('_generation'):
        record[evidence] = 0
    elif evidence == 'page':
        record['page_id'] = 'lost-dev-page'
    elif evidence == 'activity':
        record['last_activity_at'] = state['created_at']
    elif evidence == 'binding':
        owned_binding(worker, state, 'DEV')  # Binding outranks pristine hop/record.
    else:
        hop['state'] = 'sent'
        worker._append_hop(state, source_role='DEV', target_role='DEV', handoff='again')
    state = store.save(state['manifest_path'], state)
    hops_before = copy.deepcopy(state['hops'])
    ledger_path = Path(state['hops'][0]['ledger_path'])
    ledger_before = ledger_path.read_bytes() if ledger_path.exists() else None
    actions = QueuedDevStopActions(worker.config, secondary=secondary)
    stopped = stop(store, state, worker, actions)
    assert stopped['cleanup']['state'] == 'CLEARING'
    assert stopped['cleanup']['phase'] == 'writer_stop_pending'
    assert stopped['cleanup']['last_error']
    assert 'DEV' in actions.located_roles
    if secondary == 'foreign':
        assert record['physical_role'] not in actions.stopped_roles
    if evidence == 'binding':
        assert writers.get_binding('DEV')['binding_status'] != 'sealed'
    assert stopped['hops'] == hops_before
    assert (ledger_path.read_bytes() if ledger_path.exists() else None) == ledger_before
    assert actions.closed_teams == 0 and actions.preflight_calls == 0


@pytest.mark.parametrize('field', ['request_id', 'ledger_path'])
def test_queued_role_without_request_identity_is_not_proven_pristine(queued_dev_stop, field):
    _, state, worker, _ = queued_dev_stop
    state['hops'][-1][field] = None
    # Invalid manifests cannot reach Stop through TaskStore.save; exercise the
    # selection proof directly to keep malformed identity conservative as well.
    assert worker._stop_role_needs_owned_page(state, 'DEV')


@pytest.mark.parametrize('evidence', [
    'page', 'generation', 'receipt', 'sending', 'durable_attempt', 'durable_accepted',
    'upload_generation_zero', 'wait_started',
])
def test_no_binding_legacy_execution_still_requires_owned_page(tmp_path, evidence):
    from playwright_auto.durable import RequestLedger
    _, store, state, worker = setup_task(tmp_path)
    path = Path(state['manifest_path'])
    if evidence == 'page':
        state['roles']['PLAN']['page_id'] = 'lost-page'
    elif evidence == 'generation':
        state['roles']['PLAN']['conversation_generation'] = 1
    elif evidence == 'receipt':
        state['hops'][0]['receipt'] = {'accepted': True, 'user_message_id': 'accepted-user'}
    elif evidence == 'sending':
        state['hops'][0]['state'] = 'sending'
    elif evidence == 'upload_generation_zero':
        state['roles']['PLAN']['attachments_uploaded_generation'] = 0
    elif evidence == 'wait_started':
        state['hops'][0]['wait']['started_at'] = state['created_at']
    else:
        hop = state['hops'][0]
        ledger = RequestLedger(hop['ledger_path'])
        ledger.begin(role=hop['physical_role'], prompt='once', request_id=hop['request_id'])
        ledger.update(hop['request_id'], attempts=1)
        if evidence == 'durable_accepted':
            ledger.update(hop['request_id'], receipt={'accepted': True, 'user_message_id': 'durable-user'})
    state = store.save(path, state)
    actions = StopActions(missing=True)
    stopped = stop(store, state, worker, actions)
    assert stopped['cleanup']['state'] == 'CLEARING'
    assert stopped['cleanup']['last_error']
    assert actions.located_roles == ['PLAN']


@pytest.mark.parametrize('coverage', ['none', 'partial', 'sealed'])
@pytest.mark.parametrize('evidence', ['page', 'upload_generation_zero', 'hop'])
@pytest.mark.parametrize('secondary', ['streaming', 'unavailable', 'foreign', 'idle'])
def test_stop_checks_unbound_secondary_role_with_durable_evidence(
    tmp_path, coverage, evidence, secondary,
):
    _, store, state, worker = setup_task(tmp_path, roles=('PLAN', 'DEV', 'REVIEW'))
    state['roles']['PLAN']['page_id'] = f"page-{state['roles']['PLAN']['physical_role']}"
    if evidence == 'page':
        state['roles']['DEV']['page_id'] = f"page-{state['roles']['DEV']['physical_role']}"
    elif evidence == 'upload_generation_zero':
        state['roles']['DEV']['attachments_uploaded_generation'] = 0
    else:
        hop = worker._append_hop(state, source_role='PLAN', target_role='DEV', handoff='test')
        hop['state'] = 'sent'
        hop['receipt'] = {'accepted': True, 'user_message_id': 'secondary-accepted-user'}
        # Stop's remembered active role is idle PLAN, not the executing DEV role.
        state['active_role'] = 'PLAN'
        state['active_hop_id'] = 1
    state = store.save(state['manifest_path'], state)
    writers = ConversationWriterLease(worker.runtime_db)
    if coverage != 'none':
        owned_binding(worker, state, 'PLAN')
        if coverage == 'sealed':
            writers.close_binding('PLAN')  # Existing durable release/save-gap evidence.
    owned_binding(worker, state, 'DEV', identity='foreign', task='other-task')
    foreign_before = snapshot(writers)
    hops_before = copy.deepcopy(state['hops'])

    class SecondaryRoleActions(StopActions):
        def __init__(self):
            super().__init__()
            self.secondary = secondary
            self.foreign_pages = ['foreign-page']
            self.stopped_roles = []

        async def locate_owned(self, exact_state, role):
            if role == 'DEV' and self.secondary == 'unavailable':
                self.located_roles.append(role)
                raise RoleOwnershipError('secondary role unavailable')
            acquired = await super().locate_owned(exact_state, role)
            if role == 'DEV':
                current = await acquired.client.assert_ownership()
                acquired.client.assert_ownership.return_value = replace(
                    current, stop_visible=self.secondary == 'streaming',
                    page_task_id='other-task' if self.secondary == 'foreign' else current.page_task_id,
                )
            return acquired

        async def stop_if_active(self, acquired):
            current = await acquired.client.assert_ownership()
            assert current.page_task_id == state['task_id']
            self.stopped_roles.append(current.page_role)
            return await super().stop_if_active(acquired)

    actions = SecondaryRoleActions()
    stopped = stop(store, state, worker, actions)
    assert stopped['status'] == 'STOPPED'
    assert stopped['controls'][-1]['status'] == 'applied'
    if secondary == 'idle':
        assert stopped['cleanup']['state'] == 'STOP_RELEASED'
    else:
        assert stopped['cleanup']['state'] == 'CLEARING'
        assert stopped['cleanup']['phase'] == 'writer_stop_pending'
        expected_error = {
            'streaming': 'still responding', 'unavailable': 'secondary role unavailable',
            'foreign': 'ownership changed',
        }[secondary]
        assert expected_error in stopped['cleanup']['last_error']
    assert 'DEV' in actions.located_roles
    assert 'REVIEW' not in actions.located_roles  # Declared but never executed.
    if secondary == 'foreign':
        assert state['roles']['DEV']['physical_role'] not in actions.stopped_roles
    assert stopped['hops'] == hops_before
    actions.secondary = 'idle'
    if secondary != 'idle':
        assert asyncio.run(worker._continue_cleanup(
            stopped, actions, Path(state['manifest_path']), force_retry=True,
        ))
        assert stopped['cleanup']['state'] == 'STOP_RELEASED'
    after = snapshot(writers)
    assert writers.get_binding('foreign') == foreign_before['bindings']['foreign']
    for key in ('leases', 'page_claims'):
        foreign_entries = {
            k: v for k, v in foreign_before[key].items()
            if (v.get('owner_binding_id') if isinstance(v, dict) else v) == 'foreign'
        }
        assert foreign_entries and all(after[key].get(k) == v for k, v in foreign_entries.items())
    assert actions.foreign_pages == ['foreign-page']
    assert actions.closed_teams == 0 and actions.preflight_calls == 0


class OwnedClearActions(StopActions):
    """Use FakeActions' observed page removals, retaining an unrelated page."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.foreign_pages = ['foreign-page']

    async def close_team(self, state, *, preflighted_pages=None):
        assert state['cleanup']['phase'] == 'closing'
        assert not set(preflighted_pages or []) & set(self.foreign_pages)
        return await super().close_team(state, preflighted_pages=preflighted_pages)


@pytest.mark.parametrize('attempt_state', ['intent_recorded', 'submitted', 'accepted'])
def test_stop_then_explicit_clear_waits_for_writer_then_completes_exact_control(tmp_path, attempt_state):
    _, store, state, worker = setup_task(tmp_path)
    writers, lease = owned_binding(worker, state, 'PLAN')
    owned_binding(worker, state, 'PLAN', identity='foreign', task='other-task')
    foreign = writers.get_binding('foreign')
    attempt, _ = writers.record_send_intent(
        lease, idempotency_key='stop-clear', text='once',
        baseline_last_user_message_id=None, baseline_user_turn_count=0,
    )
    if attempt_state != 'intent_recorded':
        writers.transition_attempt(attempt['attempt_id'], attempt_state)
    actions = OwnedClearActions(streaming=attempt_state == 'accepted')
    stopped = stop(store, state, worker, actions)
    stop_id = stopped['controls'][-1]['control_id']
    path = Path(state['manifest_path'])
    requested = store.request_control(path, 'clear_team')
    clear_id = requested['controls'][-1]['control_id']
    asyncio.run(worker._apply_control(requested, actions, path))
    pending = store.load(path)
    assert pending['cleanup']['phase'] == 'writer_stop_pending'
    assert pending['cleanup']['control_id'] == clear_id
    assert pending['controls'][-1]['status'] == 'cleanup_pending'
    assert actions.closed_teams == 0
    if attempt_state != 'accepted':
        writers.transition_attempt(attempt['attempt_id'], 'reconciled_absent')
    actions.streaming = False
    assert asyncio.run(worker._continue_cleanup(pending, actions, path, force_retry=True))
    cleared = store.load(path)
    assert cleared['cleanup']['state'] == 'CLEARED'
    assert cleared['cleanup']['control_id'] == clear_id
    assert cleared['cleanup']['status_before'] == 'STOPPED'
    assert cleared['cleanup']['closed_tabs'] == 1
    assert cleared['controls'][-1]['status'] == 'applied'
    assert cleared['controls'][-2]['control_id'] == stop_id
    assert cleared['controls'][-2]['status'] == 'applied'
    assert 'writer_cleanup_pending' in cleared['controls'][-2]['result']
    assert actions.preflight_calls == 2
    assert actions.pages == [] and actions.foreign_pages == ['foreign-page']
    assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
    assert writers.get_binding('foreign') == foreign


@pytest.mark.parametrize('failure', ['missing', 'streaming'])
def test_stop_clear_save_gap_resumes_exact_clear_after_writer_seal(tmp_path, monkeypatch, failure):
    from playwright_auto.cdpa_worker import CDPAWorker
    _, store, state, worker = setup_task(tmp_path)
    writers, _ = owned_binding(worker, state, 'PLAN')
    actions = OwnedClearActions(**{failure: True})
    stop(store, state, worker, actions)
    path = Path(state['manifest_path'])
    requested = store.request_control(path, 'clear_team')
    clear_id = requested['controls'][-1]['control_id']
    asyncio.run(worker._apply_control(requested, actions, path))
    pending = store.load(path)
    assert pending['cleanup']['phase'] == 'writer_stop_pending'
    assert actions.closed_teams == 0
    actions.missing = actions.streaming = False
    def crash_after_seal(*args, **kwargs):
        assert writers.get_binding('PLAN')['binding_status'] == 'sealed'
        raise RuntimeError('injected task-save crash after writer seal')

    with monkeypatch.context() as patch:
        patch.setattr(store, 'update', crash_after_seal)
        with pytest.raises(RuntimeError, match='injected task-save crash'):
            asyncio.run(worker._continue_cleanup(pending, actions, path, force_retry=True))
    assert store.load(path)['cleanup']['phase'] == 'writer_stop_pending'
    restarted = CDPAWorker(worker.config, store=store)
    pending = store.load(path)
    assert asyncio.run(restarted._continue_cleanup(pending, actions, path, force_retry=True))
    cleared = store.load(path)
    assert cleared['cleanup']['state'] == 'CLEARED'
    assert cleared['cleanup']['control_id'] == clear_id
    assert cleared['controls'][-1]['status'] == 'applied'
    if failure == 'streaming':
        assert cleared['controls'][-2]['result']['stopped_response'] is True
    assert actions.closed_teams == 1
    assert actions.foreign_pages == ['foreign-page']


@pytest.mark.parametrize('restart', [False, True])
def test_stop_released_never_idle_closes_but_subsequent_explicit_clear_works(tmp_path, monkeypatch, restart):
    import playwright_auto.cdpa_worker as module
    from playwright_auto.cdpa_runtime_registry import CDPARuntimeRegistry
    _, store, state, worker = setup_task(tmp_path)
    owned_binding(worker, state, 'PLAN')
    actions = OwnedClearActions()
    stopped = stop(store, state, worker, actions)
    path = Path(state['manifest_path'])
    for key in ('last_role_activity_at', 'stopped_at'):
        stopped[key] = '2000-01-01T00:00:00+00:00'
    store.save(path, stopped)
    if restart:
        worker = module.CDPAWorker(worker.config, store=store)
        worker.hydrate_runtime()
    monkeypatch.setattr(module, 'CDPATabActions', lambda *_a, **_k: actions)
    result = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert result['cleanup']['state'] == 'STOP_RELEASED'
    assert actions.closed_teams == 0 and actions.preflight_calls == 0
    registry = CDPARuntimeRegistry.hydrate([result], now=1000, cleanup_idle_seconds=300)
    assert registry.next_due_at() is None
    requested = store.request_control(path, 'clear_team')
    registry.update_task(requested, now=1001)
    assert registry.next_due_at() == 1001.5
    cleared = asyncio.run(worker.advance(path, None, scheduling_tasks=store.discover()))
    assert cleared['cleanup']['state'] == 'CLEARED'
    assert cleared['controls'][-1]['status'] == 'applied'
    assert cleared['cleanup']['closed_tabs'] == 1
    assert actions.foreign_pages == ['foreign-page']
