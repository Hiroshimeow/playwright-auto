from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
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
    # Compact normal workflow prompts.
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
        expected_report_path: str | None = None,
    ) -> str:
        mode = str(report_mode).strip().lower()
        if mode != "file":
            raise ValueError("inline workflow reports are no longer supported; report_mode must be 'file'")
        routes = tuple(
            validate_workflow_route_key(item)
            if str(item).strip().upper() != "DONE"
            else "DONE"
            for item in (allowed_routes or ("PLAN", "DEV", "TEST", "REVIEW", "AUDIT", "PAUSE", "DONE"))
        )
        report_path = (
            str(expected_report_path).strip()
            if expected_report_path is not None
            else self.naming_rule()
        )
        if not report_path:
            raise ValueError("expected_report_path must not be empty")
        return (
            "Return exactly one valid JSON object:\n"
            + '{"route":"'
            + "|".join(routes)
            + '","handoff":"'
            + report_path
            + '"}'
        )

    @staticmethod
    def filesystem_scope(workspace: str, report_path: str) -> str:
        path_type = PureWindowsPath if PureWindowsPath(workspace).drive else PurePosixPath
        destination = path_type(workspace) / report_path
        return (
            f"FILESYSTEM_SCOPE: Resolve relative paths under {workspace}, not its parent or Git's top-level directory.\n"
            f"Absolute report destination: {destination}\n"
            "Use this exact working directory for MCP filesystem/tool calls. Keep the route handoff relative as specified."
        )

    @staticmethod
    def current_role_execution(
        role: str,
        *,
        physical_role: str,
        source_physical_role: str | None,
    ) -> str:
        normalized = validate_workflow_route_key(role)
        continuation = ""
        if source_physical_role and str(source_physical_role).strip() == str(physical_role).strip():
            continuation = f" Continue the previous {normalized} turn; do not restate it."
        directive = "Work first." + continuation + " Write the report, then return exactly one valid route JSON object."
        if normalized == "PLAN":
            directive += (
                "\n\nPLAN_CLOSURE: Treat the handoff and accepted role reports as closure evidence. After a clean REVIEW and satisfied task acceptance with no concrete remaining in-scope work, "
                "route DONE now. Do not route DEV merely because PLAN itself did not run tests, or for continued progression, closure preparation, or redundant validation. "
                "If not routing DONE, identify a concrete unmet acceptance criterion or fixable defect from the current evidence."
            )
        elif normalized == "REVIEW":
            directive += " Clean work routes to PLAN; route DEV only for a concrete reproducible defect."
        return directive

    @staticmethod
    def tool_authority(workspace: str) -> str:
        platform_hint = (
            "This workspace is Windows: use @mcp-thinkbook."
            if PureWindowsPath(workspace).drive
            else "This workspace is Linux: use @mcp-g8 or @mcp-docker as authorized."
        )
        return (
            "Use @mcp-g8 or @mcp-docker on Linux, and @mcp-thinkbook on Windows.\n"
            + platform_hint
            + "\nDo not claim tool or workspace access is unavailable until an actual tool call fails."
        )

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
        expected_report_path: str | None = None,
        constructor_sent_generation: int | None = None,
        conversation_generation: int = 0,
        report_mode: str = "file",
        constructor_text: str | None = None,
        is_system_role: bool = True,
        bootstrap_inherited: bool = False,
        bounded_context: str | None = None,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        allowed = tuple(str(item).strip().upper() for item in allowed_routes)
        if role not in allowed:
            raise ValueError(f"workflow role {role!r} is not in allowed_routes")
        handoff = str(handoff).strip()
        goal = str(goal).strip()
        if source_physical_role is not None and len(goal) > 700:
            goal = " ".join(goal.split())[:700].rsplit(" ", 1)[0] + " ..."
        expected_report_path = (
            str(expected_report_path).strip()
            if expected_report_path is not None
            else self.naming_rule()
        )
        if not handoff or not goal or not expected_report_path:
            raise ValueError("handoff, goal, and expected_report_path must not be empty")
        generation = int(conversation_generation)
        include = constructor_sent_generation != generation
        prompt_routes = tuple(
            item
            for item in allowed
            if item != "PAUSE" and (item != "DONE" or role == "PLAN")
        )
        sections = [
            f"You are {role} for the team {str(team).strip()} | {str(task_id).strip()}:",
            f"Turn: {int(turn)}\nWorkspace: {str(workspace).strip()}\nGoal: {goal}\nHandoff: {handoff}",
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
        sections.append(self.tool_authority(str(workspace).strip()))
        if bounded_context:
            sections.append("RECOVERY_CONTEXT\n" + str(bounded_context).strip())
        sections.append(
            "Inspect the handoff, current diff/source, and fresh evidence with the authorized tool now. "
            "Preserve unrelated dirty work. Do not commit, push, merge, reset, stash, clean, or switch branches unless explicitly authorized. "
            "Establish the root cause before changing or judging code; prefer the least invasive complete solution, never a smaller patch that leaves the cause intact."
        )
        sections.append(
            "REPORT\n"
            f"Write the complete report to exactly: {expected_report_path}\n"
            "The report must exist and contain concrete work evidence before routing."
        )
        sections.append(
            "ROUTE\n"
            + self._guide(
                report_mode,
                prompt_routes,
                expected_report_path=expected_report_path,
            )
            + "\nPAUSE is exceptional, not a normal route: use it only after an authorized tool/action actually fails on a concrete external/manual prerequisite, and only after writing a valid report with `block_code=...` and `resume_condition=...`."
        )
        sections.append(
            self.current_role_execution(
                role,
                physical_role=str(physical_role).strip(),
                source_physical_role=source_physical_role,
            )
        )
        return BuiltPrompt("\n\n".join(sections).strip(), include, generation)

    def build_solo(
        self,
        *,
        task_name: str,
        logical_role: str,
        goal: str = "",
        continuation: bool = False,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        name = str(task_name).strip()
        if not name:
            raise ValueError("solo task name must not be empty")
        normalized_goal = str(goal or "").strip()
        if continuation:
            sections = ["Continue from the current state."]
            if normalized_goal:
                sections.append(f"Goal: {normalized_goal}")
            sections.append(
                "ROUTE\n"
                "Return exactly one valid JSON object using only:\n"
                '{"route":"DONE","handoff":"<concise final result>"}\n'
                "or\n"
                '{"route":"PAUSE","handoff":"<reason/current state>"}'
            )
        else:
            sections = [f"You are {role} for the solo task {name}."]
            if normalized_goal:
                sections.append(f"Goal: {normalized_goal}")
            sections.extend(
                (
                    "Continue the work required by this task.",
                    "ROUTE\n"
                    "When complete, return exactly one JSON object using only:\n"
                    '{"route":"DONE","handoff":"<concise final result>"}\n'
                    "or\n"
                    '{"route":"PAUSE","handoff":"<reason/current state>"}',
                )
            )
        return BuiltPrompt("\n\n".join(sections), False, 0)

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
        expected_report_path: str,
        report_mode: str = "file",
        allowed_routes: Sequence[str] | None = None,
    ) -> str:
        if not str(validation_error).strip():
            raise ValueError("repair requires a validation error")
        exact_report = str(expected_report_path).strip()
        if not exact_report:
            raise ValueError("repair requires an expected report path")
        return "Continue from this state!\nReturn exactly one valid JSON object"

    def report_repair(
        self,
        *,
        task_id: str,
        team: str,
        physical_role: str,
        turn: int,
        route: str,
        handoff: str,
        validation_error: str,
    ) -> str:
        locked_route = str(route).strip().upper()
        locked_handoff = str(handoff).strip()
        error = self._sanitize_validation_error(validation_error)
        if not locked_route or not locked_handoff or not error:
            raise ValueError("report repair requires locked route, handoff, and validation error")
        response = json.dumps(
            {"route": locked_route, "handoff": locked_handoff},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "CDPA_REPORT_REPAIR\n"
            "The route decision is already accepted and locked. Do not redo prior work and do not change the route.\n"
            "Repair only the report contract from work/evidence already completed; do not invent evidence or redo substantive workflow.\n"
            f"Write the complete role report to exactly: {locked_handoff}\n"
            f"Artifact validation error: {error}\n"
            "If the locked route is PAUSE, preserve the concrete resume condition in the report.\n"
            "After the file exists and is non-empty, respond with only this exact JSON:\n"
            + response
        ).strip()
