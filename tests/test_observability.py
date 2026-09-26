import json
import multiprocessing
from pathlib import Path


def _append_action_events_from_process(log_path: str, worker_index: int, count: int, start) -> None:
    import playwright_auto.observability as observability

    observability.configure_action_event_log(Path(log_path))
    observability._ACTION_EVENT_MAX_BYTES = 4096
    if not start.wait(timeout=10):
        raise RuntimeError("action-writer start gate timed out")
    for index in range(count):
        observability.append_action_event(
            "mcp_allow",
            "complete",
            page_url=f"https://chatgpt.com/c/worker-{worker_index}-{index}",
            task_id=f"task-{worker_index}",
            detail="Allow mcp-thinkbook for this conversation",
        )


from playwright_auto.observability import (
    append_action_event,
    append_live_event,
    configure_action_event_log,
    configure_live_event_log,
    live_event_log_path,
    live_event_logging_enabled,
    mcp_allow_click_summary,
    read_recent_live_events,
)


def test_mcp_allow_click_summary_counts_only_live_chatgpt_clicks(tmp_path: Path):
    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)

    append_action_event(
        "mcp_allow",
        "complete",
        page_url="about:blank",
        detail="Allow mcp-thinkbook for this conversation",
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/11111111-1111-4111-8111-111111111111",
        task_id="task-1",
        role="team-dev",
        detail="Allow mcp-thinkbook for this conversation",
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/22222222-2222-4222-8222-222222222222",
        task_id="task-2",
        role="team-test",
        detail="Allow mcp-docker for this conversation",
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/44444444-4444-4444-8444-444444444444",
        task_id="task-3",
        role="team-app",
        detail="Allow Gmail for this conversation",
    )
    append_action_event(
        "send",
        "complete",
        page_url="https://chatgpt.com/c/33333333-3333-4333-8333-333333333333",
    )

    summary = mcp_allow_click_summary()

    assert summary["clicks"] == 3
    assert summary["by_tool"] == {"gmail": 1, "mcp-docker": 1, "mcp-thinkbook": 1}
    assert summary["by_task"] == {"task-1": 1, "task-2": 1, "task-3": 1}
    assert summary["latest_at"]


def test_action_summary_observation_without_sidecar_is_read_only(tmp_path: Path):
    import json

    log = tmp_path / "actions.jsonl"
    legacy = {
        "action": "mcp_allow",
        "phase": "complete",
        "page_url": "https://chatgpt.com/c/legacy",
        "task_id": "legacy-task",
        "detail": "Allow mcp-thinkbook for this conversation",
        "at": "2026-09-15T00:00:00+00:00",
    }
    log.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    configure_action_event_log(log)
    sidecar = tmp_path / "actions.summary.json"
    lock = tmp_path / "actions.jsonl.lock"

    summary = mcp_allow_click_summary()

    assert summary["clicks"] is None
    assert "unavailable" in summary["error"].lower()
    assert not sidecar.exists()
    assert not lock.exists()


def test_action_summary_migrates_once_on_first_writer_and_uses_sidecar_afterward(
    tmp_path: Path, monkeypatch
):
    import json
    import playwright_auto.observability as observability

    log = tmp_path / "actions.jsonl"
    legacy = {
        "action": "mcp_allow",
        "phase": "complete",
        "page_url": "https://chatgpt.com/c/legacy",
        "task_id": "legacy-task",
        "detail": "Allow mcp-thinkbook for this conversation",
        "at": "2026-09-15T00:00:00+00:00",
    }
    log.write_text(json.dumps(legacy) + "\n{partial", encoding="utf-8")
    configure_action_event_log(log)

    assert mcp_allow_click_summary()["clicks"] is None
    sidecar = tmp_path / "actions.summary.json"
    lock = tmp_path / "actions.jsonl.lock"
    assert not sidecar.exists()
    assert not lock.exists()

    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/new",
        task_id="new-task",
        detail="Allow mcp-docker for this conversation",
    )
    assert sidecar.exists()
    assert lock.exists()
    summary = mcp_allow_click_summary()
    assert summary["clicks"] == 2
    assert summary["by_tool"] == {"mcp-docker": 1, "mcp-thinkbook": 1}

    monkeypatch.setattr(
        observability,
        "_scan_allow_summary",
        lambda _target: (_ for _ in ()).throw(AssertionError("rescanned history")),
    )
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/newer",
        task_id="newer-task",
        detail="Allow mcp-g8 for this conversation",
    )
    assert mcp_allow_click_summary()["clicks"] == 3


def test_precreated_lock_without_action_history_does_not_suppress_first_summary_initialization(tmp_path: Path):
    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    (tmp_path / "actions.jsonl.lock").touch()

    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/first",
        task_id="task-first",
        detail="Allow mcp-thinkbook for this conversation",
    )

    assert mcp_allow_click_summary(log)["clicks"] == 1


def test_concurrent_action_writers_preserve_exact_all_time_summary_and_valid_bounded_log(tmp_path: Path):
    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    writers = 4
    per_writer = 20
    processes = [
        context.Process(
            target=_append_action_events_from_process,
            args=(str(log), worker_index, per_writer, start),
        )
        for worker_index in range(writers)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    summary = mcp_allow_click_summary(log)
    assert summary["clicks"] == writers * per_writer
    assert summary["by_tool"] == {"mcp-thinkbook": writers * per_writer}
    assert summary["by_task"] == {
        f"task-{worker_index}": per_writer for worker_index in range(writers)
    }
    sidecar = tmp_path / "actions.summary.json"
    json.loads(sidecar.read_text(encoding="utf-8"))
    assert log.stat().st_size <= 4096
    for line in log.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_action_log_trim_preserves_all_time_summary(tmp_path: Path, monkeypatch):
    import playwright_auto.observability as observability

    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    monkeypatch.setattr(observability, "_ACTION_EVENT_MAX_BYTES", 700)
    for index in range(20):
        append_action_event(
            "mcp_allow",
            "complete",
            page_url=f"https://chatgpt.com/c/{index}",
            task_id=f"task-{index}",
            detail="Allow mcp-thinkbook for this conversation",
        )
    assert log.stat().st_size < 700
    assert mcp_allow_click_summary()["clicks"] == 20


def test_missing_established_action_summary_is_not_rebuilt_from_rotated_log(
    tmp_path: Path, monkeypatch
):
    import playwright_auto.observability as observability

    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    max_bytes = 1800
    monkeypatch.setattr(observability, "_ACTION_EVENT_MAX_BYTES", max_bytes)
    for index in range(20):
        append_action_event(
            "mcp_allow",
            "complete",
            page_url=f"https://chatgpt.com/c/{index}",
            task_id=f"task-{index}",
            detail="Allow mcp-thinkbook for this conversation",
        )
    assert mcp_allow_click_summary()["clicks"] == 20
    sidecar = tmp_path / "actions.summary.json"
    lock = tmp_path / "actions.jsonl.lock"
    assert sidecar.exists()
    assert lock.exists()
    sidecar.unlink()

    summary = mcp_allow_click_summary()

    assert summary["clicks"] is None
    assert "unavailable" in summary["error"].lower()
    assert not sidecar.exists()
    append_action_event(
        "mcp_allow",
        "complete",
        page_url="https://chatgpt.com/c/after-loss",
        task_id="task-after-loss",
        detail="Allow mcp-docker for this conversation",
    )
    assert not sidecar.exists()
    assert log.stat().st_size <= max_bytes
    assert "task-after-loss" in log.read_text(encoding="utf-8")
    assert mcp_allow_click_summary()["clicks"] is None


def test_established_action_summary_corruption_is_reported_without_rebuild(
    tmp_path: Path, monkeypatch
):
    import playwright_auto.observability as observability

    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    monkeypatch.setattr(observability, "_ACTION_EVENT_MAX_BYTES", 700)
    for index in range(20):
        append_action_event(
            "mcp_allow",
            "complete",
            page_url=f"https://chatgpt.com/c/{index}",
            task_id=f"task-{index}",
            detail="Allow mcp-thinkbook for this conversation",
        )
    assert mcp_allow_click_summary()["clicks"] == 20

    sidecar = tmp_path / "actions.summary.json"
    sidecar.write_text("{broken", encoding="utf-8")

    summary = mcp_allow_click_summary()

    assert summary["clicks"] is None
    assert summary["by_tool"] == {}
    assert summary["by_task"] == {}
    assert summary["latest_at"] is None
    assert "corrupt" in summary["error"].lower()
    assert sidecar.read_text(encoding="utf-8") == "{broken"


def test_action_append_preserves_corrupt_summary_and_bounds_recent_evidence(
    tmp_path: Path, monkeypatch
):
    import playwright_auto.observability as observability

    log = tmp_path / "actions.jsonl"
    configure_action_event_log(log)
    max_bytes = 1800
    monkeypatch.setattr(observability, "_ACTION_EVENT_MAX_BYTES", max_bytes)
    for index in range(20):
        append_action_event(
            "mcp_allow",
            "complete",
            page_url=f"https://chatgpt.com/c/{index}",
            task_id=f"task-{index}",
            detail="Allow mcp-thinkbook for this conversation",
        )
    sidecar = tmp_path / "actions.summary.json"
    sidecar.write_text("{broken", encoding="utf-8")

    for index in range(20):
        append_action_event(
            "send",
            "complete",
            page_url=f"https://chatgpt.com/c/after-corruption-{index}",
            task_id=f"task-after-corruption-{index}",
            detail="x" * 120,
        )

    assert sidecar.read_text(encoding="utf-8") == "{broken"
    assert log.stat().st_size <= max_bytes
    assert "task-after-corruption-19" in log.read_text(encoding="utf-8")
    summary = mcp_allow_click_summary()
    assert summary["clicks"] is None
    assert "corrupt" in summary["error"].lower()


def test_live_event_log_filters_by_task_and_role(tmp_path: Path):
    old_log = live_event_log_path()
    old_enabled = live_event_logging_enabled()
    log = tmp_path / "live.jsonl"
    configure_live_event_log(log)
    try:
        append_live_event(
            "DOM",
            "wait_probe",
            page_url="https://chatgpt.com/c/one",
            page_id="page-1",
            role="DEV",
            task_id="task-1",
            team="alpha",
            changes={"stop_visible": {"from": False, "to": True}},
        )
        append_live_event(
            "LISTEN",
            "response_observation",
            page_url="https://chatgpt.com/c/two",
            page_id="page-2",
            role="alpha-review",
            task_id="task-1",
            team="alpha",
            request_id="req-2",
            generation=3,
            values={"coverage": "partial", "event_count": 2},
        )
        append_live_event(
            "CTRL",
            "state_transition",
            task_id="task-1",
            role="REVIEW",
            values={"active_action": "validate_route"},
        )
        append_live_event(
            "CTRL",
            "state_transition",
            task_id="task-2",
            role="DEV",
            values={"active_action": "wait_response"},
        )

        dev = read_recent_live_events(task_id="task-1", role="DEV", limit=20)
        review = read_recent_live_events(task_id="task-1", role="REVIEW", limit=20)
        all_task = read_recent_live_events(task_id="task-1", limit=20)

        assert [item["source"] for item in dev] == ["DOM"]
        assert [item["source"] for item in review] == ["LISTEN", "CTRL"]
        assert [item["source"] for item in all_task] == ["DOM", "LISTEN", "CTRL"]
        assert review[0]["request_id"] == "req-2"
        assert review[0]["generation"] == 3
        assert review[0]["team"] == "alpha"
    finally:
        configure_live_event_log(old_log, enabled=old_enabled)


def test_live_event_log_dedupes_unchanged_listen_and_ctrl_across_calls(tmp_path: Path):
    old_log = live_event_log_path()
    old_enabled = live_event_logging_enabled()
    log = tmp_path / "live.jsonl"
    configure_live_event_log(log)
    try:
        common = {
            "page_url": "https://chatgpt.com/c/one",
            "page_id": "page-1",
            "role": "DEV",
            "task_id": "task-1",
            "team": "alpha",
            "request_id": "req-1",
            "generation": 2,
        }
        append_live_event("LISTEN", "network_signal", **common, values={"status": "IS_STREAMING"})
        append_live_event("LISTEN", "network_signal", **common, values={"status": "IS_STREAMING"})
        append_live_event("CTRL", "role_decision", **common, values={"action": "wait", "reason": "mcp_allow_stable"})
        append_live_event("CTRL", "role_decision", **common, values={"action": "wait", "reason": "mcp_allow_stable"})
        append_live_event("LISTEN", "network_signal", **common, values={"status": "DONE"})
        append_live_event("CTRL", "role_decision", **common, values={"action": "accept", "reason": "stable_current_result"})
        append_live_event(
            "LISTEN", "network_signal", **{**common, "page_url": "https://chatgpt.com/c/two"},
            values={"status": "DONE"},
        )

        events = read_recent_live_events(task_id="task-1", limit=20)

        assert [(item["source"], item["values"]) for item in events] == [
            ("LISTEN", {"status": "IS_STREAMING"}),
            ("CTRL", {"action": "wait", "reason": "mcp_allow_stable"}),
            ("LISTEN", {"status": "DONE"}),
            ("CTRL", {"action": "accept", "reason": "stable_current_result"}),
            ("LISTEN", {"status": "DONE"}),
        ]
    finally:
        configure_live_event_log(old_log, enabled=old_enabled)


def test_live_transcript_dedupes_same_message_but_keeps_partial_to_final_updates(tmp_path: Path):
    old_log = live_event_log_path()
    old_enabled = live_event_logging_enabled()
    log = tmp_path / ".runtime" / "live-events.jsonl"
    configure_live_event_log(log)
    try:
        common = {
            "task_id": "task-transcript",
            "team": "alpha",
            "role": "alpha-dev",
            "page_id": "page-1",
            "page_url": "https://chatgpt.com/c/test",
        }
        partial = {
            "author": "assistant",
            "message_id": "answer-1",
            "turn_id": "turn-1",
            "text": "working",
            "text_length": 7,
            "complete": False,
        }
        append_live_event("TRANSCRIPT", "message", **common, values=partial)
        append_live_event("TRANSCRIPT", "message", **common, values=partial)
        append_live_event(
            "TRANSCRIPT",
            "message",
            **common,
            values={**partial, "text": "working done", "text_length": 12, "complete": True},
        )
        items = read_recent_live_events(log, task_id="task-transcript", role="DEV", limit=20)
        transcript = [item for item in items if item["source"] == "TRANSCRIPT"]
        assert len(transcript) == 2
        assert transcript[0]["values"]["complete"] is False
        assert transcript[1]["values"]["text"] == "working done"
        assert transcript[1]["values"]["complete"] is True
    finally:
        configure_live_event_log(old_log, enabled=old_enabled)


def test_live_event_tail_reader_does_not_use_full_file_read_text(tmp_path: Path, monkeypatch):
    import json

    log = tmp_path / "large-live.jsonl"
    lines = [
        json.dumps({
            "event_id": f"event-{index}",
            "source": "DOM",
            "kind": "wait_probe",
            "task_id": "task-1" if index % 2 == 0 else "other",
            "role": "alpha-dev",
        })
        for index in range(5000)
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full file read")),
    )
    items = read_recent_live_events(log, task_id="task-1", role="DEV", limit=5)

    assert [item["event_id"] for item in items] == [
        "event-4990", "event-4992", "event-4994", "event-4996", "event-4998",
    ]


def test_live_event_tail_reader_legacy_id_is_content_stable_and_partial_tail_is_safe(tmp_path: Path):
    import json

    log = tmp_path / "legacy-live.jsonl"
    legacy = {
        "source": "DOM",
        "kind": "wait_probe",
        "task_id": "task-1",
        "role": "alpha-dev",
        "values": {"stop_visible": False},
    }
    encoded = json.dumps(legacy, sort_keys=True)
    log.write_text(encoded + "\n{partial", encoding="utf-8")

    first = read_recent_live_events(log, task_id="task-1", role="DEV", limit=5)
    assert len(first) == 1
    legacy_id = first[0]["event_id"]
    assert legacy_id.startswith("legacy-live-")

    log.write_text(
        json.dumps({"source": "CTRL", "task_id": "other"}) + "\n" + encoded + "\n",
        encoding="utf-8",
    )
    second = read_recent_live_events(log, task_id="task-1", role="DEV", limit=5)
    assert [item["event_id"] for item in second] == [legacy_id]
