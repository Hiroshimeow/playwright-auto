from __future__ import annotations

from pathlib import Path

import playwright_auto.cdpa_cli as cdpa_cli_module
from playwright_auto.cdpa_cli import main as cdpa_main


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
