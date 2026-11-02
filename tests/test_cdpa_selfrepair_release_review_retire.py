"""The final read-only release review must close a blocked, accepted turn
after exact official Stop/Clear, without replaying Send or demanding Pi again."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FILE=Path(__file__).resolve().parents[1]/"workflows"/"self-repair"/"supervisor.py"
spec=importlib.util.spec_from_file_location("cdpa_last_review_retire",FILE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

REASON=("G6 R11 final review retry2: one accepted Send and one MCP Allow, "
        "no recoverable terminal route after exact Resume; stop and clear; never replay Send")

def env(monkeypatch,tmp_path,*,cleared=True):
    cfg=mod.load(mod.CONFIG)
    monkeypatch.setattr(mod,"HERE",tmp_path)
    monkeypatch.setattr(mod,"STATE",tmp_path/"state.json")
    monkeypatch.setattr(mod,"PROGRESS",tmp_path/"progress.md")
    (tmp_path/"receipts").mkdir()
    (tmp_path/"receipts"/"14-release-recheck.json").write_text(json.dumps({
        "slice_id":"14-release-recheck","task_id":"review-task",
        "status":"BLOCKED","release_verdict":"NOT_ACCEPTED",
        "full_regression":"NOT_RETESTED","real_cdp":"BLOCKED",
        "tests":[{"command":"pytest -q tests/test_cdpa_selfrepair_opus_review.py",
                  "result":"2 passed"}],
        "evidence":["One accepted Send, High, one Allow, absent terminal reply"]
    }),encoding="utf8")
    state=mod.new_state(cfg)
    state.update(next_index=len(cfg["slices"])-1,phase="NEEDS_PI",
                 release="NOT_ACCEPTED",
                 active={"slice_id":"14-release-recheck","team":"review-team","task_id":"review-task"})
    detail={"task_id":"review-task","team":"review-team",
            "status":"STOPPED","active_action":"cleared","stop_reason":REASON,
            "cleanup":{"state":"CLEARED" if cleared else "ACTIVE",
                       "status_before":"STOPPED",
                       "verified_empty_at":"2026-10-28T02:00:00Z" if cleared else None},
            "controls":[{"action":"stop","status":"applied","reason":REASON}]}
    board={k:v for k,v in detail.items() if k!="controls"}
    monkeypatch.setattr(mod,"api",lambda *a,**k:detail)
    return cfg,state,board

def stopped_slice15(monkeypatch, tmp_path, *, cleared=True, accepted_receipt=True):
    cfg = mod.load(mod.CONFIG)
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "15-real-terminal-reacceptance.json").write_text(
        json.dumps({
            "slice_id": "15-real-terminal-reacceptance", "task_id": "fresh-accepted-task",
            "status": "BLOCKED" if accepted_receipt else "PASS",
            "release_verdict": "NOT_ACCEPTED", "real_cdp": "NOT_ACCEPTED",
            "tests": [{"command": "pytest -q tests/test_cdpa_overlay_observation.py",
                       "result": "32 passed"}],
            "evidence": {"request_id": "fresh-accepted-task-hop1",
                         "attempts": 1, "terminal_route": "UNPROVEN"},
        }), encoding="utf8")
    state = mod.new_state(cfg)
    idx = next(i for i, s in enumerate(cfg["slices"])
               if s["id"] == "15-real-terminal-reacceptance")
    state.update(next_index=idx, phase="NEEDS_PI", release="NOT_ACCEPTED",
                 slice_retries={"15-real-terminal-reacceptance": 2},
                 active={"slice_id": "15-real-terminal-reacceptance",
                         "team": "fresh-owned-team", "task_id": "fresh-accepted-task"})
    why = "G6 self-repair runtime reload: operator priority; preserve prior Send ledger"
    detail = {"task_id": "fresh-accepted-task", "team": "fresh-owned-team",
              "status": "STOPPED", "active_action": "cleared" if cleared else "stopped",
              "stop_reason": why,
              "cleanup": {"state": "CLEARED" if cleared else "ACTIVE",
                          "status_before": "STOPPED" if cleared else None,
                          "verified_empty_at": "2026-10-28T04:12:00Z" if cleared else None},
              "browser_control": {"stop_visible": False, "retry_visible": False},
              "controls": [{"action": "stop", "status": "applied", "reason": why}]}
    board = {k: v for k, v in detail.items() if k != "controls"}
    monkeypatch.setattr(mod, "api", lambda *args, **kwargs: detail)
    return cfg, state, board


def test_stopped_slice15_with_blocked_receipt_and_exact_clear_advances_to_review_only(monkeypatch,tmp_path):
    cfg,state,board=stopped_slice15(monkeypatch,tmp_path)
    mod.process_active(cfg,state,[board])
    assert state["active"] is None
    assert cfg["slices"][state["next_index"]]["id"] == "15b-fresh-terminal-proof"
    assert state["release"] == "NOT_ACCEPTED"
    assert state["phase"] == "IDLE"
    assert state["completed"][-1]["task_terminal_status"] == "STOPPED"


@pytest.mark.parametrize("cleared,receipt_ok", [(False,True),(True,False)])
def test_stopped_slice15_does_not_advance_without_exact_stop_clear_and_blocked_receipt(
        monkeypatch,tmp_path,cleared,receipt_ok):
    cfg,state,board=stopped_slice15(monkeypatch,tmp_path,cleared=cleared,accepted_receipt=receipt_ok)
    mod.process_active(cfg,state,[board])
    assert state["active"]["task_id"] == "fresh-accepted-task"
    assert state["next_index"] == next(i for i,s in enumerate(cfg["slices"])
                                      if s["id"] == "15-real-terminal-reacceptance")
    assert state["release"] == "NOT_ACCEPTED"


@pytest.mark.parametrize("invalid", ["wrong_receipt_task", "missing_applied_stop", "no_pytest_evidence", "invalid_receipt_status", "wrong_release_verdict"])
def test_stopped_slice15_requires_exact_task_stop_and_test_evidence(monkeypatch,tmp_path,invalid):
    cfg,state,board=stopped_slice15(monkeypatch,tmp_path)
    receipt_file=tmp_path/"receipts"/"15-real-terminal-reacceptance.json"
    if invalid in ("wrong_receipt_task", "no_pytest_evidence", "invalid_receipt_status", "wrong_release_verdict"):
        receipt=json.loads(receipt_file.read_text(encoding="utf8"))
        if invalid=="wrong_receipt_task":
            receipt["task_id"]="unrelated-task"
        elif invalid=="no_pytest_evidence":
            receipt["tests"]=[]
        elif invalid=="invalid_receipt_status":
            receipt["status"]="INVALID"
        else:
            receipt["release_verdict"]="PASS"
        receipt_file.write_text(json.dumps(receipt),encoding="utf8")
    detail={**board, "controls": ([] if invalid=="missing_applied_stop" else [
        {"action":"stop", "status":"applied","reason":board["stop_reason"]}])}
    monkeypatch.setattr(mod,"api",lambda *args,**kwargs:detail)
    mod.process_active(cfg,state,[board])
    assert state["active"]["task_id"] == "fresh-accepted-task"
    assert state["release"] == "NOT_ACCEPTED"


def final_done_review(monkeypatch, tmp_path, *, terminal_cleanup=True, matching_receipt=True):
    cfg = mod.load(mod.CONFIG)
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path / "receipts").mkdir()
    active = {"slice_id": "16-final-release-evidence",
              "task_id": "review16-accepted", "team": "review16-team"}
    receipt = {
        "slice_id": active["slice_id"],
        "task_id": active["task_id"] if matching_receipt else "unrelated-task",
        "status": "BLOCKED", "release_verdict": "NOT_ACCEPTED",
        "real_cdp": "BLOCKED", "full_regression": "PENDING_CURRENT_RUN",
        "tests": [{"command": "uv run pytest -q", "exit_code": None,
                   "result": "full suite still running"}],
        "evidence": {"prior_15b": "No terminal DONE proof",
                     "tabs_before": "recorded before review16 admission"},
    }
    (tmp_path / "receipts" / "16-final-release-evidence.json").write_text(
        json.dumps(receipt), encoding="utf8")
    st = mod.new_state(cfg)
    st.update(phase="RECOVERY_EXHAUSTED", next_index=len(cfg["slices"])-1,
              release="NOT_ACCEPTED", pi_attempts=2, active=active)
    detail = {"task_id": active["task_id"], "team": active["team"],
              "status": "DONE", "active_action": "cleared",
              "cleanup": {"state": "CLEARED" if terminal_cleanup else "ACTIVE",
                          "status_before": "DONE",
                          "verified_empty_at": "2026-10-28T04:54:17Z" if terminal_cleanup else None}}
    monkeypatch.setattr(mod, "api", lambda method, path, *a, **kw: detail)
    return cfg, st, {k: v for k, v in detail.items() if k != "cleanup"}


def test_final_done_blocked_review_reconciles_without_false_release_pass(monkeypatch,tmp_path):
    cfg, st, row = final_done_review(monkeypatch,tmp_path)
    mod.process_active(cfg, st, [row])
    assert st["active"] is None
    assert st["next_index"] == len(cfg["slices"])
    assert st["phase"] == "RELEASE_REVIEW"
    assert st["release"] == "NOT_ACCEPTED"
    assert st["pi_attempts"] == 0


@pytest.mark.parametrize("cleanup,matching", [(False,True),(True,False)])
def test_final_done_blocked_review_needs_exact_receipt_and_cleanup(monkeypatch,tmp_path,cleanup,matching):
    cfg, st, row = final_done_review(monkeypatch,tmp_path,
                                      terminal_cleanup=cleanup,matching_receipt=matching)
    try:
        mod.process_active(cfg, st, [row])
    except RuntimeError:
        pass
    assert st["active"]["task_id"] == "review16-accepted"
    assert st["next_index"] == len(cfg["slices"])-1
    assert st["release"] == "NOT_ACCEPTED"


def test_final_blocked_review_retired_without_replaying_send(monkeypatch,tmp_path):
    cfg,state,board=env(monkeypatch,tmp_path)
    mod.process_active(cfg,state,[board])
    assert state["active"] is None
    assert state["next_index"]==len(cfg["slices"])
    assert state["phase"]=="RELEASE_REVIEW"
    assert state["release"]=="NOT_ACCEPTED"
    assert "review-task" in state["archived_blocked"]

def test_final_blocked_review_cannot_retire_without_clear(monkeypatch,tmp_path):
    cfg,state,board=env(monkeypatch,tmp_path,cleared=False)
    mod.process_active(cfg,state,[board])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="review-task"
    assert state["next_index"]==len(cfg["slices"])-1
