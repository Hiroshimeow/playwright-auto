from __future__ import annotations
import copy
import hashlib
import json
import threading
from pathlib import Path

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_projection import TaskProjection, build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.dashboard_api import APIError, DashboardAPI, create_server
from test_dashboard_api import request


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@pytest.fixture
def stopped():
    return {
        "task_id": "task-stopped", "team": "alpha", "status": "STOPPED",
        "active_hop_id": None, "active_role": None, "active_action": None,
        "conversation_mode": "persistent", "task_text": "Preserve the stopped work",
        "created_at": "2026-11-27T00:00:00+00:00", "updated_at": "2026-11-27T00:01:00+00:00",
        "cleanup": {"state": "CLEARING", "phase": "writer_stop_pending", "stop_control_id": 7},
        "controls": [{"control_id": 7, "action": "stop", "status": "applied",
                      "command_state": "APPLIED", "confirmed": True, "origin": "operator",
                      "external_command_id": "original-stop", "role": None,
                      "command": {"task_id": "task-stopped"},
                      "result": {"writer_cleanup_pending": True}}],
        "roles": {"DEV": {"physical_role": "alpha-dev", "turn": 1, "status": "active",
                          "conversation_binding_id": "binding-dev", "page_id": "private-page",
                          "conversation_generation": 1}},
        "hops": [{"hop_id": 1, "target_role": "DEV", "physical_role": "alpha-dev",
                  "turn": 1, "state": "waiting", "request_id": "private-request",
                  "conversation_binding_id": "binding-dev", "conversation_generation": 1}],
    }


@pytest.fixture
def payload():
    return {"binding_id": "binding-dev", "stop_control_id": 7,
            "endpoint_id": "original-endpoint", "endpoint_cdp_url": "http://127.0.0.1:9202",
            "physical_browser_id": "devtools:current", "target_id": "existing-target"}


def binding(task_id="task-stopped", role="DEV", identifier="binding-dev"):
    return {"binding_id": identifier, "task_id": task_id, "role_key": role,
            "binding_status": "blocked", "provider_id": "chatgpt_web", "temporary_chat": False,
            "endpoint_id": "original-endpoint", "endpoint_cdp_url": "http://127.0.0.1:9202",
            "canonical_conversation_id": "private-conversation", "lease_epoch": 1,
            "writer_identity_key": "conversation:"+identifier, "provisional_writer_identity_key": "page:"+identifier}


def publish(db, raw):
    projection = build_task_projection(raw, tasks=[raw])
    db.upsert_task_projections([projection])
    return db.get_task_version(raw["task_id"])


@pytest.fixture
def api(tmp_path, stopped):
    config = load_cdpa_config(None, repository_root=tmp_path)
    db = RuntimeDB(config.runtime_database)
    db.ensure_schema()
    publish(db, stopped)
    db.put_snapshot("conversation_runtime", {"bindings": {"binding-dev": binding()}})
    return DashboardAPI(config, db=db)


def enqueue(api, payload, *, expected=None, key="maintenance-once"):
    if expected is None:
        expected = api.db.get_task_version("task-stopped")
    return api.enqueue(idempotency_key=key, kind="reconcile_stopped_writer",
                       task_id="task-stopped", payload=payload, expected_task_version=expected)


def test_admission_queues_only_pinned_request_and_preserves_task(api, payload):
    before = api.db.get_task_detail("task-stopped")
    command = enqueue(api, payload)
    assert command["status"] == "queued"
    assert command["kind"] == "reconcile_stopped_writer"
    assert command["payload"] == payload
    assert api.db.get_task_detail("task-stopped") == before
    assert api.db.get_snapshot("conversation_runtime")["payload"]["bindings"]["binding-dev"]["binding_status"] == "blocked"


@pytest.mark.parametrize("extra", ["observations", "proof", "receipt", "conversation_url", "history_tail_verified"])
def test_client_cannot_supply_proof_or_transcript(api, payload, extra):
    with pytest.raises(APIError) as caught:
        enqueue(api, {**payload, extra: {}})
    assert caught.value.status == 400
    assert api.db.get_command_by_idempotency_key("maintenance-once") is None


@pytest.mark.parametrize("expected", [True, False, "1", 1.5, 0, -1])
def test_expected_version_is_required_strict_positive_integer(api, payload, expected):
    with pytest.raises(APIError) as caught:
        enqueue(api, payload, expected=expected)
    assert caught.value.status == 400


def test_missing_expected_version_rejected(api, payload):
    with pytest.raises(APIError) as caught:
        api.enqueue(idempotency_key="missing-version", kind="reconcile_stopped_writer",
                    task_id="task-stopped", payload=payload)
    assert caught.value.status == 400


@pytest.mark.parametrize("field,value", [
    ("binding_id", ""), ("target_id", " "), ("physical_browser_id", None),
    ("stop_control_id", True), ("stop_control_id", "7"), ("stop_control_id", -1),
])
def test_request_requires_exact_nonempty_pins(api, payload, field, value):
    with pytest.raises(APIError) as caught:
        enqueue(api, {**payload, field: value})
    assert caught.value.status == 400


def test_new_key_cannot_borrow_old_expected_version(api, payload, stopped):
    original = enqueue(api, payload)
    previous_version = original["expected_task_version"]
    changed = copy.deepcopy(stopped)
    changed["task_text"] = "Updated stopped task"
    publish(api.db, changed)
    with pytest.raises(APIError) as caught:
        enqueue(api, payload, expected=previous_version, key="another-key")
    assert caught.value.status == 409
    assert api.db.get_command_by_idempotency_key("another-key") is None


def test_exact_idempotent_retry_returns_original_after_release(api, payload, stopped):
    first = enqueue(api, payload)
    changed = copy.deepcopy(stopped)
    changed["cleanup"] = {"state": "STOP_RELEASED", "phase": "writer_released"}
    publish(api.db, changed)
    replay = enqueue(api, payload, expected=first["expected_task_version"])
    assert replay["command_id"] == first["command_id"]
    with pytest.raises(APIError) as caught:
        enqueue(api, {**payload, "target_id": "another-target"}, expected=first["expected_task_version"])
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize("change", ["running", "active_hop", "temporary", "cleanup", "stop_id", "unconfirmed", "foreign_stop"])
def test_admission_requires_current_exact_stopped_hold(api, payload, stopped, change):
    raw = copy.deepcopy(stopped)
    if change == "running": raw["status"] = "RUNNING"
    elif change == "active_hop": raw["active_hop_id"] = 1
    elif change == "temporary": raw["conversation_mode"] = "temporary"
    elif change == "cleanup": raw["cleanup"]["phase"] = "writer_released"
    elif change == "stop_id": raw["cleanup"]["stop_control_id"] = 8
    elif change == "unconfirmed": raw["controls"][0]["confirmed"] = False
    else: raw["controls"][0]["origin"] = "system"
    publish(api.db, raw)
    with pytest.raises(APIError) as caught:
        enqueue(api, payload)
    assert caught.value.status == 409


@pytest.mark.parametrize("field,value", [
    ("task_id", "another-task"), ("binding_status", "sealed"), ("temporary_chat", True),
    ("endpoint_id", "another-endpoint"), ("endpoint_cdp_url", "http://127.0.0.1:9212"),
])
def test_admission_preserves_original_binding_and_endpoint(api, payload, field, value):
    data = binding()
    data[field] = value
    api.db.put_snapshot("conversation_runtime", {"bindings": {"binding-dev": data}})
    with pytest.raises(APIError) as caught:
        enqueue(api, payload)
    assert caught.value.status == 409


def test_http_route_accepts_only_dedicated_maintenance_request(api, payload):
    server = create_server(api.config, host="127.0.0.1", port=0, db=api.db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = request(
            server, "POST", "/api/tasks/task-stopped/writer-maintenance",
            body={**payload, "expected_task_version": api.db.get_task_version("task-stopped")},
            headers={"Idempotency-Key": "http-maintenance"},
        )
        assert status == 202
        saved = api.db.get_command(json.loads(body)["command_id"])
        assert saved["kind"] == "reconcile_stopped_writer"
        assert saved["payload"] == payload
        assert saved["status"] == "queued"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def sealed_evidence(payload, *, role="DEV", identifier="binding-dev", command_id="seal-command"):
    pin = {**payload, "binding_id": identifier}
    command = {"command_id": command_id, "kind": "reconcile_stopped_writer", "task_id": "task-stopped",
               "idempotency_key": command_id, "expected_task_version": 1, "payload": pin, "status": "applied"}
    proof = {"kind": "stopped_controller_writer_release", "immutable_history": True,
             "command_id": command_id, "binding_id": identifier, "task_id": "task-stopped",
             "role_key": role, "stop_control_id": 7, "target_id": pin["target_id"],
             "physical_browser_id": pin["physical_browser_id"], "canonical_conversation_id": "private-conversation",
             "latest_request_id": "private-request", "latest_user_message_id": "private-user",
             "observed_at": 100.5,
             "command_sha256": digest({key: command[key] for key in
                 ("command_id", "kind", "task_id", "idempotency_key", "expected_task_version", "payload")}),
             **{key: "a"*64 for key in ("task_sha256", "binding_sha256", "attempts_sha256", "ledger_sha256", "observations_sha256")}}
    proof["attempts_sha256"] = digest([])
    proof["evidence_sha256"] = digest(proof)
    value = binding(role=role, identifier=identifier)
    value.update(binding_status="sealed", stopped_writer_release=proof)
    command["result"] = {"controller_writer_released": True, "task_id": "task-stopped",
                         "binding_id": identifier, "proof": copy.deepcopy(proof)}
    return {"bindings": [value], "commands_by_id": {command_id: command},
            "leases": {}, "page_claims": {}, "quarantine": {}, "attempts_by_binding": {identifier: []}}


def project(raw, evidence=None):
    return build_task_projection(raw, tasks=[raw], writer_maintenance_evidence=evidence)


def test_pending_projection_does_not_infer_release_from_stopped_or_missing_tabs(stopped):
    raw = copy.deepcopy(stopped)
    projection = project(raw)
    assert projection.detail["writer_maintenance"]["state"] == "pending"
    assert projection.detail["cleanup"] == stopped["cleanup"]
    assert projection.status == "STOPPED"
    assert raw == stopped


def test_exact_committed_seal_reports_controller_release_only(stopped, payload):
    evidence = sealed_evidence(payload)
    before = copy.deepcopy((stopped, evidence))
    projection = project(stopped, evidence)
    maintenance = projection.detail["writer_maintenance"]
    assert maintenance["state"] == "released"
    assert maintenance["released"] is True
    assert maintenance["released_bindings"] == 1
    assert maintenance["required_bindings"] == 1
    assert projection.summary["writer_maintenance"] == maintenance
    assert projection.status == "STOPPED"
    assert projection.detail["cleanup"] == stopped["cleanup"]
    assert (stopped, evidence) == before
    public = json.dumps(maintenance)
    assert "private-user" not in public
    assert "private-request" not in public
    assert "private-conversation" not in public


def test_partial_release_keeps_other_executed_role_pending(stopped, payload):
    stopped["roles"]["PLAN"] = {"physical_role": "alpha-plan", "turn": 1, "conversation_binding_id": "binding-plan"}
    stopped["hops"].append({"hop_id": 2, "target_role": "PLAN", "state": "waiting",
                            "request_id": "other-request", "conversation_binding_id": "binding-plan", "turn": 1})
    evidence = sealed_evidence(payload)
    evidence["bindings"].append(binding(role="PLAN", identifier="binding-plan"))
    evidence["attempts_by_binding"]["binding-plan"] = []
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "partial"
    assert result["released"] is False
    assert result["required_bindings"] == 2
    assert result["released_bindings"] == 1


def test_uncovered_executed_legacy_role_cannot_be_reported_released(stopped, payload):
    stopped["hops"].append({"hop_id": 2, "target_role": "TEST", "state": "routed", "turn": 1})
    result = project(stopped, sealed_evidence(payload)).detail["writer_maintenance"]
    assert result["state"] == "partial"
    assert "TEST" in result["uncovered_roles"]
    assert result["released"] is False


@pytest.mark.parametrize("case", ["status", "missing_proof", "bad_digest", "foreign_command", "payload",
                                 "command_result", "command_not_applied", "proof_binding", "foreign_task"])
def test_uncommitted_or_wrong_seal_cannot_authorize_release(stopped, payload, case):
    evidence = sealed_evidence(payload)
    value = evidence["bindings"][0]
    command = evidence["commands_by_id"]["seal-command"]
    if case == "status": value["binding_status"] = "blocked"
    elif case == "missing_proof": value["stopped_writer_release"] = None
    elif case == "bad_digest": value["stopped_writer_release"]["evidence_sha256"] = "0"*64
    elif case == "foreign_command": evidence["commands_by_id"] = {}
    elif case == "payload": command["payload"]["target_id"] = "different-target"
    elif case == "command_result": command["result"]["controller_writer_released"] = False
    elif case == "command_not_applied": command["status"] = "running"
    elif case == "proof_binding": value["stopped_writer_release"]["binding_id"] = "other-binding"
    else: value["task_id"] = "other-task"
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "pending"
    assert result["released"] is False


def test_unexecuted_configured_role_does_not_invent_writer(stopped, payload):
    stopped["roles"]["AUDIT"] = {"physical_role": "alpha-audit", "turn": 0, "slot_state": "unmaterialized"}
    result = project(stopped, sealed_evidence(payload)).detail["writer_maintenance"]
    assert result["state"] == "released"
    assert "AUDIT" not in result["uncovered_roles"]


def test_normal_running_tasks_do_not_gain_maintenance_controls(stopped, payload):
    stopped["status"] = "RUNNING"
    result = project(stopped, sealed_evidence(payload))
    assert "writer_maintenance" not in result.detail
    assert result.status == "RUNNING"


def test_mixed_existing_owner_releases_and_maintenance_cover_every_role(stopped, payload):
    evidence = sealed_evidence(payload)
    for index, role in enumerate(("PLAN", "TEST"), start=2):
        identifier = "binding-"+role.lower()
        stopped["roles"][role] = {"physical_role": "alpha-"+role.lower(), "turn": 1,
                                   "conversation_binding_id": identifier, "conversation_generation": 1}
        stopped["hops"].append({"hop_id": index, "target_role": role, "state": "routed", "turn": 1,
                                "conversation_binding_id": identifier, "conversation_generation": 1})
        value = binding(role=role, identifier=identifier)
        value["binding_status"] = "sealed"
        evidence["bindings"].append(value)
        evidence["attempts_by_binding"][identifier] = []
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "released"
    assert result["released_bindings"] == 3
    sources = {item["binding_id"]: item["source"] for item in result["bindings"]}
    assert sources["binding-dev"] == "maintenance"
    assert sources["binding-plan"] == sources["binding-test"] == "existing_owner"


@pytest.mark.parametrize("case", ["lease", "page_claim", "attempt", "quarantine", "missing_attempts"])
def test_normal_seal_with_remaining_authority_keeps_hold(stopped, payload, case):
    evidence = sealed_evidence(payload)
    value = evidence["bindings"][0]
    value.pop("stopped_writer_release")
    if case == "lease": evidence["leases"]["arbitrary-key"] = {"owner_binding_id": "binding-dev"}
    elif case == "page_claim": evidence["page_claims"]["arbitrary-key"] = "binding-dev"
    elif case == "attempt": evidence["attempts_by_binding"]["binding-dev"] = [
        {"attempt_id": "late", "owner_binding_id": "binding-dev", "state": "submitted"}]
    elif case == "quarantine": evidence["quarantine"][value["writer_identity_key"]] = {"reason": "collision"}
    else: evidence["attempts_by_binding"].pop("binding-dev")
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "pending"
    assert result["released"] is False


def test_sealed_maintenance_before_command_audit_is_pending_audit(stopped, payload):
    evidence = sealed_evidence(payload)
    evidence["commands_by_id"]["seal-command"]["status"] = "running"
    evidence["commands_by_id"]["seal-command"]["result"] = None
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "pending"
    assert result["pending_audit_bindings"] == 1
    assert result["bindings"][0]["state"] == "pending_audit"
    assert result["released"] is False


def test_earlier_unreleased_generation_cannot_hide_behind_current_seal(stopped, payload):
    evidence = sealed_evidence(payload)
    stopped["hops"].insert(0, {"hop_id": 0, "target_role": "DEV", "state": "routed",
                             "conversation_generation": 0, "conversation_binding_id": "old-binding"})
    evidence["bindings"].append(binding(identifier="old-binding"))
    evidence["attempts_by_binding"]["old-binding"] = []
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["state"] == "partial"
    assert result["released"] is False


@pytest.mark.parametrize("key", ["leases", "page_claims", "quarantine", "attempts_by_binding"])
def test_incomplete_owner_snapshot_is_unknown_not_released(stopped, payload, key):
    evidence = sealed_evidence(payload)
    evidence.pop(key)
    result = project(stopped, evidence).detail["writer_maintenance"]
    assert result["released"] is False


@pytest.mark.parametrize("field", ["task_sha256", "ledger_sha256", "observations_sha256",
                                  "latest_request_id", "latest_user_message_id"])
def test_incomplete_self_consistent_proof_is_not_release_evidence(stopped, payload, field):
    evidence = sealed_evidence(payload)
    proof = evidence["bindings"][0]["stopped_writer_release"]
    proof.pop(field)
    proof.pop("evidence_sha256")
    proof["evidence_sha256"] = digest(proof)
    evidence["commands_by_id"]["seal-command"]["result"]["proof"] = copy.deepcopy(proof)
    assert project(stopped, evidence).detail["writer_maintenance"]["released"] is False


def test_late_accepted_attempt_invalidates_maintenance_proof(stopped, payload):
    evidence = sealed_evidence(payload)
    evidence["attempts_by_binding"]["binding-dev"].append(
        {"attempt_id": "late-accepted", "owner_binding_id": "binding-dev", "state": "accepted",
         "text_hash": "b"*64, "lease_epoch": 1})
    assert project(stopped, evidence).detail["writer_maintenance"]["released"] is False


@pytest.mark.parametrize("case", ["missing_time", "zero_time", "boolean_time", "empty_target", "none_physical", "blank_physical"])
def test_missing_or_invalid_seal_time_and_pins_cannot_claim_release(stopped, payload, case):
    evidence = sealed_evidence(payload)
    proof = evidence["bindings"][0]["stopped_writer_release"]
    command = evidence["commands_by_id"]["seal-command"]
    if case == "missing_time": proof.pop("observed_at")
    elif case == "zero_time": proof["observed_at"] = 0
    elif case == "boolean_time": proof["observed_at"] = True
    elif case == "empty_target": proof["target_id"] = command["payload"]["target_id"] = ""
    elif case == "none_physical": proof["physical_browser_id"] = command["payload"]["physical_browser_id"] = None
    else: proof["physical_browser_id"] = command["payload"]["physical_browser_id"] = " "
    proof["command_sha256"] = digest({key: command[key] for key in
        ("command_id", "kind", "task_id", "idempotency_key", "expected_task_version", "payload")})
    proof.pop("evidence_sha256")
    proof["evidence_sha256"] = digest(proof)
    command["result"]["proof"] = copy.deepcopy(proof)
    assert project(stopped, evidence).detail["writer_maintenance"]["released"] is False


@pytest.mark.parametrize("key_name", ["writer_identity_key", "provisional_writer_identity_key"])
@pytest.mark.parametrize("lease", [None, {}, {"owner_binding_id": None}, {"owner_binding_id": ""}, "invalid"])
def test_malformed_lease_at_writer_key_keeps_authority_unknown(stopped, payload, key_name, lease):
    evidence = sealed_evidence(payload)
    key = evidence["bindings"][0][key_name]
    evidence["leases"][key] = lease
    assert project(stopped, evidence).detail["writer_maintenance"]["released"] is False


def test_foreign_valid_lease_does_not_restore_old_authority(stopped, payload):
    evidence = sealed_evidence(payload)
    key = evidence["bindings"][0]["writer_identity_key"]
    evidence["leases"][key] = {"owner_binding_id": "another-binding"}
    assert project(stopped, evidence).detail["writer_maintenance"]["released"] is True
