"""A fresh role context must not lose the exact unsent repair contract."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_worker import _active_hop
from playwright_auto.durable import RequestLedger, RequestStatus
from test_cdpa_worker import FakeActions, _prepare_sent_waiting_task


@pytest.mark.parametrize('fresh_generation', [False, True])
def test_minimal_continuation_is_only_used_in_original_context(tmp_path, fresh_generation):
    _store, state, worker, _path, parent, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id='continuation-context'
    )
    state['roles']['PLAN']['conversation_generation'] = 2
    parent['conversation_generation'] = 2
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    assert repair['conversation_generation'] == 2
    before = RequestLedger(parent['ledger_path']).peek(parent['request_id']).to_dict()
    if fresh_generation:
        state['roles']['PLAN']['conversation_generation'] = 3
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    if fresh_generation:
        assert repair['prompt'] != 'Continue from the current state.'
        assert 'repairing the route response' in repair['prompt']
        assert repair['expected_report_path'] in repair['prompt']
        assert 'Do not redo completed' in repair['prompt']
        assert str(state['repository']) in repair['prompt']
        assert parent['prompt'] not in repair['prompt']
        assert state['task_text'] not in repair['prompt']
    else:
        assert repair['prompt'] == 'Continue from the current state.'
    assert RequestLedger(parent['ledger_path']).peek(parent['request_id']).to_dict() == before
    assert RequestLedger(repair['ledger_path']).peek(repair['request_id']) is None


@pytest.mark.parametrize('kind', ['route_repair', 'report_repair'])
@pytest.mark.parametrize('binding_status', ['BOUND', 'NEW_PENDING'])
def test_url_supersession_preserves_exact_repair_contract(tmp_path, kind, binding_status):
    _store, state, worker, _path, parent, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id='repair-url-supersession'
    )
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    repair.update(kind=kind, conversation_generation=2, expected_report_path=parent['expected_report_path'])
    if kind == 'report_repair':
        repair.update(locked_route='DEV', locked_handoff=parent['expected_report_path'])
    record = state['roles']['PLAN']
    record.update(conversation_generation=3, role_url_generation=1)
    record['role_url_binding'] = {'generation':1, 'status':binding_status, 'page_id':'verified-fresh-page'}
    original = deepcopy(repair)
    parent_before = deepcopy(parent)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    replacement = _active_hop(state)
    assert replacement['hop_id'] != repair['hop_id']
    assert replacement['state'] == 'pre_send'
    for key in ('kind','turn','repair_attempt','validation_error','expected_report_path',
                'conversation_generation','locked_route','locked_handoff'):
        if key in original:
            assert replacement.get(key) == original[key], key
    assert replacement.get('minimal_continuation_prompt') is not True
    assert replacement['role_url_generation'] == 1
    assert replacement['request_id'] != original['request_id']
    assert replacement['receipt'] is None
    assert repair['superseded_by_hop_id'] == replacement['hop_id']
    assert parent == parent_before
    assert RequestLedger(parent['ledger_path']).peek(parent['request_id']).attempts == 1
    asyncio.run(worker._pre_send(state, replacement, FakeActions()))
    assert replacement['state'] == 'sending'
    assert replacement['prompt'] != 'Continue from the current state.'
    assert replacement['expected_report_path'] == original['expected_report_path']
    if kind == 'report_repair':
        assert replacement['locked_handoff'] in replacement['prompt']
        assert 'DEV' in replacement['prompt']


@pytest.mark.parametrize('status', ['RUNNING', 'PAUSED'])
def test_allocated_homepage_reconciliation_supersedes_only_running_pristine_repair(tmp_path, status):
    _store, state, worker, _path, parent, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id='fresh-binding-repair'
    )
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    state['status'] = status
    record = state['roles']['PLAN']
    record.update(role_url_generation=1, page_id='fresh', page_url=None)
    record['role_url_binding'] = {'generation':1,'status':'NEW_PENDING','page_id':'fresh'}
    pending = asyncio.run(worker._reconcile_role_url_bindings(state, SimpleNamespace(), endpoint_id=None))
    if status == 'RUNNING':
        assert pending is False
        assert _active_hop(state)['hop_id'] != repair['hop_id']
        assert _active_hop(state)['validation_error'] == repair['validation_error']
        assert _active_hop(state)['repair_attempt'] == repair['repair_attempt']
    else:
        assert pending is True
        assert _active_hop(state) is repair
        assert state['status'] == 'PAUSED'


@pytest.mark.parametrize('boundary', ['accepted_receipt', 'ambiguous_ledger'])
def test_url_supersession_never_discards_crossed_repair_evidence(tmp_path, boundary):
    _store, state, worker, _path, parent, receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id='crossed-repair-binding'
    )
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    if boundary == 'accepted_receipt':
        repair['receipt'] = receipt.to_dict()
    else:
        ledger = RequestLedger(repair['ledger_path'])
        ledger.begin(role=repair['physical_role'], prompt='repair draft', request_id=repair['request_id'])
        ledger.update(repair['request_id'], status=RequestStatus.SENDING, attempts=1)
    record = state['roles']['PLAN']
    record.update(role_url_generation=1, page_url=None)
    record['role_url_binding'] = {'generation':1, 'status':'NEW_PENDING', 'page_id':'fresh'}
    before = deepcopy(repair)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert _active_hop(state) is repair
    assert {k:v for k,v in repair.items() if k != 'errors'} == {k:v for k,v in before.items() if k != 'errors'}
    assert state['status'] == 'BLOCKED'


def test_unallocated_homepage_cannot_supersede_or_prepare(tmp_path):
    _store, state, worker, _path, parent, _receipt, _sent_at = _prepare_sent_waiting_task(
        tmp_path, task_id='unallocated-binding'
    )
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    record = state['roles']['PLAN']
    record.update(role_url_generation=1, page_url=None)
    record['role_url_binding'] = {'generation':1,'status':'NEW_PENDING'}
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert state['status'] == 'BLOCKED'
    assert _active_hop(state) is repair
    assert repair['prompt'] is None
    assert RequestLedger(repair['ledger_path']).peek(repair['request_id']) is None
