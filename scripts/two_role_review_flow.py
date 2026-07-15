#!/usr/bin/env python3
"""Run one durable three-turn review exchange between exactly two visible roles."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright_auto.chatgpt import validate_page_role
from playwright_auto.runner import run_chatgpt_loop
from playwright_auto.workflow_api import (
    DurableTeamRoleExecutor,
    LoopOptions,
    TeamConversationBlock,
    TeamRoundSpec,
    Workflow,
)

_ROLES_ENV = "PLAYWRIGHT_AUTO_REVIEW_ROLES_JSON"
_TASK_ENV = "PLAYWRIGHT_AUTO_REVIEW_TASK"
_REPO_ENV = "PLAYWRIGHT_AUTO_REVIEW_REPO"
_TASK_ID_ENV = "PLAYWRIGHT_AUTO_REVIEW_TASK_ID"
_LEDGER_ENV = "PLAYWRIGHT_AUTO_REVIEW_LEDGER"
_CHECKPOINT_ENV = "PLAYWRIGHT_AUTO_REVIEW_CHECKPOINT"


def validate_roles(roles: tuple[str, ...] | list[str]) -> tuple[str, str]:
    if len(roles) != 2:
        raise ValueError("review flow requires exactly two roles")
    first, second = (validate_page_role(str(role)) for role in roles)
    if first == second:
        raise ValueError("review roles must be distinct")
    return first, second


def _roles_from_environment() -> tuple[str, str]:
    raw = os.environ.get(_ROLES_ENV, '["REVIEW", "REVIEW1"]')
    value = json.loads(raw)
    if not isinstance(value, list):
        raise TypeError(f"{_ROLES_ENV} must contain a JSON array")
    return validate_roles(tuple(str(item) for item in value))


def _repo_from_environment() -> Path:
    raw = os.environ.get(_REPO_ENV, ".")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"review repository does not exist: {path}")
    return path


def _task_from_environment() -> str:
    task = os.environ.get(_TASK_ENV, "Review the target repository").strip()
    if not task:
        raise ValueError("review task must not be empty")
    return task


def _task_id_from_environment(task: str) -> str:
    configured = os.environ.get(_TASK_ID_ENV, "").strip()
    if configured:
        return configured
    slug = re.sub(r"[^A-Za-z0-9]+", "-", task).strip("-").lower()[:48] or "review"
    return f"two-review-{time.strftime('%Y%m%d-%H%M%S')}-{slug}"


WORKSPACE_ROLES = _roles_from_environment()
WORKSPACE_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_AUTO_WORKSPACE_TIMEOUT_MS", "30000"))
_REPO_PATH = _repo_from_environment()
_TASK = _task_from_environment()
_TASK_ID = _task_id_from_environment(_TASK)

VARIABLES = {
    "task_id": _TASK_ID,
    "goal": _TASK,
    "repo_path": str(_REPO_PATH),
    "workflow_version": "two-role-review-v1",
}


def _rules(ctx: Any) -> str:
    return f"""TARGET REPOSITORY:
{ctx.require('repo_path')}

REVIEW TASK:
{ctx.require('goal')}

OPERATING RULES:
- Use @mcp-g8 and set the working directory to the target repository.
- Read AGENTS.md before inspecting or running commands.
- Do not modify, stage, reset, clean, commit, or deploy anything.
- Preserve all current user and runtime changes.
- Inspect actual files and run only safe read-only checks or tests.
- Report only evidence-backed findings with file:line references and exact commands.
- Prioritize correctness, regressions, backward compatibility, data integrity, security,
  concurrency, recovery, performance, and operational risk.
- Do not accept another reviewer's claims without independently checking them.
"""


async def _initial_prompt(ctx: Any, role: str, transcript: Any) -> str:
    del transcript
    return f"""You are {role}, the first independent reviewer.

{_rules(ctx)}
Perform a broad but concrete review. Start from Git status and repository instructions,
then inspect the most risk-bearing code paths. Return findings ordered by severity. For
each finding include evidence, impact, and a reproducible verification path. Explicitly
state areas inspected and areas not verified.
"""


async def _challenge_prompt(ctx: Any, role: str, transcript: Any) -> str:
    previous = transcript.render(round_names=("initial_review",))
    return f"""You are {role}, the second independent reviewer.

{_rules(ctx)}
FIRST REVIEW:
{previous}

Independently verify every material claim from the first review. Mark each as CONFIRMED,
REJECTED, or UNVERIFIED with concrete evidence. Then search for important defects the
first reviewer missed. Challenge severity and assumptions where warranted. End with a
ranked list of only the findings that should survive into the final report.
"""


async def _final_prompt(ctx: Any, role: str, transcript: Any) -> str:
    previous = transcript.render(round_names=("initial_review", "challenge_review"))
    return f"""You are {role}, returning for the final adjudication.

{_rules(ctx)}
FULL REVIEW EXCHANGE:
{previous}

Re-check disputed or high-severity items with @mcp-g8. Produce one consolidated final
review, not a summary of opinions. Include only confirmed findings or clearly label items
that remain unverified. Use this structure:
1. Findings ordered by severity, each with file:line, evidence, impact, and exact remedy.
2. Rejected claims and why they were rejected.
3. Verification commands actually run and their results.
4. Remaining risks and unreviewed scope.
End with exactly one line:
FINAL_VERDICT: PASS
or
FINAL_VERDICT: FINDINGS
or
FINAL_VERDICT: INCONCLUSIVE
"""


_ROUNDS = (
    TeamRoundSpec("initial_review", (WORKSPACE_ROLES[0],), _initial_prompt, parallel=False),
    TeamRoundSpec("challenge_review", (WORKSPACE_ROLES[1],), _challenge_prompt, parallel=False),
    TeamRoundSpec("final_review", (WORKSPACE_ROLES[0],), _final_prompt, parallel=False),
)

WORKFLOW = Workflow(
    "two-role-review",
    [
        TeamConversationBlock(
            _ROUNDS,
            executor=DurableTeamRoleExecutor(
                ledger_path=Path(
                    os.environ.get(
                        _LEDGER_ENV,
                        ".runtime/two-role-review-ledger.json",
                    )
                ),
                response_timeout_ms=600_000,
                active_reload_after_ms=180_000,
                min_request_interval_seconds=5.0,
            ),
            checkpoint_path=Path(
                os.environ.get(
                    _CHECKPOINT_ENV,
                    ".runtime/two-role-review/{task_id}.json",
                )
            ),
        )
    ],
)

LOOP = LoopOptions(
    max_iterations=1,
    interval_seconds=0,
    continue_on_error=False,
    stop_file=Path(".runtime/STOP_TWO_ROLE_REVIEW"),
    checkpoint_path=Path(".runtime/two-role-review-loop.json"),
)


def _final_report(payload: dict[str, Any], final_role: str) -> str | None:
    transcript = payload.get("variables", {}).get("team_transcript", {})
    try:
        return str(
            transcript["rounds"]["final_review"][final_role]["result"]["response"]["text"]
        )
    except (KeyError, TypeError):
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one durable three-turn review exchange between two ChatGPT roles"
    )
    parser.add_argument("--roles", nargs=2, required=True, metavar=("ROLE_A", "ROLE_B"))
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    roles = validate_roles(tuple(args.roles))
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"repository does not exist: {repo}")
    task = args.task.strip()
    if not task:
        raise SystemExit("task must not be empty")
    task_id = args.task_id or _task_id_from_environment(task)

    os.environ[_ROLES_ENV] = json.dumps(list(roles))
    os.environ[_TASK_ENV] = task
    os.environ[_REPO_ENV] = str(repo)
    os.environ[_TASK_ID_ENV] = task_id

    exit_code, payload = asyncio.run(run_chatgpt_loop(Path(__file__), args.cdp))
    payload = dict(payload)
    payload["task_id"] = task_id
    payload["roles"] = list(roles)
    payload["repo_path"] = str(repo)
    payload["final_role"] = roles[0]
    payload["final_report"] = _final_report(payload, roles[0])
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
