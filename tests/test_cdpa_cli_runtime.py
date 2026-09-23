from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import playwright_auto.cdpa_cli as cdpa_cli_module
from playwright_auto.cdpa_cli import main as cdpa_main


def test_submit_task_keeps_file_mode_across_workspaces(tmp_path: Path, monkeypatch):
    control_repository = tmp_path / "control"
    execution_repository = tmp_path / "execution"
    control_repository.mkdir()
    execution_repository.mkdir()
    payloads = []

    def fake_post(_config, path, payload, **_kwargs):
        payloads.append((path, payload))
        return {"status": "queued"}

    monkeypatch.setattr(cdpa_cli_module, "_post", fake_post)
    config = SimpleNamespace(repository_root=control_repository.resolve())

    cdpa_cli_module.submit_task(
        config,
        task="cross workspace",
        repository=execution_repository,
        team="cross",
        new_roles=(),
        new_all=False,
    )
    cdpa_cli_module.submit_task(
        config,
        task="same workspace",
        repository=control_repository,
        team="same",
        new_roles=(),
        new_all=False,
    )
    cdpa_cli_module.submit_task(
        config,
        task="infer repository",
        repository=None,
        team="infer",
        new_roles=(),
        new_all=False,
    )

    assert payloads[0][0] == "/api/tasks"
    assert payloads[0][1]["report_mode"] == "file"
    assert payloads[1][1]["report_mode"] == "file"
    assert payloads[2][1]["report_mode"] == "file"
    assert "repository" not in payloads[2][1]


def test_direct_intake_submits_one_normal_unpinned_workflow(tmp_path: Path, monkeypatch):
    posted = []

    def fake_post(_config, path, payload, **_kwargs):
        posted.append((path, payload))
        return {"status": "queued"}

    monkeypatch.setattr(cdpa_cli_module, "_post", fake_post)
    config = SimpleNamespace(repository_root=tmp_path.resolve())

    cdpa_cli_module.submit_task(
        config,
        task="Implement clarified request with focused acceptance evidence",
        repository=tmp_path,
        team="direct-intake",
        new_roles=(),
        new_all=False,
    )

    assert len(posted) == 1
    path, payload = posted[0]
    assert path == "/api/tasks"
    assert payload["task"] == "Implement clarified request with focused acceptance evidence"
    assert payload["report_mode"] == "file"
    assert "browser_endpoint_id" not in payload
    assert "task_mode" not in payload
    assert "independent" not in payload


def test_task_cli_preserves_omitted_repository_for_api_inference(tmp_path: Path, monkeypatch):
    captured = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "submit_task",
        lambda _config, **kwargs: captured.append(kwargs) or {"status": "queued"},
    )

    assert cdpa_main(["Title\nRepository /some/repo"]) == 0
    assert captured[-1]["repository"] is None

    explicit = tmp_path / "execution"
    explicit.mkdir()
    assert (
        cdpa_main(
            [
                "Title\nRepository /ignored",
                "--repository",
                str(explicit),
            ]
        )
        == 0
    )
    assert captured[-1]["repository"] == explicit.resolve()


def test_cli_no_longer_accepts_inline_report_flag():
    with pytest.raises(SystemExit):
        cdpa_cli_module.build_parser().parse_args(["task", "--inline-report"])


@pytest.mark.parametrize(
    "args",
    (
        ("task", "--bootstrap", "legacy-bootstrap"),
        ("task", "--fresh"),
    ),
)
def test_task_parser_retires_public_bootstrap_options(args):
    with pytest.raises(SystemExit):
        cdpa_cli_module.build_parser().parse_args(list(args))


def test_submit_task_omits_bootstrap_for_default_fresh_context(tmp_path: Path, monkeypatch):
    payloads = []
    config = SimpleNamespace(repository_root=tmp_path.resolve())
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda _config, path, payload, **_kwargs: payloads.append((path, payload))
        or {"status": "queued"},
    )

    cdpa_cli_module.submit_task(
        config,
        task="fresh default",
        repository=None,
        team=None,
        new_roles=(),
        new_all=False,
    )

    assert payloads[0][0] == "/api/tasks"
    assert "bootstrap_id" not in payloads[0][1]


def test_cdpa_without_arguments_starts_three_service_runtime_for_current_repository(
    tmp_path: Path, monkeypatch
):
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cdpa_cli_module,
        "start_runtime",
        lambda *, repository, config_path, open_ui: captured.update(
            repository=repository, config_path=config_path, open_ui=open_ui
        )
        or 0,
    )

    assert cdpa_main([]) == 0
    assert captured == {
        "repository": tmp_path.resolve(),
        "config_path": None,
        "open_ui": True,
    }


def test_cdpa_start_and_ui_are_reserved_global_commands(tmp_path: Path, monkeypatch):
    started = {}
    opened = {}
    monkeypatch.setattr(
        cdpa_cli_module,
        "start_runtime",
        lambda *, repository, config_path, open_ui: started.update(
            repository=repository, config_path=config_path, open_ui=open_ui
        )
        or 0,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "open_runtime_ui",
        lambda *, repository, config_path: opened.update(
            repository=repository, config_path=config_path
        )
        or 0,
    )

    assert cdpa_main(["start", "--repository", str(tmp_path), "--no-open"]) == 0
    assert started == {
        "repository": tmp_path.resolve(),
        "config_path": None,
        "open_ui": False,
    }
    assert cdpa_main(["ui", "--repository", str(tmp_path)]) == 0
    assert opened == {"repository": tmp_path.resolve(), "config_path": None}


def test_runtime_supervisor_starts_and_stops_api_frontend_worker(
    tmp_path: Path, monkeypatch
):
    processes = []
    commands = []

    class FakeProcess:
        returncode = None

        def __init__(self, command, cwd, **_kwargs):
            commands.append((list(command), Path(cwd)))
            self.terminated = False
            self.signals = []
            processes.append(self)

        def poll(self):
            return None

        def send_signal(self, value):
            self.signals.append(value)
            self.terminated = True

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    def fake_read_json(_url, *, timeout):
        if not processes:
            raise RuntimeError("runtime is not started")
        return {"ok": True}

    monkeypatch.setattr(cdpa_cli_module, "_read_json", fake_read_json)
    monkeypatch.setattr(cdpa_cli_module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        cdpa_cli_module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert (
        cdpa_cli_module.start_runtime(
            repository=tmp_path.resolve(), config_path=None, open_ui=False
        )
        == 130
    )
    assert [command[2] for command, _cwd in commands] == [
        "playwright_auto.dashboard_api",
        "playwright_auto.dashboard",
        "playwright_auto.cdpa_worker",
    ]
    assert all(cwd == tmp_path.resolve() for _command, cwd in commands)
    assert all(process.terminated for process in processes)
    assert all(
        process.signals == [cdpa_cli_module._interrupt_signal()]
        for process in processes
    )


def test_packaged_entrypoint_uses_one_api_service_name():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'playwright-dashboard-api = "playwright_auto.dashboard_api:main"' in pyproject
    assert "cdpa-api =" not in pyproject


def test_independent_cli_create_posts_dependency_teams(tmp_path: Path, monkeypatch):
    posted = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda _config, path, payload, **_kwargs: posted.append((path, payload))
        or {"command_id": "cmd-create", "status": "queued"},
    )

    assert cdpa_main([
        "independent",
        "create",
        "--name",
        "Scoped recovery",
        "--system-prompt",
        "Recover only configured teams.",
        "--trigger",
        "recovery",
        "--dependency-team",
        "team-a,team-b",
        "--browser",
        "9212",
        "--repository",
        str(tmp_path),
    ]) == 0

    assert posted[0][0] == "/api/independent-agents"
    assert posted[0][1]["trigger_settings"]["recovery"] is True
    assert posted[0][1]["trigger_settings"]["teams"] == ["team-a", "team-b"]
    assert posted[0][1]["browser_endpoint_id"] == "9212"


def test_independent_cli_config_merges_dependency_teams(tmp_path: Path, monkeypatch):
    posted = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_read_json",
        lambda _url, *, timeout: {
            "agent": {
                "trigger_settings": {
                    "recovery": True,
                    "interval_minutes": None,
                    "daily_at": None,
                    "task_done": False,
                    "role_completed": [],
                    "teams": ["old-team"],
                    "states": [],
                    "check_all": False,
                }
            }
        },
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda _config, path, payload, **_kwargs: posted.append((path, payload))
        or {"command_id": "cmd-config", "task_id": "agent-scoped-g1", "status": "queued"},
    )

    assert cdpa_main([
        "independent",
        "config",
        "agent-scoped-g1",
        "--dependency-team",
        "new-team",
        "--browser",
        "9212",
        "--repository",
        str(tmp_path),
    ]) == 0

    assert posted[0][0] == "/api/independent-agents/agent-scoped-g1/settings"
    assert posted[0][1]["trigger_settings"]["recovery"] is True
    assert posted[0][1]["trigger_settings"]["teams"] == ["new-team"]
    assert posted[0][1]["browser_endpoint_id"] == "9212"


def test_independent_cli_config_pool_clears_browser_affinity(tmp_path: Path, monkeypatch):
    posted = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_read_json",
        lambda _url, *, timeout: {"agent": {"trigger_settings": {}}},
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda _config, path, payload, **_kwargs: posted.append((path, payload))
        or {"command_id": "cmd-clear", "task_id": "agent-scoped-g1", "status": "queued"},
    )

    assert cdpa_main([
        "independent",
        "config",
        "agent-scoped-g1",
        "--browser",
        "pool",
        "--repository",
        str(tmp_path),
    ]) == 0

    assert posted == [
        (
            "/api/independent-agents/agent-scoped-g1/settings",
            {"browser_endpoint_id": None},
        )
    ]


def test_cli_help_explains_pool_affinity_dependencies_context_and_controls():
    workflow = cdpa_cli_module.build_parser().format_help()
    independent = cdpa_cli_module._independent_parser().format_help()
    for text in ("one browser endpoint", "Temporary", "Fresh context", "Dependencies", "Clear team"):
        assert text in workflow
    for text in ("durable browser affinity", "wait", "Temporary Chat", "Persistent", "interval", "pool"):
        assert text in independent


def test_task_parser_retires_public_reuse_team_option():
    with pytest.raises(SystemExit):
        cdpa_cli_module.build_parser().parse_args(
            ["task", "--reuse-team", "legacy-team"]
        )


def test_task_cli_temp_and_browser_forward_creation_policy(tmp_path: Path, monkeypatch):
    captured = []
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "submit_task",
        lambda _config, **kwargs: captured.append(kwargs) or {"status": "queued"},
    )

    assert cdpa_main([
        "task",
        "--temp",
        "--browser",
        "9202",
    ]) == 0
    assert captured[0]["temporary"] is True
    assert captured[0]["browser_endpoint_id"] == "9202"
    assert "required_browser_endpoint_id" not in captured[0]


def test_submit_task_emits_only_explicit_temp_and_browser_overrides(tmp_path: Path, monkeypatch):
    payloads = []
    config = SimpleNamespace(repository_root=tmp_path.resolve())
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda _config, path, payload, **_kwargs: payloads.append(payload)
        or {"status": "queued"},
    )
    cdpa_cli_module.submit_task(
        config,
        task="placement override",
        repository=None,
        team=None,
        new_roles=(),
        new_all=False,
        temporary=True,
        browser_endpoint_id="100.80.10.4:9202",
        required_browser_endpoint_id="9202",
    )
    assert payloads[0]["temporary"] is True
    assert payloads[0]["browser_endpoint_id"] == "100.80.10.4:9202"
    assert payloads[0]["required_browser_endpoint_id"] == "9202"

    payloads.clear()
    cdpa_cli_module.submit_task(
        config,
        task="inherit defaults",
        repository=None,
        team=None,
        new_roles=(),
        new_all=False,
    )
    assert "temporary" not in payloads[0]
    assert "browser_endpoint_id" not in payloads[0]
    assert "required_browser_endpoint_id" not in payloads[0]


def test_taskless_resume_rejects_temp_and_browser(tmp_path: Path, monkeypatch, capsys):
    config = SimpleNamespace(
        repository_root=tmp_path.resolve(),
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
        dashboard_url="http://127.0.0.1:9224",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cdpa_cli_module,
        "load_cdpa_config",
        lambda _path, *, repository_root: config,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "resume_task",
        lambda *_args, **_kwargs: pytest.fail("resume API must not be called"),
    )
    assert cdpa_main(["--team", "existing", "--temp"]) == 2
    assert "--temp and --browser are invalid in taskless resume mode" in capsys.readouterr().err
    assert cdpa_main(["--team", "existing", "--browser", "9202"]) == 2
    assert "--temp and --browser are invalid in taskless resume mode" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cdpa_cli_module.build_parser().parse_args(
            ["--team", "existing", "--require-browser", "9202"]
        )


def test_windows_worker_pm2_restart_reaps_old_lease_owner(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    class FakeRuntimeDB:
        def __init__(self, _path):
            pass

        def get_snapshot(self, name):
            assert name == "worker"
            return {
                "name": "worker",
                "version": 1,
                "updated_at": "2026-09-16T00:00:00Z",
                "payload": {"pid": 4242},
            }

    monkeypatch.setattr(cdpa_cli_module, "RuntimeDB", FakeRuntimeDB)
    monkeypatch.setattr(cdpa_cli_module.shutil, "which", lambda _name: "pm2.cmd")
    monkeypatch.setattr(
        cdpa_cli_module.subprocess,
        "run",
        lambda args, **_kwargs: calls.append(list(args)) or SimpleNamespace(returncode=0),
    )

    assert cdpa_cli_module._restart_pm2_service(tmp_path, "worker") is True
    assert calls == [
        ["pm2.cmd", "stop", "playwright-cdpa-worker"],
        ["taskkill", "/PID", "4242", "/T", "/F"],
        ["pm2.cmd", "restart", "playwright-cdpa-worker"],
    ]


def test_supervisor_restarts_stale_worker_then_activates_monitor(tmp_path: Path):
    probes = iter([
        {"api": True, "dashboard": True, "worker_online": False, "worker_heartbeat_at": "old"},
        {"api": True, "dashboard": True, "worker_online": True, "worker_heartbeat_at": "new"},
    ])
    restarts = []
    activations = []
    supervisor = cdpa_cli_module.CDPASupervisor(
        state_path=tmp_path / "supervisor.json",
        probe=lambda: next(probes),
        restart=lambda service: restarts.append(service) or True,
        activate_monitor=lambda instruction, key: activations.append((key, instruction)) or True,
        sleep=lambda _seconds: None,
    )
    supervisor.tick()
    assert restarts == ["worker"]
    assert len(activations) == 1
    incident = next(iter(supervisor.state["incidents"].values()))
    assert incident["recovered_at"] is not None


def test_supervisor_defers_monitor_when_restart_does_not_restore_health(tmp_path: Path):
    stale = {"api": True, "dashboard": True, "worker_online": False, "worker_heartbeat_at": "old"}
    activations = []
    supervisor = cdpa_cli_module.CDPASupervisor(
        state_path=tmp_path / "supervisor.json",
        probe=lambda: dict(stale),
        restart=lambda _service: True,
        activate_monitor=lambda instruction, key: activations.append(key) or True,
        sleep=lambda _seconds: None,
        verify_attempts=1,
    )
    supervisor.tick()
    assert activations == []
    assert next(iter(supervisor.state["incidents"].values()))["recovered_at"] is None


def test_supervisor_persists_outage_across_process_restart(tmp_path: Path):
    state_path = tmp_path / "supervisor.json"
    first = cdpa_cli_module.CDPASupervisor(
        state_path=state_path,
        probe=lambda: {"api": False, "dashboard": True, "worker_online": False, "worker_heartbeat_at": None},
        restart=lambda _service: False,
        activate_monitor=lambda _instruction, _key: False,
        sleep=lambda _seconds: None,
        verify_attempts=1,
    )
    first.tick()
    second = cdpa_cli_module.CDPASupervisor(
        state_path=state_path,
        probe=lambda: {"api": True, "dashboard": True, "worker_online": True, "worker_heartbeat_at": "fresh"},
        restart=lambda _service: True,
        activate_monitor=lambda _instruction, _key: True,
        sleep=lambda _seconds: None,
        verify_attempts=1,
    )
    second.tick()
    assert state_path.is_file()
    assert all(item["recovered_at"] is not None for item in second.state["incidents"].values())


def test_supervisor_persists_restart_backoff_and_does_not_thrash(tmp_path: Path):
    now = [100.0]
    restarts = []
    stale = {"api": True, "dashboard": True, "worker_online": False, "worker_heartbeat_at": "old"}
    state_path = tmp_path / "supervisor.json"

    first = cdpa_cli_module.CDPASupervisor(
        state_path=state_path,
        probe=lambda: dict(stale),
        restart=lambda service: restarts.append((service, now[0])) or False,
        activate_monitor=lambda _instruction, _key: False,
        sleep=lambda _seconds: None,
        clock=lambda: now[0],
        restart_backoff_seconds=10.0,
        verify_attempts=1,
    )
    first.tick()
    first.tick()
    assert restarts == [("worker", 100.0)]

    second = cdpa_cli_module.CDPASupervisor(
        state_path=state_path,
        probe=lambda: dict(stale),
        restart=lambda service: restarts.append((service, now[0])) or False,
        activate_monitor=lambda _instruction, _key: False,
        sleep=lambda _seconds: None,
        clock=lambda: now[0],
        restart_backoff_seconds=10.0,
        verify_attempts=1,
    )
    second.tick()
    assert restarts == [("worker", 100.0)]

    now[0] = 110.0
    second.tick()
    assert restarts == [("worker", 100.0), ("worker", 110.0)]


def test_supervisor_does_not_mark_failed_monitor_activation_and_retries_with_new_attempt(tmp_path: Path):
    outcomes = iter(["failed", "applied"])
    activation_keys = []
    healthy = {"api": True, "dashboard": True, "worker_online": True, "worker_heartbeat_at": "fresh"}
    state_path = tmp_path / "supervisor.json"
    state_path.write_text(
        '{"incidents":{"worker:incident":{"key":"worker:incident","service":"worker","restart_attempts":1,"next_restart_at":0,"recovered_at":100.0,"monitor_activated":false}}}',
        encoding="utf-8",
    )
    supervisor = cdpa_cli_module.CDPASupervisor(
        state_path=state_path,
        probe=lambda: dict(healthy),
        restart=lambda _service: True,
        activate_monitor=lambda _instruction, key: activation_keys.append(key) or next(outcomes),
        sleep=lambda _seconds: None,
    )

    supervisor.tick()
    incident = supervisor.state["incidents"]["worker:incident"]
    assert incident["monitor_activated"] is False
    assert incident["monitor_activation_attempts"] == 1

    supervisor.tick()
    assert incident["monitor_activated"] is True
    assert incident["monitor_activation_attempts"] == 2
    assert activation_keys == [
        "worker:incident:activation:1",
        "worker:incident:activation:2",
    ]


def test_run_monitor_requires_applied_command_status(tmp_path: Path, monkeypatch):
    config = SimpleNamespace(
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_post",
        lambda *_args, **_kwargs: {"command_id": "cmd-monitor", "status": "queued"},
    )
    monkeypatch.setattr(
        cdpa_cli_module,
        "_read_json",
        lambda *_args, **_kwargs: {"command_id": "cmd-monitor", "status": "failed", "error": "independent agent already has an active job"},
    )
    monkeypatch.setattr(cdpa_cli_module.time, "sleep", lambda _seconds: None)

    assert cdpa_cli_module._run_monitor(
        config,
        "task-monitor",
        "verify recovery",
        "worker:incident:activation:1",
    ) == "failed"


def test_activate_monitor_uses_immutable_identity_and_waits_for_eligible_state(monkeypatch):
    config = SimpleNamespace(
        dashboard_api_host="127.0.0.1",
        dashboard_api_port=9225,
    )
    projection = {
        "independent": [
            {
                "task_id": "task-monitor",
                "agent_key": "monitor",
                "identity_name": "Monitor",
                "name": "Operations Watch",
                "enabled": True,
                "status": "WAITING",
            }
        ]
    }
    calls = []
    monkeypatch.setattr(cdpa_cli_module, "_read_json", lambda *_args, **_kwargs: projection)
    monkeypatch.setattr(
        cdpa_cli_module,
        "_run_monitor",
        lambda _config, task_id, instruction, key: calls.append((task_id, instruction, key)) or "applied",
    )

    assert cdpa_cli_module._activate_monitor(
        config,
        "verify recovery",
        "worker:incident:activation:1",
    ) == "applied"
    assert calls == [("task-monitor", "verify recovery", "worker:incident:activation:1")]

    projection["independent"][0]["enabled"] = False
    assert cdpa_cli_module._activate_monitor(
        config,
        "verify recovery",
        "worker:incident:activation:2",
    ) == "ineligible"
    assert len(calls) == 1
