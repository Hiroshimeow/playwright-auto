from __future__ import annotations

import copy
import hashlib

import pytest

from playwright_auto import cdpa_conversations as conversations
from native_history_tail_fixture import native_tail_for


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def evidence():
    prompt = "Approved work for DEV. Preserve the saved report."
    prior = "Start the approved task."
    binding = {
        "binding_id": "binding-old", "task_id": "task-stopped", "role_key": "DEV",
        "binding_status": "active", "provider_id": "chatgpt_web", "lease_epoch": 1,
        "temporary_chat": False, "canonical_conversation_id": "saved-chat",
        "conversation_url": "https://chatgpt.com/c/saved-chat",
        "endpoint_id": "endpoint-original", "endpoint_cdp_url": "http://127.0.0.1:9202",
        "physical_browser_id": "devtools:old-browser", "page_target_identity": "old-logical-page",
        "writer_identity_key": "conversation:chatgpt_web:saved-chat",
        "provisional_writer_identity_key": "page:chatgpt_web:old-browser:old-page-binding",
    }
    page_binding = {"page_id": "old-logical-page", "role": "historical-team-dev"}
    role = {
        "conversation_binding_id": "binding-old", "page_id": "old-logical-page",
        "physical_role": "historical-team-dev", "browser_endpoint_id": "endpoint-original",
        "conversation_generation": 1, "page_url": "https://chatgpt.com/c/saved-chat",
    }
    records, hops, attempts = {}, [], []
    for number, text in enumerate((prior, prompt), start=1):
        request_id = f"task-stopped-hop{number}"
        receipt = {
            "user_message_id": f"accepted-user-{number}", "prompt": text,
            "prompt_sha256": _hash(text), "binding": dict(page_binding),
            "session_id_before": "saved-chat", "conversation_id": None,
        }
        records[request_id] = {
            "request_id": request_id, "role": role["physical_role"], "status": "sent",
            "binding": dict(page_binding), "prompt": text, "rendered_prompt": text, "receipt": copy.deepcopy(receipt),
            "source_context": {"task_id": "task-stopped", "team": "historical-team", "hop_id": number},
            "response": None,
        }
        hops.append({
            "hop_id": number, "target_role": "DEV", "physical_role": role["physical_role"],
            "conversation_binding_id": "binding-old", "conversation_generation": 1,
            "conversation_url": "https://chatgpt.com/c/saved-chat", "request_id": request_id,
            "send_attempt_id": f"send-{number}", "state": "waiting" if number == 2 else "routed",
            "receipt": copy.deepcopy(receipt), "prompt": text,
            "prompt_sha256": _hash(text), "rendered_prompt_sha256": _hash(text),
        })
        attempts.append({
            "attempt_id": f"send-{number}", "owner_binding_id": "binding-old",
            "state": "accepted", "text_hash": _hash(text), "lease_epoch": 1,
        })
    stop = {
        "control_id": 7, "action": "stop", "status": "applied", "command_state": "APPLIED",
        "confirmed": True, "origin": "operator", "external_command_id": "original-stop-command",
        "command": {"task_id": "task-stopped"}, "result": {"writer_cleanup_pending": True},
        "role": None, "reason": "Preserve evidence; release only after verified Stop.",
    }
    payload = {
        "binding_id": "binding-old", "stop_control_id": 7, "endpoint_id": "endpoint-original",
        "endpoint_cdp_url": "http://127.0.0.1:9202", "physical_browser_id": "devtools:current-browser",
        "target_id": "existing-diagnostic-target",
    }
    snapshot = {
        "url": "https://chatgpt.com/c/saved-chat", "session_id": "saved-chat",
        "page_id": None, "page_role": None, "page_task_id": None, "page_team": None,
        "state": "waiting_prompt", "requires_login": False,
        "composer_present": True, "composer_editable": True, "composer_ambiguous": False,
        "composer_text": "", "attachment_markers": [], "blocking_dialogs": [], "error_texts": [],
        "choice_prompt_labels": [], "retry_visible": False, "mcp_provider_risk": False,
        "connection_interrupted_visible": False, "mcp_permission_allow_count": 0,
        "mcp_permission_node_count": 0, "stop_visible": False,
        "response_activity_turn_id": None, "response_projection_active": False,
        "messages": [
            {"role": "user", "message_id": "accepted-user-1", "text": prior},
            {"role": "assistant", "message_id": "assistant-prior", "text": "Saved earlier report."},
            {"role": "user", "message_id": "accepted-user-2", "text": prompt},
        ],
    }
    observations = [
        {
            **{key: payload[key] for key in ("endpoint_id", "endpoint_cdp_url", "physical_browser_id", "target_id")},
            "document_ready_state": "complete", "provider_challenge": False, "storage_readable": True,
            "history_tail": native_tail_for(snapshot, observed_at=timestamp),
            "observed_at": timestamp, "snapshot": copy.deepcopy(snapshot),
        }
        for timestamp in (100.0, 100.5)
    ]
    return {
        "command": {"command_id": "maintenance-command", "kind": "reconcile_stopped_writer",
                    "status": "running", "idempotency_key": "maintenance-once", "task_id": "task-stopped",
                    "expected_task_version": 12, "payload": payload},
        "task": {"task_id": "task-stopped", "team": "historical-team", "status": "STOPPED",
                 "active_hop_id": None, "conversation_mode": "persistent", "roles": {"DEV": role},
                 "hops": hops, "controls": [stop],
                 "cleanup": {"state": "CLEARING", "phase": "writer_stop_pending", "stop_control_id": 7}},
        "task_version": 12, "immutable_task": True,
        "runtime_state": {
            "bindings": {"binding-old": binding},
            "leases": {key: {"owner_binding_id": "binding-old"} for key in
                       (binding["writer_identity_key"], binding["provisional_writer_identity_key"])},
            "page_claims": {}, "quarantine": {},
        },
        "attempts": attempts, "request_records": records, "observations": observations, "now_epoch": 100.6,
    }


def _validate(evidence):
    validator = getattr(conversations, "validate_stopped_writer_proof", None)
    assert callable(validator), "R5 has no proof-validated stopped historical writer maintenance"
    return validator(**evidence)


def _set(data, path, value):
    cursor = data
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value


def test_accepted_waiting_turn_can_prove_only_controller_release_without_mutation(evidence):
    before = copy.deepcopy(evidence)
    proof = _validate(evidence)
    assert proof["kind"] == "stopped_controller_writer_release"
    assert proof["latest_user_message_id"] == "accepted-user-2"
    assert proof["binding_id"] == "binding-old"
    assert len(proof["evidence_sha256"]) == 64
    assert "prompt" not in proof and "messages" not in proof and "response" not in proof
    assert evidence == before
    assert evidence["runtime_state"]["bindings"]["binding-old"]["binding_status"] == "active"
    assert evidence["task"]["hops"][-1]["state"] == "waiting"
    assert all(record["response"] is None for record in evidence["request_records"].values())
    assert _validate(evidence) == proof


@pytest.mark.parametrize("field,value", [
    ("page_id", "foreign-page"), ("page_role", "other-role"),
    ("page_task_id", "other-task"), ("page_team", "other-team"),
])
def test_foreign_or_partial_target_owner_is_never_adopted(evidence, field, value):
    evidence["observations"][1]["snapshot"][field] = value
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("path,value", [
    (("immutable_task",), None),
    (("task", "status"), "RUNNING"),
    (("task", "active_hop_id"), 3),
    (("task", "conversation_mode"), "temporary"),
    (("command", "kind"), "direct_chat_close"),
    (("command", "status"), "queued"),
    (("command", "idempotency_key"), ""),
    (("command", "expected_task_version"), 13),
    (("command", "expected_task_version"), None),
    (("command", "task_id"), "other-task"),
    (("command", "payload", "stop_control_id"), 8),
    (("task", "controls", 0, "confirmed"), False),
    (("task", "controls", 0, "origin"), "system"),
    (("task", "controls", 0, "status"), "pending"),
    (("task", "controls", 0, "command", "task_id"), "other-task"),
    (("task", "controls", 0, "result", "writer_cleanup_pending"), False),
    (("runtime_state", "bindings", "binding-old", "task_id"), "other-task"),
    (("runtime_state", "bindings", "binding-old", "binding_status"), "sealed"),
    (("runtime_state", "bindings", "binding-old", "temporary_chat"), True),
    (("runtime_state", "bindings", "binding-old", "pending_identity"), {"target": "changed"}),
    (("command", "payload", "endpoint_id"), "another-profile"),
    (("command", "payload", "endpoint_cdp_url"), "http://127.0.0.1:9212"),
    (("task", "roles", "DEV", "conversation_binding_id"), "other-binding"),
    (("task", "roles", "DEV", "page_id"), "other-page"),
    (("task", "hops", 1, "conversation_generation"), 2),
    (("task", "hops", 1, "conversation_url"), "https://chatgpt.com/c/other-chat"),
    (("task", "hops", 1, "send_attempt_id"), "unrecorded-attempt"),
    (("attempts", 0, "state"), "unconfirmed"),
    (("attempts", 0, "state"), "submitted"),
    (("attempts", 0, "state"), "intent_recorded"),
    (("attempts", 0, "owner_binding_id"), "another-binding"),
    (("attempts", 1, "state"), "reconciled_absent"),
    (("attempts", 0, "state"), "reconciled_absent"),
    (("attempts", 1, "text_hash"), "changed"),
    (("attempts", 1, "lease_epoch"), 2),
    (("runtime_state", "bindings", "binding-old", "lease_epoch"), None),
    (("runtime_state", "bindings", "binding-old", "writer_identity_key"), "conversation:chatgpt_web:other-chat"),
    (("request_records", "task-stopped-hop2", "status"), "sending"),
    (("request_records", "task-stopped-hop2", "source_context", "task_id"), "other-task"),
    (("request_records", "task-stopped-hop2", "receipt", "user_message_id"), None),
    (("request_records", "task-stopped-hop2", "rendered_prompt"), "changed prompt"),
])
def test_authority_or_saved_provenance_mismatch_keeps_hold(evidence, path, value):
    _set(evidence, path, value)
    before = copy.deepcopy(evidence)
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)
    assert evidence == before


@pytest.mark.parametrize("field,value", [
    ("target_id", "different-target"), ("physical_browser_id", "devtools:changed"),
    ("endpoint_id", "another-profile"), ("document_ready_state", "loading"),
    ("storage_readable", False), ("storage_readable", None),
    ("provider_challenge", True), ("provider_challenge", None),
    ("history_tail", False), ("history_tail", None),
])
def test_unknown_tail_or_changed_diagnostic_target_keeps_hold(evidence, field, value):
    evidence["observations"][1][field] = value
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("field,value", [
    ("state", "unknown"), ("requires_login", True), ("composer_present", False),
    ("composer_editable", False), ("composer_ambiguous", True), ("composer_text", "User draft"),
    ("attachment_markers", ["manual-file.txt"]), ("blocking_dialogs", ["dialog"]),
    ("error_texts", ["failed"]), ("choice_prompt_labels", ["continue"]),
    ("retry_visible", True), ("mcp_provider_risk", True), ("connection_interrupted_visible", True),
    ("mcp_permission_allow_count", 1), ("mcp_permission_node_count", 1),
    ("stop_visible", True), ("stop_visible", None),
    ("response_activity_turn_id", "still-active"),
    ("response_projection_active", True), ("response_projection_active", None),
    ("url", "https://chatgpt.com/c/other-chat"), ("session_id", "other-chat"),
])
def test_readiness_and_all_three_positive_idle_signals_are_required(evidence, field, value):
    evidence["observations"][1]["snapshot"][field] = value
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("field", [
    "stop_visible", "response_activity_turn_id", "response_projection_active",
    "composer_text", "attachment_markers", "messages",
])
def test_missing_observations_are_not_defaults_for_idle(evidence, field):
    del evidence["observations"][1]["snapshot"][field]
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_stable_old_window_without_tail_evidence_cannot_release(evidence):
    for observation in evidence["observations"]:
        del observation["history_tail"]
    with pytest.raises(conversations.ConversationLeaseError, match="history tail"):
        _validate(evidence)


@pytest.mark.parametrize("position", [0, 3])
def test_any_observed_user_id_outside_ledger_blocks_even_before_latest(evidence, position):
    for observation in evidence["observations"]:
        observation["snapshot"]["messages"].insert(position, {
            "role": "user", "message_id": "unaccounted-user", "text": "Manual message",
        })
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("case", ["text", "missing_latest", "duplicate", "old_user_last", "changed_assistant"])
def test_transcript_identity_order_and_stability_are_preserved(evidence, case):
    messages = evidence["observations"][1]["snapshot"]["messages"]
    if case == "text":
        messages[-1]["text"] = "Edited accepted user text"
    elif case == "missing_latest":
        messages.pop()
    elif case == "duplicate":
        messages.append(copy.deepcopy(messages[-1]))
    elif case == "old_user_last":
        messages[0], messages[-1] = messages[-1], messages[0]
    else:
        messages[1]["text"] = "Assistant is still changing"
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("case", ["stale", "same_read", "future", "one_read"])
def test_stale_or_reused_observation_cannot_be_sealed(evidence, case):
    if case == "stale":
        evidence["now_epoch"] = 106
    elif case == "same_read":
        evidence["observations"][1]["observed_at"] = 100
    elif case == "future":
        evidence["now_epoch"] = 100.4
    else:
        evidence["observations"].pop()
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("case", ["foreign_lease", "quarantine", "foreign_page_claim"])
def test_runtime_owner_conflicts_keep_hold(evidence, case):
    state = evidence["runtime_state"]
    key = state["bindings"]["binding-old"]["writer_identity_key"]
    if case == "foreign_lease":
        state["leases"][key]["owner_binding_id"] = "another-binding"
    elif case == "quarantine":
        state["quarantine"][key] = {"reason": "collision"}
    else:
        for observation in evidence["observations"]:
            observation["snapshot"].update(page_id="old-logical-page", page_role="historical-team-dev",
                                           page_task_id="task-stopped", page_team="historical-team")
            observation["history_tail"] = native_tail_for(observation["snapshot"])
        state["page_claims"]["chatgpt_web:devtools:current-browser:old-logical-page"] = "another-binding"
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_original_exact_owner_and_project_url_can_supply_diagnostic_evidence(evidence):
    for observation in evidence["observations"]:
        observation["snapshot"].update(
            url="https://chatgpt.com/g/g-p-project/c/saved-chat",
            page_id="old-logical-page", page_role="historical-team-dev",
            page_task_id="task-stopped", page_team="historical-team",
        )
        observation["history_tail"] = native_tail_for(observation["snapshot"])
    assert _validate(evidence)["canonical_conversation_id"] == "saved-chat"


def test_existing_rendered_text_comparator_is_used_only_for_accepted_bubbles(evidence):
    for observation in evidence["observations"]:
        observation["snapshot"]["messages"][-1]["text"] = (
            "Approved work for DEV.  Preserve the saved report.\nShow more"
        )
    assert _validate(evidence)["latest_user_message_id"] == "accepted-user-2"


def test_command_and_observation_digest_bind_the_exact_seal_request(evidence):
    original = _validate(evidence)
    evidence["command"]["command_id"] = "another-maintenance-command"
    changed = _validate(evidence)
    assert original["command_sha256"] != changed["command_sha256"]
    assert original["evidence_sha256"] != changed["evidence_sha256"]
    assert original["binding_sha256"] == changed["binding_sha256"]
    evidence["observations"][1]["observed_at"] = 100.55
    observed = _validate(evidence)
    assert changed["observations_sha256"] != observed["observations_sha256"]


def test_a_later_role_execution_without_accepted_record_is_not_hidden(evidence):
    next_hop = copy.deepcopy(evidence["task"]["hops"][-1])
    next_hop.update(hop_id=3, request_id="task-stopped-hop3", send_attempt_id="send-3", receipt=None)
    evidence["task"]["hops"].append(next_hop)
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_newly_stopped_task_can_release_before_replacement_without_becoming_immutable(evidence):
    evidence["immutable_task"] = False
    before = copy.deepcopy(evidence)
    proof = _validate(evidence)
    assert proof["immutable_history"] is False
    assert proof["kind"] == "stopped_controller_writer_release"
    assert evidence == before
    # Only the existing Stop cleanup owner may later write STOP_RELEASED.
    assert evidence["task"]["cleanup"]["phase"] == "writer_stop_pending"


def test_accepted_attempt_without_saved_ledger_provenance_remains_held(evidence):
    evidence["attempts"].append({
        "attempt_id": "accepted-after-last-ledger-save",
        "owner_binding_id": "binding-old", "state": "accepted",
        "text_hash": "new-unaccounted-prompt", "lease_epoch": 1,
    })
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)

def test_earlier_accepted_attempt_cannot_disagree_with_its_ledger(evidence):
    evidence["attempts"][0]["text_hash"] = "different-from-accepted-receipt"
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_missing_earlier_accepted_attempt_record_keeps_hold(evidence):
    evidence["attempts"].pop(0)
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_terminal_reconciled_absent_attempt_needs_no_accepted_user_record(evidence):
    evidence["attempts"].append({
        "attempt_id": "proved-never-accepted", "owner_binding_id": "binding-old",
        "state": "reconciled_absent", "text_hash": "not-accepted", "lease_epoch": 1,
    })
    assert _validate(evidence)["latest_user_message_id"] == "accepted-user-2"


@pytest.mark.parametrize("raw_format", ["\r\n{prompt} \r\n", "{prompt}\t\r\n\r\n"])
def test_durable_normalization_keeps_both_raw_attempt_and_rendered_receipt_provenance(evidence, raw_format):
    hop = evidence["task"]["hops"][0]
    raw = raw_format.format(prompt=hop["prompt"])
    hop.update(prompt=raw, prompt_sha256=_hash(raw))
    evidence["attempts"][0]["text_hash"] = _hash(raw)
    assert _validate(evidence)["latest_user_message_id"] == "accepted-user-2"


def test_hop_raw_prompt_tamper_cannot_hide_behind_unchanged_accepted_receipt(evidence):
    evidence["task"]["hops"][0]["prompt"] = "Different raw task content"
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


def test_hop_raw_content_change_is_not_durable_whitespace_normalization(evidence):
    hop = evidence["task"]["hops"][0]
    hop["prompt"] = "Changed the approved task."
    hop["prompt_sha256"] = _hash(hop["prompt"])
    evidence["attempts"][0]["text_hash"] = hop["prompt_sha256"]
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)


@pytest.mark.parametrize("failure", ["missing", "tail_loader", "older_window"])
def test_caller_boolean_cannot_substitute_for_native_tail_evidence(evidence, failure):
    from native_history_tail_fixture import native_tail_for
    for observation in evidence["observations"]:
        tail = native_tail_for(observation["snapshot"])
        if failure == "missing":
            tail = None
        elif failure == "tail_loader":
            tail["loaders"] = [{"text": "Loading", "exact_older_label": False}]
        else:
            tail["reading"] = "unmeasured"
        observation["history_tail"] = tail
        observation["history_tail_verified"] = True
    with pytest.raises(conversations.ConversationLeaseError, match="history tail"):
        _validate(evidence)

@pytest.mark.parametrize("timestamp", [0, -1, float("inf"), float("nan")])
def test_observation_timestamp_must_be_positive_finite(evidence, timestamp):
    evidence["observations"][0]["observed_at"] = timestamp
    with pytest.raises(conversations.ConversationLeaseError):
        _validate(evidence)
