from __future__ import annotations

from types import SimpleNamespace

from playwright_auto.role_runtime.model import Action, Decision
from playwright_auto.role_runtime.signals import (
    RoleSignals,
    SignalIdentity,
    record_cycle,
    record_signal_error,
    transcript_messages,
)


def _snapshot(**overrides):
    values = {
        "url": "https://chatgpt.com/c/conv-1",
        "page_id": "page-1",
        "page_role": "DEV",
        "page_task_id": "task-1",
        "page_team": "team-1",
        "state": SimpleNamespace(value="waiting_prompt"),
        "requires_login": False,
        "composer_present": True,
        "composer_editable": True,
        "composer_text": "",
        "attachment_markers": (),
        "stop_visible": False,
        "retry_visible": False,
        "blocking_dialogs": (),
        "error_texts": (),
        "choice_prompt_labels": (),
        "mcp_permission_node_count": 0,
        "mcp_permission_allow_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_role_signals_normalizes_dom_and_listen_evidence():
    frame = RoleSignals.from_sources(
        _snapshot(stop_visible=True, retry_visible=True),
        {
            "status": "IS_STREAMING",
            "response": {
                "id": "assistant-1",
                "turn_id": "turn-1",
                "text": "answer",
            },
            "permission_action": {
                "target_message_id": "tool-1",
                "remember_answer": True,
            },
            "diagnostics": {"network_chunk": 3},
            "listener_attached": True,
            "streaming_supported": True,
        },
    )

    assert frame.transport_active is True
    assert frame.permission_present is True
    assert frame.network_status == "IS_STREAMING"
    assert frame.network_response["id"] == "assistant-1"
    assert frame.listen_values() == {
        "status": "IS_STREAMING",
        "response_message_id": "assistant-1",
        "response_turn_id": "turn-1",
        "response_text_length": 6,
        "permission_target": "tool-1",
    }
    values = frame.debug_values()
    assert values["dom_stop_visible"] is True
    assert values["dom_retry_visible"] is True
    assert values["listen_status"] == "IS_STREAMING"
    assert values["listener_attached"] is True
    assert values["streaming_supported"] is True


def test_signal_trace_records_network_transcript_and_decision_once(monkeypatch):
    events = []
    monkeypatch.setattr(
        "playwright_auto.role_runtime.signals.append_live_event",
        lambda source, kind, **kwargs: events.append((source, kind, kwargs)),
    )
    wait = {
        "bound_assistant_message_id": "assistant-1",
        "bound_assistant_turn_id": "turn-1",
    }
    frame = RoleSignals.from_sources(
        _snapshot(),
        {
            "status": "COMPLETE",
            "response": {
                "id": "assistant-1",
                "turn_id": "turn-1",
                "text": "done",
            },
            "permission_action": None,
            "diagnostics": {},
            "listener_attached": True,
            "streaming_supported": True,
        },
    )
    identity = SignalIdentity.from_snapshot(frame.snapshot, request_id="request-1")
    decision = Decision(Action.ACCEPT, "valid_result")

    previous = (None, None)
    record_cycle(
        frame, wait, identity=identity, phase="waiting",
        previous_decision=previous, decision=decision,
    )
    record_cycle(
        frame, wait, identity=identity, phase="waiting",
        previous_decision=(Action.ACCEPT.value, "valid_result"), decision=decision,
    )

    listen = [event for event in events if event[0] == "LISTEN"]
    transcript = [event for event in events if event[0] == "TRANSCRIPT"]
    ctrl = [event for event in events if event[0] == "CTRL"]
    assert len(listen) == 1
    assert len(transcript) == 1
    assert len(ctrl) == 1
    assert ctrl[0][2]["values"]["listen_status"] == "COMPLETE"
    assert ctrl[0][2]["values"]["action"] == "accept"
    assert ctrl[0][2]["values"]["reason"] == "valid_result"
    assert transcript[0][2]["values"]["projection_source"] == "listen"
    assert transcript[0][2]["values"]["message_id"] is None
    assert transcript[0][2]["values"]["turn_id"] == "turn-1"


def test_signal_trace_error_names_component_stage_without_payload(monkeypatch):
    events = []
    monkeypatch.setattr(
        "playwright_auto.role_runtime.signals.append_live_event",
        lambda source, kind, **kwargs: events.append((source, kind, kwargs)),
    )
    identity = SignalIdentity(
        page_url="https://chatgpt.com/c/conv-1",
        page_id="page-1",
        role="DEV",
        task_id="task-1",
        team="team-1",
        request_id="request-1",
    )

    record_signal_error(
        identity,
        component="page_observer",
        stage="network_stream_decode",
        error=ValueError("secret provider payload"),
    )

    assert len(events) == 1
    source, kind, payload = events[0]
    assert (source, kind) == ("SIGNAL", "error")
    assert payload["values"] == {
        "component": "page_observer",
        "stage": "network_stream_decode",
        "error_type": "ValueError",
    }
    assert "secret provider payload" not in str(payload)


def test_bound_dom_stream_keeps_one_logical_key_across_message_node_remount(monkeypatch):
    events = []
    monkeypatch.setattr(
        "playwright_auto.role_runtime.signals.append_live_event",
        lambda source, kind, **kwargs: events.append((source, kind, kwargs)),
    )
    frame = RoleSignals.from_sources(_snapshot(), {})
    identity = SignalIdentity.from_snapshot(frame.snapshot, request_id="request-1")
    wait = {
        "bound_assistant_message_id": "node-1",
        "bound_assistant_turn_id": "turn-stable",
        "bound_assistant_progress": {
            "message_id": "node-1",
            "turn_id": "turn-stable",
            "text": "partial",
        },
    }
    record_cycle(
        frame,
        wait,
        identity=identity,
        phase="waiting",
        previous_decision=(None, None),
        decision=Decision(Action.WAIT, "result_stability"),
    )
    wait["bound_assistant_progress"] = {
        "message_id": "node-2",
        "turn_id": "turn-stable",
        "text": "done",
    }
    record_cycle(
        frame,
        wait,
        identity=identity,
        phase="waiting",
        previous_decision=(Action.WAIT.value, "result_stability"),
        decision=Decision(Action.ACCEPT, "valid_bound_result"),
    )

    transcript = [event[2]["values"] for event in events if event[0] == "TRANSCRIPT"]
    assert [(item["message_id"], item["turn_id"], item["complete"]) for item in transcript] == [
        (None, "turn-stable", False),
        (None, "turn-stable", True),
    ]


def test_projected_response_segments_emit_distinct_transcript_rows_without_aggregate_duplicate():
    probe = SimpleNamespace(
        last_user_message_id="user-1",
        last_user_turn_id="turn-1",
        last_user_text="task",
        response_activity_turn_id="turn-1",
        response_activity_text="Thinking answer",
        response_activity_length=15,
        response_segments=(
            SimpleNamespace(key="projection:turn-1:status:0", kind="status", text="Thinking"),
            SimpleNamespace(key="projection:turn-1:answer:0", kind="answer", text="answer"),
        ),
        last_assistant_turn_id=None,
        last_assistant_message_id=None,
        assistant_text="",
    )

    rows = transcript_messages(probe)

    assistant = [row for row in rows if row["author"] == "assistant"]
    assert [(row["message_id"], row["phase"], row["text"]) for row in assistant] == [
        ("projection:turn-1:status:0", "status", "Thinking"),
        ("projection:turn-1:answer:0", "answer", "answer"),
    ]
    assert all(row["complete"] is False for row in assistant)


def test_projected_answer_deduplicates_real_assistant_aggregate_with_message_id():
    probe = SimpleNamespace(
        last_user_message_id="user-1",
        last_user_turn_id="turn-1",
        last_user_text="task",
        response_activity_turn_id="turn-1",
        response_activity_text="Thinking answer",
        response_activity_length=15,
        response_segments=(
            SimpleNamespace(key="projection:turn-1:status:0", kind="status", text="Thinking"),
            SimpleNamespace(key="projection:turn-1:answer:0", kind="answer", text="answer"),
        ),
        last_assistant_turn_id="turn-1",
        last_assistant_message_id="assistant-1",
        assistant_text="answer",
        assistant_text_length=6,
        stop_visible=False,
        transport_active=False,
    )

    rows = transcript_messages(probe)

    assistant = [row for row in rows if row["author"] == "assistant"]
    assert [(row["message_id"], row["text"]) for row in assistant] == [
        ("projection:turn-1:status:0", "Thinking"),
        ("projection:turn-1:answer:0", "answer"),
    ]
    assert sum(row["text"] == "answer" for row in assistant) == 1


def test_signal_identity_prefers_snapshot_ownership():
    identity = SignalIdentity.from_snapshot(
        _snapshot(),
        request_id="request-1",
        role="OVERRIDE",
        page_id="override-page",
    )

    assert identity.page_id == "page-1"
    assert identity.role == "DEV"
    assert identity.task_id == "task-1"
    assert identity.team == "team-1"
    assert identity.request_id == "request-1"
