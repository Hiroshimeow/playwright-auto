"""Close one provider-risk-blocked final review without bypassing MCP approval.

The original accepted Send remains fenced. The independent G6-local receipt
cannot change the NOT_ACCEPTED release verdict, only retire the stopped writer.
"""
import importlib.util
import json
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "workflows" / "self-repair" / "supervisor.py"
spec = importlib.util.spec_from_file_location("cdpa_supervisor_final_review", FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

STOP_REASON = (
    "G6 final release review: provider permission_review blocks one accepted "
    "Send; stop read-only review and audit local ledgers without MCP approval or replay"
)


def make_env(monkeypatch, tmp_path, *, valid_cleanup=True, valid_receipt=True, valid_stop=True):
    config=mod.load(mod.CONFIG)
    # Historical final-review contract ended at slice 12. Later bounded
    # reacceptance work must not retroactively change that old review's proof.
    config["slices"] = config["slices"][:14]
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path/"receipts").mkdir()
    state=mod.new_state(config)
    state.update(
        next_index=13,
        phase="NEEDS_PI",
        release="NOT_ACCEPTED",
        active={"slice_id":"12-final-release-review","team":"review-team","task_id":"task12"}
    )
    receipt={
        "slice_id":"12-final-release-review",
        "task_id":"task12" if valid_receipt else "another-task",
        "status":"BLOCKED",
        "release":"NOT_ACCEPTED",
        "release_verdict":"NOT_ACCEPTED",
        "full_regression":"PASS",
        "real_cdp":"BLOCKED",
        "tests":[{"command":"pytest tests/test_chatgpt_model_gate.py","result":"11 passed"}],
        "evidence":["Read-only independent review: first-hop terminal DONE not proven"],
    }
    (tmp_path/"receipts"/"12-final-release-review.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    detail={
        "task_id":"task12","team":"review-team","status":"STOPPED",
        "active_action":"cleared","stop_reason":STOP_REASON,
        "cleanup":{
            "state":"CLEARED" if valid_cleanup else "ACTIVE",
            "status_before":"STOPPED",
            "verified_empty_at":"2026-10-27T22:45:43Z" if valid_cleanup else None
        },
        "controls":[{"action":"stop","status":"applied" if valid_stop else "queued",
                     "reason":STOP_REASON}],
    }
    board={k:v for k,v in detail.items() if k!="controls"}
    monkeypatch.setattr(mod,"api",lambda method,path:detail)
    return config,state,board


def test_exact_stopped_review_accepted_only_as_blocked(monkeypatch,tmp_path):
    config,state,board=make_env(monkeypatch,tmp_path)
    mod.process_active(config,state,[board])
    assert state["phase"]=="IDLE"
    assert state["active"] is None
    assert state["next_index"]==len(config["slices"])
    assert state["release"]=="NOT_ACCEPTED"
    assert "task12" in state["archived_blocked"]
    assert state["completed"][-1]["receipt_status"]=="BLOCKED"


@pytest.mark.parametrize("clean,receipt,stop",[
    (False,True,True),(True,False,True),(True,True,False)
])
def test_review_never_retires_without_strict_proof(monkeypatch,tmp_path,clean,receipt,stop):
    config,state,board=make_env(
        monkeypatch,tmp_path,valid_cleanup=clean,valid_receipt=receipt,valid_stop=stop
    )
    mod.process_active(config,state,[board])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="task12"
    assert state["next_index"]==13


def test_scheduler_needs_pi_with_exact_stopped_review_routes_to_reducer_not_pi(
    monkeypatch, tmp_path
):
    config,state,board=make_env(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,"STATE",tmp_path/"state.json")
    monkeypatch.setattr(mod,"LOCK",tmp_path/"supervisor.lock")
    monkeypatch.setattr(mod,"PROGRESS",tmp_path/"progress.md")
    monkeypatch.setattr(mod,"CONFIG",tmp_path/"slices.json")
    (tmp_path/"slices.json").write_text(json.dumps(config), encoding="utf-8")
    detail={**board,"controls":[{"action":"stop","status":"applied","reason":STOP_REASON}]}
    def api(method,path):
        if path=="/health":
            return {
                "ok":True, "worker_online":True, "worker_ready":True,
                "worker_runtime_ready":True, "worker_command_loop_ready":True,
                "worker_source_drift":{"drifted":False}
            }
        if path=="/api/tasks":
            return {"items":[board]}
        if path=="/api/tasks/task12":
            return detail
        raise AssertionError(path)
    monkeypatch.setattr(mod,"api",api)
    monkeypatch.setattr(mod,"check_environment",lambda *a,**k:
                        {"worker_source_drift":{"drifted":False}})
    monkeypatch.setattr(mod,"pi_command",lambda *a,**k:
                        pytest.fail("Proven Stop/cleanup must not invoke Pi"))
    mod.save(state)
    mod.supervise_once()
    after=mod.load(mod.STATE)
    assert after["phase"]=="IDLE"
    assert after["next_index"]==len(config["slices"])
    assert after["active"] is None
    assert after["release"]=="NOT_ACCEPTED"
