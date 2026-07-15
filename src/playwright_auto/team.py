from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .workflow import WorkflowContext
from .workspace import ChatGPTWorkspace

TeamPromptBuilder = Callable[
    [WorkflowContext[ChatGPTWorkspace], str, "TeamTranscript"],
    str | Awaitable[str],
]
TeamPromptSource = str | TeamPromptBuilder

_ROUND_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TeamRoundSpec:
    name: str
    targets: tuple[str, ...]
    prompt: TeamPromptSource
    parallel: bool = True

    def __post_init__(self) -> None:
        if not _ROUND_NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "round name must start with a letter and contain only letters, "
                "digits, '_' or '-'"
            )
        if not self.targets:
            raise ValueError("team round must target at least one role selector")
        object.__setattr__(self, "targets", tuple(str(item) for item in self.targets))


@dataclass
class TeamTranscript:
    rounds: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    prompts: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, dict[str, str]] = field(default_factory=dict)

    def record_prompt(self, round_name: str, role: str, prompt: str) -> None:
        existing = self.prompts.setdefault(round_name, {}).get(role)
        if existing is not None and existing != prompt:
            raise ValueError(
                f"persisted prompt changed for round={round_name!r} role={role!r}"
            )
        self.prompts[round_name][role] = prompt

    def get_prompt(self, round_name: str, role: str) -> str | None:
        return self.prompts.get(round_name, {}).get(role)

    def record_success(
        self,
        round_name: str,
        role: str,
        *,
        prompt: str,
        result: Mapping[str, Any],
    ) -> None:
        self.record_prompt(round_name, role, prompt)
        self.rounds.setdefault(round_name, {})[role] = {
            "prompt": prompt,
            "result": dict(result),
        }
        self.errors.setdefault(round_name, {}).pop(role, None)

    def record_error(self, round_name: str, role: str, error: str) -> None:
        self.errors.setdefault(round_name, {})[role] = str(error)

    def has_success(self, round_name: str, role: str) -> bool:
        return role in self.rounds.get(round_name, {})

    def get_response(self, round_name: str, role: str) -> dict[str, Any] | None:
        item = self.rounds.get(round_name, {}).get(role)
        if not item:
            return None
        result = item.get("result") or {}
        response = result.get("response")
        return dict(response) if isinstance(response, Mapping) else None

    def response_text(self, round_name: str, role: str) -> str:
        response = self.get_response(round_name, role)
        return str(response.get("text") or "") if response else ""

    def completed_roles(self, round_name: str) -> tuple[str, ...]:
        return tuple(self.rounds.get(round_name, {}))

    def render(
        self,
        *,
        round_names: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        max_chars_per_response: int = 8_000,
        max_total_chars: int = 48_000,
    ) -> str:
        selected_rounds = tuple(round_names) if round_names is not None else tuple(self.rounds)
        role_filter = set(roles) if roles is not None else None
        sections: list[str] = []
        total = 0
        for round_name in selected_rounds:
            for role, item in self.rounds.get(round_name, {}).items():
                if role_filter is not None and role not in role_filter:
                    continue
                result = item.get("result") or {}
                response = result.get("response") or {}
                text = str(response.get("text") or "")
                if not text and int(response.get("image_count") or 0) > 0:
                    text = "[image response]"
                text = text[:max_chars_per_response]
                section = f"## {round_name} / {role}\n{text}".strip()
                remaining = max_total_chars - total
                if remaining <= 0:
                    return "\n\n".join(sections)
                section = section[:remaining]
                sections.append(section)
                total += len(section) + 2
        return "\n\n".join(sections)

    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "prompts": self.prompts,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TeamTranscript":
        raw_rounds = value.get("rounds") or {}
        raw_prompts = value.get("prompts") or {}
        raw_errors = value.get("errors") or {}
        if (
            not isinstance(raw_rounds, Mapping)
            or not isinstance(raw_prompts, Mapping)
            or not isinstance(raw_errors, Mapping)
        ):
            raise TypeError("team transcript rounds/prompts/errors must be mappings")
        rounds = {
            str(round_name): {
                str(role): dict(item)
                for role, item in dict(role_items).items()
            }
            for round_name, role_items in dict(raw_rounds).items()
        }
        prompts = {
            str(round_name): {
                str(role): str(prompt)
                for role, prompt in dict(role_prompts).items()
            }
            for round_name, role_prompts in dict(raw_prompts).items()
        }
        errors = {
            str(round_name): {
                str(role): str(error)
                for role, error in dict(role_errors).items()
            }
            for round_name, role_errors in dict(raw_errors).items()
        }
        return cls(rounds=rounds, prompts=prompts, errors=errors)


async def build_team_prompt(
    source: TeamPromptSource,
    context: WorkflowContext[ChatGPTWorkspace],
    role: str,
    transcript: TeamTranscript,
) -> str:
    raw = source(context, role, transcript) if callable(source) else source
    value = await raw if inspect.isawaitable(raw) else raw
    prompt = str(value).strip()
    if not prompt:
        raise ValueError(f"team prompt for role {role!r} must not be empty")
    return prompt


def resolve_role_selectors(
    active_roles: Sequence[str], selectors: Sequence[str]
) -> tuple[str, ...]:
    active = tuple(active_roles)
    selected: list[str] = []
    for selector in selectors:
        selector = str(selector).strip()
        if not selector:
            raise ValueError("role selector must not be empty")
        if selector == "*":
            matches = active
        elif selector.endswith("*"):
            base = re.escape(selector[:-1])
            pattern = re.compile(rf"^{base}(?:\d+)?$")
            matches = tuple(role for role in active if pattern.fullmatch(role))
        else:
            matches = (selector,) if selector in active else ()
        if not matches:
            raise ValueError(
                f"role selector {selector!r} matched no active roles: {list(active)!r}"
            )
        for role in matches:
            if role not in selected:
                selected.append(role)
    return tuple(selected)
