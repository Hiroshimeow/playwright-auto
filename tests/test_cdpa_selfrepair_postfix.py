"""Bounded 08 -> postfix CDP handoff: only after immutable Stop evidence.

A failed 08 browser attempt may not be silently called a release PASS.
After the separately reproduced Resume persistence root fix, use a NEW task
identity to prove the real browser rather than replay the accepted old Send.
"""
import importlib.util
import json
from pathlib import Path

FILE = Path(__file__).resolve().parents[1] / "workflows" / "self-repair" / "supervisor.py"
spec = importlib.util.spec_from_file_location("g6_cdpa_selfrepair_postfix", FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def setup(monkeypatch, tmp_path):
    config = mod.load(mod.CONFIG)
    config["slices"].append({
        "id": "09-postfix-real-cdp",
        "title": "Postfix live CDP verification",
        "task": "Use a fresh ChatGPT page only",
        "tests": ["tests/test_cdpa_resume_control_store.py"],
    })
    state = mod.new_state(config)
    state.update(phase="NEEDS_PI", next_index=9, release="NOT_ACCEPTED",
                 slice_retries={"08-real-browser": 2},
                 active={"slice_id": "08-real-browser", "team": "t-retry2",
                         "task_id": "old-retry2"})
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path/"receipts").mkdir()
    (tmp_path/"receipts"/"08-real-browser.json").write_text(json.dumps({
        "slice_id": "08-real-browser", "task_id": "old-retry2",
        "status": "BLOCKED", "release": "NOT_ACCEPTED",
        "tests": [{"command": "pytest -q tests/test_cdpa_resume_control_store.py",
                   "result": "1 passed, exit 0"}],
        "evidence": ["One accepted Send; later official Stop; browser release unverified"]
    }),encoding="utf-8")
    why = "G6 self-repair runtime reload: operator priority; preserve prior Send ledger"
    stopped = {
        "task_id": "old-retry2", "team": "t-retry2", "status": "STOPPED",
        "active_action": "cleared", "stop_reason": why,
        "cleanup": {"state": "CLEARED", "status_before": "STOPPED",
                    "verified_empty_at": "2026-10-27T15:18:14Z"}
    }
    detail = {**stopped, "controls": [
        {"action": "stop", "status": "applied", "reason": why}
    ]}
    return config, state, stopped, detail


def test_bounded_08_after_rootfix_advances_to_fresh_postfix_slice(monkeypatch,tmp_path):
    config,state,stopped,detail = setup(monkeypatch,tmp_path)
    monkeypatch.setattr(mod,"api",lambda method,url:detail)
    mod.process_active(config,state,[stopped])
    assert state["phase"]=="IDLE"
    assert state["active"] is None
    assert state["next_index"]==10
    assert state["release"]=="NOT_ACCEPTED"
    assert "old-retry2" in state["archived_blocked"]
    assert any(x.get("task_id")=="old-retry2" and x.get("status")=="STOPPED"
               for x in state["blocked_history"])


def test_bounded_08_without_cleared_cleanup_remains_fenced(monkeypatch,tmp_path):
    config,state,stopped,detail=setup(monkeypatch,tmp_path)
    detail["cleanup"]["state"]="ACTIVE"
    stopped["cleanup"]["state"]="ACTIVE"
    monkeypatch.setattr(mod,"api",lambda method,url:detail)
    mod.process_active(config,state,[stopped])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="old-retry2"
    assert state["next_index"]==9


def test_bounded_08_without_exact_receipt_remains_fenced(monkeypatch,tmp_path):
    config,state,stopped,detail=setup(monkeypatch,tmp_path)
    (tmp_path/"receipts"/"08-real-browser.json").write_text(json.dumps({
        "slice_id":"08-real-browser","task_id":"different","status":"BLOCKED",
        "release":"NOT_ACCEPTED"
    }),encoding="utf-8")
    monkeypatch.setattr(mod,"api",lambda method,url:detail)
    mod.process_active(config,state,[stopped])
    assert state["phase"]=="NEEDS_PI"
    assert state["active"]["task_id"]=="old-retry2"
    assert state["next_index"]==9


def setup_postfix_stop(monkeypatch, tmp_path):
    config = mod.load(mod.CONFIG)
    index = next(i for i, item in enumerate(config["slices"])
                 if item["id"] == "09-postfix-real-cdp")
    state = mod.new_state(config)
    state.update(
        phase="NEEDS_PI",
        next_index=index,
        release="NOT_ACCEPTED",
        active={
            "slice_id": "09-postfix-real-cdp",
            "team": "postfix-team",
            "task_id": "postfix-task",
        },
    )
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "09-postfix-real-cdp.json").write_text(
        json.dumps({
            "slice_id": "09-postfix-real-cdp",
            "task_id": "postfix-task",
            "status": "BLOCKED",
            "release": "NOT_ACCEPTED",
            "changed_files": ["workflows/self-repair/receipts/09-postfix-real-cdp.json"],
            "tests": [{
                "command": "pytest -q tests/test_cdpa_resume_control_store.py",
                "result": "11 passed, exit 0",
            }],
            "evidence": ["Both accepted hops preserved; release evidence remains incomplete."],
            "next_action": "Advance only to read-only release review.",
        }),
        encoding="utf-8",
    )
    reason = (
        "G6 postfix real-CDP acceptance closed: preserve accepted hops; "
        "advance read-only release review"
    )
    stopped = {
        "task_id": "postfix-task",
        "team": "postfix-team",
        "status": "STOPPED",
        "active_action": "cleared",
        "stop_reason": reason,
        "cleanup": {
            "state": "CLEARED",
            "status_before": "STOPPED",
            "verified_empty_at": "2026-10-28T00:00:00Z",
        },
    }
    detail = {
        **stopped,
        "controls": [{"action": "stop", "status": "applied", "reason": reason}],
    }
    return config, state, stopped, detail, index


def test_postfix_stop_with_exact_cleanup_advances_to_release_review(monkeypatch, tmp_path):
    config, state, stopped, detail, index = setup_postfix_stop(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "api", lambda method, url: detail)
    mod.process_active(config, state, [stopped])
    assert state["phase"] == "IDLE", state.get("reason")
    assert state["active"] is None
    assert state["next_index"] == index + 1
    assert config["slices"][state["next_index"]]["id"] == "10-release-review"
    assert state["release"] == "NOT_ACCEPTED"
    assert "postfix-task" in state["archived_blocked"]
    assert state["completed"][-1]["slice_id"] == "09-postfix-real-cdp"
    assert state["completed"][-1]["release"] == "NOT_ACCEPTED"


def test_postfix_stop_without_cleared_cleanup_stays_fenced(monkeypatch, tmp_path):
    config, state, stopped, detail, index = setup_postfix_stop(monkeypatch, tmp_path)
    stopped["cleanup"]["state"] = "ACTIVE"
    detail["cleanup"]["state"] = "ACTIVE"
    monkeypatch.setattr(mod, "api", lambda method, url: detail)
    mod.process_active(config, state, [stopped])
    assert state["phase"] == "NEEDS_PI"
    assert state["active"]["task_id"] == "postfix-task"
    assert state["next_index"] == index



def setup_release_review_stop(monkeypatch, tmp_path):
    config = mod.load(mod.CONFIG)
    index = next(i for i, item in enumerate(config["slices"])
                 if item["id"] == "10-release-review")
    state = mod.new_state(config)
    state.update(
        phase="NEEDS_PI",
        next_index=index,
        release="NOT_ACCEPTED",
        active={
            "slice_id": "10-release-review",
            "team": "review-team",
            "task_id": "review-task",
        },
    )
    monkeypatch.setattr(mod, "HERE", tmp_path)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "10-release-review.json").write_text(
        json.dumps({
            "slice_id": "10-release-review",
            "task_id": "review-task",
            "status": "BLOCKED",
            "release": "NOT_ACCEPTED",
            "tests": [{"command": "pytest -q tests/test_cdpa_selfrepair_postfix.py",
                       "result": "5 passed"}],
            "evidence": ["Final live-CDP DONE and post-rootfix regression still required."],
        }),
        encoding="utf-8",
    )
    reason = (
        "G6 release review protocol closed: preserve accepted review prompt; "
        "coordinator continues read-only evidence review"
    )
    stopped = {
        "task_id": "review-task",
        "team": "review-team",
        "status": "STOPPED",
        "active_action": "cleared",
        "stop_reason": reason,
        "cleanup": {
            "state": "CLEARED",
            "status_before": "STOPPED",
            "verified_empty_at": "2026-10-28T00:00:00Z",
        },
    }
    detail = {
        **stopped,
        "controls": [{"action": "stop", "status": "applied", "reason": reason}],
    }
    return config, state, stopped, detail, index


def test_release_review_stop_advances_only_to_final_live_cdp(monkeypatch, tmp_path):
    config, state, stopped, detail, index = setup_release_review_stop(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "api", lambda method, url: detail)
    mod.process_active(config, state, [stopped])
    assert state["phase"] == "IDLE", state.get("reason")
    assert state["active"] is None
    assert state["next_index"] == index + 1
    assert config["slices"][state["next_index"]]["id"] == "11-final-real-cdp"
    assert state["release"] == "NOT_ACCEPTED"


def setup_final_done(monkeypatch, tmp_path, *, compare):
    config = mod.load(mod.CONFIG)
    index = next(i for i, item in enumerate(config["slices"])
                 if item["id"] == "11-final-real-cdp")
    state = mod.new_state(config)
    state.update(
        phase="ACTIVE",
        next_index=index,
        release="NOT_ACCEPTED",
        active={
            "slice_id": "11-final-real-cdp",
            "team": "final-live-team",
            "task_id": "final-live-task",
        },
    )
    monkeypatch.setattr(mod, "HERE", tmp_path)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "11-final-real-cdp.json").write_text(
        json.dumps({
            "slice_id": "11-final-real-cdp",
            "task_id": "final-live-task",
            "status": "PASS",
            "tests": [{"command": "pytest -q tests/test_chatgpt_model_gate.py",
                       "result": "4 passed"}],
            "evidence": ["One direct terminal DONE with exactly one accepted Send."],
        }),
        encoding="utf-8",
    )
    if compare is not None:
        path = tmp_path / ".runtime" / "self-repair"
        path.mkdir(parents=True)
        (path / "11-final-tabs-compare.json").write_text(
            json.dumps(compare), encoding="utf-8"
        )
    done = {
        "task_id": "final-live-task",
        "team": "final-live-team",
        "status": "DONE",
        "active_action": "done",
    }
    return config, state, done, index


def test_final_live_done_waits_for_coordinator_tab_compare(monkeypatch, tmp_path):
    config, state, done, index = setup_final_done(monkeypatch, tmp_path, compare=None)
    mod.process_active(config, state, [done])
    assert state["phase"] == "ACTIVE"
    assert state["active"]["task_id"] == "final-live-task"
    assert state["next_index"] == index
    assert "awaits exact coordinator tab comparison" in state["reason"]


def test_final_live_done_with_exact_compare_advances_to_final_review(monkeypatch, tmp_path):
    compare = {
        "slice_id": "11-final-real-cdp",
        "task_id": "final-live-task",
        "status": "PASS",
        "unrelated_unchanged": True,
        "owned_page_id": "owned-page",
        "send_click_count": 1,
        "model_gate_before_fill": True,
        "model_gate_before_send": True,
        "retry_or_repair_count": 0,
        "terminal_route": "DONE",
    }
    config, state, done, index = setup_final_done(
        monkeypatch, tmp_path, compare=compare
    )
    mod.process_active(config, state, [done])
    assert state["phase"] == "IDLE"
    assert state["active"] is None
    assert state["next_index"] == index + 1
    assert config["slices"][state["next_index"]]["id"] == "12-final-release-review"
