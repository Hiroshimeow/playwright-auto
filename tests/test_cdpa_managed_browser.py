from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_endpoints import BrowserEndpointRegistry
from playwright_auto.cdpa_managed_browser import ManagedBrowserLifecycle
from test_cdpa_core import write_config


def _setup(tmp_path: Path):
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    registry = BrowserEndpointRegistry(config)
    return config, registry


def test_managed_local_profile_can_be_created_or_cloned_only_while_source_offline(tmp_path: Path):
    config, registry = _setup(tmp_path)
    source_profile = tmp_path / "source-profile"
    source_profile.mkdir()
    (source_profile / "marker.txt").write_text("copy-me", encoding="utf-8")
    source = registry.add("9202", management_mode="managed-local", profile_path=str(source_profile))
    target_profile = tmp_path / "target-profile"
    lifecycle = ManagedBrowserLifecycle(config)
    cloned = lifecycle.prepare_profile(
        profile_path=str(target_profile), clone_from=source, source_running=False
    )
    assert cloned == target_profile.resolve()
    assert (target_profile / "marker.txt").read_text(encoding="utf-8") == "copy-me"

    with pytest.raises(ValueError, match="running"):
        lifecycle.prepare_profile(
            profile_path=str(tmp_path / "blocked-profile"),
            clone_from=source,
            source_running=True,
        )


def test_managed_browser_start_stop_uses_endpoint_scoped_runtime_files(tmp_path: Path, monkeypatch):
    config, registry = _setup(tmp_path)
    profile = tmp_path / "managed-profile"
    record = registry.add(
        "9202", management_mode="managed-local", profile_path=str(profile),
        executable_path=str(tmp_path / "chrome.exe"),
    )
    (tmp_path / "chrome.exe").touch()
    monkeypatch.setattr("playwright_auto.cdpa_managed_browser._endpoint_online", lambda address: False)
    monkeypatch.setattr("playwright_auto.cdpa_managed_browser._profile_in_use", lambda path: False)
    seen = {}
    class Process:
        pid = 43210

    def popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Process()

    stopped = []
    lifecycle = ManagedBrowserLifecycle(
        config,
        popen_factory=popen,
        pid_alive=lambda pid: pid == 43210,
        process_token=lambda pid: "birth-43210",
        terminate_pid=lambda pid, _start: stopped.append(pid),
    )
    pid = lifecycle.start(record)
    assert pid == 43210
    assert f"--remote-debugging-port=9202" in seen["command"]
    assert f"--user-data-dir={profile.resolve()}" in seen["command"]
    assert lifecycle.pid_file(record["endpoint_id"]).read_text().strip() == "43210"
    assert lifecycle.stop(record) is True
    assert stopped == [43210]
    assert not lifecycle.pid_file(record["endpoint_id"]).exists()


def test_external_endpoint_has_no_process_lifecycle(tmp_path: Path):
    config, registry = _setup(tmp_path)
    record = registry.resolve()
    lifecycle = ManagedBrowserLifecycle(config)
    with pytest.raises(ValueError, match="managed-local"):
        lifecycle.start(record)


def test_pid_health_check_never_terminates_a_process():
    import subprocess
    import sys
    from playwright_auto.cdpa_managed_browser import _pid_alive
    child = subprocess.Popen([sys.executable, "-E", "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_alive(child.pid) is True
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.15)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("receipt_text", ["null", "[]"])
def test_malformed_process_receipt_shape_fails_soft_in_inventory_state(tmp_path: Path, receipt_text: str):
    config, _registry = _setup(tmp_path)
    lifecycle = ManagedBrowserLifecycle(
        config,
        pid_alive=lambda pid: pid == 43210,
        process_token=lambda pid: "birth-43210",
    )
    runtime = lifecycle.runtime_dir("managed-test")
    runtime.mkdir(parents=True)
    lifecycle.pid_file("managed-test").write_text("43210", encoding="utf-8")
    (runtime / "process.json").write_text(receipt_text, encoding="utf-8")
    state = lifecycle.process_state("managed-test")
    assert state["managed_running"] is None
    assert state["process_ownership"] == "unverified"
    assert state["process_error"]
