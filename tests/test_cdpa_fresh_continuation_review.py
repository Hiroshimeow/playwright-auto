"""Integration regressions for allocation and deferred readiness across cycles."""
import asyncio
from types import SimpleNamespace

from playwright_auto.cdpa_actions import AcquiredRole
from playwright_auto.cdpa_worker import _active_hop
from test_cdpa_change_role_url import pinned_task
from test_cdpa_worker import FakeActions, ready_client


def test_verified_homepage_allocation_invalidates_context_free_continuation(tmp_path):
    _config, store, state, worker = pinned_task(tmp_path, task_id='homepage-continuation')
    parent = _active_hop(state)
    state['roles']['PLAN']['conversation_generation'] = 2
    parent['conversation_generation'] = 2
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    store._record_role_url_intent(state, role='PLAN', url='https://chatgpt.com/',
                                browser_endpoint_id=state['browser_endpoint_id'], external_command_id='fresh-url')
    class Actions(FakeActions):
        async def locate_owned(self, *_): return None
        async def fresh_chat(self, state, role, **_):
            return AcquiredRole(client=ready_client(state, role), page_id=f"page-{state['roles'][role]['physical_role']}",
                                url='https://chatgpt.com/', created=True, new_chat=True)
    asyncio.run(worker._reconcile_role_url_bindings(state, Actions(), endpoint_id=state['browser_endpoint_id']))
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert repair['prompt'] != 'Continue from the current state.'
    assert 'repairing the route response' in repair['prompt']
    assert state['roles']['PLAN']['conversation_generation'] == 3


def test_canonical_url_supersession_invalidates_same_generation_continuation(tmp_path):
    _config, _store, state, worker = pinned_task(tmp_path, task_id='canonical-continuation')
    parent = _active_hop(state)
    parent['conversation_generation'] = 2
    state['roles']['PLAN']['conversation_generation'] = 2
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    role = state['roles']['PLAN']
    role.update(role_url_generation=1)
    role['role_url_binding'] = {'generation':1,'status':'BOUND','page_id':'new-canonical-page'}
    asyncio.run(worker._pre_send(state, _active_hop(state), FakeActions()))
    repair = _active_hop(state)
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert repair['prompt'] != 'Continue from the current state.'
    assert 'repairing the route response' in repair['prompt']


def test_deferred_readiness_cannot_restore_context_free_continuation(tmp_path):
    _config, _store, state, worker = pinned_task(tmp_path, task_id='deferred-continuation')
    parent = _active_hop(state)
    parent['conversation_generation'] = 2
    state['roles']['PLAN']['conversation_generation'] = 2
    worker._queue_continuation_repair(state, parent, 'stopped_response_continue')
    repair = _active_hop(state)
    state['roles']['PLAN']['conversation_generation'] = 3
    class Controller:
        calls = 0
        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(reason='composer_pending' if self.calls == 1 else 'ready')
    worker.role_controller = Controller()
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert repair['state'] == 'pre_send'
    asyncio.run(worker._pre_send(state, repair, FakeActions()))
    assert repair['prompt'] != 'Continue from the current state.'
    assert 'repairing the route response' in repair['prompt']
