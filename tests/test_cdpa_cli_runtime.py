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
    "bootstrap_id",
    ("g8-bootstrap", "thinkbook-bootstrap", "a5docker-bootstrap"),
)
def test_task_cli_forwards_explicit_bootstrap(
    tmp_path: Path, monkeypatch, bootstrap_id: str
):
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

    assert cdpa_main(["task", "--bootstrap", bootstrap_id]) == 0
    assert len(captured) == 1
    assert captured[0]["bootstrap_id"] == bootstrap_id


def test_task_cli_fresh_forwards_explicit_null(tmp_path: Path, monkeypatch):
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

    assert cdpa_main(["task", "--fresh"]) == 0
    assert captured[0]["bootstrap_id"] is None


@pytest.mark.parametrize(
    ("bootstrap_id", "expected"),
    (("g8-bootstrap", "g8-bootstrap"), (None, None)),
)
def test_submit_task_forwards_explicit_bootstrap_selection(
    tmp_path: Path, monkeypatch, bootstrap_id, expected
):
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
        task="explicit bootstrap",
        repository=None,
        team=None,
        new_roles=(),
        new_all=False,
        bootstrap_id=bootstrap_id,
    )

    assert payloads[0][0] == "/api/tasks"
    assert payloads[0][1]["bootstrap_id"] == expected


def test_submit_task_omits_bootstrap_when_cli_selection_is_omitted(
    tmp_path: Path, monkeypatch
):
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
        task="default bootstrap",
        repository=None,
        team=None,
        new_roles=(),
        new_all=False,
    )

    assert payloads[0][0] == "/api/tasks"
    assert "bootstrap_id" not in payloads[0][1]


def test_task_cli_bootstrap_and_fresh_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cdpa_cli_module.build_parser().parse_args(
            ["task", "--bootstrap", "g8-bootstrap", "--fresh"]
        )


@pytest.mark.parametrize(
    "selection",
    (("--bootstrap", "g8-bootstrap"), ("--fresh",)),
)
def test_taskless_resume_rejects_bootstrap_change(
    tmp_path: Path, monkeypatch, capsys, selection: tuple[str, ...]
):
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

    assert cdpa_main(["--team", "existing", *selection]) == 2
    assert "--bootstrap and --fresh are invalid in taskless resume mode" in capsys.readouterr().err


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
        "--repository",
        str(tmp_path),
    ]) == 0

    assert posted[0][0] == "/api/independent-agents"
    assert posted[0][1]["trigger_settings"]["recovery"] is True
    assert posted[0][1]["trigger_settings"]["teams"] == ["team-a", "team-b"]


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
        "--repository",
        str(tmp_path),
    ]) == 0

    assert posted[0][0] == "/api/independent-agents/agent-scoped-g1/settings"
    assert posted[0][1]["trigger_settings"]["recovery"] is True
    assert posted[0][1]["trigger_settings"]["teams"] == ["new-team"]
