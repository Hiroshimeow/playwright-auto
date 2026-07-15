"""Multi-tab PLAN/DEV/REVIEW workflow; edit only this file."""

from pathlib import Path

from playwright_auto.workflow_api import *  # noqa: F403

WORKSPACE_ROLES = ("PLAN", "DEV", "REVIEW")

VARIABLES = {
    "route_json": '{"PLAN":"Review the current task","DEV":"Implement the task"}',
}

WORKFLOW = Workflow(  # noqa: F405
    "chatgpt-role-loop",
    [
        ParseRouteBlock(variable("route_json")),  # noqa: F405
        DispatchRouteBlock(wait_for_stop=True),  # noqa: F405
        # Add WaitRouteResponsesBlock() only when authenticated response completion
        # has been verified for the active profile.
    ],
)

LOOP = LoopOptions(  # noqa: F405
    max_iterations=1,
    interval_seconds=0,
    continue_on_error=False,
    stop_file=Path(".runtime/STOP_CHATGPT_ROLE_LOOP"),
    checkpoint_path=Path(".runtime/chatgpt-role-loop-checkpoint.json"),
)
