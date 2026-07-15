from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .chatgpt import RateLimitBlockedError
from .durable_blocks import DurableSendBlock
from .file_lock import fsync_parent_directory
from .team import (
    TeamRoundSpec,
    TeamTranscript,
    build_team_prompt,
    resolve_role_selectors,
)
from .workflow import WorkflowBlock, WorkflowContext
from .workspace import ChatGPTWorkspace


class TeamCheckpointMismatchError(RuntimeError):
    pass


class TeamRoundError(RuntimeError):
    def __init__(self, round_name: str, errors: Mapping[str, str]) -> None:
        self.round_name = round_name
        self.errors = dict(errors)
        super().__init__(
            f"team round {round_name!r} failed: "
            + ", ".join(f"{role}={error}" for role, error in self.errors.items())
        )


class TeamRoleExecutor(Protocol):
    async def execute(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        *,
        role: str,
        prompt: str,
        round_name: str,
        transcript: TeamTranscript,
    ) -> dict[str, Any]: ...


class DurableTeamRoleExecutor:
    def __init__(
        self,
        *,
        ledger_path: str | Path = ".runtime/chatgpt-team-ledger.json",
        response_timeout_ms: int | None = 180_000,
        stable_ms: int = 1_000,
        poll_ms: int = 100,
        active_reload_after_ms: int | None = 120_000,
        max_attempts: int = 2,
        min_request_interval_seconds: float = 4.0,
        rate_limit_cooldown_seconds: float = 60.0,
        rate_limit_retries: int = 3,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.response_timeout_ms = response_timeout_ms
        self.stable_ms = stable_ms
        self.poll_ms = poll_ms
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must not be negative")
        if rate_limit_cooldown_seconds <= 0:
            raise ValueError("rate_limit_cooldown_seconds must be positive")
        if rate_limit_retries < 0:
            raise ValueError("rate_limit_retries must not be negative")
        self.active_reload_after_ms = active_reload_after_ms
        self.max_attempts = max_attempts
        self.min_request_interval_seconds = float(min_request_interval_seconds)
        self.rate_limit_cooldown_seconds = float(rate_limit_cooldown_seconds)
        self.rate_limit_retries = int(rate_limit_retries)
        self._pace_lock = asyncio.Lock()
        self._cooldown_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._cooldown_until = 0.0

    async def _wait_request_slot(self) -> None:
        async with self._pace_lock:
            now = asyncio.get_running_loop().time()
            target = max(
                self._last_request_started + self.min_request_interval_seconds,
                self._cooldown_until,
            )
            if target > now:
                await asyncio.sleep(target - now)
            self._last_request_started = asyncio.get_running_loop().time()

    async def _cooldown_and_dismiss(self, client: Any) -> None:
        async with self._cooldown_lock:
            now = asyncio.get_running_loop().time()
            if self._cooldown_until <= now:
                self._cooldown_until = now + self.rate_limit_cooldown_seconds
            target = self._cooldown_until
        delay = target - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
        await client.dismiss_known_rate_limit(timeout_ms=15_000)

    async def execute(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        *,
        role: str,
        prompt: str,
        round_name: str,
        transcript: TeamTranscript,
    ) -> dict[str, Any]:
        client = context.client.get(role)
        safe_key = f"{round_name}_{role}".replace("-", "_")
        source_context = {
            "team_round": round_name,
            "role": role,
            "task_id": context.variables.get("task_id"),
            "goal": context.variables.get("goal"),
        }
        role_prompt_hash = hashlib.sha256(
            f"{round_name}\0{role}".encode("utf-8")
        ).hexdigest()
        local_context = WorkflowContext(
            client=client,
            variables=dict(context.variables),
        )
        block = DurableSendBlock(
            prompt,
            ledger_path=self.ledger_path,
            source_context=source_context,
            role_prompt_hash=role_prompt_hash,
            wait_for_response=True,
            max_attempts=self.max_attempts,
            response_timeout_ms=self.response_timeout_ms,
            stable_ms=self.stable_ms,
            poll_ms=self.poll_ms,
            active_reload_after_ms=self.active_reload_after_ms,
            record_key=f"team_record_{safe_key}",
            receipt_key=f"team_receipt_{safe_key}",
            response_key=f"team_response_{safe_key}",
            recovery_key=f"team_recovery_{safe_key}",
            block_id=f"team_send_{safe_key}",
        )
        for rate_limit_attempt in range(self.rate_limit_retries + 1):
            await self._wait_request_slot()
            try:
                async with client.workflow_guard():
                    return await block.run(local_context)
            except RateLimitBlockedError:
                if rate_limit_attempt >= self.rate_limit_retries:
                    raise
                await self._cooldown_and_dismiss(client)
        raise AssertionError("unreachable rate-limit retry loop")


class TeamConversationBlock(WorkflowBlock[ChatGPTWorkspace]):
    """Execute a finite, durable multi-role conversation graph.

    Each round targets exact roles or role groups such as ``DEV*`` and ``REVIEW*``.
    Completed role outputs are checkpointed and skipped on resume. Incomplete roles
    reuse DurableSendBlock, so an ambiguous send boundary never causes blind resend.
    """

    retry_safe = False

    def __init__(
        self,
        rounds: Sequence[TeamRoundSpec],
        *,
        executor: TeamRoleExecutor | None = None,
        transcript_key: str = "team_transcript",
        checkpoint_path: str | Path | None = ".runtime/chatgpt-team/{task_id}.json",
        fail_on_error: bool = True,
        rate_limit_cooldown_seconds: float | None = None,
        rate_limit_retries: int | None = None,
        block_id: str = "team_conversation",
    ) -> None:
        super().__init__(block_id)
        self.rounds = tuple(rounds)
        if not self.rounds:
            raise ValueError("team conversation requires at least one round")
        names = [item.name for item in self.rounds]
        if len(set(names)) != len(names):
            raise ValueError("team round names must be unique")
        self.executor = executor or DurableTeamRoleExecutor()
        default_cooldown = getattr(self.executor, "rate_limit_cooldown_seconds", 60.0)
        default_retries = getattr(self.executor, "rate_limit_retries", 3)
        self.rate_limit_cooldown_seconds = float(
            default_cooldown if rate_limit_cooldown_seconds is None else rate_limit_cooldown_seconds
        )
        self.rate_limit_retries = int(
            default_retries if rate_limit_retries is None else rate_limit_retries
        )
        if self.rate_limit_cooldown_seconds <= 0:
            raise ValueError("rate_limit_cooldown_seconds must be positive")
        if self.rate_limit_retries < 0:
            raise ValueError("rate_limit_retries must not be negative")
        self.transcript_key = transcript_key
        self.checkpoint_path_template = str(checkpoint_path) if checkpoint_path else None
        self.fail_on_error = fail_on_error

    @staticmethod
    def _safe_task_component(task_id: str) -> str:
        compact = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-") or "task"
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
        return f"{compact[:70]}-{digest}"

    def _identity(
        self, context: WorkflowContext[ChatGPTWorkspace]
    ) -> dict[str, Any]:
        goal = str(context.variables.get("goal") or "").strip()
        task_id = str(context.variables.get("task_id") or "").strip()
        if not task_id:
            if not goal:
                raise ValueError("team workflow requires task_id or goal")
            task_id = f"goal-{hashlib.sha256(goal.encode('utf-8')).hexdigest()[:16]}"
            context.variables["task_id"] = task_id
        workflow_version = str(context.variables.get("workflow_version") or "1")
        payload = {
            "task_id": task_id,
            "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "workflow_version": workflow_version,
            "active_roles": sorted(context.client.active_roles),
            "rounds": [
                {
                    "name": item.name,
                    "targets": list(item.targets),
                    "parallel": item.parallel,
                }
                for item in self.rounds
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            **payload,
            "identity_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    @staticmethod
    def _semantic_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_id": str(identity.get("task_id") or ""),
            "goal_sha256": str(identity.get("goal_sha256") or ""),
            "workflow_version": str(identity.get("workflow_version") or ""),
            "active_roles": sorted(str(role) for role in identity.get("active_roles") or []),
            "rounds": identity.get("rounds") or [],
        }

    @classmethod
    def _identity_equivalent(
        cls,
        persisted: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> bool:
        return cls._semantic_identity(persisted) == cls._semantic_identity(current)

    def _checkpoint_path(self, identity: Mapping[str, Any]) -> Path | None:
        if self.checkpoint_path_template is None:
            return None
        safe_task = self._safe_task_component(str(identity["task_id"]))
        rendered = self.checkpoint_path_template.replace("{task_id}", safe_task)
        return Path(rendered).expanduser().resolve()

    def _load_transcript(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        identity: Mapping[str, Any],
        checkpoint_path: Path | None,
    ) -> TeamTranscript:
        current = context.variables.get(self.transcript_key)
        if isinstance(current, TeamTranscript):
            return current
        if isinstance(current, Mapping):
            return TeamTranscript.from_dict(current)
        if checkpoint_path and checkpoint_path.exists():
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if payload.get("version") != 2:
                raise ValueError("unsupported team checkpoint version")
            persisted_identity = payload.get("identity") or {}
            if (
                persisted_identity.get("identity_sha256") != identity.get("identity_sha256")
                and not self._identity_equivalent(persisted_identity, identity)
            ):
                raise TeamCheckpointMismatchError(
                    "team checkpoint belongs to another task, goal, role team, or workflow version"
                )
            return TeamTranscript.from_dict(payload.get("transcript") or {})
        return TeamTranscript()

    def _write_checkpoint(
        self,
        transcript: TeamTranscript,
        identity: Mapping[str, Any],
        path: Path | None,
    ) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(
            {
                "version": 2,
                "identity": dict(identity),
                "transcript": transcript.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_parent_directory(path)

    def _participating_roles(
        self, context: WorkflowContext[ChatGPTWorkspace]
    ) -> tuple[str, ...]:
        selected: list[str] = []
        for spec in self.rounds:
            for role in resolve_role_selectors(
                context.client.active_roles,
                spec.targets,
            ):
                if role not in selected:
                    selected.append(role)
        return tuple(selected)

    async def _prepare_one(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        role: str,
        task_id: str,
    ) -> tuple[str, dict[str, Any]]:
        client = context.client.get(role)
        async with client.workflow_guard():
            result = await client.prepare_task(task_id)
        return role, result

    async def _prepare_team(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        task_id: str,
    ) -> dict[str, dict[str, Any]]:
        roles = self._participating_roles(context)
        for rate_limit_attempt in range(self.rate_limit_retries + 1):
            preflight_results = await asyncio.gather(
                *(context.client.get(role).task_preflight(task_id) for role in roles),
                return_exceptions=True,
            )
            rate_limited = [
                role
                for role, result in zip(roles, preflight_results, strict=True)
                if isinstance(result, RateLimitBlockedError)
            ]
            other_errors = {
                role: f"{type(result).__name__}: {result}"
                for role, result in zip(roles, preflight_results, strict=True)
                if isinstance(result, BaseException)
                and not isinstance(result, RateLimitBlockedError)
            }
            if other_errors:
                raise TeamRoundError("prepare", other_errors)
            if not rate_limited:
                break
            if rate_limit_attempt >= self.rate_limit_retries:
                raise TeamRoundError(
                    "prepare",
                    {
                        role: "RateLimitBlockedError: request rate limit remained after cooldown retries"
                        for role in rate_limited
                    },
                )
            await asyncio.sleep(self.rate_limit_cooldown_seconds)
            dismiss_results = await asyncio.gather(
                *(
                    context.client.get(role).dismiss_known_rate_limit(timeout_ms=15_000)
                    for role in rate_limited
                ),
                return_exceptions=True,
            )
            dismiss_errors = {
                role: f"{type(result).__name__}: {result}"
                for role, result in zip(rate_limited, dismiss_results, strict=True)
                if isinstance(result, BaseException)
            }
            if dismiss_errors:
                raise TeamRoundError("prepare", dismiss_errors)
        prepared = await asyncio.gather(
            *(self._prepare_one(context, role, task_id) for role in roles)
        )
        return {role: result for role, result in prepared}

    async def _execute_one(
        self,
        context: WorkflowContext[ChatGPTWorkspace],
        spec: TeamRoundSpec,
        role: str,
        prompt: str,
        transcript: TeamTranscript,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            result = await self.executor.execute(
                context,
                role=role,
                prompt=prompt,
                round_name=spec.name,
                transcript=transcript,
            )
            return role, result, None
        except Exception as exc:
            return role, None, f"{type(exc).__name__}: {exc}"

    async def run(
        self, context: WorkflowContext[ChatGPTWorkspace]
    ) -> dict[str, Any]:
        identity = self._identity(context)
        checkpoint_path = self._checkpoint_path(identity)
        transcript = self._load_transcript(context, identity, checkpoint_path)
        context.variables[self.transcript_key] = transcript
        context.variables[f"{self.transcript_key}_identity"] = dict(identity)
        if checkpoint_path is not None:
            context.variables[f"{self.transcript_key}_checkpoint"] = str(checkpoint_path)
        task_preparation = await self._prepare_team(
            context,
            str(identity["task_id"]),
        )
        context.variables[f"{self.transcript_key}_task_preparation"] = task_preparation
        round_outputs: list[dict[str, Any]] = []

        for spec in self.rounds:
            targets = resolve_role_selectors(
                context.client.active_roles,
                spec.targets,
            )
            pending = tuple(
                role for role in targets if not transcript.has_success(spec.name, role)
            )
            prompts: dict[str, str] = {}
            for role in pending:
                persisted_prompt = transcript.get_prompt(spec.name, role)
                prompt = (
                    persisted_prompt
                    if persisted_prompt is not None
                    else await build_team_prompt(spec.prompt, context, role, transcript)
                )
                transcript.record_prompt(spec.name, role, prompt)
                prompts[role] = prompt
            context.variables[self.transcript_key] = transcript
            self._write_checkpoint(transcript, identity, checkpoint_path)

            if spec.parallel:
                outcomes = await asyncio.gather(
                    *(
                        self._execute_one(
                            context,
                            spec,
                            role,
                            prompts[role],
                            transcript,
                        )
                        for role in pending
                    )
                )
            else:
                outcomes = []
                for role in pending:
                    outcomes.append(
                        await self._execute_one(
                            context,
                            spec,
                            role,
                            prompts[role],
                            transcript,
                        )
                    )

            errors: dict[str, str] = {}
            completed_now: list[str] = []
            for role, result, error in outcomes:
                if result is not None:
                    transcript.record_success(
                        spec.name,
                        role,
                        prompt=prompts[role],
                        result=result,
                    )
                    completed_now.append(role)
                if error is not None:
                    transcript.record_error(spec.name, role, error)
                    errors[role] = error

            context.variables[self.transcript_key] = transcript
            self._write_checkpoint(transcript, identity, checkpoint_path)
            output = {
                "round": spec.name,
                "targets": list(targets),
                "resumed": [role for role in targets if role not in pending],
                "completed_now": completed_now,
                "errors": errors,
            }
            round_outputs.append(output)
            if errors and self.fail_on_error:
                raise TeamRoundError(spec.name, errors)

        return {
            "rounds": round_outputs,
            "transcript": transcript.to_dict(),
        }
