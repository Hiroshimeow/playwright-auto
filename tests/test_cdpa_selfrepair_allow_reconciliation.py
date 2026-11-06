"""Allow acceptance: retire only exact blocked accepted Send after Stop/CLEARED,
and let the existing supervisor dispatch its read-only successor without Pi timeout.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FILE=Path(__file__).resolve().parents[1]/"workflows"/"self-repair"/"supervisor.py"
spec=importlib.util.spec_from_file_location("g6_allow_reconciliation",FILE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
STOP_REASON=("G6 Gate C 13 reacceptance: terminal route absent after one accepted Send; "
             "Stop and clear owned writer after DOM/source-drift root fix; never replay Send")


def setup(monkeypatch,tmp_path,*,bad_receipt=False,missing_cleanup=False):
    config=mod.load(mod.CONFIG)
    monkeypatch.setattr(mod,"HERE",tmp_path)
    monkeypatch.setattr(mod,"STATE",tmp_path/"state.json")
    monkeypatch.setattr(mod,"LOCK",tmp_path/"supervisor.lock")
    monkeypatch.setattr(mod,"PROGRESS",tmp_path/"progress.md")
    monkeypatch.setattr(mod,"CONFIG",tmp_path/"slices.json")
    (tmp_path/"slices.json").write_text(json.dumps(config),encoding="utf-8")
    folder=tmp_path/"receipts"
    folder.mkdir()
    (folder/"13-allow-reacceptance.json").write_text(json.dumps({
        "slice_id":"13-allow-reacceptance",
        "task_id":"other" if bad_receipt else "task13",
        "status":"BLOCKED","release_verdict":"NOT_ACCEPTED",
        "tests":[{"command":"pytest -q tests/test_v11_allow_ledger.py",
                  "result":"35 passed in 22.28s"}],
        "evidence":["High verified three times; one accepted Send, one Allow; no terminal DONE"],
    }),encoding="utf-8")
    state=mod.new_state(config)
    state.update(phase="NEEDS_PI",next_index=14,release="NOT_ACCEPTED",
                 active={"slice_id":"13-allow-reacceptance","team":"team13","task_id":"task13"})
    detail={"task_id":"task13","team":"team13",
            "status":"STOPPED","active_action":"cleared","stop_reason":STOP_REASON,
            "cleanup":{"state":"ACTIVE" if missing_cleanup else "CLEARED",
                       "status_before":"STOPPED",
                       "verified_empty_at":None if missing_cleanup else "2026-10-28T00:20:00Z"},
            "controls":[{"action":"stop","status":"applied","reason":STOP_REASON}]}
    board={k:v for k,v in detail.items() if k!="controls"}
    return config,state,board,detail


def test_reconciles_blocked_allow_task_to_readonly_successor(monkeypatch,tmp_path):
    config,state,board,detail=setup(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,"api",lambda method,path:detail)
    mod.process_active(config,state,[board])
    assert state["phase"]=="IDLE"
    assert state["active"] is None
    assert state["next_index"]==15
    assert state["release"]=="NOT_ACCEPTED"
    assert "task13" in state["archived_blocked"]
    assert state["completed"][-1]["task_terminal_status"]=="STOPPED"


@pytest.mark.parametrize("bad_receipt,missing_cleanup",[(True,False),(False,True)])
def test_must_not_advance_ambiguous_allow_writer(monkeypatch,tmp_path,bad_receipt,missing_cleanup):
    config,state,board,detail=setup(
        monkeypatch,tmp_path,bad_receipt=bad_receipt,missing_cleanup=missing_cleanup
    )
    monkeypatch.setattr(mod,"api",lambda method,path:detail)
    mod.process_active(config,state,[board])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="task13"
    assert state["next_index"]==14


def test_supervisor_needs_pi_uses_verified_stop_reducer_without_pi(monkeypatch,tmp_path):
    config,state,board,detail=setup(monkeypatch,tmp_path)
    mod.save(state)
    def api(method,path,*args,**kwargs):
        if path=="/api/tasks": return {"items":[board]}
        if path=="/api/tasks/task13": return detail
        if path=="/health": return {
            "ok":True,"worker_online":True,"worker_ready":True,
            "worker_runtime_ready":True,"worker_command_loop_ready":True,
            "worker_source_drift":{"drifted":False}
        }
        raise AssertionError(path)
    monkeypatch.setattr(mod,"api",api)
    monkeypatch.setattr(mod,"check_environment",lambda *a,**k:
                        {"worker_source_drift":{"drifted":False}})
    monkeypatch.setattr(mod,"pi_command",lambda *a,**k:
                        pytest.fail("exact stopped task/receipt must not block on Pi"))
    mod.supervise_once()
    resulting=mod.load(mod.STATE)
    assert resulting["phase"]=="IDLE"
    assert resulting["next_index"]==15
    assert resulting["active"] is None
