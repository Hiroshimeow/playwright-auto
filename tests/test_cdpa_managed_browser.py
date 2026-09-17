from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import time

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


@pytest.mark.parametrize("argv", [
    lambda profile: ["--user-data-dir=" + str(profile)],
    lambda profile: ["--user-data-dir", str(profile)],
])
def test_posix_profile_argv_parser_preserves_whitespace(argv):
    import playwright_auto.cdpa_managed_browser as managed

    profile = r"/home/user/Browser Profiles/work"
    assert managed._profile_arg_from_argv(argv(profile)) == profile


@pytest.mark.parametrize("argv", [
    lambda profile: ["--user-data-dir=" + str(profile)],
    lambda profile: ["--user-data-dir", str(profile)],
])
def test_posix_profile_usage_preserves_whitespace_in_nul_delimited_argv(tmp_path: Path, argv):
    import subprocess
    import sys

    profile = tmp_path / "Browser Profiles" / "work"
    profile.mkdir(parents=True)
    child = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(30)", *argv(profile),
    ])
    try:
        import playwright_auto.cdpa_managed_browser as managed
        if managed.os.name == "nt":
            pytest.skip("POSIX /proc command line inspection")
        assert managed._profile_in_use(profile)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_profile_safety_boundary_is_shared_by_endpoint_ids(tmp_path: Path, monkeypatch):
    config, registry = _setup(tmp_path)
    profile = tmp_path / "shared profile"
    first = registry.add("9202", management_mode="managed-local", profile_path=str(profile))
    second = registry.add("9203", management_mode="managed-local", profile_path=str(profile))
    lifecycle = ManagedBrowserLifecycle(config)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def in_use(_path):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr("playwright_auto.cdpa_managed_browser._profile_in_use", in_use)
    errors = []

    def prepare(record):
        try:
            lifecycle.prepare_profile(profile_path=record["profile_path"])
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=prepare, args=(first,))
    second_thread = threading.Thread(target=prepare, args=(second,))
    first_thread.start()
    assert entered.wait(timeout=5)
    second_thread.start()
    time.sleep(0.15)
    assert len(calls) == 1
    release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert not errors
    assert len(calls) == 2


def test_clone_and_start_profile_preparation_are_serialized(tmp_path: Path, monkeypatch):
    config, registry = _setup(tmp_path)
    source = tmp_path / "source profile"
    source.mkdir()
    (source / "marker").write_text("source", encoding="utf-8")
    record = registry.add("9202", management_mode="managed-local", profile_path=str(source))
    target = tmp_path / "target profile"
    lifecycle = ManagedBrowserLifecycle(config)
    clone_copy_entered = threading.Event()
    release_copy = threading.Event()
    safety_checks = []
    clone_error = []

    monkeypatch.setattr(
        "playwright_auto.cdpa_managed_browser._profile_in_use",
        lambda path: safety_checks.append(path.resolve()) or False,
    )

    def copytree(_source, destination, **_kwargs):
        clone_copy_entered.set()
        assert release_copy.wait(timeout=5)
        return destination

    monkeypatch.setattr("playwright_auto.cdpa_managed_browser.shutil.copytree", copytree)

    def clone():
        try:
            lifecycle.prepare_profile(profile_path=str(target), clone_from=record, source_running=False)
        except BaseException as exc:
            clone_error.append(exc)

    clone_thread = threading.Thread(
        target=clone
    )
    clone_thread.start()
    assert clone_copy_entered.wait(timeout=5)
    assert safety_checks == [target.resolve(), source.resolve()]

    start_thread = threading.Thread(
        target=lambda: lifecycle.prepare_profile(profile_path=str(target))
    )
    start_thread.start()
    time.sleep(0.15)
    assert safety_checks == [target.resolve(), source.resolve()]
    release_copy.set()
    clone_thread.join(timeout=5)
    start_thread.join(timeout=5)
    assert not clone_error
    assert safety_checks == [target.resolve(), source.resolve(), target.resolve()]


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


def test_clone_holds_profile_lock_across_real_start_boundary(tmp_path: Path, monkeypatch):
    import shutil
    import sys

    import playwright_auto.cdpa_managed_browser as managed

    config, registry = _setup(tmp_path)
    source = tmp_path / "clone source"
    source.mkdir()
    (source / "marker").write_text("source", encoding="utf-8")
    target = tmp_path / "clone target"
    source_record = registry.add("9208", management_mode="managed-local", profile_path=str(source))
    target_record = registry.add(
        "9209", management_mode="managed-local", profile_path=str(target), executable_path=sys.executable,
    )
    popen_calls = []
    lifecycle = ManagedBrowserLifecycle(
        config,
        popen_factory=lambda command, **kwargs: popen_calls.append((command, kwargs)) or SimpleNamespace(pid=4242),
        pid_alive=lambda _pid: False,
        process_token=lambda _pid: "start-token",
    )
    clone_entered = threading.Event()
    release_clone = threading.Event()
    real_copytree = shutil.copytree

    monkeypatch.setattr(managed, "_profile_in_use", lambda _path: False)
    monkeypatch.setattr(managed, "_endpoint_online", lambda _address: False)

    def blocking_copytree(source_path, destination, **kwargs):
        clone_entered.set()
        assert release_clone.wait(timeout=5)
        return real_copytree(source_path, destination, **kwargs)

    monkeypatch.setattr(managed.shutil, "copytree", blocking_copytree)
    clone_errors = []
    start_result = []

    def clone():
        try:
            lifecycle.prepare_profile(profile_path=str(target), clone_from=source_record, source_running=False)
        except BaseException as exc:
            clone_errors.append(exc)

    clone_thread = threading.Thread(target=clone)
    clone_thread.start()
    assert clone_entered.wait(timeout=5)
    start_thread = threading.Thread(target=lambda: start_result.append(lifecycle.start(target_record)))
    start_thread.start()
    time.sleep(0.15)
    assert popen_calls == []
    release_clone.set()
    clone_thread.join(timeout=5)
    start_thread.join(timeout=5)
    assert not clone_errors
    assert start_result == [4242]
    assert len(popen_calls) == 1
