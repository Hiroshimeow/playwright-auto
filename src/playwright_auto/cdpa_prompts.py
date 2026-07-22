from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from .cdpa_config import CDPAConfig


@dataclass(frozen=True)
class BuiltPrompt:
    text: str
    constructor_included: bool
    conversation_generation: int


class PromptBuilder:
    def __init__(self, config: CDPAConfig) -> None:
        self.config = config

    def _plans_prefix(self) -> str:
        try:
            return self.config.plans_root.relative_to(
                self.config.repository_root
            ).as_posix()
        except ValueError:
            return str(self.config.plans_root)

    def naming_rule(self) -> str:
        return f"{self._plans_prefix()}/<team>/<physical-role>_turn<N>_<task-id>.md"

    def _guide(self) -> str:
        guide = self.config.response_guide_path.read_text(encoding="utf-8").strip()
        return guide.replace(
            ".plan/<team>/<physical-role>_turn<N>_<task-id>.md",
            self.naming_rule(),
        ).replace(".plan/<team>", f"{self._plans_prefix()}/<team>")

    def build(
        self,
        *,
        task_title: str,
        task_id: str,
        team: str,
        logical_role: str,
        physical_role: str,
        turn: int,
        allowed_routes: Sequence[str],
        workspace: str,
        source_physical_role: str | None,
        handoff: str,
        goal: str,
        constructor_sent_generation: int | None,
        conversation_generation: int,
    ) -> BuiltPrompt:
        role = str(logical_role).strip().upper()
        if role not in self.config.roles:
            raise ValueError(f"unsupported CDPA role {role!r}")
        handoff = str(handoff).strip()
        goal = str(goal).strip()
        if not handoff or not goal:
            raise ValueError("handoff and goal must not be empty")
        generation = int(conversation_generation)
        include = constructor_sent_generation != generation
        envelope = {
            "title": str(task_title).strip(),
            "task-id": str(task_id).strip(),
            "team": str(team).strip(),
            "role": str(physical_role).strip(),
            "source-role": (
                str(source_physical_role).strip() if source_physical_role else None
            ),
            "turn": int(turn),
            "workspace": str(workspace).strip(),
            "allowed-routes": [
                str(item).strip().upper() for item in allowed_routes
            ],
            "goal": goal,
            "handoff": handoff,
        }
        sections = [
            "CDPA_TASK_ENVELOPE\n"
            + json.dumps(envelope, ensure_ascii=False, indent=2)
        ]
        if include:
            sections.append(
                self.config.constructor_paths[role]
                .read_text(encoding="utf-8")
                .strip()
            )
        sections.append(self._guide())
        return BuiltPrompt("\n\n".join(sections).strip(), include, generation)

    def _sanitize_validation_error(self, validation_error: str) -> str:
        cleaned: list[str] = []
        for token in str(validation_error).split():
            bare = token.strip("`'\"()[]{}<>,;:")
            if "_turn" in bare and bare.endswith(".md"):
                cleaned.append(self.naming_rule())
            else:
                cleaned.append(token)
        return " ".join(cleaned).strip()

    def repair(
        self,
        *,
        task_id: str,
        team: str,
        physical_role: str,
        turn: int,
        validation_error: str,
    ) -> str:
        error = self._sanitize_validation_error(validation_error)
        if not error:
            raise ValueError("repair requires a validation error")
        identity = {
            "task-id": str(task_id).strip(),
            "team": str(team).strip(),
            "role": str(physical_role).strip(),
            "turn": int(turn),
        }
        return (
            "CDPA_ROUTE_REPAIR\n"
            + json.dumps(identity, ensure_ascii=False, indent=2)
            + f"\n\nValidation error: {error}\n\n"
            + f"Report naming rule: {self.naming_rule()}\n\n"
            + self._guide()
        ).strip()
