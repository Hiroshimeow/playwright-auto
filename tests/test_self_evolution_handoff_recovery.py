"""Fault injection for Pi-to-CDPA atomic successor reconciliation (offline only)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "workflows" / "self-evolution"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))
from playwright_auto.cdpa_identity import generate_idempotent_task_id
from test_dashboard_api import request, start_api

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

spawn = load("handoff_r4_spawn", HERE / "spawn_cdpa.py")
runner = load("handoff_r4_runner", HERE / "run_pi.py")


def argv_field(args, flag):
    return args[args.index(flag) + 1]


def setup_intent(tmp_path, monkeypatch, *, parent="cdpa-idem-parent", team="handoff-r4"):
    monkeypatch.setattr(runner, "STATE", tmp_path / "pi-runner.json")
    monkeypatch.setattr(runner, "STATE_LOCK", tmp_path / "pi-state.lock")
    runner.write_state(status="RUNNING", owner_pid=10, pi_pid=20, dispatch_allowed=False)
    cmd = spawn.command("DEV", team, "Inspect atomic child", parent)
    transition = runner.record_transition_intent(
        parent=parent, team=team, role="DEV",
        goal=argv_field(cmd, "--goal"),
        idempotency_key=argv_field(cmd, "--idempotency-key"),
    )
    return cmd, transition


def test_pre_submit_crash_preserves_one_parent_transition_through_heartbeats(tmp_path, monkeypatch):
    cmd, trans = setup_intent(tmp_path, monkeypatch)
    assert trans["parent_task_id"] == "cdpa-idem-parent"
    assert trans["idempotency_key"] == argv_field(cmd, "--idempotency-key")
    assert trans["task_id"] == generate_idempotent_task_id(argv_field(cmd, "--task"), trans["idempotency_key"])
    runner.write_state(status="RUNNING", owner_pid=10, pi_pid=20, no_output_seconds=5)
    persisted = json.loads(runner.STATE.read_text(encoding="utf-8"))
    assert persisted["transition"] == trans
    assert persisted["dispatch_allowed"] is False
    with pytest.raises(RuntimeError, match="conflict"):
        runner.record_transition_intent(
            parent="cdpa-idem-parent", team="second-team", role="DEV",
            goal=argv_field(cmd, "--goal"), idempotency_key=trans["idempotency_key"],
        )
    assert runner.unreconciled_state()["transition"] == trans


def test_no_accepted_command_after_intent_is_authoritative_hold_not_a_resend(tmp_path, monkeypatch):
    _cmd, trans = setup_intent(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "command_by_key", lambda key: None)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {})
    result, reason = runner.reconcile_transition(trans)
    assert result == "SAFETY_HOLD"
    assert "command" in reason.lower()
    assert trans["parent_task_id"] in reason


def child_files(tmp_path, trans, monkeypatch):
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_prompts import PromptBuilder

    config = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
    goal = argv_field(spawn.command("DEV", trans["team"], "Inspect atomic child",
                                   trans["parent_task_id"]), "--goal")
    title = f"CDPA Self-Evolution DEV: {trans['team']}"
    prompt = PromptBuilder(config).build_solo(task_name=title, logical_role="DEV", goal=goal).text
    task_id = trans["task_id"]
    target = tmp_path / ".plan" / trans["team"] / task_id
    target.mkdir(parents=True)
    receipt = {
        "prompt": prompt, "attempts": 1, "accepted_via": "user_message_identity",
        "user_message_id": "user-r4", "conversation_id": "conversation-r4",
    }
    manifest = {
        "task_id": task_id, "team": trans["team"], "parent_task_id": trans["parent_task_id"],
        "applied_command_ids": ["cmd-r4"], "goal": goal, "hops": [{
            "request_id": task_id + "-hop1", "prompt": prompt, "receipt": receipt,
        }],
    }
    ledger = {"records": {task_id + "-hop1": {
        "request_id": task_id + "-hop1", "attempts": 1, "status": "sent",
        "prompt": prompt, "receipt": receipt,
    }}}
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    return target, manifest, ledger


def fake_command(trans, status="applied"):
    return {
        "command_id": "cmd-r4", "idempotency_key": trans["idempotency_key"], "task_id": trans["task_id"],
        "status": status, "kind": "create_task",
        "parent_task_id": trans["parent_task_id"], "team": trans["team"],
        "goal_sha256": trans["goal_sha256"], "role": trans["role"],
    }


def test_lost_ack_and_commit_before_checkpoint_reconcile_without_pi_claim(tmp_path, monkeypatch):
    _cmd, trans = setup_intent(tmp_path, monkeypatch)
    target, manifest, ledger = child_files(tmp_path, trans, monkeypatch)
    command = fake_command(trans)
    monkeypatch.setattr(runner, "command_by_key", lambda key: command)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        trans["task_id"]: {"team": trans["team"], "parent_task_id": trans["parent_task_id"], "status": "RUNNING"},
    })
    assert runner.reconcile_transition(trans) == ("DISPATCH_VERIFIED", trans["task_id"])

    ledger["records"][trans["task_id"] + "-hop1"]["attempts"] = 2
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"
    ledger["records"][trans["task_id"] + "-hop1"]["attempts"] = 1
    ledger["records"][trans["task_id"] + "-hop1"]["prompt"] = "wrong prompt"
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"


def test_pending_command_and_wrong_parent_or_goal_are_not_dispatch(tmp_path, monkeypatch):
    _cmd, trans = setup_intent(tmp_path, monkeypatch)
    child_files(tmp_path, trans, monkeypatch)
    command = fake_command(trans, status="queued")
    monkeypatch.setattr(runner, "command_by_key", lambda key: command)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        trans["task_id"]: {"team": trans["team"], "parent_task_id": trans["parent_task_id"], "status": "INBOX"},
    })
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"
    command["status"] = "applied"
    command["parent_task_id"] = "other-parent"
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"
    command["parent_task_id"] = trans["parent_task_id"]
    command["goal_sha256"] = hashlib.sha256(b"different").hexdigest()
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"
    command["goal_sha256"] = trans["goal_sha256"]
    command["command_id"] = "wrong-creation-command"
    assert runner.reconcile_transition(trans)[0] == "SAFETY_HOLD"


def test_real_cli_stripped_goal_and_api_missing_role_reconcile_same_child(tmp_path, monkeypatch):
    """The live G6 CLI strips the final newline; its command projection has no role."""
    from playwright_auto.cdpa_config import load_cdpa_config
    from playwright_auto.cdpa_prompts import PromptBuilder

    _cmd, trans = setup_intent(tmp_path, monkeypatch)
    target, manifest, ledger = child_files(tmp_path, trans, monkeypatch)
    normalized = manifest["goal"].strip()
    config = load_cdpa_config(str(ROOT / "cdpa.yaml"), repository_root=ROOT)
    prompt = PromptBuilder(config).build_solo(
        task_name=f"CDPA Self-Evolution DEV: {trans['team']}",
        logical_role="DEV", goal=normalized,
    ).text
    manifest["goal"] = normalized
    manifest["start_role"] = "DEV"
    manifest["hops"][0]["target_role"] = "DEV"
    manifest["hops"][0]["prompt"] = prompt
    manifest["hops"][0]["receipt"]["prompt"] = prompt
    ledger["records"][trans["task_id"] + "-hop1"]["prompt"] = prompt
    ledger["records"][trans["task_id"] + "-hop1"]["receipt"]["prompt"] = prompt
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    command = fake_command(trans)
    command["goal_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    command["role"] = None
    monkeypatch.setattr(runner, "command_by_key", lambda key: command)
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        trans["task_id"]: {
            "team": trans["team"], "parent_task_id": trans["parent_task_id"],
            "status": "RUNNING",
        },
    })
    assert runner.reconcile_transition(trans) == ("DISPATCH_VERIFIED", trans["task_id"])


def test_missing_conversation_id_requires_exact_owned_page_binding(tmp_path, monkeypatch):
    """Real accepted Send can carry a stable owned page URL without conversation_id."""
    _cmd, trans = setup_intent(tmp_path, monkeypatch)
    target, manifest, ledger = child_files(tmp_path, trans, monkeypatch)
    hop = manifest["hops"][0]
    hop["target_role"] = "DEV"
    hop["physical_role"] = "owned-dev"
    hop["conversation_url"] = "https://chatgpt.com/c/exact-owned-thread"
    manifest["roles"] = {"DEV": {
        "logical_role": "DEV", "physical_role": "owned-dev",
        "page_id": "owned-page", "page_url": hop["conversation_url"],
    }}
    hop["receipt"]["conversation_id"] = None
    hop["receipt"]["binding"] = {"page_id": "owned-page", "role": "owned-dev"}
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / "requests.json").write_text(json.dumps(ledger), encoding="utf-8")
    after = {trans["task_id"]: {"team": trans["team"]}}
    claim = f"DISPATCH {trans['task_id']} parent {trans['parent_task_id']}"
    before = {trans["parent_task_id"]: {"status": "DONE"}}
    assert runner.verified_successor(before, after, trans["task_id"], claim)
    manifest["roles"]["DEV"]["page_id"] = "different-owner"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not runner.verified_successor(before, after, trans["task_id"], claim)


def test_canonical_constructor_persists_intent_before_any_subprocess(monkeypatch):
    events = []
    monkeypatch.setattr(sys, "argv", [
        "spawn_cdpa.py", "--role", "DEV", "--team", "handoff-r4",
        "--instruction", "Inspect atomic child", "--parent", "cdpa-idem-parent",
    ])
    import run_pi

    def record(**values):
        events.append(("intent", values))
        return values

    class Result:
        returncode = 0

    def submit(cmd, **_kwargs):
        events.append(("submit", cmd))
        return Result()

    monkeypatch.setattr(run_pi, "record_transition_intent", record)
    monkeypatch.setattr(spawn.subprocess, "run", submit)
    with pytest.raises(SystemExit) as done:
        spawn.main()
    assert done.value.code == 0
    assert [kind for kind, _ in events] == ["intent", "submit"]
    assert events[0][1]["parent"] == "cdpa-idem-parent"
    assert events[0][1]["idempotency_key"] == argv_field(events[1][1], "--idempotency-key")


def test_pi_restart_reconciles_committed_child_without_spawning(tmp_path, monkeypatch):
    from contextlib import nullcontext

    _, trans = setup_intent(tmp_path, monkeypatch)
    child_files(tmp_path, trans, monkeypatch)
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner, "single_owner", lambda: nullcontext())
    monkeypatch.setattr(runner, "contain_current_process_tree", lambda: None)
    monkeypatch.setattr(runner, "previous_pi_still_running", lambda: False)
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "DESKTOP-B5E4IUE")
    monkeypatch.setattr(runner, "health", lambda: {"ready": True})
    monkeypatch.setattr(runner, "command_by_key", lambda key: fake_command(trans))
    monkeypatch.setattr(runner, "campaign_tasks", lambda: {
        trans["task_id"]: {
            "team": trans["team"], "parent_task_id": trans["parent_task_id"],
            "status": "RUNNING",
        },
    })
    monkeypatch.setattr(
        runner, "run_cycle", lambda _: pytest.fail("restart must never resubmit"),
    )

    class RestartResolved(Exception):
        pass

    def end_after_success(*_args):
        state = json.loads(runner.STATE.read_text(encoding="utf-8"))
        assert state["status"] == "DISPATCH_VERIFIED"
        assert state["last_transition"] == trans
        assert state["transition"] is None
        raise RestartResolved

    monkeypatch.setattr(runner.time, "sleep", end_after_success)
    # End immediately after reconciliation via the next campaign inventory probe.
    original_unreconciled = runner.unreconciled_state

    def one_reconciliation_only():
        if runner.STATE.exists():
            state = json.loads(runner.STATE.read_text(encoding="utf-8"))
            if state.get("status") == "DISPATCH_VERIFIED":
                end_after_success()
        return original_unreconciled()

    monkeypatch.setattr(runner, "unreconciled_state", one_reconciliation_only)
    with pytest.raises(RestartResolved):
        runner.run()


def test_existing_command_ledger_exposes_read_only_lookup_by_stable_key(tmp_path):
    _config, db, server, thread = start_api(tmp_path)
    key = "11111111-2222-3333-4444-555555555555"
    try:
        db.enqueue_command(
            command_id="cmd-r4", idempotency_key=key,
            kind="create_task", task_id="cdpa-idem-child", expected_task_version=None,
            payload={
                "workflow_mode": "solo", "role": "DEV", "task": "CDPA Self-Evolution DEV: handoff-r4",
                "requested_team": "handoff-r4", "parent_task_id": "cdpa-idem-parent",
                "goal": "Full goal; do not leak through lookup",
            },
        )
        status, _headers, body = request(server, "GET", "/api/commands/by-idempotency/" + key)
        assert status == 200
        value = json.loads(body)
        assert value["task_id"] == "cdpa-idem-child"
        assert value["parent_task_id"] == "cdpa-idem-parent"
        assert value["idempotency_key"] == key
        assert value["goal_sha256"] == hashlib.sha256(
            b"Full goal; do not leak through lookup"
        ).hexdigest()
        assert "Full goal; do not leak" not in body.decode()
        status, _, _ = request(server, "GET", "/api/commands/by-idempotency/" + "no-match")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
