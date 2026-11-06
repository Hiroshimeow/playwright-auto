"""Self-repair must actually dispatch the next *existing* Gate C work item
after a bounded NOT_ACCEPTED review, without resubmitting historical Sends."""
import importlib.util
from pathlib import Path

FILE=Path(__file__).resolve().parents[1]/"workflows"/"self-repair"/"supervisor.py"
spec=importlib.util.spec_from_file_location("g6_cdpa_reaccept_continuation",FILE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_review_with_followup_starts_one_fresh_solo(monkeypatch,tmp_path):
    config=mod.load(mod.CONFIG)
    start=len(config["slices"])
    config["slices"].append({
        "id":"new-acceptance-fixture","title":"Existing Gate C reacceptance",
        "task":"Use a NEW owned browser page only",
        "tests":["tests/test_v11_allow_ledger.py"],
    })
    monkeypatch.setattr(mod,"STATE",tmp_path/"state.json")
    monkeypatch.setattr(mod,"PROGRESS",tmp_path/"progress.md")
    state=mod.new_state(config)
    state.update(phase="RELEASE_REVIEW",next_index=start,release="NOT_ACCEPTED")
    old={"team":"g6-selfrepair-14-12-final-release-review",
         "task_id":"old-accepted-id",
         "status":"STOPPED","active_action":"cleared"}
    posted=[]
    def api(method,path,data=None,key=None):
        if method=="GET" and path=="/api/tasks":
            return {"items":[old]}
        if method=="POST" and path=="/api/tasks":
            posted.append((data,key))
            return {"task_id":"fresh-acceptance-id","command_id":"cmd-fresh"}
        raise AssertionError((method,path))
    monkeypatch.setattr(mod,"api",api)
    monkeypatch.setattr(mod,"check_environment",lambda *a,**k:
                        {"worker_source_drift":{"drifted":False}})
    mod.tick(config,state)
    assert state["phase"]=="ACTIVE"
    assert state["active"]["task_id"]=="fresh-acceptance-id"
    assert state["active"]["slice_id"]=="new-acceptance-fixture"
    assert state["release"]=="NOT_ACCEPTED"
    assert len(posted)==1
    assert posted[0][0]["requested_team"].endswith("new-acceptance-fixture")
    assert posted[0][1]==mod.key_for(config,start,"create")
    assert "old-accepted-id" not in repr(posted[0][0])


def test_exhausted_plan_never_claims_pass_from_blocked_review(monkeypatch,tmp_path):
    config=mod.load(mod.CONFIG)
    monkeypatch.setattr(mod,"STATE",tmp_path/"state.json")
    monkeypatch.setattr(mod,"PROGRESS",tmp_path/"progress.md")
    monkeypatch.setattr(mod,"HERE",tmp_path)
    (tmp_path/"receipts").mkdir()
    final=config["slices"][-1]["id"]
    (tmp_path/"receipts"/(final+".json")).write_text(
        '{"slice_id":"'+final+'","status":"BLOCKED","release_verdict":"NOT_ACCEPTED",'
        '"full_regression":"BLOCKED","real_cdp":"BLOCKED"}',encoding="utf-8")
    state=mod.new_state(config)
    state.update(phase="RELEASE_REVIEW",next_index=len(config["slices"]),
                 release="NOT_ACCEPTED")
    monkeypatch.setattr(mod,"api",lambda method,path,*a,**k:{"items":[]})
    monkeypatch.setattr(mod,"check_environment",lambda *a,**k:{"worker_source_drift":{"drifted":False}})
    mod.tick(config,state)
    assert state["phase"]=="RELEASE_REVIEW"
    assert state["release"]=="NOT_ACCEPTED"
    assert state["active"] is None
