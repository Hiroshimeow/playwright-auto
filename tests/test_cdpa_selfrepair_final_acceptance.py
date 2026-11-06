"""Gate C final first-hop acceptance: BLOCKED must not replay the accepted Send.

After an exact applied Stop and CLEARED owned tab, advance to an independent
read-only release review, preserving NOT_ACCEPTED and the original request.
"""
import importlib.util
import json
from pathlib import Path
import pytest

FILE = Path(__file__).resolve().parents[1] / "workflows" / "self-repair" / "supervisor.py"
spec = importlib.util.spec_from_file_location("cdpa_supervisor_final", FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

STOP_REASON = (
    "G6 final acceptance 11 failed closed: BLOCKED permission_review after one "
    "accepted Send; direct DONE cannot be proven; preserve accepted ledger and "
    "forbid Retry/Resume"
)


def make_setup(monkeypatch, tmp_path, *, missing_cleanup=False, wrong_receipt=False):
    config = mod.load(mod.CONFIG)
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path / "receipts").mkdir()
    state = mod.new_state(config)
    state.update(
        next_index=12, phase="NEEDS_PI", release="NOT_ACCEPTED",
        active={"slice_id": "11-final-real-cdp", "team": "final-team", "task_id": "task11"},
    )
    (tmp_path / "receipts" / "11-final-real-cdp.json").write_text(json.dumps({
        "slice_id": "11-final-real-cdp",
        "task_id": "wrong-task" if wrong_receipt else "task11",
        "status": "BLOCKED", "release": "NOT_ACCEPTED",
        "tests": [{"command": "pytest -q tests/test_chatgpt_model_gate.py",
                   "result": "10 passed, exit 0"}],
        "evidence": ["High verified before one accepted Send; no terminal DONE"],
    }), encoding="utf-8")
    cleanup = {
        "state": "ACTIVE" if missing_cleanup else "CLEARED",
        "status_before": "STOPPED",
        "verified_empty_at": None if missing_cleanup else "2026-10-27T22:34:57Z",
    }
    record = {
        "task_id": "task11", "team": "final-team", "status": "STOPPED",
        "active_action": "cleared", "stop_reason": STOP_REASON,
        "cleanup": cleanup,
    }
    detail = {**record, "controls": [{
        "action": "stop", "status": "applied", "reason": STOP_REASON
    }]}
    monkeypatch.setattr(mod, "api", lambda method, path: detail)
    return config, state, record


def test_stopped_final_one_send_can_advance_to_review_with_not_accepted(monkeypatch,tmp_path):
    config,state,record=make_setup(monkeypatch,tmp_path)
    mod.process_active(config,state,[record])
    assert state["phase"]=="IDLE"
    assert state["active"] is None
    assert state["next_index"]==13
    assert state["release"]=="NOT_ACCEPTED"
    assert "task11" in state["archived_blocked"]
    assert any(x["slice_id"]=="11-final-real-cdp" and x["task_id"]=="task11"
               for x in state["blocked_history"])


@pytest.mark.parametrize("missing_cleanup,wrong_receipt",[(True,False),(False,True)])
def test_final_task_still_fenced_without_all_evidence(monkeypatch,tmp_path,missing_cleanup,wrong_receipt):
    config,state,record=make_setup(
        monkeypatch,tmp_path,missing_cleanup=missing_cleanup,wrong_receipt=wrong_receipt
    )
    mod.process_active(config,state,[record])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="task11"
    assert state["next_index"]==12
