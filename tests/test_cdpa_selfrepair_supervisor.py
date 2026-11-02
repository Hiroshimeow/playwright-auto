"""Focused G6 self-repair scheduler invariants."""
import importlib.util
from pathlib import Path
import pytest

FILE = Path(__file__).resolve().parents[1] / 'workflows' / 'self-repair' / 'supervisor.py'
spec = importlib.util.spec_from_file_location('cdpa_g6_supervisor', FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(mod, 'PROGRESS', tmp_path / 'progress.md')
    config = mod.load(mod.CONFIG)
    state = mod.new_state(config)
    return config, state


def test_deterministic_create_and_reconcile(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    calls = []
    def fake(method, path, data=None, key=None):
        calls.append((method,path,key))
        return {'task_id':'task-one','command_id':'cmd-one'}
    monkeypatch.setattr(mod,'api',fake)
    part=config['slices'][0]
    mod.start_slice(config,state,[],part)
    assert state['active']['task_id']=='task-one'
    assert state['phase']=='ACTIVE'
    assert len(calls)==1
    existing={'team':state['active']['team'],'task_id':'task-one'}
    mod.start_slice(config,state,[existing],part)
    assert len(calls)==1
    assert mod.key_for(config,0,'create')==mod.key_for(config,0,'create')


@pytest.mark.parametrize('retry', [0, 1, 2])
def test_selfrepair_team_name_preserves_retry_identity_below_provider_limit(retry):
    name = mod.team_for_slice(16, '15-real-terminal-reacceptance', retry)
    assert len(name) <= 48
    assert name.startswith('g6-selfrepair-17-')
    if retry:
        assert name.endswith(f'-retry{retry}')
    assert len({mod.team_for_slice(16, '15-real-terminal-reacceptance', i)
                for i in (0, 1, 2)}) == 3


def test_existing_truncated_team_reconciles_only_with_exact_task_id(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    state['next_index'] = 16
    state['active'] = {
        'slice_id': '15-real-terminal-reacceptance',
        'team': 'g6-selfrepair-17-15-real-terminal-reacceptance-retry1',
        'task_id': 'exact-accepted-task',
    }
    canonical = state['active']['team'][:48].rstrip('-_')
    board = [{'team': canonical, 'task_id': 'unrelated-task', 'status': 'RUNNING'},
             {'team': canonical, 'task_id': 'exact-accepted-task',
              'status': 'RUNNING', 'active_action': 'wait_response'}]
    mod.process_active(config, state, board)
    assert state['active']['team'] == canonical
    assert state['active']['task_id'] == 'exact-accepted-task'
    assert state['phase'] == 'ACTIVE'


def test_truncated_team_does_not_adopt_unrelated_task(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    state['active'] = {
        'slice_id': '15-real-terminal-reacceptance',
        'team': 'g6-selfrepair-17-15-real-terminal-reacceptance-retry1',
        'task_id': 'exact-accepted-task',
    }
    canonical = state['active']['team'][:48].rstrip('-_')
    mod.process_active(config, state, [
        {'team': canonical, 'task_id': 'unrelated-task', 'status': 'RUNNING'}])
    assert state['phase'] == 'RECONCILE'
    assert state['active']['team'] != canonical


def test_solo_prompt_delegates_runtime_reload_and_model_selection_to_existing_controller(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    prompt = mod.make_prompt(config, {'id': '15-real-terminal-reacceptance',
                                      'task': 'Return exactly one JSON route'}, state)
    assert 'STOP the affected CDPA task' not in prompt
    assert 'select ChatGPT High or Extra High before composer fill' not in prompt
    assert 'never stop or reload the CDPA task you are running in' in prompt
    assert 'Do not select a model, fill the composer or Send' in prompt
    assert 'never replay accepted Send' in prompt


def test_blocked_task_cannot_spawn_sibling(monkeypatch, tmp_path):
    config, state = setup(monkeypatch,tmp_path)
    state['active']={'slice_id':'01-inventory','team':'one','task_id':'one'}
    mod.process_active(config,state,[{'team':'one','task_id':'one','status':'BLOCKED','block_code':'test_failure'}])
    assert state['phase']=='NEEDS_PI'
    assert state['active']['task_id']=='one'
    assert state['next_index']==0


def test_no_done_without_valid_receipt(monkeypatch, tmp_path):
    config, state = setup(monkeypatch,tmp_path)
    state['active']={'slice_id':'01-inventory','team':'one','task_id':'one'}
    monkeypatch.setattr(mod,'HERE',tmp_path)
    with pytest.raises(RuntimeError, match='receipt'):
        mod.process_active(config,state,[{'team':'one','task_id':'one','status':'DONE'}])
    assert state['next_index']==0

def test_diagnostic_blocked_can_continue_only_after_proof(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    state['active']={'slice_id':'01-inventory','team':'one','task_id':'one'}
    monkeypatch.setattr(mod,'HERE',tmp_path)
    (tmp_path / 'receipts').mkdir()
    (tmp_path / 'receipts' / '01-inventory.json').write_text(
        '{"task_id":"one","status":"BLOCKED","evidence":{"ordered_minimum_fixes":[{"source":"x"}]}}')
    blocked={'team':'one','task_id':'one','status':'BLOCKED','active_action':'blocked',
             'block_code':'response_terminal_missing','browser_control':{'stop_visible':False}}
    mod.process_active(config,state,[blocked])
    assert state['next_index']==1
    assert state['active'] is None
    assert state['archived_blocked']==['one']
    calls=[]
    monkeypatch.setattr(mod,'api',lambda *a: (calls.append(a) or {'task_id':'two','command_id':'cmd-two'}))
    mod.start_slice(config,state,[blocked],config['slices'][1])
    assert state['active']['task_id']=='two'
    assert len(calls)==1

def test_challenge_after_verified_browser_restart_allows_only_fresh_recovery(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    report = tmp_path / '.runtime' / 'self-repair' / 'browser9202-restore-report.json'
    report.parent.mkdir(parents=True)
    report.write_text('{"results":[{"status":"opened"}]}', encoding='utf-8')
    state['next_index'] = 1
    state['archived_blocked'] = ['old-inventory']
    state['active'] = {
        'slice_id': '01b-terminal-recovery', 'team': 'team-02',
        'task_id': 'task-blocked', 'submitted_at': '2026-10-26T00:00:00+00:00'
    }
    blocked = {'team': 'team-02', 'task_id': 'task-blocked', 'status': 'BLOCKED',
               'active_action': 'blocked', 'block_code': 'provider_challenge',
               'browser_control': {'stop_visible': False}}
    mod.process_active(config, state, [blocked])
    assert state['phase'] == 'IDLE'
    assert state['next_index'] == 2
    assert state['active'] is None
    assert 'task-blocked' in state['archived_blocked']
    assert state['blocked_history'][0]['status'] == 'BLOCKED'
    calls = []
    monkeypatch.setattr(mod, 'api', lambda *args: (calls.append(args) or {
        'task_id': 'fresh-task', 'command_id': 'fresh-command'}))
    mod.start_slice(config, state, [blocked], config['slices'][2])
    assert len(calls) == 1
    assert state['active']['task_id'] == 'fresh-task'
    assert state['active']['slice_id'] == '01c-terminal-fresh-retry'
    mod.process_active(config, state, [{
        'team': state['active']['team'], 'task_id': 'fresh-task', 'status': 'BLOCKED',
        'block_code': 'provider_challenge', 'active_action': 'blocked'}])
    assert state['phase'] == 'NEEDS_PI'
    assert state['next_index'] == 2


def test_challenge_without_restart_proof_does_not_spawn(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    state['next_index'] = 1
    state['active'] = {
        'slice_id': '01b-terminal-recovery', 'team': 'team-02',
        'task_id': 'task-blocked', 'submitted_at': '2026-10-26T00:00:00+00:00'
    }
    mod.process_active(config, state, [{
        'team': 'team-02', 'task_id': 'task-blocked', 'status': 'BLOCKED',
        'active_action': 'blocked', 'block_code': 'provider_challenge',
        'browser_control': {'stop_visible': False}
    }])
    assert state['phase'] == 'NEEDS_PI'
    assert state['active']['task_id'] == 'task-blocked'
    assert state['next_index'] == 1


def test_completed_code_receipt_may_advance_from_terminal_missing_only_when_page_offline(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'HERE', tmp_path)
    (tmp_path / 'receipts').mkdir()
    (tmp_path / 'receipts' / '01c-terminal-fresh-retry.json').write_text(
        '{"slice_id":"01c-terminal-fresh-retry","task_id":"01c-task","status":"PASS",'
        '"tests":[{"command":"pytest -q tests/test_role_runtime.py","result":"PASS 96 passed"}],"evidence":{"fix":"semantic heading"}}',
        encoding='utf-8')
    state['next_index'] = 2
    state['active'] = {'slice_id':'01c-terminal-fresh-retry','team':'01c-team','task_id':'01c-task'}
    blocked = {'team':'01c-team','task_id':'01c-task','status':'BLOCKED',
        'active_action':'blocked','block_code':'response_terminal_missing',
        'availability':'offline','browser_control':{'stop_visible':False,'retry_visible':False}}
    mod.process_active(config,state,[blocked])
    assert state['next_index'] == 3
    assert state['phase'] == 'IDLE'
    assert state['active'] is None
    assert state['archived_blocked'] == ['01c-task']
    assert state['release'] == 'NOT_ACCEPTED'
    assert state['blocked_history'][-1]['block_code'] == 'response_terminal_missing'


def test_terminal_missing_with_active_browser_does_not_advance(monkeypatch, tmp_path):
    config, state = setup(monkeypatch,tmp_path)
    state['next_index'] = 2
    state['active'] = {'slice_id':'01c-terminal-fresh-retry','team':'01c-team','task_id':'01c-task'}
    blocked = {'team':'01c-team','task_id':'01c-task','status':'BLOCKED',
        'active_action':'blocked','block_code':'response_terminal_missing',
        'availability':'online','browser_control':{'stop_visible':False}}
    mod.process_active(config,state,[blocked])
    assert state['next_index'] == 2
    assert state['phase'] == 'NEEDS_PI'


@pytest.mark.parametrize('slice_id', ['15b-fresh-terminal-proof', '16-final-release-evidence'])
def test_blocked_review_receipt_uses_existing_reducer_not_pi_timeout(monkeypatch,tmp_path,slice_id):
    config, state = setup(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,'HERE',tmp_path)
    monkeypatch.setattr(mod,'CONFIG',tmp_path/'slices.json')
    monkeypatch.setattr(mod,'LOCK',tmp_path/'supervisor.lock')
    (tmp_path/'receipts').mkdir()
    (tmp_path/'slices.json').write_text(__import__('json').dumps(config),encoding='utf8')
    idx = next(i for i,p in enumerate(config['slices']) if p['id']==slice_id)
    state.update(phase='NEEDS_PI',pi_attempts=config['max_recovery_attempts'],next_index=idx,
                 active={'slice_id':slice_id,
                         'team':'owned-team','task_id':'task-review'})
    (tmp_path/'receipts'/(slice_id+'.json')).write_text(
        __import__('json').dumps({'slice_id':slice_id,
                                   'task_id':'task-review','status':'BLOCKED'}),
        encoding='utf8')
    mod.save(state)
    called=[]
    def fake_tick(cfg,current):
        called.append(current['active']['task_id'])
        current['phase']='REDUCED'
    monkeypatch.setattr(mod,'tick',fake_tick)
    monkeypatch.setattr(mod,'api',lambda method,path,*args,**kwargs: {
        'ok':True,'worker_online':True,'worker_ready':True,
        'worker_runtime_ready':True,'worker_command_loop_ready':True,
        'worker_source_drift':{'drifted':False}
    } if path=='/health' else pytest.fail('Unexpected API'))
    monkeypatch.setattr(mod,'pi_command',lambda *args: pytest.fail('Unnecessary Pi timeout'))
    mod.supervise_once()
    assert called==['task-review']


def test_recovery_budget_caps_pi_attempts(monkeypatch, tmp_path):
    config, state = setup(monkeypatch,tmp_path)
    state['phase'] = 'NEEDS_PI'
    state['pi_attempts'] = config['max_recovery_attempts']
    state['active'] = {'slice_id':'01c-terminal-fresh-retry','team':'01c','task_id':'01c-task'}
    monkeypatch.setattr(mod,'CONFIG',tmp_path/'slices.json')
    monkeypatch.setattr(mod,'LOCK',tmp_path/'supervisor.lock')
    (tmp_path/'slices.json').write_text(__import__('json').dumps(config),encoding='utf-8')
    mod.save(state)
    monkeypatch.setattr(mod,'pi_command',lambda *args: pytest.fail('Pi should not run after cap'))
    # The test owns a local reducer state; do not read/reload the live G6 worker.
    monkeypatch.setattr(mod, 'api', lambda method, path, *a, **k: {
        'ok': True, 'worker_online': True, 'worker_ready': True,
        'worker_runtime_ready': True, 'worker_command_loop_ready': True,
        'worker_source_drift': {'drifted': False},
    } if path == '/health' else pytest.fail('Unexpected live API request'))
    mod.supervise_once()
    latest=mod.load(mod.STATE)
    assert latest['phase'] == 'RECOVERY_EXHAUSTED'
    assert latest['pi_attempts'] == config['max_recovery_attempts']

def test_verified_blocked_code_slice_advances_without_resending(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'HERE', tmp_path)
    (tmp_path / 'receipts').mkdir()
    receipt = {
        'slice_id': '02-writer-lease', 'task_id': 'task-04', 'status': 'PASS',
        'tests': [{'command': 'pytest -q tests/test_spec003_gate_c_runtime.py', 'result': 'PASS 23 passed'}],
        'evidence': {'finding': 'already fenced'},
        'changed_files': ['tests/test_spec003_gate_c_runtime.py']
    }
    (tmp_path / 'receipts' / '02-writer-lease.json').write_text(
        __import__('json').dumps(receipt), encoding='utf-8')
    state['next_index'] = 3
    state['active'] = {'slice_id': '02-writer-lease', 'team': 'team-04', 'task_id': 'task-04'}
    blocked = {'team': 'team-04', 'task_id': 'task-04', 'status': 'BLOCKED',
               'block_code': 'response_terminal_missing', 'active_action': 'blocked',
               'availability': 'offline', 'browser_control': {'stop_visible': False, 'retry_visible': False}}
    mod.process_active(config, state, [blocked])
    assert state['phase'] == 'IDLE'
    assert state['next_index'] == 4
    assert state['active'] is None
    assert state['release'] == 'NOT_ACCEPTED'
    assert state['archived_blocked'] == ['task-04']
    assert state['completed'][-1]['task_id'] == 'task-04'
    assert state['completed'][-1]['task_terminal_status'] == 'BLOCKED'


@pytest.mark.parametrize('invalid', [
    {'availability': 'online'},
    {'browser_control': {'stop_visible': True, 'retry_visible': False}},
    {'browser_control': {'stop_visible': False, 'retry_visible': True}},
    {'block_code': 'provider_challenge'},
    {'status': 'RUNNING'},
])
def test_blocked_code_receipt_cannot_override_live_or_challenged_send(monkeypatch, tmp_path, invalid):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'HERE', tmp_path)
    (tmp_path / 'receipts').mkdir()
    receipt = {'slice_id': '02-writer-lease', 'task_id': 'task-04', 'status': 'PASS',
               'tests': [{'command': 'pytest -q tests/test_spec003_gate_c_runtime.py', 'result': 'PASS 23 passed'}], 'evidence': {'finding': 'done'}}
    (tmp_path / 'receipts' / '02-writer-lease.json').write_text(__import__('json').dumps(receipt))
    state['next_index'] = 3
    state['active'] = {'slice_id': '02-writer-lease', 'team': 'team-04', 'task_id': 'task-04'}
    blocked = {'team': 'team-04', 'task_id': 'task-04', 'status': 'BLOCKED',
               'block_code': 'response_terminal_missing', 'active_action': 'blocked',
               'availability': 'offline', 'browser_control': {'stop_visible': False, 'retry_visible': False}}
    blocked.update(invalid)
    mod.process_active(config, state, [blocked])
    assert state['next_index'] == 3
    assert state['active']['task_id'] == 'task-04'

def test_pi_recovery_process_uses_closed_stdin_and_real_pi_tools(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    captured = {}
    original_is_file = mod.Path.is_file
    monkeypatch.setattr(mod.Path, 'is_file',
        lambda path: True if path.name == 'cli.js' else original_is_file(path))
    class Result:
        returncode = 0
        stdout = 'PI_RECOVERY_DONE'
        stderr = ''
    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured.update(kwargs)
        return Result()
    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    ok, _ = mod.pi_command(config, 'recovery', state)
    assert ok is True
    assert captured['stdin'] == mod.subprocess.DEVNULL
    assert '--offline' in captured['cmd']
    assert '--no-session' in captured['cmd']
    assert '--no-prompt-templates' in captured['cmd']
    assert 'read,bash,edit,write' in captured['cmd']
    assert 'read,powershell,edit,write' not in captured['cmd']

def test_real_browser_release_never_advances_from_artifact_only(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'HERE', tmp_path)
    (tmp_path / 'receipts').mkdir()
    last = len(config['slices']) - 1
    state['next_index'] = last
    state['active'] = {'slice_id': '08-real-browser', 'team': 'team-real', 'task_id': 'real-task'}
    receipt = {'slice_id': '08-real-browser', 'task_id': 'real-task', 'status': 'PASS',
               'tests': [{'command': 'pytest -q tests/test_spec003_gate_c_runtime.py', 'result': 'PASS 20 passed'}],
               'evidence': {'note': 'unit tests only'}}
    (tmp_path / 'receipts' / '08-real-browser.json').write_text(__import__('json').dumps(receipt))
    blocked = {'team': 'team-real', 'task_id': 'real-task', 'status': 'BLOCKED',
               'block_code': 'response_terminal_missing', 'active_action': 'blocked',
               'availability': 'offline', 'browser_control': {'stop_visible': False, 'retry_visible': False}}
    mod.process_active(config, state, [blocked])
    assert state['next_index'] == last
    assert state['active']['task_id'] == 'real-task'
    assert state['phase'] == 'NEEDS_PI'
    assert state['release'] == 'NOT_ACCEPTED'


def test_running_stop_visible_does_not_spawn_competing_pi_repair(monkeypatch, tmp_path):
    import datetime as dt
    config, state = setup(monkeypatch, tmp_path)
    state['next_index'] = 4
    task = {'team': 'team-05', 'task_id': 'task-05', 'status': 'RUNNING',
            'active_action': 'wait_response', 'browser_control': {'stop_visible': True}}
    state['active'] = {'slice_id': '03-adopted-url', 'team': 'team-05',
                       'task_id': 'task-05', 'status': 'RUNNING', 'action': 'wait_response',
                       'signature': 'RUNNING:wait_response:None',
                       'progress_at': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()}
    monkeypatch.setattr(mod, 'check_environment', lambda config, allow_source_drift=False: {'worker_source_drift': {'drifted': False}})
    monkeypatch.setattr(mod, 'api', lambda *args: {'items': [task]})
    reloads = []
    monkeypatch.setattr(mod, 'reload_g6_runtime', lambda state: reloads.append(state['active']['task_id']))
    mod.tick(config, state)
    assert reloads == ['task-05']
    assert state['phase'] == 'ACTIVE'
    assert state['active']['task_id'] == 'task-05'

def test_applied_stale_empty_assistant_stop_advances_verified_code_slice(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, 'HERE', tmp_path)
    (tmp_path / 'receipts').mkdir()
    receipt = {
        'slice_id':'03-adopted-url', 'task_id':'task05','status':'PASS',
        'tests':[{'command':'pytest -q tests/test_spec003_gate_c_runtime.py', 'result':'PASS: 26 passed'}],
        'evidence':{'root_cause':'adopted initial URL fixed'},
    }
    (tmp_path / 'receipts' / '03-adopted-url.json').write_text(
        __import__('json').dumps(receipt), encoding='utf-8')
    state['next_index'] = 4
    state['active'] = {'slice_id':'03-adopted-url', 'team':'team05','task_id':'task05'}
    why = 'G6 self-repair stale empty assistant: verified code receipt PASS, accepted Send once'
    board = {'team':'team05','task_id':'task05','status':'STOPPED',
             'active_action':'stopped','stop_reason':why,'availability':'terminal_tab_present',
             'browser_control':{'stop_visible':False, 'retry_visible':False}}
    detail = {**board,'controls':[{'action':'stop','status':'applied','reason':why}]}
    calls = []
    def fake_api(method, path):
        calls.append((method,path))
        return detail
    monkeypatch.setattr(mod,'api',fake_api)
    mod.process_active(config,state,[board])
    assert calls == [('GET','/api/tasks/task05')]
    assert state['next_index'] == 5
    assert state['phase'] == 'IDLE'
    assert state['active'] is None
    assert state['release'] == 'NOT_ACCEPTED'
    assert state['completed'][-1]['task_terminal_status'] == 'STOPPED'
    assert state['archived_blocked'] == ['task05']


@pytest.mark.parametrize('broken', [
    {'board':{'stop_reason':'unrelated operator Stop'}},
    {'detail':{'team':'another-team'}},
    {'detail':{'controls':[{'action':'stop','status':'requested'}]}},
    {'detail':{'controls':[{'action':'stop','status':'applied','reason':'other reason'}]}},
    {'detail':{'status':'RUNNING'}},
    {'board':{'browser_control':{'stop_visible':True,'retry_visible':False}}},
    {'board':{'browser_control':{'stop_visible':False,'retry_visible':True}}},
])
def test_unverified_stopped_slice_cannot_advance(monkeypatch,tmp_path,broken):
    config,state=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,'HERE',tmp_path)
    (tmp_path/'receipts').mkdir()
    (tmp_path/'receipts'/'03-adopted-url.json').write_text(__import__('json').dumps({
        'slice_id':'03-adopted-url','task_id':'task05','status':'PASS',
        'tests':[{'command':'pytest tests/test_spec003_gate_c_runtime.py','result':'PASS'}],
        'evidence':{'root_cause':'fixed'}}))
    state['next_index']=4
    state['active']={'slice_id':'03-adopted-url','team':'team05','task_id':'task05'}
    why='G6 self-repair stale empty assistant: verified code receipt PASS'
    board={'team':'team05','task_id':'task05','status':'STOPPED','active_action':'stopped',
           'stop_reason':why,'browser_control':{'stop_visible':False,'retry_visible':False}}
    board.update(broken.get('board',{}))
    detail={**board,'controls':[{'action':'stop','status':'applied','reason':why}]}
    detail.update(broken.get('detail',{}))
    monkeypatch.setattr(mod,'api',lambda method,path: detail)
    mod.process_active(config,state,[board])
    assert state['next_index']==4
    assert state['active']['task_id']=='task05'
    assert state['phase']=='NEEDS_PI'

def test_stale_verified_code_slice_requests_one_official_stop(monkeypatch, tmp_path):
    import datetime as dt
    config, state = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mod,'HERE',tmp_path)
    (tmp_path/'receipts').mkdir()
    (tmp_path/'receipts'/'04-direct-chat.json').write_text(__import__('json').dumps({
        'slice_id':'04-direct-chat','task_id':'task06','status':'PASS',
        'tests':[{'command':'pytest tests/test_gate_c_direct_chat_worker.py','result':'PASS 23 passed'}],
        'evidence':{'fixed':'direct chat owner'}}),encoding='utf-8')
    state['next_index']=5
    state['active']={'slice_id':'04-direct-chat','team':'team06','task_id':'task06'}
    old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=34)).isoformat()
    item={'team':'team06','task_id':'task06','status':'RUNNING','active_action':'wait_response',
          'effective_activity_at':old,'version':9,
          'browser_control':{'stop_visible':False,'retry_visible':False,'composer_nonempty':False}}
    detail={**item,'active_hop':{'state':'waiting','kind':'task'},
            'control_eligibility':{'stop':{'eligible':True}},'active_role':'DEV'}
    calls=[]
    def fake_api(method,path,payload=None,key=None):
        calls.append((method,path,payload,key))
        return detail if method=='GET' else {'command_id':'cmd-stop-06'}
    monkeypatch.setattr(mod,'api',fake_api)
    mod.maybe_stop_stale_code_slice(config,state,item)
    assert [c[0] for c in calls]==['GET','POST']
    assert calls[1][1]=='/api/tasks/task06/controls'
    assert calls[1][2]['action']=='stop'
    assert calls[1][2]['expected_task_version']==9
    assert state['active']['stop_command_id']=='cmd-stop-06'
    assert state['active']['task_id']=='task06'
    mod.maybe_stop_stale_code_slice(config,state,item)
    assert len(calls)==2


@pytest.mark.parametrize('changes', [
    {'browser_control':{'stop_visible':True,'retry_visible':False,'composer_nonempty':False}},
    {'browser_control':{'stop_visible':False,'retry_visible':True,'composer_nonempty':False}},
    {'browser_control':{'stop_visible':False,'retry_visible':False,'composer_nonempty':True}},
    {'active_action':'send'},
    {'status':'BLOCKED'},
])
def test_no_stale_stop_for_active_or_ambiguous_send(monkeypatch,tmp_path,changes):
    import datetime as dt
    config,state=setup(monkeypatch,tmp_path)
    state['next_index']=5
    state['active']={'slice_id':'04-direct-chat','team':'team06','task_id':'task06'}
    item={'team':'team06','task_id':'task06','status':'RUNNING','active_action':'wait_response',
          'effective_activity_at':(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=40)).isoformat(),
          'browser_control':{'stop_visible':False,'retry_visible':False,'composer_nonempty':False}}
    item.update(changes)
    monkeypatch.setattr(mod,'api',lambda *a,**k:pytest.fail('No API calls allowed'))
    mod.maybe_stop_stale_code_slice(config,state,item)
    assert not state['active'].get('stop_command_id')

def test_source_drift_allows_reconciliation_but_blocks_new_slice(monkeypatch, tmp_path):
    config, state = setup(monkeypatch, tmp_path)
    health={'ok':True,'worker_online':True,'worker_ready':True,
            'worker_runtime_ready':True,'worker_command_loop_ready':True,
            'worker_source_drift':{'drifted':True}}
    view={'browser':{'endpoints':[{'endpoint_id':config['browser_endpoint_id'],
                                  'connected':True,'cdp_url':'http://127.0.0.1:9202'}]}}
    calls=[]
    def fake_api(method,path,payload=None,key=None):
        calls.append((method,path))
        return health if path=='/health' else view if path=='/api/state' else {'items':[]}
    monkeypatch.setattr(mod,'api',fake_api)
    reloads = []
    monkeypatch.setattr(mod, 'reload_g6_runtime', lambda state: reloads.append(state.get('campaign')))
    with pytest.raises(RuntimeError,match='source drift|Source drift|Worker source drift'):
        mod.check_environment(config)
    assert mod.check_environment(config,allow_source_drift=True)==health
    mod.tick(config,state)
    assert state['phase']=='IDLE'
    assert reloads == [config['campaign_id']]
    assert state['active'] is None
    assert not any(method=='POST' for method,path in calls)


def test_source_drift_does_not_obstruct_existing_task_reconciliation(monkeypatch,tmp_path):
    config,state=setup(monkeypatch,tmp_path)
    state['next_index']=5
    state['active']={'slice_id':'04-direct-chat','team':'team06','task_id':'task06'}
    monkeypatch.setattr(mod,'check_environment',
        lambda config,allow_source_drift=False: {'worker_source_drift':{'drifted':True}})
    existing={'team':'team06','task_id':'task06','status':'RUNNING','active_action':'wait_response',
              'browser_control':{'stop_visible':True,'retry_visible':False}}
    monkeypatch.setattr(mod,'api',lambda method,path,payload=None,key=None: {'items':[existing]})
    reloads = []
    monkeypatch.setattr(mod, 'reload_g6_runtime', lambda state: reloads.append(True))
    mod.tick(config,state)
    assert reloads == [True]
    assert state['phase']=='ACTIVE'
    assert state['active']['task_id']=='task06'

def test_supervisor_exception_is_persisted_and_nonzero(monkeypatch,tmp_path):
    config,state=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,'CONFIG',tmp_path/'slices.json')
    monkeypatch.setattr(mod,'LOCK',tmp_path/'supervisor.lock')
    (tmp_path/'slices.json').write_text(__import__('json').dumps(config),encoding='utf-8')
    mod.save(state)
    def failed_tick(*args):
        raise RuntimeError('reconciliation failed')
    monkeypatch.setattr(mod,'tick',failed_tick)
    with pytest.raises(RuntimeError,match='reconciliation failed'):
        mod.supervise_once()
    after=mod.load(mod.STATE)
    assert after['phase']=='NEEDS_PI'
    assert 'reconciliation failed' in after['reason']

def test_runtime_reload_without_receipt_retries_same_slice_in_new_team(monkeypatch, tmp_path):
    config,state=setup(monkeypatch,tmp_path)
    state['next_index']=7
    state['active']={'slice_id':'06-async-wait','team':'original','task_id':'old-task'}
    reason='G6 self-repair runtime reload: operator priority; preserve prior Send ledger'
    stopped={'team':'original','task_id':'old-task','status':'STOPPED','active_action':'stopped',
             'stop_reason':reason,'browser_control':{'stop_visible':False,'retry_visible':False}}
    detail={**stopped,'controls':[{'action':'stop','status':'applied','reason':reason}]}
    calls=[]
    def api(method,path,payload=None,key=None):
        calls.append((method,path,key))
        if method=='GET':return detail
        return {'task_id':'new-task','command_id':'new-command'}
    monkeypatch.setattr(mod,'api',api)
    mod.process_active(config,state,[stopped])
    assert state['next_index']==7
    assert state['phase']=='IDLE'
    assert state['active'] is None
    assert state['slice_retries']['06-async-wait']==1
    mod.start_slice(config,state,[stopped],config['slices'][7])
    assert state['active']['task_id']=='new-task'
    assert state['active']['team'].endswith('-retry1')
    assert calls[-1][2]!=mod.key_for(config,7,'create')


def test_runtime_reload_without_receipt_is_bounded(monkeypatch,tmp_path):
    config,state=setup(monkeypatch,tmp_path)
    state['next_index']=7
    state['active']={'slice_id':'06-async-wait','team':'original','task_id':'old-task'}
    state['slice_retries']={'06-async-wait':2}
    reason='G6 self-repair runtime reload: operator priority; preserve prior Send ledger'
    stopped={'team':'original','task_id':'old-task','status':'STOPPED','active_action':'stopped',
             'stop_reason':reason,'browser_control':{'stop_visible':False,'retry_visible':False}}
    monkeypatch.setattr(mod,'api',lambda *args,**kwargs: {**stopped,'controls':[{'action':'stop','status':'applied','reason':reason}]})
    mod.process_active(config,state,[stopped])
    assert state['phase']=='NEEDS_PI'
    assert state['active']['task_id']=='old-task'
    assert state['slice_retries']['06-async-wait']==2

def test_offline_cdpa_api_reloads_before_pi_or_new_solo(monkeypatch,tmp_path):
    config,state=setup(monkeypatch,tmp_path)
    state['next_index']=7
    state['active']={'slice_id':'06-async-wait','team':'team08','task_id':'task08'}
    monkeypatch.setattr(mod,'check_environment',lambda *a,**k: (_ for _ in ()).throw(ConnectionError('G6 CDPA API offline')))
    reloads=[]
    monkeypatch.setattr(mod,'reload_g6_runtime',lambda state: reloads.append(state['active']['task_id']))
    monkeypatch.setattr(mod,'api',lambda *a,**k:pytest.fail('No API dispatch until reload'))
    mod.tick(config,state)
    assert reloads==['task08']
    assert state['phase']=='ACTIVE'
    assert state['active']['task_id']=='task08'
