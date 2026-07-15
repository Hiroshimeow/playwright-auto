import pytest

from playwright_auto.roles import RoleSlot, expand_role_team


def test_role_slot_display_names_keep_base_role_separate_from_instance():
    assert RoleSlot("DEV", 0).display_name == "DEV"
    assert RoleSlot("DEV", 1).display_name == "DEV1"
    assert RoleSlot("REVIEW", 4).to_dict() == {
        "base_role": "REVIEW",
        "instance": 4,
        "display_name": "REVIEW4",
    }


def test_expand_arbitrary_multi_role_team():
    slots = expand_role_team({"PLAN": 1, "DEV": 3, "REVIEW": 4, "TEST": 2})
    assert [slot.display_name for slot in slots] == [
        "PLAN",
        "DEV",
        "DEV1",
        "DEV2",
        "REVIEW",
        "REVIEW1",
        "REVIEW2",
        "REVIEW3",
        "TEST",
        "TEST1",
    ]


def test_team_validation_rejects_invalid_counts_collisions_and_excess_size():
    with pytest.raises(ValueError, match="at least 1"):
        expand_role_team({"DEV": 0})
    with pytest.raises(TypeError, match="integer"):
        expand_role_team({"DEV": True})
    with pytest.raises(ValueError, match="not unique"):
        expand_role_team({"DEV": 2, "DEV1": 1})
    with pytest.raises(ValueError, match="maximum"):
        expand_role_team({"DEV": 65})
