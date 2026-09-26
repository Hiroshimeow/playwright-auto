import json
import queue
from pathlib import Path

from playwright_auto.live_stream import LiveLogFollower, LiveStreamHub, project_live_event


def _event(event_id: str, *, task: str = "task-1", role: str = "alpha-dev", source: str = "DOM"):
    return {
        "event_id": event_id,
        "source": source,
        "kind": "message" if source == "TRANSCRIPT" else "wait_probe",
        "task_id": task,
        "role": role,
        "at": "2026-09-25T00:00:00+00:00",
        "values": {"text": event_id} if source == "TRANSCRIPT" else {"stop_visible": False},
    }


def test_projection_has_only_two_durable_event_types():
    assert project_live_event(_event("a", source="TRANSCRIPT"))["type"] == "transcript.upsert"
    assert project_live_event(_event("b", source="DOM"))["type"] == "audit.append"


def test_hub_sequences_are_contiguous_per_filtered_scope(tmp_path: Path):
    hub = LiveStreamHub(tmp_path / "live.jsonl")
    dev, ready = hub.subscribe("task-1", "DEV")
    review, review_ready = hub.subscribe("task-1", "REVIEW")
    try:
        assert ready["type"] == "stream.ready"
        assert ready["seq"] == 0
        assert review_ready["seq"] == 0

        hub.publish(_event("dev-1", role="alpha-dev"))
        hub.publish(_event("review-1", role="alpha-review"))
        hub.publish(_event("dev-2", role="alpha-dev"))

        assert [dev.get(timeout=0.1).seq, dev.get(timeout=0.1).seq] == [1, 2]
        assert review.get(timeout=0.1).seq == 1
        with __import__("pytest").raises(queue.Empty):
            review.get(timeout=0.01)
    finally:
        hub.close()


def test_hub_replays_retained_events_from_last_event_id(tmp_path: Path):
    hub = LiveStreamHub(tmp_path / "live.jsonl", ring_size=4)
    first, ready = hub.subscribe("task-1", "DEV")
    anchor, _ = hub.subscribe("task-1", "REVIEW")
    epoch = ready["stream_epoch"]
    try:
        hub.publish(_event("one"))
        hub.publish(_event("two"))
        assert first.get(timeout=0.1).seq == 1
        assert first.get(timeout=0.1).seq == 2
        hub.unsubscribe(first)

        resumed, control = hub.subscribe("task-1", "DEV", last_event_id=f"{epoch}:1")
        try:
            assert control is None
            replay = resumed.get(timeout=0.1)
            assert replay.seq == 2
            assert replay.event_id == "two"
        finally:
            hub.unsubscribe(resumed)
    finally:
        hub.unsubscribe(anchor)
        hub.close()


def test_hub_reactivation_after_dormancy_invalidates_old_cursor(tmp_path: Path):
    path = tmp_path / "live.jsonl"
    path.write_bytes(b"")
    hub = LiveStreamHub(path, ring_size=4)
    first, ready = hub.subscribe("task-1", "DEV")
    old_epoch = ready["stream_epoch"]
    try:
        hub.publish(_event("one"))
        assert first.get(timeout=0.1).seq == 1
        hub.unsubscribe(first)
        assert hub.follower.active is False

        with path.open("ab") as handle:
            handle.write(json.dumps(_event("two")).encode() + b"\n")

        resumed, control = hub.subscribe(
            "task-1",
            "DEV",
            last_event_id=f"{old_epoch}:1",
        )
        try:
            assert control["type"] == "stream.reset"
            assert control["stream_epoch"] != old_epoch
            assert control["seq"] == 0
            assert hub.follower._offset == path.stat().st_size
            with __import__("pytest").raises(queue.Empty):
                resumed.get(timeout=0.01)
        finally:
            hub.unsubscribe(resumed)
    finally:
        hub.close()


def test_hub_resets_for_stale_epoch_eviction_and_ahead_cursor(tmp_path: Path):
    hub = LiveStreamHub(tmp_path / "live.jsonl", ring_size=2)
    subscription, ready = hub.subscribe("task-1", "DEV")
    epoch = ready["stream_epoch"]
    try:
        for index in range(1, 4):
            hub.publish(_event(f"e{index}"))
            subscription.get(timeout=0.1)
        hub.unsubscribe(subscription)

        for cursor in (f"{epoch}:0", f"{epoch}:99", "old-epoch:2", "malformed"):
            resumed, control = hub.subscribe("task-1", "DEV", last_event_id=cursor)
            try:
                assert control["type"] == "stream.reset"
            finally:
                hub.unsubscribe(resumed)
    finally:
        hub.close()


def test_hub_queue_overflow_terminates_slow_subscriber(tmp_path: Path):
    hub = LiveStreamHub(tmp_path / "live.jsonl", subscriber_queue_size=1)
    subscription, _ = hub.subscribe("task-1", "DEV")
    try:
        hub.publish(_event("one"))
        hub.publish(_event("two"))
        assert subscription.overflowed is True
        assert subscription.closed is True
        assert subscription not in hub.subscribers
    finally:
        hub.close()


class _Recorder:
    def __init__(self):
        self.events = []
        self.resets = 0

    def publish(self, event):
        self.events.append(event)

    def reset_stream(self):
        self.resets += 1


def test_follower_reads_complete_appends_and_preserves_partial_line(tmp_path: Path):
    path = tmp_path / "live.jsonl"
    path.write_bytes(b"")
    recorder = _Recorder()
    follower = LiveLogFollower(recorder, path)
    follower._active = True
    follower._identity = follower._stat_identity()
    follower._offset = 0

    first = json.dumps(_event("one")).encode()
    second = json.dumps(_event("two")).encode()
    path.write_bytes(first + b"\n" + second[:8])
    follower._read_once()
    assert [item["event_id"] for item in recorder.events] == ["one"]
    assert follower._partial == second[:8]

    with path.open("ab") as handle:
        handle.write(second[8:] + b"\n")
    follower._read_once()
    assert [item["event_id"] for item in recorder.events] == ["one", "two"]


def test_follower_rotation_resets_epoch_before_publishing_replacement(tmp_path: Path):
    path = tmp_path / "live.jsonl"
    original = json.dumps(_event("old")).encode() + b"\n"
    path.write_bytes(original)
    recorder = _Recorder()
    follower = LiveLogFollower(recorder, path)
    follower._active = True
    follower._identity = follower._stat_identity()
    follower._offset = len(original)

    replacement = json.dumps(_event("new")).encode() + b"\n"
    rotated = tmp_path / "replacement.jsonl"
    rotated.write_bytes(replacement)
    __import__("os").replace(rotated, path)
    follower._read_once()

    assert recorder.resets == 1
    assert recorder.events[-1]["event_id"] == "new"
