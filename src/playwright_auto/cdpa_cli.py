from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
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


def parse_dependency_ids(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip()
            for value in values or ()
            for part in str(value).split(",")
            if part.strip()
        )
    )


def _read_json(url: str, *, timeout: float) -> Mapping[str, Any]:
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            body = response.read()
    except URLError as exc:
        raise RuntimeError(f"endpoint is unavailable at {url}: {exc.reason}") from exc
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"endpoint returned a non-object response: {url}")
    return value


def _post(
    config: CDPAConfig,
    path: str,
    payload_value: Mapping[str, Any],
    *,
    expected_status: int,
) -> Mapping[str, Any]:
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
            f"dashboard is unavailable at {config.dashboard_url}: {exc.reason}; "
            "run `cdpa` in another terminal to start the UI and worker"
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
    reuse_team: str | None = None,
    report_mode: str = "file",
    depends_on_task_ids: Sequence[str] = (),
) -> Mapping[str, Any]:
    return _post(
        config,
        "/api/tasks",
        {
            "task": task,
            "repository": str(repository),
            "team": team,
            "reuse_team": reuse_team,
            "new_roles": list(new_roles),
            "new_all": bool(new_all),
            "report_mode": str(report_mode),
            "depends_on_task_ids": list(depends_on_task_ids),
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


def _runtime_commands(config: CDPAConfig) -> tuple[list[str], list[str]]:
    config_path = str(config.config_path)
    dashboard = [
        sys.executable,
        "-m",
        "playwright_auto.dashboard",
        "--config",
        config_path,
    ]
    worker = [
        sys.executable,
        "-m",
        "playwright_auto.cdpa_worker",
        "--repository",
        str(config.repository_root),
        "--config",
        config_path,
    ]
    return dashboard, worker


def _interrupt_signal() -> int:
    if os.name == "nt":
        return int(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))
    return int(signal.SIGINT)


def _start_process(command: Sequence[str], *, cwd: Path) -> subprocess.Popen[Any]:
    options: dict[str, Any] = {"cwd": cwd}
    if os.name == "nt":
        options["creationflags"] = int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    return subprocess.Popen(list(command), **options)


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(_interrupt_signal())
        process.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _open_dashboard(url: str) -> None:
    if not webbrowser.open(url):
        print(f"dashboard={url}")


def open_runtime_ui(
    *,
    repository: Path,
    config_path: str | Path | None,
) -> int:
    config = load_cdpa_config(config_path, repository_root=repository)
    payload = _read_json(f"{config.dashboard_url}/api/tasks", timeout=2)
    active_repository = Path(str(payload.get("repository") or "")).expanduser().resolve()
    if active_repository != repository:
        raise RuntimeError(
            f"dashboard is bound to {active_repository}, not {repository}; "
            "stop that runtime before switching repositories"
        )
    _open_dashboard(config.dashboard_url)
    print(f"dashboard={config.dashboard_url}", flush=True)
    return 0


def start_runtime(
    *,
    repository: Path,
    config_path: str | Path | None,
    open_ui: bool,
) -> int:
    if not repository.is_dir():
        raise ValueError(f"repository does not exist or is not a directory: {repository}")
    config = load_cdpa_config(config_path, repository_root=repository)

    try:
        existing = _read_json(f"{config.dashboard_url}/api/tasks", timeout=1)
    except RuntimeError:
        existing = None
    if existing is not None:
        active_repository = Path(
            str(existing.get("repository") or "")
        ).expanduser().resolve()
        if active_repository != repository:
            raise RuntimeError(
                f"port {config.dashboard_port} is already serving {active_repository}; "
                "stop that CDPA runtime before switching repositories"
            )
        if open_ui:
            _open_dashboard(config.dashboard_url)
        print("runtime=already-running", flush=True)
        print(f"repository={repository}", flush=True)
        print(f"dashboard={config.dashboard_url}", flush=True)
        return 0

    dashboard_command, worker_command = _runtime_commands(config)
    dashboard: subprocess.Popen[Any] | None = None
    worker: subprocess.Popen[Any] | None = None
    try:
        dashboard = _start_process(dashboard_command, cwd=repository)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            code = dashboard.poll()
            if code is not None:
                raise RuntimeError(f"dashboard exited during startup with code {code}")
            try:
                health = _read_json(f"{config.dashboard_url}/health", timeout=0.5)
            except RuntimeError:
                time.sleep(0.2)
                continue
            if health.get("task_store_ready"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"dashboard did not become ready at {config.dashboard_url}")

        worker = _start_process(worker_command, cwd=repository)
        print("runtime=running", flush=True)
        print(f"repository={repository}", flush=True)
        print(f"dashboard={config.dashboard_url}", flush=True)
        print("Press Ctrl+C to stop the dashboard and worker; Chrome remains open.", flush=True)
        if open_ui:
            _open_dashboard(config.dashboard_url)

        while True:
            dashboard_code = dashboard.poll()
            worker_code = worker.poll()
            if dashboard_code is not None:
                raise RuntimeError(f"dashboard exited with code {dashboard_code}")
            if worker_code is not None:
                raise RuntimeError(f"worker exited with code {worker_code}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        _stop_process(worker)
        _stop_process(dashboard)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or resume a durable CDPA task",
        epilog=(
            "Run `cdpa` or `cdpa start` to start the dashboard and worker for the "
            "current repository. Run `cdpa ui` to reopen the dashboard."
        ),
    )
    parser.add_argument("task", nargs="?", help="complete task text; omit to resume --team")
    parser.add_argument("--new", dest="new_roles", help="comma-separated roles reset lazily once")
    parser.add_argument("--new-all", action="store_true", help="reset every selected role lazily once")
    parser.add_argument("--team", default=None, help="new-task team base or exact team to resume")
    parser.add_argument(
        "--reuse-team",
        default=None,
        help="queue new work for one exact existing team without allocating a suffix",
    )
    parser.add_argument(
        "--inline-report",
        action="store_true",
        help="write role reports from response Markdown instead of agent-created files",
    )
    parser.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="dependency task ID; repeat or separate values with commas",
    )
    parser.add_argument("--repository", default=".", help="task repository/worktree")
    parser.add_argument(
        "--config",
        default=None,
        help="custom CDPA config; defaults to ./cdpa.yaml when present, otherwise packaged defaults",
    )
    return parser


def _runtime_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"cdpa {command}")
    parser.add_argument("--repository", default=".", help="target repository/worktree")
    parser.add_argument(
        "--config",
        default=None,
        help="custom CDPA config; defaults to ./cdpa.yaml when present, otherwise packaged defaults",
    )
    if command == "start":
        parser.add_argument("--no-open", action="store_true", help="do not open the dashboard browser tab")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        if not raw or raw[0] == "start":
            args = _runtime_parser("start").parse_args(raw[1:] if raw else [])
            return start_runtime(
                repository=Path(args.repository).expanduser().resolve(),
                config_path=args.config,
                open_ui=not args.no_open,
            )
        if raw[0] == "ui":
            args = _runtime_parser("ui").parse_args(raw[1:])
            return open_runtime_ui(
                repository=Path(args.repository).expanduser().resolve(),
                config_path=args.config,
            )

        args = build_parser().parse_args(raw)
        repository = Path(args.repository).expanduser().resolve()
        config = load_cdpa_config(args.config, repository_root=repository)
        task = str(args.task or "").strip()
        new_roles = parse_new_roles(args.new_roles)
        dependencies = parse_dependency_ids(args.depends_on)
        if task:
            if args.team and args.reuse_team:
                raise ValueError("--team and --reuse-team are mutually exclusive")
            state = submit_task(
                config,
                task=task,
                repository=repository,
                team=args.team,
                reuse_team=args.reuse_team,
                new_roles=new_roles,
                new_all=bool(args.new_all),
                report_mode="inline" if args.inline_report else "file",
                depends_on_task_ids=dependencies,
            )
            mode = "created"
        else:
            if args.reuse_team:
                raise ValueError("--reuse-team is invalid in taskless resume mode")
            if not args.team:
                raise ValueError("taskless resume requires --team <exact-existing-team>")
            if new_roles or args.new_all or args.inline_report or dependencies:
                raise ValueError(
                    "--new, --new-all, --inline-report, and --depends-on are invalid in taskless resume mode"
                )
            state = resume_task(config, repository=repository, team=str(args.team))
            mode = "resumed"
    except Exception as exc:
        print(f"cdpa: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"mode={mode}")
    print(f"task_id={state['task_id']}")
    print(f"team={state['team']}")
    print(f"manifest={state['manifest_path']}")
    print(f"dashboard={config.dashboard_url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
