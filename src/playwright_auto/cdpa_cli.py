from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
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


def _api_base_url(config: CDPAConfig) -> str:
    host = "127.0.0.1" if config.dashboard_api_host in {"localhost", "::1"} else config.dashboard_api_host
    return f"http://{host}:{config.dashboard_api_port}"


def _post(
    config: CDPAConfig,
    path: str,
    payload_value: Mapping[str, Any],
    *,
    idempotency_key: str,
    expected_status: int = 202,
) -> Mapping[str, Any]:
    payload = json.dumps(dict(payload_value), ensure_ascii=False).encode("utf-8")
    api_url = _api_base_url(config)
    request = Request(
        f"{api_url}{path}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read()
            status = response.status
    except HTTPError as exc:
        body = exc.read()
        try:
            decoded = json.loads(body.decode("utf-8"))
            error = decoded.get("error") if isinstance(decoded, Mapping) else decoded
            detail = error.get("message") if isinstance(error, Mapping) else error
        except Exception:
            detail = body.decode("utf-8", errors="replace") or exc.reason
        raise RuntimeError(f"CDPA API rejected request: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"CDPA API is unavailable at {api_url}: {exc.reason}; "
            "start the API and worker before creating tasks"
        ) from exc
    if status != expected_status:
        raise RuntimeError(f"CDPA API returned unexpected HTTP {status}")
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError("CDPA API response must be a JSON object")
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
    depends_on_task_ids: Sequence[str] = (),
    upload_paths: Sequence[str | Path] = (),
    idempotency_key: str | None = None,
) -> Mapping[str, Any]:
    repository_path = repository.expanduser().resolve()
    return _post(
        config,
        "/api/tasks",
        {
            "task": task,
            "repository": str(repository_path),
            "requested_team": team,
            "reuse_team": reuse_team,
            "new_roles": list(new_roles),
            "new_all": bool(new_all),
            "report_mode": "file",
            "depends_on_task_ids": list(depends_on_task_ids),
            "upload_paths": [str(Path(path).expanduser().resolve()) for path in upload_paths],
        },
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )


def resume_task(
    config: CDPAConfig,
    *,
    repository: Path,
    team: str,
    idempotency_key: str | None = None,
) -> Mapping[str, Any]:
    del repository
    return _post(
        config,
        "/api/tasks/resume",
        {"team": team},
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )


def _runtime_commands(config: CDPAConfig) -> tuple[list[str], list[str], list[str]]:
    config_path = str(config.config_path)
    frontend = [
        sys.executable,
        "-m",
        "playwright_auto.dashboard",
        "--config",
        config_path,
    ]
    api = [
        sys.executable,
        "-m",
        "playwright_auto.dashboard_api",
        "--repository",
        str(config.repository_root),
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
    return frontend, api, worker


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
    _read_json(f"{config.dashboard_url}/health", timeout=2)
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
        frontend_health = _read_json(f"{config.dashboard_url}/health", timeout=1)
        api_health = _read_json(f"{_api_base_url(config)}/health", timeout=1)
    except RuntimeError:
        frontend_health = api_health = None
    if frontend_health is not None and api_health is not None:
        if open_ui:
            _open_dashboard(config.dashboard_url)
        print("runtime=already-running", flush=True)
        print(f"dashboard={config.dashboard_url}", flush=True)
        print(f"api={_api_base_url(config)}", flush=True)
        return 0

    frontend_command, api_command, worker_command = _runtime_commands(config)
    frontend: subprocess.Popen[Any] | None = None
    api: subprocess.Popen[Any] | None = None
    worker: subprocess.Popen[Any] | None = None
    try:
        api = _start_process(api_command, cwd=repository)
        frontend = _start_process(frontend_command, cwd=repository)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if api.poll() is not None:
                raise RuntimeError(f"API exited during startup with code {api.returncode}")
            if frontend.poll() is not None:
                raise RuntimeError(f"frontend exited during startup with code {frontend.returncode}")
            try:
                _read_json(f"{_api_base_url(config)}/health", timeout=0.5)
                _read_json(f"{config.dashboard_url}/health", timeout=0.5)
            except RuntimeError:
                time.sleep(0.2)
                continue
            break
        else:
            raise RuntimeError("CDPA API/frontend did not become ready")
        worker = _start_process(worker_command, cwd=repository)
        print("runtime=running", flush=True)
        print(f"dashboard={config.dashboard_url}", flush=True)
        print(f"api={_api_base_url(config)}", flush=True)
        print("Press Ctrl+C to stop frontend, API, and worker; Chrome remains open.", flush=True)
        if open_ui:
            _open_dashboard(config.dashboard_url)
        while True:
            for label, process in (("frontend", frontend), ("API", api), ("worker", worker)):
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"{label} exited with code {code}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        _stop_process(worker)
        _stop_process(frontend)
        _stop_process(api)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or resume a durable CDPA task",
        epilog=(
            "Run `cdpa` or `cdpa start` to start the frontend, API, and worker for the "
            "current repository. Run `cdpa ui` to reopen the dashboard."
        ),
    )
    parser.add_argument("task", nargs="?", help="complete task text; omit to resume --team")
    parser.add_argument("--task", dest="task_option", help="complete task text (explicit form)")
    parser.add_argument("--new", dest="new_roles", help="comma-separated roles reset lazily once")
    parser.add_argument("--new-all", action="store_true", help="reset every selected role lazily once")
    parser.add_argument("--team", default=None, help="new-task team base or exact team to resume")
    parser.add_argument(
        "--reuse-team",
        default=None,
        help="queue new work for one exact existing team without allocating a suffix",
    )
    parser.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="dependency task ID; repeat or separate values with commas",
    )
    parser.add_argument(
        "--upload",
        action="append",
        default=[],
        help="file context to upload; repeat for multiple files",
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
        if args.task and args.task_option:
            raise ValueError("provide task text either positionally or with --task, not both")
        task = str(args.task_option or args.task or "").strip()
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
                depends_on_task_ids=dependencies,
                upload_paths=tuple(
                    str(Path(path).expanduser().resolve()) for path in args.upload
                ),
            )
            mode = "created"
        else:
            if args.reuse_team:
                raise ValueError("--reuse-team is invalid in taskless resume mode")
            if not args.team:
                raise ValueError("taskless resume requires --team <exact-existing-team>")
            if new_roles or args.new_all or dependencies or args.upload:
                raise ValueError(
                    "--new, --new-all, --depends-on, and --upload are invalid in taskless resume mode"
                )
            state = resume_task(config, repository=repository, team=str(args.team))
            mode = "resumed"
    except Exception as exc:
        print(f"cdpa: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"mode={mode}")
    if state.get("command_id"):
        print(f"command_id={state['command_id']}")
    if state.get("task_id"):
        print(f"task_id={state['task_id']}")
    if state.get("status"):
        print(f"status={state['status']}")
    print(f"api={_api_base_url(config)}")
    print(f"dashboard={config.dashboard_url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
