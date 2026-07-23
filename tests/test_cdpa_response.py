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


@pytest.mark.parametrize(
    "response, message",
    [
        ('```json\n{"route":"DEV","handoff":"INLINE"}\n```', "Markdown report"),
        ('   \n{"route":"DEV","handoff":"INLINE"}', "Markdown report"),
        ('# Report\n\n{"route":"DEV","handoff":"wrong"}', 'handoff "INLINE"'),
        (
            '# Report\n\n{"route":"DEV","handoff":"INLINE"}\n\ntrailing',
            "terminal",
        ),
        (
            '# Report\n\n{"route":"TEST","handoff":"INLINE"}\n\n{"route":"DEV","handoff":"INLINE"}',
            "exactly one terminal",
        ),
        (
            '# Report\n\n```json\n{"route":"DEV","route":"TEST","handoff":"INLINE"}\n```',
            "duplicate route field",
        ),
    ],
)
def test_inline_mode_rejects_invalid_or_ambiguous_report_response(response, message):
    with pytest.raises(RouteContractError, match=message):
        parse_role_response(response, source_role="PLAN", report_mode="inline")


def test_inline_mode_preserves_done_authority():
    with pytest.raises(RouteContractError, match="only PLAN"):
        parse_role_response(INLINE_RESPONSE, source_role="DEV", report_mode="inline")


def test_parse_role_response_rejects_unknown_report_mode():
    with pytest.raises(ValueError, match="report_mode"):
        parse_role_response(
            '{"route":"DEV","handoff":"x"}',
            source_role="PLAN",
            report_mode="other",
        )


def test_inline_mode_allows_unrelated_json_evidence_before_terminal_route():
    response = """# Report

```json
{"evidence": true}
```

```json
{"route":"DEV","handoff":"INLINE"}
```
"""

    parsed = parse_role_response(response, source_role="PLAN", report_mode="inline")

    assert parsed.decision.route == "DEV"
    assert parsed.inline_report == '# Report\n\n```json\n{"evidence": true}\n```'


def test_inline_materializer_rejects_report_symlink_without_external_write(tmp_path):
    from playwright_auto.cdpa_routes import materialize_inline_report

    target = tmp_path / ".plan" / "alpha" / "alpha-plan_turn1_task-link.md"
    external = tmp_path / "outside.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(external)

    with pytest.raises(RouteContractError, match="symlink"):
        materialize_inline_report(
            "# Report",
            expected_report_path=".plan/alpha/alpha-plan_turn1_task-link.md",
            repository_root=tmp_path,
            plans_root=tmp_path / ".plan",
            team="alpha",
            physical_role="alpha-plan",
            turn=1,
            task_id="task-link",
        )

    assert not external.exists()


def test_inline_materializer_rejects_symlinked_team_directory_before_write(tmp_path):
    from playwright_auto.cdpa_routes import materialize_inline_report

    plans = tmp_path / ".plan"
    external = tmp_path / "outside-team"
    plans.mkdir()
    external.mkdir()
    (plans / "alpha").symlink_to(external, target_is_directory=True)

    with pytest.raises(RouteContractError, match="escapes|exactly match|symlink"):
        materialize_inline_report(
            "# Report",
            expected_report_path=".plan/alpha/alpha-plan_turn1_task-parent-link.md",
            repository_root=tmp_path,
            plans_root=plans,
            team="alpha",
            physical_role="alpha-plan",
            turn=1,
            task_id="task-parent-link",
        )

    assert not (external / "alpha-plan_turn1_task-parent-link.md").exists()



@pytest.mark.parametrize("language", ["json", "JSON", "JsOn"])
def test_inline_mode_terminal_json_fence_is_case_insensitive(language: str):
    response = (
        "# Report\n\n"
        f"```{language}\n"
        '{"route":"DEV","handoff":"INLINE"}\n'
        "```"
    )

    parsed = parse_role_response(
        response,
        source_role="PLAN",
        report_mode="inline",
    )

    assert parsed.decision.route == "DEV"
    assert parsed.inline_report == "# Report"
