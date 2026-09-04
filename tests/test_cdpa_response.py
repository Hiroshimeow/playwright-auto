from datetime import datetime, timedelta, timezone

import pytest

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
    begin_refresh(wait, now=start + timedelta(minutes=40))
    finish_refresh(wait, now=start + timedelta(minutes=40, seconds=5))
    assert wait["refresh_count"] == 2
    assert observe_response_activity(
        wait,
        signature="later-activity",
        length=10,
        now=start + timedelta(minutes=45),
    )
    assert not refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=64))
    assert refresh_due(wait, refresh_after_seconds=1200, now=start + timedelta(minutes=65))
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




from playwright_auto.cdpa_routes import RouteContractError, parse_role_response


INLINE_RESPONSE = """# Final report

Evidence.

```json
{"route":"DONE","handoff":"INLINE"}
```
"""


def test_inline_mode_extracts_markdown_before_terminal_json():
    parsed = parse_role_response(
        INLINE_RESPONSE, source_role="PLAN", report_mode="inline"
    )
    assert parsed.inline_report == "# Final report\n\nEvidence."
    assert parsed.decision.route == "DONE"
    assert parsed.decision.handoff == "INLINE"


def test_file_mode_rejects_inline_body_and_handoff():
    with pytest.raises(RouteContractError, match="file report"):
        parse_role_response(INLINE_RESPONSE, source_role="PLAN", report_mode="file")
    with pytest.raises(RouteContractError, match="file report"):
        parse_role_response(
            '{"route":"DEV","handoff":"INLINE"}',
            source_role="PLAN",
            report_mode="file",
        )




def test_inline_mode_preserves_done_authority():
    with pytest.raises(RouteContractError, match="only PLAN"):
        parse_role_response(INLINE_RESPONSE, source_role="DEV", report_mode="inline")
