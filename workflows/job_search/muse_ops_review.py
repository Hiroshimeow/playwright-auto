#!/usr/bin/env python3
"""Read-only operational scorecard for recurring Muse workflow reviews on G6.

Business conversion figures remain authoritative on G8 and are supplied by Muse.
"""
from __future__ import annotations
import argparse
import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import muse_pi_coordinator as pi_coordinator

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / ".runtime" / "job-search" / "reviews"
PI = ROOT / ".runtime" / "job-search" / "pi-jobs"

def status():
    with urllib.request.urlopen("http://127.0.0.1:9225/api/tasks", timeout=6) as r:
        items=json.load(r).get("items",[])
    if not isinstance(items,list):
        raise RuntimeError("CDPA task inventory unavailable")
    counts=dict(Counter(str(x.get("status","UNKNOWN")) for x in items))
    blockers=dict(Counter(str(x.get("block_code")) for x in items
                          if x.get("status") in ("BLOCKED","PAUSED") and x.get("block_code")))
    run_files=list(PI.glob("pi-*.json")) if PI.is_dir() else []
    pi_runs=[]
    for f in run_files[-35:]:
        try:
            rec=json.loads(f.read_text(encoding="utf-8"))
            live=pi_coordinator.read_job(rec["id"])
            stdout=f.with_suffix(".out.log")
            stderr=f.with_suffix(".err.log")
            pi_runs.append({"id":rec.get("id"),"kind":rec.get("kind"),"model":rec.get("model"),
                            "state":live.get("state"),
                            "stdout_bytes":stdout.stat().st_size if stdout.exists() else 0,
                            "stderr_bytes":stderr.stat().st_size if stderr.exists() else 0})
        except (ValueError,OSError,KeyError):
            continue
    return {"collected_at":datetime.now(timezone.utc).isoformat(),
            "cdpa_status_counts":counts,
            "blocked_reasons":blockers,
            "pi_runs":pi_runs[-20:],
            "business_outcomes_source":"G8 canonical tracker and employer receipts; Muse must read live G8",
            "next_review_questions":[
                "Which verified paid-work milestone moved forward?",
                "Which repeated failure caused the largest time loss?",
                "Which existing owner should receive a single focused repair?",
                "Which change measurably improved time-to-award or recovery?",
                "What is the highest-return action for the next iteration?"
            ]}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--record",action="store_true")
    args=p.parse_args()
    try:
        result=status()
        if args.record:
            DIR.mkdir(parents=True,exist_ok=True)
            key=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path=DIR / ("ops-"+key+".json")
            with path.open("x",encoding="utf-8") as f:
                json.dump(result,f,ensure_ascii=False,indent=2)
            result["evidence_path"]=str(path)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as ex:
        print(json.dumps({"state":"ERROR","reason":str(ex)[:300]}))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
