"""Single canonical CDPA task constructor.

Use this launcher for EVERY generation; no LLM builds its own shortened prompt.
It embeds the complete current GOAL_AND_RULES.md into the CLI request.
"""
from __future__ import annotations
import argparse
import uuid
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from core import canonical_goal, task_prompt

def command(role: str, team: str, instruction: str, parent: str = "", depends_on: str = "") -> list[str]:
    if role not in ("DEV", "TEST", "REVIEW", "PLAN", "AUDIT"):
        raise ValueError("unsupported CDPA role")
    if not team or not instruction.strip():
        raise ValueError("missing team or instruction")
    if "## Full immutable user goal" in instruction:
        raise ValueError("nested full campaign prompt prohibited; pass only bounded instruction")
    # Critical: no abbreviated summary / no reference-only inheritance.
    prompt = task_prompt("solo_g6", instruction, parent or None)
    if canonical_goal() not in prompt:
        raise ValueError("mission must be embedded in full")
    # IMPORTANT: never pass multiline prompts through cdpa.cmd / cmd.exe.
    # CMD parses a newline as another command and silently truncates the task.
    # Call the Python CLI directly so the full policy is one argv element.
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    # Solo tasks use two distinct CDPA fields: --task is a display name,
    # while --goal is the exact first-turn instruction seen by the agent.
    # Sending the contract only as --task silently leaves Solo goal empty.
    title = f"CDPA Self-Evolution {role}: {team}"
    cmd = [str(python), "-m", "playwright_auto.cdpa_cli",
           "--solo", role, "--browser", "9202", "--team", team,
           "--task", title, "--goal", prompt,
           "--config", str(ROOT / "cdpa.yaml")]
    if depends_on:
        cmd.extend(["--depends-on", depends_on])
    if parent:
        # Same parent transition always returns the same CDPA command; a changed
        # payload conflicts instead of creating a second child after lost ack.
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, "cdpa:self-evolution:successor:" + parent))
        cmd.extend(["--parent-task-id", parent, "--idempotency-key", key])
    if len(subprocess.list2cmdline(cmd)) >= 30000:
        raise ValueError("Windows command-line length exceeds safe budget")
    return cmd

def main():
    p = argparse.ArgumentParser(
        description="Submit a CDPA self-evolution task through the campaign constructor.",
        epilog="For ordinary repository reviews, use the CDPA CLI with --solo REVIEW.",
    )
    p.add_argument("--role", required=True)
    p.add_argument("--team", required=True)
    p.add_argument("--instruction", required=True)
    p.add_argument("--parent", default="")
    p.add_argument("--depends-on", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    cmd = command(args.role, args.team, args.instruction, args.parent, args.depends_on)
    if args.dry_run:
        print("READY: role=" + args.role + " team=" + args.team
              + " embedded_contract_bytes=" + str(len(cmd[cmd.index("--goal") + 1].encode("utf-8"))))
        return
    if args.parent:
        # This is the Pi-owned dispatch path; persist the exact transition
        # before the first irreversible task-create API call.
        from run_pi import PiDispatchOwnerUnavailable, record_transition_intent
        try:
            record_transition_intent(
                parent=args.parent, team=args.team, role=args.role,
                goal=cmd[cmd.index("--goal") + 1],
                idempotency_key=cmd[cmd.index("--idempotency-key") + 1],
            )
        except PiDispatchOwnerUnavailable as error:
            p.exit(2, f"{error}. No CDPA task was submitted by this invocation. "
                   "For campaign successors, reconcile the Pi checkpoint. "
                   "For ordinary repository reviews, use cdpa --solo REVIEW.\n")
    result = subprocess.run(cmd, cwd=str(ROOT), check=False)
    raise SystemExit(result.returncode)

if __name__ == "__main__":
    main()
