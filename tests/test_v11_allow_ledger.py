"""V11 irreversible Allow actions share the existing durable action ledger."""
import pytest

from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.role_runtime import Action, RoleController
from test_operational_contract import snapshot
from test_spec003_phase15_runtime import _ObservedClient, _fake_environment, _run


def _permission(surface):
    return snapshot(mcp_permission_allow_count=1, mcp_permission_node_count=1,
                    mcp_permission_surface_key=surface)


def test_allow_intent_committed_before_click_and_no_duplicate_after_restart(tmp_path):
    db = RuntimeDB(tmp_path / "v11.sqlite")
    db.ensure_schema()

    class Client(_ObservedClient):
        async def auto_allow_mcp_permission(self, **kwargs):
            with db.connection() as connection:
                rows = connection.execute("SELECT status,kind FROM command_queue WHERE kind='mcp_allow'").fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "recovery_required"
            self.allow_calls.append((kwargs["expected_task_id"], kwargs["expected_team"]))
            raise SystemExit("crash after possible browser click")

    client = Client()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    with pytest.raises(SystemExit, match="crash"):
        _run(RoleController(), worker, state, hop, acquired, _permission("surface-1"))
    assert len(client.allow_calls) == 1

    recovered_client = _ObservedClient()
    recovered_state, recovered_hop, recovered_worker, recovered_acquired, _ = _fake_environment(
        recovered_client, runtime_db=db
    )
    result = _run(RoleController(), recovered_worker, recovered_state,
                  recovered_hop, recovered_acquired, _permission("surface-1"))
    assert (result.action, result.reason) == (Action.WAIT, "mcp_allow_reconciliation_required")
    assert recovered_client.allow_calls == []


def test_two_distinct_dom_permission_surfaces_in_one_turn_get_two_intents(tmp_path):
    db = RuntimeDB(tmp_path / "v11.sqlite")
    db.ensure_schema()
    client = _ObservedClient()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    assert _run(RoleController(), worker, state, hop, acquired, _permission("first-call")).action is Action.ALLOW
    assert _run(RoleController(), worker, state, hop, acquired, _permission("second-call")).action is Action.ALLOW
    assert client.allow_calls == [("task-1", "team-1"), ("task-1", "team-1")]
    with db.connection() as conn:
        rows = conn.execute("SELECT status FROM command_queue WHERE kind='mcp_allow'").fetchall()
    assert [row["status"] for row in rows] == ["applied", "applied"]


def test_allow_pending_surface_has_typed_reason_and_never_click_loops(tmp_path):
    db = RuntimeDB(tmp_path / "v11.sqlite")
    db.ensure_schema()
    client = _ObservedClient()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    assert _run(RoleController(), worker, state, hop, acquired, _permission("same-call")).action is Action.ALLOW
    second = _run(RoleController(), worker, state, hop, acquired, _permission("same-call"))
    assert second.reason == "mcp_allow_pending"
    assert len(client.allow_calls) == 1


def test_provider_risk_warning_blocks_allow_without_reserving_or_clicking(tmp_path):
    db = RuntimeDB(tmp_path / "v11.sqlite")
    db.ensure_schema()
    client = _ObservedClient()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    current = snapshot(
        mcp_permission_node_count=1, mcp_permission_allow_count=1,
        mcp_permission_surface_key="tool-call-1", mcp_provider_risk=True,
        error_texts=("Suspicious Instruction",),
    )
    result = _run(RoleController(), worker, state, hop, acquired, current)
    assert (result.action, result.reason) == (Action.BLOCK, "permission_review")
    assert client.allow_calls == []
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM command_queue WHERE kind='mcp_allow'").fetchone()[0] == 0


def test_two_simultaneous_permission_surfaces_require_operator_instead_of_ambiguous_click():
    current = snapshot(mcp_permission_allow_count=2, mcp_permission_node_count=2)
    result = RoleController().decide(current, {}, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert (result.action, result.reason) == (Action.BLOCK, "permission_surface_ambiguous")


def test_presend_does_not_click_permission_belonging_to_an_unbound_old_turn():
    current = _permission("old-handoff-call")
    decision = RoleController().decide(
        current, {}, phase="pre_send",
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    assert (decision.action, decision.reason) == (Action.BLOCK, "pre_send_permission_unowned")


def test_missing_dom_surface_identity_never_reserves_an_allow_intent(tmp_path):
    db = RuntimeDB(tmp_path / "v11.sqlite")
    db.ensure_schema()
    client = _ObservedClient()
    state, hop, worker, acquired, _ = _fake_environment(client, runtime_db=db)
    current = snapshot(mcp_permission_node_count=1, mcp_permission_allow_count=1)
    result = _run(RoleController(), worker, state, hop, acquired, current)
    assert (result.action, result.reason) == (Action.BLOCK, "permission_surface_unidentified")
    assert client.allow_calls == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_queue WHERE kind='mcp_allow'").fetchone()[0] == 0
