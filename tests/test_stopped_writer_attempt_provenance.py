from __future__ import annotations
import pytest
from playwright_auto.cdpa_conversations import ConversationLeaseError
from test_stopped_writer_proof import evidence
from test_stopped_writer_seal_owner import owned, enqueue, seal

@pytest.mark.parametrize("operation", ["seal", "replay", "projection"])
@pytest.mark.parametrize("change", ["command_id", "task_id", "nonterminal_command"])
def test_browser_send_row_identity_cannot_be_hidden_by_unchanged_attempt_payload(owned, operation, change):
    owner, evidence, _, _ = owned
    command_id = enqueue(owner, evidence)
    if operation != "seal":
        seal(owner, evidence, command_id)
    attempt_id = evidence["task"]["hops"][-1]["send_attempt_id"]
    with owner.db.connection() as connection:
        if change == "command_id":
            connection.execute("UPDATE command_queue SET command_id=? WHERE command_id=?",
                               ("different-command-primary-key", attempt_id))
        elif change == "task_id":
            connection.execute("UPDATE command_queue SET task_id=? WHERE command_id=?",
                               ("another-task", attempt_id))
        else:
            connection.execute("UPDATE command_queue SET status=? WHERE command_id=?",
                               ("recovery_required", attempt_id))
    before = owner.db.get_snapshot("conversation_runtime")
    with pytest.raises(ConversationLeaseError):
        if operation == "seal":
            seal(owner, evidence, command_id)
        elif operation == "replay":
            owner.replay_stopped_writer_release(command_id)
        else:
            owner.stopped_writer_maintenance_evidence()
    assert owner.db.get_snapshot("conversation_runtime") == before
