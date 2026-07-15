from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .chatgpt import validate_page_role


@dataclass(frozen=True, order=True)
class RoleSlot:
    base_role: str
    instance: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_role", validate_page_role(self.base_role))
        if self.instance < 0:
            raise ValueError("role instance must not be negative")

    @property
    def display_name(self) -> str:
        return self.base_role if self.instance == 0 else f"{self.base_role}{self.instance}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_role": self.base_role,
            "instance": self.instance,
            "display_name": self.display_name,
        }


def expand_role_team(
    team: Mapping[str, int],
    *,
    max_total_slots: int = 64,
) -> tuple[RoleSlot, ...]:
    if not isinstance(team, Mapping) or not team:
        raise ValueError("WORKSPACE_TEAM must be a non-empty mapping")

    slots: list[RoleSlot] = []
    for raw_role, raw_count in team.items():
        role = validate_page_role(str(raw_role))
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise TypeError(f"role count for {role!r} must be an integer")
        if raw_count < 1:
            raise ValueError(f"role count for {role!r} must be at least 1")
        slots.extend(RoleSlot(role, instance) for instance in range(raw_count))

    if len(slots) > max_total_slots:
        raise ValueError(
            f"workspace requests {len(slots)} role slots; maximum is {max_total_slots}"
        )
    names = [slot.display_name for slot in slots]
    if len(set(names)) != len(names):
        raise ValueError(f"generated role display names are not unique: {names!r}")
    return tuple(slots)
