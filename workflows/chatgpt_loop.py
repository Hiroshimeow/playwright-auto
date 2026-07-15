"""Only edit this file to change the ChatGPT workflow and loop policy."""

from pathlib import Path

from playwright_auto.workflow_api import *  # noqa: F403

VARIABLES = {
    "prompt": "Workflow loop probe",
}

WORKFLOW = Workflow(  # noqa: F405
    "chatgpt-main-loop",
    [
        SetRoleBlock("PLAN"),  # noqa: F405
        NewChatBlock(),  # noqa: F405
        SetComposerBlock(variable("prompt")),  # noqa: F405
        CaptureSnapshotBlock("draft", block_id="capture_draft"),  # noqa: F405
        ClearComposerBlock(),  # noqa: F405
        CaptureSnapshotBlock("ready", block_id="capture_ready"),  # noqa: F405
    ],
)

LOOP = LoopOptions(  # noqa: F405
    max_iterations=1,              # None = run forever; stop_file is then required
    interval_seconds=0,
    continue_on_error=False,
    stop_file=Path(".runtime/STOP_CHATGPT_LOOP"),
    checkpoint_path=Path(".runtime/chatgpt-loop-checkpoint.json"),
)
