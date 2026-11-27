from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import replace

import pytest

from playwright_auto.cdpa_conversations import ConversationLeaseError, ConversationWriterLease
from playwright_auto.cdpa_projection import TaskProjection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from test_stopped_writer_proof import evidence


@pytest.fixture
def owned(tmp_path, evidence):
    db = RuntimeDB(tmp_path / "runtime.sqlite3")
    owner = ConversationWriterLease(db)
    saved = evidence["runtime_state"]["bindings"]["binding-old"]
    owner.bind(
        binding_id="binding-old", provider_id="chatgpt_web", endpoint_id=saved["endpoint_id"],
        endpoint_cdp_url=saved["endpoint_cdp_url"], physical_browser_id=saved["physical_browser_id"],
        page_target_identity=saved["page_target_identity"], conversation_url=saved["conversation_url"],
        verified_new_chat=False, task_id=saved["task_id"], role_key=saved["role_key"],
    )
    lease = owner.acquire("binding-old")
    for index, hop in enumerate(evidence["task"]["hops"]):
        attempt, _ = owner.record_send_intent(
            lease, idempotency_key=f"accepted-{index}", text=hop["prompt"],
            baseline_last_user_message_id=None if index == 0 else f"accepted-user-{index}",
            baseline_user_turn_count=index,
        )
        owner.transition_attempt(attempt["attempt_id"], "accepted")
        hop["send_attempt_id"] = attempt["attempt_id"]
    projection = TaskProjection(
        task_id="task-stopped", team="historical-team", status="STOPPED", surface="history",
        active_role=None, updated_at="2026-11-27T00:00:00Z",
        summary={"task_id": "task-stopped"}, detail={"task_id": "task-stopped"},
        private=copy.deepcopy(evidence["task"]),
    )
    db.upsert_task_projections([projection])
    evidence["task_version"] = db.get_task_version("task-stopped")
    evidence["command"]["expected_task_version"] = evidence["task_version"]
    evidence["runtime_state"] = db.get_snapshot("conversation_runtime")["payload"]
    evidence["attempts"] = [
        db.get_command(hop["send_attempt_id"])["payload"]["send_attempt"]
        for hop in evidence["task"]["hops"]
    ]
    assert evidence["runtime_state"]["bindings"]["binding-old"]["lease_epoch"] == lease["lease_epoch"]
    now = time.time()
    evidence["observations"][0]["observed_at"] = now - 0.5
    evidence["observations"][1]["observed_at"] = now - 0.1
    yield owner, evidence, projection, lease
    db.close()


def enqueue(owner, evidence, *, key="maintenance-command", version=None):
    method = getattr(owner, "seal_stopped_writer", None)
    assert callable(method), "existing owner has no task-bound proof-validated maintenance operation"
    command = evidence["command"]
    owner.db.enqueue_command(
        command_id=key, idempotency_key=key, kind="reconcile_stopped_writer",
        task_id="task-stopped",
        expected_task_version=command["expected_task_version"] if version is None else version,
        payload=copy.deepcopy(command["payload"]),
    )
    claimed = owner.db.claim_next_command(kinds=["reconcile_stopped_writer"])
    assert claimed["command_id"] == key
    return key


def seal(owner, evidence, command_id):
    return owner.seal_stopped_writer(
        command_id, task=evidence["task"], immutable_task=evidence["immutable_task"],
        request_records=evidence["request_records"], observations=evidence["observations"],
    )


def test_seal_is_one_owner_transaction_and_keeps_acceptance_and_parent_evidence(owned):
    owner, evidence, _, _ = owned
    parent = copy.deepcopy(evidence["task"])
    ledger = copy.deepcopy(evidence["request_records"])
    attempts = {h["send_attempt_id"]: owner.db.get_command(h["send_attempt_id"]) for h in parent["hops"]}
    owner.bind(
        binding_id="unrelated", provider_id="chatgpt_web", endpoint_id="another-endpoint",
        endpoint_cdp_url="http://127.0.0.1:9212", physical_browser_id="other-browser",
        page_target_identity="unrelated-page", conversation_url="https://chatgpt.com/c/unrelated",
        verified_new_chat=False, task_id="another-task", role_key="PLAN",
    )
    unrelated = owner.get_binding("unrelated")
    command_id = enqueue(owner, evidence)
    result = seal(owner, evidence, command_id)
    binding = owner.get_binding("binding-old")
    assert result["controller_writer_released"] is True
    assert result["task_id"] == "task-stopped" and result["binding_id"] == "binding-old"
    assert binding["binding_status"] == "sealed"
    assert binding["stopped_writer_release"] == result["proof"]
    assert result["proof"]["command_id"] == command_id
    state = owner.db.get_snapshot("conversation_runtime")["payload"]
    assert not any(lease.get("owner_binding_id") == "binding-old" for lease in state["leases"].values())
    assert "binding-old" not in state["page_claims"].values()
    assert evidence["task"] == parent and evidence["request_records"] == ledger
    assert owner.get_binding("unrelated") == unrelated
    assert all(owner.db.get_command(key) == value for key, value in attempts.items())
    # Command completion is separate; a crash here is replayable by its sealed proof.
    assert owner.db.get_command(command_id)["status"] == "running"


@pytest.mark.parametrize("state", ["intent_recorded", "unconfirmed", "accepted"])
def test_new_attempt_after_observation_prevents_seal(owned, state):
    owner, evidence, _, lease = owned
    command_id = enqueue(owner, evidence)
    new, _ = owner.record_send_intent(
        lease, idempotency_key="after-observation", text="another turn",
        baseline_last_user_message_id="accepted-user-2", baseline_user_turn_count=2,
    )
    if state != "intent_recorded":
        owner.transition_attempt(new["attempt_id"], state)
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        seal(owner, evidence, command_id)
    assert owner.db.get_snapshot("conversation_runtime") == before
    assert owner.get_binding("binding-old")["binding_status"] != "sealed"


@pytest.mark.parametrize("change", ["version", "task", "stop", "ledger", "physical", "target", "tail", "draft"])
def test_final_proof_rechecks_changes_since_admission(owned, change):
    owner, evidence, projection, _ = owned
    command_id = enqueue(owner, evidence)
    if change == "version":
        owner.db.upsert_task_projections([replace(projection, updated_at="2026-11-27T00:00:01Z")])
    elif change == "task":
        evidence["task"]["status"] = "RUNNING"
    elif change == "stop":
        evidence["task"]["cleanup"]["stop_control_id"] = 99
    elif change == "ledger":
        evidence["request_records"]["task-stopped-hop2"]["status"] = "sending"
    elif change in {"physical", "target"}:
        key = "physical_browser_id" if change == "physical" else "target_id"
        evidence["observations"][-1][key] = "changed"
    elif change == "tail":
        evidence["observations"][-1]["history_tail"] = None
    else:
        evidence["observations"][-1]["snapshot"]["composer_text"] = "Manual draft"
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        seal(owner, evidence, command_id)
    assert owner.db.get_snapshot("conversation_runtime") == before


def test_exact_seal_replay_survives_command_finish_gap_and_later_task_version(owned):
    owner, evidence, projection, _ = owned
    command_id = enqueue(owner, evidence)
    first = seal(owner, evidence, command_id)
    state_before = owner.db.get_snapshot("conversation_runtime")
    # Normal Stop cleanup / projection may advance independently after the seal.
    owner.db.upsert_task_projections([replace(projection, updated_at="2026-11-27T00:00:02Z")])
    owner.db.requeue_running_commands()
    assert owner.db.claim_next_command(kinds=["reconcile_stopped_writer"])["command_id"] == command_id
    replay = owner.replay_stopped_writer_release(command_id)
    assert replay["controller_writer_released"] is True and replay["reconciled"] is True
    assert replay["proof"] == first["proof"]
    assert owner.db.get_snapshot("conversation_runtime") == state_before
    owner.db.finish_command(command_id, result=replay)
    assert owner.db.get_command(command_id)["status"] == "applied"


def test_unsealed_command_cannot_replay_or_ignore_original_version(owned):
    owner, evidence, projection, _ = owned
    command_id = enqueue(owner, evidence)
    assert owner.replay_stopped_writer_release(command_id) is None
    owner.db.upsert_task_projections([replace(projection, updated_at="2026-11-27T00:00:03Z")])
    with pytest.raises(ConversationLeaseError):
        seal(owner, evidence, command_id)
    assert owner.get_binding("binding-old")["binding_status"] != "sealed"


def test_another_command_cannot_adopt_predecessor_seal_proof(owned):
    owner, evidence, _, _ = owned
    original = enqueue(owner, evidence)
    seal(owner, evidence, original)
    owner.db.finish_command(original, result={"controller_writer_released": True})
    other = enqueue(owner, evidence, key="different-command")
    with pytest.raises(ConversationLeaseError):
        owner.replay_stopped_writer_release(other)
    with pytest.raises(ConversationLeaseError):
        seal(owner, evidence, other)
    assert owner.get_binding("binding-old")["stopped_writer_release"]["command_id"] == original


@pytest.mark.parametrize("change", ["proof", "own_lease", "own_claim", "quarantine", "new_attempt"])
def test_replay_rejects_changed_seal_or_new_owner_evidence(owned, change):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    seal(owner, evidence, command_id)
    state = owner.db.get_snapshot("conversation_runtime")["payload"]
    binding = state["bindings"]["binding-old"]
    if change == "proof":
        binding["stopped_writer_release"]["evidence_sha256"] = "wrong"
    elif change == "own_lease":
        state["leases"][binding["writer_identity_key"]] = {"owner_binding_id": "binding-old"}
    elif change == "own_claim":
        state["page_claims"]["unexpected"] = "binding-old"
    elif change == "quarantine":
        state["quarantine"][binding["writer_identity_key"]] = {"reason": "new conflict"}
    else:
        # Inject a durable post-seal fault in this disposable database. The
        # production owner correctly refuses to create a new intent after seal.
        key = "unexpected-after-seal"
        attempt_id = "send-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        attempt = copy.deepcopy(evidence["attempts"][-1])
        attempt.update(attempt_id=attempt_id, state="unconfirmed", text_hash="unexpected")
        with owner.db.connection() as connection:
            connection.execute(
                "INSERT INTO command_queue(command_id,idempotency_key,kind,task_id,status,"
                "expected_task_version,created_at,payload_json) VALUES(?,?,'browser_send',?,"
                "'recovery_required',NULL,?,?)",
                (attempt_id, key, "task-stopped", "2026-11-27T00:00:00Z",
                 json.dumps({"send_attempt": attempt})),
            )
            connection.commit()
    if change != "new_attempt":
        owner.db.put_snapshot("conversation_runtime", state)
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        owner.replay_stopped_writer_release(command_id)
    assert owner.db.get_snapshot("conversation_runtime") == before


def test_projection_evidence_is_one_read_snapshot_and_not_a_release(owned):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    before = owner.db.get_snapshot("conversation_runtime")
    projected = owner.stopped_writer_maintenance_evidence()
    assert projected["commands_by_id"][command_id]["status"] == "running"
    assert len(projected["attempts_by_binding"]["binding-old"]) == 2
    assert any(binding["binding_id"] == "binding-old" for binding in projected["bindings"])
    assert projected["leases"] and projected["page_claims"]
    assert owner.db.get_snapshot("conversation_runtime") == before


def test_seal_uses_fresh_transaction_time_instead_of_an_old_admission_clock(owned):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    for observation in evidence["observations"]:
        observation["observed_at"] -= 10
    with pytest.raises(ConversationLeaseError):
        seal(owner, evidence, command_id)
    assert owner.get_binding("binding-old")["binding_status"] != "sealed"


def test_retired_binding_cannot_admit_another_send_intent(owned):
    owner, evidence, _, lease = owned
    command_id = enqueue(owner, evidence)
    seal(owner, evidence, command_id)
    with pytest.raises(ConversationLeaseError):
        owner.record_send_intent(
            lease, idempotency_key="send-after-release", text="must never send",
            baseline_last_user_message_id="accepted-user-2", baseline_user_turn_count=2,
        )
    assert owner.db.get_command_by_idempotency_key("send-after-release") is None
