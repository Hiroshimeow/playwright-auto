"""Production-oriented one-file ChatGPT workflow template.

Edit VARIABLES and reorder/add/remove blocks in WORKFLOW. The file is not run
unless explicitly passed to scripts/run_chatgpt_loop.py.
"""

from pathlib import Path

from playwright_auto.workflow_api import *  # noqa: F403

VARIABLES = {
    "prompt": "Replace this prompt",
    "files": [],
}

WORKFLOW = Workflow(  # noqa: F405
    "chatgpt-durable-loop",
    [
        SetRoleBlock("DEV"),  # noqa: F405
        RecoverPageBlock(),  # noqa: F405
        WaitCleanReadyBlock(),  # noqa: F405
        DurableSendBlock(  # noqa: F405
            variable("prompt"),  # noqa: F405
            files=variable("files"),  # noqa: F405
            ledger_path=Path(".runtime/chatgpt-request-ledger.json"),
            response_timeout_ms=180_000,
            active_reload_after_ms=120_000,
        ),
    ],
)

LOOP = LoopOptions(  # noqa: F405
    max_iterations=1,
    interval_seconds=0,
    continue_on_error=False,
    stop_file=Path(".runtime/STOP_CHATGPT_DURABLE_LOOP"),
    checkpoint_path=Path(".runtime/chatgpt-durable-loop-checkpoint.json"),
)
