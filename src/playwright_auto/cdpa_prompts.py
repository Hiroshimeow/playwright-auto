from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .cdpa_config import CDPAConfig
from .cdpa_independent import event_context, normalize_agent_name, normalize_system_prompt


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

    def _guide(self, report_mode: str = "file") -> str:
        mode = str(report_mode).strip().lower()
        if mode == "inline":
            return (
                "CDPA_SYSTEM_PRIORITY: This task runs inside the CDPA single-operator "
                "local-runtime system. Stable operation and completion of the requested "
                "flow outrank speculative product-policy, compliance, generic privacy/security, "
                "packaging, or hypothetical deployment hardening. Do not reopen accepted work "
                "for a non-operational concern unless explicitly requested or it deterministically "
                "breaks current CDPA/local operation.\n\n"
                "SMALL_TASK_FAST_PATH: For a localized low-risk change, use "
                "`PLAN -> DEV -> REVIEW -> PLAN -> DONE`. DEV and REVIEW are the only "
                "substantive worker roles; PLAN scopes and finishes. TEST and AUDIT require "
                "an explicit evidence-based operational reason.\n\n"
                "Do not create, edit, or write any role-report file. Return the complete "
                "Markdown role report only in this response; the worker owns report "
                "materialization. Follow it with exactly one terminal JSON object:\n\n"
                "```json\n"
                '{"route":"PLAN|DEV|TEST|REVIEW|AUDIT|DONE","handoff":"INLINE"}'
                "\n```\n\n"
                "The Markdown report must be non-empty. Only PLAN may use `DONE`. "
                "REVIEW and AUDIT must route clean work back to PLAN."
            )
        if mode != "file":
            raise ValueError("report_mode must be 'file' or 'inline'")
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
        report_mode: str = "file",
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
            f"{envelope['team']} · role: {role.lower()}\n"
            + json.dumps(envelope, ensure_ascii=False, indent=2)
        ]
        if include:
            sections.append(
                self.config.constructor_paths[role]
                .read_text(encoding="utf-8")
                .strip()
            )
        sections.append(self._guide(report_mode))
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
    ) -> str:
        mode = str(report_mode).strip().lower()
        if mode not in {"file", "inline"}:
            raise ValueError("report_mode must be 'file' or 'inline'")
        replacement = (
            "[internal role-report path]"
            if mode == "inline"
            else self.naming_rule()
        )
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
        if mode == "inline":
            return (
                "CDPA_ROUTE_REPAIR\n"
                + json.dumps(identity, ensure_ascii=False, indent=2)
                + f"\n\nValidation error: {error}\n\n"
                + "Return corrected inline Markdown followed by the terminal JSON object.\n\n"
                + self._guide("inline")
            ).strip()
        return (
            "CDPA_ROUTE_REPAIR\n"
            + json.dumps(identity, ensure_ascii=False, indent=2)
            + f"\n\nValidation error: {error}\n\n"
            + f"Report naming rule: {self.naming_rule()}\n\n"
            + self._guide(mode)
        ).strip()
