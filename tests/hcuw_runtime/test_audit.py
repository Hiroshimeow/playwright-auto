from __future__ import annotations

from playwright_auto.hcuw_runtime.audit import AuditTimeline


def test_audit_snapshot_starts_with_observation_sources() -> None:
    timeline = AuditTimeline()
    snapshot = timeline.snapshot()
    sources = {item["source"]: item for item in snapshot["sources"]}

    assert sources["dom"]["status"] == "inactive"
    assert sources["mutation"]["status"] == "inactive"
    assert sources["listen"]["status"] == "inactive"
    assert sources["actions"]["status"] == "active"
    assert snapshot["events"] == []


def test_audit_records_action_events_and_updates_source_state() -> None:
    timeline = AuditTimeline()
    event = timeline.record_event(
        source="actions",
        kind="api_request",
        message="GET /api/health",
        payload={"path": "/api/health"},
    )

    snapshot = timeline.snapshot()
    sources = {item["source"]: item for item in snapshot["sources"]}

    assert event.event_id == 1
    assert snapshot["events"][0]["message"] == "GET /api/health"
    assert sources["actions"]["last_event_id"] == 1
    assert sources["actions"]["payload"] == {"path": "/api/health"}


def test_audit_events_since_returns_incremental_items() -> None:
    timeline = AuditTimeline()
    timeline.record_event(source="actions", kind="first", message="first")
    second = timeline.record_event(source="actions", kind="second", message="second")

    payload = timeline.events_since(1)

    assert payload["items"] == [second.to_payload()]


def test_legacy_dom_source_separates_runtime_v2_tasks_from_old_blocks(tmp_path, monkeypatch) -> None:
    from playwright_auto.hcuw_runtime.config import load_runtime_config
    from playwright_auto.hcuw_runtime.legacy_audit import LegacyObservationAudit

    board = {
        "items": [
            {"task_id": "old", "status": "BLOCKED", "block_code": "accepted_conversation_identity_unresolved",
             "roles": [{"logical_role": "PLAN", "page_url": "https://chatgpt.com/c/local-chatgpt%3Aab"}]},
            {"task_id": "new", "status": "RUNNING", "updated_at": "t9",
             "roles": [{"logical_role": "DEV", "page_url": "https://chatgpt.com/c/6abc-1"},
                       {"logical_role": "REVIEW", "page_url": "https://chatgpt.com/"}]},
            {"task_id": "done", "status": "DONE", "roles": []},
        ]
    }
    audit = LegacyObservationAudit(load_runtime_config(repository_root=tmp_path))
    monkeypatch.setattr(audit, "_read_legacy_board", lambda: board)
    timeline = AuditTimeline()

    audit.refresh(timeline, frozenset({"new"}))
    dom = {item["source"]: item for item in timeline.snapshot()["sources"]}["dom"]
    tasks = dom["payload"]["active_tasks"]

    assert "2 active task(s), 1 blocked" in dom["message"]
    assert "runtime-v2 tasks: 1 active, 0 blocked" in dom["message"]
    assert [(item["task_id"], item["runtime_v2"]) for item in tasks] == [("new", True), ("old", False)]
    assert [role["page_url_kind"] for role in tasks[0]["roles"]] == ["canonical", "root"]
    assert tasks[1]["roles"][0]["page_url_kind"] == "provisional"
    assert set(audit.task_summaries()) == {"old", "new", "done"}
