"""Opus-identified supervisor root causes: do not reload forever or repair ownership by restart."""
import importlib.util
from pathlib import Path

FILE=Path(__file__).resolve().parents[1]/'workflows'/'self-repair'/'supervisor.py'
spec=importlib.util.spec_from_file_location('cdpa_opus_review_supervisor',FILE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_stalled_unchanged_task_reloads_once_then_reconciles(monkeypatch):
    state=mod.new_state(mod.load(mod.CONFIG))
    state['phase']='ACTIVE'
    state['active']={'team':'team-x','task_id':'task-x','slice_id':'13-allow-reacceptance',
                     'status':'RUNNING','action':'wait_response',
                     'signature':'RUNNING:wait_response:0','progress_at':'2020-01-01T00:00:00+00:00'}
    config=mod.load(mod.CONFIG)
    config['stall_seconds']=1
    monkeypatch.setattr(mod,'check_environment',lambda *a,**k:{'worker_source_drift':{'drifted':False}})
    monkeypatch.setattr(mod,'api',lambda method,path,*args,**kwargs:
                        {'items':[{'team':'team-x','task_id':'task-x',
                                   'status':'RUNNING','timeline_total':0}]} if path=='/api/tasks' else {})
    monkeypatch.setattr(mod,'process_active',lambda *a:None)
    monkeypatch.setattr(mod,'maybe_stop_stale_code_slice',lambda *a:None)
    monkeypatch.setattr(mod,'save',lambda *a:None)
    reloads=[]
    monkeypatch.setattr(mod,'reload_g6_runtime',lambda state:reloads.append('reload'))
    mod.tick(config,state)
    assert reloads==['reload']
    assert state['active']['stall_reload_sig']=='RUNNING:wait_response:0'
    assert state['active']['progress_at'] != '2020-01-01T00:00:00+00:00'
    state['active']['progress_at']='2020-01-01T00:00:00+00:00'
    mod.tick(config,state)
    assert reloads==['reload']
    assert state['phase']=='NEEDS_PI'

def test_archived_writer_violation_does_not_trigger_runtime_reload(monkeypatch):
    config=mod.load(mod.CONFIG)
    state=mod.new_state(config)
    state['phase']='IDLE'
    state['archived_blocked']=['archived-accepted-task']
    monkeypatch.setattr(mod,'check_environment',lambda *a,**k:{'worker_source_drift':{'drifted':False}})
    monkeypatch.setattr(mod,'api',lambda method,path,*args,**kwargs:
                        {'items':[{'task_id':'archived-accepted-task','status':'RUNNING'}]})
    monkeypatch.setattr(mod,'save',lambda *a:None)
    reloads=[]
    monkeypatch.setattr(mod,'reload_g6_runtime',lambda state:reloads.append('reload'))
    mod.tick(config,state)
    assert reloads==[]
    assert state['phase']=='NEEDS_PI'
    assert 'Archived blocked task resumed concurrently' in state['reason']
