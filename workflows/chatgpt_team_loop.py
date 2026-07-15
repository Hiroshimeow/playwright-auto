"""Stable visible multi-role team workflow.

Version 1 preserves the original round graph for existing task manifests.
Version 2 uses deterministic per-role assignments and independent re-verification.
"""

import json
import os
import re
from pathlib import Path

from playwright_auto.workflow_api import *  # noqa: F403

_DEFAULT_TEAM = {
    "PLAN": 1,
    "DEV": 2,
    "REVIEW": 1,
    "TEST": 1,
}


def _team_from_environment():
    raw = os.environ.get("PLAYWRIGHT_AUTO_TEAM_JSON")
    if not raw:
        return dict(_DEFAULT_TEAM)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("PLAYWRIGHT_AUTO_TEAM_JSON must contain a JSON object")
    return value


WORKSPACE_TEAM = _team_from_environment()
WORKSPACE_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_AUTO_WORKSPACE_TIMEOUT_MS", "30000"))
_WORKFLOW_VERSION = os.environ.get("PLAYWRIGHT_AUTO_WORKFLOW_VERSION", "2").strip() or "2"
if _WORKFLOW_VERSION not in {"1", "2"}:
    raise ValueError(f"unsupported team workflow version {_WORKFLOW_VERSION!r}")

VARIABLES = {
    "task_id": os.environ.get("PLAYWRIGHT_AUTO_TASK_ID", "replace-task-id"),
    "goal": os.environ.get("PLAYWRIGHT_AUTO_GOAL", "Replace this goal"),
    "workflow_version": _WORKFLOW_VERSION,
}


def _role_sort_key(role: str, base: str) -> tuple[int, str]:
    match = re.fullmatch(rf"{re.escape(base)}(\d*)", role)
    if not match:
        return (10_000, role)
    return (int(match.group(1) or 0), role)


def _roles(ctx, base: str) -> tuple[str, ...]:
    pattern = re.compile(rf"^{re.escape(base)}(?:\d+)?$")
    return tuple(
        sorted(
            (role for role in ctx.client.active_roles if pattern.fullmatch(role)),
            key=lambda role: _role_sort_key(role, base),
        )
    )


def _assignment_map(ctx, transcript):
    dev_roles = _roles(ctx, "DEV")
    plan_text = transcript.response_text("plan", "PLAN")
    assignments = parse_route_map(plan_text, dev_roles)  # noqa: F405
    if set(assignments) != set(dev_roles):
        missing = sorted(set(dev_roles) - set(assignments))
        extra = sorted(set(assignments) - set(dev_roles))
        raise ValueError(
            f"PLAN assignment map must contain every DEV role exactly; missing={missing!r} extra={extra!r}"
        )
    normalized = [re.sub(r"\s+", " ", assignments[role]).strip().casefold() for role in dev_roles]
    if len(normalized) != len(set(normalized)):
        raise ValueError("PLAN assigned duplicate implementation work to multiple DEV roles")
    return assignments


# ---------------------------------------------------------------------------
# Version 1 compatibility prompts.


def legacy_plan_prompt(ctx, role, transcript):
    return f"""You are {role}, the planning coordinator.

GOAL:
{ctx.require('goal')}

Produce a concrete execution plan. Split independent implementation work where useful.
Do not claim implementation or tests that you did not perform.
"""


def legacy_implement_prompt(ctx, role, transcript):
    plan = transcript.render(round_names=("plan",))
    return f"""You are {role}, one implementation worker in a multi-role team.

GOAL:
{ctx.require('goal')}

PLAN CONTEXT:
{plan}

Implement or investigate the portion appropriate for this worker. Report exact changes,
commands, evidence, blockers, and anything another worker must know.
"""


def legacy_review_prompt(ctx, role, transcript):
    implementation = transcript.render(round_names=("implement",))
    return f"""You are {role}, an independent review or test worker.

GOAL:
{ctx.require('goal')}

IMPLEMENTATION REPORTS:
{implementation}

Verify independently. Identify reproducible defects, missing evidence, unsafe assumptions,
and exact fixes required. Do not accept unsupported claims.
"""


def legacy_revise_prompt(ctx, role, transcript):
    context = transcript.render(round_names=("implement", "review"))
    return f"""You are {role}, an implementation worker handling review feedback.

GOAL:
{ctx.require('goal')}

IMPLEMENTATION AND REVIEW CONTEXT:
{context}

Resolve applicable defects. Re-run relevant verification and report exact evidence.
"""


def legacy_closeout_prompt(ctx, role, transcript):
    context = transcript.render()
    return f"""You are {role}, the final coordinator.

GOAL:
{ctx.require('goal')}

FULL TEAM TRANSCRIPT:
{context}

Produce the final status. Separate completed, verified, blocked, and unverified items.
Never mark the task complete when review defects remain unresolved.
"""


# ---------------------------------------------------------------------------
# Version 2 deterministic assignment and verification prompts.


def plan_prompt_v2(ctx, role, transcript):
    dev_roles = _roles(ctx, "DEV")
    schema = {name: f"One distinct, non-overlapping assignment for {name}" for name in dev_roles}
    return f"""You are {role}, the planning coordinator.

GOAL:
{ctx.require('goal')}

IMPLEMENTATION ROLE SLOTS:
{', '.join(dev_roles)}

Return ONLY one JSON object mapping every exact role name above to one non-empty,
distinct implementation or investigation assignment. Do not use Markdown fences or prose.
Each assignment must state scope, expected evidence, and handoff. Assignments must not be
identical or overlap without an explicit integration boundary.

Required shape:
{json.dumps(schema, ensure_ascii=False)}
"""


def implement_prompt_v2(ctx, role, transcript):
    assignments = _assignment_map(ctx, transcript)
    assignment = assignments[role]
    return f"""You are {role}, one exact implementation slot in a multi-role team.

GOAL:
{ctx.require('goal')}

YOUR ASSIGNMENT FROM PLAN:
{assignment}

Do only this assignment. Do not silently take another role's assignment. Report:
STATUS, CHANGES/ANALYSIS, COMMANDS OR TOOLS ACTUALLY USED, EVIDENCE, BLOCKERS, and HANDOFF.
Never claim execution or verification that did not occur.
"""


def _verification_focus(role: str) -> str:
    if role.startswith("TEST"):
        index = _role_sort_key(role, "TEST")[0]
        focuses = (
            "reproduce tests and validate concrete runtime evidence",
            "exercise failure, recovery, retry, and resume paths",
            "check concurrency, load, timing, and resource behavior",
            "check UI, integration, compatibility, and regression coverage",
        )
    else:
        index = _role_sort_key(role, "REVIEW")[0]
        focuses = (
            "correctness, requirements coverage, API and backward compatibility",
            "failure handling, recovery, idempotency, race and concurrency safety",
            "security, data integrity, observability, and operational risks",
            "maintainability, performance, UX, and missing verification evidence",
        )
    return focuses[index % len(focuses)]


def review_prompt_v2(ctx, role, transcript):
    plan_and_implementation = transcript.render(round_names=("plan", "implement"))
    return f"""You are {role}, an independent verification slot.

GOAL:
{ctx.require('goal')}

YOUR PRIMARY FOCUS:
{_verification_focus(role)}

PLAN ASSIGNMENTS AND IMPLEMENTATION REPORTS:
{plan_and_implementation}

Verify independently. Identify reproducible defects, missing evidence, unsafe assumptions,
and exact fixes. Do not accept unsupported claims. End with exactly one line:
VERDICT: ACCEPTED
or
VERDICT: BLOCKED
"""


def revise_prompt_v2(ctx, role, transcript):
    assignments = _assignment_map(ctx, transcript)
    own_report = transcript.render(round_names=("implement",), roles=(role,))
    reviews = transcript.render(round_names=("review",))
    return f"""You are {role}, the owner of one implementation assignment.

GOAL:
{ctx.require('goal')}

YOUR ORIGINAL ASSIGNMENT:
{assignments[role]}

YOUR ORIGINAL REPORT:
{own_report}

INDEPENDENT REVIEW AND TEST FEEDBACK:
{reviews}

Resolve feedback applicable to your assignment. Do not claim another role's work. Re-run
relevant verification where possible and report exact evidence. End with exactly one line:
REVISION_STATUS: COMPLETE
or
REVISION_STATUS: BLOCKED
"""


def reverify_prompt_v2(ctx, role, transcript):
    context = transcript.render(round_names=("plan", "implement", "review", "revise"))
    return f"""You are {role}, performing post-revision independent verification.

GOAL:
{ctx.require('goal')}

YOUR PRIMARY FOCUS:
{_verification_focus(role)}

IMPLEMENTATION, INITIAL REVIEW, AND REVISION REPORTS:
{context}

Verify whether the relevant defects are actually resolved. Do not rely only on the revising
worker's claim. List remaining defects and missing evidence. End with exactly one line:
VERDICT: ACCEPTED
or
VERDICT: BLOCKED
"""


def _reverify_gate(ctx, transcript):
    roles = (*_roles(ctx, "REVIEW"), *_roles(ctx, "TEST"))
    results = {}
    for role in roles:
        text = transcript.response_text("reverify", role)
        upper = text.upper()
        accepted = "VERDICT: ACCEPTED" in upper and "VERDICT: BLOCKED" not in upper
        results[role] = "ACCEPTED" if accepted else "BLOCKED"
    return results


def closeout_prompt_v2(ctx, role, transcript):
    context = transcript.render()
    gate = _reverify_gate(ctx, transcript)
    all_accepted = bool(gate) and all(value == "ACCEPTED" for value in gate.values())
    required_status = "COMPLETED" if all_accepted else "BLOCKED"
    return f"""You are {role}, the final coordinator.

GOAL:
{ctx.require('goal')}

FULL TEAM TRANSCRIPT:
{context}

DETERMINISTIC POST-REVISION VERIFICATION GATE:
{json.dumps(gate, ensure_ascii=False, sort_keys=True)}

The required final task status is {required_status}. Produce a concise final report with:
COMPLETED, VERIFIED, BLOCKED, UNVERIFIED, and NEXT ACTIONS. Never override the gate.
End with exactly:
TASK_STATUS: {required_status}
"""


if _WORKFLOW_VERSION == "1":
    _ROUNDS = [
        TeamRoundSpec("plan", ("PLAN",), legacy_plan_prompt, parallel=False),  # noqa: F405
        TeamRoundSpec("implement", ("DEV*",), legacy_implement_prompt),  # noqa: F405
        TeamRoundSpec("review", ("REVIEW*", "TEST*"), legacy_review_prompt),  # noqa: F405
        TeamRoundSpec("revise", ("DEV*",), legacy_revise_prompt),  # noqa: F405
        TeamRoundSpec("closeout", ("PLAN",), legacy_closeout_prompt, parallel=False),  # noqa: F405
    ]
else:
    _ROUNDS = [
        TeamRoundSpec("plan", ("PLAN",), plan_prompt_v2, parallel=False),  # noqa: F405
        TeamRoundSpec("implement", ("DEV*",), implement_prompt_v2),  # noqa: F405
        TeamRoundSpec("review", ("REVIEW*", "TEST*"), review_prompt_v2),  # noqa: F405
        TeamRoundSpec("revise", ("DEV*",), revise_prompt_v2),  # noqa: F405
        TeamRoundSpec("reverify", ("REVIEW*", "TEST*"), reverify_prompt_v2),  # noqa: F405
        TeamRoundSpec("closeout", ("PLAN",), closeout_prompt_v2, parallel=False),  # noqa: F405
    ]


WORKFLOW = Workflow(  # noqa: F405
    "chatgpt-team-loop",
    [
        TeamConversationBlock(  # noqa: F405
            _ROUNDS,
            executor=DurableTeamRoleExecutor(  # noqa: F405
                ledger_path=Path(
                    os.environ.get(
                        "PLAYWRIGHT_AUTO_TEAM_LEDGER",
                        ".runtime/chatgpt-team-ledger.json",
                    )
                ),
                response_timeout_ms=180_000,
                active_reload_after_ms=120_000,
            ),
            checkpoint_path=Path(
                os.environ.get(
                    "PLAYWRIGHT_AUTO_TEAM_CHECKPOINT",
                    ".runtime/chatgpt-team/{task_id}.json",
                )
            ),
        ),
    ],
)

LOOP = LoopOptions(  # noqa: F405
    max_iterations=1,
    interval_seconds=0,
    continue_on_error=False,
    stop_file=Path(".runtime/STOP_CHATGPT_TEAM_LOOP"),
    checkpoint_path=Path(".runtime/chatgpt-team-loop-run.json"),
)
