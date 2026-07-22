from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .cdpa_config import CDPAConfig, load_cdpa_config


def parse_new_roles(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        dict.fromkeys(part.strip().upper() for part in str(value).split(",") if part.strip())
    )


def _post(config: CDPAConfig, path: str, payload_value: Mapping[str, Any], *, expected_status: int) -> Mapping[str, Any]:
    payload = json.dumps(dict(payload_value), ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{config.dashboard_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read()
            status = response.status
    except HTTPError as exc:
        body = exc.read()
        try:
            detail = json.loads(body.decode("utf-8")).get("error")
        except Exception:
            detail = body.decode("utf-8", errors="replace") or exc.reason
        raise RuntimeError(f"dashboard rejected request: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"dashboard is unavailable at {config.dashboard_url}: {exc.reason}"
        ) from exc
    if status != expected_status:
        raise RuntimeError(f"dashboard returned unexpected HTTP {status}")
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError("dashboard response must be a JSON object")
    return value


def submit_task(
    config: CDPAConfig,
    *,
    task: str,
    repository: Path,
    team: str | None,
    new_roles: Sequence[str],
    new_all: bool,
) -> Mapping[str, Any]:
    return _post(
        config,
        "/api/tasks",
        {
            "task": task,
            "repository": str(repository),
            "team": team,
            "new_roles": list(new_roles),
            "new_all": bool(new_all),
        },
        expected_status=201,
    )


def resume_task(config: CDPAConfig, *, repository: Path, team: str) -> Mapping[str, Any]:
    return _post(
        config,
        "/api/tasks/resume",
        {"repository": str(repository), "team": team},
        expected_status=202,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or resume a durable CDPA task")
    parser.add_argument("task", nargs="?", help="complete task text; omit to resume --team")
    parser.add_argument("--new", dest="new_roles", help="comma-separated roles reset lazily once")
    parser.add_argument("--new-all", action="store_true", help="reset every selected role lazily once")
    parser.add_argument("--team", default=None, help="new-task team base or exact team to resume")
    parser.add_argument("--repository", default=".", help="task repository/worktree")
    parser.add_argument("--config", default="cdpa.yaml", help="root CDPA config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repository = Path(args.repository).expanduser().resolve()
        config = load_cdpa_config(args.config, repository_root=repository)
        task = str(args.task or "").strip()
        new_roles = parse_new_roles(args.new_roles)
        if task:
            state = submit_task(
                config,
                task=task,
                repository=repository,
                team=args.team,
                new_roles=new_roles,
                new_all=bool(args.new_all),
            )
            mode = "created"
        else:
            if not args.team:
                raise ValueError("taskless resume requires --team <exact-existing-team>")
            if new_roles or args.new_all:
                raise ValueError("--new and --new-all are invalid in taskless resume mode")
            state = resume_task(config, repository=repository, team=str(args.team))
            mode = "resumed"
    except Exception as exc:
        print(f"cdpa: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"mode={mode}")
    print(f"task_id={state['task_id']}")
    print(f"team={state['team']}")
    print(f"manifest={state['manifest_path']}")
    print(f"dashboard={config.dashboard_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
