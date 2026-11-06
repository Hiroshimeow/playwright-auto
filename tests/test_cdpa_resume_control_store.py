"""Regression: exact-team Resume must persist its control without mutating the
manifest's loaded baseline through a shallow copy."""
from test_cdpa_worker import setup_task


def test_exact_team_resume_has_independent_control_delta_before_persistence(tmp_path, monkeypatch):
    _config, store, created, _worker = setup_task(
        tmp_path, task_id="task-persist-real-role-pause"
    )
    def pause(current):
        current["status"] = "PAUSED"
        current["active_action"] = "paused"
        current["kanban_column"] = "PAUSED"
        current["pause_reason"] = "route PAUSE"
        return current

    paused = store.update(created["manifest_path"], pause)
    captured = []
    original = store._persist_existing_unlocked

    def assert_delta(target, baseline, candidate):
        assert baseline is not candidate
        # If mutable children alias the original, the equality guard considers
        # the Resume a no-op and discards the queued control on disk.
        assert baseline.get("controls") is not candidate.get("controls")
        assert baseline.get("applied_command_ids") is not candidate.get("applied_command_ids")
        assert all(c.get("action") != "resume" for c in baseline.get("controls") or [])
        assert any(c.get("action") == "resume" for c in candidate.get("controls") or [])
        assert "resume-persist-once" not in (baseline.get("applied_command_ids") or [])
        captured.append(True)
        return original(target, baseline, candidate)

    monkeypatch.setattr(store, "_persist_existing_unlocked", assert_delta)
    store.resume_team(paused["team"], external_command_id="resume-persist-once")
    assert captured == [True]
    durable = store.load(paused["manifest_path"])
    assert len(durable["controls"]) == 1
    assert durable["controls"][0]["action"] == "resume"
    assert durable["controls"][0]["status"] == "requested"
    assert durable["controls"][0]["external_command_id"] == "resume-persist-once"
