"""Disposable, offline falsification of Pi -> CDPA successor crash handoff.

Diagnostic only; never imports a browser, calls the CDPA API, or sends a request.
An exit code of 2 is evidence the release gate is NOT_ACCEPTED, not an
unexpected infrastructure/test error.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "workflows" / "self-evolution"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("offline_pi_runner", HERE / "run_pi.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from core import canonical_goal
from spawn_cdpa import command
from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_prompts import PromptBuilder
from playwright_auto import cdpa_cli

POLICY = canonical_goal()
PARENT = "cdpa-idem-offline-parent-20261107"
CONFIG = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
BUILDER = PromptBuilder(CONFIG)
results = []


def add_task(ledger: Path, task_id: str, *, parent: str = "", good: bool = True,
             attempts: int = 1, status: str = "RUNNING") -> Path:
    team = "offline-" + task_id
    title = "CDPA Self-Evolution TEST: " + team
    bounded = "Verify atomic handoff in disposable ledger"
    argv = command("TEST", team, bounded, parent)
    goal = argv[argv.index("--goal") + 1] if good else "truncated contract"
    first_turn = BUILDER.build_solo(task_name=title, logical_role="TEST", goal=goal).text
    path = ledger / team / task_id
    path.mkdir(parents=True, exist_ok=False)
    manifest = {
        "task_id": task_id, "team": team, "task_title": title,
        "status": status, "workflow_mode": "solo", "goal": goal,
        "parent_task_id": parent,
        "hops": [{"request_id": task_id + "-hop1", "prompt": first_turn if good else "invalid",
                  "receipt": {"prompt": first_turn if good else "invalid",
                              "attempts": attempts}}],
    }
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    requests = {"version": 1, "records": {
        task_id + "-hop1": {"request_id": task_id + "-hop1",
                            "status": "sent" if attempts else "reserved",
                            "attempts": attempts},
    }}
    (path / "requests.json").write_text(json.dumps(requests), encoding="utf-8")
    return path


def inventory(ledger: Path):
    """Feed production campaign_tasks() a projection of disposable file-backed manifests."""
    def offline_urlopen(_url, timeout=0):
        assert _url == "http://127.0.0.1:9225/api/tasks"
        items = []
        for path in ledger.rglob("manifest.json"):
            d = json.loads(path.read_text(encoding="utf-8"))
            items.append({k: d[k] for k in ("task_id", "task_title", "status")})
        return io.BytesIO(json.dumps({"items": items}).encode("utf-8"))
    old = runner.urlopen
    try:
        runner.urlopen = offline_urlopen
        return runner.campaign_tasks()
    finally:
        runner.urlopen = old


def check(window: str, passed: bool, observed: dict, explanation: str):
    results.append({"window": window, "gate": "PASS" if passed else "NOT_ACCEPTED",
                    "observed": observed, "explanation": explanation})


with tempfile.TemporaryDirectory(prefix="cdpa-atomic-handoff-r2-") as temporary:
    tmp = Path(temporary)

    # Positive bounded control: the actual canonical constructor and first-turn renderer.
    ledger = tmp / "full-contract"
    child = add_task(ledger, "cdpa-idem-offline-child-good", parent=PARENT)
    manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
    request = json.loads((child / "requests.json").read_text(encoding="utf-8"))["records"]
    first = manifest["hops"][0]
    good = (POLICY in first["prompt"]
            and first["prompt"] == first["receipt"]["prompt"]
            and str(HERE / "GOAL_AND_RULES.md") in first["prompt"]
            and f"Parent task: {PARENT}" in first["prompt"]
            and "spawn_cdpa.py" in first["prompt"]
            and list(request.values())[0]["attempts"] == 1)
    check("first-turn-policy-and-attempts", good,
          {"full_policy": POLICY in first["prompt"],
           "parent": manifest["parent_task_id"],
           "attempts": list(request.values())[0]["attempts"]},
          "Offline constructor/PromptBuilder only; not an accepted browser Send")

    # Crash before CDPA submit: runner records only a runner-local label.
    ledger = tmp / "pre-submit"
    add_task(ledger, PARENT)
    before = inventory(ledger)
    status, reason = runner.classify_cycle(before, inventory(ledger), "", 137)
    durable_holds = list(ledger.rglob("safety-hold.json"))
    check("pre-submit-crash", status == "SAFETY_HOLD" and len(durable_holds) == 1,
          {"status": status, "reason": reason, "committed_successors": 0,
           "durable_holds": len(durable_holds)},
          "No atomically committed successor or CDPA-owned durable hold")

    # Lost acknowledgement: real Solo CLI generates a new idempotency key per invocation.
    keys = []
    old_post = cdpa_cli._post
    def offline_post(_config, _endpoint, _payload, *, idempotency_key=None):
        assert _endpoint == "/api/tasks"
        keys.append(idempotency_key)
        return {"task_id": "cdpa-idem-offline-reservation-" + str(len(keys))}
    try:
        cdpa_cli._post = offline_post
        argv = command("TEST", "offline-unack", "Prove no replay", PARENT)
        goal = argv[argv.index("--goal") + 1]
        for _ in range(2):
            cdpa_cli.submit_task(CONFIG, task="offline", repository=None,
                                 team="offline-unack", new_roles=(), new_all=False,
                                 workflow_mode="solo", solo_role="TEST",
                                 browser_endpoint_id="9202", goal=goal)
    finally:
        cdpa_cli._post = old_post
    check("submitted-but-unacknowledged", bool(keys[0] and keys[0] == keys[1]),
          {"same_submission_identity": keys[0] == keys[1],
           "cli_has_explicit_idempotency_key": "--idempotency-key" in argv,
           "offline_submit_calls": len(keys)},
          "No stable per-parent submit identity; second invocation can allocate a second task")

    # Child committed but Pi checkpoint absent: status + untrusted log can falsely verify
    # a child lacking full goal, correct request provenance and exactly-once Send.
    ledger = tmp / "post-commit-pre-checkpoint"
    add_task(ledger, PARENT)
    before = inventory(ledger)
    bad = "cdpa-idem-offline-child-bad"
    child = add_task(ledger, bad, parent="wrong-parent", good=False, attempts=2)
    after = inventory(ledger)
    status, reason = runner.classify_cycle(before, after, "PI_ACTION: DISPATCH " + bad, 0)
    stored = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
    attempts = next(iter(json.loads((child / "requests.json").read_text())["records"].values()))["attempts"]
    check("post-commit-pre-checkpoint", status != "DISPATCH_VERIFIED",
          {"status": status, "reason": reason, "wrong_parent": stored["parent_task_id"],
           "first_turn_has_policy": POLICY in stored["hops"][0]["prompt"],
           "request_attempts": attempts},
          "Projection+PI_ACTION mislabels invalid child as verified without ledger/parent/receipt checks")

    # Crash after commit, followed by a second Pi cycle: no authoritative pending intent
    # prevents a different successor for the same parent.
    ledger = tmp / "coordinator-child-termination"
    add_task(ledger, PARENT)
    before = inventory(ledger)
    first_id = "cdpa-idem-offline-first-child"
    add_task(ledger, first_id, parent=PARENT)
    stage1, _ = runner.classify_cycle(before, inventory(ledger),
                                       "PI_ACTION: DISPATCH " + first_id, 1)
    after_restart = inventory(ledger)
    second_id = "cdpa-idem-offline-second-child"
    add_task(ledger, second_id, parent=PARENT)
    stage2, _ = runner.classify_cycle(after_restart, inventory(ledger),
                                       "PI_ACTION: DISPATCH " + second_id, 0)
    children = [json.loads(p.read_text(encoding="utf-8"))
                for p in ledger.rglob("manifest.json") if p.parent.name != PARENT]
    children = [d for d in children if d.get("parent_task_id") == PARENT]
    check("coordinator-child-termination", len(children) == 1 or stage2 != "DISPATCH_VERIFIED",
          {"first_cycle": stage1, "second_cycle": stage2,
           "committed_children_same_parent": len(children)},
          "Restart can classify a second child as verified with no persisted parent handoff owner")

    # A log declaration is not a durable safety-hold transaction.
    ledger = tmp / "undurable-safety-hold"
    add_task(ledger, PARENT)
    before = inventory(ledger)
    status, reason = runner.classify_cycle(
        before, before, "PI_ACTION: SAFETY_HOLD task " + PARENT + " blocked", 0)
    durable = list(ledger.rglob("safety-hold.json"))
    check("safety-hold-provenance", status != "SAFETY_HOLD" or bool(durable),
          {"status": status, "reason": reason, "durable_holds": len(durable)},
          "Log text alone is accepted as SAFETY_HOLD without an authoritative hold receipt")

print(json.dumps({"kind": "OFFLINE_ATOMIC_HANDOFF_DIAGNOSTIC", "device": "G6",
                  "no_network_or_browser_send": True,
                  "total": len(results),
                  "passed": sum(x["gate"] == "PASS" for x in results),
                  "not_accepted": sum(x["gate"] != "PASS" for x in results),
                  "cases": results}, indent=2, ensure_ascii=False))
raise SystemExit(2 if any(x["gate"] != "PASS" for x in results) else 0)
