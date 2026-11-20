"""Offline regression for self-evolution parent handoff and CDPA creation idempotency.

No network requests, browser Send, or live runtime writes.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "workflows" / "self-evolution"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("self_evolution_atomic_spawn", HERE / "spawn_cdpa.py")
spawn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spawn)
spec = importlib.util.spec_from_file_location("self_evolution_atomic_runner", HERE / "run_pi.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from playwright_auto import cdpa_cli
from test_cdpa_solo_task import make_store
from playwright_auto.dashboard_api import DashboardAPI


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_successor_creation_key_is_stable_for_exact_parent_not_new_team():
    first = spawn.command("TEST", "unique-team-a", "Verify parent", "cdpa-idem-parent")
    after_restart = spawn.command("DEV", "unique-team-b", "Different instruction", "cdpa-idem-parent")
    other_parent = spawn.command("TEST", "unique-team-c", "Verify other", "cdpa-idem-other")
    assert flag(first, "--idempotency-key") == flag(after_restart, "--idempotency-key")
    assert flag(first, "--idempotency-key") != flag(other_parent, "--idempotency-key")
    assert flag(first, "--parent-task-id") == "cdpa-idem-parent"
    assert flag(after_restart, "--parent-task-id") == "cdpa-idem-parent"


def test_existing_cli_submit_uses_exact_parent_and_stable_command_key(monkeypatch):
    config = object()
    seen = []

    def fake_post(_config, endpoint, payload, *, idempotency_key):
        assert endpoint == "/api/tasks"
        seen.append((payload, idempotency_key))
        return {"command_id": "cmd-once", "task_id": "cdpa-idem-child", "status": "queued"}

    monkeypatch.setattr(cdpa_cli, "_post", fake_post)
    for _ in range(2):
        cdpa_cli.submit_task(
            config, task="CDPA Self-Evolution DEV: child", repository=None,
            team="unique-team-a", new_roles=(), new_all=False,
            workflow_mode="solo", solo_role="DEV", browser_endpoint_id="9202",
            goal="Full immutable goal", parent_task_id="cdpa-idem-parent",
            idempotency_key="cdpa-self-evolution-parent-key",
        )
    assert seen[0] == seen[1]
    assert seen[0][0]["parent_task_id"] == "cdpa-idem-parent"
    assert seen[0][1] == "cdpa-self-evolution-parent-key"


def test_api_store_manifest_records_parent_without_dependency_block(tmp_path):
    config, store = make_store(tmp_path)
    parent = store.create_task(
        "Parent review", task_id="cdpa-idem-parent", requested_team="parent-review",
        workflow_mode="solo", roles=("REVIEW",), browser_endpoint_id="9222",
    )
    payload = DashboardAPI(config).normalize_create({
        "workflow_mode": "solo",
        "task": "CDPA Self-Evolution DEV: child",
        "requested_team": "unique-team-a",
        "role": "DEV",
        "goal": "Full immutable goal",
        "browser_endpoint_id": "9222",
        "parent_task_id": parent["task_id"],
    })
    child = store.create_task(
        payload["task"], task_id="cdpa-idem-child",
        requested_team=payload["requested_team"], workflow_mode="solo",
        roles=("DEV",), browser_endpoint_id="9222", goal=payload["goal"],
        parent_task_id=payload["parent_task_id"],
    )
    assert child["parent_task_id"] == "cdpa-idem-parent"
    assert child["depends_on_task_ids"] == []  # Provenance is not a scheduling dependency.


def test_output_claim_is_not_a_durable_safety_hold():
    before = {"cdpa-idem-parent": {"status": "DONE", "team": "parent-review"}}
    state, _ = runner.classify_cycle(
        before, before,
        "PI_ACTION: SAFETY_HOLD task cdpa-idem-parent blocked", 0,
    )
    assert state == "NO_PROGRESS"


def test_child_creation_requires_proven_parent_and_request_not_pi_text(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    parent = "cdpa-idem-parent"
    child = "cdpa-idem-child"
    before = {parent: {"status": "DONE", "team": "parent-review"}}
    after = {
        **before, child: {
            "status": "RUNNING", "team": "unique-team-a",
            "parent_task_id": parent,
        },
    }
    # No manifest/requests/receipt exists: a matching PI_ACTION is untrusted.
    state, reason = runner.classify_cycle(before, after, f"PI_ACTION: DISPATCH {child}", 0)
    assert state != "DISPATCH_VERIFIED"
    assert child in reason


def test_valid_offline_child_requires_exact_parent_receipt_request_and_policy(tmp_path, monkeypatch):
    import json

    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_prompts import PromptBuilder

    parent, child = "cdpa-idem-parent", "cdpa-idem-child"
    team = "unique-team-a"
    root = tmp_path
    target = root / ".plan" / team / child
    target.mkdir(parents=True)
    cmd = spawn.command("DEV", team, "Verify offline handoff", parent)
    title = flag(cmd, "--task")
    goal = flag(cmd, "--goal")
    config = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
    prompt = PromptBuilder(config).build_solo(task_name=title, logical_role="DEV", goal=goal).text
    manifest = {
        "task_id": child, "team": team, "parent_task_id": parent, "goal": goal,
        "hops": [{
            "request_id": child + "-hop1", "prompt": prompt,
            "receipt": {
                "prompt": prompt, "attempts": 1,
                "accepted_via": "user_message_identity",
                "user_message_id": "user-atomic", "conversation_id": "conversation-atomic",
            },
        }],
    }
    (target / "primary.json").write_text(json.dumps(manifest), encoding="utf-8")
    ledger = {"records": {child + "-hop1": {
        "request_id": child + "-hop1", "status": "sent", "attempts": 1,
        "prompt": prompt, "receipt": manifest["hops"][0]["receipt"],
    }}}
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    before = {parent: {"status": "DONE", "team": "parent-review"}}
    after = {**before, child: {"status": "RUNNING", "team": team, "parent_task_id": parent}}
    claim = f"PI_ACTION: DISPATCH {child} parent {parent} request {child}-hop1"
    assert runner.classify_cycle(before, after, claim, 0) == ("DISPATCH_VERIFIED", child)

    ledger["records"][child + "-hop1"]["attempts"] = 2
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    assert runner.classify_cycle(before, after, claim, 0)[0] == "SAFETY_HOLD"

    ledger["records"][child + "-hop1"]["attempts"] = 1
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    manifest["parent_task_id"] = "wrong-parent"
    (target / "primary.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert runner.classify_cycle(before, after, claim, 0)[0] == "SAFETY_HOLD"




def test_ambiguous_submit_reuses_durable_cdpa_command_and_rejects_changed_payload(tmp_path):
    from playwright_auto.dashboard_api import APIError

    config, _store = make_store(tmp_path)
    api = DashboardAPI(config)
    payload = api.normalize_create({
        "workflow_mode": "solo", "task": "CDPA Self-Evolution DEV: child",
        "requested_team": "stable-team", "role": "DEV",
        "browser_endpoint_id": "9222",
        "goal": "Full immutable goal",
        "parent_task_id": "cdpa-idem-parent",
    })
    key = flag(spawn.command("DEV", "stable-team", "repair", "cdpa-idem-parent"),
               "--idempotency-key")
    first = api.enqueue(idempotency_key=key, kind="create_task",
                        task_id=None, payload=payload)
    second = api.enqueue(idempotency_key=key, kind="create_task",
                         task_id=None, payload=payload)
    assert first["command_id"] == second["command_id"]
    assert first["task_id"] == second["task_id"]
    assert api.db.get_command_by_idempotency_key(key)["command_id"] == first["command_id"]
    with pytest.raises(APIError, match="Idempotency-Key"):
        api.enqueue(idempotency_key=key, kind="create_task", task_id=None,
                    payload={**payload, "requested_team": "accidental-second-team"})

def test_pre_submit_and_restart_holds_survive_runner_checkpoint(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(runner, "STATE", tmp_path / "runner.json")
    parent = "cdpa-idem-parent"
    before = {parent: {"status": "RUNNING", "team": "parent"}}
    runner.commit_cycle_decision(
        iteration=1, before=before, after=before, output="", exit_code=137
    )
    stored = json.loads(runner.STATE.read_text(encoding="utf-8"))
    assert stored["status"] == "SAFETY_HOLD"
    assert parent in stored["affected_task_ids"]
    assert "Pi exited" in stored["reason"]
    assert runner.unreconciled_state() is not None
    assert stored["dispatch_allowed"] is False
    # A restarted runner must not silently reenter the dispatch path.
    runner.commit_cycle_decision(
        iteration=2, before=before, after=before, output="", exit_code=0
    )
    assert runner.unreconciled_state()["dispatch_allowed"] is False




def test_runner_restart_does_not_dispatch_while_authoritative_reconciliation_pending(
    tmp_path, monkeypatch
):
    import json
    from contextlib import nullcontext

    monkeypatch.setattr(runner, "STATE", tmp_path / "pi-runner.json")
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner, "single_owner", lambda: nullcontext())
    monkeypatch.setattr(runner, "contain_current_process_tree", lambda: None)
    monkeypatch.setattr(runner, "previous_pi_still_running", lambda: False)
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "DESKTOP-B5E4IUE")
    monkeypatch.setattr(runner, "health", lambda: {"ready": False})
    monkeypatch.setattr(
        runner, "run_cycle",
        lambda iteration: pytest.fail("unreconciled restart must not dispatch Pi"),
    )
    class EndProbe(Exception):
        pass
    def stop_after_heartbeat(_seconds):
        raise EndProbe
    monkeypatch.setattr(runner.time, "sleep", stop_after_heartbeat)
    runner.STATE.write_text(json.dumps({
        "status": "RUNNING", "pi_pid": 222, "owner_pid": 111,
        "affected_task_ids": ["cdpa-idem-parent"],
        "dispatch_allowed": False,
    }), encoding="utf-8")
    with pytest.raises(EndProbe):
        runner.run()
    state = json.loads(runner.STATE.read_text(encoding="utf-8"))
    assert state["status"] == "SAFETY_HOLD"
    assert state["dispatch_allowed"] is False
    assert state["affected_task_ids"] == ["cdpa-idem-parent"]

def test_post_commit_before_checkpoint_forces_reconciliation_not_replay(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(runner, "STATE", tmp_path / "runner.json")
    parent, child = "cdpa-idem-parent", "cdpa-idem-child"
    before = {parent: {"status": "RUNNING", "team": "parent"}}
    after = {**before, child: {"status": "INBOX", "team": "unique-team-a"}}
    runner.commit_cycle_decision(
        iteration=1, before=before, after=after,
        output=f"PI_ACTION: DISPATCH {child} parent {parent}", exit_code=1,
    )
    saved = json.loads(runner.STATE.read_text(encoding="utf-8"))
    assert saved["status"] == "SAFETY_HOLD"
    assert saved["affected_task_ids"] == sorted((parent, child))
    assert saved["dispatch_allowed"] is False
    assert child + "-hop1" in saved["possible_request_ids"]
