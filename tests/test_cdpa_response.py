from datetime import datetime, timedelta, timezone

from playwright_auto.cdpa_response import (
    begin_refresh,
    finish_refresh,
    observe_response_activity,
    observe_responding,
    recover_incomplete_refresh,
    refresh_due,
    remaining_timeout_ms,
    start_wait_budget,
)


def test_wait_budget_is_fixed_across_refreshes():
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)
    deadline = wait["deadline_at"]
    observe_responding(
        wait,
        stop_visible=True,
        composer_empty=True,
        manual_input_pending=False,
        now=start,
    )
    assert not refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=19))
    assert refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=20))
    begin_refresh(wait, now=start + timedelta(minutes=20))
    finish_refresh(wait, now=start + timedelta(minutes=20, seconds=5))
    assert wait["refresh_count"] == 1
    assert wait["deadline_at"] == deadline
    assert not refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=39))
    assert refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=40))
    assert remaining_timeout_ms(wait, now=start + timedelta(hours=1)) == 3_600_000


def test_incomplete_refresh_is_reconciled_without_duplicate_reload():
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)
    observe_responding(
        wait,
        stop_visible=True,
        composer_empty=True,
        manual_input_pending=False,
        now=start,
    )
    begin_refresh(wait, now=start + timedelta(minutes=20))

    assert recover_incomplete_refresh(
        wait,
        now=start + timedelta(minutes=20, seconds=10),
    )
    assert wait["refresh_count"] == 1
    assert wait["refresh_in_progress"] is None
    assert wait["last_refresh_result"]["status"] == "interrupted"
    assert not refresh_due(
        wait,
        refresh_after_seconds=1200,
        now=start + timedelta(minutes=39),
    )
    assert refresh_due(
        wait,
        refresh_after_seconds=1200,
        now=start + timedelta(minutes=40),
    )


def test_manual_input_clears_continuous_responding_signal():
    now = datetime.now(timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=10, now=now)
    assert observe_responding(wait, stop_visible=True, composer_empty=True, manual_input_pending=False, now=now)
    assert not observe_responding(wait, stop_visible=True, composer_empty=False, manual_input_pending=True, now=now)
    assert wait["continuous_responding_since"] is None


def test_interrupted_refresh_preserves_recovery_baseline():
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)
    wait["continuous_responding_since"] = start.isoformat()
    wait["recovery_baseline"] = {
        "assistant_message_ids": ["a1"],
        "assistant_turn_ids": ["t1"],
        "assistant_fingerprints": ["fingerprint"],
    }
    begin_refresh(wait, now=start + timedelta(minutes=20))

    assert recover_incomplete_refresh(wait, now=start + timedelta(minutes=21)) is True
    assert wait["recovery_baseline"] == {
        "assistant_message_ids": ["a1"],
        "assistant_turn_ids": ["t1"],
        "assistant_fingerprints": ["fingerprint"],
    }


def test_response_activity_resets_no_progress_anchor_only_on_change():
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)

    assert observe_response_activity(
        wait, signature="sig-a", length=10, now=start + timedelta(minutes=1)
    )
    first_change = wait["activity_changed_at"]
    assert not observe_response_activity(
        wait, signature="sig-a", length=10, now=start + timedelta(minutes=5)
    )
    assert wait["activity_changed_at"] == first_change
    assert observe_response_activity(
        wait, signature="sig-b", length=20, now=start + timedelta(minutes=6)
    )
    assert wait["activity_length"] == 20
    assert wait["activity_changed_at"] != first_change


def test_no_progress_refresh_is_allowed_when_stop_disappeared():
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    wait = {}
    start_wait_budget(wait, timeout_seconds=7200, now=start)
    observe_response_activity(wait, signature="progress", length=8, now=start)

    assert not refresh_due(
        wait,
        refresh_after_seconds=1200,
        composer_empty=True,
        manual_input_pending=False,
        now=start + timedelta(minutes=19),
    )
    assert refresh_due(
        wait,
        refresh_after_seconds=1200,
        composer_empty=True,
        manual_input_pending=False,
        now=start + timedelta(minutes=20),
    )
    assert not refresh_due(
        wait,
        refresh_after_seconds=1200,
        composer_empty=False,
        manual_input_pending=True,
        now=start + timedelta(minutes=21),
    )
