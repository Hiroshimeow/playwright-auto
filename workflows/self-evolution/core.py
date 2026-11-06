"""Canonical self-evolution prompt construction and read-only CDPA health.

CDPA's own task/request ledger is the sole work queue. This module never
creates a competing dispatcher, persistence store, or browser Send path.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "workflows" / "self-evolution" / "GOAL_AND_RULES.md"
KINDS = {"opus_fjp", "solo_g6", "implement_g6", "recover_g6"}


def canonical_goal():
    content = CONTRACT.read_text(encoding="utf-8-sig")
    required = ("## Full immutable user goal", "## Hard rules",
                "## Delegation", "## Acceptance")
    if not all(section in content for section in required):
        raise ValueError("Incomplete canonical campaign contract")
    return content


def task_prompt(kind, brief, parent=None):
    """Every actual Solo --goal includes the full current campaign contract."""
    if kind not in KINDS:
        raise ValueError("invalid work kind")
    if not brief.strip():
        raise ValueError("empty task brief")
    return (
        canonical_goal()
        + "\n\n## MANDATORY SPAWN COMMAND RULE\n"
        "Mandatory first read: " + str(CONTRACT.resolve()) + "\n"
        "Before spawning ANY CDPA task, read the canonical contract and "
        "embed its FULL CURRENT TEXT in the --goal argument using "
        "workflows/self-evolution/spawn_cdpa.py. Never rely on inherited chat "
        "context or a file pointer alone. Pi is the sole dispatch owner. "
        "Preserve task/browser ownership and do not independently Send. "
        "Task kind: " + kind + "\nParent task: " + (parent or "root")
        + "\nCurrent bounded instruction: " + brief + "\n"
    )


def health():
    try:
        with urlopen("http://127.0.0.1:9225/health", timeout=4) as response:
            data = json.load(response)
        required = ("ok", "worker_online", "worker_ready",
                    "worker_runtime_ready", "worker_command_loop_ready")
        return {
            "ready": all(data.get(field) for field in required)
            and not data.get("worker_source_drift", {}).get("drifted", False),
            "worker": data.get("worker_status"),
        }
    except (OSError, ValueError) as error:
        return {"ready": False, "error": str(error)}
