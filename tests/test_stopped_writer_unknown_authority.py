from __future__ import annotations

import copy

import pytest

from playwright_auto.cdpa_conversations import ConversationLeaseError
from test_stopped_writer_proof import evidence
from test_stopped_writer_seal_owner import owned, enqueue, seal

@pytest.mark.parametrize("operation", ["seal", "replay"])
def test_own_canonical_lease_with_unknown_owner_must_hold(owned, operation):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    if operation == "replay":
        seal(owner, evidence, command_id)
    state = owner.db.get_snapshot("conversation_runtime")["payload"]
    binding = state["bindings"]["binding-old"]
    state["leases"][binding["writer_identity_key"]] = {}
    owner.db.put_snapshot("conversation_runtime", state)
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        if operation == "seal":
            seal(owner, evidence, command_id)
        else:
            owner.replay_stopped_writer_release(command_id)
    assert owner.db.get_snapshot("conversation_runtime") == before

@pytest.mark.parametrize("operation", ["seal", "replay", "projection"])
def test_unknown_lease_container_must_not_be_normalized_to_release_evidence(owned, operation):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    if operation in {"replay", "projection"}:
        seal(owner, evidence, command_id)
    state = owner.db.get_snapshot("conversation_runtime")["payload"]
    state["leases"] = None
    owner.db.put_snapshot("conversation_runtime", state)
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        if operation == "seal":
            seal(owner, evidence, command_id)
        elif operation == "replay":
            owner.replay_stopped_writer_release(command_id)
        else:
            owner.stopped_writer_maintenance_evidence()
    assert owner.db.get_snapshot("conversation_runtime") == before
