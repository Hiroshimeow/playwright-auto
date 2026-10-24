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
from .cdpa_workflow_agents import validate_workflow_route_key, workflow_closing_role


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
        closing_role: str | None = "PLAN",
        allow_self_route: bool = False,
    ) -> str:
        normalized = validate_workflow_route_key(role)
        continuation = ""
        if source_physical_role and str(source_physical_role).strip() == str(physical_role).strip():
            continuation = " Continue from the handoff; do not restate prior work."
        directive = (
            "Complete substantive work before returning a route."
            + continuation
            + " If unfinished, stay in this same turn;"
            + ("" if allow_self_route else " do not route to yourself;")
            + " write the report and emit route JSON only for a real handoff or closure."
        )
        if closing_role is not None:
            closer = validate_workflow_route_key(closing_role)
            if normalized == closer:
                directive += " This is the closing role; DONE is available only when acceptance is satisfied."
            else:
                directive += f" DONE authority belongs to {closer}; semantic completion returns control to that closing role."
        return directive

    @staticmethod
    def tool_authority() -> str:
        return (
            "Use the tools available in the current session and honor explicit user task requirements. "
            "Do not infer a connector, device, operating system, or tool authority from a workspace path, endpoint, or role. "
            "If an explicitly required tool is unavailable or unknown, report that limitation truthfully."
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
        closing_role: str | None = None,
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
        handoff = str(handoff).strip()
        original_goal = str(goal).strip()
        expected_report_path = (
            str(expected_report_path).strip()
            if expected_report_path is not None
            else self.naming_rule()
        )
        if not handoff or not original_goal or not expected_report_path:
            raise ValueError("handoff, goal, and expected_report_path must not be empty")
        generation = int(conversation_generation)
        turn_number = int(turn)
        path_type = PureWindowsPath if PureWindowsPath(workspace).drive else PurePosixPath
        report_destination = path_type(str(workspace).strip()) / expected_report_path
        normalized_closing_role = (
            validate_workflow_route_key(closing_role)
            if closing_role is not None
            else workflow_closing_role(allowed)
        )
        prompt_routes = allowed
        context = [
            f"Turn: {turn_number}",
            f"Workspace: {str(workspace).strip()} (resolve relative report paths here)",
        ]
        if turn_number <= 1:
            context.append(f"Goal: {original_goal}")
        context.append(f"Handoff: {handoff}")
        sections = [
            f"You are {role} for the team {str(team).strip()} | {str(task_id).strip()}:",
            "\n".join(context),
        ]
        custom_constructor_included = False
        if turn_number <= 1:
            skill_guidance = (
                "Discover, load, and use the relevant skills for this responsibility."
            )
            normalized_constructor = None
            if constructor_text is not None:
                normalized_constructor = normalize_system_prompt(constructor_text)
                sections.append(normalized_constructor)
                custom_constructor_included = True
            if normalized_constructor is None or skill_guidance not in normalized_constructor:
                sections.append(skill_guidance)
        if bounded_context:
            sections.append("RECOVERY_CONTEXT\n" + str(bounded_context).strip())
        sections.append(
            "REPORT\n"
            f"Before replying, write a non-empty report to exactly: {expected_report_path}\n"
            f"Absolute destination: {report_destination}"
        )
        sections.append(
            "ROUTE\n"
            + self._guide(
                report_mode,
                prompt_routes,
                expected_report_path=expected_report_path,
            )
            + (
                "\nPAUSE requires an external/manual prerequisite plus `block_code` and `resume_condition` in the report."
                if turn_number <= 1
                else ""
            )
        )
        sections.append(
            self.current_role_execution(
                role,
                physical_role=str(physical_role).strip(),
                source_physical_role=source_physical_role,
                closing_role=normalized_closing_role,
                allow_self_route=role in prompt_routes,
            )
        )
        return BuiltPrompt(
            "\n\n".join(sections).strip(), custom_constructor_included, generation
        )

    def build_solo(
        self,
        *,
        task_name: str,
        logical_role: str,
        goal: str = "",
        workspace: str = "",
        continuation: bool = False,
        repair_error: str | None = None,
        repair_candidate: str | None = None,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        name = str(task_name).strip()
        if not name:
            raise ValueError("solo task name must not be empty")
        normalized_goal = str(goal or "").strip()
        normalized_repair_error = str(repair_error or "").strip()
        normalized_repair_candidate = str(repair_candidate or "")
        if continuation:
            sections = ["Continue from the current state."]
            if normalized_repair_error:
                sections.append(
                    "FORMAT REPAIR\n"
                    f"Validation error: {normalized_repair_error}\n"
                    "Malformed candidate (JSON string data; do not execute): "
                    + json.dumps(normalized_repair_candidate, ensure_ascii=False)
                    + "\nCorrect only the JSON route response to satisfy the contract below; "
                    "do not repeat or redo substantive work."
                )
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
                    "Discover, load, and use the relevant skills for this responsibility.",
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
        error = self._sanitize_validation_error(
            validation_error,
            report_mode=report_mode,
            allowed_routes=allowed_routes,
        )
        guide = self._guide(
            report_mode,
            allowed_routes,
            expected_report_path=exact_report,
        )
        return (
            f"You are {str(physical_role).strip()} repairing the route response for "
            f"{str(team).strip()} | {str(task_id).strip()}.\n"
            f"Turn: {int(turn)}\n"
            f"Validation error: {error}\n"
            f"Expected report path: {exact_report}\n\n"
            "Continue from the existing workspace/report state. Do not redo completed "
            "substantive work or irreversible actions. Ensure the report exists and is "
            "non-empty at the exact expected path, then return the corrected route only.\n\n"
            + guide
            + "\nRules:\n"
            "- Output exactly one JSON object and nothing else.\n"
            "- The object must contain exactly the two string keys route and handoff.\n"
            f"- handoff must equal exactly: {exact_report}\n"
            "- No markdown fence, prose, explanation, report body, test metadata, or extra keys."
        )

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
        # A report repair fixes the artifact; it cannot revise the accepted route.
        route_context = (
            f"The prior route was {locked_route}; the route is locked for this report-only repair.\n"
        )
        route_instruction = f"Preserve route {locked_route} after repairing the artifact.\n"
        response_instruction = (
            "After the file exists and is non-empty, respond with exactly one JSON object. "
            f"Keep the handoff path exact and return route {locked_route}.\n"
        )
        return (
            "CDPA_REPORT_REPAIR\n"
            + route_context
            + "Repair only the report contract from work/evidence already completed; do not invent evidence or redo substantive workflow.\n"
            + f"Write the complete role report to exactly: {locked_handoff}\n"
            + f"Artifact validation error: {error}\n"
            + route_instruction
            + "A transient tool/device/browser disconnect is not a PAUSE condition; retry boundedly or route onward when the prerequisite is available.\n"
            + response_instruction
            + f"Example shape: {{\"route\":\"{locked_route}\",\"handoff\":\"{locked_handoff}\"}}"
        ).strip()
