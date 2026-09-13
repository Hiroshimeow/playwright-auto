from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import playwright_auto.cdpa_managed_browser as managed
from test_cdpa_managed_browser import _setup


@pytest.mark.parametrize("profile", ["", ".", "relative-profile"])
def test_profile_creation_requires_explicit_absolute_path(tmp_path, profile):
    config, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        managed.ManagedBrowserLifecycle(config).prepare_profile(profile_path=profile)


def test_clone_cannot_recurse_into_source_or_its_parent(tmp_path, monkeypatch):
    config, registry = _setup(tmp_path)
    source = tmp_path / 'source'
    source.mkdir()
    (source/'marker').write_text('untouched')
    record = registry.add('9317', management_mode='managed-local', profile_path=str(source))
    lifecycle = managed.ManagedBrowserLifecycle(config)
    copy = Mock()
    monkeypatch.setattr(managed.shutil, 'copytree', copy)
    with pytest.raises(ValueError, match="overlap"):
        lifecycle.prepare_profile(profile_path=str(source/'child'), clone_from=record)
    copy.assert_not_called()
    assert not (source/'child').exists()
    assert (source/'marker').read_text() == 'untouched'


def test_start_rejects_remote_address_even_if_called_without_registry(tmp_path):
    config, _ = _setup(tmp_path)
    popen = Mock()
    lifecycle = managed.ManagedBrowserLifecycle(config, popen_factory=popen)
    with pytest.raises(ValueError, match='local|loopback'):
        lifecycle.start({'endpoint_id':'bad', 'management_mode':'managed-local', 'cdp_url':'http://remote.invalid:9222', 'profile_path':str(tmp_path/'profile')})
    popen.assert_not_called()
    assert not (tmp_path/'profile').exists()


def test_occupied_port_is_not_adopted_or_terminated(tmp_path):
    config, registry = _setup(tmp_path)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        listener.listen()
        port = listener.getsockname()[1]
        record = registry.add(str(port), management_mode='managed-local', profile_path=str(tmp_path/'profile'))
        popen = Mock()
        lifecycle = managed.ManagedBrowserLifecycle(config, popen_factory=popen)
        with pytest.raises(ValueError, match='occupied|already'):
            lifecycle.start(record)
        popen.assert_not_called()
        assert listener.fileno() >= 0


def test_legacy_pid_file_without_birth_receipt_cannot_kill_foreign_process(tmp_path):
    config, registry = _setup(tmp_path)
    endpoint = registry.add('9347', management_mode='managed-local', profile_path=str(tmp_path/'profile'))
    killed = []
    lifecycle = managed.ManagedBrowserLifecycle(config, pid_alive=lambda pid: True, terminate_pid=lambda *args: killed.append(args))
    pidfile = lifecycle.pid_file(endpoint['endpoint_id'])
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text('12345')
    with pytest.raises(ValueError, match='ownership|receipt'):
        lifecycle.stop(endpoint)
    assert not killed
    assert pidfile.exists()


def test_reused_pid_cannot_stop_an_unrelated_process(tmp_path, monkeypatch):
    config, registry = _setup(tmp_path)
    endpoint = registry.add('9367', management_mode='managed-local', profile_path=str(tmp_path/'profile'), executable_path=str(Path(sys.executable).resolve()))
    token = ['first-process']
    killed = []
    monkeypatch.setattr(managed, '_endpoint_online', lambda address: False)
    monkeypatch.setattr(managed, '_profile_in_use', lambda path: False)
    lifecycle = managed.ManagedBrowserLifecycle(config, popen_factory=lambda *args, **kwargs: SimpleNamespace(pid=4567),
        pid_alive=lambda pid: True, process_token=lambda pid: token[0], terminate_pid=lambda *args: killed.append(args))
    assert lifecycle.start(endpoint) == 4567
    token[0] = 'different-process'
    with pytest.raises(ValueError, match='ownership|reused'):
        lifecycle.stop(endpoint)
    assert not killed


def test_profile_used_by_unmanaged_browser_is_not_cloned(tmp_path, monkeypatch):
    config, registry = _setup(tmp_path)
    source = tmp_path/'source'
    source.mkdir()
    endpoint = registry.add('9387', management_mode='managed-local', profile_path=str(source))
    monkeypatch.setattr(managed, '_profile_in_use', lambda path: True)
    with pytest.raises(ValueError, match='running|in use'):
        managed.ManagedBrowserLifecycle(config).prepare_profile(profile_path=str(tmp_path/'clone'), clone_from=endpoint)
    assert not (tmp_path/'clone').exists()


def test_legacy_browser_pid_health_is_non_destructive(tmp_path):
    from playwright_auto.browser import read_pid
    child = subprocess.Popen([sys.executable, '-E', '-c', 'import time;time.sleep(30)'])
    pid_file = tmp_path / 'legacy.pid'
    pid_file.write_text(str(child.pid))
    try:
        assert read_pid(SimpleNamespace(pid_file=pid_file)) == child.pid
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.15)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_windows_managed_browser_finds_installed_executable(tmp_path, monkeypatch):
    import os
    if os.name != 'nt':
        pytest.skip('Windows installation paths')
    from playwright_auto.config import resolve_chromium
    exe = tmp_path / 'Google/Chrome/Application/chrome.exe'
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('CHROMIUM_BIN', raising=False)
    monkeypatch.setattr('playwright_auto.config.shutil.which', lambda name: None)
    assert resolve_chromium() == str(exe)


def test_process_identity_is_stable_for_a_live_test_owned_process():
    child = subprocess.Popen([sys.executable,'-E','-c','import time;time.sleep(30)'])
    try:
        first = managed._process_start_token(child.pid)
        assert first
        assert managed._process_start_token(child.pid) == first
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert managed._process_start_token(child.pid) is None
