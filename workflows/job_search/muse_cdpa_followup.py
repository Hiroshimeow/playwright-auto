#!/usr/bin/env python3
"""Conservative Muse lead result monitor and follow-up dispatcher.

Reads CDPA outcome files produced by independent Solo tasks. Never interprets
a submitted bid as an award. Uses GitHub labels as durable action reservations.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import muse_cdpa_bridge as bridge

ROOT = bridge.ROOT
OUTCOMES = ROOT / ".runtime" / "job-search" / "outcomes"
REPO = bridge.REPO
MAX_WATCH = 4
WATCH_INTERVAL = timedelta(hours=24)


def stage_result(team):
    path = OUTCOMES / (team + ".json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("stage"), str):
        raise ValueError("malformed outcome " + path.name)
    if data["stage"] not in {
        "applied", "awarded", "rejected", "not_submitted",
        "blocked", "working", "done", "paid"
    }:
        raise ValueError("unknown stage")
    return data


def issue_update(number, label):
    bridge.shell("gh", "issue", "edit", str(number), "-R", REPO, "--add-label", label)


def status(items):
    steps = []
    for issue in sorted(items, key=lambda x: x["number"]):
        labs = bridge.labels(issue)
        if "cdpa-dispatched" not in labs:
            continue
        if "cdpa-paid" in labs or "cdpa-closed" in labs or "cdpa-hold" in labs:
            continue
        url, title = bridge.parse(issue)
        root_team = "muse-lead-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        if "cdpa-work-dispatched" in labs:
            delivery = stage_result(root_team + "-work")
            if delivery and delivery.get("stage") == "paid" and delivery.get("receipt"):
                steps.append({"issue": issue["number"], "status": "paid",
                              "label": "cdpa-paid"})
            elif delivery and delivery.get("stage") == "done":
                steps.append({"issue": issue["number"], "status": "DELIVERED_AWAITING_PAYMENT"})
            continue
        count = max((n for n in range(1, MAX_WATCH+1)
                     if f"cdpa-watch-{n}" in labs), default=0)
        target_team = root_team if count == 0 else f"{root_team}-watch-{count}"
        outcome = stage_result(target_team)
        if outcome is None:
            steps.append({"issue": issue["number"], "status": "AWAITING_OUTCOME",
                          "team": target_team})
            continue
        state = outcome["stage"]
        if state in {"paid", "done", "rejected", "not_submitted", "blocked"}:
            lab = ("cdpa-paid" if state=="paid" else
                   "cdpa-closed" if state in {"done","rejected","not_submitted"} else "cdpa-hold")
            steps.append({"issue": issue["number"], "status": state, "label": lab})
            continue
        if state == "awarded":
            if not outcome.get("receipt"):
                steps.append({"issue": issue["number"], "status": "AWARD_MISSING_RECEIPT"})
                continue
            if "cdpa-work-dispatched" in labs or "cdpa-work-reserved" in labs:
                continue
            steps.append({"issue": issue["number"], "status": "WORK_READY",
                          "url": url, "title": title, "root_team": root_team})
            continue
        if state == "applied":
            if not outcome.get("receipt"):
                steps.append({"issue": issue["number"], "status": "APPLIED_MISSING_RECEIPT"})
                continue
            if count >= MAX_WATCH:
                steps.append({"issue": issue["number"], "status": "WATCH_LIMIT"})
                continue
            try:
                updated = datetime.fromisoformat(outcome["updated_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError, AttributeError):
                steps.append({"issue": issue["number"], "status": "INVALID_OUTCOME_TIME"})
                continue
            if updated.tzinfo is None:
                steps.append({"issue": issue["number"], "status": "INVALID_OUTCOME_TIME"})
                continue
            if datetime.now(timezone.utc) - updated < WATCH_INTERVAL:
                continue
            steps.append({"issue": issue["number"], "status": "WATCH_READY",
                          "number": count+1, "url": url,
                          "title": title, "root_team": root_team})
    return steps


def submit_followup(step, apply=False):
    issue = step["issue"]
    if step["status"] == "WATCH_READY":
        seq = step["number"]
        new_team = f'{step["root_team"]}-watch-{seq}'
        label = f"cdpa-watch-{seq}"
        task_title = f'Muse follow-up {seq}: {step["title"]}'[:110]
        goal = (
            f'You are DEV for CDPA Solo follow-up of a real submitted job. Job URL: {step["url"]}. '
            f'Previous task: {step["root_team"]}. Do NOT send another application. '
            'Read canonical G8 tracker, receipt, authority and pipeline contract before acting. '
            'Check employer/marketplace authenticated UI and communications for an actual '
            'acceptance/award, refusal, or unchanged pending state. Never fabricate acceptance '
            'or legal eligibility. If an award exists, record exact receipt/scope/payment; '
            'otherwise mark applied pending, rejected, or blocked. Update canonical G8. '
            f'Write JSON outcome file E:\\git-project\\cdpa\\.runtime\\job-search\\outcomes\\{new_team}.json '
            'with stage (applied, awarded, rejected, blocked, paid), detail, receipt, updated_at; '
            'do not overwrite previous receipt. Use your own NEW CDPA Solo chat on 9202.'
        )
    elif step["status"] == "WORK_READY":
        new_team = step["root_team"] + "-work"
        label = "cdpa-work-reserved"
        task_title = f'Muse awarded delivery: {step["title"]}'[:110]
        goal = (
            f'You are DEV for an AWARDED paid freelance delivery. Job URL: {step["url"]}. '
            f'Owner: {step["root_team"]}. Reconcile actual employer award scope and payment protection '
            'from the original application receipts and G8 canonical records. '
            'Do not begin unverified/unfunded scope. When paid work confirmed, build and verify '
            'the agreed deliverable, submit once and record delivery/payment receipt in G8; '
            f'write outcome E:\\git-project\\cdpa\\.runtime\\job-search\\outcomes\\{new_team}.json '
            'with stage working/done/paid/blocked, detail, receipt, updated_at. '
            'Respect original intellectual property and no-duplicate-send rules.'
        )
    else:
        return {"status": "NOT_SUBMIT_READY"}

    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "muse-cdpa:" + new_team))
    if not apply:
        return {"status": "DRY_RUN", "issue": issue, "team": new_team}
    # Durable action barrier before CDPA API create.
    issue_update(issue, label)
    argv = [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m",
            "playwright_auto.cdpa_cli", "--solo", "DEV", "--browser", "9202",
            "--team", new_team, "--task", task_title, "--goal", goal,
            "--idempotency-key", key, "--repository", str(ROOT),
            "--config", str(ROOT / "cdpa.yaml")]
    try:
        output = bridge.shell(*argv, timeout=26)
    except Exception:
        issue_update(issue, "cdpa-hold")
        return {"status": "HOLD_RECONCILE", "issue": issue, "team": new_team}
    if step["status"] == "WORK_READY":
        issue_update(issue, "cdpa-work-dispatched")
    return {"status": "DISPATCHED", "issue": issue, "team": new_team,
            "api": output[-250:]}


def once(apply=False):
    leads = json.loads(bridge.shell("gh", "issue", "list", "-R", REPO,
                                   "--state", "open", "--label", "cdpa-dispatched",
                                   "--limit", "100", "--json",
                                   "number,title,body,author,labels,url"))
    items = status(leads)
    for it in items:
        state = it["status"]
        if "label" in it:
            if apply:
                issue_update(it["issue"], it["label"])
            print(json.dumps({"issue": it["issue"], "status": state,
                              "apply": apply}))
            return 0
        if state in {"WATCH_READY", "WORK_READY"}:
            ok, owner = bridge.available(bridge.inventory())
            if not ok:
                print(json.dumps({"state": "BUSY_9202", "owner": owner}))
                return 0
            print(json.dumps(submit_followup(it, apply=apply), ensure_ascii=False))
            return 0
    print(json.dumps({"state": "NO_ACTION", "issues_seen": len(leads)}))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    try:
        sys.exit(once(args.apply))
    except Exception as exc:
        print(json.dumps({"error": str(exc)[-300:]}), file=sys.stderr)
        sys.exit(2)
