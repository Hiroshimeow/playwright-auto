from __future__ import annotations

import argparse
import json
import os
import shutil
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
from .cdpa_runtime_db import RuntimeDB


_BOOTSTRAP_UNSET = object()


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
    repository: Path | None,
    team: str | None,
    new_roles: Sequence[str],
    new_all: bool,
    depends_on_task_ids: Sequence[str] = (),
    temporary: bool = False,
    browser_endpoint_id: str | None = None,
    required_browser_endpoint_id: str | None = None,
    upload_paths: Sequence[str | Path] = (),
    bootstrap_id: str | None | object = _BOOTSTRAP_UNSET,
    idempotency_key: str | None = None,
    workflow_mode: str = "team",
    solo_role: str | None = None,
    goal: str | None = None,
    conversation_url: str | None = None,
) -> Mapping[str, Any]:
    mode = str(workflow_mode or "team").strip().lower()
    if mode == "solo":
        if not solo_role or not str(solo_role).strip():
            raise ValueError("Solo Task requires --solo ROLE")
        if not browser_endpoint_id:
            raise ValueError("Solo Task requires --browser ENDPOINT")
        payload = {
            "workflow_mode": "solo",
            "role": str(solo_role).strip().upper(),
            "goal": str(goal or "").strip(),
            "browser_endpoint_id": browser_endpoint_id,
        }
        if str(task).strip():
            payload["task"] = str(task).strip()
        if team:
            payload["requested_team"] = team
        if conversation_url:
            payload["conversation_url"] = str(conversation_url).strip()
        if repository is not None:
            payload["repository"] = str(repository.expanduser().resolve())
        return _post(
            config,
            "/api/tasks",
            payload,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
    if mode != "team":
        raise ValueError("workflow_mode must be team or solo")
    payload = {
        "task": task,
        "requested_team": team,
        "new_roles": list(new_roles),
        "new_all": bool(new_all),
        "report_mode": "file",
        "depends_on_task_ids": list(depends_on_task_ids),
        "upload_paths": [str(Path(path).expanduser().resolve()) for path in upload_paths],
    }
    if repository is not None:
        payload["repository"] = str(repository.expanduser().resolve())
    if temporary:
        payload["temporary"] = True
    if browser_endpoint_id is not None:
        payload["browser_endpoint_id"] = browser_endpoint_id
    if required_browser_endpoint_id is not None:
        payload["required_browser_endpoint_id"] = required_browser_endpoint_id
    if bootstrap_id is not _BOOTSTRAP_UNSET:
        payload["bootstrap_id"] = bootstrap_id
    return _post(
        config,
        "/api/tasks",
        payload,
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


def _independent_trigger_settings(args: argparse.Namespace, *, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = dict(base or {})
    settings.setdefault("recovery", False)
    settings.setdefault("interval_minutes", None)
    settings.setdefault("daily_at", None)
    settings.setdefault("task_done", False)
    settings.setdefault("role_completed", [])
    settings.setdefault("teams", [])
    settings.setdefault("states", [])
    settings.setdefault("check_all", False)

    trigger = getattr(args, "trigger", None)
    if trigger is not None:
        settings.update(
            recovery=False,
            interval_minutes=None,
            task_done=False,
            role_completed=[],
            states=[],
            check_all=False,
        )
        if trigger == "recovery":
            settings["recovery"] = True
        elif trigger == "task-done":
            settings["task_done"] = True
        elif trigger == "role-completed":
            role = str(getattr(args, "role", "") or "").strip().upper()
            if not role:
                raise ValueError("--role is required for --trigger role-completed")
            settings["role_completed"] = [role]
        elif trigger == "task-state":
            state = str(getattr(args, "state", "") or "").strip().upper()
            if not state:
                raise ValueError("--state is required for --trigger task-state")
            settings["states"] = [state]
        elif trigger in {"interval", "check-all"}:
            minutes = getattr(args, "interval_minutes", None)
            if minutes is None:
                raise ValueError(f"--interval-minutes is required for --trigger {trigger}")
            settings["interval_minutes"] = int(minutes)
            settings["check_all"] = trigger == "check-all"
        elif trigger != "manual":
            raise ValueError(f"unsupported independent trigger: {trigger}")

    teams = parse_dependency_ids(getattr(args, "dependency_team", None))
    if getattr(args, "clear_dependency_teams", False):
        settings["teams"] = []
    elif teams:
        settings["teams"] = list(teams)
    return settings


def _independent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdpa independent",
        description="Create or configure long-lived independent agents with optional durable browser affinity; --browser sets durable browser affinity.",
        epilog=(
            "Independent agents use the shared endpoint catalog, but --browser is a durable browser affinity; use --browser pool to clear it: "
            "a pinned agent waits when that endpoint has no capacity instead of relocating. Default jobs use "
            "Temporary Chat; --persistent-chat selects Persistent reuse. interval, Recovery, task, role, state, "
            "CHECK_ALL, and manual triggers use the same durable agent identity."
        ),
    )
    sub = parser.add_subparsers(dest="operation", required=True)

    create = sub.add_parser("create", help="create an independent agent")
    create.add_argument("--name", required=True)
    create.add_argument("--system-prompt", required=True)
    create.add_argument("--trigger", choices=("manual", "recovery", "task-done", "role-completed", "task-state", "interval", "check-all"), default="manual")
    create.add_argument("--dependency-team", action="append", default=[], help="exact workflow team; repeat or comma-separate")
    create.add_argument("--role")
    create.add_argument("--state")
    create.add_argument("--interval-minutes", type=int)
    create.add_argument("--max-cycles", type=int, default=0)
    create.add_argument("--persistent-chat", action="store_true", help="use Persistent chat reuse; default is a fresh Temporary Chat per job")
    create.add_argument("--browser", help="pin this independent agent to one browser endpoint ID, port, host:port, or CDP URL; use 'pool' for Pool default")
    create.add_argument("--repository", default=".")
    create.add_argument("--config", default=None)

    configure = sub.add_parser("config", help="update an existing independent agent")
    configure.add_argument("task_id")
    configure.add_argument("--dependency-team", action="append", default=[], help="replace dependency teams; repeat or comma-separate")
    configure.add_argument("--clear-dependency-teams", action="store_true")
    configure.add_argument("--trigger", choices=("manual", "recovery", "task-done", "role-completed", "task-state", "interval", "check-all"))
    configure.add_argument("--role")
    configure.add_argument("--state")
    configure.add_argument("--interval-minutes", type=int)
    configure.add_argument("--display-name")
    configure.add_argument("--system-prompt")
    configure.add_argument("--max-cycles", type=int)
    configure.add_argument("--enable", action="store_true")
    configure.add_argument("--disable", action="store_true")
    configure.add_argument("--browser", help="pin this independent agent to one browser endpoint ID, port, host:port, or CDP URL; use 'pool' to clear affinity")
    configure.add_argument("--repository", default=".")
    configure.add_argument("--config", default=None)
    return parser


def _run_independent_cli(raw: Sequence[str]) -> int:
    args = _independent_parser().parse_args(list(raw))
    repository = Path(args.repository).expanduser().resolve()
    config = load_cdpa_config(args.config, repository_root=repository)
    if args.operation == "create":
        payload = {
            "name": args.name,
            "system_prompt": args.system_prompt,
            "mode": "Independent",
            "trigger_settings": _independent_trigger_settings(args),
            "max_cycles": args.max_cycles,
            "temporary_chat": not args.persistent_chat,
        }
        if args.browser is not None:
            payload["browser_endpoint_id"] = (
                None if str(args.browser).strip().casefold() == "pool" else args.browser
            )
        result = _post(config, "/api/independent-agents", payload, idempotency_key=str(uuid.uuid4()))
    else:
        if args.enable and args.disable:
            raise ValueError("--enable and --disable are mutually exclusive")
        detail = _read_json(f"{_api_base_url(config)}/api/tasks/{args.task_id}", timeout=10)
        agent = detail.get("agent") if isinstance(detail.get("agent"), Mapping) else {}
        current = agent.get("trigger_settings") if isinstance(agent.get("trigger_settings"), Mapping) else {}
        payload: dict[str, Any] = {}
        if args.trigger is not None or args.dependency_team or args.clear_dependency_teams:
            payload["trigger_settings"] = _independent_trigger_settings(args, base=current)
        if args.display_name is not None:
            payload["display_name"] = args.display_name
        if args.system_prompt is not None:
            payload["system_prompt"] = args.system_prompt
        if args.max_cycles is not None:
            payload["max_cycles"] = args.max_cycles
        if args.enable or args.disable:
            payload["enabled"] = args.enable
        if args.browser is not None:
            payload["browser_endpoint_id"] = (
                None if str(args.browser).strip().casefold() == "pool" else args.browser
            )
        if not payload:
            raise ValueError("no independent agent setting was requested")
        result = _post(
            config,
            f"/api/independent-agents/{args.task_id}/settings",
            payload,
            idempotency_key=str(uuid.uuid4()),
        )
    for key in ("command_id", "task_id", "status"):
        if result.get(key) is not None:
            print(f"{key}={result[key]}")
    return 0


class CDPASupervisor:
    def __init__(self, *, state_path: Path, probe, restart, activate_monitor, sleep=time.sleep, clock=time.time, restart_backoff_seconds: float = 10.0, verify_attempts: int = 3):
        self.state_path = Path(state_path)
        self.probe = probe
        self.restart = restart
        self.activate_monitor = activate_monitor
        self.sleep = sleep
        self.clock = clock
        self.restart_backoff_seconds = max(1.0, float(restart_backoff_seconds))
        self.verify_attempts = max(1, int(verify_attempts))
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping) and isinstance(value.get("incidents"), Mapping):
                return {"incidents": dict(value["incidents"])}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return {"incidents": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _open_incident(self, service: str, error: str | None = None) -> dict[str, Any]:
        incidents = self.state["incidents"]
        current = next((item for item in incidents.values() if item.get("service") == service and not item.get("recovered_at")), None)
        if current is not None:
            current["last_seen_at"] = self.clock()
            current["last_error"] = error
            self._save()
            return current
        key = f"{service}:{uuid.uuid4()}"
        incident = {"key": key, "service": service, "first_seen_at": self.clock(), "last_seen_at": self.clock(), "restart_attempts": 0, "next_restart_at": 0.0, "recovered_at": None, "last_error": error, "monitor_activated": False, "monitor_activation_attempts": 0}
        incidents[key] = incident
        self._save()
        return incident

    @staticmethod
    def _healthy(snapshot: Mapping[str, Any]) -> bool:
        return bool(snapshot.get("api") and snapshot.get("dashboard") and snapshot.get("worker_online"))

    def _mark_recovered(self, incident: dict[str, Any]) -> None:
        incident["recovered_at"] = self.clock()
        self._save()

    def tick(self) -> None:
        snapshot = dict(self.probe())
        failures = []
        if not snapshot.get("api"):
            failures.append("api")
        if not snapshot.get("dashboard"):
            failures.append("dashboard")
        if snapshot.get("api") and not snapshot.get("worker_online"):
            failures.append("worker")
        if not failures:
            changed = False
            for incident in self.state["incidents"].values():
                if not incident.get("recovered_at"):
                    incident["recovered_at"] = self.clock()
                    changed = True
            if changed:
                self._save()
            self._activate_recovered(snapshot)
            return
        for service in failures:
            incident = self._open_incident(service)
            now = self.clock()
            if now < float(incident.get("next_restart_at") or 0.0):
                continue
            attempts = int(incident.get("restart_attempts") or 0) + 1
            incident["restart_attempts"] = attempts
            incident["next_restart_at"] = now + self.restart_backoff_seconds * min(2 ** (attempts - 1), 8)
            self._save()
            if not self.restart(service):
                continue
            before_heartbeat = snapshot.get("worker_heartbeat_at")
            for _ in range(self.verify_attempts):
                self.sleep(0.2)
                verified = dict(self.probe())
                service_ok = verified.get("api") if service == "api" else verified.get("dashboard") if service == "dashboard" else bool(verified.get("worker_online") and verified.get("worker_heartbeat_at") != before_heartbeat)
                if service_ok:
                    self._mark_recovered(incident)
                    snapshot = verified
                    break
        self._activate_recovered(snapshot)

    def _activate_recovered(self, snapshot: Mapping[str, Any]) -> None:
        if not self._healthy(snapshot):
            return
        for incident in self.state["incidents"].values():
            if incident.get("recovered_at") and not incident.get("monitor_activated"):
                instruction = f"Operational incident recovered: {incident['service']}. Verify downstream CDPA flow and trigger existing repair mechanisms only if needed."
                attempt = int(incident.get("monitor_activation_attempts") or 0) + 1
                attempt_key = f"{incident['key']}:activation:{attempt}"
                outcome = self.activate_monitor(instruction, attempt_key)
                if outcome is True or outcome == "applied":
                    incident["monitor_activation_attempts"] = attempt
                    incident["monitor_activated"] = True
                    self._save()
                elif outcome is False or outcome == "failed":
                    incident["monitor_activation_attempts"] = attempt
                    self._save()


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


def _restart_pm2_service(repository: Path, service: str) -> bool:
    names = {"api": "playwright-dashboard-api", "dashboard": "playwright-dashboard", "worker": "playwright-cdpa-worker"}
    executable = shutil.which("pm2.cmd" if os.name == "nt" else "pm2") or shutil.which("pm2")
    if executable is None:
        return False
    try:
        if os.name == "nt" and service == "worker":
            worker = RuntimeDB(repository / ".runtime" / "cdpa-control.sqlite3").get_snapshot("worker") or {}
            old_pid = int((worker.get("payload") or {}).get("pid") or 0)
            stopped = subprocess.run([executable, "stop", names[service]], cwd=repository, timeout=15, check=False)
            if stopped.returncode != 0:
                return False
            if old_pid:
                subprocess.run(["taskkill", "/PID", str(old_pid), "/T", "/F"], cwd=repository, timeout=15, check=False)
        result = subprocess.run([executable, "restart", names[service]], cwd=repository, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0


def _supervisor_probe(config: CDPAConfig) -> dict[str, Any]:
    result = {"api": False, "dashboard": False, "worker_online": False, "worker_heartbeat_at": None}
    try:
        api = _read_json(f"{_api_base_url(config)}/health", timeout=1.0)
        result.update(api=True, worker_online=bool(api.get("worker_online")), worker_heartbeat_at=api.get("worker_heartbeat_at"))
    except RuntimeError:
        pass
    try:
        _read_json(f"{config.dashboard_url}/health", timeout=1.0)
        result["dashboard"] = True
    except RuntimeError:
        pass
    return result


def _run_monitor(config: CDPAConfig, task_id: str, instruction: str, incident_key: str) -> str:
    try:
        command = _post(
            config,
            f"/api/independent-agents/{task_id}/run",
            {"trigger_type": "manual", "instruction": instruction},
            idempotency_key=str(uuid.uuid5(uuid.NAMESPACE_URL, f"cdpa-supervisor:{incident_key}")),
        )
    except RuntimeError:
        return "pending"
    command_id = str(command.get("command_id") or "")
    for attempt in range(6):
        status = str(command.get("status") or "").lower()
        if status == "applied":
            return "applied"
        if status in {"failed", "recovery_required"}:
            return "failed"
        if status not in {"queued", "running"} or not command_id:
            return "failed"
        if attempt == 5:
            break
        time.sleep(0.2)
        try:
            command = _read_json(
                f"{_api_base_url(config)}/api/commands/{command_id}", timeout=1.0
            )
        except RuntimeError:
            return "pending"
    return "pending"


def _activate_monitor(config: CDPAConfig, instruction: str, incident_key: str) -> str:
    try:
        agents = _read_json(f"{_api_base_url(config)}/api/agents", timeout=2.0)
    except RuntimeError:
        return "pending"
    items = agents.get("independent") if isinstance(agents, Mapping) else None
    monitor = next(
        (
            item
            for item in items or ()
            if isinstance(item, Mapping)
            and (
                str(item.get("agent_key") or "").casefold() == "monitor"
                or str(item.get("identity_name") or "").casefold() == "monitor"
            )
        ),
        None,
    )
    if not isinstance(monitor, Mapping) or not monitor.get("task_id"):
        return "ineligible"
    if monitor.get("enabled") is not True or str(monitor.get("status") or "").upper() != "WAITING":
        return "ineligible"
    return _run_monitor(config, str(monitor["task_id"]), instruction, incident_key)


def supervisor_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the external CDPA liveness supervisor")
    parser.add_argument("--repository", default=str(Path.cwd()))
    parser.add_argument("--config", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    repository = Path(args.repository).expanduser().resolve()
    config = load_cdpa_config(args.config, repository_root=repository)
    supervisor = CDPASupervisor(state_path=repository / ".runtime" / "cdpa-supervisor.json", probe=lambda: _supervisor_probe(config), restart=lambda service: _restart_pm2_service(repository, service), activate_monitor=lambda instruction, key: _activate_monitor(config, instruction, key), sleep=time.sleep, verify_attempts=max(3, int(config.worker_stale_seconds // 0.5) + 4))
    while True:
        supervisor.tick()
        if args.once:
            return 0
        time.sleep(max(1.0, min(config.heartbeat_seconds, 5.0)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or resume a durable CDPA task on a shared browser endpoint pool",
        epilog=(
            "Workflow --browser is an initial placement preference; safe pre-Send work may use another endpoint, "
            "while accepted or ambiguous Send stays bound to its exact execution. Dependencies are global across "
            "browser endpoints. --temp forces Temporary Chat with Fresh context; Persistent workflows may use a "
            "bootstrap. Endpoint rate-limit scope can group browsers into one cooldown domain. Supported dashboard/API "
            "controls include Resume, Change goal, Route PLAN, Retry for structured retryable blocks, Pause, Stop, "
            "Restart role, New chat, Open tab, and Clear team. Run `cdpa` or `cdpa start` for the runtime and `cdpa ui` "
            "to reopen the dashboard."
        ),
    )
    parser.add_argument("task", nargs="?", help="complete task text; omit to resume --team")
    parser.add_argument("--task", dest="task_option", help="complete task text (explicit form)")
    parser.add_argument("--new", dest="new_roles", help="comma-separated roles reset lazily once")
    parser.add_argument("--new-all", action="store_true", help="reset every selected role lazily once")
    parser.add_argument("--team", default=None, help="new-task team base or exact team to resume")
    parser.add_argument(
        "--solo",
        metavar="ROLE",
        default=None,
        help="create a Solo Task using exactly this existing workflow role",
    )
    parser.add_argument("--goal", default=None, help="optional Solo Task goal")
    parser.add_argument(
        "--conversation-url",
        default=None,
        help="optional exact ChatGPT /c/... URL to adopt for a Solo Task",
    )
    bootstrap = parser.add_mutually_exclusive_group()
    bootstrap.add_argument(
        "--bootstrap",
        dest="bootstrap_id",
        default=_BOOTSTRAP_UNSET,
        help="explicit bootstrap ID for a new task",
    )
    bootstrap.add_argument(
        "--fresh",
        dest="bootstrap_id",
        action="store_const",
        const=None,
        help="start a new task with Fresh context instead of a bootstrap",
    )
    parser.add_argument(
        "--temp",
        action="store_true",
        help="force Temporary Chat with Fresh context for this workflow task",
    )
    parser.add_argument(
        "--browser",
        default=None,
        help="initial placement preference: browser endpoint ID, port, host:port, or CDP URL",
    )
    parser.add_argument(
        "--require-browser",
        default=None,
        help="hard browser requirement for this workflow; waits instead of falling back before Send",
    )
    parser.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="global dependency task ID across browser endpoints; repeat or separate values with commas",
    )
    parser.add_argument(
        "--upload",
        action="append",
        default=[],
        help="file context to upload; repeat for multiple files",
    )
    parser.add_argument("--repository", default=None, help="task repository/worktree")
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
        if raw[0] == "independent":
            return _run_independent_cli(raw[1:])

        args = build_parser().parse_args(raw)
        repository = (
            Path(args.repository).expanduser().resolve()
            if args.repository is not None
            else None
        )
        control_repository = repository or Path.cwd().resolve()
        config = load_cdpa_config(args.config, repository_root=control_repository)
        if args.task and args.task_option:
            raise ValueError("provide task text either positionally or with --task, not both")
        task = str(args.task_option or args.task or "").strip()
        new_roles = parse_new_roles(args.new_roles)
        dependencies = parse_dependency_ids(args.depends_on)
        existing_solo = bool(args.solo and args.conversation_url)
        if task or existing_solo:
            if args.solo and (
                new_roles
                or args.new_all
                or args.temp
                or args.require_browser is not None
                or dependencies
                or args.upload
                or args.bootstrap_id is not _BOOTSTRAP_UNSET
            ):
                raise ValueError(
                    "Solo Task accepts --team, --solo, --goal, --browser, "
                    "--conversation-url, and --repository only"
                )
            if not args.solo and (
                args.goal is not None or args.conversation_url is not None
            ):
                raise ValueError("--goal and --conversation-url require --solo ROLE")
            state = submit_task(
                config,
                task=task,
                repository=repository,
                team=args.team,
                new_roles=new_roles,
                new_all=bool(args.new_all),
                temporary=bool(args.temp),
                browser_endpoint_id=args.browser,
                required_browser_endpoint_id=args.require_browser,
                depends_on_task_ids=dependencies,
                upload_paths=tuple(
                    str(Path(path).expanduser().resolve()) for path in args.upload
                ),
                bootstrap_id=args.bootstrap_id,
                workflow_mode="solo" if args.solo else "team",
                solo_role=args.solo,
                goal=args.goal,
                conversation_url=args.conversation_url,
            )
            mode = "created"
        else:
            if not args.team:
                raise ValueError("taskless resume requires --team <exact-existing-team>")
            if args.bootstrap_id is not _BOOTSTRAP_UNSET:
                raise ValueError("--bootstrap and --fresh are invalid in taskless resume mode")
            if (
                args.temp
                or args.browser is not None
                or args.require_browser is not None
                or args.solo is not None
                or args.goal is not None
                or args.conversation_url is not None
            ):
                raise ValueError(
                    "--temp, --browser, and --require-browser are invalid in taskless resume mode; "
                    "--solo, --goal, and --conversation-url are also invalid"
                )
            if new_roles or args.new_all or dependencies or args.upload:
                raise ValueError(
                    "--new, --new-all, --depends-on, and --upload are invalid in taskless resume mode"
                )
            state = resume_task(
                config,
                repository=control_repository,
                team=str(args.team),
            )
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
