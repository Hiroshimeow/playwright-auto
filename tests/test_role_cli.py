from __future__ import annotations

import pytest

from playwright_auto.role_cli import (
    RoleSelectionRequired,
    TabCandidate,
    build_assignment_plan,
)


def tab(index: int, title: str, role: str | None = None) -> TabCandidate:
    return TabCandidate(
        index=index,
        title=title,
        url=f"https://chatgpt.com/c/{index}",
        current_role=role,
        page_id=f"page-{index}",
    )


def test_roles_auto_map_when_chatgpt_tab_count_matches() -> None:
    tabs = [tab(0, "First"), tab(1, "Second")]

    plan = build_assignment_plan(tabs, ["REVIEW", "REVIEW1"])

    assert [(item.tab.index, item.role) for item in plan] == [
        (0, "REVIEW"),
        (1, "REVIEW1"),
    ]


def test_more_tabs_require_explicit_or_interactive_selection() -> None:
    tabs = [tab(0, "A"), tab(1, "B"), tab(2, "C")]

    with pytest.raises(RoleSelectionRequired, match="3 ChatGPT tabs"):
        build_assignment_plan(tabs, ["REVIEW", "REVIEW1"])


def test_explicit_selection_maps_arbitrary_roles_without_duplicate_tabs() -> None:
    tabs = [tab(0, "A", "DEV"), tab(1, "B"), tab(2, "C", "TEST")]

    plan = build_assignment_plan(
        tabs,
        ["REVIEW", "SECURITY"],
        selected_indices=[2, 0],
    )

    assert [(item.tab.index, item.tab.current_role, item.role) for item in plan] == [
        (2, "TEST", "REVIEW"),
        (0, "DEV", "SECURITY"),
    ]

    with pytest.raises(ValueError, match="selected more than once"):
        build_assignment_plan(
            tabs,
            ["REVIEW", "REVIEW1"],
            selected_indices=[1, 1],
        )
