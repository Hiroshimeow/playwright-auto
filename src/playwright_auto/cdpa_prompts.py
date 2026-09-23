from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
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


_EXPLICIT_TOOL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_-])(?:@mcp-[A-Za-z0-9._-]+|@md(?:\s+device)?\s+[A-Za-z0-9._-]+|md:[A-Za-z0-9._-]+)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def explicit_tool_literals(text: str) -> tuple[str, ...]:
    literals: list[str] = []
    seen: set[str] = set()
    for match in _EXPLICIT_TOOL_LITERAL.finditer(str(text or "")):
        literal = match.group(0).rstrip(".,;:!?)]}")
        key = literal.casefold()
        if key not in seen:
            seen.add(key)
            literals.append(literal)
    return tuple(literals)


def _explicit_tool_section(text: str) -> str:
    literals = explicit_tool_literals(text)
    if not literals:
        return ""
    return (
        "EXPLICIT_TOOL_REQUIREMENTS\n"
        "Preserve every exact user tool literal (ordered, deduplicated): "
        + " | ".join(literals)
        + "\nUse the exact named tool/device target for the operations it governs; "
        "do not substitute another connector, device, host, or path-derived target unless the task explicitly changes it."
    )


MAX_REPOSITORY_SKILLS = 12
MAX_REPOSITORY_SKILL_FILE_BYTES = 8192
MAX_REPOSITORY_SKILL_FRONTMATTER_CHARS = 2048
MAX_REPOSITORY_SKILL_NAME_CHARS = 64
MAX_REPOSITORY_SKILL_DESCRIPTION_CHARS = 220
MAX_REPOSITORY_SKILL_CATALOG_CHARS = 4096
_REPOSITORY_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True)
class RepositorySkill:
    name: str
    description: str


def _frontmatter_scalar(raw: str) -> str:
    value = str(raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return " ".join(value.split())


def _repository_skill_metadata(path: Path, directory_name: str) -> RepositorySkill | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_REPOSITORY_SKILL_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    normalized = text.replace("\r\n", "\n")
    end = normalized.find("\n---\n", 4)
    if end < 0 or end > MAX_REPOSITORY_SKILL_FRONTMATTER_CHARS:
        return None
    fields: dict[str, str] = {}
    for line in normalized[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return None
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key not in {"name", "description"} or key in fields:
            continue
        fields[key] = _frontmatter_scalar(raw_value)
    name = fields.get("name", "")
    description = fields.get("description", "")
    if (
        not name
        or name != directory_name
        or len(name) > MAX_REPOSITORY_SKILL_NAME_CHARS
        or _REPOSITORY_SKILL_NAME.fullmatch(name) is None
        or not description
        or len(description) > MAX_REPOSITORY_SKILL_DESCRIPTION_CHARS
    ):
        return None
    return RepositorySkill(name=name, description=description)


def discover_repository_skills(workspace: str) -> tuple[RepositorySkill, ...]:
    try:
        repository = Path(str(workspace).strip()).resolve()
        skills_root = repository / ".skills"
        if skills_root.is_symlink() or not skills_root.is_dir():
            return ()
        entries = sorted(skills_root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return ()

    found: list[RepositorySkill] = []
    for entry in entries[:MAX_REPOSITORY_SKILLS]:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if skill_file.is_symlink():
                continue
            resolved = skill_file.resolve()
            resolved.relative_to(repository)
        except (OSError, ValueError):
            continue
        metadata = _repository_skill_metadata(resolved, entry.name)
        if metadata is not None:
            found.append(metadata)
    return tuple(found)


def _repository_skills_section(workspace: str, selection_text: str = "") -> str:
    skills = discover_repository_skills(workspace)
    if not skills:
        return ""

    selected_text = str(selection_text or "")
    selected = tuple(
        skill
        for skill in skills
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(skill.name)}(?![A-Za-z0-9_-])",
            selected_text,
            re.IGNORECASE,
        )
    )
    selected_names = {skill.name for skill in selected}
    lines = ["REPOSITORY_SKILLS"]

    def append_complete(line: str) -> bool:
        candidate = "\n".join((*lines, line))
        if len(candidate) > MAX_REPOSITORY_SKILL_CATALOG_CHARS:
            return False
        lines.append(line)
        return True

    append_complete(
        "Use the smallest matching repository-local skill before substantive work. "
        "Read only its SKILL.md and the minimum referenced files needed; do not preload skill bodies."
    )
    append_complete(
        "If the task or goal explicitly names a listed skill, load that exact skill first."
    )

    for skill in selected:
        append_complete(
            f"REQUIRED_REPOSITORY_SKILL: Load .skills/{skill.name}/SKILL.md before substantive work."
        )

    for skill in (*selected, *(item for item in skills if item.name not in selected_names)):
        append_complete(f"- {skill.name}: {skill.description}")

    return "\n".join(lines)


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
        explicit_tool_text: str | None = None,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        allowed = tuple(str(item).strip().upper() for item in allowed_routes)
        if role not in allowed:
            raise ValueError(f"workflow role {role!r} is not in allowed_routes")
        handoff = str(handoff).strip()
        original_goal = str(goal).strip()
        goal = original_goal
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
        explicit_tools = _explicit_tool_section(
            "\n".join(item for item in (original_goal, explicit_tool_text or "") if item)
        )
        if explicit_tools:
            sections.append(explicit_tools)
        repository_skills = _repository_skills_section(str(workspace).strip(), original_goal)
        if repository_skills:
            sections.append(repository_skills)
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
        sections.append(self.tool_authority())
        sections.append(self.filesystem_scope(str(workspace).strip(), expected_report_path))
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
        workspace: str = "",
        continuation: bool = False,
    ) -> BuiltPrompt:
        role = validate_workflow_route_key(logical_role)
        name = str(task_name).strip()
        if not name:
            raise ValueError("solo task name must not be empty")
        normalized_goal = str(goal or "").strip()
        repository_skills = (
            _repository_skills_section(str(workspace).strip(), normalized_goal)
            if str(workspace).strip()
            else ""
        )
        if continuation:
            sections = ["Continue from the current state."]
            if normalized_goal:
                sections.append(f"Goal: {normalized_goal}")
            if repository_skills:
                sections.append(repository_skills)
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
            if repository_skills:
                sections.append(repository_skills)
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
        repository_skills = _repository_skills_section(str(workspace).strip(), prompt)
        if repository_skills:
            sections.append(repository_skills)
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
        tool_intent_text: str = "",
    ) -> str:
        locked_route = str(route).strip().upper()
        locked_handoff = str(handoff).strip()
        error = self._sanitize_validation_error(validation_error)
        if not locked_route or not locked_handoff or not error:
            raise ValueError("report repair requires locked route, handoff, and validation error")
        explicit_tools = _explicit_tool_section(tool_intent_text)
        route_context = (
            "The prior route was PAUSE; report repair may repair the artifact only and must preserve PAUSE.\n"
            if locked_route == "PAUSE"
            else f"The prior route was {locked_route}; treat it as historical context, not a lock.\n"
        )
        route_instruction = (
            "Preserve route PAUSE after repairing the artifact.\n"
            if locked_route == "PAUSE"
            else "Choose the route that is correct now from the completed evidence.\n"
        )
        response_instruction = (
            "After the file exists and is non-empty, respond with exactly one JSON object. Keep the handoff path exact and return route PAUSE.\n"
            if locked_route == "PAUSE"
            else "After the file exists and is non-empty, respond with exactly one JSON object. Keep the handoff path exact and set route to the role you actually intend next.\n"
        )
        return (
            "CDPA_REPORT_REPAIR\n"
            + (explicit_tools + "\n" if explicit_tools else "")
            + route_context
            + "Repair only the report contract from work/evidence already completed; do not invent evidence or redo substantive workflow.\n"
            + f"Write the complete role report to exactly: {locked_handoff}\n"
            + f"Artifact validation error: {error}\n"
            + route_instruction
            + "A transient tool/device/browser disconnect is not a PAUSE condition; retry boundedly or route onward when the prerequisite is available.\n"
            + response_instruction
            + f"Example shape: {{\"route\":\"{locked_route}\",\"handoff\":\"{locked_handoff}\"}}"
        ).strip()
