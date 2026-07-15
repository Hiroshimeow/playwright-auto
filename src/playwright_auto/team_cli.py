from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .chatgpt import ChatGPTPage
from .connection import connected_browser
from .roles import expand_role_team
from .runner import run_chatgpt_loop

DEFAULT_TEAM: dict[str, int] = {
    "PLAN": 1,
    "DEV": 2,
    "REVIEW": 1,
    "TEST": 1,
}
ALLOWED_BASE_ROLES = frozenset(DEFAULT_TEAM)
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_team_mapping(team: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(team, Mapping) or not team:
        raise ValueError("team must be a non-empty mapping")

    normalized: dict[str, int] = {}
    for raw_role, raw_count in team.items():
        role = str(raw_role).strip().upper()
        if role not in ALLOWED_BASE_ROLES:
            raise ValueError(
                f"unsupported standard-flow role {role!r}; "
                f"allowed={sorted(ALLOWED_BASE_ROLES)!r}"
            )
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise TypeError(f"role count for {role!r} must be an integer")
        if raw_count < 1:
            raise ValueError(f"role count for {role!r} must be at least 1")
        normalized[role] = raw_count

    if normalized.get("PLAN") != 1:
        raise ValueError("the standard team flow requires exactly PLAN=1")
    if normalized.get("DEV", 0) < 1:
        raise ValueError("the standard team flow requires at least DEV=1")
    if normalized.get("REVIEW", 0) + normalized.get("TEST", 0) < 1:
        raise ValueError("the standard team flow requires REVIEW or TEST")
    expand_role_team(normalized, max_total_slots=32)
    return normalized


def parse_team_spec(spec: str | None) -> dict[str, int]:
    team = dict(DEFAULT_TEAM)
    if spec:
        for item in spec.split(","):
            token = item.strip()
            if not token:
                continue
            if "=" not in token:
                raise ValueError(f"invalid team item {token!r}; expected ROLE=COUNT")
            raw_role, raw_count = token.split("=", 1)
            role = raw_role.strip().upper()
            if role not in ALLOWED_BASE_ROLES:
                raise ValueError(
                    f"unsupported standard-flow role {role!r}; "
                    f"allowed={sorted(ALLOWED_BASE_ROLES)!r}"
                )
            try:
                count = int(raw_count.strip())
            except ValueError as exc:
                raise ValueError(f"role count for {role!r} must be an integer") from exc
            if count < 0:
                raise ValueError(f"role count for {role!r} must not be negative")
            if count == 0:
                team.pop(role, None)
            else:
                team[role] = count
    return validate_team_mapping(team)


def validate_task_id(value: str) -> str:
    task_id = str(value).strip()
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(
            "task ID must start with a letter or number and contain only "
            "letters, numbers, dot, underscore, or hyphen (max 128 characters)"
        )
    return task_id


def slugify(value: str, *, limit: int = 36) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (slug or "task")[:limit].rstrip("-") or "task"


def generate_task_id(goal: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(goal)}"


def atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required in {path}")
    return value


def viewer_url() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        address = next(
            (line.strip() for line in result.stdout.splitlines() if line.strip()),
            "127.0.0.1",
        )
    except (OSError, subprocess.SubprocessError):
        address = "127.0.0.1"
    return f"http://{address}:9223/"


async def authenticated_profile(cdp_url: str) -> tuple[bool, str]:
    try:
        async with connected_browser(cdp_url) as browser:
            if not browser.contexts:
                return False, "CDP browser has no persistent context"
            pages = [
                page
                for page in browser.contexts[0].pages
                if page.url.startswith("https://chatgpt.com/")
            ]
            if not pages:
                return False, "no ChatGPT tab is open"
            for page in pages:
                snapshot = await ChatGPTPage(page).snapshot()
                if not snapshot.requires_login:
                    return True, "authenticated"
            return False, "ChatGPT profile is not logged in"
    except Exception as exc:
        return False, f"cannot inspect browser authentication: {type(exc).__name__}: {exc}"


def extract_team_transcript(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the persisted team transcript from supported runner payload shapes."""
    candidates: list[Any] = [payload.get("variables")]
    try:
        candidates.append(payload["iterations"][-1]["workflow_run"].get("variables"))
    except (KeyError, IndexError, TypeError):
        pass
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        transcript = candidate.get("team_transcript")
        if isinstance(transcript, Mapping):
            return transcript
    return None


def extract_final_report(payload: Mapping[str, Any]) -> str | None:
    transcript = extract_team_transcript(payload)
    if not transcript:
        return None
    try:
        entry = transcript["rounds"]["closeout"]["PLAN"]
        return str(entry["result"]["response"]["text"]).strip() or None
    except (KeyError, TypeError):
        return None


_TASK_STATUS_PATTERN = re.compile(
    r"(?i)\bTASK_STATUS:\s*(COMPLETED|BLOCKED)\b"
)


def extract_task_outcome(final_report: str | None) -> str | None:
    if not final_report:
        return None
    matches = _TASK_STATUS_PATTERN.findall(final_report)
    if len(matches) != 1:
        return None
    return matches[0].lower()


def validate_workflow_version(value: Any) -> str:
    version = str(value).strip()
    if version not in {"1", "2"}:
        raise ValueError("workflow version must be 1 or 2")
    return version


def extract_failure(payload: Mapping[str, Any]) -> str | None:
    stage = payload.get("stage")
    error = payload.get("error")
    if error:
        return f"{stage or 'run'}: {error}"
    try:
        trace = payload["iterations"][-1]["workflow_run"]["trace"]
        failed = next(item for item in reversed(trace) if item.get("status") == "failed")
        return str(failed.get("error") or "workflow failed")
    except (KeyError, IndexError, StopIteration, TypeError):
        return None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="playwright-team",
        description="Run or resume the stable visible multi-role ChatGPT team workflow.",
    )
    parser.add_argument("goal", nargs="?", help="Task goal for a new run")
    parser.add_argument(
        "--team",
        help="Override default counts, e.g. DEV=3,REVIEW=2,TEST=2 or TEST=0",
    )
    parser.add_argument("--task-id", help="Explicit ID for a new task")
    parser.add_argument("--resume", metavar="TASK_ID", help="Resume a saved task manifest")
    parser.add_argument("--workflow-version", default="2")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--allow-guest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


async def run(args: argparse.Namespace) -> int:
    root = repo_root()
    manifest_dir = root / ".runtime" / "team-tasks"
    run_dir = root / ".runtime" / "team-runs"

    if args.resume:
        if args.goal or args.team or args.task_id:
            raise ValueError("--resume cannot be combined with goal, --team, or --task-id")
        task_id = validate_task_id(args.resume)
        manifest_path = manifest_dir / f"{task_id}.json"
        manifest = load_json(manifest_path)
        goal = str(manifest["goal"])
        saved_team = manifest.get("team")
        if not isinstance(saved_team, dict):
            raise TypeError("saved task manifest has no valid team mapping")
        team = validate_team_mapping(saved_team)
        workflow_version = validate_workflow_version(manifest["workflow_version"])
    else:
        if not args.goal or not args.goal.strip():
            raise ValueError("a non-empty goal is required for a new task")
        goal = args.goal.strip()
        team = parse_team_spec(args.team)
        task_id = validate_task_id(args.task_id) if args.task_id else generate_task_id(goal)
        workflow_version = validate_workflow_version(args.workflow_version)
        manifest_path = manifest_dir / f"{task_id}.json"
        if manifest_path.exists():
            raise FileExistsError(
                f"task {task_id!r} already exists; use --resume {task_id}"
            )
        manifest = {
            "task_id": task_id,
            "goal": goal,
            "team": team,
            "workflow_version": workflow_version,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        atomic_json_write(manifest_path, manifest)

    slots = expand_role_team(team, max_total_slots=32)
    summary = {
        "task_id": task_id,
        "goal": goal,
        "team": team,
        "roles": [slot.display_name for slot in slots],
        "viewer": viewer_url(),
        "manifest": str(manifest_path),
    }
    if args.dry_run:
        print(json.dumps({"status": "dry_run", **summary}, ensure_ascii=False, indent=2))
        return 0

    if not args.allow_guest:
        authenticated, reason = await authenticated_profile(args.cdp)
        if not authenticated:
            print(
                json.dumps(
                    {
                        "status": "waiting_for_login",
                        **summary,
                        "reason": reason,
                        "next_command": f"uv run playwright-team --resume {task_id}",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3

    os.environ["PLAYWRIGHT_AUTO_TASK_ID"] = task_id
    os.environ["PLAYWRIGHT_AUTO_GOAL"] = goal
    os.environ["PLAYWRIGHT_AUTO_TEAM_JSON"] = json.dumps(team, separators=(",", ":"))
    os.environ["PLAYWRIGHT_AUTO_WORKFLOW_VERSION"] = workflow_version

    workflow_path = root / "workflows" / "chatgpt_team_loop.py"
    exit_code, payload = await run_chatgpt_loop(workflow_path, args.cdp)
    run_path = run_dir / f"{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    atomic_json_write(run_path, payload)

    final_report = extract_final_report(payload)
    task_outcome = extract_task_outcome(final_report)
    failure = extract_failure(payload)
    output: dict[str, Any] = {
        "status": payload.get("status", "failed"),
        "task_outcome": task_outcome,
        **summary,
        "workflow_version": workflow_version,
        "run_file": str(run_path),
    }
    if final_report:
        output["final_report"] = final_report
    if failure:
        output["failure"] = failure

    if args.json_output:
        print(
            json.dumps(
                {**output, "result": payload},
                ensure_ascii=False,
                indent=2,
            )
        )
        return exit_code

    if failure:
        output["error"] = failure
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (FileNotFoundError, FileExistsError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
