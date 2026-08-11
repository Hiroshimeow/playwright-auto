from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .cdpa_config import CDPAConfig
from .cdpa_independent import (
    event_context,
    load_trigger_learning,
    normalize_agent_name,
    normalize_system_prompt,
)
from .cdpa_workflow_agents import validate_workflow_route_key


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

    def _base_context(self) -> str:
        return self.config.response_guide_path.with_name("BASE_CONTEXT.md").read_text(
            encoding="utf-8"
        ).strip()

    def _guide(
        self,
        report_mode: str = "file",
        allowed_routes: Sequence[str] | None = None,
    ) -> str:
        mode = str(report_mode).strip().lower()
        if mode != "file":
            raise ValueError("inline workflow reports are no longer supported; report_mode must be 'file'")
        guide = self.config.response_guide_path.read_text(encoding="utf-8").strip()
        if allowed_routes is not None:
            route_contract = "|".join(
                validate_workflow_route_key(item)
                if str(item).strip().upper() != "DONE"
                else "DONE"
                for item in allowed_routes
            )
            guide = guide.replace(
                "PLAN|DEV|TEST|REVIEW|AUDIT|PAUSE|DONE", route_contract
            )
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
        report_mode: str = "file",
        constructor_text: str | None = None,
        is_system_role: bool = True,
        bootstrap_inherited: bool = False,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        allowed = tuple(str(item).strip().upper() for item in allowed_routes)
        if role not in allowed:
            raise ValueError(f"workflow role {role!r} is not in allowed_routes")
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
            f"{envelope['team']} · role: {role.lower()}\n"
            + json.dumps(envelope, ensure_ascii=False, indent=2)
        ]
        if include:
            constructor = (
                normalize_system_prompt(constructor_text)
                if constructor_text is not None
                else self.config.constructor_paths[role]
                .read_text(encoding="utf-8")
                .strip()
            )
            if is_system_role and not bootstrap_inherited:
                sections.append(self._base_context())
            sections.append(constructor)
        sections.append(self._guide(report_mode, allowed_routes))
        return BuiltPrompt("\n\n".join(sections).strip(), include, generation)

    def build_independent(
        self,
        *,
        agent_name: str,
        system_prompt: str,
        task_id: str,
        team: str,
        physical_role: str,
        workspace: str,
        event: Mapping[str, Any],
        cycle: int,
        max_cycles: int,
        constructor_sent_generation: int | None,
        conversation_generation: int,
    ) -> BuiltPrompt:
        display, _key, _derived_team = normalize_agent_name(agent_name)
        prompt = normalize_system_prompt(system_prompt)
        generation = int(conversation_generation)
        include = constructor_sent_generation != generation
        context = {
            "agent_name": display,
            "task_id": str(task_id).strip(),
            "team": str(team).strip(),
            "role": str(physical_role).strip(),
            "workspace": str(workspace).strip(),
            **event_context(event, cycle=int(cycle), max_cycles=int(max_cycles)),
        }
        sections: list[str] = []
        if include:
            sections.extend(
                (
                    prompt,
                    self.config.independent_rule_path.read_text(encoding="utf-8").strip(),
                )
            )
        learning = load_trigger_learning(workspace, str(event.get("trigger_type") or ""))
        if learning is not None and learning[1]:
            sections.append(
                f"TRIGGER_LEARNING ({learning[0].name})\n{learning[1]}"
            )
        sections.append(
            "INDEPENDENT_AGENT_TRIGGER_CONTEXT\n"
            + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
        )
        return BuiltPrompt("\n\n".join(sections).strip(), include, generation)

    def _sanitize_validation_error(
        self,
        validation_error: str,
        *,
        report_mode: str = "file",
        allowed_routes: Sequence[str] | None = None,
    ) -> str:
        mode = str(report_mode).strip().lower()
        if mode != "file":
            raise ValueError("inline workflow reports are no longer supported; report_mode must be 'file'")
        replacement = self.naming_rule()
        cleaned: list[str] = []
        for token in str(validation_error).split():
            bare = token.strip("`'\"()[]{}<>,;:")
            if "_turn" in bare and ".md" in bare:
                cleaned.append(replacement)
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
        report_mode: str = "file",
        allowed_routes: Sequence[str] | None = None,
    ) -> str:
        mode = str(report_mode).strip().lower()
        error = self._sanitize_validation_error(
            validation_error,
            report_mode=mode,
        )
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
            + self._guide(mode, allowed_routes)
        ).strip()
